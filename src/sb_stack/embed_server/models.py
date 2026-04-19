"""Pydantic models for the OpenAI-compatible embeddings endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class EmbedRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # OpenAI allows string | list[str]; normalise to list on the server.
    input: str | list[str]
    model: str | None = None
    # OpenAI also supports `encoding_format` and `dimensions`; we ignore
    # them for now (`extra="ignore"`).


class EmbeddingItem(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class Usage(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0


class EmbedResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingItem]
    model: str
    usage: Usage = Field(default_factory=Usage)


class ModelEntry(BaseModel):
    id: str
    object: str = "model"
    created: int = 0
    owned_by: str = "sb-stack"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelEntry]
