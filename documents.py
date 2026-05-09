from langchain_community.document_loaders import (
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    TextLoader
)

# Optional loaders - graceful degradation if deps missing
try:
    from langchain_community.document_loaders import BSHTMLLoader
    _HTML_LOADER = BSHTMLLoader
except Exception:
    _HTML_LOADER = None

try:
    from langchain_community.document_loaders import UnstructuredEPubLoader
    _EPUB_LOADER = UnstructuredEPubLoader
except Exception:
    _EPUB_LOADER = None

from langchain_text_splitters import RecursiveCharacterTextSplitter

import logging
import shutil
import os
import pathlib
import json
from typing import Any

from embedding_config import (
    active_embedding_metadata,
    describe_active_embedding,
    get_embedding_config,
    index_metadata_matches,
    read_index_metadata,
    write_index_metadata,
)
from embedding_providers import get_embedding_provider, release_embedding_resources

logger = logging.getLogger(__name__)

# Store data in %APPDATA%/Thoth (writable even when app is in Program Files)
DATA_DIR = pathlib.Path(os.environ.get("THOTH_DATA_DIR", pathlib.Path.home() / ".thoth"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

PROCESSED_FILES_PATH = DATA_DIR / "processed_files.json"
VECTOR_STORE_DIR = DATA_DIR / "vector_store"

def load_processed_files():
    """Load the set of already processed file paths."""
    if PROCESSED_FILES_PATH.exists():
        with open(PROCESSED_FILES_PATH, "r") as f:
            return set(json.load(f))
    return set()

def save_processed_file(file_path):
    """Add a file to the processed files list."""
    processed = load_processed_files()
    processed.add(file_path)
    with open(PROCESSED_FILES_PATH, "w") as f:
        json.dump(list(processed), f, indent=2)

def is_file_processed(file_path):
    """Check if a file has already been processed."""
    return file_path in load_processed_files()

def clear_processed_files():
    """Clear the processed files list."""
    if PROCESSED_FILES_PATH.exists():
        PROCESSED_FILES_PATH.unlink()

def reset_vector_store():
    """Clear all indexed documents and reinitialize an empty vector store."""
    global _vector_store
    from langchain_classic.vectorstores import FAISS
    clear_processed_files()
    if VECTOR_STORE_DIR.exists():
        shutil.rmtree(VECTOR_STORE_DIR)
    _vector_store = FAISS.from_texts([" "], embedding=get_embedding_model())
    _vector_store.save_local(str(VECTOR_STORE_DIR))
    write_index_metadata(VECTOR_STORE_DIR)


def remove_document(display_name: str) -> bool:
    """Remove a single document from the FAISS vector store and processed list.

    Finds all chunks whose ``metadata["source"]`` matches *display_name*,
    deletes them from the vector store, and removes the entry from the
    processed-files list.  Returns True if anything was removed.
    """
    global _vector_store
    vs = get_vector_store()

    # Find docstore IDs whose source matches this document
    ids_to_delete: list[str] = []
    if hasattr(vs, "docstore") and hasattr(vs.docstore, "_dict"):
        for doc_id, doc in vs.docstore._dict.items():
            if getattr(doc, "metadata", {}).get("source") == display_name:
                ids_to_delete.append(doc_id)

    if ids_to_delete:
        try:
            vs.delete(ids_to_delete)
            vs.save_local(str(VECTOR_STORE_DIR))
        except Exception as exc:
            logger.warning("Failed to delete FAISS chunks for %s: %s", display_name, exc)

    # Remove from processed files list
    processed = load_processed_files()
    if display_name in processed:
        processed.discard(display_name)
        with open(PROCESSED_FILES_PATH, "w") as f:
            json.dump(list(processed), f, indent=2)

    # Remove vault/raw/ copy
    try:
        import wiki_vault
        if wiki_vault.is_enabled():
            raw_file = wiki_vault.get_vault_path() / "raw" / display_name
            if raw_file.exists():
                raw_file.unlink()
    except Exception:
        pass

    return bool(ids_to_delete) or display_name in load_processed_files()


def _load_text_file(path: str):
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            pathlib.Path(path).read_text(encoding=encoding)
            return TextLoader(path, encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    return TextLoader(path, encoding="utf-8")

class DocumentLoader(object):
    supported_file_types = {
        ".pdf": PyPDFLoader,
        ".docx": UnstructuredWordDocumentLoader,
        ".doc": UnstructuredWordDocumentLoader,
        ".txt": _load_text_file,
        ".md": _load_text_file,
    }


# Dynamically add optional loaders if their dependencies are available
if _HTML_LOADER is not None:
    DocumentLoader.supported_file_types[".html"] = _HTML_LOADER
    DocumentLoader.supported_file_types[".htm"] = _HTML_LOADER
if _EPUB_LOADER is not None:
    DocumentLoader.supported_file_types[".epub"] = _EPUB_LOADER

text_splitter = RecursiveCharacterTextSplitter(
    separators = ["\n\n", "\n", " ", ""],
    chunk_size = 1500,
    chunk_overlap = 150
)

# Lazy-loaded singletons (avoids heavy imports in child processes)
import threading as _threading
_embedding_lock = _threading.Lock()
_vector_store = None


def get_embedding_model():
    """Return the configured embedding provider (created on first call)."""
    with _embedding_lock:
        return get_embedding_provider()


def get_vector_store():
    """Return the FAISS vector store (loaded/created on first call)."""
    global _vector_store
    if _vector_store is None:
        from langchain_classic.vectorstores import FAISS
        em = get_embedding_model()
        metadata_ok = VECTOR_STORE_DIR.exists() and index_metadata_matches(VECTOR_STORE_DIR)
        if VECTOR_STORE_DIR.exists() and not metadata_ok:
            logger.warning(
                "Document vector index is stale for active embedding model %s; "
                "new writes will start a compatible index until documents are rebuilt.",
                describe_active_embedding(),
            )
        _vector_store = (
            FAISS.load_local(
                str(VECTOR_STORE_DIR),
                embeddings=em,
                allow_dangerous_deserialization=True,
            )
            if metadata_ok
            else FAISS.from_texts([" "], embedding=em)
        )
    return _vector_store


def load_and_vectorize_document(file_path, skip_if_processed=True, display_name=None):
    record_name = display_name or file_path
    # Skip if already processed
    if skip_if_processed and is_file_processed(record_name):
        logger.info("Skipping already processed file: %s", record_name)
        return
    
    file_extension = pathlib.Path(file_path).suffix
    if file_extension in DocumentLoader.supported_file_types:
        loader_class = DocumentLoader.supported_file_types[file_extension]
        loader = loader_class(file_path)
        document = loader.load()
        documents = [
            doc
            for doc in document
            if isinstance(doc.page_content, str) and doc.page_content.strip()
        ]
        if not documents:
            logger.warning("No valid text content found in: %s", file_path)
            return
        chunks = text_splitter.split_documents(documents)
        # Replace temp file paths with the actual display name in metadata
        if display_name:
            for chunk in chunks:
                chunk.metadata["source"] = display_name
        vs = get_vector_store()
        vs.add_documents(chunks)
        vs.save_local(str(VECTOR_STORE_DIR))
        write_index_metadata(VECTOR_STORE_DIR)
        # Mark as processed using the display name
        save_processed_file(record_name)
        return

    else:
        raise ValueError(f"Unsupported file type: {file_extension}")


def load_document_text(file_path: str) -> tuple[str, str]:
    """Load full text from a document file (no chunking).

    Returns ``(full_text, title)`` where *title* is derived from the
    filename.  Uses the same loader classes as ``DocumentLoader`` but
    joins all pages instead of splitting into chunks.
    """
    p = pathlib.Path(file_path)
    ext = p.suffix.lower()
    if ext not in DocumentLoader.supported_file_types:
        raise ValueError(f"Unsupported file type: {ext}")
    loader_class = DocumentLoader.supported_file_types[ext]
    loader = loader_class(str(p))
    pages = loader.load()
    parts = [
        doc.page_content
        for doc in pages
        if isinstance(doc.page_content, str) and doc.page_content.strip()
    ]
    if not parts:
        raise ValueError(f"No text content found in: {file_path}")
    full_text = "\n\n".join(parts)
    # Strip UTF-16 surrogates that can appear in PDF text extraction -
    # they crash orjson serialisation downstream (NiceGUI socketio emit).
    full_text = full_text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    title = p.stem  # filename without extension
    return full_text, title


def document_vector_status() -> dict[str, Any]:
    """Return display-safe status for the document FAISS index."""
    active = active_embedding_metadata()
    return {
        "exists": VECTOR_STORE_DIR.exists(),
        "stale": VECTOR_STORE_DIR.exists() and not index_metadata_matches(VECTOR_STORE_DIR, active),
        "stored": read_index_metadata(VECTOR_STORE_DIR),
        "active": active,
        "active_label": describe_active_embedding(),
    }


def release_document_embedding_resources(reason: str = "document work complete") -> None:
    """Release cached vector and embedding resources after heavyweight work."""
    global _vector_store
    if reason != "embedding settings changed" and not get_embedding_config().get("auto_unload", True):
        return
    _vector_store = None
    release_embedding_resources(reason)


def rebuild_vector_store_from_vault() -> int:
    """Rebuild the document FAISS index from wiki vault raw document copies."""
    global _vector_store
    from langchain_classic.vectorstores import FAISS

    try:
        import wiki_vault

        raw_dir = wiki_vault.get_vault_path() / "raw"
    except Exception:
        raw_dir = DATA_DIR / "vault" / "raw"
    if not raw_dir.exists():
        raise FileNotFoundError("No vault/raw document copies were found to rebuild from.")
    files = [
        path for path in sorted(raw_dir.iterdir())
        if path.is_file() and path.suffix.lower() in DocumentLoader.supported_file_types
    ]
    if not files:
        raise FileNotFoundError("No supported document files were found in vault/raw.")

    all_chunks = []
    indexed_names: list[str] = []
    for path in files:
        loader = DocumentLoader.supported_file_types[path.suffix.lower()](str(path))
        pages = [
            doc
            for doc in loader.load()
            if isinstance(doc.page_content, str) and doc.page_content.strip()
        ]
        if not pages:
            logger.warning("No valid text content found in vault copy: %s", path)
            continue
        chunks = text_splitter.split_documents(pages)
        for chunk in chunks:
            chunk.metadata["source"] = path.name
        all_chunks.extend(chunks)
        indexed_names.append(path.name)

    if not all_chunks:
        raise ValueError("No valid text content was found in vault/raw documents.")

    tmp_dir = VECTOR_STORE_DIR.with_name(f"{VECTOR_STORE_DIR.name}_rebuild_tmp")
    backup_dir = VECTOR_STORE_DIR.with_name(f"{VECTOR_STORE_DIR.name}_rebuild_backup")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    if backup_dir.exists():
        shutil.rmtree(backup_dir)

    vs = FAISS.from_documents(all_chunks, embedding=get_embedding_model())
    vs.save_local(str(tmp_dir))
    write_index_metadata(tmp_dir)

    try:
        if VECTOR_STORE_DIR.exists():
            shutil.move(str(VECTOR_STORE_DIR), str(backup_dir))
        shutil.move(str(tmp_dir), str(VECTOR_STORE_DIR))
        PROCESSED_FILES_PATH.write_text(json.dumps(indexed_names, indent=2), encoding="utf-8")
        _vector_store = vs
    except Exception:
        logger.exception("Failed to swap rebuilt document vector store into place")
        if VECTOR_STORE_DIR.exists():
            shutil.rmtree(VECTOR_STORE_DIR, ignore_errors=True)
        if backup_dir.exists():
            shutil.move(str(backup_dir), str(VECTOR_STORE_DIR))
        raise
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        release_document_embedding_resources("document vector rebuild")

    logger.info(
        "Rebuilt document vector store with %d document(s), %d chunk(s)",
        len(indexed_names),
        len(all_chunks),
    )
    return len(indexed_names)
