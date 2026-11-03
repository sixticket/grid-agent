"""Bounded request retries, without changing the callback's API arguments.

SDK retries must be disabled with max_retries=0. The elapsed budget includes
request time, but cannot interrupt an in-flight synchronous callback: its SDK
timeout remains a separate limit. No new attempt starts after this budget.
See https://developers.openai.com/api/docs/guides/rate-limits.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from email.utils import parsedate_to_datetime
import importlib
import json
import math
import os
from pathlib import Path
import random as _random
import time
from typing import TypeVar

T = TypeVar("T")
_HTTP_RETRYABLE = {408, 409}
_MAX_BODY_RETRY_AFTER = 86400.0
_RATE_LIMIT = {"rate_limit_error", "rate_limit_exceeded", "rate_limit_reached",
               "slow_down", "too_many_requests", "requests", "tokens"}
_ACTION_REQUIRED = ("quota", "billing", "payment", "credit", "spend", "budget",
                    "balance", "hard_limit", "usage_limit", "trial_expired")
_CANCELED = {"CancelledError", "CancellationError", "RequestCancelledError",
             "DeadlineExceededError"}


def _retryable(exc: Exception, provider: str) -> bool:
    # SDKs can wrap cancellation as a connection error; don't revive a canceled
    # operation. Plain cancellation exceptions also fail the SDK type check.
    cause = exc
    seen = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if type(cause).__name__ in _CANCELED or not isinstance(cause, Exception):
            return False
        cause = cause.__cause__ or cause.__context__
    sdk = importlib.import_module(provider)
    if isinstance(exc, sdk.APIConnectionError):
        return True
    if not isinstance(exc, sdk.APIStatusError):
        return False
    if exc.status_code != 429:
        return exc.status_code in _HTTP_RETRYABLE or 500 <= exc.status_code < 600
    body = exc.body if isinstance(exc.body, Mapping) else {}
    error = body.get("error", body)
    error = error if isinstance(error, Mapping) else {}
    codes = {str(value).lower() for value in
             (getattr(exc, "code", None), getattr(exc, "type", None),
              error.get("code"), error.get("type")) if value is not None}
    # Read messages only to classify nontransient 429s; never journal them.
    description = " ".join([*codes, str(error.get("message", "")),
                            str(getattr(exc, "message", ""))]).lower()
    return not any(x in description for x in _ACTION_REQUIRED) and bool(codes & _RATE_LIMIT)


def _server_delay(exc: Exception, wall_clock: Callable[[], float]) -> float | None:
    headers = getattr(getattr(exc, "response", None), "headers", {})
    hints = []
    for key, scale in (("retry-after-ms", .001), ("retry-after", 1.0)):
        raw = headers.get(key)
        if raw is None:
            continue
        try:
            seconds = float(raw) * scale
        except (TypeError, ValueError, OverflowError):
            if key != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(raw)
                if date.tzinfo is None:
                    from datetime import timezone
                    date = date.replace(tzinfo=timezone.utc)
                seconds = max(0.0, date.timestamp() - wall_clock())
            except (TypeError, ValueError, OverflowError):
                continue
        if math.isfinite(seconds) and seconds >= 0:
            hints.append(seconds)
    # Cloudflare's JSON error payload can carry a minimum retry delay even
    # without Retry-After headers. Accept only its explicit, consistent 5xx
    # schema and a finite JSON number; do not parse strings or error messages.
    body = getattr(exc, "body", None)
    status = getattr(exc, "status_code", None)
    if (isinstance(body, Mapping) and type(status) is int and 500 <= status < 600
            and body.get("cloudflare_error") is True and body.get("retryable") is True
            and type(body.get("status")) is int and body["status"] == status
            and type(body.get("error_code")) is int and body["error_code"] == status):
        seconds = body.get("retry_after")
        if (type(seconds) in (int, float) and 0 <= seconds <= _MAX_BODY_RETRY_AFTER
                and math.isfinite(seconds)):
            hints.append(seconds)
    return max(hints) if hints else None


def _journal(provider: str, attempt: int, event: str, elapsed: float,
             exc: Exception | None = None, delay: float | None = None,
             hint: bool = False) -> None:
    output = os.environ.get("GRID_RUN_OUTPUT")
    response_log = os.environ.get("GRID_API_RESPONSE_LOG")
    path = (Path(output) / "api_retries.jsonl" if output else
            Path(response_log).with_name("api_retries.jsonl") if response_log else None)
    if path is None:
        return
    record = dict(event=event, provider=provider, attempt=attempt,
                  elapsed_seconds=round(elapsed, 3))
    if exc is not None:
        record.update(error_type=type(exc).__name__,
                      http_status=getattr(exc, "status_code", None))
    if delay is not None:
        record.update(delay_seconds=round(delay, 3), server_hint=hint)
    try:
        with path.open("a") as stream:
            stream.write(json.dumps(record, allow_nan=False) + "\n")
    except OSError:
        # Diagnostics must not replace an otherwise successful API operation.
        pass


def retry_request(request: Callable[[], T], *, provider: str,
                  max_attempts: int = 8, max_elapsed: float = 1800,
                  initial_delay: float = 30, max_delay: float = 300,
                  jitter_seconds: float = 5,
                  clock: Callable[[], float] = time.monotonic,
                  wall_clock: Callable[[], float] = time.time,
                  sleep: Callable[[float], None] = time.sleep,
                  random: Callable[[], float] = _random.random) -> T:
    """Retry only eligible SDK failures; re-raise the original error on limits.

    max_attempts includes the initial request. Valid server delay hints override
    fallback backoff and are never capped by max_delay; if they cannot fit the
    remaining elapsed budget, defer by raising instead of retrying too soon.
    KeyboardInterrupt, SystemExit and cancellation propagate unchanged.
    """
    if provider not in ("openai", "anthropic"):
        raise ValueError("Unsupported request provider")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    limits = (max_elapsed, initial_delay, max_delay, jitter_seconds)
    if any(not math.isfinite(x) or x < 0 for x in limits) or min(limits[:3]) <= 0:
        raise ValueError("Retry durations must be finite; budgets/delays positive")
    started = clock()
    deadline = started + max_elapsed
    last_error = None
    last_traceback = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and clock() >= deadline:
            _journal(provider, attempt - 1, "elapsed_limit", clock() - started, last_error)
            raise last_error.with_traceback(last_traceback)
        try:
            result = request()
        except Exception as exc:
            if not _retryable(exc, provider):
                raise
            last_error, last_traceback = exc, exc.__traceback__
            remaining = deadline - clock()
            if attempt == max_attempts or remaining <= 0:
                _journal(provider, attempt, "attempt_limit" if attempt == max_attempts
                         else "elapsed_limit", clock() - started, exc)
                raise
            hint = _server_delay(exc, wall_clock)
            base = hint if hint is not None else min(initial_delay * 2 ** (attempt - 1), max_delay)
            # Positive jitter preserves the minimum delay requested by server.
            delay = base + jitter_seconds * max(0.0, min(1.0, random()))
            if delay >= remaining:
                _journal(provider, attempt, "deferred_deadline", clock() - started,
                         exc, delay, hint is not None)
                raise
            _journal(provider, attempt, "retry_scheduled", clock() - started,
                     exc, delay, hint is not None)
            wake = clock() + delay
            while clock() < wake:
                # Chunk waits to remain responsive to SIGTERM/SIGINT handlers.
                sleep(min(60.0, wake - clock()))
                if clock() >= deadline:
                    _journal(provider, attempt, "elapsed_limit", clock() - started, exc)
                    raise
            # Account for journal overhead or clock progress before reentering.
            if clock() >= deadline:
                _journal(provider, attempt, "elapsed_limit", clock() - started, exc)
                raise
        else:
            if attempt > 1:
                _journal(provider, attempt, "recovered", clock() - started)
            return result
