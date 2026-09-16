#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_relay.py —— llm-relay 单元 + 集成测试（全部打本地 mock，不烧真额度）。

跑法：~/hindsight-mac-env/bin/python -m unittest -v test_relay
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import llm_relay
from mock_provider import MockProvider

# 测试进程也可能被 ClashX 的代理环境变量影响 → 回环请求一律绕代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def model_cfg(mid, chain=1, caps=None, params=None):
    return {
        "id": mid,
        "chain": chain,
        "caps": caps or {"json_schema": "native", "tools": True, "reasoning_only": False},
        "params": params or {},
    }


def provider_cfg(name, models, keys, base_url, *, enabled=True, rpm=600, max_concurrency=4,
                 key_cooldown_s=60, provider_cooldown_s=60, wire=None, backoff=None, free=False):
    return {
        "name": name,
        "enabled": enabled,
        "free": free,
        "base_url": base_url,
        "wire": wire or {"max_tokens_param": "max_tokens", "extra_headers": {}},
        "keys": keys,
        "rpm": rpm,
        "max_concurrency": max_concurrency,
        "key_cooldown_s": key_cooldown_s,
        "provider_cooldown_s": provider_cooldown_s,
        "cooldown_backoff": backoff or {"initial_s": 5, "max_s": 20, "factor": 2},
        "models": [_as_model(m) for m in models],
    }


def _as_model(m):
    if isinstance(m, dict):
        return m
    if isinstance(m, (tuple, list)):
        return model_cfg(*m)
    return model_cfg(m)


SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}, "language": {"type": "string"}},
    "required": ["facts", "language"],
    "additionalProperties": False,
}
JSON_REQ = {
    "type": "json_schema",
    "json_schema": {"name": "facts", "strict": True, "schema": SCHEMA},
}
TOOLS = [{"type": "function", "function": {"name": "get_time", "description": "查时间",
                                           "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]


def _lan_ip() -> str:
    """本机 LAN IP（非 loopback），用来从「远程来源」访问管理接口。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        s.close()


LAN_IP = _lan_ip()
USAGE_KEYS = ("caller", "route", "provider", "model")


def _usage_row(ts: float, *, caller="hindsight", route="hindsight", provider="p1",
               model="mock-ok", http=200, prompt=10, completion=5) -> dict:
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)),
            "alias": route, "caller": caller, "provider": provider, "model": model,
            "key_index": 0, "http": http, "latency_ms": 100,
            "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "verdict": "ok" if http == 200 else "relay_all_failed"}


class RelayTestCase(unittest.TestCase):
    """公共脚手架：一个 mock 上游 + 一个临时目录（config.json / keys.env 都写在这里）。"""

    hang_s = 3.0

    def setUp(self) -> None:
        self.mock = MockProvider(hang_s=self.hang_s).start()
        self.addCleanup(self.mock.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="llm-relay-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.config_path = self.tmp / "config.json"
        self.keys_path = self.tmp / "keys.env"
        self._servers: list = []

    # ---------------- 脚手架 ----------------
    def write_keys(self, keys: dict) -> None:
        self.keys_path.write_text("".join(f'export {k}="{v}"\n' for k, v in keys.items()), encoding="utf-8")

    def write_config(self, providers, *, request=None, concurrency=None, local_fallback=None,
                     token="", keys=None, routes=None, auth=None, callers=None, usage_log=None) -> Path:
        cfg = {
            "listen": {"host": "127.0.0.1", "port": 0},
            "local_token": token,
            "default_alias": "default",
            "request": {
                "per_attempt_timeout_s": 5,
                "total_budget_s": 30,
                "max_candidates": 3,
                "queue_timeout_s": 10,
                "validate_json": True,
                "json_extract_fallback": True,
                **(request or {}),
            },
            "concurrency": {"global": 8, **(concurrency or {})},
            "providers": providers,
            "local_fallback": local_fallback or {"enabled": False},
            "routes": (routes if routes is not None else
                       {"default": {"chain": "auto"}, "hindsight": {"chain": "auto"}}),
            "auth": auth or {"mode": "loopback_trust"},
            "callers": callers or {},
            "usage_log": (usage_log if usage_log is not None else {"enabled": False}),
        }
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_keys(keys if keys is not None else {"TEST_KEY_A": "k1-ok", "TEST_KEY_B": "k2-ok"})
        return self.config_path

    def make_relay(self, providers=None, **kw) -> llm_relay.Relay:
        if providers is not None:
            self.write_config(providers, **kw)
        return llm_relay.Relay(self.config_path, self.keys_path, quiet=True)

    def mock_provider(self, name="p1", models=None, keys=("TEST_KEY_A", "TEST_KEY_B"), **kw):
        return provider_cfg(name, models or ["mock-ok"], list(keys), self.mock.base_url, **kw)

    def start_http(self, relay: llm_relay.Relay) -> int:
        srv = llm_relay.create_server(relay, "127.0.0.1", 0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self._servers.append(srv)
        return srv.server_address[1]

    def http(self, port: int, path: str, *, payload=None, token=None, timeout=30):
        url = f"http://127.0.0.1:{port}{path}"
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if payload is not None else "GET")
        try:
            with OPENER.open(req, timeout=timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers or {})

    def http_json(self, port, path, **kw):
        status, raw, headers = self.http(port, path, **kw)
        try:
            return status, json.loads(raw.decode()), headers
        except Exception:  # noqa: BLE001
            return status, {"_raw": raw.decode("utf-8", "replace")}, headers

    @staticmethod
    def ask(text="只回答两个字：在的", **extra):
        payload = {"model": "hindsight", "messages": [{"role": "user", "content": text}], "stream": False}
        payload.update(extra)
        return payload

    # ============================================================ 1) 429 换 key
    def test_01_429_switches_to_second_key(self):
        relay = self.make_relay([self.mock_provider()], keys={"TEST_KEY_A": "k1-busy", "TEST_KEY_B": "k2-ok"})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["key_index"], 1, "429 后必须换到第 2 把 key")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit", "http_ok"])
        stats = self.mock.stats()
        self.assertEqual(len(stats["per_key_fp"]), 2, "假源应看到两把不同的 key")
        self.assertEqual(stats["per_behavior"].get("429reset"), 1)
        self.assertNotIn("k1-busy", relay.log_lines[-1], "日志不得出现 key 明文")

    # ============================================================ 2) 401 禁用 key
    def test_02_401_disables_key_then_uses_next(self):
        relay = self.make_relay([self.mock_provider()], keys={"TEST_KEY_A": "k1-dead", "TEST_KEY_B": "k2-ok"})
        status, _, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, meta)
        self.assertEqual(meta["key_index"], 1)
        self.assertTrue(relay.providers["p1"].keys[0].disabled, "401 的 key 必须被标记 disabled")
        self.assertIn("鉴权失败", relay.providers["p1"].keys[0].disabled_reason)
        status2, _, meta2 = relay.chat(self.ask())
        self.assertEqual(status2, 200)
        self.assertEqual(meta2["key_index"], 1)
        self.assertEqual(len(relay.providers["p1"].keys[0].fp), 8)
        st = relay.status()
        self.assertTrue(st["providers"][0]["keys"][0]["disabled"])
        self.assertNotIn("k1-dead", json.dumps(st, ensure_ascii=False))

    # ==================================================== 3) 500 换候选 + provider 冷却
    def test_03_500_jumps_candidate_and_cools_provider(self):
        bad = provider_cfg("p_bad", ["mock-500"], ["TEST_KEY_A"], self.mock.base_url)
        good = provider_cfg("p_good", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([bad, good])
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_good")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["server", "http_ok"])
        self.assertGreater(relay.providers["p_bad"].cooldown_left(), 0, "5xx 后 provider 必须进冷却")
        st = relay.status()
        self.assertGreater(st["providers"][0]["cooldown_left_s"], 0)
        self.assertIn("server", st["providers"][0]["counts"])

    # ============================================================ 4) 超时换候选
    def test_04_timeout_jumps_candidate(self):
        bad = provider_cfg("p_slow", ["mock-hang"], ["TEST_KEY_A"], self.mock.base_url)
        good = provider_cfg("p_good", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([bad, good], request={"per_attempt_timeout_s": 1})
        t0 = time.time()
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_good")
        self.assertEqual(meta["attempts"][0]["kind"], "transport")
        self.assertLess(time.time() - t0, 10)
        self.assertGreater(relay.providers["p_slow"].cooldown_left(), 0)

    # ================================================ 5) reasoning-only → content 提升
    def test_05_reasoning_promoted_and_json_parsed(self):
        relay = self.make_relay([self.mock_provider(models=["mock-reasoning"])])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        self.assertEqual(parsed["language"], "zh")
        self.assertTrue(meta["promoted"], "必须记录 reasoning→content 提升")
        self.assertIsNotNone(body["choices"][0]["message"].get("reasoning_content"),
                             "reasoning 字段要保留，不能删")

    def test_06_nonstandard_reasoning_field_promoted(self):
        relay = self.make_relay([self.mock_provider(models=["mock-reasoning-ns"])])
        status, body, _ = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"])["language"], "zh")
        self.assertIsNotNone(body["choices"][0]["message"].get("reasoning"))

    # ============================================================ 7) 坏 JSON 换候选
    def test_07_bad_json_switches_candidate(self):
        p1 = provider_cfg("p_bad_json", [("mock-badjson", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-json")
        self.assertEqual(meta["attempts"][0]["normalize"], "bad_json")
        self.assertEqual(relay.models["p_bad_json/mock-badjson"].counts.get("bad_json"), 1)
        json.loads(body["choices"][0]["message"]["content"])

    def test_08_json_extract_fallback_repairs_broken_block(self):
        relay = self.make_relay([self.mock_provider(models=["mock-json-messy"])])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"])["language"], "zh")
        self.assertTrue(meta["json_extracted"], "应通过配平提取修好这种坏形状")

    def test_09_cot_leak_model_is_skipped_when_json_required(self):
        p1 = provider_cfg("p_cot", [("mock-cot", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-json", "思维链漏进 content 且 JSON 不可解析 → 换候选")

    # ============================================== 10) json_schema:false 不被选中
    def test_10_json_schema_false_model_not_candidate(self):
        p1 = provider_cfg("p_nojson", [("mock-ok", 1, {"json_schema": False, "tools": True})],
                          ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_json")
        self.assertNotIn("mock-ok", self.mock.stats()["per_model"], "json_schema:false 的模型根本不该被调用")

    # ==================================================== 11) degradable 降级路径
    def test_11_degradable_sends_no_response_format_and_schema_is_still_enforced(self):
        """P0.7/R1 语义更新：降级照旧剥掉 response_format、把 schema 内嵌提示词，**但也照样按 schema
        校验**。mock-inspect 回的是 `{"received": ...}`（不符 schema）→ 新语义下必须判 schema_mismatch，
        单候选最终 502；绝不再当 200 交出去（旧断言把它当降级成功，正是 §0「静默抽空事实」的同源漏洞）。
        """
        p1 = provider_cfg("p_deg", [("mock-inspect", 1, {"json_schema": "degradable", "tools": True})],
                          ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        # 原断言 status == 200 → 新断言 status == 502（降级只改怎么要 JSON，不改期望形状）
        self.assertNotEqual(status, 200, f"不符 schema 的降级 content 绝不许当成功交出去：{body}")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "relay_unusable_content")
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_mismatch")
        self.assertIn("missing_required:facts", meta["attempts"][0].get("normalize_detail", ""))
        self.assertTrue(meta["attempts"][0]["degrade"], "这次尝试确实走了降级路径")
        self.assertEqual(meta.get("degraded_rejected"), 1)
        last = self.mock.stats()["requests"][-1]
        self.assertFalse(last["has_response_format"], "降级请求里必须没有 response_format")
        self.assertIn("JSON Schema", last["last_user"])
        self.assertIn("facts", last["last_user"])
        # 原断言 counts["degraded"] == 1 → 新语义：判不过记 degraded_schema_rejected，不记「降级成功」
        self.assertEqual(relay.models["p_deg/mock-inspect"].counts.get("degraded_schema_rejected"), 1)
        self.assertIsNone(relay.models["p_deg/mock-inspect"].counts.get("degraded"),
                          "判不过的降级 content 不该算降级成功")
        counts = relay.status()["providers"][0]["models"][0]["counts"]
        self.assertEqual(counts["degraded_schema_rejected"], 1)

    # ==================================================== 12) tools 能力过滤
    def test_12_tools_request_skips_tools_false_model(self):
        p1 = provider_cfg("p_notools", [("mock-ok", 1, {"json_schema": "native", "tools": False})],
                          ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_tools", [("mock-tools", 2, {"json_schema": "native", "tools": True})],
                          ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("北京现在几点？", tools=TOOLS, tool_choice="auto"))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-tools")
        self.assertNotIn("mock-ok", self.mock.stats()["per_model"])
        self.assertEqual(body["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "get_time")

    # ==================================================== 13) 并发闸（集成/HTTP）
    def test_13_provider_concurrency_cap_enforced(self):
        p1 = provider_cfg("p_conc", ["mock-slow"], ["TEST_KEY_A", "TEST_KEY_B"],
                          self.mock.base_url, max_concurrency=2, rpm=1000)
        relay = self.make_relay([p1])
        port = self.start_http(relay)
        results: list = []
        lock = threading.Lock()

        def worker(i):
            st, _, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(f"第{i}个"))
            with lock:
                results.append(st)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 8, results)
        self.assertLessEqual(self.mock.stats()["max_concurrency"], 2,
                             "假源观测到的最大并发不得超过 provider 的 max_concurrency")
        self.assertEqual(self.mock.stats()["max_concurrency"], 2)

    # ==================================================== 14) 热重载 config.json
    def test_14_hot_reload_config_without_restart(self):
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-ok"})
        status, _, meta = relay.chat(self.ask())
        self.assertEqual(status, 200)
        self.assertEqual(meta["model"], "mock-ok")
        self.write_config([provider_cfg("p1", ["mock-slow"], ["TEST_KEY_A"], self.mock.base_url)],
                          keys={"TEST_KEY_A": "k1-ok"})
        status2, _, meta2 = relay.chat(self.ask())
        self.assertEqual(status2, 200)
        self.assertEqual(meta2["model"], "mock-slow", "改 config.json 后不重启即生效")
        self.assertEqual(self.mock.stats()["requests"][-1]["model"], "mock-slow")
        self.assertTrue(any("热重载" in line for line in relay.log_lines))

    # ==================================================== 15) 热重载 keys.env
    def test_15_hot_reload_keys_env_adds_key(self):
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A", "TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-ok"})
        usable = lambda r: [k for k in r.providers["p1"].keys if not k.disabled]  # noqa: E731
        self.assertEqual(len(usable(relay)), 1, "keys.env 里还没有 TEST_KEY_B → 占位但不可用")
        with self.keys_path.open("a", encoding="utf-8") as fh:
            fh.write('export TEST_KEY_B="k2-ok"\n')
        status, _, _ = relay.chat(self.ask())
        self.assertEqual(status, 200)
        self.assertEqual(len(usable(relay)), 2, "往 keys.env 追加一行 key 后池子立刻多一把（无需重启）")
        relay.chat(self.ask())  # 轮转应该用到新 key
        self.assertEqual(len(self.mock.stats()["per_key_fp"]), 2, "新 key 必须真的被用上")
        st = relay.status()
        self.assertFalse(st["providers"][0]["keys"][1]["disabled"])
        self.assertEqual(len(st["providers"][0]["keys"][1]["fp"]), 8)

    # ==================================================== 16) 密钥卫生
    def test_16_logs_status_and_panel_never_leak_keys(self):
        secret_a = "sk-FAKESECRETVALUE0001"
        secret_b = "nvapi-FAKESECRETVALUE0002"
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A", "TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": secret_a, "TEST_KEY_B": secret_b})
        relay.chat(self.ask())
        blob = "\n".join(relay.log_lines) + json.dumps(relay.status(), ensure_ascii=False) + relay.panel_html()
        self.assertNotIn(secret_a, blob)
        self.assertNotIn(secret_b, blob)
        self.assertNotIn("FAKESECRETVALUE", blob)
        self.assertNotIn("sk-", "\n".join(relay.log_lines), "日志里连 sk- 前缀都不允许出现")
        st = relay.status()
        self.assertEqual(st["providers"][0]["keys"][0]["fp"], llm_relay.sha8(secret_a))

    # ==================================================== 17) 429 响应体保留
    def test_17_all_keys_429_preserves_upstream_body(self):
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-busy"})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 429, body)
        self.assertIn("429001", json.dumps(body, ensure_ascii=False), "429 必须保留上游响应体")
        self.assertEqual(relay.providers["p1"].keys[0].n429, 1)

    # ==================================================== 18) 402 长冷却
    def test_18_402_quota_long_cooldown(self):
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-quota"})
        status, _, _ = relay.chat(self.ask())
        self.assertEqual(status, 402)
        self.assertGreaterEqual(relay.providers["p1"].keys[0].cooldown_left(), 3500, "额度耗尽要长冷却 ≥1h")

    # ==================================================== 19) temperature 适配
    def test_19_temperature_400_adapts_and_writes_back_config(self):
        p1 = provider_cfg("p1", ["mock-temp400"], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", temperature=0.1))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["kind"], "temp_invalid")
        temp_attempts = [r for r in self.mock.stats()["requests"] if r["model"] == "mock-temp400"]
        self.assertTrue(temp_attempts[0]["temperature_present"])
        self.assertFalse(temp_attempts[1]["temperature_present"], "适配后必须剥掉 temperature")
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(cfg["providers"][0]["models"][0]["params"]["strip_temperature"])
        self.assertEqual(len(list(self.tmp.glob("config.json.bak.*"))), 1, "写回 config.json 前必须先备份")
        self.assertIn("已写回 config.json", "\n".join(relay.log_lines))

    # ==================================================== 20) 400 拒绝 response_format
    def test_20_rf400_degrades_in_place_then_falls_back(self):
        p1 = provider_cfg("p1", ["mock-rf400"], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-json")
        self.assertEqual(meta["attempts"][0]["kind"], "rf_unsupported")
        rf_attempts = [r for r in self.mock.stats()["requests"] if r["model"] == "mock-rf400"]
        self.assertFalse(rf_attempts[-1]["has_response_format"], "降级重试不再带 response_format")
        self.assertTrue(rf_attempts[0]["has_response_format"], "第一次尝试仍应带 response_format")

    # ==================================================== 21) 模型 400 永久禁用
    def test_21_model_not_found_disables_model_permanently(self):
        p1 = provider_cfg("p1", ["mock-model400"], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, _, meta = relay.chat(self.ask())
        self.assertEqual(status, 200)
        self.assertTrue(relay.models["p1/mock-model400"].disabled)
        status2, _, meta2 = relay.chat(self.ask())
        self.assertEqual(status2, 200)
        self.assertEqual(self.mock.stats()["per_model"].get("mock-model400", 0), 1,
                         "禁用后第二次请求不该再打到这个模型")
        self.assertEqual(meta2["provider"], "p2")
        self.assertTrue(relay.status()["providers"][0]["models"][0]["disabled"])

    # ==================================================== 22) usage 补零 / 字段改写
    def test_22_usage_zero_filled_and_max_tokens_renamed(self):
        p1 = provider_cfg("p1", ["mock-no-usage"], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, _ = relay.chat(self.ask(max_completion_tokens=64))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        last = self.mock.stats()["requests"][-1]
        self.assertEqual(last["max_tokens_param"], "max_tokens",
                         "wire 要求 max_tokens，需把 max_completion_tokens 改写过去")
        self.assertEqual(last["max_tokens_value"], 64)
        self.assertFalse(last["temperature_present"])

    def test_23_wire_max_completion_tokens_rewrite_reverse(self):
        p1 = provider_cfg("p1", ["mock-inspect"], ["TEST_KEY_A"], self.mock.base_url,
                          wire={"max_tokens_param": "max_completion_tokens", "extra_headers": {}})
        relay = self.make_relay([p1])
        status, _, _ = relay.chat(self.ask(max_tokens=32))
        self.assertEqual(status, 200)
        last = self.mock.stats()["requests"][-1]
        self.assertEqual(last["max_tokens_param"], "max_completion_tokens")
        self.assertEqual(last["max_tokens_value"], 32)

    # ==================================================== 24) extra_body / 未知字段
    def test_24_extra_body_expanded_unknown_fields_passthrough(self):
        p1 = provider_cfg("p1", ["mock-inspect"], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, _ = relay.chat(self.ask(seed=7, extra_body={"top_k": 5, "custom_flag": True}))
        self.assertEqual(status, 200, body)
        last = self.mock.stats()["requests"][-1]
        self.assertIn("top_k", last["top_level_keys"])
        self.assertIn("custom_flag", last["top_level_keys"])
        self.assertIn("seed", last["top_level_keys"])
        self.assertNotIn("extra_body", last["top_level_keys"])
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"])["received"]["model"], "mock-inspect")

    # ==================================================== 25) 本地兜底与窗口
    def test_25_local_fallback_outside_window_is_not_candidate(self):
        now = time.localtime()
        start = (now.tm_hour * 60 + now.tm_min + 5) % 1440
        end = (start + 5) % 1440
        window = f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
        local = {"enabled": True, "base_url": "http://127.0.0.1:1/v1", "model": "local-mlx",
                 "window": window, "health_check_path": "/models", "autostart": False}
        relay = self.make_relay([], local_fallback=local)
        self.assertEqual(relay.build_candidates(False, False), [])
        status, body, _ = relay.chat(self.ask())
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["type"], "relay_no_candidates")

    def test_26_local_fallback_in_window_needs_health_check(self):
        now = time.localtime()
        start = (now.tm_hour * 60 + now.tm_min - 2) % 1440
        end = (now.tm_hour * 60 + now.tm_min + 3) % 1440
        window = f"{start // 60:02d}:{start % 60:02d}-{end // 60:02d}:{end % 60:02d}"
        local = {"enabled": True, "base_url": "http://127.0.0.1:1/v1", "model": "local-mlx",
                 "window": window, "health_check_path": "/models", "autostart": False}
        relay = self.make_relay([], local_fallback=local)
        self.assertEqual(relay.build_candidates(False, False), [], "窗口内但健康检查失败 → 不能进候选")
        local = dict(local, base_url=self.mock.base_url)
        relay2 = self.make_relay([], local_fallback=local)
        cands = relay2.build_candidates(False, False)
        self.assertEqual([c.provider for c in cands], ["__local__"])
        self.assertEqual(cands[0].model_id, "local-mlx")

    # ==================================================== 27) HTTP 层：健康/状态/面板
    def test_27_health_status_endpoints(self):
        relay = self.make_relay([self.mock_provider()])
        port = self.start_http(relay)
        status, body, _ = self.http_json(port, "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")
        self.assertGreaterEqual(body["candidates_healthy"], 1)
        status, st, _ = self.http_json(port, "/status")
        self.assertEqual(status, 200)
        self.assertIn("providers", st)
        self.assertLessEqual(len(st["recent"]), 20)
        status, raw, headers = self.http(port, "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("llm-relay", raw.decode())
        status, models, _ = self.http_json(port, "/v1/models")
        self.assertEqual(status, 200)
        ids = [m["id"] for m in models["data"]]
        self.assertIn("default", ids)
        self.assertIn("p1/mock-ok", ids)

    def test_28_health_degraded_when_no_candidate(self):
        relay = self.make_relay([provider_cfg("p1", ["mock-500"], ["TEST_KEY_A"], self.mock.base_url)])
        port = self.start_http(relay)
        relay.chat(self.ask())
        status, body, _ = self.http_json(port, "/health")
        self.assertEqual(status, 503)
        self.assertEqual(body["candidates_healthy"], 0)

    # ==================================================== 29) local_token 鉴权
    def test_29_local_token_auth(self):
        relay = self.make_relay([self.mock_provider()], token="t0ken-local")
        port = self.start_http(relay)
        status, _, _ = self.http(port, "/health")
        self.assertEqual(status, 200, "/health 保持开放，给 launchd/启动脚本探活")
        status, _, _ = self.http(port, "/status")
        self.assertEqual(status, 401)
        status, _, _ = self.http_json(port, "/status", token="t0ken-local")
        self.assertEqual(status, 200)
        status, _, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask())
        self.assertEqual(status, 401)
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(), token="t0ken-local")
        self.assertEqual(status, 200, body)

    # ==================================================== 30) SSE 尽力透传
    def test_30_stream_passthrough(self):
        relay = self.make_relay([self.mock_provider()])
        port = self.start_http(relay)
        status, raw, headers = self.http(port, "/v1/chat/completions", payload=self.ask(stream=True))
        self.assertEqual(status, 200)
        self.assertIn("text/event-stream", headers.get("Content-Type", ""))
        self.assertIn("[DONE]", raw.decode("utf-8", "replace"))

    # ==================================================== 31) 排序/能力优先级
    def test_31_chain_order_and_native_before_degradable(self):
        fast = provider_cfg("p_fast", [("mock-ok", 1)], ["TEST_KEY_A"], self.mock.base_url)
        slow = provider_cfg("p_slow", [("mock-inspect", 5)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([slow, fast])
        self.assertEqual([(c.provider, c.chain) for c in relay.build_candidates(False, False)],
                         [("p_fast", 1), ("p_slow", 5)], "候选必须按 chain 升序排")
        p1 = provider_cfg("p_deg", [("mock-json", 3, {"json_schema": "degradable", "tools": True})],
                          ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_native", [("mock-json", 3, {"json_schema": "native", "tools": True})],
                          ["TEST_KEY_B"], self.mock.base_url)
        relay2 = self.make_relay([p1, p2])
        self.assertEqual([c.provider for c in relay2.build_candidates(True, False)], ["p_native", "p_deg"])

    # ==================================================== 32) max_candidates 上限
    def test_32_max_candidates_cap(self):
        provs = [provider_cfg(f"p{i}", [("mock-500", i + 1)], [f"TEST_KEY_{i}"], self.mock.base_url)
                 for i in range(4)]
        relay = self.make_relay(provs, keys={f"TEST_KEY_{i}": f"k{i}-ok" for i in range(4)},
                                request={"max_candidates": 3})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 500)
        self.assertLessEqual(len(meta["attempts"]), 3, "单次请求最多试 max_candidates 个候选")

    # ================================== 33) key 全冷却 → 回 429 + reset（不是 502）
    def test_33_all_keys_cooling_returns_429_with_reset(self):
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url, key_cooldown_s=120)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-busy"})
        status1, _, _ = relay.chat(self.ask())
        self.assertEqual(status1, 429)
        status2, body2, meta2 = relay.chat(self.ask())
        self.assertEqual(status2, 429, "key 全冷却时也必须回 429（保留 reset 语义），不能回 502")
        self.assertIn("relay_key_cooldown", json.dumps(body2, ensure_ascii=False))
        self.assertEqual(len(meta2["attempts"]), 0, "冷却中不会再打上游（假源调用数不增长）")
        self.assertEqual(self.mock.stats()["calls"], 1)

    # ============================== 34) 慢失败（超时）之后必须还能 failover 到备胎
    def test_34_slow_failure_failover_within_budget(self):
        """D1/D2 回归：候选 1、2 挂起 90s，候选 3 正常 → 必须走到候选 3 并 200。

        修复前：per_attempt_timeout_s=60 × 3 候选 = 180s 远超 total_budget_s=100，
        第一次超时就把预算吃掉一大半，备胎根本没机会试 → 502（断网事故的形状）。
        """
        self.mock.state.hang_s = 90.0  # 假源真的挂 90s，靠中转站自己的超时切断
        p1 = provider_cfg("p1", [("mock-hang", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-hang", 2)], ["TEST_KEY_B"], self.mock.base_url)
        p3 = provider_cfg("p3", [("mock-ok", 3)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p2, p3], keys={f"TEST_KEY_{k}": f"k{k}-ok" for k in "ABC"},
                                request={"per_attempt_timeout_s": 3, "total_budget_s": 20,
                                         "max_candidates": 3})
        t0 = time.time()
        status, body, meta = relay.chat(self.ask())
        elapsed = time.time() - t0
        self.assertEqual(status, 200, body)
        self.assertEqual((meta["provider"], meta["model"]), ("p3", "mock-ok"),
                         "超时类失败之后必须走到下一个候选")
        self.assertGreaterEqual(len(meta["attempts"]), 2, f"至少两次尝试，实测 {meta['attempts']}")
        self.assertEqual([a["kind"] for a in meta["attempts"]],
                         ["transport", "transport", "http_ok"])
        self.assertLess(elapsed, 20.0, f"总耗时 {elapsed:.1f}s 必须小于 total_budget_s=20")

    # ======================== 35) 候选不因「先截断再过滤」而失效（断网事故根因）
    def test_35_candidates_not_lost_to_truncation(self):
        """D1 回归：chain 1/2/3 分别是「未启用 / 没 key / 冷却中」，chain 6 必须仍在候选列表里。"""
        p1 = provider_cfg("p1", [("mock-ok", 1)], ["TEST_KEY_A"], self.mock.base_url, enabled=False)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["MISSING_KEY_X"], self.mock.base_url)
        p3 = provider_cfg("p3", [("mock-ok", 3)], ["TEST_KEY_B"], self.mock.base_url)
        p6 = provider_cfg("p6", [("mock-ok", 6)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p2, p3, p6],
                                keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_B": "kb-ok", "TEST_KEY_C": "kc-ok"},
                                request={"max_candidates": 3})
        relay.providers["p3"].enter_provider_cooldown("测试注入：模拟 5xx 后的冷却", 60)
        chains = [c.chain for c in relay.build_candidates(False, False)]
        self.assertIn(6, chains, "chain 6 不能被 chain 1/2/3 的不可用候选挤掉（D1）")
        for gone in (1, 2, 3):
            self.assertNotIn(gone, chains, f"chain {gone} 不可用，不该出现在候选列表")
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertNotEqual(status, 502)
        self.assertEqual((meta["provider"], meta["model"]), ("p6", "mock-ok"))
        self.assertEqual(self.mock.stats()["calls"], 1, "只有 chain 6 那个候选真的打了上游")

    # ==================================== 36) 过滤后候选不足 max_candidates → 照常用剩下的
    def test_36_fewer_candidates_than_max_still_serves(self):
        p1 = provider_cfg("p1", [("mock-ok", 1)], ["TEST_KEY_A"], self.mock.base_url, enabled=False)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["MISSING_KEY_X"], self.mock.base_url)
        p5 = provider_cfg("p5", [("mock-ok", 5)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p2, p5], keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_C": "kc-ok"},
                                request={"max_candidates": 3})
        cands = relay.build_candidates(False, False)
        self.assertEqual(len(cands), 1, "过滤后不足 max_candidates 是正常情况，不是错误")
        self.assertEqual(cands[0].provider, "p5")
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, "候选不足也不能 502")
        self.assertEqual(meta["provider"], "p5")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["http_ok"])

    # ==================== 37) 全部候选只是「冷却中/禁用」→ 429 + reset，不是 502
    def test_37_all_cooling_or_disabled_returns_429_not_502(self):
        p1 = provider_cfg("p1", [("mock-ok", 1)], ["TEST_KEY_A"], self.mock.base_url, enabled=False)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url, key_cooldown_s=120)
        relay = self.make_relay([p1, p2], keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_B": "kb-ok"})
        relay.providers["p2"].enter_provider_cooldown("测试注入：模拟超时后的冷却", 60)
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 429, f"全冷却/禁用必须是 429，不能是 502：{body}")
        self.assertIn("relay_key_cooldown", json.dumps(body, ensure_ascii=False))
        self.assertGreater(body["error"].get("retry_after_s", 0), 0, "429 必须带 reset 信息")
        self.assertEqual(len(meta["attempts"]), 0, "没有可用候选就不该空打上游")
        self.assertEqual(self.mock.stats()["calls"], 0)
        port = self.start_http(relay)
        status2, body2, headers = self.http_json(port, "/v1/chat/completions", payload=self.ask())
        self.assertNotEqual(status2, 502)
        self.assertEqual(status2, 429)
        self.assertEqual(headers.get("Retry-After"),
                         str(int(round(body2["error"]["retry_after_s"]))),
                         "HTTP 层必须把 reset 信息放到 Retry-After 头")

    # ======================= 38) 预算装得下 2 次尝试：第一次超时 25s + 第二次正常
    def test_38_budget_holds_two_attempts(self):
        """D2 回归：per_attempt_timeout_s=25 + total_budget_s=100（生产配置）下，
        第一次吃满 25s 超时之后必须还有预算试第二个候选。
        """
        self.mock.state.hang_s = 90.0
        p1 = provider_cfg("p1", [("mock-hang", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2], keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_B": "kb-ok"},
                                request={"per_attempt_timeout_s": 25, "total_budget_s": 100,
                                         "min_attempts_within_budget": 2})
        t0 = time.time()
        status, body, meta = relay.chat(self.ask())
        elapsed = time.time() - t0
        self.assertEqual(status, 200, body)
        self.assertEqual(len(meta["attempts"]), 2, f"两次尝试都要被记录：{meta['attempts']}")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["transport", "http_ok"])
        self.assertGreaterEqual(meta["attempts"][0]["latency_s"], 24.0, "第一次应吃满 25s 超时")
        self.assertGreaterEqual(elapsed, 24.0, f"总耗时应≈25–30s，实测 {elapsed:.1f}s")
        self.assertLessEqual(elapsed, 32.0, f"总耗时应≈25–30s，实测 {elapsed:.1f}s")

    # ========================= 39) 候选跳过原因进日志，且日志里绝不出现 key 明文
    def test_39_candidate_skip_reasons_logged_without_keys(self):
        secret = "sk-FAKESECRETFORLOG0009"
        p1 = provider_cfg("p1", [("mock-ok", 1)], ["TEST_KEY_A"], self.mock.base_url, enabled=False)
        p3 = provider_cfg("p3", [("mock-ok", 3)], ["TEST_KEY_B"], self.mock.base_url)
        p6 = provider_cfg("p6", [("mock-ok", 6)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p3, p6],
                                keys={"TEST_KEY_A": secret, "TEST_KEY_B": "kb-ok", "TEST_KEY_C": "kc-ok"})
        relay.providers["p3"].enter_provider_cooldown("测试注入：模拟冷却", 60)
        status, _, _ = relay.chat(self.ask())
        self.assertEqual(status, 200)
        log = "\n".join(relay.log_lines)
        self.assertIn("候选列表:", log, "日志要打印完整候选列表")
        self.assertIn("p6/mock-ok(chain6)", log)
        self.assertIn("跳过 p1/mock-ok(chain1): provider 未启用", log)
        self.assertIn("p3/mock-ok(chain3): provider 冷却中", log)
        self.assertEqual(len(re.findall(r"sk-|nvapi-", log)), 0, "日志里 sk-/nvapi- 计数必须为 0")
        self.assertNotIn(secret, log)

    # ==================================================================================
    # P0：JSON 校验必须对照 schema（R1–R5 的回归用例，全打 mock，不烧真额度）
    # ==================================================================================
    ENUM_SCHEMA = {
        "type": "object",
        "properties": {"facts": {"type": "array", "items": {"type": "string"}},
                       "language": {"type": "string", "enum": ["en", "ja"]}},
        "required": ["facts", "language"],
        "additionalProperties": False,
    }
    ENUM_REQ = {"type": "json_schema", "json_schema": {"name": "facts", "strict": True,
                                                       "schema": ENUM_SCHEMA}}

    # ---------------------------------------------------------- 40) schema 回显必须丢
    def test_40_schema_echo_candidate_rejected_then_next_candidate(self):
        """R2/R4：content 里回显 JSON Schema 本体（finish_reason=length）→ 判失败换候选。"""
        p1 = provider_cfg("p_echo", [("mock-schema-echo", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("从这句话抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-json", "schema 回显的候选必须被丢弃，换下一个候选")
        self.assertGreaterEqual(len(meta["attempts"]), 2, meta["attempts"])
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_echo")
        self.assertGreaterEqual(relay.status()["counters"].get("schema_echo", 0), 1,
                                "schema 回显 + finish_reason=length 要计入独立计数器 schema_echo")
        content = body["choices"][0]["message"]["content"]
        self.assertNotIn('"properties"', content, "绝不能把 schema 本体交给 Hindsight")
        self.assertIn("facts", json.loads(content), "交给 Hindsight 的必须是 facts 结果")

    # ---------------------------------------------------- 41) 合规 JSON 不能误杀
    def test_41_schema_conformant_json_passes(self):
        """R1：符合 schema 的 JSON 必须照常通过（回归，不误伤）。"""
        relay = self.make_relay([self.mock_provider(models=["mock-json"])])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "ok")
        parsed = json.loads(body["choices"][0]["message"]["content"])
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"])
        self.assertEqual(parsed["language"], "zh")
        self.assertEqual(relay.status()["counters"].get("schema_echo", 0), 0)

    # -------------------------------------------------------- 42) 缺 required 判失败
    def test_42_missing_required_field_rejected(self):
        """R1/R4：缺 required 字段（facts）→ 判失败并换候选，不返回该 content。"""
        p1 = provider_cfg("p_missing", [("mock-schema-missing", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_mismatch")
        self.assertIn("missing_required:facts", meta["attempts"][0].get("normalize_detail", ""))
        self.assertEqual(meta["model"], "mock-json", "判不过就必须换下一个候选")
        parsed = json.loads(body["choices"][0]["message"]["content"])
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"], "不能把缺字段的内容交出去")

    # ------------------------------------------------------ 43) 顶层类型不符判失败
    def test_43_toplevel_type_mismatch_rejected(self):
        """R1：schema type=object 而上游给 [...] → 判失败。"""
        p1 = provider_cfg("p_arr", [("mock-schema-array", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_mismatch")
        self.assertIn("type_mismatch", meta["attempts"][0].get("normalize_detail", ""))
        self.assertEqual(meta["model"], "mock-json")
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"])["language"], "zh")

    # ------------------------------------------- 44) additionalProperties:false 生效
    def test_44_additional_properties_false_rejects_extra_field(self):
        """R1：additionalProperties:false 时多出未声明字段 → 判失败。"""
        p1 = provider_cfg("p_extra", [("mock-schema-extra", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_mismatch")
        self.assertIn("unexpected_property:extra", meta["attempts"][0].get("normalize_detail", ""))
        self.assertNotIn("extra", json.loads(body["choices"][0]["message"]["content"]))

    # ------------------------------------------------------------- 45) enum 不匹配
    def test_45_enum_mismatch_rejected(self):
        """R1：值不在 enum 内 → 判失败并换候选。"""
        p1 = provider_cfg("p_zh", [("mock-json", 1)], ["TEST_KEY_A"], self.mock.base_url)  # language=zh
        p2 = provider_cfg("p_en", [("mock-schema-en", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=self.ENUM_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_mismatch")
        self.assertIn("enum_mismatch", meta["attempts"][0].get("normalize_detail", ""))
        self.assertEqual(meta["model"], "mock-schema-en")
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"])["language"], "en")
        ok, why = llm_relay.validate_against_schema({"facts": ["x"], "language": "zh"}, self.ENUM_SCHEMA)
        self.assertFalse(ok)
        self.assertIn("enum_mismatch", why)

    # ----------------------------------------- 46) 配平块挑第一个符合 schema 的
    def test_46_extract_fallback_picks_first_schema_valid_block(self):
        """R3：先出现的合法 JSON 不符合 schema，必须继续往后挑到合格块。"""
        relay = self.make_relay([self.mock_provider(models=["mock-schema-blocks"])])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "extracted")
        self.assertTrue(meta["json_extracted"])
        parsed = json.loads(body["choices"][0]["message"]["content"])
        self.assertEqual(parsed["language"], "zh")
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"])

    # ------------------------------------------------ 47) validate_json=false 不变
    def test_47_validate_json_false_disables_schema_check(self):
        """R1 回归：validate_json=false 时不启用 schema 校验，行为与旧版一致。"""
        relay = self.make_relay([self.mock_provider(models=["mock-schema-echo"])],
                                request={"validate_json": False})
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["normalize"], "ok")
        self.assertEqual(json.loads(body["choices"][0]["message"]["content"]), SCHEMA,
                         "旧版行为：不做 schema 校验时内容原样透传")
        self.assertEqual(relay.status()["counters"].get("schema_echo", 0), 0)

    # --------------------------------------------------------------- 48) 密钥卫生
    def test_48_no_key_plaintext_in_logs_status_panel(self):
        """走一遍「schema 回显 + 换候选」的新日志路径，确认没有任何 key 明文。"""
        secret_a = "sk-FAKESECRETP0CHECK001"
        secret_b = "nvapi-FAKESECRETP0CHECK002"
        p1 = provider_cfg("p_echo", [("mock-schema-echo", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2], keys={"TEST_KEY_A": secret_a, "TEST_KEY_B": secret_b})
        relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        blob = ("\n".join(relay.log_lines) + json.dumps(relay.status(), ensure_ascii=False)
                + relay.panel_html())
        self.assertEqual(len(re.findall(r"sk-|nvapi-", blob)), 0,
                         "日志/状态/面板里 sk-/nvapi- 匹配数必须为 0")
        self.assertNotIn("FAKESECRET", blob)

    # ------------------------------------------- 49) R5：小 max_tokens 被抬起（只抬不降）
    def test_49_schema_request_raises_small_max_tokens(self):
        """R5：带 json_schema 的请求 max_tokens < schema_min_max_tokens → 抬到下限并记 meta。"""
        relay = self.make_relay([self.mock_provider(models=["mock-json"])],
                                request={"schema_min_max_tokens": 1024})
        status, _, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ, max_tokens=64))
        self.assertEqual(status, 200)
        self.assertEqual(self.mock.stats()["requests"][-1]["max_tokens_value"], 1024)
        self.assertTrue(meta["max_tokens_raised"], "meta 里必须记录 max_tokens_raised")
        relay.chat(self.ask("普通请求", max_tokens=64))
        self.assertEqual(self.mock.stats()["requests"][-1]["max_tokens_value"], 64,
                         "不带 json_schema 的请求不受影响")
        relay.chat(self.ask("抽取事实", response_format=JSON_REQ, max_tokens=4096))
        self.assertEqual(self.mock.stats()["requests"][-1]["max_tokens_value"], 4096, "只抬不降")
        relay2 = self.make_relay([self.mock_provider(models=["mock-json"])],
                                 request={"schema_min_max_tokens": 512})
        relay2.chat(self.ask("抽取事实", response_format=JSON_REQ, max_tokens=64))
        self.assertEqual(self.mock.stats()["requests"][-1]["max_tokens_value"], 512, "下限可配置")


    # ==================================================================================
    # P0.5（R1–R4 的回归用例，全打 mock，不烧真额度）
    # ==================================================================================
    # ---------------------------------------------------------------- 50) 单次超时封顶
    def test_50_single_transport_timeout_cooldown_is_capped(self):
        """R2 回归：一次 transport 超时 → provider 冷却 ≤ provider_cooldown_cap_s，绝不是 900s。"""
        self.mock.state.hang_s = 90.0
        bad = provider_cfg("p_hang", [("mock-hang", 1)], ["TEST_KEY_A"], self.mock.base_url,
                           provider_cooldown_s=900,
                           backoff={"initial_s": 900, "max_s": 900, "factor": 2})
        good = provider_cfg("p_good", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([bad, good], keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_B": "kb-ok"},
                                request={"per_attempt_timeout_s": 1, "total_budget_s": 30,
                                         "provider_cooldown_cap_s": 120,
                                         "provider_cooldown_hard_cap_s": 300})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["attempts"][0]["kind"], "transport")
        rt = relay.providers["p_hang"]
        left = rt.cooldown_left()
        self.assertGreater(left, 0.0, "transport 失败后 provider 必须进冷却")
        self.assertLessEqual(left, 120.5, "单次 transport 超时的冷却不得超过 provider_cooldown_cap_s=120")
        self.assertLess(left, 300.0, "单次失败绝不许直接跳到 hard cap=300（更不许是 900）")
        self.assertEqual(rt.backoff_level, 1, "backoff_level 仍要递增，保留可观测性")
        st = relay.status()
        self.assertLessEqual(st["providers"][0]["cooldown_left_s"], 120.5)
        self.assertNotIn("900s", str(st["providers"][0]["cooldown_reason"]))

    # ------------------------------------------------- 51) 连败 ≥3 才允许升到 hard cap
    def test_51_provider_cooldown_escalates_only_after_three_failures(self):
        """R2 回归：短窗口内连续 ≥3 次失败才抬到硬封顶；前两次都不得超软封顶。"""
        bad = provider_cfg("p_bad", ["mock-500"], ["TEST_KEY_A"], self.mock.base_url,
                           provider_cooldown_s=900,
                           backoff={"initial_s": 600, "max_s": 900, "factor": 2})
        relay = self.make_relay([bad], keys={"TEST_KEY_A": "ka-ok"},
                                request={"provider_cooldown_cap_s": 60,
                                         "provider_cooldown_hard_cap_s": 300,
                                         "provider_failure_window_s": 300,
                                         "provider_escalate_after_failures": 3})
        rt = relay.providers["p_bad"]
        durs: list[float] = []
        for i in range(3):
            rt.cooldown_until = 0.0  # 模拟冷却到期后再失败一次（不改 backoff_level / 连败窗口）
            status, _, _ = relay.chat(self.ask())
            self.assertEqual(status, 500, f"第 {i + 1} 次应打到上游并拿到 500")
            durs.append(rt.cooldown_left())
        self.assertLessEqual(durs[0], 60.5, f"第 1 次失败不得超过软封顶：{durs}")
        self.assertLessEqual(durs[1], 60.5, f"第 2 次失败也不得超过软封顶：{durs}")
        self.assertLessEqual(durs[2], 300.5, f"第 3 次连败可以到硬封顶：{durs}")
        self.assertGreater(durs[2], 60.0, f"第 3 次连败必须才抬到 hard cap：{durs}")
        self.assertEqual(rt.backoff_level, 3, "backoff_level 每次失败都递增（可观测性保留）")
        self.assertTrue(all(d <= 300.5 for d in durs), f"任何一次都不得超过 hard cap：{durs}")

    # ------------------------------------------------------- 52) 一把 key 429 错峰
    def test_52_one_key_429_does_not_punish_sibling_key(self):
        """R3 回归：key#0 撞 429 只罚它自己，请求继续用 key#1 成功，不许整池清空。"""
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A", "TEST_KEY_B"], self.mock.base_url,
                          key_cooldown_s=120, provider_cooldown_s=300)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-busy", "TEST_KEY_B": "k2-ok"})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["key_index"], 1, "key#0 撞 429 后必须继续用同 provider 的 key#1")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit", "http_ok"])
        rt = relay.providers["p1"]
        self.assertEqual(rt.cooldown_left(), 0.0, "一把 key 撞限绝不许把 provider 整池冷却")
        # P0.6/R1：429 冷却粒度从整把 key 改成 (key, 模型)；整把 key 的冷却只留给账号级 429。
        self.assertGreater(rt.keys[0].model_cooldown_left("mock-ok"), 0.0,
                           "撞限的 (key#0, mock-ok) 必须冷却")
        self.assertEqual(rt.keys[0].cooldown_left(), 0.0, "模型级 429 不许再冷整把 key")
        self.assertEqual(rt.keys[1].cooldown_left(), 0.0, "同 provider 的另一把 key 不许被连坐")
        self.assertEqual(rt.keys[1].model_cooldown_left("mock-ok"), 0.0,
                         "另一把 key 上同一模型也不许被连坐")
        st = relay.status()
        self.assertEqual(st["providers"][0]["cooldown_left_s"], 0.0)
        st_keys = st["providers"][0]["keys"]
        self.assertEqual(st_keys[0]["cooldown_left_s"], 0.0, "状态里整把 key 也不该显示成冷却")
        self.assertEqual(st_keys[0]["model_cooldowns"][0]["model"], "mock-ok")
        self.assertIn("模型额度", st_keys[0]["model_cooldowns"][0]["reason"])
        self.assertEqual(st_keys[1]["model_cooldowns"], [])
        self.assertEqual(self.mock.stats()["per_key_fp"].get(llm_relay.sha8("k2-ok")), 1)

    # --------------------------------------------- 53) 提前超时后仍装得下两次尝试
    def test_53_two_attempts_still_fit_in_budget_after_early_timeout(self):
        """R1 回归：45×2=90 ≤ total_budget_s=100；提前超时后备胎仍被记录到并成功。"""
        relay = self.make_relay([self.mock_provider()],
                                request={"per_attempt_timeout_s": 45, "total_budget_s": 100,
                                         "min_attempts_within_budget": 2})
        cfg = relay.cfg["request"]
        self.assertEqual(round(relay._attempt_timeout(cfg, time.time() + 100, 2), 1), 45.0,
                         "生产预算下单次尝试应给满 45s")
        self.assertLessEqual(45 * 2, 100, "两次 45s 尝试必须装进 total_budget_s=100")
        self.assertLessEqual(relay._attempt_timeout(cfg, time.time() + 100, 3) * 2, 100,
                             "剩余候选 3 个时也要保证至少两次尝试装得进预算")
        # R1.3：思考模型不被 clamp 压到 45 以下（拿 per_attempt_timeout_s=25 的旧配置对照）
        small_cap = {"per_attempt_timeout_s": 25, "reasoning_min_timeout_s": 45,
                     "total_budget_s": 200, "min_attempts_within_budget": 2}
        self.assertEqual(round(relay._attempt_timeout(small_cap, time.time() + 200, 2, reasoning=False), 1),
                         25.0, "普通模型仍按 per_attempt_timeout_s 封顶")
        self.assertEqual(round(relay._attempt_timeout(small_cap, time.time() + 200, 2, reasoning=True), 1),
                         45.0, "思考模型的单次超时硬下限是 45s")
        # 集成：mock-hang 提前超时 → 备胎成功，两次尝试都记录，总耗时 < total_budget_s
        self.mock.state.hang_s = 90.0
        p1 = provider_cfg("p1", [("mock-hang", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p2", [("mock-ok", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay2 = self.make_relay([p1, p2], keys={"TEST_KEY_A": "ka-ok", "TEST_KEY_B": "kb-ok"},
                                 request={"per_attempt_timeout_s": 3, "total_budget_s": 12,
                                          "min_attempts_within_budget": 2})
        t0 = time.time()
        status, body, meta = relay2.chat(self.ask())
        elapsed = time.time() - t0
        self.assertEqual(status, 200, body)
        self.assertEqual(len(meta["attempts"]), 2, f"两次尝试都要被记录：{meta['attempts']}")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["transport", "http_ok"])
        self.assertLess(elapsed, 12.0, f"总耗时 {elapsed:.1f}s 必须小于 total_budget_s=12")
        log = "\n".join(relay2.log_lines)
        self.assertIn("单次超时预算", log, "日志必须能看出这次给了模型多长超时")

    # -------------------------------------------- 54) 单候选 + schema 回显绝不 200
    def test_54_single_candidate_schema_echo_never_returns_200(self):
        """R5 回归（修遗留项 5.3）：单候选 + schema 回显 → 502 relay_unusable_content。"""
        p1 = provider_cfg("p_echo", [("mock-schema-echo", 1)], ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, meta = relay.chat(self.ask("从这句话抽取事实", response_format=JSON_REQ))
        self.assertNotEqual(status, 200, f"schema 回显绝不许当成功交出去：{body}")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "relay_unusable_content")
        self.assertEqual(body["error"]["code"], "relay_unusable_content")
        self.assertNotIn('"properties"', json.dumps(body, ensure_ascii=False),
                         "响应体里绝不许出现 schema 本体")
        self.assertEqual(meta["attempts"][0]["normalize"], "schema_echo")
        self.assertEqual(self.mock.stats()["calls"], 1)
        self.assertGreaterEqual(relay.status()["counters"].get("unusable_content_rejected", 0), 1)

    # ------------------------------------------------ 55) 全部候选内容都不合格
    def test_55_all_candidates_unusable_returns_error_not_last_200(self):
        """R5 回归：所有候选的 content 都不合格 → 502，而不是最后一条上游 200 体。"""
        p1 = provider_cfg("p_echo", [("mock-schema-echo", 1)], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_arr", [("mock-schema-array", 2)], ["TEST_KEY_B"], self.mock.base_url)
        p3 = provider_cfg("p_bad", [("mock-badjson", 3)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p2, p3],
                                keys={f"TEST_KEY_{k}": f"k{k}-ok" for k in "ABC"},
                                request={"max_candidates": 3})
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertNotEqual(status, 200, f"不合格内容绝不许当成功交出去：{body}")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "relay_unusable_content")
        self.assertEqual(len(meta["attempts"]), 3)
        self.assertEqual({a["normalize"] for a in meta["attempts"]},
                         {"schema_echo", "schema_mismatch", "bad_json"})
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn('"properties"', blob)
        self.assertNotIn("我从 Mac 上跑 Hindsight", blob, "不合格的上游 200 体不许透传")
        self.assertEqual(self.mock.stats()["calls"], 3)

    # ------------------------------------------------------- 56) 本地 rpm 限流生效
    def test_56_local_rpm_limit_throttles_before_upstream(self):
        """R4 回归：本地 rpm 到限就排队/退避并回 429，绝不把请求放过去让上游打 429。"""
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url, rpm=1)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "ka-ok"},
                                request={"queue_timeout_s": 0.3, "total_budget_s": 5,
                                         "per_attempt_timeout_s": 1})
        status1, body1, _ = relay.chat(self.ask())
        self.assertEqual(status1, 200, body1)
        self.assertEqual(self.mock.stats()["calls"], 1)
        status2, body2, meta2 = relay.chat(self.ask())
        self.assertNotEqual(status2, 200, f"本地 rpm 打满不许继续打上游：{body2}")
        self.assertEqual(status2, 429, body2)
        self.assertEqual(self.mock.stats()["calls"], 1, "被本地 rpm 拦下的请求绝不能打到上游")
        self.assertEqual(body2["error"]["code"], "relay_local_rpm_limit")
        self.assertGreater(body2["error"]["retry_after_s"], 0, "429 必须带 reset 信息")
        self.assertEqual(len(meta2["attempts"]), 0, "没打上游就不该有 attempt 记录")
        self.assertIn("本地 rpm 限流生效", "\n".join(relay.log_lines))
        self.assertGreaterEqual(relay.status()["counters"].get("rpm_throttle", 0), 1)
        self.assertGreaterEqual(relay.status()["providers"][0]["rpm_blocked"], 1)

    # ------------------------------------------------ 57) 冷却封顶可配置（热重载）
    def test_57_provider_cooldown_caps_are_configurable(self):
        """R2 回归：provider_cooldown_cap_s / hard_cap_s 设成别的值也照样生效。"""
        bad = provider_cfg("p_bad", ["mock-500"], ["TEST_KEY_A"], self.mock.base_url,
                           provider_cooldown_s=900,
                           backoff={"initial_s": 900, "max_s": 900, "factor": 2})
        relay = self.make_relay([bad], keys={"TEST_KEY_A": "ka-ok"},
                                request={"provider_cooldown_cap_s": 7,
                                         "provider_cooldown_hard_cap_s": 11,
                                         "provider_failure_window_s": 300,
                                         "provider_escalate_after_failures": 3})
        rt = relay.providers["p_bad"]
        durs: list[float] = []
        for _ in range(3):
            rt.cooldown_until = 0.0
            status, _, _ = relay.chat(self.ask())
            self.assertEqual(status, 500)
            durs.append(rt.cooldown_left())
        self.assertLessEqual(durs[0], 7.5, f"软封顶改成 7s 必须生效：{durs}")
        self.assertLessEqual(durs[1], 7.5, f"软封顶改成 7s 必须生效：{durs}")
        self.assertLessEqual(durs[2], 11.5, f"硬封顶改成 11s 必须生效：{durs}")
        self.assertGreater(durs[2], 7.0, "只有第 3 次连败才允许越过软封顶")
        # 不显式配置时用默认 120 / 300
        relay2 = self.make_relay([provider_cfg("p_bad2", ["mock-500"], ["TEST_KEY_A"], self.mock.base_url,
                                               provider_cooldown_s=900,
                                               backoff={"initial_s": 900, "max_s": 900, "factor": 2})],
                                 keys={"TEST_KEY_A": "ka-ok"})
        defaults = relay2.status()["request"]
        self.assertEqual(defaults["provider_cooldown_cap_s"], 120.0)
        self.assertEqual(defaults["provider_cooldown_hard_cap_s"], 300.0)
        self.assertEqual(defaults["provider_escalate_after_failures"], 3)
        status, _, _ = relay2.chat(self.ask())
        self.assertEqual(status, 500)
        self.assertLessEqual(relay2.providers["p_bad2"].cooldown_left(), 120.5,
                             "默认软封顶 120s 必须生效")

    # =====================================================================
    # P0.6（R1–R4）：429 冷却粒度 = (key, 模型)。以下 T58–T64 全打 mock，不烧真额度。
    # =====================================================================

    def _sibling_key_scaffold(self, *, key_value="ka-ok", key_cooldown_s=60, keys=("TEST_KEY_A",)):
        """同 provider + 同一把 key 上挂「模型 A 会 429 / 模型 B 健康」两个模型。"""
        p1 = provider_cfg("p1", [("mock-429", 1), ("mock-ok", 2)], list(keys), self.mock.base_url,
                          key_cooldown_s=key_cooldown_s)
        relay = self.make_relay([p1], keys={name: key_value for name in keys})
        return relay, p1

    # ------------------------------------------- T58) 同 key 两模型：A 429，B 照常成功
    def test_58_429_on_one_model_does_not_cool_sibling_model_same_key(self):
        """T58（R1）：模型 A 429 只冷 (key,A)；同 key 的模型 B 仍被选中并 200。"""
        relay, _ = self._sibling_key_scaffold()
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual((meta["provider"], meta["model"]), ("p1", "mock-ok"),
                         "A 429 之后必须由同 key 上的 B 接手")
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit", "http_ok"])
        ks = relay.providers["p1"].keys[0]
        self.assertGreater(ks.model_cooldown_left("mock-429"), 0.0, "A 必须被冷")
        self.assertEqual(ks.model_cooldown_left("mock-ok"), 0.0, "B 绝不能被连坐")
        self.assertEqual(ks.cooldown_left(), 0.0, "模型级 429 不许冷整把 key")
        self.assertIn("模型额度", ks.model_cooldown_reason("mock-429"))
        # 两次尝试确实落在同一把 key 上（key#0 与 key#1 不存在之分）
        stats = self.mock.stats()
        self.assertEqual(stats["per_model"].get("mock-429"), 1)
        self.assertEqual(stats["per_model"].get("mock-ok"), 1)
        self.assertEqual(stats["per_key_fp"], {llm_relay.sha8("ka-ok"): 2},
                         "两次尝试必须用同一把 key，证明是「同 key 两个模型」的场景")

    # ---------------------------------------- T59) 第二次请求：B 不需要等冷却，直接再用
    def test_59_sibling_model_reusable_on_second_request_without_waiting(self):
        """T59（R3）：A 还在冷却时，B 的 next_key_slot 仍拿得到这把 key，第二次直接 200。"""
        relay, _ = self._sibling_key_scaffold(key_cooldown_s=120)
        status1, body1, _ = relay.chat(self.ask())
        self.assertEqual(status1, 200, body1)
        rt = relay.providers["p1"]
        ks = rt.keys[0]
        self.assertGreater(ks.model_cooldown_left("mock-429"), 0.0)
        self.assertIs(rt.next_key_slot("mock-ok"), ks, "B 必须仍能拿到这把 key（不必等冷却）")
        self.assertIsNone(rt.next_key_slot("mock-429"), "A 在这把 key 上仍应被跳过")
        self.assertTrue(rt.has_usable_key("mock-ok"))
        self.assertFalse(rt.has_usable_key("mock-429"))
        status2, body2, meta2 = relay.chat(self.ask())
        self.assertEqual(status2, 200, body2)
        self.assertEqual((meta2["provider"], meta2["model"]), ("p1", "mock-ok"))
        self.assertEqual([a["kind"] for a in meta2["attempts"]], ["http_ok"],
                         "第二次请求不该再空打还在冷却的 A，直接命中 B")
        self.assertEqual(self.mock.stats()["per_model"].get("mock-429"), 1,
                         "A 仍在冷却，第二次请求不应再打 A")

    # ------------------------------------------------ T60) 账号级 429 冷整把 key
    def test_60_account_level_429_cools_whole_key(self):
        """T60（R2）：响应体明确指向账户/套餐 → 整把 key 冷却，所有模型都拿不到。"""
        p1 = provider_cfg("p1", [("mock-429-account", 1), ("mock-ok", 2)], ["TEST_KEY_A"],
                          self.mock.base_url)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "ka-ok"})
        status, body, meta = relay.chat(self.ask())
        ks = relay.providers["p1"].keys[0]
        self.assertGreater(ks.cooldown_left(), 0.0, "账号级 429 必须冷整把 key")
        self.assertIn("账号级", ks.cooldown_reason)
        self.assertEqual(ks.model_cooldown, {}, "账号级走整把 key，不再另记模型级明细")
        rt = relay.providers["p1"]
        self.assertIsNone(rt.next_key_slot("mock-ok"), "整把 key 冷却时任何模型都拿不到")
        self.assertIsNone(rt.next_key_slot("mock-429-account"))
        self.assertFalse(rt.has_usable_key("mock-ok"))
        self.assertFalse(rt.has_usable_key("mock-429-account"))
        self.assertEqual(status, 429, body)
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit"],
                         "整把 key 冷却后，同 key 的健康模型 B 也不该再打上游")
        self.assertEqual(self.mock.stats()["per_model"].get("mock-ok"), None,
                         "账号级 429 之后 B 绝不许再去打上游")

    # ------------------------------------------- T61) Retry-After 决定模型冷却时长
    def test_61_retry_after_seconds_drives_model_cooldown(self):
        """T61（R1.3）：429 带 Retry-After: 2 → 冷却是 2s，不是默认的 key_cooldown_s=120。"""
        p1 = provider_cfg("p1", ["mock-429-reset"], ["TEST_KEY_A"], self.mock.base_url,
                          key_cooldown_s=120)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "ka-ok"})
        status, _, _ = relay.chat(self.ask())
        self.assertEqual(status, 429)
        ks = relay.providers["p1"].keys[0]
        left = ks.model_cooldown_left("mock-429-reset")
        self.assertGreater(left, 0.0, "必须真的冷却")
        self.assertLessEqual(left, 2.5, f"必须按 Retry-After: 2 走 2s，而不是默认 120s（实测 {left:.2f}s）")
        self.assertGreater(left, 1.0, f"不能比 Retry-After 短太多（实测 {left:.2f}s）")
        self.assertLess(left, float(relay._key_cooldown("p1")),
                        "冷却时长必须短于默认 key_cooldown_s，证明用的是 Retry-After")

    # ------------------------------------------- T62) 候选过滤按模型判断（provider 级）
    def test_62_candidate_filter_is_per_model(self):
        """T62（R3.3）：provider 只有一把 key、该 key 只是被模型 A 弄脏 → 含 B 的候选不许被过滤掉。"""
        relay, _ = self._sibling_key_scaffold(key_cooldown_s=120)
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-ok")
        rt = relay.providers["p1"]
        self.assertGreater(rt.keys[0].model_cooldown_left("mock-429"), 0.0)
        self.assertEqual(rt.keys[0].cooldown_left(), 0.0, "脏的只是模型，不是整把 key")
        cands = relay.build_candidates(False, False)
        self.assertEqual([c.model_id for c in cands], ["mock-ok"],
                         "B 的候选必须还在；A 的模型冷却不许把整个 provider 过滤掉")
        skipped = relay.last_candidates_info["skipped"]
        self.assertIn("p1/mock-429", [s[0] for s in skipped], "A 自身应被跳过")
        self.assertNotIn("p1/mock-ok", [s[0] for s in skipped], "B 绝不许被跳过")
        self.assertIn("mock-429", relay.last_candidates_info["skipped"][0][2],
                      "跳过原因要写清是哪个模型把这把 key 弄脏的")
        self.assertIsNotNone(rt.next_key_slot("mock-ok"))

    # ---------------------------------------------- T63) /status 可观测按模型冷却明细
    def test_63_status_exposes_per_model_cooldown_details(self):
        """T63（R4）：/status 能看到按模型的冷却明细与原因分类（模型额度 vs 账号级）。"""
        relay, _ = self._sibling_key_scaffold()
        relay.chat(self.ask())
        st = relay.status()
        key0 = st["providers"][0]["keys"][0]
        self.assertEqual(key0["cooldown_left_s"], 0.0, "模型级 429 不冷整把 key")
        mcs = {d["model"]: d for d in key0["model_cooldowns"]}
        self.assertIn("mock-429", mcs, "必须能看到是哪个模型被冷")
        self.assertNotIn("mock-ok", mcs, "健康模型不许出现在冷却明细里")
        self.assertGreater(mcs["mock-429"]["left_s"], 0.0)
        self.assertIn("模型额度", mcs["mock-429"]["reason"], "原因要能区分「模型额度」")
        self.assertNotIn("ka-ok", json.dumps(st, ensure_ascii=False), "状态里不许出现 key 明文")
        # 面板同样能看到按模型冷却，且不泄露 key（必须在 relay2 覆盖同一份临时 config 之前查）
        port = self.start_http(relay)
        status, raw, _ = self.http(port, "/")
        self.assertEqual(status, 200)
        panel = raw.decode("utf-8", "replace")
        self.assertIn("模型冷却", panel)
        self.assertIn("mock-429", panel)
        self.assertNotIn("ka-ok", panel)
        # 账号级 → 整把 key 冷却，原因分类为「账号级」
        p2 = provider_cfg("p2", [("mock-429-account", 1)], ["TEST_KEY_B"], self.mock.base_url)
        relay2 = self.make_relay([p2], keys={"TEST_KEY_B": "kb-ok"})
        relay2.chat(self.ask())
        key2 = relay2.status()["providers"][0]["keys"][0]
        self.assertGreater(key2["cooldown_left_s"], 0.0)
        self.assertIn("账号级", key2["cooldown_reason"])
        self.assertEqual(key2["model_cooldowns"], [])

    # --------------------------- T64) 所有 key 都在窗口内失败 → provider 级冷却仍触发
    def test_64_provider_cooldown_still_escalates_when_all_keys_fail(self):
        """T64（R4.3）：≥2 把 key 都在窗口内 429 → provider 冷却照旧；只有 1 把 key 时不许升级。"""
        p1 = provider_cfg("p1", ["mock-ok"], ["TEST_KEY_A", "TEST_KEY_B"], self.mock.base_url,
                          key_cooldown_s=60, provider_cooldown_s=300)
        relay = self.make_relay([p1], keys={"TEST_KEY_A": "k1-busy", "TEST_KEY_B": "k2-busy"})
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 429, body)
        rt = relay.providers["p1"]
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit", "ratelimit"],
                         "两把 key 都该被试到（各自只冷自己的 (key, 模型)）")
        self.assertTrue(all(k.model_cooldown_left("mock-ok") > 0.0 for k in rt.keys),
                        "两把 key 上的该模型都应被冷")
        self.assertGreater(rt.cooldown_left(), 0.0,
                           "所有 key 在窗口内都失败过 → 既有升级逻辑必须触发 provider 冷却")
        self.assertIn("所有 key 在窗口内都被 429", "\n".join(relay.log_lines))
        # 只有一把 key 的 provider 不许被整池冷却（升级逻辑要求 len(keys) >= 2）
        p2 = provider_cfg("p2", ["mock-ok"], ["TEST_KEY_C"], self.mock.base_url,
                          key_cooldown_s=60, provider_cooldown_s=300)
        relay2 = self.make_relay([p2], keys={"TEST_KEY_C": "k3-busy"})
        status2, _, _ = relay2.chat(self.ask())
        self.assertEqual(status2, 429)
        self.assertEqual(relay2.providers["p2"].cooldown_left(), 0.0,
                         "只有一把 key 时不许把 provider 整池冷却")
        self.assertGreater(relay2.providers["p2"].keys[0].model_cooldown_left("mock-ok"), 0.0)


    # ==================================================================================
    # P0.7：降级路径也必须做 schema 一致性校验（T65–T70，全打 mock，不烧真额度）
    # ==================================================================================
    DEGRADE_CAPS = {"json_schema": "degradable", "tools": True, "reasoning_only": False}
    # Hindsight retain 的真实形状：facts 是「对象数组」（含 content/entities），language 是字符串
    FACTS_OBJECT_SCHEMA = {
        "type": "object",
        "properties": {
            "facts": {"type": "array", "items": {"type": "object", "properties": {
                "content": {"type": "string"},
                "entities": {"type": "array", "items": {"type": "string"}}},
                "required": ["content"]}},
            "language": {"type": "string"},
        },
        "required": ["facts", "language"],
        "additionalProperties": False,
    }
    FACTS_OBJECT_REQ = {"type": "json_schema", "json_schema": {"name": "facts", "strict": True,
                                                               "schema": FACTS_OBJECT_SCHEMA}}
    # §0 真机抓到的坏 content：properties 片段回显（facts 不是数组，而是类型说明对象）
    PROPS_ECHO = {"facts": {"type": "array", "items": {"type": "string"}}, "language": {"type": "string"}}

    # ------------------------- T65) 降级 + properties 片段回显 → 单候选 502，绝不 200
    def test_65_degraded_props_echo_rejected_single_candidate_502(self):
        """T65（P0.7 R1/R2/R3）：降级候选回 properties 片段回显 → 不合格；单候选最终 502。"""
        p1 = provider_cfg("p_deg_echo", [("mock-schema-props-echo", 1, self.DEGRADE_CAPS)],
                          ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, meta = relay.chat(self.ask("从这句话抽取事实", response_format=JSON_REQ))
        self.assertNotEqual(status, 200, f"降级路径的坏 content 绝不许当成功交出去：{body}")
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "relay_unusable_content")
        self.assertEqual(body["error"]["code"], "relay_unusable_content")
        self.assertEqual(len(meta["attempts"]), 1, meta["attempts"])
        self.assertIn(meta["attempts"][0]["normalize"], ("schema_echo", "schema_mismatch"),
                      "properties 片段回显必须被判不合格")
        self.assertTrue(meta["attempts"][0]["degrade"], "这次尝试确实走了降级路径")
        # R3：降级路径 + R1 判不过要能被单独看出来（不是只看 normalize）
        self.assertEqual(meta.get("degraded_rejected"), 1)
        self.assertGreaterEqual(relay.status()["counters"].get("degraded_schema_rejected", 0), 1)
        self.assertTrue(any("降级路径" in line for line in relay.log_lines),
                        "日志要能看出这是降级路径判不过")
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn('"properties"', blob, "响应体里绝不许出现 schema 本体")
        self.assertNotIn("items", blob, "回显片段也不许透传给 Hindsight")
        # 降级请求里必须没有 response_format（schema 内嵌提示词）
        self.assertFalse(self.mock.stats()["requests"][-1]["has_response_format"])
        self.assertEqual(self.mock.stats()["calls"], 1)

    # ------------------------- T66) 降级回显判失败后 failover 到健康候选，返回合规结果
    def test_66_degraded_props_echo_fails_over_to_healthy_candidate(self):
        """T66（P0.7 R1/R4）：候选 1（降级）回显判失败 → 换候选 2，200 且结果是合规的。"""
        p1 = provider_cfg("p_deg_echo", [("mock-schema-props-echo", 1, self.DEGRADE_CAPS)],
                          ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_json", [("mock-json", 2)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p1, p2])
        status, body, meta = relay.chat(self.ask("从这句话抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["model"], "mock-json", "回显候选必须被丢弃，换下一个候选")
        self.assertGreaterEqual(len(meta["attempts"]), 2, meta["attempts"])
        self.assertIn(meta["attempts"][0]["normalize"], ("schema_echo", "schema_mismatch"))
        self.assertTrue(meta["attempts"][0]["degrade"])
        parsed = json.loads(body["choices"][0]["message"]["content"])
        ok, why = llm_relay.validate_against_schema(parsed, SCHEMA)
        self.assertTrue(ok, f"交给 Hindsight 的必须是符合 schema 的结果：{why}")
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"])

    # ------------------------- T67) 降级 + 合规输出 → 放行（不误伤）
    def test_67_degraded_conformant_output_passes(self):
        """T67（P0.7 R1/R4）：降级候选给出符合 schema 的输出 → 200 通过，不得误伤。"""
        p1 = provider_cfg("p_deg_ok", [("mock-json", 1, self.DEGRADE_CAPS)],
                          ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([p1])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertTrue(meta["degraded"])
        self.assertEqual(meta["attempts"][0]["normalize"], "ok",
                         "合规的降级输出不能被任何 schema 类判据误伤")
        self.assertTrue(meta["attempts"][0]["degrade"])
        parsed = json.loads(body["choices"][0]["message"]["content"])
        ok, why = llm_relay.validate_against_schema(parsed, SCHEMA)
        self.assertTrue(ok, why)
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"])
        self.assertEqual(parsed["language"], "zh")
        last = self.mock.stats()["requests"][-1]
        self.assertFalse(last["has_response_format"], "降级请求里必须没有 response_format")
        self.assertIn("JSON Schema", last["last_user"], "schema 必须内嵌进提示词")
        self.assertEqual(relay.models["p_deg_ok/mock-json"].counts.get("degraded"), 1,
                         "合规的降级成功仍要记 degraded 计数")

    # ------------------------- T68) 降级 + 缺 required / 顶层类型错 → 判失败并 failover
    def test_68_degraded_missing_required_and_type_mismatch_failover(self):
        """T68（P0.7 R1）：降级路径下缺 required / 顶层类型错也必须判失败并换候选。"""
        p1 = provider_cfg("p_deg_missing", [("mock-schema-missing", 1, self.DEGRADE_CAPS)],
                          ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_deg_arr", [("mock-schema-array", 2, self.DEGRADE_CAPS)],
                          ["TEST_KEY_B"], self.mock.base_url)
        p3 = provider_cfg("p_json", [("mock-json", 3)], ["TEST_KEY_C"], self.mock.base_url)
        relay = self.make_relay([p1, p2, p3],
                                keys={f"TEST_KEY_{k}": f"k{k}-ok" for k in "ABC"},
                                request={"max_candidates": 3})
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 200, body)
        self.assertEqual([a["normalize"] for a in meta["attempts"][:2]],
                         ["schema_mismatch", "schema_mismatch"])
        self.assertIn("missing_required:facts", meta["attempts"][0].get("normalize_detail", ""))
        self.assertIn("type_mismatch", meta["attempts"][1].get("normalize_detail", ""))
        self.assertTrue(all(a["degrade"] for a in meta["attempts"][:2]))
        self.assertEqual(meta["model"], "mock-json", "判不过就必须继续换候选")
        parsed = json.loads(body["choices"][0]["message"]["content"])
        self.assertEqual(parsed["facts"], ["我从 Mac 上跑 Hindsight"])

    # ------------------------- T69) R2 新判据不误伤合法结果
    def test_69_props_echo_criterion_does_not_misfire_on_legit_result(self):
        """T69（P0.7 R2）：新回显判据抓得住 properties 片段，又不误伤合法的对象数组结果。"""
        good = {"facts": [{"content": "x", "entities": ["y"]}], "language": "zh"}
        self.assertFalse(llm_relay.looks_like_schema_echo(good), "键名不是 schema 关键字，旧判据本就不该命中")
        self.assertFalse(llm_relay.looks_like_props_fragment_echo(good, self.FACTS_OBJECT_SCHEMA),
                         "facts 是数组、language 是字符串 → 不是 schema 节点回显")
        ok, why, detail = llm_relay.check_content_against_schema(good, self.FACTS_OBJECT_SCHEMA)
        self.assertTrue(ok, f"合法结果必须放行：{why} {detail}")
        parsed, _, reason = llm_relay.validate_json_content(json.dumps(good, ensure_ascii=False), True,
                                                           self.FACTS_OBJECT_SCHEMA)
        self.assertIsNotNone(parsed, reason)
        self.assertEqual(reason, "ok")
        # 反向：§0 的 properties 片段回显在这条新判据下必须判 schema_echo
        self.assertTrue(llm_relay.looks_like_props_fragment_echo(self.PROPS_ECHO, SCHEMA))
        ok2, why2, _ = llm_relay.check_content_against_schema(self.PROPS_ECHO, SCHEMA)
        self.assertFalse(ok2)
        self.assertEqual(why2, "schema_echo")
        # 顶层类型说明对象也可以是一串别的 schema 说明值 → 同样判回显
        other = {"a": {"properties": {"x": {"type": "string"}}}, "b": {"enum": ["x"]}}
        self.assertTrue(llm_relay.looks_like_props_fragment_echo(other, {"properties": {"a": {}, "b": {}}}))

    # ------------- T71) 降级 + 全候选不合格 → 502（不是最后一条上游 200 体，也不透传坏 content）
    def test_71_degraded_all_candidates_bad_never_returns_200(self):
        """T71（P0.7 R1/R3/R4）：降级候选不合格 → 502 relay_unusable_content，绝不 200 / 透传。"""
        bad = provider_cfg("p_deg_bad", [("mock-schema-props-echo", 1, self.DEGRADE_CAPS)],
                           ["TEST_KEY_A"], self.mock.base_url)
        relay = self.make_relay([bad])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 502, body)
        self.assertEqual(body["error"]["type"], "relay_unusable_content")
        self.assertEqual(body["error"].get("degraded_attempts"), 1)
        self.assertEqual(meta.get("degraded_rejected"), 1)
        counters = relay.status()["counters"]
        self.assertGreaterEqual(counters.get("unusable_content", 0), 1)
        self.assertGreaterEqual(counters.get("unusable_content_rejected", 0), 1)
        self.assertGreaterEqual(counters.get("degraded_schema_rejected", 0), 1)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn('"items"', blob, "坏 content 不许透传")
        self.assertNotIn("我从 Mac 上跑 Hindsight", blob)
        # 降级请求里没有 response_format；一次请求只打一次上游
        self.assertFalse(self.mock.stats()["requests"][-1]["has_response_format"])
        self.assertEqual(self.mock.stats()["calls"], 1)

    # ==================================== T0.2 默认别名 default（改名 + 兼容锚点）
    def test_72_missing_model_uses_default_alias(self):
        """T0.2：请求不带 model → meta.alias 必须等于 config.default_alias（= default）。"""
        relay = self.make_relay([self.mock_provider()])
        payload = self.ask()
        payload.pop("model", None)
        status, body, meta = relay.chat(payload)
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["alias"], "default")
        self.assertEqual(meta["alias"], relay.cfg["default_alias"],
                         "不带 model 时记进 usage 的 alias 必须与 default_alias 一致")
        port = self.start_http(relay)
        status, models, _ = self.http_json(port, "/v1/models")
        self.assertEqual(status, 200)
        self.assertEqual(models["data"][0]["id"], "default",
                         "/v1/models 暴露的别名 id 必须等于 default_alias，不再是旧名")

    def test_73_model_default_returns_200(self):
        """T0.2：model=default → 200，alias 回显 default。"""
        relay = self.make_relay([self.mock_provider()])
        status, body, meta = relay.chat(self.ask(model="default"))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["alias"], "default")

    def test_74_model_hindsight_compat_returns_200(self):
        """T0.2：model=hindsight（线上调用方真的在用的旧名字）→ 仍然 200（兼容锚点）。"""
        relay = self.make_relay([self.mock_provider()])
        status, body, meta = relay.chat(self.ask(model="hindsight"))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["alias"], "hindsight", "兼容别名要原样记进 usage.jsonl，不能偷偷改写")

    # ==================================================== T1.2 命名路由
    # 每个用例都断言零副作用：临时目录里的 config.json / keys.env 内容哈希前后一致，
    # repo 的 config.json 全程只读。
    def _file_digests(self):
        import hashlib
        out = {}
        for p in sorted(self.tmp.rglob("*")):
            if p.is_file():
                out[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def test_75_route_hit_uses_route_chain_and_auto_matches_global(self):
        """T1.2①：model 命中 route 名 → 用该 route 的 chain；auto 路由与 build_candidates() 逐项同序。"""
        p1 = provider_cfg("p_auto1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_auto2", ["mock-ok"], ["TEST_KEY_B"], self.mock.base_url)
        routes = {"default": {"chain": "auto"}, "only2": {"chain": ["p_auto2/mock-ok"]}}
        relay = self.make_relay([p1, p2], routes=routes)
        before = self._file_digests()

        # auto 路由锚点：与直接调 build_candidates() 的序列逐项一致
        direct = [(c.provider, c.model_id, c.chain) for c in relay.build_candidates(False, False)]
        via_auto = [(c.provider, c.model_id, c.chain)
                    for c in relay.build_candidates(False, False, chain="auto", policy={})]
        self.assertEqual(via_auto, direct, "auto 路由必须与旧版 build_candidates() 逐项同序")

        status, body, meta = relay.chat(self.ask(model="only2"))
        self.assertEqual(status, 200, body)
        self.assertEqual((meta["provider"], meta["model"]), ("p_auto2", "mock-ok"),
                         "model 命中 route 名时只该用该 route 的 chain")
        self.assertEqual(meta["alias"], "only2")
        self.assertEqual(before, self._file_digests(), "路由解析不许碰临时目录里的任何文件")

    def test_76_explicit_provider_model_points_at_one_candidate(self):
        """T1.2②：model=provider/model → 只打那一个候选（不经全局链）。"""
        p_early = provider_cfg("p_early", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        p_named = provider_cfg("p_named", [("mock-json", 99)], ["TEST_KEY_B"], self.mock.base_url)
        relay = self.make_relay([p_early, p_named])
        before = self._file_digests()
        status, body, meta = relay.chat(self.ask(model="p_named/mock-json"))
        self.assertEqual(status, 200, body)
        self.assertEqual((meta["provider"], meta["model"]), ("p_named", "mock-json"),
                         "精确点名必须无视 chain 升序，只打点名的那个模型")
        self.assertEqual(meta["alias"], "p_named/mock-json")
        self.assertEqual(before, self._file_digests())

    def test_77_route_policy_max_candidates_overrides_request(self):
        """T1.2③：policy.max_candidates 覆盖 request.max_candidates（比它小）。"""
        ps = [provider_cfg(f"p_cap{i}", ["mock-ok"], [f"TEST_KEY_{i}"], self.mock.base_url)
              for i in range(1, 4)]
        routes = {"default": {"chain": "auto", "policy": {"max_candidates": 1}}}
        relay = self.make_relay(ps, request={"max_candidates": 3},
                                keys={f"TEST_KEY_{i}": f"k{i}-ok" for i in range(1, 4)},
                                routes=routes)
        before = self._file_digests()
        status, body, meta = relay.chat(self.ask(model="default"))
        self.assertEqual(status, 200, body)
        self.assertEqual(len(relay.last_candidates_info["picked"]), 1,
                         "route policy.max_candidates=1 必须把候选截到 1 个（request 里是 3）")
        self.assertEqual(before, self._file_digests())

    def test_78_free_only_keeps_free_and_503_when_none(self):
        """T1.2④：free_only 只留 free:true；零候选回 503 + relay_no_candidates，说明里含 free_only。"""
        free_p = provider_cfg("p_free", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url, free=True)
        paid_p = provider_cfg("p_paid", ["mock-json"], ["TEST_KEY_B"], self.mock.base_url)
        routes = {"default": {"chain": "auto", "policy": {"free_only": True}}}
        relay = self.make_relay([paid_p, free_p], routes=routes)
        before = self._file_digests()
        status, body, meta = relay.chat(self.ask(model="default"))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_free", "free_only 只允许显式 free=true 的候选")
        picked = [(c.provider, c.model_id) for c in relay.last_candidates_info["picked"]]
        self.assertEqual(picked, [("p_free", "mock-ok")])
        self.assertFalse(any(c.free for c in relay.last_candidates_info["picked"] if c.provider == "p_paid"))
        self.assertEqual(before, self._file_digests())

        # 反向：没有任何 free=true 候选 → 503，且原因里必须点名 free_only
        relay2 = self.make_relay([paid_p], routes=routes)
        before2 = self._file_digests()
        status2, body2, meta2 = relay2.chat(self.ask(model="default"))
        self.assertEqual(status2, 503, body2)
        self.assertEqual(body2["error"]["type"], "relay_no_candidates")
        self.assertIn("free_only", body2["error"]["message"])
        self.assertEqual(before2, self._file_digests())

    def test_79_unknown_model_returns_400_with_names(self):
        """T1.2⑤：未知 model → 400，body 里列出可用 route 名与可点名的 provider/model。"""
        relay = self.make_relay([self.mock_provider()])
        before = self._file_digests()
        status, body, meta = relay.chat(self.ask(model="no-such-model"))
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"]["type"], "relay_unknown_model")
        blob = json.dumps(body, ensure_ascii=False)
        self.assertIn("hindsight", blob, "body 必须列出现有 route 名")
        self.assertIn("default", blob)
        self.assertIn("p1/mock-ok", blob, "body 必须列出可点名的 provider/model")
        self.assertEqual(meta["status"], 400)
        self.assertEqual(before, self._file_digests())

    def test_80_probe_bypass_still_uses_global_chain(self):
        """T1.2⑥：model=probe 是内部旁路 —— 不做路由解析、仍走全局链，alias 记 probe。"""
        p1 = provider_cfg("p_probe1", ["mock-ok"], ["TEST_KEY_A"], self.mock.base_url)
        p2 = provider_cfg("p_probe2", ["mock-json"], ["TEST_KEY_B"], self.mock.base_url)
        # routes 里故意没有 probe：若参与解析就会 400
        relay = self.make_relay([p1, p2], routes={"default": {"chain": "auto"}})
        before = self._file_digests()
        status, body, meta = relay.chat(self.ask(model="probe"))
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_probe1", "probe 必须仍走全局 auto 链（chain 最小的 p_probe1）")
        self.assertEqual(meta["alias"], "probe")
        self.assertEqual(before, self._file_digests())

    # ==================================================== T1.1/T1.4 调用方 key / 配额隔离
    CALLERS = {
        "hindsight": {"key": "local-relay", "rpm": 60, "daily_tokens": 3000000,
                      "allow_routes": ["hindsight"], "note": "Hindsight 保留链路"},
        "hermes-mac": {"key_env": "RELAY_CALLER_HERMES", "rpm": 5, "daily_tokens": 1000,
                       "allow_routes": ["default", "cheap"]},
    }

    def usage_rows(self):
        path = self.tmp / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def usage_log_tmp(self):
        return {"enabled": True, "path": str(self.tmp / "usage.jsonl"), "max_mb": 20, "keep": 3}

    def test_81_loopback_trust_allows_anonymous_and_records_caller(self):
        """T1.4①：默认 loopback_trust —— 本机不带 key 仍 anonymous，usage/recent 都记 caller。"""
        relay = self.make_relay([self.mock_provider()], usage_log=self.usage_log_tmp())
        port = self.start_http(relay)
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(relay.recent[-1]["caller"], "anonymous")
        self.assertEqual(self.usage_rows()[-1]["caller"], "anonymous")
        self.assertEqual(relay.status()["auth_mode"], "loopback_trust")
        self.assertFalse(relay.admin_callers()["configured"],
                         "config.json 没有 callers 段时 /admin/callers 必须 configured:false（面板隐藏 tab）")

    def test_82_caller_key_match_and_usage_caller_field(self):
        """T1.1：Bearer 命中 caller key → 该 caller；usage/recent 记名；/admin/callers 只给指纹。"""
        relay = self.make_relay(
            [self.mock_provider()], callers=self.CALLERS, usage_log=self.usage_log_tmp(),
            keys={"TEST_KEY_A": "k1-ok", "TEST_KEY_B": "k2-ok", "RELAY_CALLER_HERMES": "hm-key"})
        port = self.start_http(relay)
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                        token="local-relay")
        self.assertEqual(status, 200, body)
        self.assertEqual(relay.recent[-1]["caller"], "hindsight")
        self.assertEqual(self.usage_rows()[-1]["caller"], "hindsight")
        d = relay.admin_callers()
        self.assertTrue(d["configured"])
        row = {c["name"]: c for c in d["callers"]}["hindsight"]
        self.assertTrue(row["key_set"])
        self.assertEqual(len(row["key_fp"]), 8)
        self.assertEqual(row["allow_routes"], ["hindsight"])
        self.assertNotIn("local-relay", json.dumps(d, ensure_ascii=False), "面板侧永远看不到明文 key")
        self.assertNotIn("local-relay", json.dumps(relay.status(), ensure_ascii=False))

    def test_83_unknown_bearer_401_relay_auth(self):
        """T1.4②：带了 Bearer 但谁都不匹配 → 401 relay_auth，且日志不落 key 明文。"""
        relay = self.make_relay([self.mock_provider()], callers=self.CALLERS)
        port = self.start_http(relay)
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                        token="totally-wrong")
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"]["type"], "relay_auth")
        self.assertIn("hindsight", body["error"]["callers"])
        self.assertNotIn("totally-wrong", "\n".join(relay.log_lines))

    def test_84_require_key_mode_and_non_loopback_rules(self):
        """T1.4③：auth.mode=require_key 时本机也必须带 key；非本机（无论什么模式）一律要 key。"""
        relay = self.make_relay([self.mock_provider()], auth={"mode": "require_key"},
                                callers=self.CALLERS)
        port = self.start_http(relay)
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask())
        self.assertEqual(status, 401, body)
        self.assertEqual(body["error"]["type"], "relay_auth")
        self.assertEqual(body["error"]["auth_mode"], "require_key")
        status, body, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                        token="local-relay")
        self.assertEqual(status, 200, body)
        self.assertEqual(relay.status()["auth_mode"], "require_key")

        r2 = self.make_relay([self.mock_provider()], callers=self.CALLERS)
        self.assertEqual(r2.resolve_caller("", False)[1], 401, "非本机 + 无 key 必须 401")
        self.assertEqual(r2.resolve_caller("Bearer local-relay", False)[0], "hindsight",
                         "非本机带对 key 仍应放行")
        self.assertEqual(r2.resolve_caller("", True)[0], "anonymous",
                         "本机 + loopback_trust → anonymous")

    def test_85_rpm_limit_429_with_retry_after(self):
        """T1.1①：rpm 滑动窗口超限 → 429 relay_caller_rate_limit + Retry-After，被拒不烧 token。"""
        callers = {"hindsight": {"key": "local-relay", "rpm": 1, "allow_routes": ["hindsight"]}}
        relay = self.make_relay([self.mock_provider()], callers=callers,
                                usage_log=self.usage_log_tmp())
        port = self.start_http(relay)
        s1, b1, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                   token="local-relay")
        self.assertEqual(s1, 200, b1)
        used = relay.callers.tokens_today("hindsight")
        s2, b2, h2 = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                    token="local-relay")
        self.assertEqual(s2, 429, b2)
        self.assertEqual(b2["error"]["type"], "relay_caller_rate_limit")
        self.assertIn("Retry-After", h2, "调用方限流必须带 Retry-After")
        self.assertEqual(relay.callers.rpm_used("hindsight"), 1, "被拒的请求不占窗口名额")
        self.assertEqual(relay.callers.tokens_today("hindsight"), used, "被拒的请求不得再记 token")
        row = self.usage_rows()[-1]
        self.assertEqual(row["caller"], "hindsight")
        self.assertEqual(row["verdict"], "caller_rate_limit")
        self.assertEqual(row["prompt_tokens"], 0)

    def test_86_daily_tokens_quota_429_and_lazy_rebuild(self):
        """T1.1②：daily_tokens 用本地自然日累计；老 rows 惰性重建，隔天/别的 caller 不算。"""
        today = time.strftime("%Y-%m-%d")
        old = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        pre = [
            {"ts": f"{today}T01:00:00", "caller": "hindsight", "prompt_tokens": 60,
             "completion_tokens": 60, "total_tokens": 120},
            {"ts": f"{today}T02:00:00", "caller": "hindsight", "prompt_tokens": 40,
             "completion_tokens": 0, "total_tokens": 40},
            {"ts": f"{old}T02:00:00", "caller": "hindsight", "prompt_tokens": 9999,
             "completion_tokens": 0, "total_tokens": 9999},
            {"ts": f"{today}T03:00:00", "prompt_tokens": 777, "completion_tokens": 0,
             "total_tokens": 777},  # 老版本没有 caller 字段 → 不算到任何调用方
        ]
        (self.tmp / "usage.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in pre), encoding="utf-8")
        callers = {"hindsight": {"key": "local-relay", "rpm": 60, "daily_tokens": 100,
                                 "allow_routes": ["hindsight"]}}
        relay = self.make_relay([self.mock_provider()], callers=callers,
                                usage_log=self.usage_log_tmp())
        self.assertEqual(relay.callers.tokens_today("hindsight"), 160,
                         "只累加「同一 caller + 今天」：120 + 40 = 160；昨天的 9999 与无 caller 的 777 都不算")
        port = self.start_http(relay)
        s, b, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(),
                                 token="local-relay")
        self.assertEqual(s, 429, b)
        self.assertEqual(b["error"]["type"], "relay_caller_quota")
        self.assertEqual(relay.callers.tokens_today("hindsight"), 160,
                         "被预算拒掉的请求不得再加 token（也不能把老 rows 重复累加）")

    def test_87_allow_routes_forbidden_403(self):
        """T1.1③：allow_routes 白名单外的 route → 403 relay_forbidden；白名单内照常放行。"""
        routes = {"default": {"chain": "auto"}, "hindsight": {"chain": "auto"},
                  "cheap": {"chain": "auto"}}
        callers = {"hindsight": {"key": "local-relay", "allow_routes": ["cheap"]}}
        relay = self.make_relay([self.mock_provider()], callers=callers, routes=routes,
                                usage_log=self.usage_log_tmp())
        port = self.start_http(relay)
        s1, b1, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(model="hindsight"),
                                   token="local-relay")
        self.assertEqual(s1, 403, b1)
        self.assertEqual(b1["error"]["type"], "relay_forbidden")
        self.assertEqual(b1["error"]["allow_routes"], ["cheap"])
        s2, b2, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(model="cheap"),
                                   token="local-relay")
        self.assertEqual(s2, 200, b2)
        self.assertEqual(self.usage_rows()[-1]["caller"], "hindsight")

    def test_88_probe_bypasses_caller_quota_and_routes(self):
        """T1.1④：model=probe 是内部旁路 —— 不参与调用方配额/白名单（面板实测不能把调用方锁死）。"""
        callers = {"hindsight": {"key": "local-relay", "rpm": 1, "allow_routes": ["cheap"]}}
        relay = self.make_relay([self.mock_provider()], callers=callers,
                                routes={"default": {"chain": "auto"}, "cheap": {"chain": "auto"}})
        port = self.start_http(relay)
        for i in range(3):
            s, b, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(model="probe"),
                                     token="local-relay")
            self.assertEqual(s, 200, f"probe 第 {i + 1} 次也必须放行：{b}")
        self.assertEqual(relay.callers.rpm_used("hindsight"), 0, "probe 不吃调用方 rpm")

    def test_89_key_env_resolution_and_admin_callers_view(self):
        """T1.1⑤：callers.key_env 从 keys.env 取值；/admin/callers 只暴露指纹与用量。"""
        relay = self.make_relay(
            [self.mock_provider()], callers=self.CALLERS, usage_log=self.usage_log_tmp(),
            keys={"TEST_KEY_A": "k1-ok", "TEST_KEY_B": "k2-ok", "RELAY_CALLER_HERMES": "hm-key"})
        port = self.start_http(relay)
        s, b, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(model="default"),
                                 token="hm-key")
        self.assertEqual(s, 200, b)
        self.assertEqual(relay.recent[-1]["caller"], "hermes-mac")
        d = relay.admin_callers()
        row = {c["name"]: c for c in d["callers"]}["hermes-mac"]
        self.assertEqual(row["key_source"], "key_env:RELAY_CALLER_HERMES")
        self.assertTrue(row["key_set"])
        self.assertEqual(row["key_fp"], llm_relay.sha8("hm-key"))
        self.assertNotIn("hm-key", json.dumps(d, ensure_ascii=False))
        self.assertEqual(row["rpm"], 5)
        self.assertEqual(row["daily_tokens"], 1000)

    def test_90_missing_callers_section_equals_today(self):
        """T1.1⑨：没有 callers 段 = 调用方体系未启用 —— Authorization 一律忽略，无 401/403/429。"""
        relay = self.make_relay([self.mock_provider()], callers={},
                                usage_log=self.usage_log_tmp())
        port = self.start_http(relay)
        # 今天 Hindsight 一直在发 Bearer local-relay；没配 callers 时它必须照旧 200
        for tok in (None, "local-relay", "whatever-unknown"):
            s, b, _ = self.http_json(port, "/v1/chat/completions", payload=self.ask(), token=tok)
            self.assertEqual(s, 200, f"token={tok} 时没有 callers 段也必须照旧放行：{b}")
            self.assertEqual(relay.recent[-1]["caller"], "anonymous")
        self.assertEqual(relay.callers.rpm_used("anonymous"), 0, "anonymous 不参与限流计数")
        self.assertFalse(relay.admin_callers()["configured"])


    def write_usage(self, rows: list[dict]) -> None:
        (self.tmp / "usage.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")

    def t1_3_relay(self, **kw):
        return self.make_relay([self.mock_provider()], usage_log=self.usage_log_tmp(), **kw)

    def start_http_all(self, relay) -> int:
        srv = llm_relay.create_server(relay, "0.0.0.0", 0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()
        self._servers.append(srv)
        return srv.server_address[1]

    def test_91_usage_api_four_groups_return_complete_structure(self):
        """T1.3①：四种 group 都返回结构完整的窗口聚合（请求/成败/token/占比/窗口起止）。"""
        now = time.time()
        self.write_usage([
            _usage_row(now - 60, caller="hindsight", route="hindsight", provider="p1", model="m-a"),
            _usage_row(now - 120, caller="hindsight", route="cheap", provider="p1", model="m-b"),
            _usage_row(now - 180, caller="hermes-mac", route="cheap", provider="p2", model="m-b"),
            _usage_row(now - 240, caller="hermes-mac", route="hindsight", provider="p2",
                       model="m-a", http=429, prompt=0, completion=0),
        ])
        relay = self.t1_3_relay()
        for group in USAGE_KEYS:
            d = relay.admin_usage_group(group, "24h")
            self.assertEqual(d["group"], group)
            self.assertEqual(d["window"], "24h")
            self.assertEqual(d["totals"]["requests"], 4, d)
            self.assertEqual(d["totals"]["ok"], 3)
            self.assertEqual(d["totals"]["failed"], 1)
            self.assertEqual(d["totals"]["total_tokens"], 3 * 15)
            self.assertEqual(len(d["groups"]), 2, f"{group} 应有两个分组：{d}")
            for item in d["groups"]:
                for k in ("name", "requests", "ok", "failed", "prompt_tokens",
                          "completion_tokens", "total_tokens", "share", "token_share",
                          "success_rate", "last_ts"):
                    self.assertIn(k, item, f"{group}.{k} 缺失：{item}")
            self.assertAlmostEqual(sum(g["share"] for g in d["groups"]), 1.0, places=3)
            self.assertIn("since", d)
            self.assertIn("until", d)
        # 每个分组名确实按对应字段分：caller 分两组、provider 分两组
        self.assertEqual({g["name"] for g in relay.admin_usage_group("caller", "24h")["groups"]},
                         {"hindsight", "hermes-mac"})
        self.assertEqual({g["name"] for g in relay.admin_usage_group("route", "24h")["groups"]},
                         {"hindsight", "cheap"})
        self.assertEqual({g["name"] for g in relay.admin_usage_group("model", "24h")["groups"]},
                         {"m-a", "m-b"})

    def test_92_usage_api_illegal_group_and_window_400(self):
        """T1.3②：非法 group / window → 400，并在错误体里列出可选值。"""
        relay = self.t1_3_relay()
        port = self.start_http(relay)
        code, body, _ = self.http_json(port, "/admin/usage?group=bogus")
        self.assertEqual(code, 400, body)
        self.assertIn("caller", body["detail"])
        self.assertIn("model", body["detail"])
        code, body, _ = self.http_json(port, "/admin/usage?window=99y")
        self.assertEqual(code, 400, body)
        self.assertIn("1h", body["detail"])
        self.assertIn("7d", body["detail"])
        # 合法值 + 缺省（不传 group/window → caller / 24h）必须 200
        code, body, _ = self.http_json(port, "/admin/usage")
        self.assertEqual(code, 200, body)
        self.assertEqual((body["group"], body["window"]), ("caller", "24h"))
        code, body, _ = self.http_json(port, "/admin/usage?group=provider&window=1h")
        self.assertEqual(code, 200, body)
        self.assertEqual((body["group"], body["window"]), ("provider", "1h"))
        # 旧口径（days）仍可用：面板首屏快照还在用
        code, body, _ = self.http_json(port, "/admin/usage?days=7")
        self.assertEqual(code, 200, body)
        self.assertIn("by_day", body)

    def test_93_usage_api_window_filter_matches_manual_sum(self):
        """T1.3③：1h / 24h / 7d 的窗口过滤正确，且与「同一窗口手工累加」逐项一致。"""
        now = time.time()
        rows = [
            _usage_row(now - 1800, caller="c-recent", route="r1", provider="p1", model="m1",
                       prompt=100, completion=50),                      # 30 分钟前
            _usage_row(now - 7200, caller="c-2h", route="r1", provider="p1", model="m1",
                       prompt=200, completion=100),                     # 2 小时前
            _usage_row(now - 3 * 86400, caller="c-3d", route="r2", provider="p2", model="m2",
                       prompt=300, completion=150, http=500),           # 3 天前（失败）
        ]
        self.write_usage(rows)
        relay = self.t1_3_relay()

        def manual(window_s: float, field: str) -> tuple[int, int]:
            hit = [r for r in rows if now - window_s <= llm_relay.UsageIndex._parse_ts(r["ts"])]
            return len(hit), sum(int(r["total_tokens"]) for r in hit)

        for window, secs in (("1h", 3600), ("24h", 86400), ("7d", 7 * 86400)):
            d = relay.admin_usage_group("caller", window)
            want_req, want_tok = manual(secs, "total_tokens")
            self.assertEqual(d["totals"]["requests"], want_req, f"{window}: {d}")
            self.assertEqual(d["totals"]["total_tokens"], want_tok, f"{window}: {d}")
        # 精确到分组：7d 里 3 天前那笔落在 c-3d，1h 里只有 c-recent
        self.assertEqual({g["name"] for g in relay.admin_usage_group("caller", "7d")["groups"]},
                         {"c-recent", "c-2h", "c-3d"})
        self.assertEqual({g["name"] for g in relay.admin_usage_group("caller", "24h")["groups"]},
                         {"c-recent", "c-2h"})
        self.assertEqual({g["name"] for g in relay.admin_usage_group("caller", "1h")["groups"]},
                         {"c-recent"})
        self.assertEqual(relay.admin_usage_group("caller", "7d")["totals"]["failed"], 1)

    def test_94_metrics_default_404_then_enabled_and_samples_match(self):
        """T1.3④：默认 /metrics=404；usage_log.metrics=true 后 200 且样本值与临时 usage 一致。"""
        now = time.time()
        self.write_usage([
            _usage_row(now - 60, caller="hindsight", provider="p1", model="m1", prompt=7, completion=3),
            _usage_row(now - 90, caller="hindsight", provider="p1", model="m1", prompt=5, completion=5),
            _usage_row(now - 120, caller="hermes-mac", provider="p2", model="m2",
                       http=502, prompt=11, completion=0),
        ])
        relay = self.t1_3_relay()
        port = self.start_http(relay)
        code, body, _ = self.http_json(port, "/metrics")
        self.assertEqual(code, 404, body)
        # 开启 metrics 后重载
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        cfg["usage_log"]["metrics"] = True
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        relay.reload(force=True)
        self.assertTrue(relay.metrics_enabled())
        status, raw, headers = self.http(port, "/metrics")
        self.assertEqual(status, 200, raw)
        self.assertIn("text/plain", headers.get("Content-Type", ""))
        text = raw.decode("utf-8")
        self.assertIn("llm_relay_", text)
        samples: dict[str, float] = {}
        for line in text.splitlines():
            if line and not line.startswith("#"):
                name, _, val = line.rpartition(" ")
                samples[name] = float(val)
        self.assertEqual(
            samples['llm_relay_requests_total{group="caller",name="hindsight"}'], 2.0, text)
        self.assertEqual(
            samples['llm_relay_requests_total{group="caller",name="hermes-mac"}'], 1.0, text)
        self.assertEqual(
            samples['llm_relay_requests_failed_total{group="caller",name="hermes-mac"}'], 1.0, text)
        self.assertEqual(
            samples['llm_relay_tokens_total{group="caller",name="hindsight",kind="prompt"}'], 12.0, text)
        self.assertEqual(
            samples['llm_relay_tokens_total{group="provider",name="p2",kind="total"}'], 11.0, text)
        self.assertIn("llm_relay_candidates_healthy", text)
        self.assertIn("llm_relay_providers_cooling", text)

    def test_95_metrics_and_admin_usage_reject_non_loopback(self):
        """T1.3⑤：非 loopback 访问 /metrics 与 /admin/usage → 403（沿用管理接口远程只读语义）。"""
        if LAN_IP.startswith("127."):
            self.skipTest("没有可用的非 loopback 地址")
        relay = self.t1_3_relay()
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        cfg["usage_log"]["metrics"] = True
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        relay.reload(force=True)
        port = self.start_http_all(relay)
        for path in ("/metrics", "/admin/usage?group=caller"):
            try:
                with OPENER.open(f"http://{LAN_IP}:{port}{path}", timeout=10) as r:
                    code, raw = r.status, r.read()
            except urllib.error.HTTPError as e:
                code, raw = e.code, e.read()
            self.assertEqual(code, 403, f"{path} 来自 {LAN_IP} 必须 403：{raw[:200]}")
        # 同一进程本机访问必须照样 200（证明 403 只针对非 loopback）
        code, body, _ = self.http_json(port, "/admin/usage?group=caller")
        self.assertEqual(code, 200, body)

    def test_96_admin_usage_group_does_not_rescan_usage_file(self):
        """T1.3 性能红线：/admin/usage 走内存索引，绝不每次请求全量扫 usage.jsonl。"""
        now = time.time()
        self.write_usage([_usage_row(now - 60, caller="hindsight")])
        relay = self.t1_3_relay()
        first = relay.admin_usage_group("caller", "24h")
        self.assertEqual(first["totals"]["requests"], 1)

        def _boom(*a, **kw):  # 老的全量扫描路径被调用就失败
            raise AssertionError("分组用量 API 又去全量扫 usage.jsonl 了")

        relay._usage_rows = _boom  # type: ignore[assignment]
        for _ in range(5):
            again = relay.admin_usage_group("caller", "24h")
            self.assertEqual(again["totals"]["requests"], 1)
        # 增量记账也在同一份缓存：新起一笔真实请求后立刻能看到
        relay.chat(self.ask("hi"))
        after = relay.admin_usage_group("route", "24h")
        self.assertEqual(after["totals"]["requests"], 2, after)

    # ==================================================== T1.5 config schema 校验
    def t1_5_relay(self, mutate=None, *, keys=None, providers=None) -> llm_relay.Relay:
        """以 write_config 的合法地基为准，按 mutate(cfg) 改坏后热重载，返回 Relay。"""
        self.write_config(providers if providers is not None else [self.mock_provider()])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        if mutate is not None:
            mutate(cfg)
        self.config_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        self.write_keys(keys if keys is not None else {"TEST_KEY_A": "k1-ok", "TEST_KEY_B": "k2-ok"})
        return llm_relay.Relay(self.config_path, self.keys_path, quiet=True)

    @staticmethod
    def t1_5_issues(relay) -> dict:
        return {i["path"]: i for i in relay.config_issues}

    def test_97_config_misspelled_field_reports_readable_error(self):
        """T1.5①：字段名拼错（顶层 + provider 级）→ 可读错误并忽略该字段，服务照常。"""
        def mutate(cfg):
            cfg["provders"] = []                            # 顶层：providers 拼错
            cfg["providers"][0]["max_concurency"] = 4       # provider 级：max_concurrency 拼错
        relay = self.t1_5_relay(mutate)
        got = self.t1_5_issues(relay)
        for path in ("config.provders", "providers[p1].max_concurency"):
            self.assertIn(path, got, f"{path} 应有可读校验错误：{relay.config_issues}")
            it = got[path]
            self.assertEqual(it["action"], "忽略该字段")
            self.assertIn("已知字段", it["expected"])
            self.assertIn("拼写", it["message"])
            self.assertTrue(it["actual"])
        self.assertNotIn("provders", relay.cfg, "未知顶层字段必须从工作配置里剔除")
        self.assertNotIn("max_concurency", relay.cfg["providers"][0])
        status, body, _ = relay.chat(self.ask())
        self.assertEqual(status, 200, body)

    def test_98_config_wrong_type_falls_back_and_disables_provider(self):
        """T1.5②：类型不对 → 可读错误 + 回退默认；坏 provider 被禁用，不抛裸 TypeError 栈。"""
        def mutate(cfg):
            p = cfg["providers"][0]
            p["rpm"] = "fast"
            p["enabled"] = "yes"
            p["max_concurrency"] = 0
            p["keys"] = "TEST_KEY_A"                        # 字符串不是数组 → 禁用该 provider
            cfg["concurrency"] = {"global": 0}
            cfg["usage_log"] = {"enabled": True, "path": str(self.tmp / "usage.jsonl"),
                                "max_mb": "big"}
        relay = self.t1_5_relay(mutate)
        got = self.t1_5_issues(relay)
        for path, expect in (("providers[p1].rpm", "number"),
                             ("providers[p1].enabled", "boolean"),
                             ("providers[p1].max_concurrency", ">=1"),
                             ("providers[p1].keys", "数组"),
                             ("concurrency.global", ">=1"),
                             ("usage_log.max_mb", "number")):
            self.assertIn(path, got, f"{path} 应有可读校验错误：{relay.config_issues}")
            self.assertIn(expect, got[path]["expected"], got[path])
        p = relay.cfg["providers"][0]
        self.assertFalse(p["enabled"], "坏 provider 必须被禁用而不是带病上岗")
        self.assertEqual(p["keys"], [])
        self.assertEqual(p["rpm"], 0)
        self.assertEqual(p["max_concurrency"], 1)
        self.assertEqual(relay.cfg["concurrency"]["global"], 8)
        self.assertEqual(relay.cfg["usage_log"]["max_mb"], 20)
        self.assertEqual(str(relay.cfg["usage_log"]["path"]), str(self.tmp / "usage.jsonl"),
                         "合法的 path 不许被回退掉")
        # 没有可用候选时给的是中继错误响应（429 冷却/禁用 或 502/503），而不是 Python 栈
        status, body, _ = relay.chat(self.ask())
        self.assertIn(status, (429, 502, 503), body)
        self.assertIn("relay_", json.dumps(body, ensure_ascii=False))

    def test_99_config_chain_to_unknown_provider_model(self):
        """T1.5③：routes[].chain 指向不存在的 provider/model → 可读错误 + 丢弃坏项、保留好项。"""
        def mutate(cfg):
            cfg["routes"]["broken"] = {"chain": ["ghost/model-x", "p1/mock-ok"]}
            cfg["routes"]["allbad"] = {"chain": ["ghost/model-x", "ghost/model-y"]}
            cfg["routes"]["notalist"] = {"chain": 7}
        relay = self.t1_5_relay(mutate)
        got = self.t1_5_issues(relay)
        self.assertEqual(got["routes.broken.chain"]["actual"], "ghost/model-x")
        self.assertIn("不存在", got["routes.broken.chain"]["message"])
        self.assertEqual(got["routes.broken.chain"]["action"], "丢弃该项")
        self.assertEqual(relay.cfg["routes"]["broken"]["chain"], ["p1/mock-ok"])
        self.assertEqual(got["routes.allbad.chain"]["action"], "回退 auto")
        self.assertEqual(relay.cfg["routes"]["allbad"]["chain"], "auto")
        self.assertEqual(got["routes.notalist.chain"]["action"], "回退 auto")
        self.assertEqual(relay.cfg["routes"]["notalist"]["chain"], "auto")
        self.assertTrue(relay.build_candidates(False, False), "合法候选必须还在")

    def test_100_config_allow_routes_to_unknown_route(self):
        """T1.5④：callers[].allow_routes 指向不存在的 route → 可读错误 + 丢弃坏项。"""
        def mutate(cfg):
            cfg["callers"] = {
                "c1": {"key": "ck1", "allow_routes": ["nope", "hindsight"]},
                "c2": {"key": "ck2", "allow_routes": ["nope", "ghost"]},
            }
        relay = self.t1_5_relay(mutate)
        got = self.t1_5_issues(relay)
        self.assertEqual(got["callers.c1.allow_routes"]["actual"], "nope")
        self.assertIn("不存在", got["callers.c1.allow_routes"]["message"])
        self.assertEqual(relay.cfg["callers"]["c1"]["allow_routes"], ["hindsight"])
        self.assertIn("hindsight", got["callers.c1.allow_routes"]["expected"])
        # 白名单项全坏时去掉白名单（避免把调用方彻底锁死），并额外给一条可读错误
        self.assertIn("锁死", got["callers.c2.allow_routes"]["action"])
        self.assertNotIn("allow_routes", relay.cfg["callers"]["c2"])
        self.assertEqual(relay.resolve_caller("Bearer ck1", True)[0], "c1")
        self.assertTrue(relay.check_caller("c1", "hindsight")[0])
        ok, code, _, _ = relay.check_caller("c1", "cheap")
        self.assertFalse(ok)
        self.assertEqual(code, 403)
        self.assertTrue(relay.check_caller("c2", "cheap")[0])

    def test_101_config_caller_missing_key_and_key_env(self):
        """T1.5⑤：caller 同时缺 key / key_env → 可读错误；条目保留但该身份认不出来（401）。"""
        def mutate(cfg):
            cfg["callers"] = {"ghost": {"rpm": 5, "daily_tokens": 100}}
        relay = self.t1_5_relay(mutate)
        got = self.t1_5_issues(relay)
        it = got["callers.ghost"]
        self.assertIn("key", it["expected"])
        self.assertIn("401", it["action"])
        self.assertIn("ghost", relay.cfg.get("callers") or {})
        self.assertEqual(relay._caller_key(relay.cfg["callers"]["ghost"]), "")
        name, code, _ = relay.resolve_caller("Bearer whatever", True)
        self.assertEqual((name, code), ("", 401))
        row = next(c for c in relay.admin_callers()["callers"] if c["name"] == "ghost")
        self.assertEqual(row["key_source"], "none")
        self.assertFalse(row["key_set"])

    def test_102_good_config_has_no_issues_and_junk_never_raises(self):
        """T1.5 附加：生产形状的合法配置零误报（/admin/config.issues 也一致）；垃圾输入不抛。"""
        def mutate(cfg):
            p = cfg["providers"][0]
            p["rpm_note"] = "免费额度说明"
            p["note"] = "演示 provider"
            p["models"][0]["note"] = "演示模型"
            cfg["key_state"] = {"p1": {"k1": {"disabled": False}}}
            cfg["local_fallback"] = {"enabled": True, "base_url": "http://127.0.0.1:9/v1",
                                     "model": "local-mlx", "window": "01:00-07:00",
                                     "health_check_path": "/models", "autostart": False}
            cfg["integrations"] = {"upstream": {"label": "上游", "health_url": "http://127.0.0.1:8988/health",
                                                "usage_alias": "hindsight", "env_prefix": "HINDSIGHT_"}}
            cfg["callers"] = {"hindsight": {"key": "relay-key", "rpm": 60, "daily_tokens": 2000000,
                                            "allow_routes": ["hindsight"], "note": "线上唯一调用方"}}
            cfg["routes"]["hindsight"]["policy"] = {"validate_json": True, "max_candidates": 3}
        relay = self.t1_5_relay(mutate)
        self.assertEqual(relay.config_issues, [], relay.config_issues)
        port = self.start_http(relay)
        code, body, _ = self.http_json(port, "/admin/config")
        self.assertEqual(code, 200, body)
        self.assertEqual(body["issues"], [])
        # 校验器对任何垃圾输入都不许抛异常
        for junk in ([], "x", 42, None):
            out, issues = llm_relay.validate_config(junk, {})
            self.assertEqual(out, {})
            self.assertTrue(issues)
            self.assertIn("顶层", issues[0]["message"])
        for junk, expect in (([], True), ("x", True), (None, False), (3.5, True)):
            out, issues = llm_relay.validate_config({"providers": junk, "routes": junk,
                                                     "callers": junk}, {})
            self.assertIsInstance(out, dict)
            self.assertEqual(bool(issues), expect, f"{junk!r}: {issues}")
            if expect:
                self.assertTrue(all(set(i) >= {"path", "expected", "actual", "action"}
                                    for i in issues), issues)
        # 合法配置热重载后 issues 必须清零（不残留上一次的坏记录）
        self.write_config([self.mock_provider()])
        relay.reload(force=True)
        self.assertEqual(relay.config_issues, [])

    # ============================================ T1.6 假源场景 / 多厂商路径 / 流式
    def scenario_mock(self, scenario: str, **kw) -> MockProvider:
        """起一个注入了指定场景的独立假源（与 setUp 的 self.mock 互不干扰）。"""
        mp = MockProvider(scenario=scenario, **kw).start()
        self.addCleanup(mp.stop)
        return mp

    def test_103_scenario_rate_limit_recorded_as_upstream_ratelimit(self):
        """T1.6①：--scenario rate_limit → 任何模型都 429；中继按「上游限流」记账。"""
        mp = self.scenario_mock("rate_limit")
        relay = self.make_relay([provider_cfg("p_rl", ["mock-ok"], ["TEST_KEY_A"], mp.base_url)])
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 429, body)
        self.assertEqual([a["kind"] for a in meta["attempts"]], ["ratelimit"], meta)
        self.assertEqual(llm_relay.usage_verdict(status, meta), "ratelimit")
        st = mp.stats()
        self.assertEqual(st["scenario"], "rate_limit")
        self.assertEqual(st["per_behavior"].get("429"), 1, st["per_behavior"])
        self.assertEqual([r["path"] for r in st["requests"]], ["/v1/chat/completions"])

    def test_104_scenario_timeout_switches_to_next_candidate(self):
        """T1.6②：--scenario timeout → 挂过单次超时，中继换下一个候选成功。"""
        hang = self.scenario_mock("timeout")
        good = self.scenario_mock("normal")
        relay = self.make_relay(
            [provider_cfg("p_hang", ["mock-ok", ("mock-ok", 2)], ["TEST_KEY_A"], hang.base_url),
             provider_cfg("p_good", ["mock-ok"], ["TEST_KEY_B"], good.base_url)],
            request={"per_attempt_timeout_s": 1, "min_attempt_timeout_s": 1, "total_budget_s": 30})
        t0 = time.time()
        status, body, meta = relay.chat(self.ask())
        self.assertEqual(status, 200, body)
        self.assertEqual(meta["provider"], "p_good")
        self.assertEqual(meta["attempts"][0]["kind"], "transport", meta)
        self.assertLess(time.time() - t0, 6, "超时后必须立刻换候选，不该真等挂满")
        self.assertEqual(good.stats()["per_model"].get("mock-ok"), 1)

    def test_105_scenario_empty_content_is_failure(self):
        """T1.6③：--scenario empty_content → content 空且无 tool_calls，判失败（502）。"""
        mp = self.scenario_mock("empty_content")
        relay = self.make_relay([provider_cfg("p_empty", ["mock-ok"], ["TEST_KEY_A"], mp.base_url)])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 502, body)
        self.assertEqual(meta["attempts"][-1]["normalize"], "empty_content", meta)
        self.assertEqual(llm_relay.usage_verdict(status, meta), "relay_unusable_content")

    def test_106_scenario_schema_echo_is_rejected_502(self):
        """T1.6④：--scenario schema_echo → 回显 schema 本体必须被 502 拒（P0 坑回归）。"""
        mp = self.scenario_mock("schema_echo")
        relay = self.make_relay([provider_cfg("p_echo", ["mock-ok"], ["TEST_KEY_A"], mp.base_url)])
        status, body, meta = relay.chat(self.ask("抽取事实", response_format=JSON_REQ))
        self.assertEqual(status, 502, body)
        self.assertEqual(meta["attempts"][-1]["normalize"], "schema_echo", meta)
        self.assertEqual(llm_relay.usage_verdict(status, meta), "schema_echo")

    def test_107_custom_base_path_sse_and_tools(self):
        """T1.6⑤：自定义 base path 都认；SSE 真分块；tools 调用可透传。"""
        mp = self.scenario_mock("normal")
        for prefix in ("openai/v1", "proxy/llm"):
            relay = self.make_relay([provider_cfg(f"p_{prefix.replace('/', '_')}", ["mock-ok"],
                                                  ["TEST_KEY_A"], mp.base_url_at(prefix))])
            status, body, _ = relay.chat(self.ask())
            self.assertEqual(status, 200, body)
            self.assertIn(f"/{prefix}/chat/completions", mp.stats()["per_path"], mp.stats()["per_path"])
        # 流式：假源按 data: 分多块，聚合后的 content 与整包一致
        payload = self.ask("流式", stream=True)
        payload["model"] = "mock-stream"
        req = urllib.request.Request(mp.base_url + "/chat/completions",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer k1-ok"}, method="POST")
        with OPENER.open(req, timeout=15) as r:
            sse = r.read().decode("utf-8", "replace")
        self.assertGreaterEqual(sse.count("data: "), 4, sse)
        self.assertTrue(sse.rstrip().endswith("data: [DONE]"), sse[-60:])
        text = "".join(json.loads(line[6:])["choices"][0]["delta"].get("content", "")
                       for line in sse.splitlines()
                       if line.startswith("data: ") and line[6:] != "[DONE]")
        self.assertIn("我从 Mac 上跑 Hindsight", text)
        self.assertGreaterEqual(mp.stats()["sse_events"], 4, mp.stats()["sse_events"])
        # tools：mock-tools 的 tool_calls 必须原样透传
        relay = self.make_relay([provider_cfg("p_tools", ["mock-tools"], ["TEST_KEY_A"], mp.base_url)])
        status, body, _ = relay.chat(self.ask("北京现在几点？", tools=TOOLS, tool_choice="auto"))
        self.assertEqual(status, 200, body)
        calls = body["choices"][0]["message"].get("tool_calls")
        self.assertTrue(calls and calls[0]["function"]["name"] == "get_time", body)
        # 场景可运行时切换（POST /__scenario 的进程内等价物）
        mp.set_scenario("rate_limit")
        self.assertEqual(mp.stats()["scenario"], "rate_limit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
