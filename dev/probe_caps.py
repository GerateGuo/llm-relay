#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Capability probe — 对免费源做"能不能真的当 Hindsight 后端"的硬指标测试。

测什么（全部是 Hindsight 真实会发的调用形态）：
  1. chat       : 基础可用性 + 延迟
  2. json_schema: strict 结构化输出 → **内容必须能 json.loads 且字段齐全**（只看 HTTP 200 是假阳性）
  3. tools      : tool_choice="auto" 是否真的回 tool_calls
  4. concurrency: 并发放几个请求，看是否吃 429（免费源最致命的指标）

结果同时打到 stdout 和 results JSONL（供后续汇总，不靠记忆）。
永不打印 key。
"""
from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "probe_results.jsonl"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {"type": "string"}},
        "language": {"type": "string"},
    },
    "required": ["facts", "language"],
    "additionalProperties": False,
}
FACT_PROMPT = (
    "从下面这段对话抽取事实，输出 JSON。\n"
    "User: 我在 Mac 上用 Hindsight 做长期记忆，embedding 走 SiliconFlow 的 BGE-m3。\n"
    "Assistant: 收到，已记录。"
)
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "查询城市当前时间",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        },
    }
]

PROVIDERS = {
    # name: (base_url, extra_headers, [models])
    "zen": (
        "https://opencode.ai/zen/v1",
        {"x-opencode-session": "hermes-llm-relay-probe", "x-opencode-client": "hermes-llm-relay"},
        ["mimo-v2.5-free", "nemotron-3.5-lightning-free", "big-pickle"],
    ),
    "sensenova": (
        "https://token.sensenova.cn/v1",
        {},
        ["deepseek-v4-flash", "deepseek-v4-pro", "kimi-k3", "glm-5.2", "sensenova-6.8-flash-lite"],
    ),
    "nvidia": (
        "https://integrate.api.nvidia.com/v1",
        {},
        ["deepseek-ai/deepseek-v4-flash-0731", "deepseek-ai/deepseek-v4-pro-0813", "moonshotai/kimi-k3", "z-ai/glm-5.3-flash"],
    ),
}


def load_keys() -> dict[str, str]:
    keys: dict[str, str] = {}
    f = pathlib.Path.home() / ".hermes/llm-relay/keys.env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("export ") and "=" in line:
                n, v = line[len("export ") :].split("=", 1)
                keys[n.strip()] = v.strip().strip('"')
    return keys


def call(base: str, headers: dict, key: str, payload: dict, timeout: int = 180):
    h = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": UA,
        "Accept": "application/json",
    }
    h.update(headers)
    req = urllib.request.Request(base + "/chat/completions", data=json.dumps(payload).encode(), headers=h)
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace")), time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw), time.time() - t0
        except json.JSONDecodeError:
            return e.code, raw[:300], time.time() - t0
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


def msg_of(body) -> dict:
    if isinstance(body, dict):
        return (body.get("choices") or [{}])[0].get("message") or {}
    return {}


def record(kind: str, provider: str, model: str, status: int, dt: float, verdict: str, detail: str = "") -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "kind": kind,
        "provider": provider,
        "model": model,
        "http": status,
        "latency_s": round(dt, 1),
        "verdict": verdict,
        "detail": detail[:300],
    }
    with OUT.open("a") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  {provider:10s} {model:36s} http={status:<4} {dt:6.1f}s  {verdict}  {detail[:90]}")


def probe_model(provider: str, base: str, headers: dict, key: str, model: str) -> None:
    # 1) chat
    st, body, dt = call(base, headers, key, {"model": model, "max_tokens": 64, "messages": [{"role": "user", "content": "只回答两个字：在的"}]})
    ok = st == 200 and msg_of(body).get("content") is not None
    record("chat", provider, model, st, dt, "UP" if ok else "DOWN", json.dumps(body, ensure_ascii=False)[:200] if not ok else repr((msg_of(body).get("content") or "")[:50]))

    # 2) strict json_schema —— 必须真的 parse 得出且字段齐全
    st, body, dt = call(
        base,
        headers,
        key,
        {
            "model": model,
            "max_tokens": 500,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": FACT_PROMPT}],
            "response_format": {"type": "json_schema", "json_schema": {"name": "facts", "strict": True, "schema": SCHEMA}},
        },
    )
    verdict, detail = "JSON_FAIL", ""
    if st == 200:
        c = msg_of(body).get("content") or ""
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict) and isinstance(parsed.get("facts"), list) and "language" in parsed:
                verdict, detail = "JSON_OK", f"facts={len(parsed['facts'])} {parsed['facts'][:1]}"
            else:
                verdict, detail = "JSON_SHAPE_BAD", str(parsed)[:150]
        except Exception:
            verdict, detail = "JSON_UNPARSEABLE", repr(c[:150])
    else:
        detail = json.dumps(body, ensure_ascii=False)[:200] if isinstance(body, dict) else str(body)[:200]
    record("json_schema", provider, model, st, dt, verdict, detail)

    # 3) tools
    st, body, dt = call(
        base, headers, key,
        {"model": model, "max_tokens": 200, "messages": [{"role": "user", "content": "北京现在几点？用工具查。"}], "tools": TOOLS, "tool_choice": "auto"},
    )
    tc = msg_of(body).get("tool_calls") if st == 200 else None
    record("tools", provider, model, st, dt, "TOOLS_OK" if tc else "TOOLS_NO", (json.dumps(tc, ensure_ascii=False)[:150] if tc else json.dumps(body, ensure_ascii=False)[:150]))


def probe_concurrency(provider: str, base: str, headers: dict, keys: list[str], model: str, n: int = 4) -> None:
    """并发 n 个请求（key 池轮转），看是否吃 429 —— 免费源最致命的指标。"""
    results: list[tuple[int, float]] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        key = keys[i % len(keys)]
        st, body, dt = call(base, headers, key, {"model": model, "max_tokens": 32, "messages": [{"role": "user", "content": f"回复数字 {i}"}]})
        with lock:
            results.append((st, dt))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    codes = [s for s, _ in results]
    n429 = sum(1 for c in codes if c == 429)
    n200 = sum(1 for c in codes if c == 200)
    verdict = "CONC_OK" if n200 == n else ("CONC_429" if n429 else "CONC_FAIL")
    record("concurrency", provider, model, codes[0] if codes else 0, wall, verdict, f"codes={codes} ok={n200}/{n}")


def main() -> int:
    provider = sys.argv[1] if len(sys.argv) > 1 else "sensenova"
    base, headers, models = PROVIDERS[provider]
    keys = load_keys()
    if provider == "zen":
        pool = [keys.get("ZEN_KEY", "")]
    elif provider == "sensenova":
        pool = [v for k, v in sorted(keys.items()) if k.startswith("SENSENOVA_KEY_") and v]
    else:
        pool = [keys.get("NVIDIA_KEY", "")]
    pool = [k for k in pool if k]
    print(f"provider={provider} keys_in_pool={len(pool)} models={len(models)}")
    if not pool:
        print("no key for provider, skip")
        return 1
    for m in models:
        print(f"--- {m} ---")
        probe_model(provider, base, headers, pool[0], m)
    print(f"--- concurrency x4 on {models[0]} ---")
    probe_concurrency(provider, base, headers, pool, models[0])
    print(f"\nresults appended -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
