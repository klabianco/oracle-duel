"""Shared research toolbox. Identical for both agents — the only mutable layer is prompt.md.

Tools: web_search, fetch_page, calculator, category_error_log.
The runner enforces the per-market tool-call cap; this module just executes calls.
"""

import ast
import html
import json
import operator
import os
import re

import requests

TOOL_SPECS = [
    {
        "name": "web_search",
        "description": "Search the web. Returns titles, URLs and snippets. "
                       "Use precise queries including dates and proper nouns.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch_page",
        "description": "Fetch a URL and return its readable text (truncated). "
                       "Use for primary sources found via web_search.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "calculator",
        "description": "Evaluate an arithmetic expression, e.g. '0.62*(1-0.55)+3/7'.",
        "input_schema": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
    },
    {
        "name": "category_error_log",
        "description": "Your own historical calibration record for a market category "
                       "(brier score, hit rate, sample size). Categories: economics, "
                       "weather, sports, politics, science.",
        "input_schema": {
            "type": "object",
            "properties": {"category": {"type": "string"}},
            "required": ["category"],
        },
    },
]

_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg,
    ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
}


def _safe_eval(node):
    if isinstance(node, ast.Expression):
        return _safe_eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("unsupported expression")


def _strip_html(raw: str, limit: int = 8000) -> str:
    raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


class Toolbox:
    def __init__(self, telemetry, agent: str):
        self.telemetry = telemetry
        self.agent = agent
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0 (oracle-duel research bot)"

    def dispatch(self, name: str, args: dict) -> str:
        try:
            fn = getattr(self, f"_tool_{name}", None)
            if fn is None:
                return f"error: unknown tool {name}"
            return fn(**args)
        except Exception as e:
            return f"tool error: {e}"

    def _tool_web_search(self, query: str) -> str:
        brave_key = os.environ.get("BRAVE_API_KEY")
        results = []
        if brave_key:
            r = self.session.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": 6},
                headers={"X-Subscription-Token": brave_key},
                timeout=15,
            )
            r.raise_for_status()
            for item in (r.json().get("web", {}).get("results") or [])[:6]:
                results.append({
                    "title": item.get("title"), "url": item.get("url"),
                    "snippet": _strip_html(item.get("description") or "", 300),
                })
        else:
            r = self.session.get(
                "https://html.duckduckgo.com/html/", params={"q": query}, timeout=15
            )
            r.raise_for_status()
            blocks = re.findall(
                r'<a rel="nofollow" class="result__a" href="([^"]+)">(.*?)</a>.*?'
                r'class="result__snippet"[^>]*>(.*?)</',
                r.text, re.S,
            )
            for url, title, snippet in blocks[:6]:
                results.append({
                    "title": _strip_html(title, 200), "url": html.unescape(url),
                    "snippet": _strip_html(snippet, 300),
                })
        if not results:
            return "no results"
        return json.dumps(results, ensure_ascii=False)

    def _tool_fetch_page(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            return "error: only http(s) URLs allowed"
        r = self.session.get(url, timeout=20, allow_redirects=True)
        r.raise_for_status()
        return _strip_html(r.text)

    def _tool_calculator(self, expression: str) -> str:
        return str(_safe_eval(ast.parse(expression, mode="eval")))

    def _tool_category_error_log(self, category: str) -> str:
        log = self.telemetry.category_error_log(self.agent)
        rows = [r for r in log if r["category"] == category.lower()]
        if not rows:
            return f"no resolved forecasts yet in category '{category}'"
        r = rows[0]
        return (f"category={r['category']} n={r['n']} brier={r['brier']:.3f} "
                f"avg_forecast={r['avg_prob']:.2f} actual_hit_rate={r['hit_rate']:.2f}")
