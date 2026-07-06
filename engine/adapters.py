"""Model-agnostic LLM adapters. One interface, provider-specific wiring inside.

Interface:
    complete(system, user, json_schema=None) -> str          # single clean-context call
    run_tool_loop(system, user, tool_specs, dispatch, max_tool_calls) -> str
    take_usage() -> (input_tokens, output_tokens, dollars)   # since last take
"""

import hashlib
import json
import re


class AdapterError(Exception):
    pass


class BaseAdapter:
    cache_read_mult = 0.1    # cached input billed at 10% of input price (both providers)
    cache_write_mult = 1.25  # anthropic cache-write premium; openai has none

    def __init__(self, model: str, prices: dict):
        self.model = model
        p = prices.get(model, {"input": 0.0, "output": 0.0})
        self.price_in = p["input"] / 1e6
        self.price_out = p["output"] / 1e6
        self._in = 0
        self._out = 0
        self._cache_read = 0
        self._cache_write = 0

    def _track(self, input_tokens: int, output_tokens: int,
               cache_read: int = 0, cache_write: int = 0):
        self._in += input_tokens
        self._out += output_tokens
        self._cache_read += cache_read
        self._cache_write += cache_write

    def take_usage(self):
        dollars = (self._in * self.price_in + self._out * self.price_out
                   + self._cache_read * self.price_in * self.cache_read_mult
                   + self._cache_write * self.price_in * self.cache_write_mult)
        usage = (self._in + self._cache_read + self._cache_write, self._out, dollars)
        self._in = self._out = self._cache_read = self._cache_write = 0
        return usage

    # subclasses implement:
    def complete(self, system, user, json_schema=None, max_tokens=8000) -> str:
        raise NotImplementedError

    def run_tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls=4,
                      max_tokens=4000) -> str:
        raise NotImplementedError


