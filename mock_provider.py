#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mock_provider.py —— 本地假源（免配额测试用）。

按「模型 id / key 里的标记 / 预置脚本」返回各种真实上游的刁钻形状，供 test_relay.py 驱动：
  正常 200 / 200 但 content 空 + reasoning 有值 / 200 但 JSON 坏 / 200 但思维链漏进 content /
  429（带不带 reset 头）/ 401 / 402 / 500 / 挂起（默认 90s 制造超时）/
  400 temperature 非法 / 400 response_format 不支持 / tools 返回 tool_calls / 无 usage。

统计：总调用数、按模型、按 key（**只存 sha256 前 8 位指纹，绝不存明文**）、观测到的最大并发数、
最近的请求摘要（是否带 response_format / temperature / 用哪个 max_tokens 参数名 / tools 数量）。

用法：
    ~/hindsight-mac-env/bin/python mock_provider.py [--port 9199] [--hang-s 90]
    curl -s --noproxy '*' http://127.0.0.1:9199/__stats
    curl -s --noproxy '*' -X POST http://127.0.0.1:9199/__script -d '["ok","429","ok"]'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 模型 id → 行为（测试里把 config 的 model id 直接写成这些名字）
MODEL_BEHAVIORS = {
    "mock-ok": "ok",
    "mock-slow": "slow",
    "mock-inspect": "inspect",
    "mock-reasoning": "reasoning",
    "mock-reasoning-ns": "reasoning_ns",
    "mock-badjson": "badjson",
    "mock-json": "json",
    "mock-json-messy": "jsonmessy",
    "mock-cot": "cot",
    "mock-empty": "emptymsg",
    "mock-no-usage": "nousage",
    "mock-tools": "tools",
    "mock-stream": "stream",
    "mock-429": "429",
    "mock-429-reset": "429reset",
    "mock-429-account": "429account",
    "mock-401": "401",
    "mock-402": "402",
    "mock-500": "500",
    "mock-hang": "hang",
    "mock-temp400": "temp400",
    "mock-rf400": "rf400",
    "mock-model400": "model400",
    # P0（schema 一致性）用的形状：schema 回显 / 缺 required / 顶层类型不符 / 多余字段 /
    # 先出现不合 schema 的合法 JSON 再出现合格块 / enum 不匹配的对照片
    "mock-schema-echo": "schemaecho",
    "mock-schema-props-echo": "schemapropsecho",
    "mock-schema-missing": "schemamissing",
    "mock-schema-array": "schemaarray",
    "mock-schema-extra": "schemaextra",
    "mock-schema-blocks": "schemablocks",
    "mock-schema-en": "schemaen",
}

# key 明文里含这些标记 → 按标记行事（测试里用 "k1-busy" 这类无害假值）
KEY_MARKERS = [
    ("dead", "401"),
    ("busy", "429reset"),
    ("quota", "402"),
    ("temp", "temp400"),
    ("boom", "500"),
]

# T1.6：可注入场景（CLI --scenario / MockProvider(scenario=...)）—— 与模型名无关，整个假源统一表现。
#   normal       不强制，按模型 / key 标记决定（默认）
#   rate_limit   上游一律 429
#   timeout      一律挂住，超过中继单次超时
#   empty_content 一律 200 但 content=""
#   schema_echo  一律回显请求里的 JSON Schema 本体（复现 P0 修过的坑）
#   slow         一律慢回复但在总预算内
SCENARIOS: dict[str, str | None] = {
    "normal": None,
    "rate_limit": "429",
    "timeout": "hang",
    "empty_content": "emptymsg",
    "schema_echo": "schemaecho",
    "slow": "slow",
}

