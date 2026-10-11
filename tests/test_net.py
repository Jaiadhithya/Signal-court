from datetime import date

import httpx
import pytest

from signalcourt.net.http import Blocked, PoliteClient
from signalcourt.net.nse import NSE


class Clock:
    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def client(handler, rps=1.0, **kw):
    ck = Clock()
    c = PoliteClient(
        "UA/1", default_rps=rps, transport=httpx.MockTransport(handler), clock=ck.now, sleep=ck.sleep, **kw
    )
    return c, ck


def test_throttle_spaces_requests_per_host():
    c, ck = client(lambda r: httpx.Response(200, json={}))
    for _ in range(3):
        c.get("https://a.example/x")
    c.get("https://b.example/x")  # other host is not delayed
    assert ck.sleeps == [1.0, 1.0] or [round(s, 6) for s in ck.sleeps] == [1.0, 1.0]


def test_retry_backoff_and_retry_after():
    seen = []

    def h(r):
        seen.append(1)
        if len(seen) < 3:
            return httpx.Response(429, headers={"retry-after": "5"} if len(seen) == 1 else {})
        return httpx.Response(200, json={"ok": 1})

    c, ck = client(h, rps=1000)
    assert c.get("https://a.example/x").json() == {"ok": 1}
    assert [s for s in ck.sleeps if s >= 2][:2] == [5, 4]  # retry-after beats the 2 s step, then 4 s


def test_gives_up_after_four_retries():
    c, _ = client(lambda r: httpx.Response(503), rps=1000)
    with pytest.raises(httpx.HTTPStatusError):
        c.get("https://a.example/x")


def test_prime_once_on_403_then_success():
    state = {"primed": False, "n": 0}

    def h(r):
        state["n"] += 1
        return httpx.Response(200, json={"v": 1}) if state["primed"] else httpx.Response(403, text="nope")

    c, _ = client(h, rps=1000)
    assert c.get("https://a.example/x", prime=lambda: state.update(primed=True)).json() == {"v": 1}
    assert state["n"] == 2


def test_blocked_on_persistent_403_and_on_captcha_page():
    c, _ = client(lambda r: httpx.Response(403, text="denied"), rps=1000)
    with pytest.raises(Blocked):
        c.get("https://a.example/x", prime=lambda: None)
    c2, _ = client(
        lambda r: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<html><body>Please solve the CAPTCHA</body></html>"
        ),
        rps=1000,
    )
    with pytest.raises(Blocked):
        c2.get("https://a.example/page")


def test_pdf_and_json_never_look_blocked():
    c, _ = client(lambda r: httpx.Response(200, content=b"%PDF-1.7 access denied captcha"), rps=1000)
    assert c.get("https://a.example/f.pdf").content.startswith(b"%PDF")


def test_redirect_across_hosts_and_fetch_log():
    log = []

    def h(r):
        if r.url.host == "old.example":
            return httpx.Response(301, headers={"location": "https://new.example/y"})
        return httpx.Response(200, json={"ok": True})

    c, _ = client(h, rps=1000, fetch_log=lambda **kw: log.append(kw))
    r = c.get("https://old.example/x")
    assert r.url == "https://new.example/y" and log[-1]["status"] == 200


class MemCache:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def put(self, k, v, ttl):
        self.d[k] = v


def test_listing_cache_avoids_second_request():
    n = []
    c, _ = client(lambda r: (n.append(1), httpx.Response(200, json={"a": 1}))[1], rps=1000, listing_cache=MemCache())
    c.get("https://a.example/api", params={"s": "X"}, listing_ttl_s=60)
    r = c.get("https://a.example/api", params={"s": "X"}, listing_ttl_s=60)
    assert len(n) == 1 and r.from_cache and r.json() == {"a": 1}


def test_nse_integrated_pagination_and_params():
    pages = []

    def h(r):
        if r.url.path.endswith("integrated-filing-results"):
            q = dict(r.url.params)
            pages.append(q)
            p = int(q["page"])
            rows = [{"qe_Date": "30-Jun-2026", "i": p * 10 + k} for k in range(2 if p < 3 else 1)]
            return httpx.Response(200, json={"data": rows, "totalCount": 5, "page": p, "size": 2})
        return httpx.Response(404)

    c, _ = client(h, rps=1000)
    rows = NSE(c).results_integrated("SYM", "Sym Limited", size=2)
    assert len(rows) == 5 and [p["page"] for p in pages] == ["1", "2", "3"]
    assert pages[0]["issuer"] == "Sym Limited" and pages[0]["type"] == "Integrated Filing- Financials"


def test_nse_download_forces_https_and_announcement_dates():
    seen = []
    c, _ = client(lambda r: (seen.append(str(r.url)), httpx.Response(200, json=[]))[1], rps=1000)
    n = NSE(c)
    n.download("http://nsearchives.nseindia.com/corporate/xbrl/F.xml")
    n.announcements("SYM", date(2026, 1, 5), date(2026, 6, 30))
    assert seen[0].startswith("https://nsearchives")
    assert "from_date=05-01-2026" in seen[1] and "to_date=30-06-2026" in seen[1]
