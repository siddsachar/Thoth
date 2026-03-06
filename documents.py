from langchain_community.document_loaders import (
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    TextLoader
)

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_classic.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings
import torch

import shutil
import os
import pathlib
import json

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
    global vector_store
    clear_processed_files()
    if VECTOR_STORE_DIR.exists():
        shutil.rmtree(VECTOR_STORE_DIR)
    vector_store = FAISS.from_texts([" "], embedding=embedding_model)
    vector_store.save_local(str(VECTOR_STORE_DIR))

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

embedding_model = HuggingFaceEmbeddings(
    model_name = "Qwen/Qwen3-Embedding-0.6B"
)

vector_store = (
    FAISS.load_local(
        str(VECTOR_STORE_DIR),
        embeddings=embedding_model,
        allow_dangerous_deserialization=True,
    )
    if VECTOR_STORE_DIR.exists()
    else FAISS.from_texts([" "], embedding=embedding_model)
)

def load_and_vectorize_document(file_path, skip_if_processed=True, display_name=None):
    record_name = display_name or file_path
    # Skip if already processed
    if skip_if_processed and is_file_processed(record_name):
        print(f"Skipping already processed file: {record_name}")
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
            print(f"No valid text content found in: {file_path}")
            return
        chunks = text_splitter.split_documents(documents)
        # Replace temp file paths with the actual display name in metadata
        if display_name:
            for chunk in chunks:
                chunk.metadata["source"] = display_name
        vector_store.add_documents(chunks)
        vector_store.save_local(str(VECTOR_STORE_DIR))
        # Mark as processed using the display name
        save_processed_file(record_name)
        return

    else:
        raise ValueError(f"Unsupported file type: {file_extension}")
    