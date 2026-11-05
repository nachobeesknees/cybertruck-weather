"""The only module that talks to the Claude API.

- model per role from config/models.yaml; prompts hashed for versioning
- spend guard: refuses the call when today's spend (America/Phoenix) is at the cap
- every call (success or failure) logged to agent_runs with tokens, cost, latency
- structured outputs: the response is parsed into the caller's pydantic model; on validation failure
  the model is re-prompted once with the validation errors, then the job fails loudly
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
from pydantic import BaseModel, ValidationError

from argusvue.config import load_models
from argusvue.settings import get_settings
from argusvue.store.blobs import BlobStore

log = logging.getLogger(__name__)
WEB_SEARCH_TOOL = "web_search_20260209"


class LLMError(RuntimeError):
    """The model did not produce a usable answer after the allowed re-prompt."""


class SpendCapExceeded(RuntimeError):
    pass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    search_uses: int = 0


@dataclass
class LLMResult[T: BaseModel]:
    parsed: T
    run_id: int
    model: str
    usage: Usage
    cost_usd: float
    latency_ms: int
    raw_text: str
    attempts: int = 1
    citations: list[dict] = field(default_factory=list)


# ---- pricing / spend -------------------------------------------------------------------------
def model_for(role: str) -> str:
    roles = load_models().roles
    if role not in roles:
        raise KeyError(f"no model configured for role {role!r} (config/models.yaml roles: {sorted(roles)})")
    return roles[role]


def effort_for(role: str) -> str | None:
    return (load_models().effort or {}).get(role)


def cost_of(model: str, usage: Usage) -> float:
    pricing = load_models().pricing
    p = pricing.get(model)
    if p is None:
        raise KeyError(f"no pricing for model {model!r} in config/models.yaml")
    per_m = 1_000_000
    cost = (usage.input_tokens * p["input"] + usage.output_tokens * p["output"]
            + usage.cache_read_tokens * p["cache_read"] + usage.cache_write_tokens * p["cache_write"]) / per_m
    cost += usage.search_uses * pricing.get("web_search_per_1k_usd", 10.0) / 1000
    return round(cost, 6)


def local_today() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


def today_spend(conn: psycopg.Connection) -> float:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT coalesce(sum(cost_usd), 0) AS c FROM agent_runs WHERE (created_at AT TIME ZONE %s)::date = %s",
            (get_settings().timezone, local_today().date()),
        )
        return float(cur.fetchone()["c"])


def check_spend(conn: psycopg.Connection) -> float:
    """Remaining budget for today; raises when the cap is reached."""
    cap = get_settings().daily_spend_cap_usd
    spent = today_spend(conn)
    if spent >= cap:
        raise SpendCapExceeded(f"daily spend cap reached: ${spent:.2f} of ${cap:.2f} ({get_settings().timezone} day)")
    return cap - spent


def prompt_hash(system: str) -> str:
    return hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]


# ---- client ----------------------------------------------------------------------------------
_client = None


def get_client():
    global _client
    if _client is None:
        import anthropic

        key = get_settings().anthropic_api_key
        _client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    return _client


def _usage_of(resp) -> Usage:
    u = getattr(resp, "usage", None)
    out = Usage()
    if u is None:
        return out
    out.input_tokens = int(getattr(u, "input_tokens", 0) or 0)
    out.output_tokens = int(getattr(u, "output_tokens", 0) or 0)
    out.cache_read_tokens = int(getattr(u, "cache_read_input_tokens", 0) or 0)
    out.cache_write_tokens = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
    stu = getattr(u, "server_tool_use", None)
    if stu is not None:
        out.search_uses = int(getattr(stu, "web_search_requests", 0) or 0)
    return out


def _text_of(resp) -> str:
    parts = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "".join(parts)


def _citations_of(resp) -> list[dict]:
    cites: list[dict] = []
    for block in getattr(resp, "content", []) or []:
        if getattr(block, "type", None) == "web_search_tool_result":
            content = getattr(block, "content", None)
            if isinstance(content, list):
                for r in content:
                    if getattr(r, "type", None) == "web_search_result":
                        cites.append({"url": r.url, "title": getattr(r, "title", None),
                                      "page_age": getattr(r, "page_age", None)})
        for c in getattr(block, "citations", None) or []:
            url = getattr(c, "url", None)
            if url:
                cites.append({"url": url, "title": getattr(c, "title", None), "cited_text": getattr(c, "cited_text", None)})
    return cites


def _log_run(conn, *, role, model, phash, region_id, geo_key, input_ref, output_ref, usage: Usage, cost, latency_ms,
             ok, error, attempt) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_runs(role, model, prompt_hash, region_id, geo_key, input_ref, output_ref, input_tokens,
                                      output_tokens, cache_read_tokens, cache_write_tokens, search_uses, cost_usd, latency_ms,
                                      ok, error, attempt)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (role, model, phash, region_id, geo_key, input_ref, output_ref, usage.input_tokens, usage.output_tokens,
             usage.cache_read_tokens, usage.cache_write_tokens, usage.search_uses, cost, latency_ms, ok, error, attempt),
        )
        run_id = cur.fetchone()["id"]
    conn.commit()
    return run_id


def _extract_json(text: str) -> Any:
    """Parse the model's text as JSON; tolerate a fenced block or leading prose."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        s = s.rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start >= 0 and end > start:
            return json.loads(s[start:end + 1])
        raise


