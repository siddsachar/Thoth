from langchain_community.document_loaders import (
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    TextLoader
)

from langchain_text_splitters import RecursiveCharacterTextSplitter

import logging
import shutil
import os
import pathlib
import json

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

class DocumentLoader(object):
    supported_file_types = {
        ".pdf": PyPDFLoader,
        ".docx": UnstructuredWordDocumentLoader,
        ".doc": UnstructuredWordDocumentLoader,
        ".txt": TextLoader
    }

text_splitter = RecursiveCharacterTextSplitter(
    separators = ["\n\n", "\n", " ", ""],
    chunk_size = 1500,
    chunk_overlap = 150
)

# ── Lazy-loaded singletons (avoids heavy imports in child processes) ────────
_embedding_model = None
_vector_store = None


def get_embedding_model():
    """Return the shared HuggingFaceEmbeddings instance (created on first call)."""
    global _embedding_model
    if _embedding_model is None:
        import io as _io
        import os as _os
        import sys as _sys
        # Suppress the noisy tqdm/safetensors "Loading weights" progress bar
        # that writes ~1200 lines to stderr.
        _os.environ["TQDM_DISABLE"] = "1"
        _old_stderr = _sys.stderr
        _sys.stderr = _io.StringIO()        # swallow progress bar output
        try:
            from langchain_huggingface import HuggingFaceEmbeddings
            logger.info("Loading embedding model Qwen/Qwen3-Embedding-0.6B …")
            _embedding_model = HuggingFaceEmbeddings(model_name="Qwen/Qwen3-Embedding-0.6B")
        finally:
            _sys.stderr = _old_stderr       # restore real stderr
            _os.environ.pop("TQDM_DISABLE", None)
        logger.info("Embedding model loaded")
    return _embedding_model


def get_vector_store():
    """Return the FAISS vector store (loaded/created on first call)."""
    global _vector_store
    if _vector_store is None:
        from langchain_classic.vectorstores import FAISS
        em = get_embedding_model()
        _vector_store = (
            FAISS.load_local(
                str(VECTOR_STORE_DIR),
                embeddings=em,
                allow_dangerous_deserialization=True,
            )
            if VECTOR_STORE_DIR.exists()
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
        # Mark as processed using the display name
        save_processed_file(record_name)
        return

    else:
        raise ValueError(f"Unsupported file type: {file_extension}")
    