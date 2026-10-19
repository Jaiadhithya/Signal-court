"""Moonveil day-1 model smoke test.

For every LLM provider in config/models.yaml it checks that your keys work,
finds the exact model ids for the deployments marked `<verify>`, sends each chosen
model one tiny JSON task (17 + 25), and records which structured-output mode works,
the latency and the rate-limit headers. It also runs one Tavily search.

Run on your laptop, on your home network:
    1. Copy .env.example to .env in the repo root and paste your keys (comma-separated
       if you have several for one provider). Leave a line empty to skip a provider.
    2. python scripts/models_smoke.py
Needs only `requests`. About 25 small requests in total; no paid usage.
Keys are never printed: they show as key1, key2, ... and are masked in error text.
It prints a SUMMARY block; the same text is saved
in reports/models_smoke_<date>.md.
"""
import datetime
import json
import os
import pathlib
import re
import time

import requests

HERE = pathlib.Path(__file__).resolve().parent.parent  # repo root (script lives in scripts/)

PROVIDERS = {
    "mistral": ("https://api.mistral.ai/v1", "MISTRAL_API_KEYS"),
    "nim": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEYS"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEYS"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEYS"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEYS"),
    "zai": ("https://api.z.ai/api/paas/v4", "ZAI_API_KEYS"),
    "github": ("https://models.github.ai/inference", "GITHUB_MODELS_TOKENS"),
}
GITHUB_CATALOG = "https://models.github.ai/catalog/models"

# Deployment slots from config/models.yaml. Each slot lists id patterns in order of
# preference, then a fallback id used when the listing fails or nothing matches.
SLOTS = {
    "mistral": [("mistral-medium", [r"^mistral-medium-latest$"], "mistral-medium-latest"),
                ("mistral-large", [r"^mistral-large-latest$"], "mistral-large-latest"),
                ("mistral-small", [r"^mistral-small-latest$"], "mistral-small-latest")],
    "nim": [("nemotron-ultra", [r"nemotron-3.*ultra", r"nemotron.*ultra"], None),
            ("deepseek-flash", [r"deepseek.*v4.*flash", r"deepseek.*flash", r"deepseek-v\d"], None),
            ("glm-52-nim", [r"glm-?5\.2", r"glm-?5"], None)],
    "groq": [("gpt-oss-120b", [r"^openai/gpt-oss-120b$"], "openai/gpt-oss-120b"),
             ("gpt-oss-20b", [r"^openai/gpt-oss-20b$"], "openai/gpt-oss-20b"),
             ("qwen-27b", [r"qwen.*27b", r"qwen"], None)],
    "gemini": [("gemma-31b", [r"^gemma-4.*31b", r"^gemma-\d.*-it$"], None),
               ("gemini-flash", [r"^gemini-[\d.]+-flash$", r"^gemini-[\d.]+-flash-preview"], None),
               ("gemini-flash-lite", [r"^gemini-[\d.]+-flash-lite$", r"^gemini-[\d.]+-flash-lite-preview"], None)],
    "openrouter": [("nemotron-or", [r"nemotron.*ultra.*:free$", r"nemotron.*:free$"], None)],
    "zai": [("glm-flash", [r"^glm-4\.7-flash$", r"^glm-[\d.]+-flash$"], "glm-4.7-flash")],
    "github": [("github-check", [r"^openai/gpt-4\.1-mini$", r"^openai/gpt-5-mini$", r"^openai/gpt-4o-mini$"],
                "openai/gpt-4.1-mini")],
}
GAP_SECONDS = {"zai": 13.0}  # Z.ai free tier is ~5 requests a minute; others get 2 s

SCHEMA = {"type": "object", "properties": {"answer": {"type": "integer"}},
          "required": ["answer"], "additionalProperties": False}
MODES = [
    ("json_schema", {"type": "json_schema", "json_schema": {"name": "sum", "strict": True, "schema": SCHEMA}}),
    ("json_object", {"type": "json_object"}),
    ("plain", None),
]
MESSAGES = [
    {"role": "system", "content": "You are a calculator. Reply with JSON only, no prose."},
    {"role": "user", "content": 'Add 17 and 25. Reply exactly as {"answer": <integer>}.'},
]

ALL_KEYS: list = []


def load_env() -> dict:
    env = {}
    path = HERE / ".env"
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            value = value.split(" #", 1)[0].strip().strip('"').strip("'")
            env[name.strip()] = value
    for _, var in list(PROVIDERS.values()) + [(None, "TAVILY_API_KEYS")]:
        if not env.get(var) and os.environ.get(var):
            env[var] = os.environ[var]
    return env


