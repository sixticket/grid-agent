"""Provider abstraction: the same agent scripts run on Anthropic (Claude) or
OpenAI (GPT) — the model-provider ablation axis (2nd_documents.md §14, §17).

Prompts and tool schemas are defined ONCE in the agent scripts, in a neutral
format ({"name", "description", "input_schema"}); each adapter translates to
its provider's wire format and keeps conversation history in native form.
Keeping a single copy of the prompts is what makes the cross-provider
comparison a fair ablation rather than a prompt comparison.

Neutral step() result:
  {"text": str, "tool_calls": [{"id", "name", "input"}],
   "stop": "tool_use" | "end" | "refusal", "usage": (input_toks, output_toks),
   "raw": dict}   # provider-native response dump, for the JSONL log
"""

from datetime import datetime, timezone
import copy
import hashlib
import re
import fcntl
import json
import os
from pathlib import Path

import anthropic
import openai
from api_usage import record_response, normalize_usage

# Paper snapshot defaults, verified with official model docs 2026-09-23.
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-6-sol"}
# Reasoning is billed within max_tokens; retain the original experiment limit.
MAX_TOKENS = 16000
THINKING_BINDING_BETA = "thinking-binding-controls-2026-08-01"
ANTHROPIC_REPLAY_VERSION = 1

# $/Mtok (input, output). VERIFY against current price pages before a paper run
# — these are best-known values at time of writing and change over time.
PRICING_PER_MTOK = {
    "claude-opus-5-5": (4.0, 20.0),
    "gpt-6-sol": (2.0, 10.0),
    "claude-fable-5-1": (10.0, 50.0),  # flagship (2026-06)
    "gpt-6-astra": (10.0, 50.0),       # flagship (2026-09-03); >272k-input
                                       # requests reprice to 20/75 (we stay far
                                       # below that per call)
    "claude-opus-5": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "gpt-5.6-sol": (4.0, 20.0),   # promo through 2026-11-21; standard 5/30
    "gpt-5": (1.25, 10.0),
}

# Models that reject forced tool_choice ({"type": "tool"|"any"}) with a 400 —
# for these, forcing degrades to tool_choice auto + an explicit instruction,
# and single_call() retries if the model still answers in prose.
NO_FORCED_TOOL_CHOICE = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5-5")


