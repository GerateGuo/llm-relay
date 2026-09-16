#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用**合成数据**重出文档用的面板截图（T2.5）。

为什么要有这个脚本：仓库里的截图必须是「谁都能复现的假数据」，不能是某个人线上部署的截屏
（那会带真实厂商名、真实记忆条数、内网地址）。这个脚本就是那份假数据：

  1. 在 /tmp 下建一个一次性工作目录，写一份 demo 配置（两个 provider，全部指向本机假上游）；
  2. 进程内起两个假上游（一个正常、一个专门回 429）与一个中继实例（显式空闲端口 ——
     千万别传 0：`int(port or listen.port)` 会把 0 吃掉，落到配置里的端口上）；
  3. 打一批真假混合的请求，让「请求日志 / 用量 / Key 池冷却」都有内容；
  4. 起一个临时面板（访问密钥在临时目录里，不碰线上 9111），逐 tab 截图到 docs/。

用法：`cd <repo> && python3 dev/make_demo_shots.py`
依赖：无（截图用 Chrome 无头模式，macOS 默认路径；其它平台设 CHROME=/path/to/chrome）。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import llm_relay            # noqa: E402  （仓库根目录下的中继本体）
import mock_provider        # noqa: E402  （仓库根目录下的假上游）

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 回环保绕系统代理
DOCS = ROOT / "docs"
SHOTS = [                    # (tab 名, 输出文件名)  —— 与 llm-relay-dashboard.py 的 TABS 对齐
    ("overview", "dashboard-overview.png"),
    ("chain", "dashboard-chain.png"),
    ("keys", "dashboard-keys.png"),
    ("logs", "dashboard-logs.png"),
    ("usage", "dashboard-usage.png"),
    ("callers", "dashboard-callers.png"),
    ("caps", "dashboard-caps.png"),
    ("settings", "dashboard-settings.png"),
]
NARROW = ("overview", "dashboard-overview-narrow-480.png")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:              # 理论不可达：路径是本仓库内的 .py
        raise RuntimeError(f"加载不了模块：{path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def demo_config(mock_a: str, mock_b: str, usage_path: Path, port: int) -> dict:
    """两个 provider + 命名路由 + 一个调用方 —— 全是假名字，不映射任何真实厂商。"""
    def provider(name, base_url, keys, rpm, models, note):
        return {
            "name": name, "enabled": True, "base_url": base_url, "free": True,
            "wire": {"max_tokens_param": "max_tokens", "extra_headers": {}},
            "keys": keys, "rpm": rpm, "note": note,
            "max_concurrency": 4, "key_cooldown_s": 60, "provider_cooldown_s": 120,
            "cooldown_backoff": {"initial_s": 30, "max_s": 300, "factor": 2},
            "models": models,
        }

    def model(mid, chain, caps):
        return {"id": mid, "chain": chain, "caps": caps, "params": {}}

    native = {"json_schema": "native", "tools": True, "reasoning_only": False}
    return {
        "_comment": "本文件由 dev/make_demo_shots.py 生成，只用于重出文档截图（合成数据）。",
        "listen": {"host": "127.0.0.1", "port": port},
        "default_alias": "default",
        "auth": {"mode": "loopback_trust"},
        "callers": {
            "demo-app": {"key_env": "DEMO_APP_KEY", "rpm": 60, "daily_tokens": 2000000,
                         "allow_routes": ["default", "strict-json"],
                         "note": "示例调用方（截图用）"}
        },
        "request": {
            "per_attempt_timeout_s": 20, "reasoning_min_timeout_s": 15, "total_budget_s": 45,
            "max_candidates": 3, "queue_timeout_s": 10, "validate_json": True,
            "json_extract_fallback": True, "schema_min_max_tokens": 1024,
            "provider_cooldown_cap_s": 120, "provider_cooldown_hard_cap_s": 300,
        },
        "concurrency": {"global": 8},
        "usage_log": {"enabled": True, "path": str(usage_path), "max_mb": 5, "keep": 3},
        "key_state": {},
        "providers": [
            provider("demo-alpha", mock_a, ["DEMO_ALPHA_KEY_1", "DEMO_ALPHA_KEY_2"], 30,
                     [model("mock-ok", 10, native),
                      model("mock-json", 20, {"json_schema": "native", "tools": False,
                                              "reasoning_only": False})],
                     "合成数据：指向本机假上游"),
            provider("demo-beta", mock_b, ["DEMO_BETA_KEY_1"], 10,
                     [model("mock-429", 30, native)],
                     "合成数据：这个假上游专门回 429，用来看冷却"),
        ],
        "local_fallback": {"enabled": False},
        "routes": {
            "default": {"chain": "auto", "policy": {}},
            "strict-json": {"chain": ["demo-alpha/mock-json"],
                            "policy": {"validate_json": True, "max_candidates": 1}},
        },
    }


def post(port: int, payload: dict, token: str | None = None, path: str = "/v1/chat/completions"):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {token}"} if token else {})},
        method="POST")
    try:
        with OPENER.open(req, timeout=60) as r:
            return r.status, r.read()[:200]
    except urllib.error.HTTPError as e:                     # 400/429/502 都是预期内的演示数据
        return e.code, e.read()[:200]
    except Exception as exc:                                # noqa: BLE001
        return 0, str(exc).encode()