def keys_for(env: dict, var: str) -> list:
    keys = [k.strip() for k in env.get(var, "").split(",") if k.strip()]
    ALL_KEYS.extend(keys)
    return keys


def redact(text: str) -> str:
    for key in ALL_KEYS:
        text = text.replace(key, "***")
    return re.sub(r"\s+", " ", text)[:240]


def limit_headers(r: requests.Response) -> dict:
    return {k.lower(): v for k, v in r.headers.items()
            if "ratelimit" in k.lower() or k.lower() in ("retry-after", "x-ratelimit-limit-requests")}


def version_key(model_id: str):
    nums = tuple(float(n) for n in re.findall(r"\d+(?:\.\d+)?", model_id))
    stable = not re.search(r"preview|exp|beta", model_id)
    return (stable, nums)


def list_models(name: str, base: str, key: str):
    """Returns (status text, list of ids). Never raises."""
    if name == "github":
        url, headers = GITHUB_CATALOG, {"Authorization": f"Bearer {key}", "Accept": "application/vnd.github+json",
                                        "X-GitHub-Api-Version": "2022-11-28"}
    else:
        url, headers = f"{base}/models", {"Authorization": f"Bearer {key}"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
    except requests.RequestException as e:
        return f"listing failed: {redact(str(e))}", []
    if r.status_code != 200:
        return f"listing HTTP {r.status_code}: {redact(r.text)}", []
    try:
        data = r.json()
    except ValueError:
        return "listing returned non-JSON", []
    rows = data if isinstance(data, list) else data.get("data") or data.get("models") or []
    ids = []
    for row in rows:
        mid = row.get("id") or row.get("name") if isinstance(row, dict) else row
        if isinstance(mid, str):
            ids.append(mid[len("models/"):] if mid.startswith("models/") else mid)
    return f"listing ok, {len(ids)} models", ids


def pick(ids: list, patterns: list, fallback):
    for pattern in patterns:
        hits = sorted({i for i in ids if re.search(pattern, i, re.I)}, key=version_key, reverse=True)
        if hits:
            return hits[0], hits[:5]
    return fallback, []


def parse_answer(content: str):
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0)).get("answer")
    except (ValueError, AttributeError):
        return None


def chat(name: str, base: str, key: str, model: str, response_format) -> dict:
    body = {"model": model, "messages": MESSAGES, "temperature": 0, "max_tokens": 1024}
    if response_format:
        body["response_format"] = response_format
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if name == "openrouter":
        headers.update({"HTTP-Referer": "https://github.com/Jaiadhithya", "X-Title": "Moonveil smoke test"})
    for attempt in range(2):
        start = time.time()
        try:
            r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=120)
        except requests.RequestException as e:
            return {"status": "network", "error": redact(str(e)), "latency_s": round(time.time() - start, 1)}
        latency = round(time.time() - start, 1)
        if r.status_code == 429 and attempt == 0:
            try:
                wait = min(float(re.sub(r"[^\d.]", "", r.headers.get("retry-after", "")) or 10), 30)
            except ValueError:
                wait = 10.0
            time.sleep(wait)
            continue
        out = {"status": r.status_code, "latency_s": latency, "limits": limit_headers(r)}
        if r.status_code != 200:
            out["error"] = redact(r.text)
            return out
        try:
            data = r.json()
            content = data["choices"][0]["message"].get("content") or ""
            out["answer"] = parse_answer(content)
            out["usage"] = data.get("usage", {})
            out["served_model"] = data.get("model", "")
            if out["answer"] is None:
                out["error"] = "could not parse JSON from: " + redact(content)
        except (ValueError, KeyError, IndexError, TypeError) as e:
            out["error"] = f"unexpected response shape: {e}"
        return out
    return {"status": 429, "error": "rate limited twice"}


def test_model(name: str, base: str, key: str, model: str) -> dict:
    """Tries json_schema, then json_object, then plain. Stops at the first mode that answers 42."""
    tried = []
    for mode, fmt in MODES:
        res = chat(name, base, key, model, fmt)
        res["mode"] = mode
        tried.append(res)
        time.sleep(GAP_SECONDS.get(name, 2.0))
        if res.get("answer") == 42:
            break
        if res["status"] in (401, 403, 404, "network"):
            break  # a bad key, a wrong id or no network will not improve with another mode
    best = next((t for t in tried if t.get("answer") == 42), tried[-1])
    best["modes_tried"] = [f"{t['mode']}:{t['status']}{'' if t.get('answer') == 42 else ' x'}" for t in tried]
    return best


