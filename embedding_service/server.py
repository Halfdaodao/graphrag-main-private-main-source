"""OpenAI-compatible local embedding service for GraphRAG."""

from __future__ import annotations

import os
from collections.abc import Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sentence_transformers import SentenceTransformer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "BAAI/bge-m3"
MODEL_NAME = os.getenv("EMBEDDING_MODEL", str(PROJECT_ROOT / "models" / "bge-m3"))
MODEL_CACHE = Path(os.getenv("HF_HOME", PROJECT_ROOT / "models" / "huggingface"))
model: SentenceTransformer | None = None


class EmbeddingRequest(BaseModel):
    input: str | list[str]
    model: str | None = None
    encoding_format: str | None = None


def get_model() -> SentenceTransformer:
    if model is None:
        raise HTTPException(status_code=503, detail="Embedding model is still loading.")
    return model


@asynccontextmanager
async def lifespan(_: FastAPI):
    global model
    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    model = SentenceTransformer(MODEL_NAME, cache_folder=str(MODEL_CACHE))
    yield
    model = None


app = FastAPI(title="Local BGE-M3 Embedding Service", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, Any]:
    loaded_model = get_model()
    return {
        "status": "ok",
        "model": MODEL_ID,
        "dimension": loaded_model.get_sentence_embedding_dimension(),
    }


@app.get("/v1/models")
def list_models() -> dict[str, list[dict[str, str]]]:
    get_model()
    return {"data": [{"id": MODEL_ID, "object": "model", "owned_by": "local"}]}


@app.post("/v1/embeddings")
def create_embeddings(request: EmbeddingRequest) -> dict[str, Any]:
    inputs: Sequence[str] = [request.input] if isinstance(request.input, str) else request.input
    if not inputs or any(not isinstance(item, str) for item in inputs):
        raise HTTPException(status_code=400, detail="input must be a non-empty string or list of strings.")

    vectors = get_model().encode(
        list(inputs),
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": index, "embedding": vector.tolist()}
            for index, vector in enumerate(vectors)
        ],
        "model": request.model or MODEL_ID,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
        "created": int(time()),
    }