def journal_response(provider, model, raw):
    """Persist each received provider body before usage or tool parsing.

    This separate journal includes responses hidden by single_call's nudges.
    Existing assistant/llm events remain the agent's logical conversation log.
    Only live provider calls write here; replay does not create new responses.
    A configured journal must be writable. Without GRID_API_RESPONSE_LOG,
    retain the standalone adapter's existing behavior without creating files.
    """
    path = os.environ.get("GRID_API_RESPONSE_LOG")
    if not path:
        return
    row = dict(schema_version=1,
               recorded_at=datetime.now(timezone.utc).isoformat(),
               provider=provider, requested_model=model,
               response_model=raw.get("model"), response_id=raw.get("id"),
               response=raw)
    # Serialize before opening the file so a serialization error cannot append
    # a partial record. Never include request headers, clients, or environment.
    line = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def journal_input_transformations(model, raw):
    """Durable, content-free companion to the exact provider response journal."""
    response_path = os.environ.get("GRID_API_RESPONSE_LOG")
    if not response_path:
        return
    transformations = raw.get("input_transformations") or []
    if not isinstance(transformations, list):
        raise ValueError("Unexpected Anthropic input_transformations format")
    safe = []
    for item in transformations:
        if not isinstance(item, dict):
            raise ValueError("Unexpected Anthropic input transformation")
        # Accept only documented machine metadata; never copy opaque provider
        # strings, thinking content, signatures, or arbitrary extra fields.
        path = item.get("path", "")
        reason = item.get("reason", "")
        kind = item.get("type", "")
        if (kind != "thinking_dropped" or
                reason not in {"model_binding_mismatch", "prefix_binding_mismatch"} or
                not isinstance(path, str) or
                not re.fullmatch(r"messages(?:\[\d+\]|\.\d+)\.content(?:\[\d+\]|\.\d+)", path)):
            raise ValueError("Unknown Anthropic input transformation metadata; raw response retained")
        safe.append({"type": kind, "path": path, "reason": reason})
    row = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
           "provider": "anthropic", "requested_model": model,
           "response_id": raw.get("id"), "beta": THINKING_BINDING_BETA,
           "prefix_mismatch_behavior": "drop_block", "drop_count": len(safe),
           "input_transformations_present": "input_transformations" in raw,
           "input_transformations": safe,
           "response_sha256": hashlib.sha256(json.dumps(raw, sort_keys=True,
               separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()}
    line = json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    path = Path(response_path).with_name("input_transformations.jsonl")
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def anthropic_history_json(value):
    """Canonical native content, independent of SDK model versus saved dict."""
    if hasattr(value, "to_dict"):
        return anthropic_history_json(value.to_dict())
    if isinstance(value, dict):
        return {k: anthropic_history_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [anthropic_history_json(v) for v in value]
    return value


def anthropic_saved_result(raw, model):
    """Validate the exact supported message protocol before reconstructing it."""
    if (model != "claude-opus-5-5" or raw.get("model") != model or
            raw.get("type") != "message" or raw.get("role") != "assistant" or
            not isinstance(raw.get("id"), str) or not raw["id"].startswith("msg_") or
            not isinstance(raw.get("content"), list) or
            raw.get("stop_reason") not in {"end_turn", "tool_use", "max_tokens",
                "stop_sequence", "refusal", "pause_turn"} or
            not isinstance(raw.get("usage"), dict)):
        raise RuntimeError("Unsupported Anthropic saved response protocol/model")
    calls, texts = [], []
    for block in raw["content"]:
        if not isinstance(block, dict):
            raise RuntimeError("Invalid Anthropic content block")
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        elif (kind == "tool_use" and isinstance(block.get("id"), str) and
              isinstance(block.get("name"), str) and isinstance(block.get("input"), dict)):
            calls.append({"id": block["id"], "name": block["name"], "input": copy.deepcopy(block["input"])})
        elif (kind == "thinking" and isinstance(block.get("thinking"), str) and
              isinstance(block.get("signature"), str)):
            pass
        elif kind == "redacted_thinking" and isinstance(block.get("data"), str):
            pass
        else:
            raise RuntimeError("Unsupported Anthropic saved content block")
    for field in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = raw["usage"].get(field, 0)
        if type(value) is not int or value < 0:
            raise RuntimeError("Invalid Anthropic saved usage")
    if not {"input_tokens", "output_tokens"} <= raw["usage"].keys():
        raise RuntimeError("Missing Anthropic saved usage")
    tokens = normalize_usage("anthropic", raw["usage"])
    return {"text": "".join(texts), "tool_calls": calls,
            "stop": "tool_use" if raw["stop_reason"] == "tool_use" else
                    "refusal" if raw["stop_reason"] == "refusal" else "end",
            "usage": (tokens["input_tokens"], tokens["output_tokens"]), "raw": raw}


def configured_effort():
    effort = os.environ.get("GRID_REASONING_EFFORT", "high")
    if effort not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError(f"Invalid GRID_REASONING_EFFORT: {effort}")
    return effort


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int):
    """Legacy base-rate estimate; the per-response ledger is authoritative.

    Campaign totals cannot represent per-request context tiers or cache usage.
    For the new paper models, do not emit a misleading base-rate estimate.
    """
    if model in {"gpt-6-sol", "claude-opus-5-5"}:
        return None
    p = PRICING_PER_MTOK.get(model)
    if p is None:
        return None
    return round((input_tokens * p[0] + output_tokens * p[1]) / 1e6, 4)


class AnthropicChat:
    def __init__(self, model: str):
        self.client = anthropic.Anthropic()
        self.model = model
        self.history = []
        self.effort = configured_effort() if model == "claude-opus-5-5" else None
        self.cache_ttl = (os.environ.get("GRID_ANTHROPIC_CACHE_TTL", "5m")
                          if model == "claude-opus-5-5" else "off")
        if self.cache_ttl not in {"off", "5m", "1h"}:
            raise ValueError("GRID_ANTHROPIC_CACHE_TTL must be off, 5m, or 1h")

    def send_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def send_tool_results(self, results: list) -> None:
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"],
             "content": r["content"]} for r in results]})

    def restore_response(self, raw):
        result = anthropic_saved_result(raw, self.model)
        # Keep every original block, including the opaque signature, in native
        # history. Only the provider may drop incompatible blocks via the beta.
        self.history.append({"role": "assistant", "content": copy.deepcopy(raw["content"])})
        return result

    def step(self, system: str, tools: list | None = None,
             force_tool: str | None = None,
             max_tokens: int | None = None) -> dict:
        kwargs = {}
        if self.model == "claude-opus-5-5":
            kwargs["thinking"] = {"type": "adaptive"}
            # SDK 1.2.0 predates this beta. extra_body preserves the exact wire
            # option without altering prompts, tools, adaptive effort, or TTL.
            kwargs["extra_headers"] = {"anthropic-beta": THINKING_BINDING_BETA}
            kwargs["extra_body"] = {"thinking": {"type": "adaptive", "block_binding": {
                "prefix_mismatch_behavior": "drop_block"}}}
            kwargs["output_config"] = {"effort": self.effort}
            kwargs["service_tier"] = "standard_only"
            if self.cache_ttl != "off":
                kwargs["cache_control"] = {"type": "ephemeral", "ttl": self.cache_ttl}
        if tools:
            kwargs["tools"] = tools  # neutral format == Anthropic format
        if force_tool:
            if self.model in NO_FORCED_TOOL_CHOICE:
                # These models reject forced tool_choice; keep the existing
                # instruction fallback and preserve the neutral tool schema.
                kwargs["tool_choice"] = {"type": "auto"}
                system = (system + "\n\n[Response format — mandatory] Respond "
                          f"by calling the `{force_tool}` tool exactly once. "
                          "Do not answer in plain text.")
            else:
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens or MAX_TOKENS,
            system=system, messages=self.history, **kwargs)
        raw = resp.to_dict()
        journal_response("anthropic", self.model, raw)
        if self.model == "claude-opus-5-5":
            journal_input_transformations(self.model, raw)
        tokens = record_response("anthropic", self.model, resp,
                                 effort=self.effort, cache_ttl=self.cache_ttl)
        self.history.append({"role": "assistant", "content": resp.content})
        return {
            "text": "".join(b.text for b in resp.content if b.type == "text"),
            "tool_calls": [{"id": b.id, "name": b.name, "input": b.input}
                           for b in resp.content if b.type == "tool_use"],
            "stop": ("tool_use" if resp.stop_reason == "tool_use" else
                     "refusal" if resp.stop_reason == "refusal" else "end"),
            "usage": (tokens["input_tokens"], tokens["output_tokens"]),
            "raw": raw,
        }


