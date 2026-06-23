"""Pydantic config schema + loader for LLM health monitor.

All config validation happens here.  Bad config fails fast at import time
with a human-readable error.
"""

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator


class CheckOverride(BaseModel):
    """Per-endpoint override — every field is optional.

    The same key names are used across endpoint types so overrides feel
    consistent, even though not every key applies to every endpoint type.
    """

    # LLM-specific
    prompt_request: Optional[str] = None
    prompt_expected: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_tokens: Optional[int] = None

    # Embedding-specific
    input: Optional[str] = None
    expected_dimension: Optional[int] = None

    # Rerank-specific
    query: Optional[str] = None
    documents: Optional[list[str]] = None
    expected_index: Optional[int] = None

    # Shared across all endpoint types
    assert_response: Optional[bool] = None
    timeout_seconds: Optional[int] = None
    interval_seconds: Optional[int] = None
    random_prefix: Optional[bool] = None
    concurrency: Optional[int] = None


class LlmConfig(BaseModel):
    """Global defaults for LLM / chat-completions checks."""

    prompt_request: str
    prompt_expected: str
    temperature: float = 0
    top_p: float = 1.0
    top_k: int = 0
    max_tokens: int = 10
    assert_response: bool = True
    timeout_seconds: int = 10
    interval_seconds: int = 30
    random_prefix: bool = False
    concurrency: int = 1


class EmbeddingConfig(BaseModel):
    """Global defaults for embedding checks."""

    input: str = "Hello world"
    expected_dimension: Optional[int] = None
    assert_response: bool = False
    timeout_seconds: int = 10
    interval_seconds: int = 30
    random_prefix: bool = False
    concurrency: int = 1


class RerankConfig(BaseModel):
    """Global defaults for rerank checks."""

    query: str = "What is the capital of France?"
    documents: list[str] = Field(default_factory=list)
    expected_index: Optional[int] = None
    assert_response: bool = False
    timeout_seconds: int = 10
    interval_seconds: int = 30
    random_prefix: bool = False
    concurrency: int = 1


class CheckConfig(BaseModel):
    """Container for global defaults of all endpoint types."""

    llm: LlmConfig
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    rerank: RerankConfig = Field(default_factory=RerankConfig)


class EndpointConfig(BaseModel):
    """Single endpoint definition."""

    monitor_id: str = Field(..., alias="monitor-id")
    name: str
    base_url: str = Field(..., alias="base_url")
    api_key: Optional[str] = ""
    model: str
    enabled: bool = True
    show: bool = True
    endpoint_type: Literal["llm", "embedding", "rerank"]
    check_override: Optional[CheckOverride] = Field(default=None, alias="check_override")

    def effective(self, defaults: "CheckConfig") -> dict:
        """Resolve this endpoint's config against global defaults for its type."""
        ov = self.check_override
        type_defaults = getattr(defaults, self.endpoint_type)

        def _ov(key: str, fallback):
            if ov is None:
                return fallback
            val = getattr(ov, key)
            return val if val is not None else fallback

        common = {
            "assert_response": _ov("assert_response", type_defaults.assert_response),
            "timeout_seconds": _ov("timeout_seconds", type_defaults.timeout_seconds),
            "interval_seconds": _ov("interval_seconds", type_defaults.interval_seconds),
            "random_prefix": _ov("random_prefix", type_defaults.random_prefix),
            "concurrency": _ov("concurrency", type_defaults.concurrency),
        }

        if self.endpoint_type == "embedding":
            return {
                "input": _ov("input", type_defaults.input),
                "expected_dimension": _ov("expected_dimension", type_defaults.expected_dimension),
                **common,
            }

        if self.endpoint_type == "rerank":
            return {
                "query": _ov("query", type_defaults.query),
                "documents": _ov("documents", type_defaults.documents),
                "expected_index": _ov("expected_index", type_defaults.expected_index),
                **common,
            }

        # LLM
        return {
            "prompt_request": _ov("prompt_request", type_defaults.prompt_request),
            "prompt_expected": _ov("prompt_expected", type_defaults.prompt_expected),
            "temperature": _ov("temperature", type_defaults.temperature),
            "top_p": _ov("top_p", type_defaults.top_p),
            "top_k": _ov("top_k", type_defaults.top_k),
            "max_tokens": _ov("max_tokens", type_defaults.max_tokens),
            **common,
        }


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 9880
    timezone: str = "UTC"


class AlertsConfig(BaseModel):
    enabled: bool = False
    ntfy_topic: Optional[str] = None


class AppConfig(BaseModel):
    """Root config model — validates the entire config.yaml."""

    endpoints: list[EndpointConfig]
    check: CheckConfig
    server: ServerConfig = Field(default_factory=ServerConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    @model_validator(mode="after")
    def _check_unique_monitor_ids(self):
        """Fail fast if two endpoints share the same monitor-id."""
        seen = set()
        duplicates = []
        for ep in self.endpoints:
            mid = ep.monitor_id
            if mid in seen:
                duplicates.append(mid)
            seen.add(mid)
        if duplicates:
            raise ValueError(f"duplicate monitor-id values found: {sorted(set(duplicates))}")
        return self


def load_config(path: str = "config.yaml") -> AppConfig:
    """Load and validate config.yaml."""
    raw = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(raw)
