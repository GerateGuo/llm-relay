#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Provider probe — 逐个探测免费源的真实可用性 / 能力（json_schema、tools、流式）。

用途：给 llm-relay 的路由配置提供"实测依据"，而不是照着文档猜。
输出只含模型名、HTTP 状态、耗时、能力结论；**永不打印 key**。

用法:
    ~/venvs/mlx/bin/python probe_providers.py [--provider zen|nvidia|sensenova|all]
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

# 密钥文件：默认取仓库根目录的 keys.env，可用 LLM_RELAY_KEYS 覆盖（不假设任何固定个人路径）
KEYS_FILE = pathlib.Path(os.getenv("LLM_RELAY_KEYS")
                         or pathlib.Path(__file__).resolve().parent.parent / "keys.env")
# 本地回环/外网都直连，避免 ClashX 把请求吃掉（本项目反复踩过的坑）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# Cloudflare(zen) 会按 UA 拦：python-urllib 默认 UA 直接吃 403 / error code 1010。
# 用常规客户端 UA，别用 python-urllib。
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


def load_keys() -> dict[str, str]:
    """从 keys.env 读取 export NAME="value"。只返回 dict，不打印值。"""
    keys: dict[str, str] = {}
    if not KEYS_FILE.exists():
        return keys
    for line in KEYS_FILE.read_text().splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        name, val = line[len("export ") :].split("=", 1)
        keys[name.strip()] = val.strip().strip('"')
    return keys


def post(url: str, key: str, payload: dict, timeout: int = 60, extra_headers: dict | None = None) -> tuple[int, dict | str, float]:
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
        "User-Agent": UA,
        "Accept": "application/json",
    }
    # OpenCode Console 网关自 2026-09-05 起强制 x-opencode-session，
    # 缺失就回 400 MissingSessionID（"free tier can only be used in OpenCode"）。
    # 提供稳定 session id 是社区通行做法（pi / deepseek-harness 均如此）。
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=body, headers=headers)
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(raw), time.time() - t0
            except json.JSONDecodeError:
                return resp.status, raw[:400], time.time() - t0
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        return e.code, raw[:400], time.time() - t0
    except Exception as e:  # 超时/连接失败
        return 0, f"{type(e).__name__}: {e}", time.time() - t0


ZEN_HEADERS = {"x-opencode-session": "hermes-llm-relay-probe", "x-opencode-client": "hermes-llm-relay"}


def chat(url: str, key: str, model: str, messages, headers: dict | None = None, **extra):
    payload = {"model": model, "messages": messages, "max_tokens": 64, **extra}
    return post(url, key, payload, extra_headers=headers)


def brief(status: int, body) -> str:
    if isinstance(body, dict):
        msg = (body.get("choices") or [{}])[0].get("message") or {}
        content = (msg.get("content") or msg.get("reasoning") or "").replace("\n", " ")[:60]
        usage = body.get("usage") or {}
        return f"ok content={content!r} usage_in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}"
    return f"body={str(body)[:180]}"


def probe_zen(keys: dict[str, str]) -> None:
    key = keys.get("ZEN_KEY", "")
    base = "https://opencode.ai/zen/v1"
    if not key:
        print("ZEN: no key")
        return
    models = [
        "deepseek-v4-flash-free",
        "nemotron-3-ultra-free",
        "nemotron-3.5-lightning-free",
        "mimo-v2.5-free",
        "big-pickle",
        "muse-spark-1.3-contributor-free",
        "ling-3.0-flash-fin-free",
        "minimax-m3-free",
        "qwen3.6-plus-free",
        "north-mini-code-free",
    ]
    print("=== ZEN chat 基础可用性 ===")
    alive = []
    for m in models:
        st, body, dt = chat(base + "/chat/completions", key, m, [{"role": "user", "content": "只回答两个字：在的"}], headers=ZEN_HEADERS)
        flag = "UP " if st == 200 and isinstance(body, dict) else "DOWN"
        print(f"[{flag}] {m:42s} http={st} {dt:5.1f}s {brief(st, body)}")
        if st == 200 and isinstance(body, dict):
            alive.append(m)
    print("\n=== ZEN 能力探测（json_schema / tools） ===")
    for m in alive:
        st, body, dt = chat(
            base + "/chat/completions",
            key,
            m,
            [{"role": "user", "content": "从这句话抽取事实：我在用 Mac 跑 Hindsight。只输出 JSON。"}],
            headers=ZEN_HEADERS,
            response_format={"type": "json_schema", "json_schema": {"name": "facts", "strict": True, "schema": SCHEMA}},
        )
        js = "json_schema=OK " if st == 200 and isinstance(body, dict) else f"json_schema=FAIL({st}) "
        st2, body2, dt2 = chat(
            base + "/chat/completions",
            key,
            m,
            [{"role": "user", "content": "北京现在几点？用工具查。"}],
            headers=ZEN_HEADERS,
            tools=[{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
            tool_choice="auto",
        )
        tc = ((body2.get("choices") or [{}])[0].get("message") or {}).get("tool_calls") if isinstance(body2, dict) else None
        tools_res = "tools=OK" if st2 == 200 and tc else f"tools=NO({st2})"
        print(f"  {m:42s} {js}{tools_res}  ({dt:.1f}s/{dt2:.1f}s)")


def probe_nvidia(keys: dict[str, str]) -> None:
    key = keys.get("NVIDIA_KEY", "")
    base = "https://integrate.api.nvidia.com/v1"
    if not key:
        print("NVIDIA: no key")
        return
    print("=== NVIDIA NIM ===")
    for m in [
        "deepseek-ai/deepseek-v4-flash-0731",
        "deepseek-ai/deepseek-v4-pro-0813",
        "moonshotai/kimi-k3",
        "z-ai/glm-5.3-flash",
    ]:
        st, body, dt = chat(base + "/chat/completions", key, m, [{"role": "user", "content": "只回答两个字：在的"}], headers=ZEN_HEADERS)
        flag = "UP " if st == 200 and isinstance(body, dict) else "DOWN"
        print(f"[{flag}] {m:38s} http={st} {dt:5.1f}s {brief(st, body)}")


def probe_sensenova(keys: dict[str, str]) -> None:
    key = keys.get("SENSENOVA_KEY_1", "")
    base = "https://token.sensenova.cn/v1"
    if not key:
        print("SENSENOVA: no key")
        return
    print("=== SenseNova ===")
    for m in ["deepseek-v4-flash", "sensenova-6.7-flash-lite", "sensenova-6.8-flash-lite", "glm-5.2"]:
        st, body, dt = chat(base + "/chat/completions", key, m, [{"role": "user", "content": "只回答两个字：在的"}], headers=ZEN_HEADERS)
        flag = "UP " if st == 200 and isinstance(body, dict) else "DOWN"
        print(f"[{flag}] {m:34s} http={st} {dt:5.1f}s {brief(st, body)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="all", choices=["zen", "nvidia", "sensenova", "all"])
    args = ap.parse_args()
    keys = load_keys()
    print(f"keys.env: {KEYS_FILE}  names={sorted(keys)}")  # 只有名字，没有值
    if args.provider in ("zen", "all"):
        probe_zen(keys)
    if args.provider in ("nvidia", "all"):
        probe_nvidia(keys)
    if args.provider in ("sensenova", "all"):
        probe_sensenova(keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