class OpenAIChat:
    """Uses the Responses API (/v1/responses): gpt-5.6 reasoning models do not
    support function tools + reasoning on Chat Completions. Multi-turn state
    is chained server-side via previous_response_id."""

    def __init__(self, model: str):
        self.client = openai.OpenAI()
        self.model = model
        self.prev_id = None
        self.pending = []  # input items for the next step
        self.effort = configured_effort() if model == "gpt-6-sol" else "high"

    def send_user(self, text: str) -> None:
        self.pending.append({"role": "user", "content": text})

    def send_tool_results(self, results: list) -> None:
        for r in results:
            self.pending.append({"type": "function_call_output",
                                 "call_id": r["id"], "output": r["content"]})

    def step(self, system: str, tools: list | None = None,
             force_tool: str | None = None,
             max_tokens: int | None = None) -> dict:
        kwargs = {}
        if tools:
            kwargs["tools"] = [{"type": "function", "name": t["name"],
                                "description": t["description"],
                                "parameters": t["input_schema"],
                                "strict": False} for t in tools]
        if force_tool:
            kwargs["tool_choice"] = {"type": "function", "name": force_tool}
        if self.model.startswith(("gpt-5", "gpt-6", "o")):
            kwargs["reasoning"] = {"effort": self.effort}
        if self.model == "gpt-6-sol":
            kwargs["service_tier"] = "default"
        if self.prev_id:
            kwargs["previous_response_id"] = self.prev_id
        for attempt in range(3):
            try:
                resp = self.client.responses.create(
                    model=self.model, instructions=system,
                    max_output_tokens=max_tokens or MAX_TOKENS,
                    input=self.pending, **kwargs)
                break
            except openai.BadRequestError as e:
                # New paper runs must never silently change the effort or
                # tool protocol after the experiment configuration was saved.
                if self.model == "gpt-6-sol":
                    raise
                # degrade gracefully: some models reject the reasoning param,
                # some reject forced tool_choice — drop the offending knob
                # and retry rather than crash a mid-study run
                msg = str(e)
                if "tool_choice" in msg and force_tool \
                        and kwargs.get("tool_choice") != "auto":
                    kwargs["tool_choice"] = "auto"
                    system = (system + "\n\n[Response format — mandatory] "
                              f"Respond by calling the `{force_tool}` tool "
                              "exactly once. Do not answer in plain text.")
                elif "reasoning" in kwargs:
                    kwargs.pop("reasoning")
                else:
                    raise
        else:
            raise RuntimeError("OpenAI request failed after three parameter compatibility retries")
        raw = resp.model_dump()
        journal_response("openai", self.model, raw)
        tokens = record_response("openai", self.model, resp, effort=self.effort)
        self.prev_id = resp.id
        self.pending = []
        tool_calls = [{"id": item.call_id, "name": item.name,
                       "input": json.loads(item.arguments or "{}")}
                      for item in resp.output
                      if getattr(item, "type", "") == "function_call"]
        return {
            "text": getattr(resp, "output_text", "") or "",
            "tool_calls": tool_calls,
            "stop": "tool_use" if tool_calls else "end",
            "usage": (tokens["input_tokens"], tokens["output_tokens"]),
            "raw": raw,
        }


def make_chat(provider: str, model: str | None = None):
    cls = {"anthropic": AnthropicChat, "openai": OpenAIChat}[provider]
    return cls(model or DEFAULT_MODELS[provider])


def single_call(provider: str, model: str | None, system: str, user: str,
                tool: dict | None = None,
                max_tokens: int | None = None) -> dict:
    """Stateless one-shot call (multi_agent.py's per-agent pattern).
    Returns the step() dict; with `tool`, the forced call's input is in
    ["tool_calls"][0]["input"]. On models where tool forcing is degraded to
    an instruction (see NO_FORCED_TOOL_CHOICE), a prose-only answer gets up
    to two nudge retries before being returned as-is."""
    chat = make_chat(provider, model)
    chat.send_user(user)
    r = chat.step(system, tools=[tool] if tool else None,
                  force_tool=tool["name"] if tool else None,
                  max_tokens=max_tokens)
    for _ in range(2):
        if tool is None or r["tool_calls"]:
            break
        chat.send_user(f"Respond now by calling the `{tool['name']}` tool — "
                       "no plain-text answer.")
        r = chat.step(system, tools=[tool], force_tool=tool["name"],
                      max_tokens=max_tokens)
    return r
