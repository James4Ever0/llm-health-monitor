"""Async health checker — polls all endpoints concurrently.

Runs as a background asyncio task so it shares the event loop with the
FastAPI web server.  Uses httpx for non-blocking HTTP and aiosqlite for
non-blocking DB writes.

Each endpoint can override timeout_seconds and interval_seconds via
check_override.  The loop tracks per-endpoint schedules so a slow endpoint
never blocks others, and each endpoint is checked on its own interval.

Supports three endpoint types:
  - llm        -> POST /v1/chat/completions
  - embedding  -> POST /v1/embeddings
  - rerank     -> POST /v1/rerank
"""

import asyncio
import logging
import time
import uuid
from typing import Any

import httpx

import db
import alerts
from config import load_config

logger = logging.getLogger("llm_monitor.checker")

# Load and validate config once at import time
_CONFIG = load_config()

# LLM defaults are used as the global fallback when an endpoint config object
# is missing (should not happen in normal operation).
_LLM_CFG = _CONFIG.check.llm
_DEFAULT_PROMPT = _LLM_CFG.prompt_request
_DEFAULT_EXPECTED = _LLM_CFG.prompt_expected
_DEFAULT_TEMP = _LLM_CFG.temperature
_DEFAULT_TOP_P = _LLM_CFG.top_p
_DEFAULT_TOP_K = _LLM_CFG.top_k
_DEFAULT_MAX_TOKENS = _LLM_CFG.max_tokens
_DEFAULT_ASSERT = _LLM_CFG.assert_response
_DEFAULT_TIMEOUT = _LLM_CFG.timeout_seconds
_DEFAULT_INTERVAL = _LLM_CFG.interval_seconds
_DEFAULT_RANDOM_PREFIX = _LLM_CFG.random_prefix
_DEFAULT_CONCURRENCY = _LLM_CFG.concurrency

# Fast lookup: monitor_id -> typed EndpointConfig
_CFG_BY_MONITOR: dict[str, Any] = {e.monitor_id: e for e in _CONFIG.endpoints}

# Shared async HTTP client (created in lifespan, closed on shutdown)
_http_client: httpx.AsyncClient | None = None


def _build_llm_payload(model: str, eff: dict[str, Any]) -> dict[str, Any]:
    """Build the chat completions payload from a resolved effective config."""
    prompt = eff["prompt_request"]
    temperature = eff["temperature"]
    top_p = eff["top_p"]
    top_k = eff["top_k"]
    max_tokens = eff["max_tokens"]

    if eff.get("random_prefix"):
        prompt = f"trace-id: {uuid.uuid4()}\n{prompt}"

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
    }

    if top_k and top_k > 0:
        payload["top_k"] = top_k

    return payload


def _build_embedding_payload(model: str, eff: dict[str, Any]) -> dict[str, Any]:
    """Build the embeddings payload from a resolved effective config."""
    input_text = eff["input"]
    if eff.get("random_prefix"):
        input_text = f"trace-id: {uuid.uuid4()}\n{input_text}"
    return {
        "model": model,
        "input": input_text,
    }


def _build_rerank_payload(model: str, eff: dict[str, Any]) -> dict[str, Any]:
    """Build the rerank payload from a resolved effective config."""
    query = eff["query"]
    documents = eff["documents"]
    if eff.get("random_prefix"):
        query = f"trace-id: {uuid.uuid4()}\n{query}"
    return {
        "model": model,
        "query": query,
        "documents": documents,
    }


def _effective_or_fallback(endpoint_type: str, cfg_ep: Any | None) -> dict[str, Any]:
    """Return the resolved effective config, falling back to LLM defaults."""
    if cfg_ep is None:
        return _CONFIG.check.llm.model_dump()
    return cfg_ep.effective(_CONFIG.check)