SCHEMA_JSON = '{"facts": ["我从 Mac 上跑 Hindsight"], "language": "zh"}'
MESSY_JSON = '{\n{ "facts": ["我从 Mac 上跑 Hindsight"], "language": "zh" }'
# 先出现「合法 JSON 但不符合 schema」的块，后出现合格块（R3 挑块的回归用）
BLOCKS_TEXT = ('先看这个占位：{"type":"object"} 然后才是结果：'
               '{"facts": ["我从 Mac 上跑 Hindsight"], "language": "zh"}')
COT_TEXT = (
    "Here's a thinking process:\n\n1. 先理解用户想要什么\n2. 再决定格式\n\n"
    "抱歉，我无法按要求完成。"
)


def _fp(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


class MockState:
    """假源的可观测状态（测试进程内可直接读，也可通过 /__stats 读）。"""

    def __init__(self, hang_s: float = 90.0, scenario: str | None = None) -> None:
        self.lock = threading.Lock()
        self.hang_s = hang_s
        # T1.6：注入场景（None / "normal" = 不强制，按模型与 key 标记决定）
        self.scenario: str | None = None if scenario in (None, "normal") else SCENARIOS[scenario]
        self.scenario_name = "normal" if self.scenario is None else scenario
        self.calls = 0
        self.per_model: dict[str, int] = {}
        self.per_key: dict[str, int] = {}
        self.per_behavior: dict[str, int] = {}
        self.per_path: dict[str, int] = {}
        self.sse_events = 0
        self.active = 0
        self.max_concurrency = 0
        self.requests: list[dict] = []
        self.script: list[str] = []
        self.started = time.time()

    def reset(self) -> None:
        with self.lock:
            self.calls = 0
            self.per_model = {}
            self.per_key = {}
            self.per_behavior = {}
            self.per_path = {}
            self.sse_events = 0
            self.active = 0
            self.max_concurrency = 0
            self.requests = []
            self.script = []

    def enter(self, model: str, key_fp: str, path: str = "") -> None:
        with self.lock:
            self.active += 1
            self.max_concurrency = max(self.max_concurrency, self.active)
            self.calls += 1
            self.per_model[model] = self.per_model.get(model, 0) + 1
            self.per_key[key_fp] = self.per_key.get(key_fp, 0) + 1
            if path:
                self.per_path[path] = self.per_path.get(path, 0) + 1

    def leave(self) -> None:
        with self.lock:
            self.active -= 1

    def record(self, row: dict) -> None:
        with self.lock:
            self.requests.append(row)
            if len(self.requests) > 500:
                del self.requests[: len(self.requests) - 500]

    def bump_behavior(self, name: str) -> None:
        with self.lock:
            self.per_behavior[name] = self.per_behavior.get(name, 0) + 1

    def bump_sse(self, events: int) -> None:
        with self.lock:
            self.sse_events += int(events)

    def set_scenario(self, scenario: str) -> None:
        if scenario not in SCENARIOS:
            raise ValueError(f"未知场景 {scenario!r}，可选：{', '.join(SCENARIOS)}")
        with self.lock:
            self.scenario_name = scenario
            self.scenario = None if scenario == "normal" else SCENARIOS[scenario]

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "calls": self.calls,
                "scenario": self.scenario_name,
                "per_model": dict(self.per_model),
                "per_key_fp": dict(self.per_key),
                "per_behavior": dict(self.per_behavior),
                "per_path": dict(self.per_path),
                "sse_events": self.sse_events,
                "max_concurrency": self.max_concurrency,
                "active": self.active,
                "requests": list(self.requests),
            }


def decide(state: MockState, model: str, key_value: str) -> str:
    """决定这次请求的行为：预置脚本 > 注入场景 > key 标记 > 模型名 > 默认正常。"""
    with state.lock:
        if state.script:
            return state.script.pop(0)
        forced = state.scenario
    if forced is not None:
        return forced
    for marker, behavior in KEY_MARKERS:
        if marker in key_value:
            return behavior
    if model in MODEL_BEHAVIORS:
        return MODEL_BEHAVIORS[model]
    return "ok"


