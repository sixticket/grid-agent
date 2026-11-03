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

import json

import anthropic
import openai

# Inherited model defaults. Availability and live pricing are not established
# by offline integration tests; override --model for the research run.
DEFAULT_MODELS = {"anthropic": "claude-fable-5-1", "openai": "gpt-6-astra"}
# Fable 5.1 thinking is always on and billed within max_tokens — leave room.
MAX_TOKENS = 16000

# $/Mtok (input, output). VERIFY against current price pages before a paper run
# — these are best-known values at time of writing and change over time.
PRICING_PER_MTOK = {
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
NO_FORCED_TOOL_CHOICE = ("claude-fable-5-1", "claude-mythos-5-1")


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int):
    """Rough dollar cost; None if the model has no pricing entry."""
    p = PRICING_PER_MTOK.get(model)
    if p is None:
        return None
    return round((input_tokens * p[0] + output_tokens * p[1]) / 1e6, 4)


class AnthropicChat:
    def __init__(self, model: str):
        self.client = anthropic.Anthropic()
        self.model = model
        self.history = []

    def send_user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def send_tool_results(self, results: list) -> None:
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r["id"],
             "content": r["content"]} for r in results]})

    def step(self, system: str, tools: list | None = None,
             force_tool: str | None = None,
             max_tokens: int | None = None) -> dict:
        kwargs = {}
        if tools:
            kwargs["tools"] = tools  # neutral format == Anthropic format
        if force_tool:
            if self.model in NO_FORCED_TOOL_CHOICE:
                # Fable 5.1 400s on forced tool_choice — instruct instead
                kwargs["tool_choice"] = {"type": "auto"}
                system = (system + "\n\n[Response format — mandatory] Respond "
                          f"by calling the `{force_tool}` tool exactly once. "
                          "Do not answer in plain text.")
            else:
                kwargs["tool_choice"] = {"type": "tool", "name": force_tool}
        resp = self.client.messages.create(
            model=self.model, max_tokens=max_tokens or MAX_TOKENS,
            system=system, messages=self.history, **kwargs)
        self.history.append({"role": "assistant", "content": resp.content})
        return {
            "text": "".join(b.text for b in resp.content if b.type == "text"),
            "tool_calls": [{"id": b.id, "name": b.name, "input": b.input}
                           for b in resp.content if b.type == "tool_use"],
            "stop": ("tool_use" if resp.stop_reason == "tool_use" else
                     "refusal" if resp.stop_reason == "refusal" else "end"),
            "usage": (resp.usage.input_tokens, resp.usage.output_tokens),
            "raw": resp.to_dict(),
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
            # vendor-recommended best; "high" matches the Claude side's
            # default effort so the flagship pair runs symmetrically.
            # (Astra reasoning is always on — effort "none" is a 400.)
            kwargs["reasoning"] = {"effort": "high"}
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
            "usage": (resp.usage.input_tokens, resp.usage.output_tokens),
            "raw": resp.model_dump(),
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