def warm_up(port: int, caller_key: str) -> list:
    """打一批请求：成功、json_schema、429、未知模型、带调用方身份的都有。"""
    out = []
    simple = {"model": "default", "messages": [{"role": "user", "content": "hello"}],
              "max_tokens": 32}
    for _ in range(6):
        out.append(post(port, simple))
    schema_payload = {
        "model": "strict-json",
        "messages": [{"role": "user", "content": "从这句话里抽事实：今天下雨了。"}],
        "max_tokens": 256,
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "facts", "schema": {"type": "object", "properties": {
                "facts": {"type": "array", "items": {"type": "string"}},
                "language": {"type": "string"}},
                "required": ["facts", "language"], "additionalProperties": False}}},
    }
    for _ in range(3):
        out.append(post(port, schema_payload))
    for _ in range(3):
        out.append(post(port, {**simple, "model": "demo-beta/mock-429"}))
    for _ in range(4):
        out.append(post(port, simple, token=caller_key))
    out.append(post(port, {**simple, "model": "no-such-route"}))       # 400 未知路由
    return out


def main() -> int:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("别用 root 跑：截图会被写进 docs/")
        return 2
    work = Path(f"/tmp/llm-relay-demo-{os.getpid()}")       # 用短的中性路径（截图里会显示 config 路径）
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    DOCS.mkdir(exist_ok=True)

    mock_a = mock_provider.MockProvider(port=free_port()).start()
    mock_b = mock_provider.MockProvider(port=free_port(), scenario="rate_limit").start()
    relay_port, dash_port = free_port(), free_port()

    keys_path = work / "keys.env"
    keys_path.write_text("\n".join([
        "export DEMO_ALPHA_KEY_1=demo-alpha-key-1",
        "export DEMO_ALPHA_KEY_2=demo-alpha-key-2",
        "export DEMO_BETA_KEY_1=demo-beta-key-1",
        "export DEMO_APP_KEY=demo-app-caller-key",
    ]) + "\n", encoding="utf-8")

    config_path = work / "config.json"
    config_path.write_text(json.dumps(
        demo_config(mock_a.base_url, mock_b.base_url, work / "usage.jsonl", relay_port),
        ensure_ascii=False, indent=2), encoding="utf-8")

    relay = llm_relay.Relay(config_path, keys_path, quiet=True)
    relay_srv = llm_relay.create_server(relay, "127.0.0.1", relay_port)
    relay_srv.daemon_threads = True
    threading.Thread(target=relay_srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    time.sleep(0.4)
    print(f"中继：http://127.0.0.1:{relay_port}  假上游：{mock_a.base_url} / {mock_b.base_url}")

    results = warm_up(relay_port, "demo-app-caller-key")
    codes: dict = {}
    for code, _ in results:
        codes[code] = codes.get(code, 0) + 1
    print(f"预热请求 {len(results)} 条，状态码分布：{codes}")

    # ---- 面板：临时密钥文件 + 临时 ui 目录，只监听 127.0.0.1 ----
    dash = load_module("llm_relay_dashboard", ROOT / "llm-relay-dashboard.py")
    dash.RELAY_BASE = f"http://127.0.0.1:{relay_port}"
    dash.DEFAULT_KEY_FILE = str(work / "access-key.txt")
    dash.UI_DIR = str(work / "ui")
    os.makedirs(dash.UI_DIR, exist_ok=True)
    dash.Handler.access_key = dash.load_or_create_access_key(dash.DEFAULT_KEY_FILE)
    dash_srv = dash.ThreadingHTTPServer(("127.0.0.1", dash_port), dash.Handler)
    dash_srv.daemon_threads = True
    threading.Thread(target=dash_srv.serve_forever, kwargs={"poll_interval": 0.05},
                     daemon=True).start()
    time.sleep(0.4)
    base = f"http://127.0.0.1:{dash_port}"
    print(f"面板：{base}")

    smoke = load_module("dash_smoke", HERE / "dashboard_smoke_test.py")
    written = []
    for tab, fname in SHOTS + [NARROW]:
        out = DOCS / fname
        width, height = (480, 1500) if "narrow" in fname else (1280, 1500)
        url = f"{base}/?snapshot=1&tab={tab}#{tab}"
        smoke.chrome_screenshot(url, out, width, height)
        size = out.stat().st_size if out.exists() else 0
        flag = "OK " if size > 100_000 else "⚠️ 偏小（可能是假象）"
        written.append((fname, size))
        print(f"  {flag} {fname}  {size:,} B")

    relay_srv.shutdown()
    dash_srv.shutdown()
    mock_a.stop()
    mock_b.stop()
    shutil.rmtree(work, ignore_errors=True)

    small = [n for n, s in written if s <= 100_000]
    print(f"\n共 {len(written)} 张，落盘 {DOCS}/")
    if small:
        print(f"⚠️ 这些偏小、需要人工看一眼：{small}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