def _summarize(payload: dict, auth: str, path: str = "") -> dict:
    msgs = payload.get("messages") or []
    last_user = ""
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                last_user = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)[:400]
                break
    rf = payload.get("response_format")
    return {
        "ts": time.time(),
        "path": path,
        "stream": payload.get("stream") is True,
        "model": payload.get("model"),
        "key_fp": _fp(auth) if auth else "",
        "has_response_format": rf is not None,
        "response_format_type": (rf or {}).get("type") if isinstance(rf, dict) else None,
        "temperature_present": "temperature" in payload,
        "temperature": payload.get("temperature"),
        "max_tokens_param": "max_tokens" if "max_tokens" in payload else (
            "max_completion_tokens" if "max_completion_tokens" in payload else None
        ),
        "max_tokens_value": payload.get("max_tokens", payload.get("max_completion_tokens")),
        "tools_count": len(payload.get("tools") or []),
        "has_tool_choice": "tool_choice" in payload,
        "top_level_keys": sorted(k for k in payload.keys()),
        "last_user": last_user[:600],
    }


def _usage() -> dict:
    return {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def _request_schema(payload: dict) -> object:
    """从请求体里取 response_format.json_schema.schema（形状不对就回 None）。"""
    rf = payload.get("response_format")
    if not isinstance(rf, dict):
        return None
    js = rf.get("json_schema")
    if not isinstance(js, dict):
        return None
    return js.get("schema")


def _completion(model: str, message: dict, finish: str = "stop", usage: dict | None = None) -> dict:
    body = {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


# P0.7 §0 真机抓到的 properties 片段回显（假源的真实缺省形状；schema 不给 properties 时用它兜底）
PROPS_ECHO_DEFAULT = {"facts": {"type": "array", "items": {"type": "string"}}, "language": {"type": "string"}}


def _props_fragment(schema: object) -> str:
    """把请求 schema 的 properties 原样回显成「类型说明对象」片段（就是 §0 那种坏 content）。"""
    props = schema.get("properties") if isinstance(schema, dict) else None
    frag = props if isinstance(props, dict) and props else PROPS_ECHO_DEFAULT
    return json.dumps(frag, ensure_ascii=False, separators=(",", ":"))


def build_response(behavior: str, model: str, payload: dict) -> tuple[int, dict, dict]:
    """返回 (http_status, body, extra_headers)。"""
    if behavior == "ok":
        return 200, _completion(model, {"role": "assistant", "content": "在的"}, usage=_usage()), {}
    if behavior == "slow":
        time.sleep(0.4)
        return 200, _completion(model, {"role": "assistant", "content": "慢回复"}, usage=_usage()), {}
    if behavior == "stream":
        # 流式（SSE data: 分块）：content 长一点，便于看到多块 delta
        text = json.dumps({"facts": ["我从 Mac 上跑 Hindsight"], "language": "zh"}, ensure_ascii=False)
        return 200, _completion(model, {"role": "assistant", "content": text}, usage=_usage()), {}
    if behavior == "inspect":
        summary = {k: v for k, v in payload.items() if k != "messages"}
        content = json.dumps({"received": summary, "last_user": _last_user(payload)}, ensure_ascii=False)
        return 200, _completion(model, {"role": "assistant", "content": content}, usage=_usage()), {}
    if behavior == "reasoning":
        return 200, _completion(model, {"role": "assistant", "content": None, "reasoning_content": SCHEMA_JSON},
                                usage=_usage()), {}
    if behavior == "reasoning_ns":
        return 200, _completion(model, {"role": "assistant", "reasoning": SCHEMA_JSON}, usage=_usage()), {}
    if behavior == "badjson":
        return 200, _completion(model, {"role": "assistant", "content": "抱歉，我没办法输出 JSON。"},
                                usage=_usage()), {}
    if behavior == "json":
        return 200, _completion(model, {"role": "assistant", "content": SCHEMA_JSON}, usage=_usage()), {}
    if behavior == "jsonmessy":
        return 200, _completion(model, {"role": "assistant", "content": MESSY_JSON}, usage=_usage()), {}
    if behavior == "cot":
        return 200, _completion(model, {"role": "assistant", "content": COT_TEXT}, usage=_usage()), {}
    if behavior == "emptymsg":
        return 200, _completion(model, {"role": "assistant", "content": ""}, usage=_usage()), {}
    if behavior == "nousage":
        return 200, _completion(model, {"role": "assistant", "content": "在的"}), {}
    if behavior == "tools":
        msg = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_mock_1",
                    "type": "function",
                    "function": {"name": "get_time", "arguments": '{"city": "北京"}'},
                }
            ],
        }
        return 200, _completion(model, msg, finish="tool_calls", usage=_usage()), {}
    if behavior == "429":
        return 429, {"error": {"message": "inference exceeds tpm/rpm limit", "type": "rate_limit_error",
                               "code": "429001"}}, {}
    if behavior == "429reset":
        return 429, {"error": {"message": "rate limit, retry later", "type": "rate_limit_error",
                               "code": "429001"}}, {"Retry-After": "2", "x-ratelimit-reset-requests": "2s"}
    if behavior == "429account":
        # P0.6/R2 的对照形状：账号级/套餐级 429（响应体明确指向账户额度与套餐）→ 整把 key 冷却
        return 429, {"error": {"message": "account quota exceeded：当前账户额度与套餐额度均已用尽",
                               "type": "insufficient_quota",
                               "code": "account_quota_exceeded"}}, {}
    if behavior == "401":
        return 401, {"error": {"message": "invalid api key", "type": "authentication_error"}}, {}
    if behavior == "402":
        return 402, {"error": {"message": "余额不足，额度已耗尽 (insufficient_quota)", "type": "insufficient_quota"}}, {}
    if behavior == "500":
        return 500, {"error": {"message": "upstream internal error", "type": "server_error"}}, {}
    if behavior == "hang":
        return 0, {"__hang__": True}, {}
    if behavior == "temp400":
        return 400, {"error": {"message": "field Temperature invalid", "type": "invalid_request_error"}}, {}
    if behavior == "rf400":
        return 400, {"error": {"message": "response_format json_schema is not supported",
                               "type": "invalid_request_error"}}, {}
    if behavior == "model400":
        return 400, {"error": {"message": "model not found or unsupported", "type": "invalid_request_error"}}, {}
    # ---------- P0：schema 一致性相关的形状 ----------
    if behavior == "schemaecho":
        # 思考模式下预算被 reasoning 烧光：content 里回显 JSON Schema 本体，finish_reason=length
        echo = json.dumps(_request_schema(payload) or {}, ensure_ascii=False, separators=(",", ":"))
        msg = {"role": "assistant", "content": echo,
               "reasoning_content": "我们需要回答用户中文请求：从这句话抽取事实……需要符合给定 schema："}
        return 200, _completion(model, msg, finish="length", usage=_usage()), {}
    if behavior == "schemapropsecho":
        # P0.7 §0 真机抓到的形状：content 是 schema 的 properties 片段回显（键名 = properties 键名），
        # 看起来「像一次正常成功」（finish_reason=stop、无 reasoning），但 facts 不是数组而是类型说明对象。
        # 它不含 type/properties/required 这些关键字 → 旧的「键集 ⊆ schema 关键字集」判据抓不到。
        echo = _props_fragment(_request_schema(payload))
        return 200, _completion(model, {"role": "assistant", "content": echo}, usage=_usage()), {}
    if behavior == "schemamissing":
        # 缺 required 字段 facts
        return 200, _completion(model, {"role": "assistant", "content": '{"language": "zh"}'},
                                usage=_usage()), {}
    if behavior == "schemaarray":
        # 顶层类型不符（schema 要 object，这里给 array）
        return 200, _completion(model, {"role": "assistant", "content": '["facts"]'},
                                usage=_usage()), {}
    if behavior == "schemaextra":
        # additionalProperties:false 下多出一个未声明字段
        return 200, _completion(model, {"role": "assistant",
                                        "content": '{"facts": ["我从 Mac 上跑 Hindsight"], '
                                                   '"language": "zh", "extra": 1}'}, usage=_usage()), {}
    if behavior == "schemablocks":
        # 前面先有一个合 schema 关键字形状的合法 JSON 块，后面才是真正合格的块
        return 200, _completion(model, {"role": "assistant", "content": BLOCKS_TEXT}, usage=_usage()), {}
    if behavior == "schemaen":
        # language 落在 enum 里的对照结果
        return 200, _completion(model, {"role": "assistant",
                                        "content": '{"facts": ["我从 Mac 上跑 Hindsight"], "language": "en"}'},
                                usage=_usage()), {}
    return 200, _completion(model, {"role": "assistant", "content": "在的"}, usage=_usage()), {}