def extract_json(text: str) -> dict:
    """Parse a JSON object out of model text, tolerating code fences and preamble."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


class AnthropicAdapter(BaseAdapter):
    def __init__(self, model, prices):
        super().__init__(model, prices)
        import anthropic
        self.client = anthropic.Anthropic()

    def _create(self, **kw):
        resp = self.client.messages.create(model=self.model, **kw)
        u = resp.usage
        self._track(u.input_tokens, u.output_tokens,
                    getattr(u, "cache_read_input_tokens", 0) or 0,
                    getattr(u, "cache_creation_input_tokens", 0) or 0)
        return resp

    @staticmethod
    def _text(resp) -> str:
        return "".join(b.text for b in resp.content if b.type == "text")

    @staticmethod
    def _move_cache_marker(messages: list, results: list):
        """Keep exactly one cache breakpoint, on the newest tool results.

        The API allows max 4 breakpoints per request; a marker that walks forward
        each iteration lets later loop rounds re-read the whole earlier
        conversation at the 10% cached rate.
        """
        for msg in messages:
            if msg["role"] == "user" and isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        if results:
            results[-1]["cache_control"] = {"type": "ephemeral"}

    def complete(self, system, user, json_schema=None, max_tokens=8000):
        kw = dict(
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": user}],
        )
        if json_schema:
            kw["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
        resp = self._create(**kw)
        if resp.stop_reason == "refusal":
            raise AdapterError("model refused the request")
        return self._text(resp)

    def run_tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls=4,
                      max_tokens=4000):
        messages = [{"role": "user", "content": user}]
        calls = 0
        for _ in range(max_tool_calls + 2):
            resp = self._create(
                max_tokens=max_tokens, system=system, thinking={"type": "adaptive"},
                tools=tool_specs, messages=messages,
            )
            if resp.stop_reason != "tool_use":
                return self._text(resp)
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                if calls >= max_tool_calls:
                    out = "tool budget exhausted — write your summary from what you have"
                else:
                    out = dispatch(block.name, block.input)
                    calls += 1
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": str(out)[:8000]})
            self._move_cache_marker(messages, results)
            messages.append({"role": "user", "content": results})
        return self._text(resp)


class OpenAIAdapter(BaseAdapter):
    def __init__(self, model, prices):
        super().__init__(model, prices)
        import openai
        self.client = openai.OpenAI()

    cache_write_mult = 0.0  # openai auto-caches with no write premium

    def _create(self, **kw):
        resp = self.client.chat.completions.create(model=self.model, **kw)
        u = resp.usage
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        self._track(u.prompt_tokens - cached, u.completion_tokens, cache_read=cached)
        return resp

    def complete(self, system, user, json_schema=None, max_tokens=8000):
        kw = dict(
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_completion_tokens=max_tokens,
        )
        if json_schema:
            kw["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": json_schema, "strict": True},
            }
        resp = self._create(**kw)
        return resp.choices[0].message.content or ""

    def run_tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls=4,
                      max_tokens=4000):
        tools = [{"type": "function",
                  "function": {"name": t["name"], "description": t["description"],
                               "parameters": t["input_schema"]}}
                 for t in tool_specs]
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        calls = 0
        for _ in range(max_tool_calls + 2):
            resp = self._create(messages=messages, tools=tools,
                                max_completion_tokens=max_tokens)
            msg = resp.choices[0].message
            if not msg.tool_calls:
                return msg.content or ""
            messages.append({"role": "assistant", "content": msg.content,
                             "tool_calls": [tc.model_dump() for tc in msg.tool_calls]})
            for tc in msg.tool_calls:
                if calls >= max_tool_calls:
                    out = "tool budget exhausted — write your summary from what you have"
                else:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    out = dispatch(tc.function.name, args)
                    calls += 1
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": str(out)[:8000]})
        return msg.content or ""


class MockAdapter(BaseAdapter):
    """Deterministic fake model for zero-cost pipeline testing.

    Estimates are the market price plus a market-id-seeded jitter, so realistic
    edges exist. Postmortems always propose appending one line to the prompt.
    """

    def _jitter(self, key: str, scale: float = 0.15) -> float:
        h = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        return ((h % 1000) / 1000.0 - 0.5) * 2 * scale

    def complete(self, system, user, json_schema=None, max_tokens=8000):
        self._track(1200, 300)
        if "post-mortem" in user.lower():
            m = re.search(r"CURRENT PROMPT:\n---\n(.*?)\n---", user, re.S)
            old = m.group(1) if m else ""
            new = old.rstrip() + "\n- Mock lesson: widen intervals on politics markets."
            return json.dumps({
                "postmortem": "Mock post-mortem: overconfident on politics; brier worst there.",
                "change_description": "Append one calibration rule about politics markets.",
                "new_prompt": new,
            })
        # estimate phase
        mid = re.search(r"Market ID: (\S+)", user)
        price = re.search(r"Current YES price \(mid\): ([\d.]+)", user)
        p = float(price.group(1)) if price else 0.5
        key = mid.group(1) if mid else user[:40]
        prob = min(0.97, max(0.03, p + self._jitter(key)))
        return json.dumps({
            "prob": round(prob, 2),
            "confidence_notes": f"mock estimate anchored to market {p:.2f} with seeded adjustment",
        })

    def run_tool_loop(self, system, user, tool_specs, dispatch, max_tool_calls=4,
                      max_tokens=4000):
        self._track(2000, 400)
        # exercise the offline tools only (no network in mock mode)
        calc = dispatch("calculator", {"expression": "0.5*(1+0.1)"})
        cat = re.search(r"Category: (\w+)", user)
        log = dispatch("category_error_log", {"category": cat.group(1) if cat else "economics"})
        return (
            "FACTS:\n- Mock research fact 1 (dated 2026-07-01).\n"
            f"- Base-rate arithmetic check: {calc}.\n"
            f"- Own calibration: {log}\n"
            "FIGURES:\n- Synthetic indicator at 42.\n"
            "SOURCES:\n- https://example.com/mock-source\n"
        )


def make_adapter(provider: str, model: str, prices: dict) -> BaseAdapter:
    if provider == "mock" or model == "mock":
        return MockAdapter("mock", prices)
    if provider == "anthropic":
        return AnthropicAdapter(model, prices)
    if provider == "openai":
        return OpenAIAdapter(model, prices)
    raise AdapterError(f"unknown provider {provider}")