def complete[T: BaseModel](conn: psycopg.Connection, role: str, *, system: str, user: str, schema: type[T],
             region_id: str | None = None, geo_key: str | None = None, max_tokens: int = 8000,
             effort: str | None = None, tools: list[dict] | None = None, store: BlobStore | None = None,
             client=None, prior_errors: list[str] | None = None) -> LLMResult[T]:
    """One structured call. `schema` is the pydantic model the answer must satisfy.

    Without `tools` the API's structured-output mode is used (schema enforced server-side; pydantic
    validators still run here). With server tools (web search), the model answers in text and the JSON
    is parsed here. Either way a validation failure re-prompts once with the errors, then raises LLMError.
    """
    model = model_for(role)
    effort = effort or effort_for(role)
    client = client or get_client()
    store = store or BlobStore()
    phash = prompt_hash(system)
    errors: list[str] = list(prior_errors or [])
    system_blocks = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    for attempt in (1, 2):
        check_spend(conn)
        user_text = user if not errors else (
            f"{user}\n\nYour previous answer failed validation with these errors; return a corrected answer:\n- "
            + "\n- ".join(errors)
        )
        packet = {"role": role, "model": model, "system": system, "user": user_text, "schema": schema.model_json_schema(),
                  "tools": tools, "effort": effort, "attempt": attempt}
        input_ref = store.put_bytes(json.dumps(packet, default=str).encode(), suffix=".json").ref
        kwargs: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "system": system_blocks,
                                  "messages": [{"role": "user", "content": user_text}]}
        if effort:
            kwargs["output_config"] = {"effort": effort}
        t0 = time.monotonic()
        try:
            if tools:
                kwargs["tools"] = tools
                resp = client.messages.create(**kwargs)
            else:
                resp = client.messages.parse(output_format=schema, **kwargs)
        except Exception as e:  # API/transport errors: log and re-raise, no retry here (SDK retries transport)
            latency = int((time.monotonic() - t0) * 1000)
            _log_run(conn, role=role, model=model, phash=phash, region_id=region_id, geo_key=geo_key, input_ref=input_ref,
                     output_ref=None, usage=Usage(), cost=0.0, latency_ms=latency, ok=False,
                     error=f"{type(e).__name__}: {e}", attempt=attempt)
            raise
        latency = int((time.monotonic() - t0) * 1000)
        usage = _usage_of(resp)
        cost = cost_of(model, usage)
        raw_text = _text_of(resp)
        output_ref = store.put_bytes(raw_text.encode(), suffix=".txt").ref if raw_text else None
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            details = getattr(resp, "stop_details", None)
            err = f"refusal: {getattr(details, 'category', None)} {getattr(details, 'explanation', '')}".strip()
            _log_run(conn, role=role, model=model, phash=phash, region_id=region_id, geo_key=geo_key, input_ref=input_ref,
                     output_ref=output_ref, usage=usage, cost=cost, latency_ms=latency, ok=False, error=err, attempt=attempt)
            raise LLMError(err)
        parsed: T | None = None
        err: str | None = None
        try:
            if tools:
                parsed = schema.model_validate(_extract_json(raw_text))
            else:
                parsed = getattr(resp, "parsed_output", None)
                if parsed is None:
                    parsed = schema.model_validate(_extract_json(raw_text))
                elif not isinstance(parsed, schema):
                    parsed = schema.model_validate(parsed)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            err = f"{type(e).__name__}: {e}"
        if stop == "max_tokens" and parsed is None:
            err = (err or "") + " [stop_reason=max_tokens: output truncated]"
        run_id = _log_run(conn, role=role, model=model, phash=phash, region_id=region_id, geo_key=geo_key,
                          input_ref=input_ref, output_ref=output_ref, usage=usage, cost=cost, latency_ms=latency,
                          ok=parsed is not None, error=err, attempt=attempt)
        if parsed is not None:
            return LLMResult(parsed=parsed, run_id=run_id, model=model, usage=usage, cost_usd=cost, latency_ms=latency,
                             raw_text=raw_text, attempts=attempt, citations=_citations_of(resp))
        log.warning("%s attempt %d failed validation: %s", role, attempt, err)
        errors = [err or "invalid output"]
    raise LLMError(f"{role}: output failed validation twice; last error: {errors[-1]}")


def web_search_tool(max_uses: int = 5, allowed_domains: list[str] | None = None,
                    blocked_domains: list[str] | None = None, user_location: dict | None = None) -> dict:
    tool: dict[str, Any] = {"type": WEB_SEARCH_TOOL, "name": "web_search", "max_uses": max_uses}
    if allowed_domains:
        tool["allowed_domains"] = allowed_domains
    elif blocked_domains:
        tool["blocked_domains"] = blocked_domains
    if user_location:
        tool["user_location"] = user_location
    return tool