async def _check_one(endpoint: dict) -> None:
    """Perform a single health check against one endpoint."""
    ep_id = endpoint["id"]
    ep_name = endpoint["name"]
    ep_monitor_id = endpoint.get("monitor_id", "unknown")
    endpoint_type = endpoint.get("endpoint_type", "llm")
    base_url = endpoint["base_url"].rstrip("/")
    api_key = endpoint["api_key"]

    cfg_ep = _CFG_BY_MONITOR.get(ep_monitor_id)
    eff = _effective_or_fallback(endpoint_type, cfg_ep)

    assert_response = eff.get("assert_response", _DEFAULT_ASSERT)
    timeout = eff.get("timeout_seconds", _DEFAULT_TIMEOUT)

    headers: dict[str, str] = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    model = cfg_ep.model if cfg_ep else endpoint.get("model", "")

    # Build URL and payload based on endpoint type
    if endpoint_type == "embedding":
        url = f"{base_url}/embeddings"
        payload = _build_embedding_payload(model, eff)
    elif endpoint_type == "rerank":
        url = f"{base_url}/rerank"
        payload = _build_rerank_payload(model, eff)
    else:
        url = f"{base_url}/chat/completions"
        payload = _build_llm_payload(model, eff)

    start = time.perf_counter()
    latency_ms: float | None = None
    status = "ok"
    response_text: str | None = None
    request_body_json = ""
    response_body_json = ""
    alert_triggered = False

    try:
        request_body_json = str(payload)
        resp = await _http_client.post(
            url,
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        latency_ms = (time.perf_counter() - start) * 1000
        response_body_json = resp.text[:2000]

        if latency_ms > timeout * 1000:
            status = "timeout"
            response_text = f"slow ({latency_ms:.0f} ms > {timeout * 1000:.0f} ms)"
        elif resp.status_code >= 400:
            status = "error"
            response_text = f"HTTP {resp.status_code}: {resp.text[:200]}"
        else:
            data = resp.json()

            if endpoint_type == "embedding":
                expected_dim = eff.get("expected_dimension")
                embeddings = data.get("data", [])
                if not embeddings:
                    status = "error"
                    response_text = "no embeddings in response"
                elif assert_response and expected_dim is not None:
                    emb = embeddings[0].get("embedding", [])
                    if len(emb) != expected_dim:
                        status = "unexpected"
                        response_text = f"expected dimension {expected_dim}, got {len(emb)}"
                    else:
                        response_text = f"embedding dimension {len(emb)}"
                else:
                    response_text = f"embedding dimension {len(embeddings[0].get('embedding', []))}"

            elif endpoint_type == "rerank":
                expected_idx = eff.get("expected_index")
                results = data.get("results", [])
                if not results:
                    status = "error"
                    response_text = "no rerank results in response"
                elif assert_response and expected_idx is not None:
                    top_idx = results[0].get("index")
                    if top_idx != expected_idx:
                        status = "unexpected"
                        response_text = f"expected top index {expected_idx}, got {top_idx}"
                    else:
                        response_text = f"top rerank index {top_idx}"
                else:
                    response_text = f"top rerank index {results[0].get('index')}"

            else:
                # LLM
                expected = eff.get("prompt_expected", _DEFAULT_EXPECTED)
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    if not content:
                        content = ""
                    content = content.strip()
                    response_text = content
                    if assert_response and content != expected:
                        status = "unexpected"
                        if content == "":
                            response_text = f"[unexpected] model returned EMPTY string (expected '{expected}')"
                        else:
                            response_text = f"[unexpected] expected='{expected}' got='{content}'"
                else:
                    status = "error"
                    response_text = "no choices in response"

    except httpx.TimeoutException:
        latency_ms = (time.perf_counter() - start) * 1000
        status = "timeout"
        response_text = f"request timed out after {timeout}s"
    except httpx.HTTPStatusError as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        status = "error"
        response_text = f"HTTP error: {exc.response.status_code}"
    except Exception as exc:
        latency_ms = (time.perf_counter() - start) * 1000
        status = "error"
        response_text = f"{type(exc).__name__}: {str(exc)[:200]}"

    # Persist check with full request/response bodies
    check_id = await db.insert_check(
        endpoint_id=ep_id,
        latency_ms=latency_ms,
        status=status,
        response_text=response_text,
        request_body=request_body_json,
        response_body=response_body_json,
        alert_triggered=alert_triggered,
    )

    # Alert handling
    if status != "ok":
        alert_triggered = True
        await alerts.trigger_alert(
            endpoint_id=ep_id,
            endpoint_name=ep_name,
            alert_type=status,
            message=response_text or "unknown failure",
            check_id=check_id,
        )
    else:
        await alerts.resolve_alert(ep_id, ep_name)

    logger.info(
        "check %-12s %-20s | %-10s | %6.1f ms | timeout=%ds | assert=%s | type=%s | %s",
        ep_monitor_id,
        ep_name,
        status,
        latency_ms or 0,
        timeout,
        assert_response,
        endpoint_type,
        (response_text or "")[:60],
    )


async def _sync_endpoints() -> None:
    """Upsert endpoints from config.yaml into the database."""
    for ep in _CONFIG.endpoints:
        if not ep.enabled:
            continue
        await db.upsert_endpoint(
            monitor_id=ep.monitor_id,
            name=ep.name,
            base_url=ep.base_url,
            api_key=ep.api_key or "",
            model=ep.model,
            endpoint_type=ep.endpoint_type,
            enabled=True,
            check_override=ep.check_override.model_dump(exclude_none=True) if ep.check_override else {},
        )


async def checker_loop() -> None:
    """Background task: sync endpoints then poll forever.

    Each endpoint is checked on its own interval (default or overridden).
    A slow endpoint never blocks others — they are polled concurrently
    via asyncio.gather.
    """
    global _http_client
    _http_client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )

    try:
        await _sync_endpoints()

        # Track last check time per endpoint (monitor_id -> timestamp)
        last_check: dict[str, float] = {}

        while True:
            endpoints = await db.get_enabled_endpoints()
            if not endpoints:
                logger.warning("No enabled endpoints configured — sleeping")
                await asyncio.sleep(_DEFAULT_INTERVAL)
                continue

            now = time.monotonic()

            # Find endpoints that are due for a check
            due = []
            next_due_at = float("inf")
            for ep in endpoints:
                mid = ep.get("monitor_id", "unknown")
                cfg_ep = _CFG_BY_MONITOR.get(mid)
                endpoint_type = ep.get("endpoint_type", "llm")
                eff = _effective_or_fallback(endpoint_type, cfg_ep)
                interval = eff.get("interval_seconds", _DEFAULT_INTERVAL)
                last = last_check.get(mid, 0)
                if now - last >= interval:
                    due.append(ep)
                else:
                    next_due_at = min(next_due_at, last + interval)

            if due:
                # Fire all due endpoints concurrently.  If an endpoint has
                # concurrency > 1, fire that many independent single-request
                # checks simultaneously — each gets its own DB row and is
                # treated exactly like a check at a different time.
                tasks = []
                for ep in due:
                    mid = ep.get("monitor_id", "unknown")
                    cfg_ep = _CFG_BY_MONITOR.get(mid)
                    endpoint_type = ep.get("endpoint_type", "llm")
                    eff = _effective_or_fallback(endpoint_type, cfg_ep)
                    concurrency = eff.get("concurrency", _DEFAULT_CONCURRENCY)
                    concurrency = max(1, concurrency)
                    tasks.extend(_check_one(ep) for _ in range(concurrency))

                await asyncio.gather(*tasks, return_exceptions=True)
                for ep in due:
                    last_check[ep.get("monitor_id", "unknown")] = time.monotonic()
            else:
                # Sleep until the next endpoint is due
                sleep_for = max(0.1, next_due_at - now)
                await asyncio.sleep(sleep_for)

    finally:
        if _http_client:
            await _http_client.aclose()
