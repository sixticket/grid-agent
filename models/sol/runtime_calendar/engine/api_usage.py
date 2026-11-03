"""Per-response API usage ledger for the new Sol / Opus paper snapshots.

GPT-6.1 Sol prices verified against official API documentation on 2026-09-30.
Other model rates retain the archived 2026-09-23 snapshot. Estimates cover
standard, global, text/tool tokens only; the provider invoice is final.
Reasoning is included in output_tokens and is never charged a second time.
"""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path

PRICING_AS_OF = "2026-09-30"
LEGACY_PRICING_AS_OF = "2026-09-23"
PRICING_SOURCES = {
    "openai": "https://developers.openai.com/api/docs/pricing",
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
}


def as_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {k: as_dict(v) if hasattr(v, "__dict__") else v
            for k, v in vars(value).items()}


def _count(value):
    n = int(value or 0)
    if n < 0:
        raise ValueError("API usage contains negative token count")
    return n


def normalize_usage(provider, usage):
    """Return disjoint input categories and an inclusive output total."""
    u = as_dict(usage)
    output = _count(u.get("output_tokens"))
    out_detail = as_dict(u.get("output_tokens_details"))
    if provider == "openai":
        detail = as_dict(u.get("input_tokens_details"))
        total = _count(u.get("input_tokens"))
        read = _count(detail.get("cached_tokens"))
        write = _count(detail.get("cache_write_tokens"))
        uncached = total - read - write
        if uncached < 0:
            raise ValueError("OpenAI cache tokens exceed total input tokens")
        write_5m = write_1h = None
        reasoning = out_detail.get("reasoning_tokens")
    elif provider == "anthropic":
        # Claude input_tokens excludes both cache reads and cache writes.
        uncached = _count(u.get("input_tokens"))
        read = _count(u.get("cache_read_input_tokens"))
        write = _count(u.get("cache_creation_input_tokens"))
        total = uncached + read + write
        breakdown = as_dict(u.get("cache_creation"))
        write_5m = _count(breakdown.get("ephemeral_5m_input_tokens"))
        write_1h = _count(breakdown.get("ephemeral_1h_input_tokens"))
        if write_5m + write_1h > write:
            raise ValueError("Claude cache TTL subtotals exceed cache writes")
        reasoning = out_detail.get("thinking_tokens")
    else:
        raise ValueError(f"Unsupported provider: {provider}")
    return dict(input_tokens=total, uncached_input_tokens=uncached,
                cache_read_tokens=read, cache_write_tokens=write,
                cache_write_5m_tokens=write_5m,
                cache_write_1h_tokens=write_1h,
                output_tokens=output,
                reasoning_tokens=None if reasoning is None else _count(reasoning))


def estimate_request_cost(provider, model, tokens, cache_ttl="5m"):
    """Price a single request (long-context rates cannot use campaign totals)."""
    if provider == "openai" and model in {"gpt-6-sol", "gpt-6.1-sol"}:
        long = tokens["input_tokens"] > 272_000
        cached_rate = 0.1 if model == "gpt-6.1-sol" else 0.2
        rates = dict(input=4.0 if long else 2.0,
                     cache_read=cached_rate * (2 if long else 1),
                     cache_write=5.0 if long else 2.5,
                     output=15.0 if long else 10.0)
        cost = (tokens["uncached_input_tokens"] * rates["input"]
                + tokens["cache_read_tokens"] * rates["cache_read"]
                + tokens["cache_write_tokens"] * rates["cache_write"]
                + tokens["output_tokens"] * rates["output"])
        tier = "long" if long else "short"
        inferred_write_ttl = None
    elif provider == "anthropic" and model == "claude-opus-5-5":
        rates = dict(input=4.0, cache_read=0.2, cache_write_5m=5.0,
                     cache_write_1h=8.0, output=20.0)
        five = tokens["cache_write_5m_tokens"]
        hour = tokens["cache_write_1h_tokens"]
        unclassified = tokens["cache_write_tokens"] - five - hour
        # Older usage payloads omit TTL subtotals. Record this assumption.
        inferred_write_ttl = cache_ttl if unclassified else None
        if cache_ttl == "1h":
            hour += unclassified
        else:
            five += unclassified
        cost = (tokens["uncached_input_tokens"] * rates["input"]
                + tokens["cache_read_tokens"] * rates["cache_read"]
                + five * rates["cache_write_5m"]
                + hour * rates["cache_write_1h"]
                + tokens["output_tokens"] * rates["output"])
        tier = "standard_full_context"
    else:
        return dict(estimated_cost_usd=None, pricing_context_tier=None,
                    rates_usd_per_million=None, inferred_write_ttl=None)
    return dict(estimated_cost_usd=round(cost / 1_000_000, 10),
                pricing_context_tier=tier, rates_usd_per_million=rates,
                inferred_write_ttl=inferred_write_ttl)


def record_response(provider, model, response, *, effort=None, cache_ttl="off"):
    """Log every returned response, including nudge calls hidden by single_call.

    No prompts, response bodies or secrets are included. A missing response
    (timeout/cancelled request) has unknown usage and cannot be counted here.
    Errors writing the requested ledger are fatal, so accounting loss is visible.
    """
    raw = as_dict(response)
    usage = as_dict(getattr(response, "usage", None) or raw.get("usage"))
    tokens = normalize_usage(provider, usage)
    path = os.environ.get("GRID_API_USAGE_LOG")
    if not path:
        return tokens
    row = dict(schema_version=1,
               recorded_at=datetime.now(timezone.utc).isoformat(),
               provider=provider, requested_model=model,
               response_model=raw.get("model") or getattr(response, "model", None),
               response_id=raw.get("id") or getattr(response, "id", None),
               effort=effort, cache_ttl=cache_ttl,
               service_tier=raw.get("service_tier") or usage.get("service_tier"),
               usage=usage, **tokens,
               **estimate_request_cost(provider, model, tokens, cache_ttl),
               pricing_as_of=PRICING_AS_OF if model == "gpt-6.1-sol" else LEGACY_PRICING_AS_OF,
               pricing_source=PRICING_SOURCES[provider],
               pricing_basis="standard_global_text_tokens; excludes tax/credits")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
        fcntl.flock(f, fcntl.LOCK_UN)
    return tokens


def summarize_ledger(path):
    """Total completed responses, ignoring duplicate response IDs on replay.

    A missing reasoning decomposition is unknown, never zero; inclusive output
    remains available. Invalid/truncated JSON is an accounting error, not a row
    to silently omit.
    """
    target = Path(path)
    rows = []
    seen = set()
    if target.exists():
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            response_id = row.get("response_id")
            key = (row.get("provider"), response_id)
            if response_id and key in seen:
                continue
            if response_id:
                seen.add(key)
            rows.append(row)
    result = {"api_calls": len(rows)}
    for key in ("input_tokens", "uncached_input_tokens", "cache_read_tokens",
                "cache_write_tokens", "output_tokens", "reasoning_tokens"):
        values = [row.get(key) for row in rows]
        result[key] = None if any(v is None for v in values) else sum(values)
    costs = [row.get("estimated_cost_usd") for row in rows]
    result["estimated_cost_usd"] = (None if any(c is None for c in costs)
                                    else round(sum(costs), 10))
    result["pricing_as_of"] = PRICING_AS_OF
    result["ledger_path"] = str(target)
    result["usage_scope"] = "received_successful_api_responses_including_nudges"
    return result