def openrouter_key_info(key: str) -> str:
    try:
        r = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}, timeout=30)
        d = r.json().get("data", {}) if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return "key info unavailable"
    if not d:
        return f"key info HTTP {r.status_code}"
    return (f"free tier: {d.get('is_free_tier')}, limit: {d.get('limit')}, usage: {d.get('usage')}, "
            f"rate_limit: {d.get('rate_limit')}")


def run_provider(name: str, keys: list) -> list:
    base, _ = PROVIDERS[name]
    lines = [f"\n{name}: {len(keys)} key(s)"]
    status, ids = list_models(name, base, keys[0])
    lines.append(f"  {status}")
    if name == "openrouter":
        lines.append(f"  {openrouter_key_info(keys[0])}")
    first_ok_model = None
    for slot, patterns, fallback in SLOTS[name]:
        model, candidates = pick(ids, patterns, fallback)
        if candidates:
            lines.append(f"  {slot}: candidates {', '.join(candidates)}")
        if not model:
            lines.append(f"  {slot}: NO MATCH in the listing; check the console for the exact id")
            continue
        res = test_model(name, base, keys[0], model)
        ok = res.get("answer") == 42
        first_ok_model = first_ok_model or (model if ok else None)
        lines.append(f"  {slot}: {'OK ' if ok else 'FAIL'} id={model} mode={res['mode'] if ok else '-'} "
                     f"latency={res.get('latency_s')}s tried=[{', '.join(res['modes_tried'])}]")
        if res.get("served_model") and res["served_model"] != model:
            lines.append(f"    served as: {res['served_model']}")
        if res.get("usage"):
            lines.append(f"    usage: {json.dumps(res['usage'])[:200]}")
        if res.get("limits"):
            lines.append(f"    limits: {json.dumps(res['limits'])[:300]}")
        if not ok and res.get("error"):
            lines.append(f"    error: {res['error']}")
    # Extra keys: one plain call each on a model that worked, so rotation is known to work.
    for n, key in enumerate(keys[1:], start=2):
        if not first_ok_model:
            lines.append(f"  key{n}: not checked (no model worked with key1)")
            continue
        res = chat(name, base, key, first_ok_model, None)
        time.sleep(GAP_SECONDS.get(name, 2.0))
        lines.append(f"  key{n}: {'OK' if res.get('answer') == 42 else 'FAIL'} HTTP {res['status']}"
                     + (f" error: {res['error']}" if res.get("error") and res.get("answer") != 42 else ""))
    return lines


def run_tavily(keys: list) -> list:
    lines = [f"\ntavily: {len(keys)} key(s)"]
    for n, key in enumerate(keys, start=1):
        start = time.time()
        try:
            r = requests.post("https://api.tavily.com/search", timeout=60,
                              headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                              json={"query": "ICICI Bank quarterly results net interest margin",
                                    "max_results": 2, "search_depth": "basic"})
        except requests.RequestException as e:
            lines.append(f"  key{n}: network error {redact(str(e))}")
            continue
        latency = round(time.time() - start, 1)
        if r.status_code == 200:
            results = r.json().get("results", [])
            lines.append(f"  key{n}: OK {len(results)} results in {latency}s; first: "
                         f"{results[0].get('url', '') if results else '-'}")
        else:
            lines.append(f"  key{n}: FAIL HTTP {r.status_code} {redact(r.text)}")
    return lines


def main() -> None:
    env = load_env()
    if not (HERE / ".env").exists():
        print("No .env in the repo root; using environment variables only. "
              "Copy .env.example to .env and fill it in.")
    lines = ["==== SUMMARY (paste this into the thread) ====",
             f"models smoke test {datetime.datetime.now().isoformat(timespec='seconds')}"]
    skipped = []
    for name, (_, var) in PROVIDERS.items():
        keys = keys_for(env, var)
        if not keys:
            skipped.append(f"{name} ({var} empty)")
            continue
        print(f"... {name}", flush=True)
        try:
            lines += run_provider(name, keys)
        except Exception as e:
            lines.append(f"\n{name}: crashed: {redact(repr(e))}")
    tavily = keys_for(env, "TAVILY_API_KEYS")
    if tavily:
        print("... tavily", flush=True)
        lines += run_tavily(tavily)
    else:
        skipped.append("tavily (TAVILY_API_KEYS empty)")
    if skipped:
        lines.append("\nskipped: " + "; ".join(skipped))
    text = "\n".join(lines)
    (HERE / "reports").mkdir(exist_ok=True)
    out = HERE / "reports" / f"models_smoke_{datetime.date.today().isoformat()}.md"
    out.write_text(text + "\n", encoding="utf-8")
    print("\n" + text)
    print(f"\nSaved to {out.name}. It contains no keys.")


if __name__ == "__main__":
    main()
