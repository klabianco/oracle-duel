import json

from agents.shared_toolbox import Toolbox

DDG_HTML = (
    '<a rel="nofollow" class="result__a" '
    'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa&amp;rut=x">Title A</a>'
    '<div class="result__snippet">Snippet A</div>'
)


class FakeResponse:
    def __init__(self, status=200, body="", payload=None):
        self.status_code = status
        self.text = body
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    """Routes by host: brave_response for the Brave API, ddg_response for DDG."""

    headers = {}

    def __init__(self, brave_response, ddg_response):
        self.brave_response = brave_response
        self.ddg_response = ddg_response
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.brave_response if "brave.com" in url else self.ddg_response


def _toolbox(session):
    tb = Toolbox(telemetry=None, agent="a")
    tb.session = session
    return tb


def test_brave_used_when_healthy(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    brave = FakeResponse(payload={"web": {"results": [
        {"title": "T", "url": "https://x.com", "description": "D"}]}})
    session = FakeSession(brave, FakeResponse(body=DDG_HTML))
    out = _toolbox(session).dispatch("web_search", {"query": "q"})
    assert json.loads(out)[0]["url"] == "https://x.com"
    assert not any("duckduckgo" in c for c in session.calls)


def test_falls_back_to_ddg_when_brave_errors(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    session = FakeSession(FakeResponse(status=402), FakeResponse(body=DDG_HTML))
    tb = _toolbox(session)
    out = tb.dispatch("web_search", {"query": "q"})
    assert json.loads(out)[0]["url"] == "https://example.com/a"
    assert tb.stats["web_search"] == {"ok": 1, "err": 0}


def test_ddg_used_directly_without_brave_key(monkeypatch):
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    session = FakeSession(FakeResponse(status=500), FakeResponse(body=DDG_HTML))
    out = _toolbox(session).dispatch("web_search", {"query": "q"})
    assert json.loads(out)[0]["title"] == "Title A"
    assert not any("brave.com" in c for c in session.calls)


def test_both_backends_down_counts_error(monkeypatch):
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    session = FakeSession(FakeResponse(status=402), FakeResponse(status=429))
    tb = _toolbox(session)
    out = tb.dispatch("web_search", {"query": "q"})
    assert out.startswith("tool error")
    assert tb.stats["web_search"] == {"ok": 0, "err": 1}