def _last_user(payload: dict) -> str:
    msgs = payload.get("messages") or []
    for m in reversed(msgs if isinstance(msgs, list) else []):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return ""


class _Handler(BaseHTTPRequestHandler):
    server_version = "mock-provider/1.0"
    protocol_version = "HTTP/1.1"

    # ---------- 工具 ----------
    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send_json(self, status: int, obj, extra: dict | None = None) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, fmt, *args):  # 静音，避免刷屏
        return

    # ---------- 路由 ----------
    @staticmethod
    def _route(path: str) -> str:
        """多厂商路径：`/chat/completions` 与 `/models` 无论挂在什么前缀下都认（/v1、自定义 base path）。"""
        p = path.rstrip("/") or "/"
        if p.endswith("/chat/completions") or p == "/chat/completions":
            return "chat"
        if p.endswith("/models") or p == "/models":
            return "models"
        return p

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        route = self._route(path)
        if route == "models":
            self._send_json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in MODEL_BEHAVIORS]})
        elif path == "/__stats":
            self._send_json(200, self.server.state.snapshot())
        elif path == "/health":
            self._send_json(200, {"status": "ok", "mock": True,
                                  "scenario": self.server.state.scenario_name})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        raw = self._read_body()
        if path == "/__reset":
            self.server.state.reset()
            self._send_json(200, {"ok": True})
            return
        if path == "/__scenario":
            try:
                name = str((json.loads(raw or b"{}") or {}).get("scenario") or "normal")
                self.server.state.set_scenario(name)
            except Exception as e:  # noqa: BLE001
                self._send_json(400, {"error": f"bad scenario: {e}"})
                return
            self._send_json(200, {"scenario": self.server.state.scenario_name})
            return
        if path == "/__script":
            try:
                script = json.loads(raw or b"[]")
                assert isinstance(script, list)
            except Exception as e:  # noqa: BLE001
                self._send_json(400, {"error": f"bad script: {e}"})
                return
            with self.server.state.lock:
                self.server.state.script = [str(x) for x in script]
            self._send_json(200, {"script": script})
            return
        if self._route(path) != "chat":
            self._send_json(404, {"error": "not found"})
            return
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError:
            self._send_json(400, {"error": {"message": "invalid json body"}})
            return

        auth = self.headers.get("Authorization") or ""
        key_value = auth[7:] if auth.lower().startswith("bearer ") else ""
        model = str(payload.get("model") or "")
        behavior = decide(self.server.state, model, key_value)

        state = self.server.state
        state.enter(model, _fp(key_value), path)
        try:
            state.bump_behavior(behavior)
            state.record(_summarize(payload, key_value, path))
            if behavior == "hang":
                time.sleep(self.server.state.hang_s)  # 制造超时
                return
            status, body, extra = build_response(behavior, model, payload)
            if status == 200 and (payload.get("stream") is True or behavior == "stream"):
                self._send_sse(body)
                return
            self._send_json(status, body, extra)
        finally:
            state.leave()

    def _send_sse(self, body: dict) -> None:
        """SSE 分块：role 块 → 若干 content delta 块 →（有 tool_calls 则分块）→ finish 块 → [DONE]。

        多厂商路径都能用；中继聚合后必须得到与整包一致的 content。
        """
        choice0 = (body.get("choices") or [{}])[0]
        message = choice0.get("message") or {}
        text = message.get("content") or ""
        model = body.get("model")
        base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model}
        chunks: list[dict] = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}]
        # 每块最多 12 个字符，保证「多块 data:」可观测
        for i in range(0, len(text), 12):
            chunks.append({**base, "choices": [
                {"index": 0, "delta": {"content": text[i:i + 12]}, "finish_reason": None}]})
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            chunks.append({**base, "choices": [{"index": 0, "finish_reason": None, "delta": {"tool_calls": [
                {"index": 0, "id": tc.get("id"), "type": "function",
                 "function": {"name": fn.get("name"), "arguments": fn.get("arguments")}}]}}]})
        chunks.append({**base, "choices": [
            {"index": 0, "delta": {}, "finish_reason": choice0.get("finish_reason") or "stop"}]})
        buf = "".join("data: " + json.dumps(c, ensure_ascii=False) + "\n\n" for c in chunks)
        buf += "data: [DONE]\n\n"
        raw = buf.encode("utf-8")
        self.server.state.bump_sse(len(chunks))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MockProvider:
    """进程内可用的假源（测试直接 new + start，无需 curl）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0, hang_s: float = 90.0,
                 scenario: str | None = None) -> None:
        if scenario is not None and scenario not in SCENARIOS:
            raise ValueError(f"未知场景 {scenario!r}，可选：{', '.join(SCENARIOS)}")
        self.state = MockState(hang_s=hang_s, scenario=scenario)
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.state = self.state  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}/v1"
        self._thread: threading.Thread | None = None

    def base_url_at(self, prefix: str) -> str:
        """自定义 base path（多厂商形状）：例如 prefix="/openai/v1" 或 "/proxy/llm"。"""
        return f"http://127.0.0.1:{self.port}/{prefix.strip('/')}"

    def start(self) -> "MockProvider":
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def stats(self) -> dict:
        return self.state.snapshot()

    def script(self, behaviors: list[str]) -> None:
        with self.state.lock:
            self.state.script = list(behaviors)

    def set_scenario(self, name: str) -> None:
        """运行时切换注入场景（等价 POST /__scenario），便于同一假源多场景测试。"""
        self.state.set_scenario(name)


def main() -> int:
    ap = argparse.ArgumentParser(description="llm-relay 假上游（免配额测试用）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9199)
    ap.add_argument("--hang-s", type=float, default=90.0, help="mock-hang 模型挂起多少秒（默认 90，制造超时）")
    ap.add_argument("--scenario", choices=sorted(SCENARIOS), default="normal",
                    help="注入场景：normal/rate_limit/timeout/empty_content/schema_echo/slow（默认 normal）")
    args = ap.parse_args()
    mp = MockProvider(args.host, args.port, hang_s=args.hang_s, scenario=args.scenario).start()
    print(f"mock 上游已启动: {mp.base_url}  (scenario={args.scenario}, hang={args.hang_s}s)", flush=True)
    print(f"  多厂商路径：{mp.base_url}/chat/completions、{mp.base_url_at('openai/v1')}/chat/completions、"
          f"{mp.base_url_at('proxy/llm')}/chat/completions 都认；/models 同理", flush=True)
    print(f"  统计/注入：GET /__stats  POST /__script  POST /__scenario {{\"scenario\": \"rate_limit\"}}", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        mp.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
