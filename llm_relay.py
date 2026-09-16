#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm_relay.py —— 零依赖单文件 · OpenAI 兼容 · 多厂商免费额度治理中继（纯标准库）。

OpenAI 兼容客户端 ──(POST /v1/chat/completions, base_url 指向本进程)──> 本进程 :9110
    ├─ Provider A（key 池轮换）
    ├─ Provider B（免费档，备胎）
    ├─ Provider C（key 未到时 enabled=false + missing_key）
    └─ 本地兜底（仅窗口内 + 健康检查通过才进候选，永远排最后，不自动拉起）

职责：key 池轮换、熔断冷却（指数退避）、并发闸 + RPM 令牌桶、响应形状归一化
     （reasoning→content 提升、usage 补零、JSON 校验 + 配平提取、json_schema 降级）、
     按能力路由、用量与健康统计、config.json / keys.env 热重载（SIGHUP 亦可）。

硬性卫生：日志/状态页里**永不出现 key 明文**，只出现 key_index 与 sha256 前 8 位指纹；
          访问回环与上游都绕系统代理（本机 ClashX 会把 localhost 变 502）。

用法（生产环境由 launchd 拉起）：
    python3 llm_relay.py [--config config.json] [--keys keys.env]
    python3 llm_relay.py --init         # 用 config.example.json 生成一份本地配置（已存在则拒绝覆盖）
    kill -HUP <pid>       # 强制热重载
    ... --check           # 只做配置体检并打印候选，不起服务
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "config.json"
DEFAULT_KEYS = HERE / "keys.env"
CONFIG_EXAMPLE = HERE / "config.example.json"

# 常规浏览器 UA：Zen 的 Cloudflare 见到 python-urllib 默认 UA 直接 403 (error code: 1010)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# 一律绕代理：ClashX(127.0.0.1:7890) 会把回环请求变成 502
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 单次尝试的最小超时（秒）：预算不够时也不会缩到比它还小，除非预算真的见底
MIN_ATTEMPT_TIMEOUT_S = 8.0

# ---- P0.5（R1）：单次超时预算 ----------------------------------------------------
# 思考模型的单次超时硬下限。真机量测（2026-09-14）：Hindsight 真请求形状
# （system 7166 字符 + 严格 schema 3239 字符 + max_completion_tokens=64000）上游 200 用 25.3s，
# 其中 reasoning 1423 tokens —— 25s 的超时正好卡在这个请求的边界上，上游一抖必超时。
# 所以思考模型（caps.reasoning_only=true，或该模型历史响应带过 reasoning_content/reasoning）
# 的单次超时不被 clamp 压到 45s 以下（仍受剩余总预算约束）。
REASONING_MIN_ATTEMPT_TIMEOUT_S = 45.0

# ---- P0.5（R2/R3）：provider 冷却分级封顶 + 429 错峰 -----------------------------
# 单次 transport/server 类失败时 provider 级冷却的软上限；只有「同一 provider 在
# 短窗口内连续 ≥3 次失败」才允许抬到硬上限。事故里一次 25s transport 超时被放大器推到 900s，
# 把主力 provider 关了 15 分钟 —— 单次 transport 超时永远不该直接产出 900s 冷却。
DEFAULT_PROVIDER_COOLDOWN_CAP_S = 120.0
DEFAULT_PROVIDER_COOLDOWN_HARD_CAP_S = 300.0
# 「连续失败」的观察窗口（秒）与抬到硬上限所需的失败次数
DEFAULT_PROVIDER_FAILURE_WINDOW_S = 300.0
DEFAULT_PROVIDER_ESCALATE_AFTER = 3

_KEY_PAT = re.compile(r"(sk-|nvapi-)[A-Za-z0-9_\-]{4,}")
_QUOTA_WORDS = (
    "insufficient_quota", "quota", "exceeded your current quota", "balance", "credit",
    "free usage limit", "freeusagelimiterror", "unavailable for free", "额度", "欠费", "余额不足",
)
_RF_WORDS = ("response_format", "json_schema", "structured output", "structured_output", "grammar")
_MODEL_WORDS = ("not found", "unsupported", "does not exist", "not exist", "unknown model",
                "invalid model", "no such model", "model_not_found")


def redact(text: object) -> str:
    """兜底脱敏：任何要落日志的字符串都过一遍。"""
    return _KEY_PAT.sub(r"\1***", str(text))


def sha8(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def file_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def deep_merge_patch(base: dict, patch: dict, _prefix: str = "") -> list[str]:
    """把 patch 里出现的键合并进 base，**只动这些键**，返回被改动的键路径列表。

    ⚠️ 安全红线（TASK-DASHBOARD §9.2）：任何 config.json 写回都必须
    「读磁盘最新内容 → 只应用 patch 里的键 → 原子写回」，**绝不允许**把内存里的整份
    config 对象 dump 回去 —— 那会静默冲掉按实测定下来的 chain 顺序与超时/冷却/rpm 参数。
    所以这里只做「键级浅合并 + dict 递归下钻」，patch 没提到的键一个都不碰。
    """
    changed: list[str] = []
    for k, v in (patch or {}).items():
        path = f"{_prefix}{k}"
        cur = base.get(k)
        if isinstance(v, dict) and isinstance(cur, dict):
            changed.extend(deep_merge_patch(cur, v, f"{path}."))
        elif cur != v:
            base[k] = v
            changed.append(path)
    return changed


def atomic_write_json(path: Path, data: dict, *, mode: int | None = None) -> str:
    """先备份成 <file>.bak.<ts>，再 tmp + os.replace 原子写回。返回备份文件名。"""
    backup = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
    if path.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
    return backup.name


def _default_exit(code: int) -> None:  # pragma: no cover —— 真机路径，测试一律注入假回调
    """默认退出方式：进程自己 os._exit(0)，plist 的 KeepAlive 会在 1–2 秒内拉起。

    刻意**不**去 shell 调 launchctl：那会引入权限/路径依赖，还可能误伤别的服务。
    """
    os._exit(int(code))

def parse_keys_text(text: str) -> dict[str, str]:
    """解析 keys.env 文本的 export NAME="value"。只返回 dict，绝不打印值。"""
    keys: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("export ") or "=" not in line:
            continue
        name, val = line[len("export "):].split("=", 1)
        keys[name.strip()] = val.strip().strip('"').strip("'")
    return keys


def load_keys(path: Path) -> dict[str, str]:
    """读 keys.env 并解析（文件不存在/读不到就是空池）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    return parse_keys_text(text)


# ---- keys.env 读写（TASK-ADMIN-RESTART-KEY §B3）-----------------------------------
# 要求：允许「末尾有/没有换行、空行、# 注释行、值带/不带引号」四种形态；
#       只**原地**替换/删除目标行，其它行（含注释、空行、缩进、行尾换行）逐字节不变。
# 实现：splitlines(keepends=True) 保留每行原样；只对命中的那一行做行内值替换/整行删除。
def key_line_re(name: str) -> re.Pattern:
    """匹配 `export NAME=`（允许 `export NAME = "..."` 这种带空格的写法）。

    分组：pre=行首到 `=` 为止（含空白）、q=引号（可空）、val=值、post=行尾空白与行内注释。
    """
    return re.compile(
        r"^(?P<pre>\s*export\s+" + re.escape(name) + r"\s*=\s*)(?P<q>[\"']?)"
        r"(?P<val>.*?)(?P=q)(?P<post>\s*(?:#.*)?)$"
    )


def split_key_lines(text: str) -> list[str]:
    """按行切开但保留行尾换行，保证未命中的行逐字节可还原。"""
    return text.splitlines(keepends=True)


def key_line_index(lines: list[str], name: str) -> int | None:
    """返回 `export NAME=` 这一行的下标（找不到返回 None）。"""
    rx = key_line_re(name)
    for i, line in enumerate(lines):
        if rx.match(line.rstrip("\n")):
            return i
    return None


def key_file_names(text: str) -> list[str]:
    """按文件顺序列出 keys.env 里的变量名（只返回值名，不返回值）。"""
    out: list[str] = []
    for line in split_key_lines(text):
        m = re.match(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            out.append(m.group(1))
    return out


def replace_key_line(text: str, name: str, new_value: str) -> tuple[str, str]:
    """把 name 那一行的值换成 new_value，返回 (新文本, 旧值)。找不到行则抛 KeyError。"""
    lines = split_key_lines(text)
    idx = key_line_index(lines, name)
    if idx is None:
        raise KeyError(name)
    line = lines[idx]
    tail = "\n" if line.endswith("\n") else ""
    body = line[:-1] if tail else line
    m = key_line_re(name).match(body)
    if m is None:  # 理论上不可达（key_line_index 用的是同一个正则）
        raise KeyError(name)
    lines[idx] = f"{m.group('pre')}{m.group('q')}{new_value}{m.group('q')}{m.group('post')}{tail}"
    return "".join(lines), m.group("val")


def remove_key_line(text: str, name: str) -> tuple[str, str]:
    """整行删除 name 的定义（含它自己的换行），返回 (新文本, 旧值)。找不到行则抛 KeyError。"""
    lines = split_key_lines(text)
    idx = key_line_index(lines, name)
    if idx is None:
        raise KeyError(name)
    m = key_line_re(name).match(lines[idx].rstrip("\n"))
    old = m.group("val") if m else ""
    lines.pop(idx)
    return "".join(lines), old


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> str:
    """先备份成 <file>.bak.<ts>，再 tmp + os.replace 原子写回。返回备份文件名。"""
    backup = path.with_name(path.name + ".bak." + time.strftime("%Y%m%d%H%M%S"))
    if path.exists():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return backup.name


def in_window(window: str, ts: float | None = None) -> bool:
    """"01:00-07:00" 形式的本地窗口判断（支持跨午夜，如 23:00-06:00）。"""
    try:
        a, b = window.split("-")
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except Exception:  # noqa: BLE001
        return False
    now = time.localtime(ts if ts is not None else time.time())
    cur = now.tm_hour * 60 + now.tm_min
    start, end = ah * 60 + am, bh * 60 + bm
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end  # 跨午夜


def req_flags(payload: dict) -> tuple[bool, bool, bool]:
    """返回 (要 strict json_schema, 要 JSON, 要 tools)。"""
    rf = payload.get("response_format")
    wants_schema = isinstance(rf, dict) and rf.get("type") == "json_schema"
    wants_json = isinstance(rf, dict) and rf.get("type") in ("json_schema", "json_object")
    wants_tools = bool(payload.get("tools"))
    return wants_schema, wants_json, wants_tools


def request_schema(payload: dict) -> object:
    """取出请求里的 response_format.json_schema.schema（形状不对就回 None，绝不抛）。"""
    rf = payload.get("response_format")
    if not isinstance(rf, dict):
        return None
    js = rf.get("json_schema")
    if not isinstance(js, dict):
        return None
    return js.get("schema")


def first_finish_reason(up_body: object) -> str:
    """取 choices[0].finish_reason；形状不对就回空串（R5 用它区分思考模型烧光 token）。"""
    try:
        ch0 = up_body["choices"][0]  # type: ignore[index]
        return str(ch0.get("finish_reason") or "")
    except Exception:  # noqa: BLE001
        return ""


# R1/R2/R3 的规模上限：既有的 200k 字符扫描上限保留，另加候选块数与扫描步数上限，
# 避免病态文本把「枚举所有配平块」变成 O(n²) 卡死；schema 递归也另有深度/节点上限。
_JSON_BLOCK_SCAN_LIMIT = 200000
_JSON_BLOCK_MAX_CANDIDATES = 200
_JSON_BLOCK_SCAN_STEPS = 3000000

_SCHEMA_MAX_DEPTH = 32
_SCHEMA_MAX_NODES = 20000

# JSON Schema 关键字集合（R2）：解析出的对象键集全部落在这里 → 判定为「schema 回显」
_SCHEMA_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "description",
    "$schema", "$id", "$ref", "$comment", "title", "definitions", "$defs",
    "anyOf", "oneOf", "allOf", "not", "if", "then", "else",
    "format", "pattern", "patternProperties", "propertyNames", "prefixItems", "contains",
    "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "multipleOf", "minItems", "maxItems", "uniqueItems", "minProperties", "maxProperties",
    "const", "default", "examples", "deprecated", "readOnly", "writeOnly",
    "dependentRequired", "dependentSchemas", "unevaluatedItems", "unevaluatedProperties",
    "contentEncoding", "contentMediaType",
})


def iter_json_blocks(text: str):
    """R3：按出现顺序枚举 text 里所有「配平且能 json.loads」的 {...} 块。

    旧实现只返回第一个能解析的块；现在把全部候选交给调用方按 schema 挑选，
    避免「先出现的合法 JSON 但不符合 schema」把真正的结果挡在后面。
    文本长度 / 候选个数 / 扫描步数都有硬上限，防止病态输入拖死提取。
    """
    if not isinstance(text, str) or "{" not in text:
        return
    text = text[:_JSON_BLOCK_SCAN_LIMIT]
    n = len(text)
    steps = 0
    yielded = 0
    for start in range(n):
        if text[start] != "{":
            continue
        depth = 0
        in_str = False
        esc = False
        for i in range(start, n):
            steps += 1
            if steps > _JSON_BLOCK_SCAN_STEPS:
                return
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    cand = text[start:i + 1]
                    try:
                        json.loads(cand)
                    except json.JSONDecodeError:
                        break  # 这个起点不行，换下一个起点
                    yielded += 1
                    yield cand
                    if yielded >= _JSON_BLOCK_MAX_CANDIDATES:
                        return
                    break


def json_type_matches(value: object, type_name: str) -> bool:
    """JSON Schema 的 type 判定；bool 不算 number/integer（Python 里 True 是 int 的子类）。"""
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True  # 未知/未声明的类型名不判失败


def validate_against_schema(obj: object, schema: object, _depth: int = 0,
                            _budget: list | None = None) -> tuple[bool, str]:
    """R1：纯函数，把 obj 对照 JSON Schema 做一致性检查。返回 (是否通过, 失败原因)。

    覆盖 required / 顶层类型 / enum / properties / additionalProperties / items（递归）。
    只用标准库；递归深度与节点数有硬上限，防止病态 schema 打爆栈。
    失败原因只含字段名与类型，不含业务数据（日志安全）。
    """
    if not isinstance(schema, dict) or not schema:
        return True, "ok"
    if _budget is None:
        _budget = [0]
    _budget[0] += 1
    if _budget[0] > _SCHEMA_MAX_NODES:
        return False, "schema_too_large"
    if _depth > _SCHEMA_MAX_DEPTH:
        return False, "schema_too_deep"

    declared = schema.get("type")
    if isinstance(declared, str):
        if not json_type_matches(obj, declared):
            return False, f"type_mismatch:期望 {declared}"
    elif isinstance(declared, list):
        names = [t for t in declared if isinstance(t, str)]
        if names and not any(json_type_matches(obj, t) for t in names):
            return False, f"type_mismatch:期望 {'/'.join(names)}"

    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        try:
            if obj not in enum:
                return False, "enum_mismatch"
        except Exception:  # noqa: BLE001  自定义类型的 == 比较可能抛异常
            return False, "enum_mismatch"

    if isinstance(obj, dict):
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if isinstance(name, str) and name not in obj:
                    return False, f"missing_required:{name}"
        props = schema.get("properties")
        if isinstance(props, dict):
            for key, sub in props.items():
                if key in obj:
                    ok, why = validate_against_schema(obj[key], sub, _depth + 1, _budget)
                    if not ok:
                        return False, f"{key}.{why}"
        if schema.get("additionalProperties") is False:
            allowed = set(props.keys()) if isinstance(props, dict) else set()
            extra = sorted(k for k in obj.keys() if k not in allowed)
            if extra:
                return False, f"unexpected_property:{extra[0]}"
    elif isinstance(obj, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for idx, item in enumerate(obj):
                ok, why = validate_against_schema(item, items, _depth + 1, _budget)
                if not ok:
                    return False, f"[{idx}].{why}"
        elif isinstance(items, list):
            for idx, sub in enumerate(items[:len(obj)]):
                ok, why = validate_against_schema(obj[idx], sub, _depth + 1, _budget)
                if not ok:
                    return False, f"[{idx}].{why}"
    return True, "ok"


def looks_like_schema_echo(obj: object) -> bool:
    """R2：键集 ⊆ JSON Schema 关键字集（空 dict 也算）→ 是 schema 回显，不是业务结果。"""
    if not isinstance(obj, dict):
        return False
    return set(obj.keys()) <= _SCHEMA_KEYWORDS


# R2（P0.7 收窄）：单个值「长得像 JSON Schema 节点」的判据 —— dict 且含 type/properties/items/enum 之一。
_SCHEMA_NODE_HINTS = frozenset({"type", "properties", "items", "enum"})


def looks_like_schema_node(value: object) -> bool:
    """R2（P0.7）：值是 dict 且含 type/properties/items/enum 之一 → 像 JSON Schema 节点。

    只用于「回显判定」，不参与 R1 的一致性校验；合法业务结果里的值是数组/字符串/普通对象时不会命中。
    """
    if not isinstance(value, dict) or not value:
        return False
    return bool(set(value.keys()) & _SCHEMA_NODE_HINTS)


def looks_like_props_fragment_echo(obj: object, schema: object) -> bool:
    """R2（P0.7 收窄）：整段 content 就是 schema `properties` 的片段回显。

    判据（§0 真机抓到的形状）：顶层**每个值都长得像 JSON Schema 节点**
    （是 dict 且含 type/properties/items/enum 之一，即 "facts"/"language" 的值各是一个 {type: ...} 说明）；
    若请求 schema 里能拿到 `properties`，还要求顶层键集是它的子集（键名对得上才叫 properties 片段回显）。
    因为要求「每个值都像 schema 节点」，`{"facts":[{"content":"x","entities":["y"]}],"language":"zh"}`
    这类合法结果不会被误伤（facts 是数组、language 是字符串，都不是 schema 节点）。
    键名对不上、值也不像 schema 节点的其它坏 content 不在这里判，交给 R1 一致性校验兜住。
    """
    if not isinstance(obj, dict) or not obj:
        return False
    if not all(looks_like_schema_node(v) for v in obj.values()):
        return False
    props = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(props, dict) and props:
        return set(obj.keys()) <= set(props.keys())
    # 没有 schema（或 schema 没有 properties）可对照时，只看「顶层每个值都像不像 schema 节点」。
    return True


def check_content_against_schema(obj: object, schema: object) -> tuple[bool, str, str]:
    """返回 (是否通过, 原因, 细节)；原因 ∈ {"", "schema_echo", "schema_mismatch"}。

    R2 的回显判定不依赖请求里的 schema（键集本身就能判定，§0.2 的复现脚本也靠它）；
    R1 的一致性判定在拿到 schema 时执行。
    """
    if looks_like_schema_echo(obj):
        return False, "schema_echo", "键集全部是 JSON Schema 关键字"
    if looks_like_props_fragment_echo(obj, schema):
        return False, "schema_echo", "顶层每个值都像 JSON Schema 节点（疑似 properties 片段回显）"
    if isinstance(schema, dict) and schema:
        ok, why = validate_against_schema(obj, schema)
        if not ok:
            return False, "schema_mismatch", why
    return True, "", ""


def validate_json_content(text: str, extract_fallback: bool, schema: object = None,
                          detail_out: list | None = None) -> tuple[object, str, str]:
    """返回 (parsed|None, 规范化后的文本, 原因)。

    原因 ∈ {ok, extracted, bad_json, schema_echo, schema_mismatch}。
    R1/R2：解析出来还必须符合 schema（拿到 schema 时）且不是 schema 回显，才算 ok；
    整个 content 就是一个 JSON 值但不合格时不挖子块（子块只可能是 schema 碎片），直接判失败。
    R3：整个 content 解析不过时才走配平块提取，依次尝试所有块，挑第一个通过检查的；
    全都不合格 → 判失败（细节留在 detail_out 里供日志诊断）。
    """
    parsed_ok = False
    try:
        obj = json.loads(text)
        parsed_ok = True
    except Exception:  # noqa: BLE001
        obj = None
    if parsed_ok:
        ok, why, detail = check_content_against_schema(obj, schema)
        if ok:
            return obj, text, "ok"
        if detail_out is not None:
            detail_out.clear()
            if detail:
                detail_out.append(detail)
        return None, text, why
    first_fail = ""
    first_detail = ""
    if extract_fallback:
        for block in iter_json_blocks(text):
            try:
                cand = json.loads(block)
            except Exception:  # noqa: BLE001
                continue
            ok, why, detail = check_content_against_schema(cand, schema)
            if ok:
                return cand, block, "extracted"
            if not first_fail:
                first_fail, first_detail = why, detail
    if detail_out is not None:
        detail_out.clear()
        if first_detail:
            detail_out.append(first_detail)
    return None, text, first_fail or "bad_json"


def parse_reset_seconds(headers: dict[str, str], body_text: str, default: float) -> float:
    """429/额度类响应里尽量读出 reset 时间；读不到用 default。"""
    for name in ("retry-after", "x-ratelimit-reset-requests", "x-ratelimit-reset",
                 "x-ratelimit-reset-tokens", "x-ratelimit-reset-request"):
        raw = (headers or {}).get(name)
        if not raw:
            continue
        s = str(raw).strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            v = float(s)
            return v / 1000.0 if v > 100000 else v
        m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", s)
        if m and any(m.groups()):
            return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)
        try:  # HTTP 日期形式
            dt = parsedate_to_datetime(s)
            return max(1.0, dt.timestamp() - time.time())
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r'"?reset(?:_after|_in)?"?\s*[:=]\s*"?(\d+(?:\.\d+)?)', body_text or "")
    if m:
        return float(m.group(1))
    return default


def has_quota_words(body_text: str) -> bool:
    low = (body_text or "").lower()
    return any(w in low for w in _QUOTA_WORDS)


# P0.6/R2：账号级 429 的判定词。只有明确指向「账号 / 套餐 / 组织 / 整体额度耗尽」时才算
# 账号级，才允许整把 key 冷却（所有模型一起停）。模型额度耗尽的 429（上游只说 tpm/rpm/
# 模型限流）一律走 R1 的 (key, 模型) 粒度冷却。
_ACCOUNT_429_WORDS = (
    "account", "organization", "organisation", "plan", "billing", "subscription",
    "insufficient_quota", "quota exceeded", "exceeded your current quota",
    "账号", "账户", "套餐", "账户额度", "余额不足",
)


def is_account_level_429(body_text: str, headers: dict[str, str]) -> bool:
    """429 是否「账号级/套餐级」（而不是单个模型额度耗尽）。

    只有在 429 响应体/响应头里读出明确指向账号或套餐的字样时才返回 True；
    判不出来一律按「模型级」处理（只冷 (key, model)），宁可多试一个模型，
    也不要平白停掉同 key 上健康的模型。
    """
    blob = (body_text or "").lower()
    for name, value in (headers or {}).items():
        blob += "\n" + str(name).lower() + ": " + str(value).lower()
    return any(w in blob for w in _ACCOUNT_429_WORDS)


def classify(status: int, body_text: str, headers: dict[str, str]) -> str:
    if status == 200:
        return "http_ok"
    if status == 0:
        return "transport"
    if status == 429:
        return "ratelimit"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota"
    if status == 404:
        return "model_gone"
    if status >= 500:
        return "server"
    if status == 400:
        low = (body_text or "").lower()
        if "temperature" in low:
            return "temp_invalid"
        if any(w in low for w in _RF_WORDS):
            return "rf_unsupported"
        if any(w in low for w in _MODEL_WORDS):
            return "model_gone"
        if has_quota_words(body_text):
            return "quota"
        return "bad_request"
    return "other"


def schema_instruction(schema: object) -> str:
    return (
        "\n\n[llm-relay] 请只输出一个 JSON，必须严格符合下面这个 JSON Schema；"
        "不要输出解释、Markdown 代码块标记或任何额外文字：\n"
        + json.dumps(schema, ensure_ascii=False)
    )


def append_schema_prompt(messages: object, schema: object) -> object:
    """把 schema 以提示词形式追加到最后一条 user 消息（没有 user 就补一条 system）。"""
    if not isinstance(messages, list) or not messages:
        return [{"role": "user", "content": schema_instruction(schema).strip()}]
    msgs = [dict(m) if isinstance(m, dict) else m for m in messages]
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                m["content"] = c + schema_instruction(schema)
            elif isinstance(c, list):
                m["content"] = list(c) + [{"type": "text", "text": schema_instruction(schema)}]
            else:
                m["content"] = schema_instruction(schema).strip()
            return msgs
    msgs.append({"role": "system", "content": schema_instruction(schema).strip()})
    return msgs


def normalize_response(up_body: object, wants_json: bool, validate_json: bool,
                       extract_fallback: bool, schema: object = None,
                       detail_out: list | None = None) -> tuple[bool, dict | None, str]:
    """§3.4 响应归一化。返回 (可用, 归一化后的响应体, 原因)。

    schema（可选，默认 None 保持向后兼容）：R1 的一致性校验目标；
    为 None 时仍然做 R2 的 schema 回显判定（键集本身就能判定）。
    detail_out：可选，写入失败细节（如 missing_required:facts），只进日志。
    """
    if not isinstance(up_body, dict):
        return False, None, "not_json_body"
    choices = up_body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False, None, "no_choices"
    ch0 = choices[0]
    msg = ch0.get("message")
    if not isinstance(msg, dict):
        return False, None, "no_message"

    content = msg.get("content")
    promoted = False
    if not (isinstance(content, str) and content.strip()):
        for field_name in ("reasoning_content", "reasoning"):
            r = msg.get(field_name)
            if isinstance(r, str) and r.strip():
                content = r
                promoted = True
                break
    tool_calls = msg.get("tool_calls")
    if not (isinstance(content, str) and content.strip()) and not tool_calls:
        return False, None, "empty_content"

    reason = "promoted" if promoted else "ok"
    if wants_json and validate_json and isinstance(content, str) and content.strip():
        parsed, norm_text, why = validate_json_content(content, extract_fallback, schema, detail_out)
        if parsed is None:
            return False, None, why or "bad_json"  # 该候选判失败，换下一个（R4）
        content = norm_text
        reason = why if why != "ok" else reason

    new_msg = dict(msg)
    new_msg.setdefault("role", "assistant")
    if isinstance(content, str) or content is None:
        new_msg["content"] = content
    new_msg.setdefault("finish_reason", None)

    body: dict = {k: v for k, v in up_body.items() if k != "choices"}
    body["object"] = up_body.get("object") or "chat.completion"
    body["choices"] = [dict(ch0, message=new_msg)] + [c for c in choices[1:]]
    usage = body.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if not isinstance(usage.get(k), int):
            usage[k] = 0
    body["usage"] = usage
    return True, body, reason


@dataclass
class KeySlot:
    env_name: str
    value: str
    index: int
    fp: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    cooldown_until: float = 0.0
    cooldown_reason: str = ""
    uses: int = 0
    n429: int = 0
    n401: int = 0
    # P0.6/R1：429 冷却按「(这把 key, 这个模型)」记，而不是整把 key。上游的模型额度是
    # 按模型独立的：一个枯竭的模型（如 deepseek-v4-*）不该把同 key 上额度富余的模型
    # （如 sensenova-6.8-flash-lite）一起停掉。结构：model_id -> {"until": float, "reason": str}。
    # 整把 key 的 cooldown_until 只保留给 P0.6/R2 的账号级 429（账号/套餐/额度耗尽）。
    model_cooldown: dict[str, dict] = field(default_factory=dict)
    # R3（P0.5）：这把 key 最近的 429 / 鉴权失败时间戳，用来判断「同 provider 的 key 是不是
    # 在短窗口里全都被罚过」。只有到那时才允许把 provider 一起冷却，而不是一把 key 撞限全池清空。
    fail_ts: deque[float] = field(default_factory=deque)

    def __post_init__(self) -> None:
        if not self.fp:
            self.fp = sha8(self.value) if self.value else "-"

    def cooldown_left(self, ts: float | None = None) -> float:
        return max(0.0, self.cooldown_until - (ts or time.time()))

    def note_model_429(self, model_id: str, dur_s: float, reason: str,
                       ts: float | None = None) -> float:
        """只冷却 (这把 key, 这个模型)，返回冷却结束时间戳。"""
        until = (ts or time.time()) + max(0.0, float(dur_s))
        cur = self.model_cooldown.get(model_id) or {}
        prev_until = float(cur.get("until") or 0.0)
        if until >= prev_until:
            self.model_cooldown[model_id] = {"until": until, "reason": reason}
        return self.model_cooldown[model_id]["until"]

    def model_cooldown_left(self, model_id: str, ts: float | None = None) -> float:
        entry = self.model_cooldown.get(model_id)
        if not entry:
            return 0.0
        return max(0.0, float(entry.get("until") or 0.0) - (ts or time.time()))

    def model_cooldown_reason(self, model_id: str) -> str:
        return str((self.model_cooldown.get(model_id) or {}).get("reason") or "")

    def model_cooldown_details(self, ts: float | None = None) -> list[dict]:
        """按模型的冷却明细（只列还没到期的），供 /status 观测。"""
        now = ts or time.time()
        out = []
        for mid, entry in self.model_cooldown.items():
            left = max(0.0, float(entry.get("until") or 0.0) - now)
            if left > 0:
                out.append({"model": mid, "left_s": round(left, 1),
                            "reason": str(entry.get("reason") or "")})
        out.sort(key=lambda d: (-d["left_s"], str(d["model"])))
        return out

    def note_failure(self, window_s: float, ts: float | None = None) -> None:
        """记一次限流/鉴权失败，并把窗口外的时间戳清掉。"""
        now = ts or time.time()
        self.fail_ts.append(now)
        while self.fail_ts and now - self.fail_ts[0] > window_s:
            self.fail_ts.popleft()


@dataclass
class ModelState:
    provider: str
    model_id: str
    disabled: bool = False
    disabled_reason: str = ""
    strip_temperature_forced: bool = False
    # R1（P0.5）：该模型历史响应里出现过 reasoning_content/reasoning → 下次给它更宽的超时
    saw_reasoning: bool = False
    counts: dict[str, int] = field(default_factory=dict)

    def bump(self, name: str, n: int = 1) -> None:
        self.counts[name] = self.counts.get(name, 0) + n


@dataclass
class Candidate:
    provider: str
    model_id: str
    chain: int
    json_schema: object  # "native" / "degradable" / False
    caps: dict
    params: dict
    cap_rank: int
    health: int
    order: tuple
    # T1.2：只有显式标记 free=true（provider 级或模型级）的候选才算「免费档」。
    # 缺失一律当作非免费 —— 绝不靠模型名里有没有 "free" 去猜。
    free: bool = False


@dataclass
class SendOpts:
    model_id: str
    degrade: bool = False
    strip_temp: bool = False
    max_tokens_mult: int | None = None
    schema: object = None
    alias: str = ""
    # R5：带 json_schema 的请求的 max_tokens 下限（只抬不降），以及本次是否真的抬过
    min_max_tokens: int | None = None
    max_tokens_raised: bool = False
    raised_from: int | None = None
    raised_to: int | None = None


@dataclass
class UpstreamResult:
    status: int
    raw: bytes
    headers: dict[str, str]
    latency_s: float
    error: str = ""
    json_body: dict | None = None

    def text(self) -> str:
        return self.raw.decode("utf-8", "replace")


class ProviderRuntime:
    """provider 运行时：key 池 + 并发信号量 + RPM 令牌桶 + 冷却/退避 + 计数。"""

    def __init__(self, name: str, rpm: int, max_concurrency: int, backoff_cfg: dict) -> None:
        self.name = name
        self.rpm = int(rpm or 0)
        self.sem = threading.BoundedSemaphore(max(1, int(max_concurrency or 1)))
        self.keys: list[KeySlot] = []
        self.cooldown_until = 0.0
        self.cooldown_reason = ""
        self.backoff_level = 0
        self.backoff_cfg = backoff_cfg or {}
        self.counts: dict[str, int] = {}
        self.rpm_events: deque[float] = deque()
        self.cursor = 0
        self.lock = threading.RLock()
        self.active = False
        self.missing_key = False
        self.max_concurrency = max(1, int(max_concurrency or 1))
        # R2（P0.5）：最近连续失败的 provider 级时间戳（成功即清空），用于「连续 ≥3 次才升到硬封顶」
        self.fail_events: deque[float] = deque()
        self.rpm_waited_s = 0.0
        self.rpm_blocked = 0

    def bump(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.counts[name] = self.counts.get(name, 0) + n

    def cooldown_left(self, ts: float | None = None) -> float:
        return max(0.0, self.cooldown_until - (ts or time.time()))

    def enter_provider_cooldown(self, reason: str, base_s: float, *, cap_s: float | None = None,
                                hard_cap_s: float | None = None,
                                escalate_window_s: float = DEFAULT_PROVIDER_FAILURE_WINDOW_S,
                                escalate_after: int = DEFAULT_PROVIDER_ESCALATE_AFTER) -> float:
        """指数退避 + 分级封顶（R2）。

        `backoff_level` 照旧递增（保留可观测性），但冷却时长必须 clamp：
        - 单次失败：`min(退避值, cap_s)`（默认 120s）→ 单次 transport 超时绝不产出 900s；
        - 同一 provider 在 `escalate_window_s`（默认 300s）窗口里**连续 ≥ escalate_after 次**
          （默认 3 次）失败，才允许抬到 `hard_cap_s`（默认 300s）。
        """
        cfg = self.backoff_cfg
        initial = float(cfg.get("initial_s", base_s or 60))
        factor = float(cfg.get("factor", 2))
        max_s = float(cfg.get("max_s", 900))
        soft = float(cap_s) if cap_s is not None else DEFAULT_PROVIDER_COOLDOWN_CAP_S
        hard = float(hard_cap_s) if hard_cap_s is not None else DEFAULT_PROVIDER_COOLDOWN_HARD_CAP_S
        hard = max(hard, soft)
        window = max(1.0, float(escalate_window_s or DEFAULT_PROVIDER_FAILURE_WINDOW_S))
        threshold = max(1, int(escalate_after or DEFAULT_PROVIDER_ESCALATE_AFTER))
        with self.lock:
            now = time.time()
            self.fail_events.append(now)
            while self.fail_events and now - self.fail_events[0] > window:
                self.fail_events.popleft()
            consecutive = len(self.fail_events)
            raw = min(max_s, initial * (factor ** self.backoff_level))
            self.backoff_level = min(self.backoff_level + 1, 4)
            dur = min(raw, hard if consecutive >= threshold else soft)
            dur = max(1.0, min(dur, hard))
            self.cooldown_until = max(self.cooldown_until, now + dur)
            self.cooldown_reason = reason
        return dur

    def reset_backoff(self) -> None:
        with self.lock:
            self.backoff_level = 0
            self.fail_events.clear()

    def note_key_failure(self, ks: KeySlot, window_s: float) -> bool:
        """记一把 key 的 429/鉴权失败；返回「同 provider 每把 key 在窗口内都失败过」。

        P0.6/R4.3：这个返回值是**跨 key 升级**的唯一触发条件 —— 单把 key 撞限只罚它自己，
        只有**每把 key** 都在 `window_s` 窗口内失败过（`all(...)`，所以 key 数 = 1 时也算
        「全部失败过」），调用方才允许把 provider 一起冷却；调用点再额外要求 `len(rt.keys) >= 2`，
        保证只有确实存在 ≥2 把 key 的 provider 才会被整池冷却。
        """
        with self.lock:
            ks.note_failure(window_s)
            if not self.keys:
                return False
            return all(bool(k.fail_ts) for k in self.keys)

    def next_key_slot(self, model_id: str) -> KeySlot | None:
        """round-robin，跳过 disabled / 整把 key 冷却中 / **该模型在这把 key 上冷却中** 的 key。

        P0.6/R3：429 冷却按 (key, 模型) 记，所以这里必须带 model_id —— 模型 A 在某把 key 上
        被冷时，模型 B 仍然能拿到这把 key。
        """
        with self.lock:
            n = len(self.keys)
            if not n:
                return None
            now = time.time()
            for i in range(n):
                idx = (self.cursor + i) % n
                ks = self.keys[idx]
                if ks.disabled or ks.cooldown_left(now) > 0:
                    continue
                if model_id and ks.model_cooldown_left(model_id, now) > 0:
                    continue
                self.cursor = (idx + 1) % n
                return ks
            return None

    def has_usable_key(self, model_id: str) -> bool:
        """只看不动 cursor（健康检查/候选排序用，不能有副作用），同样按模型维度判断。"""
        now = time.time()
        with self.lock:
            return any((not k.disabled) and k.cooldown_left(now) <= 0
                       and (not model_id or k.model_cooldown_left(model_id, now) <= 0)
                       for k in self.keys)

    def rpm_used(self) -> int:
        with self.lock:
            now = time.time()
            while self.rpm_events and now - self.rpm_events[0] > 60:
                self.rpm_events.popleft()
            return len(self.rpm_events)

    def rpm_reset_s(self) -> float:
        """本地 rpm 打满时，最早一个事件滑出 60s 窗口还要多久（秒）。"""
        with self.lock:
            if not self.rpm or len(self.rpm_events) < self.rpm:
                return 0.0
            return max(0.0, 60.0 - (time.time() - self.rpm_events[0]))

    def rpm_wait(self, deadline: float, queue_timeout: float) -> tuple[bool, float]:
        """到点排队而不是直接甩 429 给上游。返回 (是否拿到令牌, 排队秒数)。

        超预算/超排队上限 → (False, 已等秒数)。R4（P0.5）：本地 rpm 限流必须真的拦住请求，
        不许把它放过去让上游回 429。
        """
        if not self.rpm:
            return True, 0.0
        t_start = time.time()
        limit_t = min(deadline, t_start + queue_timeout)
        while True:
            with self.lock:
                now = time.time()
                while self.rpm_events and now - self.rpm_events[0] > 60:
                    self.rpm_events.popleft()
                if len(self.rpm_events) < self.rpm:
                    self.rpm_events.append(now)
                    return True, max(0.0, time.time() - t_start)
                wait = 60 - (now - self.rpm_events[0]) + 0.05
            if time.time() + wait > limit_t:
                return False, max(0.0, time.time() - t_start)
            self.rpm_waited_s = max(self.rpm_waited_s, time.time() - t_start)
            time.sleep(min(max(wait, 0.05), 0.5))


# ---------------------------------------------------------------------------
# usage.jsonl（TASK-DASHBOARD §3）：每个 /v1/chat/completions 请求结束追加一行
# ---------------------------------------------------------------------------
def usage_verdict(status: int, meta: dict) -> str:
    """把一次请求的终局翻译成 usage.jsonl 的 verdict（§9.6：新失败原因也要能表达）。"""
    attempts = meta.get("attempts") or []
    kinds = [str(a.get("kind") or "") for a in attempts]
    reasons = [str(a.get("normalize") or "") for a in attempts if a.get("normalize")]
    last_reason = reasons[-1] if reasons else ""
    last_degraded = bool(attempts and attempts[-1].get("degrade"))
    if status == 200:
        return "ok"
    if status == 502 and reasons:
        # 降级路径判不过单独成词：降级只改变「怎么向上游要 JSON」，不改变期望形状
        if last_degraded:
            return "degraded_schema_rejected"
        if last_reason == "schema_echo":
            return "schema_echo"
        if last_reason == "schema_mismatch":
            return "schema_mismatch"
        if last_reason == "bad_json":
            return "bad_json"
        return "relay_unusable_content"
    if status == 502:
        return "relay_all_failed"
    if status == 429:
        return "ratelimit" if "ratelimit" in kinds else "cooldown"
    if status in (401, 403):
        return "auth"
    if status == 402:
        return "quota"
    if status and status >= 500:
        if "transport" in kinds:
            return "transport_timeout"
        if "server" in kinds:
            return "upstream_error"
        return "relay_error"
    if status == 503:
        return "no_candidates" if not attempts else "upstream_error"
    if status == 400:
        return "bad_request"
    return "other"


class UsageLog:
    """usage.jsonl 的追加写入：加锁 + 单行原子 + 超限轮转（.1 … .keep）。"""

    def __init__(self, relay: "Relay") -> None:
        self.relay = relay
        self.lock = threading.Lock()

    def cfg(self) -> dict:
        return self.relay.cfg.get("usage_log") or {}

    def enabled(self) -> bool:
        return bool(self.cfg().get("enabled"))

    def path(self) -> Path:
        return Path(os.path.expanduser(str(self.cfg().get("path") or
                                           "~/.hermes/llm-relay/usage.jsonl")))

    def record(self, row: dict) -> None:
        """写一行。关闭时完全不写；任何异常都不影响主流程（只是记不上用量）。"""
        try:
            if not self.enabled():
                return
            p = self.path()
            max_mb = float(self.cfg().get("max_mb") or 20)
            keep = int(self.cfg().get("keep") or 5)
            line = json.dumps(row, ensure_ascii=False) + "\n"
            with self.lock:
                p.parent.mkdir(parents=True, exist_ok=True)
                # 先轮转再写：转完当前文件重新从这一行开始，任何时刻都读得到「当前文件」
                try:
                    if max_mb > 0 and p.exists() and p.stat().st_size > max_mb * 1024 * 1024:
                        self._rotate(p, keep)
                except OSError:
                    pass
                with p.open("a", encoding="utf-8") as fh:
                    fh.write(line)
                    fh.flush()
        except Exception as e:  # noqa: BLE001
            try:
                self.relay.log(f"usage.jsonl 写入失败（不影响请求）：{type(e).__name__}: {e}")
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _rotate(p: Path, keep: int) -> None:
        keep = max(1, keep)
        for i in range(keep - 1, 0, -1):
            src = p.with_name(p.name + f".{i}")
            if src.exists():
                os.replace(src, p.with_name(p.name + f".{i + 1}"))
        os.replace(p, p.with_name(p.name + ".1"))


# ---------------------------------------------------------------------------
# 调用方配额（T1.1）：rpm 滑动窗口 + 当日 token 预算
# ---------------------------------------------------------------------------
class CallerLimiter:
    """每个调用方的限流/预算，全部在内存里判，**不逐请求重扫 usage.jsonl**。

    · `rpm`：60s 滑动窗口。放行时才把时间戳记进窗口，因此「被拒的请求不占额度」。
    · `daily_tokens`：自然日（本地时区）内 prompt+completion 之和。
      当天第一次用到某个调用方时，从 usage.jsonl **懒加载重建一次**（只扫 `caller` 字段
      匹配、`ts` 落在当天的行），之后只在内存里累加；`_day_loaded` 记住「按哪个日期重建过」，
      所以同一天不会重复扫描全量文件，也不会把已经落盘的同一行重复计入。
      进程中途重启后，第一笔请求会重新扫一遍当天历史 —— 这正是「预算不因重启清零」的保证。
    """

    WINDOW_S = 60.0

    def __init__(self, relay: "Relay") -> None:
        self.relay = relay
        self.lock = threading.Lock()
        self._rpm: dict[str, list[float]] = {}
        self._day: dict[str, list] = {}        # name -> [日期, 当日 token]
        self._day_loaded: dict[str, str] = {}  # name -> 已重建过的日期

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    def _scan_today(self, name: str, today: str) -> int:
        """扫一遍 usage.jsonl，只累加「这个调用方 + 今天」的 token（文件坏了也不抛）。

        usage_log 关闭时没有历史可重建（也就没有持久预算），直接回 0 —— 避免误读别的路径。
        """
        if not self.relay.usage.enabled():
            return 0
        total = 0
        try:
            with self.relay.usage.path().open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or '"caller"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:  # noqa: BLE001  坏行跳过，不能让一行坏数据毁掉预算统计
                        continue
                    if not isinstance(row, dict) or str(row.get("caller") or "") != name:
                        continue
                    if not str(row.get("ts") or "").startswith(today):
                        continue
                    total += int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
        except OSError:
            return 0
        return total

    def _ensure_day_locked(self, name: str) -> None:
        today = self._today()
        if self._day_loaded.get(name) != today:
            self._day[name] = [today, self._scan_today(name, today)]
            self._day_loaded[name] = today

    def rpm_used(self, name: str) -> int:
        """当前 60s 窗口内已放行的请求数（面板只读，顺带清理过期时间戳）。"""
        now = time.time()
        with self.lock:
            ev = [t for t in self._rpm.get(name, []) if now - t < self.WINDOW_S]
            self._rpm[name] = ev
            return len(ev)

    def tokens_today(self, name: str) -> int:
        with self.lock:
            self._ensure_day_locked(name)
            return int(self._day[name][1])

    def allow(self, name: str, cfg: dict) -> tuple[str, float]:
        """判这次请求能不能放行。返回 (原因, 建议退避秒)；原因 ∈ {"", "rpm", "quota"}。

        先判 rpm 再判当日预算（rpm 被拒时不动预算），通过后记一次 rpm 窗口。
        """
        now = time.time()
        with self.lock:
            ev = [t for t in self._rpm.get(name, []) if now - t < self.WINDOW_S]
            rpm = self._as_int(cfg.get("rpm"))
            if rpm > 0 and len(ev) >= rpm:
                self._rpm[name] = ev
                return "rpm", max(1.0, self.WINDOW_S - (now - ev[0]))
            self._ensure_day_locked(name)
            limit = self._as_int(cfg.get("daily_tokens"))
            if limit > 0 and int(self._day[name][1]) >= limit:
                self._rpm[name] = ev
                return "quota", 0.0
            ev.append(now)
            self._rpm[name] = ev
            return "", 0.0

    def add_tokens(self, name: str, n: int) -> None:
        """请求结束后把真实 token 计入当天预算（被拒不产生 token，因此不会走到这里加数）。"""
        n = int(n or 0)
        if not name or n <= 0:
            return
        with self.lock:
            self._ensure_day_locked(name)
            self._day[name][1] = int(self._day[name][1]) + n

    @staticmethod
    def _as_int(v: object) -> int:
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


# ---------------------------------------------------------------------------
# 用量聚合索引（T1.3）：按 caller / route / provider / model 分组，1h / 24h / 7d 窗口
# ---------------------------------------------------------------------------
class UsageIndex:
    """`/admin/usage` 与 `/metrics` 背后的分组聚合，**不逐请求重扫 usage.jsonl**。

    做法与 T1.1 的 :class:`CallerLimiter` 同一套「惰性重建 + 增量计数」：
      · 第一次查询（或 usage.jsonl 被外部改动、轮转、进程重启后）时扫一遍当前 + 轮转文件，
        只把落在最大窗口（7 天）内的行装进内存事件表；之后 `note()` 每落一行就增量追加；
      · 因此单次查询的代价只与「窗口内的行数」有关，与文件总大小、查询次数无关；
      · 事件表按老到新排序，超过 7 天的头部在 `note()` 时顺手丢弃。

    口径与 usage.jsonl **逐条可对账**：`snapshot()` 就是「ts ≥ now - window」这些行的直接求和；
    `alias == "probe"` 的内部探针行不计（与旧 `/admin/usage` 一致，docstring 同步写明）。
    """

    WINDOWS: dict[str, float] = {"1h": 3600.0, "24h": 86400.0, "7d": 7 * 86400.0}
    GROUPS: tuple[str, ...] = ("caller", "route", "provider", "model")
    MAX_WINDOW_S = 7 * 86400.0

    def __init__(self, relay: "Relay") -> None:
        self.relay = relay
        self.lock = threading.Lock()
        self._events: deque[tuple] = deque()   # (ts, caller, route, provider, model, ok, p, c, t)
        self._loaded = False
        self._sig: tuple | None = None

    # ---------------------------------------------------------------- 重建 / 增量
    @staticmethod
    def _parse_ts(value: object) -> float:
        """usage.jsonl 里的 ts 是本地时间 `%Y-%m-%dT%H:%M:%S`，转成 epoch；读不出回 0。"""
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value or "")[:19]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                return time.mktime(time.strptime(s, fmt))
            except Exception:  # noqa: BLE001
                continue
        return 0.0

    def _event(self, row: dict, ts: float | None = None) -> tuple | None:
        if str(row.get("alias") or "") == "probe":
            return None
        t = self._parse_ts(row.get("ts")) if ts is None else float(ts)
        if t <= 0:
            return None
        p = int(row.get("prompt_tokens") or 0)
        c = int(row.get("completion_tokens") or 0)
        total = int(row.get("total_tokens") or 0) or (p + c)
        return (t,
                str(row.get("caller") or "anonymous"),
                str(row.get("alias") or "-"),
                str(row.get("provider") or "-"),
                str(row.get("model") or "-"),
                int(row.get("http") or 0) == 200,
                p, c, total)

    def _rebuild(self, now: float) -> None:
        cutoff = now - self.MAX_WINDOW_S
        evs: list[tuple] = []
        for f in self.relay._usage_files():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(obj, dict):
                    continue
                ev = self._event(obj)
                if ev is not None and ev[0] >= cutoff:
                    evs.append(ev)
        evs.sort(key=lambda e: e[0])
        self._events = deque(evs)
        self._loaded = True
        self._sig = file_sig(self.relay.usage.path())

    def _ensure(self) -> None:
        now = time.time()
        if not self._loaded or self._sig != file_sig(self.relay.usage.path()):
            self._rebuild(now)

    def note(self, row: dict) -> None:
        """每写一行 usage.jsonl 就增量更新索引（写盘后调用，顺便同步文件签名）。"""
        ev = self._event(row)
        if ev is None:
            return
        with self.lock:
            self._events.append(ev)
            cutoff = time.time() - self.MAX_WINDOW_S
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
            self._loaded = True
            self._sig = file_sig(self.relay.usage.path())

    # ---------------------------------------------------------------- 查询
    def snapshot(self, group: str, window: str, now: float | None = None) -> dict:
        """把窗口内的行按 group 求和；group ∈ GROUPS，window ∈ WINDOWS。"""
        if group not in self.GROUPS:
            raise ValueError(f"group 只能是 {'/'.join(self.GROUPS)}")
        if window not in self.WINDOWS:
            raise ValueError(f"window 只能是 {'/'.join(self.WINDOWS)}")
        with self.lock:
            self._ensure()
            evs = list(self._events)
        now = time.time() if now is None else float(now)
        since = now - self.WINDOWS[window]
        idx = self.GROUPS.index(group) + 1   # 事件表里分组名字段的偏移
        agg: dict[str, dict] = {}
        totals = {"requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                  "completion_tokens": 0, "total_tokens": 0}
        last_ts: dict[str, float] = {}
        for ev in evs:
            if ev[0] < since:
                continue
            name = ev[idx] or "-"
            b = agg.setdefault(name, {"requests": 0, "ok": 0, "failed": 0, "prompt_tokens": 0,
                                      "completion_tokens": 0, "total_tokens": 0})
            b["requests"] += 1
            b["ok" if ev[5] else "failed"] += 1
            b["prompt_tokens"] += ev[6]
            b["completion_tokens"] += ev[7]
            b["total_tokens"] += ev[8]
            totals["requests"] += 1
            totals["ok" if ev[5] else "failed"] += 1
            totals["prompt_tokens"] += ev[6]
            totals["completion_tokens"] += ev[7]
            totals["total_tokens"] += ev[8]
            last_ts[name] = max(last_ts.get(name, 0.0), ev[0])
        req_total = totals["requests"] or 1
        tok_total = totals["total_tokens"] or 1
        groups = []
        for name, b in agg.items():
            item = dict(b)
            item["name"] = name
            item["success_rate"] = round(b["ok"] / b["requests"], 4) if b["requests"] else 0.0
            item["share"] = round(b["requests"] / req_total, 4)
            item["token_share"] = round(b["total_tokens"] / tok_total, 4)
            item["last_ts"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                            time.localtime(last_ts.get(name, 0.0)))
            groups.append(item)
        groups.sort(key=lambda x: (-x["requests"], x["name"]))
        return {
            "group": group, "window": window,
            "since": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since)),
            "until": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "since_ts": round(since, 3), "until_ts": round(now, 3),
            "window_s": self.WINDOWS[window],
            "totals": dict(totals, success_rate=round(totals["ok"] / req_total, 4)),
            "groups": groups,
            "options": {"group": list(self.GROUPS), "window": list(self.WINDOWS)},
            "excluded_alias": "probe",
            "metrics": self.relay.metrics_enabled(),
            "metrics_path": "/metrics",
            "enabled": self.relay.usage.enabled(),
            "path": str(self.relay.usage.path()),
        }

    def metrics_lines(self) -> list[str]:
        """Prometheus 文本（不含 relay 侧的健康/冷却 gauge，那部分在 Relay.metrics_text）。"""
        lines: list[str] = []
        for metric, help_text in (
            ("llm_relay_requests_total", "窗口内的请求数（7 天保留窗，alias=probe 不计）"),
            ("llm_relay_requests_ok_total", "窗口内 HTTP 200 的请求数"),
            ("llm_relay_requests_failed_total", "窗口内非 200 的请求数"),
        ):
            lines += [f"# HELP {metric} {help_text}", f"# TYPE {metric} counter"]
        lines += ["# HELP llm_relay_tokens_total 窗口内 token 数（kind=prompt|completion|total）",
                  "# TYPE llm_relay_tokens_total counter"]
        for group in self.GROUPS:
            snap = self.snapshot(group, "7d")
            for item in snap["groups"]:
                lab = f'group="{group}",name="{UsageIndex._esc(item["name"])}"'
                lines.append(f"llm_relay_requests_total{{{lab}}} {item['requests']}")
                lines.append(f"llm_relay_requests_ok_total{{{lab}}} {item['ok']}")
                lines.append(f"llm_relay_requests_failed_total{{{lab}}} {item['failed']}")
                for kind in ("prompt", "completion", "total"):
                    val = item.get(f"{kind}_tokens", 0)
                    lines.append(f"llm_relay_tokens_total{{{lab},kind=\"{kind}\"}} {val}")
        lines.append(f"# HELP llm_relay_usage_index_window_seconds 用量索引保留窗（秒）")
        lines.append("# TYPE llm_relay_usage_index_window_seconds gauge")
        lines.append(f"llm_relay_usage_index_window_seconds {int(self.MAX_WINDOW_S)}")
        return lines

    @staticmethod
    def _esc(value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# ---------------------------------------------------------------------------
# 能力探测（/admin/probe 与 probe_caps.py 同一套形态：chat + strict json_schema + tools）
# ---------------------------------------------------------------------------
PROBE_SCHEMA = {
    "type": "object",
    "properties": {"facts": {"type": "array", "items": {"type": "string"}}, "language": {"type": "string"}},
    "required": ["facts", "language"],
    "additionalProperties": False,
}
PROBE_TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "查询城市当前时间",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
}]


def probe_cases() -> list[tuple[str, dict]]:
    """三条探测用例（每次调用都新建，避免被上游 body 适配就地改坏）。"""
    return [
        ("chat", {"max_tokens": 64, "temperature": 0.1, "messages": [
            {"role": "user", "content": "只回答两个字：在的"}]}),
        ("json_schema", {"max_tokens": 500, "temperature": 0.1, "messages": [
            {"role": "user", "content": "从「我在 Mac 上用 Hindsight 做长期记忆」这句话里抽取事实，输出 JSON。"}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "facts", "strict": True, "schema": PROBE_SCHEMA}}}),
        ("tools", {"max_tokens": 200, "temperature": 0.1, "messages": [
            {"role": "user", "content": "北京现在几点？用工具查。"}],
            "tools": PROBE_TOOLS, "tool_choice": "auto"}),
    ]


# ---------------------------------------------------------------------------
# config.json schema 校验（T1.5）：启动 + 热重载都过一遍，坏配置给可读错误（字段路径 /
# 期望值 / 实际值），**绝不抛裸 KeyError/TypeError 栈**。
# 行为选择：不拒绝启动。能救的救（未知字段忽略、坏 chain 项丢弃、坏标量回退默认），
# 救不了的把「出错的 provider / 模型」禁用（enabled=false）并打进日志，其余照常服务，
# 避免一处配置笔误让整站瘫掉。校验结果也放进了 /admin/config.issues 供面板/排查查看。
# ---------------------------------------------------------------------------
CONFIG_FIELD_RULES: dict[str, list[str]] = {
    "top": ["listen", "local_token", "default_alias", "auth", "callers", "request", "concurrency",
            "usage_log", "key_state", "providers", "local_fallback", "integrations", "routes"],
    "listen": ["host", "port"],
    "auth": ["mode"],
    "usage_log": ["enabled", "path", "max_mb", "keep", "metrics"],
    "request": ["per_attempt_timeout_s", "min_attempt_timeout_s", "reasoning_min_timeout_s",
                "total_budget_s", "min_attempts_within_budget", "max_candidates", "queue_timeout_s",
                "validate_json", "json_extract_fallback", "schema_min_max_tokens",
                "provider_cooldown_cap_s", "provider_cooldown_hard_cap_s",
                "provider_failure_window_s", "provider_escalate_after_failures"],
    "concurrency": ["global"],
    "caller": ["key", "key_env", "rpm", "daily_tokens", "allow_routes", "note"],
    "provider": ["name", "enabled", "free", "base_url", "api", "wire", "keys", "rpm", "rpm_note",
                 "note", "max_concurrency", "key_cooldown_s", "provider_cooldown_s",
                 "cooldown_backoff", "models"],
    "wire": ["max_tokens_param", "extra_headers", "max_tokens_multiplier", "strip_temperature"],
    "backoff": ["initial_s", "max_s", "factor"],
    "model": ["id", "chain", "caps", "params", "disabled", "free", "note", "max_tokens_multiplier",
              "rpm", "max_concurrency"],
    "caps": ["json_schema", "tools", "reasoning_only", "cot_leak"],
    "route": ["chain", "policy"],
    "policy": ["max_candidates", "free_only", "validate_json", "schema_min_max_tokens",
               "min_attempt_timeout_s"],
    "local_fallback": ["enabled", "base_url", "model", "window", "health_check_path", "autostart",
                       "free"],
    "upstream": ["label", "health_url", "control_plane_url", "usage_alias", "start_script",
                 "rollback_script", "env_prefix", "service", "dashboard_url", "cp_url"],
}


def _cfg_valid_window(window: str) -> bool:
    """判断 "HH:MM-HH:MM" 语法是否合法（只看格式，不看当前时间）。"""
    try:
        a, b = str(window).split("-")
        ah, am = (int(x) for x in a.strip().split(":"))
        bh, bm = (int(x) for x in b.strip().split(":"))
    except Exception:  # noqa: BLE001
        return False
    return (0 <= ah < 24 and 0 <= am < 60 and 0 <= bh < 24 and 0 <= bm < 60)


def _cfg_is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _cfg_is_num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_config(cfg: object, keys_raw: dict | None = None) -> tuple[dict, list[dict]]:
    """校验 config.json，返回 (可用的配置副本, 问题列表)。永不抛异常。

    每条问题形如 ``{"path", "expected", "actual", "action", "message"}``，供日志与
    ``/admin/config.issues`` 展示；调用方按 ``action`` 决定继续用哪份配置。
    """
    issues: list[dict] = []

    def add(path: str, expected: object, actual: object, action: str, message: str = "") -> None:
        issues.append({"path": path, "expected": expected, "actual": actual,
                       "action": action, "message": message})

    def unknown(prefix: str, obj: dict, allowed: list[str]) -> None:
        for k in list(obj):
            if k not in allowed:
                add(f"{prefix}.{k}", f"已知字段之一（{', '.join(allowed)}）", k,
                    "忽略该字段", "字段名拼写错误或已废弃")
                obj.pop(k, None)

    def scalar(prefix: str, obj: dict, field: str, kind: str, default: object,
               action: str | None = None) -> None:
        if field not in obj:
            return
        v = obj[field]
        ok = ((kind == "boolean" and isinstance(v, bool))
              or (kind == "string" and isinstance(v, str))
              or (kind == "integer" and _cfg_is_int(v))
              or (kind == "number" and _cfg_is_num(v))
              or (kind == "object" and isinstance(v, dict))
              or (kind == "array" and isinstance(v, list)))
        if not ok:
            add(f"{prefix}.{field}", kind, v, action or f"回退默认值 {default!r}")
            obj[field] = default

    if not isinstance(cfg, dict):
        add("config", "object", cfg, "拒绝本次配置，沿用旧配置", "config.json 顶层必须是 JSON 对象")
        return {}, issues
    try:
        out = json.loads(json.dumps(cfg, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        out = dict(cfg)
    unknown("config", out, CONFIG_FIELD_RULES["top"])

    # ---------------------------------------------------------------- 简单段
    for seg, rules in (("listen", CONFIG_FIELD_RULES["listen"]),
                       ("auth", CONFIG_FIELD_RULES["auth"]),
                       ("usage_log", CONFIG_FIELD_RULES["usage_log"]),
                       ("request", CONFIG_FIELD_RULES["request"]),
                       ("concurrency", CONFIG_FIELD_RULES["concurrency"])):
        if seg in out and not isinstance(out[seg], dict):
            add(seg, "object", out[seg], "忽略该段（各字段用默认值）")
            out.pop(seg, None)
        elif isinstance(out.get(seg), dict):
            unknown(seg, out[seg], rules)
    if isinstance(out.get("listen"), dict):
        scalar("listen", out["listen"], "host", "string", "127.0.0.1")
        scalar("listen", out["listen"], "port", "integer", 9110)
    if isinstance(out.get("usage_log"), dict):
        for f, kind, dflt in (("enabled", "boolean", True), ("path", "string", "usage.jsonl"),
                              ("max_mb", "number", 20), ("keep", "integer", 5),
                              ("metrics", "boolean", False)):
            scalar("usage_log", out["usage_log"], f, kind, dflt)
    if isinstance(out.get("concurrency"), dict):
        cg = out["concurrency"].get("global")
        if "global" in out["concurrency"] and (not _cfg_is_int(cg) or cg < 1):
            add("concurrency.global", ">=1 的 integer", cg, "回退 8")
            out["concurrency"]["global"] = 8
    if isinstance(out.get("auth"), dict):
        mode = out["auth"].get("mode")
        if mode is not None and mode not in ("loopback_trust", "require_key"):
            add("auth.mode", "loopback_trust | require_key", mode, "回退 loopback_trust")
            out["auth"]["mode"] = "loopback_trust"

    # ---------------------------------------------------------------- providers
    providers = out.get("providers")
    if providers is None:
        providers = []
    if not isinstance(providers, list):
        add("providers", "array", providers, "忽略该段（没有任何 provider）")
        providers = []
    valid_models: set[str] = set()
    provider_names: set[str] = set()
    clean_providers: list[dict] = []
    for i, pcfg in enumerate(providers):
        if not isinstance(pcfg, dict):
            add(f"providers[{i}]", "object", pcfg, "丢弃该 provider")
            continue
        name = pcfg.get("name")
        if not isinstance(name, str) or not name.strip():
            add(f"providers[{i}].name", "非空 string", name, "丢弃该 provider")
            continue
        if name in provider_names:
            add(f"providers[{i}].name", "不重复的 provider 名", name, "丢弃重复的 provider")
            continue
        provider_names.add(name)
        p = f"providers[{name}]"
        unknown(p, pcfg, CONFIG_FIELD_RULES["provider"])
        broken = False
        scalar(p, pcfg, "enabled", "boolean", False)
        scalar(p, pcfg, "free", "boolean", False)
        if not isinstance(pcfg.get("base_url"), str) or not str(pcfg.get("base_url") or "").strip():
            add(f"{p}.base_url", "非空 string（上游根地址，如 https://host/v1）",
                pcfg.get("base_url"), "禁用该 provider", "缺少/类型不对的 base_url")
            broken = True
        keys = pcfg.get("keys")
        if keys is None:
            keys = []
        if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
            add(f"{p}.keys", "keys.env 里的环境变量名数组", keys, "禁用该 provider")
            broken = True
            keys = []
        pcfg["keys"] = keys
        for f in ("rpm", "key_cooldown_s", "provider_cooldown_s"):
            scalar(p, pcfg, f, "number", 0, "回退 0（不限制）")
        if "max_concurrency" in pcfg and (not _cfg_is_int(pcfg["max_concurrency"])
                                          or pcfg["max_concurrency"] < 1):
            add(f"{p}.max_concurrency", ">=1 的 integer", pcfg["max_concurrency"], "回退 1")
            pcfg["max_concurrency"] = 1
        wire = pcfg.get("wire")
        if wire is not None and not isinstance(wire, dict):
            add(f"{p}.wire", "object", wire, "忽略 wire（上游 body 用默认形制）")
            pcfg["wire"] = {}
        elif isinstance(wire, dict):
            unknown(f"{p}.wire", wire, CONFIG_FIELD_RULES["wire"])
            scalar(f"{p}.wire", wire, "max_tokens_param", "string", "max_tokens")
            scalar(f"{p}.wire", wire, "extra_headers", "object", {})
            scalar(f"{p}.wire", wire, "max_tokens_multiplier", "number", 1)
            scalar(f"{p}.wire", wire, "strip_temperature", "boolean", False)
        backoff = pcfg.get("cooldown_backoff")
        if backoff is not None and not isinstance(backoff, dict):
            add(f"{p}.cooldown_backoff", "object", backoff, "忽略（用默认退避）")
            pcfg["cooldown_backoff"] = {}
        elif isinstance(backoff, dict):
            unknown(f"{p}.cooldown_backoff", backoff, CONFIG_FIELD_RULES["backoff"])
            for f in CONFIG_FIELD_RULES["backoff"]:
                if f in backoff and not _cfg_is_num(backoff[f]):
                    add(f"{p}.cooldown_backoff.{f}", "number", backoff[f], "忽略该项")
                    backoff.pop(f, None)
        models = pcfg.get("models")
        if models is None:
            models = []
        if not isinstance(models, list):
            add(f"{p}.models", "array", models, "禁用该 provider")
            broken = True
            models = []
        clean_models: list[dict] = []
        seen_ids: set[str] = set()
        for j, mcfg in enumerate(models):
            if not isinstance(mcfg, dict):
                add(f"{p}.models[{j}]", "object", mcfg, "丢弃该模型")
                continue
            mid = mcfg.get("id")
            if not isinstance(mid, str) or not mid.strip():
                add(f"{p}.models[{j}].id", "非空 string", mid, "丢弃该模型")
                continue
            if mid in seen_ids:
                add(f"{p}.models[{j}].id", "不重复的模型 id", mid, "丢弃重复模型")
                continue
            seen_ids.add(mid)
            mp = f"{p}.models[{mid}]"
            unknown(mp, mcfg, CONFIG_FIELD_RULES["model"])
            if "chain" in mcfg and not _cfg_is_int(mcfg["chain"]):
                add(f"{mp}.chain", "integer（越小越优先）", mcfg["chain"], "禁用该模型")
                mcfg["disabled"] = True
            caps = mcfg.get("caps")
            if caps is not None and not isinstance(caps, dict):
                add(f"{mp}.caps", "object", caps, "禁用该模型（caps 用默认值）")
                mcfg["caps"] = {}
                mcfg["disabled"] = True
            elif isinstance(caps, dict):
                unknown(f"{mp}.caps", caps, CONFIG_FIELD_RULES["caps"])
                if "json_schema" in caps:
                    js = caps["json_schema"]
                    if js is False:
                        pass  # 显式声明「不支持 json_schema」，运行时据此跳过该模型
                    elif js not in ("native", "degradable", "none"):
                        add(f"{mp}.caps.json_schema", "native | degradable | none | false", js,
                            "回退 native")
                        caps["json_schema"] = "native"
                for f in ("tools", "reasoning_only"):
                    if f in caps and not isinstance(caps[f], bool):
                        add(f"{mp}.caps.{f}", "boolean", caps[f], "按 true 处理")
                        caps[f] = bool(caps[f])
            scalar(mp, mcfg, "params", "object", {})
            scalar(mp, mcfg, "disabled", "boolean", False)
            scalar(mp, mcfg, "free", "boolean", False)
            clean_models.append(mcfg)
            valid_models.add(f"{name}/{mid}")
        pcfg["models"] = clean_models
        if broken:
            pcfg["enabled"] = False
        clean_providers.append(pcfg)
    out["providers"] = clean_providers

    # ---------------------------------------------------------------- local_fallback
    lf = out.get("local_fallback")
    if lf is not None and not isinstance(lf, dict):
        add("local_fallback", "object", lf, "忽略该段（不启用本地兜底）")
        out["local_fallback"] = {}
    elif isinstance(lf, dict):
        unknown("local_fallback", lf, CONFIG_FIELD_RULES["local_fallback"])
        scalar("local_fallback", lf, "enabled", "boolean", False)
        for f in ("base_url", "model", "window", "health_check_path"):
            scalar("local_fallback", lf, f, "string", "")
        if isinstance(lf.get("window"), str) and lf["window"] and not _cfg_valid_window(lf["window"]):
            add("local_fallback.window", "HH:MM-HH:MM（可跨午夜）", lf["window"],
                "该时间窗口解析不了 → 本地兜底不生效")

    # ---------------------------------------------------------------- routes
    routes = out.get("routes")
    if routes is None:
        routes = {}
    if not isinstance(routes, dict):
        add("routes", "object", routes, "忽略该段（没有命名路由）")
        routes = {}
    for rname, spec in list(routes.items()):
        if not isinstance(spec, dict):
            add(f"routes.{rname}", "object", spec, "丢弃该 route")
            routes.pop(rname, None)
            continue
        unknown(f"routes.{rname}", spec, CONFIG_FIELD_RULES["route"])
        policy = spec.get("policy")
        if policy is not None and not isinstance(policy, dict):
            add(f"routes.{rname}.policy", "object", policy, "忽略该 policy")
            spec.pop("policy", None)
        elif isinstance(policy, dict):
            unknown(f"routes.{rname}.policy", policy, CONFIG_FIELD_RULES["policy"])
    out["routes"] = routes
    # chain 必须指向真实存在的 provider/model —— 放在 providers 之后才能拿到 valid_models
    for rname, spec in (out.get("routes") or {}).items():
        if not isinstance(spec, dict):
            continue
        chain = spec.get("chain", "auto")
        if chain == "auto":
            continue
        if not isinstance(chain, list):
            add(f"routes.{rname}.chain", '"auto" 或 [provider/model, ...]', chain, "回退 auto")
            spec["chain"] = "auto"
            continue
        clean_chain: list[str] = []
        for x in chain:
            if not isinstance(x, str):
                add(f"routes.{rname}.chain", "string 数组元素", x, "丢弃该项")
                continue
            if x not in valid_models:
                add(f"routes.{rname}.chain",
                    f"已存在的 provider/model（如 {sorted(valid_models)[:3] or '（无）'}）", x,
                    "丢弃该项", "chain 指向不存在的 provider/model")
                continue
            clean_chain.append(x)
        if not clean_chain:
            add(f"routes.{rname}.chain", "至少一个有效候选", chain, "回退 auto",
                "chain 里的候选全部无效")
            spec["chain"] = "auto"
        else:
            spec["chain"] = clean_chain

    # ---------------------------------------------------------------- callers
    callers = out.get("callers")
    if callers is None:
        callers = {}
    if not isinstance(callers, dict):
        add("callers", "object", callers, "忽略该段（调用方体系不启用）")
        callers = {}
    for cname, ccfg in list(callers.items()):
        cp = f"callers.{cname}"
        if not isinstance(ccfg, dict):
            add(cp, "object", ccfg, "丢弃该调用方")
            callers.pop(cname, None)
            continue
        unknown(cp, ccfg, CONFIG_FIELD_RULES["caller"])
        if not ccfg.get("key") and not ccfg.get("key_env"):
            add(cp, "'key' 或 'key_env' 至少一个",
                {"key": ccfg.get("key"), "key_env": ccfg.get("key_env")},
                "保留条目，但该身份认不出来（一律 401）", "key 与 key_env 同时缺失")
        for f in ("key", "key_env", "note"):
            scalar(cp, ccfg, f, "string", "")
        for f in ("rpm", "daily_tokens"):
            if f in ccfg and (not _cfg_is_num(ccfg[f]) or ccfg[f] < 0):
                add(f"{cp}.{f}", ">=0 的 number", ccfg[f], "按 0（不限额）处理")
                ccfg[f] = 0
        ar = ccfg.get("allow_routes")
        if ar is not None and not isinstance(ar, list):
            add(f"{cp}.allow_routes", "route 名数组", ar, "去掉该白名单（等于允许全部 route）")
            ccfg.pop("allow_routes", None)
        elif isinstance(ar, list):
            clean_ar: list[str] = []
            for x in ar:
                if not isinstance(x, str) or x not in (out.get("routes") or {}):
                    add(f"{cp}.allow_routes", f"已存在的 route 名（{sorted(out.get('routes') or {})}）",
                        x, "丢弃该项", "allow_routes 指向不存在的 route")
                    continue
                clean_ar.append(x)
            if clean_ar:
                ccfg["allow_routes"] = clean_ar
            else:
                ccfg.pop("allow_routes", None)
                add(f"{cp}.allow_routes", "至少一个有效 route 名", ar,
                    "去掉白名单（等于允许全部 route，避免把调用方锁死）", "白名单项全部无效")
    out["callers"] = callers

    # ---------------------------------------------------------------- integrations
    integ = out.get("integrations")
    if integ is not None and not isinstance(integ, dict):
        add("integrations", "object", integ, "忽略该段")
        out["integrations"] = {}
    elif isinstance(integ, dict):
        up = integ.get("upstream")
        if up is not None and not isinstance(up, dict):
            add("integrations.upstream", "object", up, "忽略该段（面板隐藏「上游」tab）")
            integ["upstream"] = {}
        elif isinstance(up, dict):
            unknown("integrations.upstream", up, CONFIG_FIELD_RULES["upstream"])

    return out, issues


class Relay:
    """中转站核心（HTTP 层只是薄壳，测试可直接调 chat()）。"""

    # 候选被跳过的原因分类：RETRYABLE = 冷却/禁用这类"不是网络故障"的可退避重试状态（回 429）；
    #                       OTHER = 能力不匹配 / 本地兜底窗口外等（回 503）
    SKIP_RETRYABLE = "cooldown"
    SKIP_OTHER = "other"

    def __init__(self, config_path: Path | str = DEFAULT_CONFIG, keys_path: Path | str = DEFAULT_KEYS,
                 quiet: bool = False) -> None:
        self.config_path = Path(config_path)
        self.keys_path = Path(keys_path)
        self.quiet = quiet
        self.lock = threading.RLock()
        self.log_lines: deque[str] = deque(maxlen=5000)
        self.providers: dict[str, ProviderRuntime] = {}
        self.models: dict[str, ModelState] = {}
        self.cfg: dict = {}
        self.keys_raw: dict[str, str] = {}
        self.global_sem = threading.BoundedSemaphore(8)
        self.counters: dict[str, int] = {}
        self.recent: deque[dict] = deque(maxlen=20)
        self._sig: tuple = ()
        self.last_candidates_info: dict = {}
        self._sighup = False
        self._local_ok_cache: tuple[float, bool] = (0.0, False)
        self.started_at = time.time()
        # TASK-ADMIN-RESTART-KEY §A：一键重启 = 让进程自己退出，交给 launchd 的 KeepAlive 拉起。
        # 白名单只有中转站自己（com.user.llm-relay）；绝不 shell 调 launchctl，也绝不碰别的服务。
        self._restarting = False
        self._restart_timer: threading.Timer | None = None
        self.restart_delay_s = 0.4          # 响应先 flush 出去，再退出（0.3–0.5s）
        self.restart_calls: list[dict] = []  # 只给测试/观测用：每次「真的排了重启」记一条
        self._exit_fn = _default_exit        # 测试可注入假回调，验证不会真退进程
        # usage.jsonl 写入器（配置从 self.cfg 动态读，热重载即生效）
        self.usage = UsageLog(self)
        # 调用方配额（T1.1）：rpm 滑动窗口 + 当日 token 预算，内存判定 + 懒重建
        self.callers = CallerLimiter(self)
        # 用量分组聚合（T1.3）：/admin/usage?group=&window= 与可选 /metrics 共用这一份状态缓存
        self.usage_index = UsageIndex(self)
        # T1.5：最近一次 config 校验的问题列表（可读错误，见 validate_config）
        self.config_issues: list[dict] = []
        self.reload(force=True, initial=True)

    # ------------------------------------------------------------------ 日志
    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {redact(msg)}"
        self.log_lines.append(line)
        if not self.quiet:
            print(line, flush=True)

    def bump(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + n

    # ------------------------------------------------------------ 配置/热重载
    def _sig_now(self) -> tuple:
        return (file_sig(self.config_path), file_sig(self.keys_path))

    def maybe_reload(self) -> bool:
        if self._sighup or self._sig_now() != self._sig:
            self._sighup = False
            self.reload(force=True)
            return True
        return False

    def request_reload(self) -> None:
        self._sighup = True

    def reload(self, force: bool = False, initial: bool = False) -> None:
        with self.lock:
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                self.log(f"配置读取失败，保留旧配置：{type(e).__name__}: {e}")
                return
            keys_raw = load_keys(self.keys_path)
            # T1.5：schema 校验（非致命）。坏字段不抛栈 —— 报告并尽力修复，出错的 provider/模型被禁用。
            cfg, config_issues = validate_config(cfg, keys_raw)
            self.config_issues = config_issues
            for it in config_issues:
                self.log(f"配置校验：{it['path']} 期望 {it['expected']!r}，实际 {it['actual']!r}"
                         f" → {it['action']}" + (f"（{it['message']}）" if it["message"] else ""))
            # 管理面板持久化的 key 状态（禁用/启用），重启后仍然生效
            key_state: dict = cfg.get("key_state") or {}
            old_providers, old_models = self.providers, self.models
            changed: list[str] = []

            providers: dict[str, ProviderRuntime] = {}
            models: dict[str, ModelState] = {}
            for pcfg in cfg.get("providers", []):
                name = pcfg.get("name") or "?"
                old = old_providers.get(name)
                rt = ProviderRuntime(
                    name,
                    rpm=pcfg.get("rpm", 0),
                    max_concurrency=pcfg.get("max_concurrency", 1),
                    backoff_cfg=pcfg.get("cooldown_backoff") or {},
                )
                env_names = list(pcfg.get("keys") or [])
                missing = [n for n in env_names if not keys_raw.get(n)]
                rt.missing_key = bool(env_names) and len(missing) == len(env_names)
                rt.active = bool(pcfg.get("enabled", False)) and not rt.missing_key and bool(env_names)
                if old is not None:
                    rt.counts = dict(old.counts)
                    rt.cooldown_until, rt.cooldown_reason = old.cooldown_until, old.cooldown_reason
                    rt.backoff_level, rt.cursor = old.backoff_level, old.cursor
                    rt.rpm_events = old.rpm_events
                    rt.fail_events = old.fail_events
                    rt.rpm_waited_s, rt.rpm_blocked = old.rpm_waited_s, old.rpm_blocked
                    # 并发上限没变就复用旧信号量（保住"正在并发"的语义），变了才换新的
                    if old.max_concurrency == rt.max_concurrency:
                        rt.sem = old.sem
                old_key_map = {k.env_name: k for k in (old.keys if old else [])}
                for i, env in enumerate(env_names):
                    val = keys_raw.get(env, "")
                    ks = KeySlot(env_name=env, value=val, index=i)
                    if not val:
                        # keys.env 里还没这一行 → 占位但不可用；主人追加一行后热重载即生效
                        ks.disabled = True
                        ks.disabled_reason = "keys.env 中未定义或为空"
                    prev = old_key_map.get(env)
                    if prev is not None and prev.fp == ks.fp:
                        ks.disabled = prev.disabled
                        ks.disabled_reason = prev.disabled_reason
                        ks.cooldown_until = prev.cooldown_until
                        ks.cooldown_reason = prev.cooldown_reason
                        # P0.6/R1：按模型的冷却明细也要在热重载后保留（否则热重载会白送一次 429）
                        ks.model_cooldown = dict(prev.model_cooldown)
                        ks.uses, ks.n429, ks.n401 = prev.uses, prev.n429, prev.n401
                        ks.fail_ts = prev.fail_ts
                    elif prev is not None and not initial:
                        changed.append(f"key {env} 值已变更（指纹 {prev.fp}→{ks.fp}），健康态已重置")
                    persisted = (key_state.get(name) or {}).get(env) or {}
                    if persisted and (prev is None or prev.fp != ks.fp):
                        # 进程重启（或这把 key 的值变了）后内存态归零 → 从磁盘的 key_state 恢复，
                        # 保证「禁用」不是只在内存里活到下次重启。
                        if "disabled" in persisted:
                            ks.disabled = bool(persisted.get("disabled"))
                            ks.disabled_reason = str(
                                persisted.get("disabled_reason")
                                or ("管理面板手动禁用（已持久化）" if ks.disabled else ""))
                    rt.keys.append(ks)
                if not old and not initial and rt.active:
                    changed.append(f"新增 provider {name}")
                providers[name] = rt
                for mcfg in pcfg.get("models", []):
                    mid = mcfg.get("id") or "?"
                    key = f"{name}/{mid}"
                    ms = old_models.get(key) or ModelState(provider=name, model_id=mid)
                    ms.provider, ms.model_id = name, mid
                    models[key] = ms
            # 本地兜底 provider（无 key）
            lf = cfg.get("local_fallback") or {}
            if lf.get("enabled"):
                rt = old_providers.get("__local__") or ProviderRuntime("__local__", rpm=0, max_concurrency=1, backoff_cfg={})
                rt.rpm = 0
                rt.keys = [KeySlot(env_name="LOCAL_MLX", value="", index=0)]
                rt.active = True
                providers["__local__"] = rt
                lkey = "__local__/" + str(lf.get("model") or "local-mlx")
                models[lkey] = old_models.get(lkey) or ModelState(provider="__local__", model_id=str(lf.get("model")))

            old_ids = {f"{m.provider}/{m.model_id}" for m in old_models.values()}
            new_ids = set(models)
            for mid in sorted(new_ids - old_ids):
                if not initial:
                    changed.append(f"新增模型 {mid}")
            for mid in sorted(old_ids - new_ids):
                if not initial:
                    changed.append(f"移除模型 {mid}")

            self.cfg = cfg
            self.keys_raw = keys_raw
            self.providers, self.models = providers, models
            glob = int(((cfg.get("concurrency") or {}).get("global")) or 8)
            if glob != getattr(self, "_glob_cached", None):
                self.global_sem = threading.BoundedSemaphore(max(1, glob))
                self._glob_cached = glob
            self._sig = self._sig_now()
        if initial:
            n_models = sum(1 for k in self.models if not k.startswith("__local__"))
            self.log(f"启动：配置 {self.config_path.name} 已加载（providers={len(self.providers)} models={n_models} keys={len(self.keys_raw)}）")
        else:
            detail = "；".join(changed) if changed else "keys/provider 值未变"
            self.log(f"热重载：配置已生效（{detail}）")
        # 调用方鉴权（T1.1/T1.4）：只报模式与名字，**永不打印 key 本身**；key_env 取不到值要显式告警，
        # 否则「配置了却认不出身份」会变成难查的静默 401。
        callers = self.cfg.get("callers")
        callers = callers if isinstance(callers, dict) else {}
        self.log(f"调用方：auth.mode={self.auth_mode()}，已配置 {len(callers)} 个"
                 + (f"（{', '.join(sorted(str(k) for k in callers))}）" if callers else ""))
        for cname, ccfg in callers.items():
            if not self._caller_key(ccfg):
                src = (ccfg or {}).get("key_env") if isinstance(ccfg, dict) else None
                self.log(f"调用方「{cname}」没有可用 key"
                         + (f"（key_env={src} 在 keys.env 里查不到）" if src else "（既没有 key 也没有 key_env）")
                         + " → 该身份暂时认不出来")

    # ------------------------------------------------------------- 候选与信号量
    def _local_ready(self) -> bool:
        lf = self.cfg.get("local_fallback") or {}
        if not lf.get("enabled"):
            return False
        if not in_window(str(lf.get("window") or "")):
            return False
        ts, ok = self._local_ok_cache
        if time.time() - ts < 30:
            return ok
        url = str(lf.get("base_url")).rstrip("/") + str(lf.get("health_check_path") or "/models")
        ok = False
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with OPENER.open(req, timeout=3) as r:
                ok = 200 <= r.status < 300
        except Exception:  # noqa: BLE001
            ok = False
        self._local_ok_cache = (time.time(), ok)
        return ok

    # --------------------------------------------------------- 命名路由（T1.2）
    def route_names(self) -> list[str]:
        """config.routes 里的 route 名（排序后，用于 400 错误体与 /v1/models）。"""
        routes = self.cfg.get("routes")
        return sorted(str(k) for k in routes) if isinstance(routes, dict) else []

    def exact_model_names(self) -> list[str]:
        """可被 `provider/model` 精确点名的模型（本地兜底不参与点名）。"""
        return sorted(k for k in self.models if not k.startswith("__local__"))

    def resolve_request_route(self, model: object) -> tuple[str, str | None, object, dict, dict | None]:
        """把请求里的 `model` 解析成 (alias, route 名, chain 规格, policy, 错误体)。

        解析顺序（与 README 的接口文档逐条对应）：
          1. `model` 命中某个 route 名 → 用该 route 的 chain + policy；
          2. `model` 形如 `provider/model` 且该模型存在 → 精确点名（只打这一个候选）；
          3. `model` 缺失/空 → 用 `default_alias`（再按 1/2 解析）；
          4. 都不命中 → 返回 400 错误体，body 里列出可用 route 名与可点名的 provider/model。
        注意：error 为 None 表示解析成功；alias 始终是「原样记进 usage 的调用方别名」。
        """
        raw = model.strip() if isinstance(model, str) else ""
        if not raw:
            raw = str(self.cfg.get("default_alias") or "default")
        routes = self.cfg.get("routes")
        routes = routes if isinstance(routes, dict) else {}
        if raw in routes:
            spec = routes.get(raw) if isinstance(routes.get(raw), dict) else {}
            chain = spec.get("chain", "auto")
            policy = spec.get("policy") if isinstance(spec.get("policy"), dict) else {}
            if chain != "auto" and not isinstance(chain, list):
                return raw, raw, "auto", {}, self._route_config_error(
                    f"route「{raw}」的 chain 既不是 \"auto\" 也不是模型名数组")
            if isinstance(chain, list):
                chain = [str(x) for x in chain]
            return raw, raw, chain, dict(policy), None
        if raw in self.models:
            # 精确点名：只按列表顺序取这一个候选（不走全局链）
            return raw, None, [raw], {}, None
        return raw, None, None, None, self._unknown_model_error(raw)

    def _route_config_error(self, why: str) -> dict:
        return {"error": {"message": f"llm-relay: {why}", "type": "relay_bad_request"}}

    def _unknown_model_error(self, raw: str) -> dict:
        routes = self.route_names()
        models = self.exact_model_names()
        return {"error": {
            "message": (f"llm-relay: 未知 model「{raw}」；可用 route：{routes or '（无）'}；"
                        f"可点名 provider/model：{models}"),
            "type": "relay_unknown_model",
            "routes": routes,
            "models": models,
        }}

    # ------------------------------------------------- 调用方鉴权与配额（T1.1/T1.4）
    def auth_mode(self) -> str:
        """auth.mode：loopback_trust（默认，本机不带 key 也算 anonymous）/ require_key。"""
        mode = str((self.cfg.get("auth") or {}).get("mode") or "loopback_trust")
        return mode if mode in ("loopback_trust", "require_key") else "loopback_trust"

    def _caller_key(self, ccfg: object) -> str:
        """取某个调用方的 key：先内联 `key`（只该出现在不提交的 config.json），再 `key_env`（keys.env）。"""
        if not isinstance(ccfg, dict):
            return ""
        inline = ccfg.get("key")
        if isinstance(inline, str) and inline:
            return inline
        env = str(ccfg.get("key_env") or "")
        return str(self.keys_raw.get(env) or "") if env else ""

    def resolve_caller(self, authorization: str, is_loopback: bool) -> tuple[str, int, dict]:
        """把 Authorization 头解析成调用方名字：返回 (名字, 错误状态码, 错误体)。

        1. Bearer == local_token（旧的运维令牌）→ anonymous；
        2. Bearer 命中某个 caller 的 key（内联 key 或 key_env）→ 该 caller（定长比较，防时序侧信道）；
        3. 带了 Bearer 但谁都不匹配 → 401 relay_auth；
        4. 没带 Bearer：loopback + auth.mode=loopback_trust + **没配 local_token** → anonymous；其余 → 401。
           （配了 local_token 就仍然要求带钥匙，保持旧语义不放松。）

        兼容优先：**根本没配 `callers`（或空表）时，Authorization 一律忽略**（认成 anonymous）——
        这正是「缺失 callers 段 = 行为逐项等于今天」，Hindsight 那个 `Bearer local-relay` 不会因为
        主人删掉 callers 段而被 401。只有显式配了 caller 才启用「不认识就 401」。
        """
        got = str(authorization or "").strip()
        token = got[7:].strip() if got.lower().startswith("bearer ") else ""
        local = str(self.cfg.get("local_token") or "")
        if token and local and hmac.compare_digest(token, local):
            return "anonymous", 0, {}
        callers = self.cfg.get("callers")
        callers = callers if isinstance(callers, dict) else {}
        if token:
            for name, ccfg in callers.items():
                k = self._caller_key(ccfg)
                if k and hmac.compare_digest(token, k):
                    return str(name), 0, {}
            if not callers and is_loopback and self.auth_mode() == "loopback_trust":
                # 没启用调用方体系：与今天一致，忽略 Authorization（但非 loopback 仍然拒）
                return "anonymous", 0, {}
            self.log(f"调用方鉴权失败：Bearer 与 {len(callers)} 个已配置 caller 都不匹配 → 401")
            return "", 401, self._auth_error("未知的调用方 key")
        if is_loopback and not local and self.auth_mode() == "loopback_trust":
            return "anonymous", 0, {}
        self.log(f"调用方鉴权失败：缺少 Bearer（auth.mode={self.auth_mode()}，loopback={is_loopback}，"
                 f"local_token={'已配' if local else '未配'}）→ 401")
        return "", 401, self._auth_error("缺少调用方 key")

    def _auth_error(self, why: str) -> dict:
        callers = self.cfg.get("callers")
        names = sorted(str(k) for k in callers) if isinstance(callers, dict) else []
        return {"error": {
            "message": f"llm-relay: {why}；请用 Authorization: Bearer <key> 带调用方 key",
            "type": "relay_auth", "auth_mode": self.auth_mode(), "callers": names,
        }}

    def check_caller(self, caller: str, route_key: str) -> tuple[bool, int, dict, float]:
        """调用方授权 + 配额：返回 (是否放行, 状态码, 错误体, 建议 Retry-After 秒)。

        · anonymous / probe / 未配置的 caller：不设限（保持今天本机免密的行为）；
        · allow_routes 非空列表时只允许列表里的 route 名（`provider/model` 精确点名用原样的
          `provider/model` 当 route key），越权 → 403 relay_forbidden；
        · rpm / daily_tokens 任一超限 → 429（relay_caller_rate_limit / relay_caller_quota），
          **绝不回 502**，并给 Retry-After。
        """
        callers = self.cfg.get("callers")
        if not caller or caller in ("anonymous", "probe") or not isinstance(callers, dict):
            return True, 0, {}, 0.0
        ccfg = callers.get(caller)
        if not isinstance(ccfg, dict):
            return True, 0, {}, 0.0
        allowed = ccfg.get("allow_routes")
        if isinstance(allowed, list):
            names = [str(x) for x in allowed]
            if route_key not in names:
                self.log(f"调用方「{caller}」越权：route「{route_key}」不在 allow_routes {names} → 403")
                return False, 403, {"error": {
                    "message": f"llm-relay: 调用方「{caller}」无权使用 route「{route_key}」",
                    "type": "relay_forbidden", "allow_routes": names}}, 0.0
        reason, retry = self.callers.allow(caller, ccfg)
        if reason == "rpm":
            self.log(f"调用方「{caller}」触发 rpm={ccfg.get('rpm')} 限流，约 {retry:.0f}s 后重试 → 429")
            return False, 429, {"error": {
                "message": (f"llm-relay: 调用方「{caller}」超过 rpm={ccfg.get('rpm')} 限流，"
                            f"约 {retry:.0f}s 后重试"),
                "type": "relay_caller_rate_limit", "code": "relay_caller_rate_limit",
                "retry_after_s": round(retry, 1)}}, retry
        if reason == "quota":
            self.log(f"调用方「{caller}」当日 token 预算用尽（上限 {ccfg.get('daily_tokens')}）→ 429")
            return False, 429, {"error": {
                "message": (f"llm-relay: 调用方「{caller}」今日 token 预算已用尽"
                            f"（上限 {ccfg.get('daily_tokens')}）"),
                "type": "relay_caller_quota", "code": "relay_caller_quota",
                "retry_after_s": 60.0}}, 60.0
        return True, 0, {}, 0.0

    def admin_callers(self) -> dict:
        """GET /admin/callers：调用方只读视图 —— 只给「是否配置 key + 指纹」，**永不回明文 key**。

        没有 `callers` 段时回 `{"configured": false}`（面板据此隐藏「调用方」tab）。
        """
        self.maybe_reload()
        callers = self.cfg.get("callers")
        if not isinstance(callers, dict) or not callers:
            return {"configured": False, "auth_mode": self.auth_mode(), "callers": []}
        out = []
        for name, ccfg in callers.items():
            ccfg = ccfg if isinstance(ccfg, dict) else {}
            key = self._caller_key(ccfg)
            src = "inline" if isinstance(ccfg.get("key"), str) and ccfg.get("key") else (
                f"key_env:{ccfg.get('key_env')}" if ccfg.get("key_env") else "none")
            out.append({
                "name": str(name),
                "key_source": src,
                "key_set": bool(key),
                "key_fp": sha8(key) if key else "-",
                "rpm": ccfg.get("rpm"),
                "rpm_used_last_min": self.callers.rpm_used(str(name)),
                "daily_tokens": ccfg.get("daily_tokens"),
                "tokens_today": self.callers.tokens_today(str(name)),
                "allow_routes": ccfg.get("allow_routes"),
                "recent_requests": sum(1 for r in self.recent if r.get("caller") == str(name)),
                "note": ccfg.get("note"),
            })
        return {"configured": True, "auth_mode": self.auth_mode(), "callers": out}

    def build_candidates(self, wants_schema: bool, wants_tools: bool, *,
                         chain: object = "auto", policy: dict | None = None,
                         limit: int | None = None) -> list[Candidate]:
        """构造候选列表。

        **顺序必须是：先过滤 → 再按 chain 升序排序 → 最后才取前 `max_candidates`。**
        反过来（先截断再在尝试阶段跳过冷却候选）会让可用的备胎永远进不了列表：
        2026-09-13 断网事故里 chain 1/2 同属正在冷却的 sensenova、chain 3 未启用，
        于是 chain 6 的 zen 与 chain 7 的 nemotron 根本没进过列表 → 端点一直 502。

        过滤条件：provider enabled / 有可用 key / provider 与 key 都不在冷却 /
        模型未 disabled / 能力匹配（wants_schema、wants_tools）/ 本地兜底在窗口内且健康检查通过。
        过滤后不足 `max_candidates` 就直接用剩下的候选，不报错。

        T1.2 的命名路由只改「挑选与排序」，不改上面这批过滤：
          · `chain="auto"`（默认）= **与旧版逐项同序**：过滤后按 (chain, cap_rank, order) 升序，再截断；
          · `chain=[...]` = 只按列表顺序取候选，未列出的不参与；列表里不存在/被过滤的名字
            记进 `last_candidates_info["skipped"]`（不存在的模型还会写日志）；
          · `policy.free_only=true` = 只保留显式 `free: true`（provider 级或模型级）的候选；
          · `limit` 缺省取 `request.max_candidates`，命名路由的 `policy.max_candidates` 由调用方传进来。

        被跳过的条目及原因记在 `self.last_candidates_info`，并写进日志（不含 key，只有 provider/model/chain）。
        """
        policy = policy if isinstance(policy, dict) else {}
        # 注意：下面的 provider 循环会复用 `chain` 作为「当前模型的 chain 整数」，
        # 所以先把路由规格另存一份，别让循环变量把它冲掉。
        chain_spec = chain
        now = time.time()
        picked: list[Candidate] = []
        skipped: list[tuple[str, int, str, str]] = []  # (名字, chain, 原因, 分类)
        cooldowns: list[float] = []

        def skip(tag: str, chain: int, reason: str, klass: str = self.SKIP_OTHER,
                 reset_s: float | None = None) -> None:
            skipped.append((tag, chain, reason, klass))
            if reset_s is not None and reset_s > 0:
                cooldowns.append(float(reset_s))

        for pi, pcfg in enumerate(self.cfg.get("providers", [])):
            name = str(pcfg.get("name") or "?")
            rt = self.providers.get(name)
            for mi, mcfg in enumerate(pcfg.get("models", [])):
                mid = mcfg.get("id") or ""
                chain = int(mcfg.get("chain", 99))
                tag = f"{name}/{mid}"
                caps = dict(mcfg.get("caps") or {})
                if rt is None:
                    skip(tag, chain, "provider 不存在")
                    continue
                if not bool(pcfg.get("enabled", True)):
                    skip(tag, chain, "provider 未启用（enabled=false）", self.SKIP_RETRYABLE)
                    continue
                if rt.missing_key:
                    skip(tag, chain, "provider 无可用 key（keys.env 里未定义）", self.SKIP_RETRYABLE)
                    continue
                if not rt.active:
                    skip(tag, chain, "provider 不可用（active=false）", self.SKIP_RETRYABLE)
                    continue
                ms = self.models.get(tag)
                if ms is not None and ms.disabled:
                    skip(tag, chain, f"模型已禁用（{ms.disabled_reason or '原因未记录'}）", self.SKIP_RETRYABLE)
                    continue
                left = rt.cooldown_left(now)
                if left > 0:
                    skip(tag, chain,
                         f"provider 冷却中（{left:.0f}s：{rt.cooldown_reason or '原因未记录'}）",
                         self.SKIP_RETRYABLE, left)
                    continue
                # P0.6/R3.3：provider 可用性必须按「当前候选的模型」判断。模型额度是按模型
                # 独立的，用无参 has_usable_key() 会让 flash-lite 因为同 key 在给 deepseek
                # 陪葬而被整个 provider 过滤掉（2026-09-14 事故的根因之一）。
                if not rt.has_usable_key(mid):
                    live = [k for k in rt.keys if not k.disabled]
                    key_lefts = [v for v in (k.cooldown_left(now) for k in live) if v > 0]
                    model_lefts = [v for v in (k.model_cooldown_left(mid, now) for k in live) if v > 0]
                    lefts = key_lefts + model_lefts
                    why = "本 provider 的 key 全在冷却/禁用"
                    if model_lefts:
                        why = (f"本 provider 的 key 对该模型都不可用（{mid} 429 冷却中，"
                               f"最短 {min(model_lefts):.0f}s 后恢复）")
                    elif key_lefts:
                        why = f"本 provider 的 key 全在冷却（最短 {min(key_lefts):.0f}s 后恢复）"
                    skip(tag, chain, why, self.SKIP_RETRYABLE, min(lefts) if lefts else None)
                    continue
                js = caps.get("json_schema", "native")
                if wants_schema and js is False:
                    skip(tag, chain, "声明不支持 json_schema，但本次请求要 schema")
                    continue
                if wants_tools and not caps.get("tools"):
                    skip(tag, chain, "声明不支持 tools，但本次请求带 tools")
                    continue
                cap_rank = 0
                if wants_schema:
                    cap_rank = 0 if js == "native" else 1
                picked.append(Candidate(
                    provider=name, model_id=mid, chain=chain,
                    json_schema=js, caps=caps, params=dict(mcfg.get("params") or {}),
                    cap_rank=cap_rank, health=0, order=(pi, mi),
                    free=bool(pcfg.get("free")) or bool(mcfg.get("free")),
                ))

        lf = self.cfg.get("local_fallback") or {}
        if lf.get("enabled"):
            lmodel = str(lf.get("model") or "local-mlx")
            ltag = f"__local__/{lmodel}"
            if not in_window(str(lf.get("window") or "")):
                skip(ltag, 9999, f"本地兜底不在时间窗口（{lf.get('window')}）")
            elif not self._local_ready():
                skip(ltag, 9999, "本地兜底健康检查未通过")
            else:
                picked.append(Candidate(
                    provider="__local__", model_id=lmodel, chain=9999,
                    json_schema="degradable", caps={"json_schema": "degradable", "tools": False},
                    params={}, cap_rank=(1 if wants_schema else 0), health=0, order=(9999, 0),
                    free=bool(lf.get("free")),
                ))

        # free_only：只留显式标记 free=true 的候选（缺失 = 不是免费档，绝不猜模型名）
        if policy.get("free_only"):
            kept_free: list[Candidate] = []
            for c in picked:
                if c.free:
                    kept_free.append(c)
                else:
                    skip(f"{c.provider}/{c.model_id}", c.chain,
                         "free_only：未显式标记 free=true（免费与否需主人确认）")
            picked = kept_free

        if isinstance(chain_spec, list):
            # 显式列表：只按列表顺序取候选，未列出的不参与。
            by_name = {f"{c.provider}/{c.model_id}": c for c in picked}
            ordered: list[Candidate] = []
            for item in chain_spec:
                nm = str(item)
                cand = by_name.get(nm)
                if cand is None:
                    reason = ("显式 chain 列出的模型不存在" if nm not in self.models
                              else "显式 chain 列出的模型被过滤（原因见同名条目）")
                    skip(nm, 99, reason)
                    continue
                ordered.append(cand)
            out_all = ordered
        else:
            picked.sort(key=lambda c: (c.chain, c.cap_rank, c.order))
            out_all = picked

        if limit is None:
            limit = int((self.cfg.get("request") or {}).get("max_candidates", 3) or 3)
        limit = max(1, int(limit))
        out = out_all[:limit]
        self.last_candidates_info = {
            "picked": out,
            "truncated": out_all[limit:],
            "skipped": skipped,
            "reset_s": (min(cooldowns) if cooldowns else None),
            "chain": chain_spec,
        }
        self._log_candidates(out, out_all[limit:], skipped)
        return out

    def _log_candidates(self, out: list[Candidate], truncated: list[Candidate],
                        skipped: list[tuple[str, int, str, str]]) -> None:
        """把「完整候选列表 + 每个被跳过的条目及原因」写进日志，方便下次故障一眼定位。

        只打印 provider/model/chain 与原因，**绝不打印 key**（redact 兜底）。
        """
        shown = "、".join(f"{c.provider}/{c.model_id}(chain{c.chain})" for c in out) or "（空）"
        parts = [f"候选列表: {shown}"]
        if skipped:
            parts.append("跳过 " + "；".join(
                f"{tag}(chain{chain}): {reason}" for tag, chain, reason, _ in skipped))
        if truncated:
            parts.append("超出 max_candidates 未进入列表: " + "、".join(
                f"{c.provider}/{c.model_id}(chain{c.chain})" for c in truncated))
        self.log("｜".join(parts))

    def _acquire(self, rt: ProviderRuntime, deadline: float) -> tuple[bool, str]:
        """拿并发 + rpm 令牌：返回 (是否拿到, 失败原因)。原因 ∈ {"", "queue", "rpm_limit"}。"""
        req = self.cfg.get("request") or {}
        qto = float(req.get("queue_timeout_s", 30) or 30)
        left = min(qto, max(0.05, deadline - time.time()))
        if not self.global_sem.acquire(timeout=left):
            self.bump("queue_timeout")
            rt.bump("queue_timeout")
            rt.bump("global_queue_timeout")
            return False, "queue"
        prov_ok = False
        try:
            left = min(qto, max(0.05, deadline - time.time()))
            if not rt.sem.acquire(timeout=left):
                self.bump("queue_timeout")
                rt.bump("queue_timeout")
                return False, "queue"
            prov_ok = True
            got, waited = rt.rpm_wait(deadline, qto)
            if not got:
                rt.rpm_blocked += 1
                self.bump("queue_timeout")
                rt.bump("rpm_queue_timeout")
                self.bump("rpm_throttle")
                rt.bump("rpm_throttle")
                self.log(f"本地 rpm 限流生效：{rt.name} rpm={rt.rpm}，最近 60s 已用 "
                         f"{rt.rpm_used()}/{rt.rpm}，排队超过 queue_timeout={qto:.0f}s "
                         f"→ 本候选退避 {rt.rpm_reset_s():.0f}s，不放行给上游")
                return False, "rpm_limit"
            if waited > 0.05:
                self.bump("rpm_throttle")
                rt.bump("rpm_throttle")
                self.log(f"本地 rpm 限流生效：{rt.name} rpm={rt.rpm}，最近 60s 已用 "
                         f"{rt.rpm_used()}/{rt.rpm}，已排队 {waited:.2f}s 后再打上游")
            return True, ""
        finally:
            if not prov_ok:
                self.global_sem.release()

    def _release(self, rt: ProviderRuntime) -> None:
        try:
            rt.sem.release()
        finally:
            self.global_sem.release()

    # ---------------------------------------------------------------- 上游调用
    def _provider_cfg(self, name: str) -> dict:
        for p in self.cfg.get("providers", []):
            if p.get("name") == name:
                return p
        return {}

    def _base_url(self, provider: str) -> str:
        if provider == "__local__":
            return str((self.cfg.get("local_fallback") or {}).get("base_url") or "").rstrip("/")
        return str(self._provider_cfg(provider).get("base_url") or "").rstrip("/")

    def build_upstream_body(self, payload: dict, provider: str, opts: SendOpts) -> dict:
        body = {k: v for k, v in payload.items() if k != "extra_body"}
        eb = payload.get("extra_body")
        if isinstance(eb, dict):
            for k, v in eb.items():
                if k == "extra_body" and isinstance(v, dict):
                    body.update(v)
                else:
                    body[k] = v
        body["model"] = opts.model_id
        wire = (self._provider_cfg(provider).get("wire") or {}) if provider != "__local__" else {}
        want = str(wire.get("max_tokens_param") or "max_tokens")
        alt = "max_completion_tokens" if want == "max_tokens" else "max_tokens"
        if alt in body:
            body[want] = body.pop(alt)
        if opts.strip_temp:
            body.pop("temperature", None)
        if opts.max_tokens_mult and isinstance(body.get("max_tokens"), int):
            body["max_tokens"] = body["max_tokens"] * int(opts.max_tokens_mult)
        # R5：带 json_schema 的请求若 max_tokens 太小，思考模型会把预算烧在 reasoning 里
        # （上游 finish_reason=length，content 只剩 schema 回显/半句思考）→ 只抬不降。
        floor = int(opts.min_max_tokens or 0)
        want_schema, _, _ = req_flags(payload)
        cur = body.get(want)
        if (floor > 0 and want_schema and isinstance(cur, int) and not isinstance(cur, bool)
                and cur < floor):
            body[want] = floor
            opts.max_tokens_raised = True
            opts.raised_from, opts.raised_to = cur, floor
        if opts.degrade:
            body.pop("response_format", None)
            body["messages"] = append_schema_prompt(body.get("messages"), opts.schema)
        return body

    def _post_upstream(self, provider: str, key_value: str, body: dict, timeout: float) -> UpstreamResult:
        url = self._base_url(provider) + "/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": UA,
            "Accept": "application/json",
        }
        if key_value:
            headers["Authorization"] = f"Bearer {key_value}"
        if provider != "__local__":
            headers.update((self._provider_cfg(provider).get("wire") or {}).get("extra_headers") or {})
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        t0 = time.time()
        try:
            with OPENER.open(urllib.request.Request(url, data=data, headers=headers, method="POST"), timeout=timeout) as r:
                raw = r.read()
                return UpstreamResult(r.status, raw, {k.lower(): v for k, v in r.headers.items()}, time.time() - t0)
        except urllib.error.HTTPError as e:
            raw = b""
            try:
                raw = e.read()
            except Exception:  # noqa: BLE001
                pass
            return UpstreamResult(e.code, raw, {k.lower(): v for k, v in (e.headers or {}).items()}, time.time() - t0)
        except Exception as e:  # noqa: BLE001  ← 超时/连接失败
            return UpstreamResult(0, b"", {}, time.time() - t0, error=f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------- 主流程
    def chat(self, payload: dict, client: str = "", auth: str = "") -> tuple[int, object, dict]:
        self.maybe_reload()
        t0 = time.time()
        req_cfg = self.cfg.get("request") or {}
        budget = float(req_cfg.get("total_budget_s", 100) or 100)
        deadline = t0 + budget
        wants_schema, wants_json, wants_tools = req_flags(payload)
        meta: dict = {
            "alias": None, "caller": auth or "anonymous", "provider": None, "model": None,
            "key_index": None, "key_fp": None,
            "attempts": [], "degraded": False, "promoted": False, "json_extracted": False,
            "max_tokens_raised": False, "status": None, "latency_s": None,
        }
        self.bump("total")
        # 路由解析（T1.2）。`model="probe"` 是内部旁路（面板能力实测/内部探针）：
        # 不参与路由解析、不吃调用方配额，仍走全局 auto 链（= 今天的行为）。
        probe = str(payload.get("model") or "").strip() == "probe"
        if probe:
            alias, route_name, chain_spec, policy = "probe", None, "auto", {}
        else:
            alias, route_name, chain_spec, policy, route_err = self.resolve_request_route(
                payload.get("model"))
            meta["alias"] = alias
            if route_err is not None:
                meta["status"] = 400
                self.bump("unknown_model")
                self.log(f"路由解析失败：未知 model「{alias}」→ 400（可用 route：{self.route_names()}）")
                self._record_recent(meta, t0, 400, {})
                return 400, route_err, meta
        meta["alias"] = alias
        # 调用方授权 + 配额（T1.1/T1.4）：probe 与 anonymous 不设限；被拒的请求不产生 token 统计，
        # 但要在 recent/usage.jsonl 里留下 caller 与 verdict，方便审计。
        if not probe:
            ok, ccode, cbody, cretry = self.check_caller(
                str(meta["caller"]), route_name or alias)
            if not ok:
                meta["status"] = ccode
                meta["retry_after"] = cretry
                meta["verdict"] = "caller_forbidden" if ccode == 403 else (
                    "caller_rate_limit" if "rate_limit" in str(cbody.get("error", {}).get("type")) else
                    "caller_quota")
                self.bump("caller_denied")
                self._record_recent(meta, t0, ccode, {})
                return ccode, cbody, meta
        # policy 只允许覆盖 request.* 的这 4 个同名键（max_candidates / validate_json /
        # schema_min_max_tokens / free_only），别的一律忽略。
        eff_cfg = dict(req_cfg)
        for k in ("max_candidates", "validate_json", "schema_min_max_tokens"):
            if k in policy:
                eff_cfg[k] = policy[k]
        if payload.get("stream") is True:
            return self._stream_chat(payload, wants_schema, wants_tools, deadline, t0, meta,
                                     chain_spec, eff_cfg, policy)
        cands = self.build_candidates(wants_schema, wants_tools, chain=chain_spec,
                                      policy=policy, limit=eff_cfg.get("max_candidates"))
        if not cands:
            self.bump("no_candidates")
            info = self.last_candidates_info or {}
            retryable = [s for s in (info.get("skipped") or []) if s[3] == self.SKIP_RETRYABLE]
            if retryable:
                # D3：候选全都只是「冷却中/禁用」（不是网络故障）→ 回 429 + reset，
                # 让 Hindsight 走 ProviderRateLimitResetError 优雅退避，而不是记一条 InternalServerError。
                reset_s = self._cooldown_reset_s(info)
                self.bump("cooldown_429")
                self.log(f"所有候选暂不可用（冷却/禁用，{len(retryable)} 个），回 429 并建议约 {reset_s:.0f}s 后重试")
                meta["status"] = 429
                meta["retry_after"] = reset_s
                body = self._cooldown_body(
                    reset_s, f"llm-relay: 所有候选都不可用（冷却/禁用），约 {reset_s:.0f}s 后恢复")
                self._record_recent(meta, t0, 429,
                                    {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                return 429, body, meta
            meta["status"] = 503
            hint = self._no_candidates_hint(info)
            self._record_recent(meta, t0, 503, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
            return 503, {"error": {"message": "llm-relay: 没有可用候选（全部 disabled / 冷却中 / 能力不匹配）"
                                              + hint,
                                   "type": "relay_no_candidates"}}, meta
        last: UpstreamResult | None = None
        for ci, cand in enumerate(cands):
            if time.time() >= deadline:
                self.bump("budget_exceeded")
                self.log(f"总预算 {budget:.0f}s 用尽，放弃剩余候选（alias={alias}）")
                break
            outcome, res, norm_body = self._attempt_candidate(cand, payload, deadline, meta,
                                                             wants_json, req_cfg,
                                                             remaining_candidates=len(cands) - ci)
            if outcome == "ok" and norm_body is not None:
                meta["status"] = 200
                self.bump("ok")
                ms = self.models.get(f"{cand.provider}/{cand.model_id}")
                if ms:
                    ms.bump("ok")
                self._record_recent(meta, t0, 200, norm_body.get("usage") or {})
                norm_body["_relay"] = {
                    "provider": cand.provider, "model": cand.model_id,
                    "key_index": meta["key_index"], "key_fp": meta["key_fp"],
                    "alias": alias, "latency_s": round(time.time() - t0, 3),
                    "attempts": len(meta["attempts"]), "degraded": meta["degraded"],
                    "promoted_reasoning": meta["promoted"],
                }
                return 200, norm_body, meta
            if res is not None:
                last = res
        self.bump("fail")
        if last is not None and last.status == 200:
            # R5（P0.5，修遗留项 5.3）：上游回了 200，但 content 被判 schema_echo / schema_mismatch /
            # bad_json → 绝不能走「原样返回最后一条上游 200 体」的兜底分支。底线是：只要 content
            # 不合格，Hindsight 就永远拿不到它。所有候选都不合格 → 502 relay_unusable_content。
            self.bump("unusable_content_rejected")
            meta["status"] = 502
            degraded_attempts = [a for a in meta["attempts"] if a.get("degrade")]
            if degraded_attempts:
                # R3（P0.7）：最终拒绝时也要能看出「降级路径 + R1 判不过」各发生了几次
                meta["degraded_rejected"] = len(degraded_attempts)
                self.log(f"其中 {len(degraded_attempts)} 次走的是降级路径：降级只改变怎么向上游要 "
                         f"JSON，不改变期望形状，所以这些 content 照样按 R1 判不过丢弃")
            self._record_recent(meta, t0, 502, {})
            self.log(f"所有候选返回的内容都不合格（{len(meta['attempts'])} 次尝试），"
                     f"按 {502} relay_unusable_content 拒绝，不回传上游 200 体（alias={alias}）")
            return 502, {"error": {
                "message": "llm-relay: 所有候选返回的内容都不合格（schema 回显 / 不符合 schema / "
                           "无法解析 JSON），已全部丢弃；绝不把不合格内容当成功交出去",
                "type": "relay_unusable_content",
                "code": "relay_unusable_content",
                "attempts": len(meta["attempts"]),
                "degraded_attempts": len([a for a in meta["attempts"] if a.get("degrade")]),
                "reasons": sorted({str(a.get("normalize")) for a in meta["attempts"] if a.get("normalize")}),
            }}, meta
        if last is not None and last.status:
            raw_text = last.text()
            try:
                err_body = json.loads(raw_text)
            except Exception:  # noqa: BLE001
                err_body = {"error": {"message": raw_text[:600] or last.error or "upstream failed",
                                      "type": "relay_upstream_error"}}
            if not isinstance(err_body, dict):
                err_body = {"error": {"message": str(err_body)[:600], "type": "relay_upstream_error"}}
            err_body.setdefault("_relay", {})
            if isinstance(err_body["_relay"], dict):
                err_body["_relay"].update({"alias": alias, "attempts": len(meta["attempts"]),
                                           "last_status": last.status, "latency_s": round(time.time() - t0, 3)})
            meta["status"] = last.status
            self._record_recent(meta, t0, last.status, {})
            return last.status, err_body, meta
        meta["status"] = 502
        self._record_recent(meta, t0, 502, {})
        return 502, {"error": {"message": "llm-relay: 所有候选都失败（无可用上游响应）",
                               "type": "relay_all_failed"}}, meta

    def _attempt_candidate(self, cand: Candidate, payload: dict, deadline: float, meta: dict,
                           wants_json: bool, req_cfg: dict,
                           remaining_candidates: int = 1) -> tuple[str, UpstreamResult | None, dict | None]:
        rt = self.providers.get(cand.provider)
        if rt is None:
            return "next", None, None
        ms = self.models.get(f"{cand.provider}/{cand.model_id}")
        validate_json = bool(req_cfg.get("validate_json", True))
        extract_fallback = bool(req_cfg.get("json_extract_fallback", True))
        wants_schema, _, _ = req_flags(payload)
        req_schema = request_schema(payload)
        schema_min_max_tokens = int(req_cfg.get("schema_min_max_tokens", 1024) or 0)
        # 候选本身是 degradable（或上游明确 400 拒绝 response_format）→ 去掉 response_format，schema 内嵌提示词
        degrade = bool(wants_schema and cand.json_schema == "degradable")
        opts = SendOpts(model_id=cand.model_id, alias=meta["alias"])
        strip_temp = bool(cand.params.get("strip_temperature")) or bool(ms and ms.strip_temperature_forced)
        mult = cand.params.get("max_tokens_multiplier")
        retries = 2
        last_res: UpstreamResult | None = None
        while True:
            if rt.cooldown_left() > 0:
                return "next", last_res, None
            adapted: str | None = None
            keys_exhausted = False
            synth_429: UpstreamResult | None = None
            while True:
                if time.time() >= deadline:
                    return "next", last_res, None
                ks = rt.next_key_slot(cand.model_id)
                if ks is None:
                    keys_exhausted = True
                    synth_429 = self._cooldown_429(rt, cand.model_id)
                    break
                acquired, why = self._acquire(rt, deadline)
                if not acquired:
                    if why == "rpm_limit":
                        # R4：本地 rpm 限流真拦住了这次请求（没打上游）。给 Hindsight 一个带 reset 的
                        # 429 让它优雅退避，而不是把请求放过去让上游回 429。
                        reset = max(1.0, rt.rpm_reset_s())
                        meta["retry_after"] = reset
                        return "next", self._local_rpm_429(rt, reset), None
                    return "next", last_res, None
                opts.degrade = degrade
                opts.strip_temp = strip_temp
                opts.max_tokens_mult = mult
                opts.schema = req_schema
                opts.min_max_tokens = schema_min_max_tokens
                body = self.build_upstream_body(payload, cand.provider, opts)
                if opts.max_tokens_raised and not meta.get("max_tokens_raised"):
                    meta["max_tokens_raised"] = True
                    # R5：只记数值，不记 key（日志一律过 redact 兜底）
                    self.log(f"json_schema 请求的 max_tokens={opts.raised_from} 偏小 → 抬到 "
                             f"{opts.raised_to}（防思考模型把预算烧在 reasoning 里）")
                # R1：思考模型（能力声明 reasoning_only=true，或历史响应带过 reasoning 字段）
                # 给一个不被 clamp 压到 45s 以下的硬下限；普通模型仍是 25→45s 的预算感知分配。
                reasoning = bool(cand.caps.get("reasoning_only")) or bool(ms and ms.saw_reasoning)
                timeout = self._attempt_timeout(req_cfg, deadline, remaining_candidates,
                                                reasoning=reasoning)
                self.log(f"{cand.provider}/{cand.model_id} key#{ks.index}({ks.fp}) 单次超时预算 "
                         f"{timeout:.1f}s（剩余候选 {remaining_candidates}，"
                         f"思考模型={'是' if reasoning else '否'}，backoff_level={rt.backoff_level}）")
                try:
                    res = self._post_upstream(cand.provider, ks.value, body, timeout)
                finally:
                    self._release(rt)
                last_res = res
                ks.uses += 1
                kind = classify(res.status, res.text(), res.headers)
                self.bump(f"upstream_{kind}")
                rt.bump(kind)
                if ms is not None and kind != "http_ok":
                    ms.bump(kind)
                attempt = {
                    "provider": cand.provider, "model": cand.model_id, "key_index": ks.index,
                    "key_fp": ks.fp, "kind": kind, "http": res.status,
                    "latency_s": round(res.latency_s, 3), "timeout_s": round(timeout, 1),
                    "reasoning_timeout": reasoning,
                    # R3（P0.7）：每次尝试都记下是否走了降级路径，便于区分「降级 + R1 判不过」
                    "degrade": bool(degrade),
                }
                meta["attempts"].append(attempt)
                meta["provider"], meta["model"] = cand.provider, cand.model_id
                meta["key_index"], meta["key_fp"] = ks.index, ks.fp
                if kind == "http_ok":
                    try:
                        res.json_body = json.loads(res.text())
                    except Exception:  # noqa: BLE001
                        res.json_body = None
                    if ms is not None and self._body_has_reasoning(res.json_body):
                        # R1：这个模型会吐 reasoning → 下次给它更宽的超时预算
                        ms.saw_reasoning = True
                    # R1（P0.7，修静默抽空事实）：降级只改变「我们怎么向上游要 JSON」（剥掉
                    # response_format、把 schema 内嵌进提示词），**不改变「我们期望什么形状」**——
                    # 调用方（Hindsight）的严格 schema 在降级后一字不变。所以这里绝不能因为
                    # degrade 就把 schema 丢掉：一旦丢成 None，降级路径就跳过 R1 一致性校验，
                    # 会把 §0 那种「properties 片段回显」当成功 200 交出去（Hindsight 抽到 0 条事实）。
                    norm_schema = req_schema
                    norm_detail: list = []
                    ok, norm_body, reason = normalize_response(res.json_body, wants_json,
                                                               validate_json, extract_fallback,
                                                               norm_schema, norm_detail)
                    attempt["normalize"] = reason
                    if norm_detail:
                        attempt["normalize_detail"] = str(norm_detail[0])[:200]
                    if ok and norm_body is not None:
                        meta["degraded"] = bool(degrade)
                        meta["promoted"] = reason == "promoted"
                        meta["json_extracted"] = reason == "extracted"
                        if degrade and ms is not None:
                            ms.bump("degraded")
                        if reason == "promoted":
                            rt.bump("promoted_reasoning")
                        rt.reset_backoff()
                        if any(a["kind"] == "ratelimit" for a in meta["attempts"]):
                            rt.bump("recovered_after_429")
                        return "ok", res, norm_body
                    if ms is not None:
                        ms.bump("bad_json" if reason == "bad_json" else "unusable")
                    rt.bump("unusable_content")
                    self.bump("unusable_content")
                    if degrade:
                        # R3（P0.7）：降级路径判不过要能单独看出来 ——「降级只改变怎么向上游要
                        # JSON，不改变期望形状」，所以降级 + schema 判不过 = 上游给坏了（非我方放宽）。
                        self.bump("degraded_schema_rejected")
                        rt.bump("degraded_schema_rejected")
                        if ms is not None:
                            ms.bump("degraded_schema_rejected")
                    if reason == "schema_echo" and first_finish_reason(res.json_body) == "length":
                        # R5.3：思考模型把小 max_tokens 全烧在 reasoning 里，content 只剩 schema 回显
                        self.bump("schema_echo")
                    detail = f" detail={attempt['normalize_detail']}" if attempt.get("normalize_detail") else ""
                    path = "降级路径(已内嵌 schema，仍按 R1 校验)" if degrade else "原生 json_schema"
                    self.log(f"候选内容不可用（{cand.provider}/{cand.model_id} 路径={path} "
                             f"reason={reason}{detail}），换下一候选")
                    return "next", res, None
                if kind == "ratelimit":
                    ks.n429 += 1
                    dur = parse_reset_seconds(res.headers, res.text(), float(self._key_cooldown(cand.provider)))
                    # P0.6/R1+R2：模型额度按模型独立。默认只冷 (这把 key, 这个模型)，同 key 上
                    # 其他模型照常用；只有明确账号级/套餐级的 429 才冷整把 key（所有模型都停）。
                    if is_account_level_429(res.text(), res.headers):
                        ks.cooldown_until = max(ks.cooldown_until, time.time() + dur)
                        ks.cooldown_reason = f"429（账号级，{dur:.0f}s）"
                        self.log(f"{cand.provider} key#{ks.index}({ks.fp}) 429 账号级限流 → "
                                 f"整把 key 冷却 {dur:.0f}s（所有模型都停，先换同 provider 下一把 key）")
                    else:
                        ks.note_model_429(cand.model_id, dur, f"429（模型额度，{dur:.0f}s）")
                        self.log(f"{cand.provider} key#{ks.index}({ks.fp}) 429 模型级限流 → 只冷却 "
                                 f"({cand.model_id}) {dur:.0f}s，同 key 上其他模型照常用（不整池罚）")
                    # R3：一把 key 撞限只罚它自己；只有同 provider 的每把 key 在短窗口里都被
                    # 429/鉴权失败过（且确实不止一把 key），才让 provider 一起冷却。
                    if rt.note_key_failure(ks, self._provider_failure_window_s()) \
                            and len(rt.keys) >= 2:
                        self._enter_provider_cooldown(rt, cand, f"所有 key 在窗口内都被 429（{dur:.0f}s）")
                    continue
                if kind == "auth":
                    ks.n401 += 1
                    ks.disabled = True
                    ks.disabled_reason = f"HTTP {res.status} 鉴权失败"
                    self.log(f"{cand.provider} key#{ks.index}({ks.fp}) {res.status} → 该 key 禁用，换下一把")
                    if rt.note_key_failure(ks, self._provider_failure_window_s()) \
                            and len(rt.keys) >= 2:
                        self._enter_provider_cooldown(rt, cand, f"所有 key 在窗口内都鉴权失败（HTTP {res.status}）")
                    continue
                if kind == "quota":
                    ks.cooldown_until = max(ks.cooldown_until, time.time() + 3600)
                    ks.cooldown_reason = "额度耗尽 → 长冷却 ≥1h"
                    self.log(f"{cand.provider} key#{ks.index}({ks.fp}) 额度耗尽 → 长冷却 3600s，换 key")
                    continue
                if kind == "temp_invalid":
                    if not strip_temp and retries > 0:
                        strip_temp = True
                        if ms is not None:
                            ms.strip_temperature_forced = True
                        self.persist_param(cand.provider, cand.model_id, "strip_temperature", True)
                        retries -= 1
                        adapted = "temp"
                        break
                    return "next", res, None
                if kind == "rf_unsupported":
                    if not degrade and retries > 0:
                        degrade = True
                        retries -= 1
                        adapted = "degrade"
                        self.log(f"{cand.provider}/{cand.model_id} 400 拒绝 response_format → 降级为提示词内嵌 schema 重试")
                        break
                    return "next", res, None
                if kind == "model_gone":
                    if ms is not None:
                        ms.disabled = True
                        ms.disabled_reason = f"HTTP {res.status} 模型不可用/不存在"
                    self.log(f"模型 {cand.provider}/{cand.model_id} 标记 disabled（HTTP {res.status}）")
                    return "next", res, None
                if kind in ("server", "transport"):
                    self._enter_provider_cooldown(rt, cand, f"{kind} 错误（HTTP {res.status or 'N/A'}）")
                    return "next", res, None
                # bad_request / other：直接换下一候选
                return "next", res, None
            if adapted:
                continue
            # key 全在冷却 → 造一个带 reset 的 429，让 Hindsight 按 429 退避（别给它 502）
            return "next", (last_res or synth_429), None

    # -------------------------------------------------------------- 流式（尽力）
    def _stream_chat(self, payload: dict, wants_schema: bool, wants_tools: bool,
                     deadline: float, t0: float, meta: dict,
                     chain_spec: object = "auto", eff_cfg: dict | None = None,
                     policy: dict | None = None) -> tuple[int, object, dict]:
        eff_cfg = eff_cfg if isinstance(eff_cfg, dict) else (self.cfg.get("request") or {})
        cands = self.build_candidates(wants_schema, wants_tools, chain=chain_spec,
                                      policy=policy, limit=eff_cfg.get("max_candidates"))[:1]
        if not cands:
            return 503, {"error": {"message": "llm-relay: 无可用候选（stream）", "type": "relay_no_candidates"}}, meta
        cand = cands[0]
        rt = self.providers[cand.provider]
        ks = rt.next_key_slot(cand.model_id)
        acquired, why = self._acquire(rt, deadline) if ks is not None else (False, "queue")
        if ks is None or not acquired:
            if why == "rpm_limit":
                # R4：stream 路径也照样不回传，直接给带 reset 的 429
                reset = max(1.0, rt.rpm_reset_s())
                meta["status"] = 429
                meta["retry_after"] = reset
                return 429, self._local_rpm_body(rt, reset), meta
            return 503, {"error": {"message": "llm-relay: 并发/限流排队超时（stream）", "type": "relay_queue_timeout"}}, meta
        opts = SendOpts(model_id=cand.model_id, alias=meta["alias"],
                        degrade=bool(wants_schema and cand.json_schema == "degradable"),
                        strip_temp=bool(cand.params.get("strip_temperature")),
                        max_tokens_mult=cand.params.get("max_tokens_multiplier"),
                        schema=request_schema(payload),
                        min_max_tokens=int(eff_cfg.get(
                            "schema_min_max_tokens", 1024) or 0))
        body = self.build_upstream_body(payload, cand.provider, opts)
        if opts.max_tokens_raised:
            meta["max_tokens_raised"] = True
            self.log(f"json_schema 请求的 max_tokens={opts.raised_from} 偏小 → 抬到 "
                     f"{opts.raised_to}（stream）")
        url = self._base_url(cand.provider) + "/chat/completions"
        headers = {"Content-Type": "application/json", "User-Agent": UA, "Accept": "text/event-stream"}
        if ks.value:
            headers["Authorization"] = f"Bearer {ks.value}"
        if cand.provider != "__local__":
            headers.update((self._provider_cfg(cand.provider).get("wire") or {}).get("extra_headers") or {})
        try:
            stream_timeout = self._attempt_timeout(self.cfg.get("request") or {}, deadline, 1)
            resp = OPENER.open(urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(),
                                                      headers=headers, method="POST"),
                               timeout=stream_timeout)
        except Exception as e:  # noqa: BLE001
            self._release(rt)
            self.bump("stream_fail")
            meta["status"] = 502
            self._record_usage(meta, t0, 502, {})
            return 502, {"error": {"message": f"llm-relay: 流式上游连接失败 {type(e).__name__}", "type": "relay_stream_error"}}, meta
        meta["provider"], meta["model"], meta["key_index"], meta["key_fp"] = cand.provider, cand.model_id, ks.index, ks.fp
        meta["status"] = 200
        self.bump("stream")
        self._record_usage(meta, t0, 200, {})
        return 200, StreamBody(resp, rt), meta

    # ------------------------------------------------------- 参数适配写回 config
    def persist_param(self, provider: str, model_id: str, key: str, value: object) -> None:
        """把参数适配写回 config.json（写前先备份）。失败不影响主流程。"""
        try:
            tmp_cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            self.log(f"参数适配无法写回 config.json：{e}")
            return
        hit = False
        for p in tmp_cfg.get("providers", []):
            if p.get("name") != provider:
                continue
            for m in p.get("models", []):
                if m.get("id") == model_id:
                    m.setdefault("params", {})[key] = value
                    hit = True
        if not hit:
            return
        if any(k in str(provider) for k in ("__local__",)):
            return
        backup = self.config_path.with_name(
            self.config_path.name + ".bak." + time.strftime("%Y%m%d%H%M%S")
        )
        try:
            shutil.copy2(self.config_path, backup)
            tmp = self.config_path.with_name(self.config_path.name + ".tmp")
            tmp.write_text(json.dumps(tmp_cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.config_path)
            self._sig = self._sig_now()
            self.log(f"参数适配：{provider}/{model_id} 的 {key}={value} 已写回 config.json（备份 {backup.name}）")
        except Exception as e:  # noqa: BLE001
            self.log(f"写回 config.json 失败：{type(e).__name__}: {e}")

    # --------------------------------------------------------------- 统计/状态
    def _cooldown_body(self, reset_s: float, message: str) -> dict:
        """统一的"带 reset 的 429"响应体（Hindsight 读 retry_after_s / Retry-After 头退避）。"""
        return {
            "error": {
                "message": message,
                "type": "rate_limit_error",
                "code": "relay_key_cooldown",
                "retry_after_s": round(float(reset_s), 1),
            }
        }

    @staticmethod
    def _no_candidates_hint(info: dict) -> str:
        """零候选时的「明确原因」尾巴：命名路由/免费档筛掉全部候选时不能只回一句通用话。"""
        marks: list[str] = []
        reasons = [str(s[2]) for s in (info.get("skipped") or [])]
        if any("free_only" in r for r in reasons):
            marks.append("free_only 只保留显式 free=true 的候选，当前 0 个（其余 provider 待主人确认免费标记）")
        unknown = [str(s[0]) for s in (info.get("skipped") or []) if "不存在" in str(s[2])]
        if unknown:
            marks.append("显式 chain 里不存在的模型：" + "、".join(unknown))
        return ("；" + "；".join(marks)) if marks else ""

    def _cooldown_reset_s(self, info: dict) -> float:
        """候选全不可用时给 Hindsight 的退避秒数：优先用真实的冷却剩余，其余用 provider 的 key_cooldown_s。"""
        reset = info.get("reset_s")
        if isinstance(reset, (int, float)) and reset > 0:
            return max(1.0, float(reset))
        for pcfg in self.cfg.get("providers", []):
            v = pcfg.get("key_cooldown_s")
            if v:
                return max(1.0, float(v))
        return 60.0

    def _cooldown_429(self, rt: ProviderRuntime, model_id: str) -> UpstreamResult | None:
        """provider 的 key 对这个模型全在冷却时，造一个带 Retry-After 的 429（Hindsight 读 reset 退避）。

        P0.6/R3：必须按模型判断 —— 「这个模型在每把 key 上都被冷」才是这个模型真的没得用；
        同 provider 上别的模型可能仍然健康。
        """
        now = time.time()
        lefts = []
        for k in rt.keys:
            if k.disabled:
                continue
            left = max(k.cooldown_left(now), k.model_cooldown_left(model_id, now))
            if left > 0:
                lefts.append(left)
        if not lefts:
            return None
        left = min(lefts)
        body = self._cooldown_body(
            left, f"llm-relay: {rt.name} 的 key 对模型 {model_id} 都在冷却中，约 {left:.0f}s 后恢复")
        return UpstreamResult(429, json.dumps(body, ensure_ascii=False).encode(),
                              {"retry-after": f"{left:.0f}"}, 0.0)

    def _local_rpm_body(self, rt: ProviderRuntime, reset_s: float | None = None) -> dict:
        """R4：本地 rpm 限流生效时的 429 响应体（带 retry_after_s，Hindsight 按 429 退避）。"""
        reset = max(1.0, float(reset_s if reset_s is not None else rt.rpm_reset_s()))
        return {
            "error": {
                "message": f"llm-relay: 本地 rpm 限流生效（{rt.name} rpm={rt.rpm}，最近 60s 已用 "
                           f"{rt.rpm_used()}），约 {reset:.0f}s 后恢复",
                "type": "rate_limit_error",
                "code": "relay_local_rpm_limit",
                "retry_after_s": round(reset, 1),
            }
        }

    def _local_rpm_429(self, rt: ProviderRuntime, reset_s: float | None = None) -> UpstreamResult:
        """R4：本地 rpm 限流生效时的 429（UpstreamResult 形状，供 chat() 的控制流使用）。"""
        reset = max(1.0, float(reset_s if reset_s is not None else rt.rpm_reset_s()))
        body = self._local_rpm_body(rt, reset)
        return UpstreamResult(429, json.dumps(body, ensure_ascii=False).encode(),
                              {"retry-after": f"{reset:.0f}"}, 0.0)

    @staticmethod
    def _body_has_reasoning(up_body: object) -> bool:
        """响应里是否带过 reasoning_content/reasoning（R1 判断「思考模型」的历史证据）。"""
        if not isinstance(up_body, dict):
            return False
        choices = up_body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return False
        msg = choices[0].get("message")
        if not isinstance(msg, dict):
            return False
        return any(isinstance(msg.get(f), str) and msg.get(f).strip()
                   for f in ("reasoning_content", "reasoning"))

    def _provider_cooldown_limits(self) -> tuple[float, float, float, int]:
        """(单次软封顶, 连败硬封顶, 连败观察窗口, 抬到硬封顶所需次数)，全部可配置（R2/R3）。"""
        rc = self.cfg.get("request") or {}

        def num(key: str, default: float) -> float:
            try:
                v = float(rc.get(key, default))
            except (TypeError, ValueError):
                return default
            return v if v > 0 else default

        cap = num("provider_cooldown_cap_s", DEFAULT_PROVIDER_COOLDOWN_CAP_S)
        hard = max(cap, num("provider_cooldown_hard_cap_s", DEFAULT_PROVIDER_COOLDOWN_HARD_CAP_S))
        window = num("provider_failure_window_s", DEFAULT_PROVIDER_FAILURE_WINDOW_S)
        try:
            threshold = int(rc.get("provider_escalate_after_failures", DEFAULT_PROVIDER_ESCALATE_AFTER))
        except (TypeError, ValueError):
            threshold = DEFAULT_PROVIDER_ESCALATE_AFTER
        return cap, hard, window, max(1, threshold)

    def _provider_failure_window_s(self) -> float:
        return self._provider_cooldown_limits()[2]

    def _enter_provider_cooldown(self, rt: ProviderRuntime, cand: Candidate, reason: str) -> float:
        cap, hard, window, threshold = self._provider_cooldown_limits()
        dur = rt.enter_provider_cooldown(
            reason, float(self._provider_cooldown(cand.provider)),
            cap_s=cap, hard_cap_s=hard, escalate_window_s=window, escalate_after=threshold,
        )
        recent = len(rt.fail_events)
        self.log(f"provider {rt.name} 进入冷却 {dur:.0f}s（{reason}；backoff_level={rt.backoff_level}，"
                 f"窗口内连续失败 {recent} 次，单次封顶 {cap:.0f}s／连败 {threshold} 次封顶 {hard:.0f}s）"
                 f" → 换下一候选")
        return dur

    def _key_cooldown(self, provider: str) -> int:
        return int(self._provider_cfg(provider).get("key_cooldown_s", 120) or 120)

    def _provider_cooldown(self, provider: str) -> int:
        return int(self._provider_cfg(provider).get("provider_cooldown_s", 180) or 180)

    def _attempt_timeout(self, req_cfg: dict, deadline: float, remaining: int,
                         reasoning: bool = False) -> float:
        """预算感知的单次尝试超时（D2 修复 + P0.5/R1）。

        本次尝试超时 = clamp(剩余预算 / 剩余候选数, 8s, per_attempt_timeout_s)，
        再额外保证给后面 `min_attempts_within_budget` 个候选留出最小槽位（默认至少能试 2 次）。

        R1：`reasoning=True`（能力声明 reasoning_only 或历史响应带过 reasoning 字段）时，
        floor 与 cap 都抬到 `reasoning_min_timeout_s`（默认 45s），保证思考模型的单次超时
        不被 clamp 压到 45s 以下；但仍受剩余总预算与总预算下限约束。
        生产配置 per_attempt_timeout_s=45 + total_budget_s=100 时，45×2=90 ≤ 100，
        两次尝试一定装得进总预算。

        事故背景：`per_attempt_timeout_s`(60) × 3 候选 = 180s 远超 `total_budget_s`(100)，
        第一次超时就把预算吃掉一大半，备胎根本没机会试 —— 6 条 502 全是这么来的。
        现在剩余预算不足时，宁可提前缩短当前这次尝试的超时，也不放弃剩余候选。
        """
        now = time.time()
        left = max(0.0, deadline - now)
        n = max(1, int(remaining))
        reserve_floor = max(0.2, float(req_cfg.get("min_attempt_timeout_s", MIN_ATTEMPT_TIMEOUT_S)
                                       or MIN_ATTEMPT_TIMEOUT_S))
        floor_s = reserve_floor
        cap = max(0.2, float(req_cfg.get("per_attempt_timeout_s", 60) or 60))
        if reasoning:
            r_floor = max(0.2, float(req_cfg.get("reasoning_min_timeout_s",
                                                 REASONING_MIN_ATTEMPT_TIMEOUT_S)
                                     or REASONING_MIN_ATTEMPT_TIMEOUT_S))
            floor_s = max(floor_s, r_floor)
            cap = max(cap, r_floor)
        t = min(cap, max(floor_s, left / n))
        min_attempts = max(1, int(req_cfg.get("min_attempts_within_budget", 2) or 2))
        # 预留槽位用最小 floor（默认 8s），不能拿思考模型的 45s 下限去挤掉后面的候选
        reserve = reserve_floor * max(0, min(min_attempts, n) - 1)
        if left - reserve >= 0.2:
            t = min(t, left - reserve)
        else:
            t = min(t, left)  # 预算真的不够了：能用多少算多少，别放弃剩下的候选
        return max(0.2, t)

    def healthy_candidates(self) -> int:
        n = 0
        for pcfg in self.cfg.get("providers", []):
            rt = self.providers.get(pcfg.get("name"))
            if rt is None or not rt.active or rt.cooldown_left() > 0:
                continue
            # P0.6/R3：可用 key 也必须按模型判断 —— 某个模型在每把 key 上都被冷时，
            # 只该把那个模型从健康候选里去掉，同 provider 其他模型照样算健康。
            for m in pcfg.get("models", []):
                mid = str(m.get("id") or "")
                ms = self.models.get(f"{rt.name}/{mid}") or ModelState(rt.name, "")
                if ms.disabled:
                    continue
                if not rt.has_usable_key(mid):
                    continue
                n += 1
        if self._local_ready():
            n += 1
        return n

    def status(self) -> dict:
        self.maybe_reload()
        now = time.time()
        providers = []
        for name, rt in self.providers.items():
            pcfg = self._provider_cfg(name) if name != "__local__" else {"models": [], "enabled": True}
            models = []
            if name == "__local__":
                lf = self.cfg.get("local_fallback") or {}
                models.append({
                    "id": lf.get("model"), "chain": 9999, "disabled": not self._local_ready(),
                    "in_window": in_window(str(lf.get("window") or "")),
                    "counts": dict((self.models.get(f"__local__/{lf.get('model')}") or ModelState("", "")).counts),
                })
            for mcfg in pcfg.get("models", []):
                ms = self.models.get(f"{name}/{mcfg.get('id')}") or ModelState(name, str(mcfg.get("id")))
                models.append({
                    "id": mcfg.get("id"), "chain": mcfg.get("chain"),
                    "caps": mcfg.get("caps"), "params": mcfg.get("params") or {},
                    "disabled": ms.disabled, "disabled_reason": ms.disabled_reason,
                    "strip_temperature_forced": ms.strip_temperature_forced,
                    "saw_reasoning": ms.saw_reasoning,
                    "counts": dict(ms.counts),
                })
            cool_left = round(rt.cooldown_left(now), 1)
            providers.append({
                "name": name,
                "enabled": bool(pcfg.get("enabled", True)) and rt.active,
                "active": rt.active,
                "missing_key": rt.missing_key,
                "base_url": self._base_url(name),
                "cooldown_left_s": cool_left,
                # 冷却已结束就不再回显旧原因，避免误读
                "cooldown_reason": rt.cooldown_reason if cool_left > 0 else "",
                "backoff_level": rt.backoff_level,
                "recent_provider_failures": len(rt.fail_events),
                "rpm": rt.rpm,
                "rpm_used_last_min": rt.rpm_used(),
                "rpm_blocked": rt.rpm_blocked,
                "max_concurrency": getattr(rt.sem, "_initial_value", None),
                "counts": dict(rt.counts),
                "keys": [
                    {
                        "index": ks.index, "env": ks.env_name, "fp": ks.fp,
                        "disabled": ks.disabled, "disabled_reason": ks.disabled_reason,
                        "cooldown_left_s": round(ks.cooldown_left(now), 1),
                        "cooldown_reason": ks.cooldown_reason, "uses": ks.uses,
                        "n429": ks.n429, "n401": ks.n401,
                        # P0.6/R4：按模型的 429 冷却明细，一眼看出是哪个模型把这把 key 弄脏了
                        "model_cooldowns": ks.model_cooldown_details(now),
                    }
                    for ks in rt.keys
                ],
                "models": models,
            })
        rc = self.cfg.get("request") or {}
        cap, hard, window, threshold = self._provider_cooldown_limits()
        return {
            "status": "healthy" if self.healthy_candidates() > 0 else "degraded",
            # 新增字段（本轮）：面板要靠 pid + uptime_s 变小来证明重启真的换了进程
            "pid": os.getpid(),
            "uptime_s": round(now - self.started_at, 1),
            "listen": self.cfg.get("listen"),
            "default_alias": self.cfg.get("default_alias"),
            "auth_mode": self.auth_mode(),
            "request": {
                "per_attempt_timeout_s": rc.get("per_attempt_timeout_s"),
                "reasoning_min_timeout_s": rc.get("reasoning_min_timeout_s",
                                                  REASONING_MIN_ATTEMPT_TIMEOUT_S),
                "total_budget_s": rc.get("total_budget_s"),
                "provider_cooldown_cap_s": cap,
                "provider_cooldown_hard_cap_s": hard,
                "provider_failure_window_s": window,
                "provider_escalate_after_failures": threshold,
            },
            "candidates_healthy": self.healthy_candidates(),
            "counters": dict(self.counters),
            "keys_loaded": sorted(self.keys_raw.keys()),
            "providers": providers,
            "recent": list(self.recent),
        }

    def _record_recent(self, meta: dict, t0: float, status: int, usage: dict) -> None:
        self.recent.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "alias": meta.get("alias"), "caller": meta.get("caller") or "anonymous",
            "provider": meta.get("provider"), "model": meta.get("model"),
            "key_index": meta.get("key_index"), "key_fp": meta.get("key_fp"),
            "status": status, "latency_s": round(time.time() - t0, 3),
            "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
            "completion_tokens": (usage or {}).get("completion_tokens", 0),
            "total_tokens": (usage or {}).get("total_tokens", 0),
            "degraded": meta.get("degraded"), "attempts": len(meta.get("attempts") or []),
            "attempt_kinds": [a.get("kind") for a in (meta.get("attempts") or [])],
        })
        # usage.jsonl（§3）：每个请求结束都追加一行；关闭时完全无副作用
        self._record_usage(meta, t0, status, usage)

    def _record_usage(self, meta: dict, t0: float, status: int, usage: dict) -> None:
        """把一次请求的终局写进 usage.jsonl（只写 key_index / 指纹，永不写 key 明文）。"""
        u = usage or {}
        caller = str(meta.get("caller") or "anonymous")
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "alias": meta.get("alias"),
            "caller": caller,
            "provider": meta.get("provider"),
            "model": meta.get("model"),
            "key_index": meta.get("key_index"),
            "http": status,
            "latency_ms": int(round((time.time() - t0) * 1000)),
            "attempts": [
                {
                    "provider": a.get("provider"), "model": a.get("model"),
                    "key_index": a.get("key_index"), "http": a.get("http"),
                    "reason": a.get("kind"), "degrade": bool(a.get("degrade")),
                    "normalize": a.get("normalize"),
                }
                for a in (meta.get("attempts") or [])
            ],
            "degraded_json": bool(meta.get("degraded")),
            "json_valid": bool(status == 200),
            "prompt_tokens": int(u.get("prompt_tokens") or 0),
            "completion_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
            # 被 401/403/429 拒掉的请求用 meta["verdict"] 保留「为什么被拒」，
            # 其余按既有规则从 status/attempts 推。
            "verdict": str(meta.get("verdict") or usage_verdict(status, meta)),
        }
        self.usage.record(row)
        # T1.3：同一行增量喂给分组索引（不重扫文件）；usage_log 关闭时索引仍反映内存里的真实请求
        self.usage_index.note(row)
        # 当日预算只在「真实花掉的 token」上累加；被拒请求 usage 为空 → n=0 → 不加。
        self.callers.add_tokens(caller, int(u.get("prompt_tokens") or 0)
                                + int(u.get("completion_tokens") or 0))

    def models_payload(self) -> dict:
        self.maybe_reload()
        data = [{"id": self.cfg.get("default_alias", "default"), "object": "model",
                 "owned_by": "llm-relay", "relay_alias": True}]
        for pcfg in self.cfg.get("providers", []):
            rt = self.providers.get(pcfg.get("name"))
            for mcfg in pcfg.get("models", []):
                ms = self.models.get(f"{pcfg.get('name')}/{mcfg.get('id')}")
                data.append({
                    "id": f"{pcfg.get('name')}/{mcfg.get('id')}", "object": "model",
                    "owned_by": pcfg.get("name"), "chain": mcfg.get("chain"),
                    "available": bool(rt and rt.active and not (ms and ms.disabled)),
                })
        lf = self.cfg.get("local_fallback") or {}
        if lf.get("enabled"):
            data.append({"id": f"__local__/{lf.get('model')}", "object": "model", "owned_by": "mlx",
                         "available": self._local_ready(), "window": lf.get("window")})
        return {"object": "list", "data": data}

    # ==================================================================== 管理接口
    # TASK-DASHBOARD §2：/admin/* 只给本机 loopback 用，所有写回都先备份 + 原子替换。
    # §9.2 红线：任何 config.json 写回都是「读磁盘最新 → 只应用指定键 → 原子写回」，
    # 绝不把内存里的整份 cfg dump 回去（那会静默冲掉按实测定下的 chain / 超时 / rpm）。

    def _read_disk_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _write_disk_config(self, cfg: dict) -> str:
        """备份 + tmp + os.replace 原子写回磁盘，随后热重载。返回备份文件名。"""
        backup = atomic_write_json(self.config_path, cfg)
        self.reload(force=True)
        return backup

    @staticmethod
    def _flat_keys(obj: object, prefix: str = "") -> dict:
        """把嵌套 dict 摊平成 dotted-key → 值，用于「到底改了哪些键」的对比。"""
        out: dict = {}
        if isinstance(obj, dict):
            for k, val in obj.items():
                out.update(Relay._flat_keys(val, f"{prefix}{k}."))
        else:
            out[prefix.rstrip(".")] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        return out

    @staticmethod
    def _config_diff(before: dict, after: dict) -> list[str]:
        fb, fa = Relay._flat_keys(before), Relay._flat_keys(after)
        return [k for k in sorted(set(fb) | set(fa)) if fb.get(k) != fa.get(k)]

    def admin_config(self) -> dict:
        """GET /admin/config：返回磁盘上的 config.json（key 只给变量名 + 指纹）。"""
        cfg = self._read_disk_config()
        try:
            st = self.config_path.stat()
            mtime = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime))
            size = st.st_size
        except OSError:
            mtime, size = "", 0
        keys = [{"env": n, "fp": sha8(v) if v else "-", "set": bool(v)}
                for n, v in sorted(self.keys_raw.items())]
        # 兜底脱敏：万一有人把明文写进 config.json，也别从接口漏出去
        safe = json.loads(redact(json.dumps(cfg, ensure_ascii=False)))
        # 调用方的内联 key 不是厂商密钥，redact 正则抓不到，单独摘掉（只留 key_env 与是否配置）
        for ccfg in (safe.get("callers") or {}).values():
            if isinstance(ccfg, dict) and ccfg.pop("key", None):
                ccfg["key_redacted"] = True
        return {"config": safe, "path": str(self.config_path), "mtime": mtime, "size": size,
                "keys": keys, "keys_path": str(self.keys_path),
                # T1.5：最近一次 reload 的 schema 校验问题（字段路径/期望/实际/处置）
                "issues": list(getattr(self, "config_issues", []))}

    def admin_patch_config(self, patch: dict) -> tuple[int, dict]:
        """POST /admin/config：读磁盘最新 → 只应用 patch 里的键 → 原子写回 → 热重载。

        patch 支持两种写法：
          · 普通嵌套 patch，如 {"request": {"total_budget_s": 110}}、{"local_fallback": {"window": "01:00-07:00"}}
          · providers_by_name，如 {"providers_by_name": {"sensenova": {"rpm": 10}}}
            —— 按名字精确定位 provider，避免「整份 providers 列表一起写回」这种危险写法。
        """
        if not isinstance(patch, dict) or not patch:
            return 400, {"detail": "body 必须是 {\"patch\": {...}}，且 patch 非空"}
        patch = dict(patch)
        by_name = patch.pop("providers_by_name", None)
        if not patch and not by_name:
            return 400, {"detail": "patch 里没有任何键"}
        cfg = self._read_disk_config()
        before = json.loads(json.dumps(cfg, ensure_ascii=False))
        changed = deep_merge_patch(cfg, patch)
        if isinstance(by_name, dict):
            names = {p.get("name"): p for p in (cfg.get("providers") or [])}
            for pname, sub in by_name.items():
                if pname not in names:
                    return 400, {"detail": f"未知 provider：{pname}", "providers": sorted(names)}
                if not isinstance(sub, dict):
                    return 400, {"detail": f"providers_by_name.{pname} 必须是对象"}
                changed.extend(deep_merge_patch(names[pname], sub, f"providers.{pname}."))
        diff = self._config_diff(before, cfg)
        if not diff:
            return 200, {"changed": [], "reloaded": True, "backup": None, "note": "无变化，未写盘"}
        backup = self._write_disk_config(cfg)
        self.log(f"管理接口：config.json 已更新（改动 {len(diff)} 个键：{', '.join(diff[:8])}），"
                 f"备份 {backup}，已热重载")
        return 200, {"changed": diff, "reloaded": True, "backup": backup}

    def admin_reorder(self, order: list) -> tuple[int, dict]:
        """POST /admin/reorder：按给定顺序重写各模型 chain（1..N 连续），未知条目 400。"""
        if not isinstance(order, list) or not order:
            return 400, {"detail": "order 必须是非空数组（元素形如 sensenova/deepseek-v4-pro）"}
        cfg = self._read_disk_config()
        known: dict[str, dict] = {}
        for p in cfg.get("providers", []):
            for m in p.get("models", []):
                known[f"{p.get('name')}/{m.get('id')}"] = m
        unknown = [str(x) for x in order if str(x) not in known]
        if unknown:
            return 400, {"detail": f"未知条目：{', '.join(unknown)}", "unknown": unknown}
        uniq: list[str] = []
        for x in order:
            if str(x) not in uniq:
                uniq.append(str(x))
        remaining = sorted((k for k in known if k not in uniq),
                           key=lambda k: (known[k].get("chain") or 9999, k))
        final = uniq + remaining
        before = json.loads(json.dumps(cfg, ensure_ascii=False))
        chains: dict[str, int] = {}
        for i, key in enumerate(final, start=1):
            known[key]["chain"] = i
            chains[key] = i
        diff = self._config_diff(before, cfg)
        backup = self._write_disk_config(cfg)
        nums = sorted(chains.values())
        continuous = nums == list(range(1, len(final) + 1))
        self.log(f"管理接口：chain 已重排为 1..{len(final)}（连续={continuous}），备份 {backup}，已热重载")
        return 200, {"chains": chains, "order": final, "changed": diff, "reloaded": True,
                     "backup": backup, "continuous": continuous}

    def _find_key_slot(self, provider: str, env: str) -> KeySlot | None:
        rt = self.providers.get(provider)
        if rt is None:
            return None
        for ks in rt.keys:
            if ks.env_name == env:
                return ks
        return None

    # ------------------------------------------------------- 模型增删改（面板「Key 池 → 模型」）
    MODEL_CAPS_KEYS = ("json_schema", "tools", "reasoning_only")
    # add 时不写 caps（或只写一半）就用这套保守默认值落地。理由：relay 取能力时
    # `caps.get("json_schema", "native")` 缺省是 native（= 声称支持严格 schema），
    # 半截 caps 会静默把一个未知模型抬成 native；这里宁可先保守，确认过能力再改回来。
    MODEL_CAPS_DEFAULT = {"json_schema": "degradable", "tools": False, "reasoning_only": False}

    @classmethod
    def _validate_chain(cls, raw: object, field: str) -> tuple[int | None, str]:
        """chain 必须是 1..9999 的**整数**（字符串数字一律拒绝，别静默转）。"""
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None, f"{field} 必须是整数（1..9999），不接字符串数字"
        if not (1 <= raw <= 9999):
            return None, f"{field} 超出范围：{raw}（只允许 1..9999）"
        return raw, ""

    @classmethod
    def _validate_caps(cls, raw: object, field: str) -> tuple[dict | None, str]:
        """caps 只认 json_schema/tools/reasoning_only 三个键，未知键与非法值一律拒绝。"""
        if not isinstance(raw, dict):
            return None, f"{field} 必须是对象（如 {{\"json_schema\":\"native\",\"tools\":true}}）"
        unknown = [k for k in raw if k not in cls.MODEL_CAPS_KEYS]
        if unknown:
            return None, (f"{field} 含未知键：{', '.join(str(k) for k in unknown)}"
                          f"（只允许 {', '.join(cls.MODEL_CAPS_KEYS)}）")
        out: dict = {}
        if "json_schema" in raw:
            js = raw["json_schema"]
            if js not in ("native", "degradable"):
                return None, f"{field}.json_schema 只允许 native / degradable，得到 {js!r}"
            out["json_schema"] = js
        for key in ("tools", "reasoning_only"):
            if key in raw:
                val = raw[key]
                if not isinstance(val, bool):
                    return None, f"{field}.{key} 只允许 true / false（布尔），得到 {val!r}"
                out[key] = val
        return out, ""

    @staticmethod
    def _model_view(models: list) -> list[dict]:
        """回应体里的模型快照：只回 id/chain/caps，不回 params 等其它字段。"""
        return [{"id": m.get("id"), "chain": m.get("chain"), "caps": m.get("caps")}
                for m in models]

    @staticmethod
    def _flat_keys_idx(obj: object, prefix: str = "") -> dict:
        """同 `_flat_keys`，但数组按下标展开（providers.zen.models.2.chain），
        便于「模型增删改到底动了哪几个键」这种细粒度对比。"""
        out: dict = {}
        if isinstance(obj, dict):
            for k, val in obj.items():
                out.update(Relay._flat_keys_idx(val, f"{prefix}{k}."))
        elif isinstance(obj, list):
            for i, val in enumerate(obj):
                out.update(Relay._flat_keys_idx(val, f"{prefix}{i}."))
        else:
            out[prefix.rstrip(".")] = json.dumps(obj, ensure_ascii=False, sort_keys=True)
        return out

    def admin_model(self, payload: dict) -> tuple[int, dict]:
        """POST /admin/model：管理 providers[].models（add / update / remove）。

        body 三种形态（由 action 区分）：
          · add    {action:"add",    provider, model:{id, chain?, caps?}}
          · update {action:"update", provider, model_id, patch:{chain?, caps?, params?}}
          · remove {action:"remove", provider, model_id, confirm:"REMOVE"}

        ★ 写回契约（与 §2 R1 一致，别改成别的写法）：
          1. `self._read_disk_config()` 读**磁盘最新**内容；
          2. **只改目标 provider 的 models 列表**（其它键一个都不碰，尤其 request.* 里
             的实测调参、各 provider 的 rpm / chain / keys / enabled）；
          3. `atomic_write_json(self.config_path, cfg)`（自动先备份 .bak.<ts>）；
          4. `self.reload(force=True)`；
          5. 返回体含 action/provider/models_before/models_after/backup/reloaded。
        校验不过一律 400 且**零副作用**（在读盘之后、任何写盘之前返回）。
        """
        if not isinstance(payload, dict):
            return 400, {"detail": "body 必须是 JSON 对象"}
        action = str(payload.get("action") or "")
        if action not in ("add", "update", "remove"):
            return 400, {"detail": "action 只允许 add / update / remove"}
        provider = str(payload.get("provider") or "")
        cfg = self._read_disk_config()
        provs = cfg.get("providers") or []
        names = [str(p.get("name")) for p in provs]
        target = next((p for p in provs if str(p.get("name")) == provider), None)
        if target is None:
            return 400, {"detail": f"未知 provider：{provider or '(空)'}", "providers": names}
        models = target.get("models")
        if models is None:
            models = []
            target["models"] = models
        if not isinstance(models, list):
            return 400, {"detail": f"provider {provider} 的 models 不是数组，拒绝改写"}
        before = json.loads(json.dumps(cfg, ensure_ascii=False))
        models_before = self._model_view(models)

        if action == "add":
            spec = payload.get("model")
            if not isinstance(spec, dict):
                return 400, {"detail": "add 需要 model:{id, chain?, caps?}"}
            mid = spec.get("id")
            if not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", mid):
                return 400, {"detail": "模型 id 只允许 1–120 位字母/数字/._:/-（不许空格）"}
            if any(str(m.get("id")) == mid for m in models):
                return 400, {"detail": f"provider {provider} 里已有模型 {mid}（不许重名）"}
            if "chain" in spec:
                chain, err = self._validate_chain(spec["chain"], "chain")
                if err:
                    return 400, {"detail": err}
            else:
                chain = 1 + max([int(m.get("chain") or 0) for m in models] or [0])
                chain = min(chain, 9999)
            caps_raw = spec.get("caps", self.MODEL_CAPS_DEFAULT)
            caps, err = self._validate_caps(caps_raw, "caps")
            if err:
                return 400, {"detail": err}
            caps = dict(self.MODEL_CAPS_DEFAULT, **caps)
            models.append({"id": mid, "chain": chain, "caps": caps, "params": {}})
            changed_id = mid
        elif action == "update":
            mid = payload.get("model_id")
            entry = next((m for m in models if str(m.get("id")) == str(mid)), None)
            if not isinstance(mid, str) or not mid or entry is None:
                return 400, {"detail": f"provider {provider} 里没有模型 {mid or '(空)'}"}
            patch = payload.get("patch")
            if not isinstance(patch, dict) or not patch:
                return 400, {"detail": "update 需要 patch:{chain?, caps?, params?} 且非空"}
            unknown = [k for k in patch if k not in ("chain", "caps", "params")]
            if unknown:
                return 400, {"detail": f"patch 含未知键：{', '.join(str(k) for k in unknown)}"
                                       "（只允许 chain / caps / params）"}
            if "chain" in patch:
                chain, err = self._validate_chain(patch["chain"], "patch.chain")
                if err:
                    return 400, {"detail": err}
                entry["chain"] = chain
            if "caps" in patch:
                caps, err = self._validate_caps(patch["caps"], "patch.caps")
                if err:
                    return 400, {"detail": err}
                merged = dict(entry.get("caps") or {})
                merged.update(caps)          # 只覆盖 patch 里出现的子键，其余保持原样
                entry["caps"] = merged
            if "params" in patch:
                if not isinstance(patch["params"], dict):
                    return 400, {"detail": "patch.params 必须是对象"}
                params = dict(entry.get("params") or {})
                deep_merge_patch(params, patch["params"])   # 只动 patch 提到的子键
                entry["params"] = params
            changed_id = mid
        else:  # remove
            if str(payload.get("confirm") or "") != "REMOVE":
                return 400, {"detail": "删除模型必须在 confirm 里原样输入 REMOVE"}
            mid = payload.get("model_id")
            entry = next((m for m in models if str(m.get("id")) == str(mid)), None)
            if not isinstance(mid, str) or not mid or entry is None:
                return 400, {"detail": f"provider {provider} 里没有模型 {mid or '(空)'}"}
            models.remove(entry)
            changed_id = mid
            if not models:
                # 守卫：不许把某 provider 的模型删空（要下线整个源请用 enabled=false）
                return 400, {"detail": f"拒绝：删掉 {provider}/{mid} 会让该 provider 一个模型都不剩；"
                                       "请改用 provider 级开关（enabled=false）或先加别的模型"}
            if not any(p.get("models") for p in provs):
                return 400, {"detail": "拒绝：删掉这个模型会让全库变成「零模型」，至少保留一个"}

        models_after = self._model_view(models)
        fb, fa = self._flat_keys_idx(before), self._flat_keys_idx(cfg)
        changed = [k for k in sorted(set(fb) | set(fa)) if fb.get(k) != fa.get(k)]
        # 只允许出现在目标 provider 的 models 里的改动。判断时先把目标 provider 的
        # models 抹平（_flat_keys 对数组是整块比较，不抹平就分不清动的是哪一段），
        # 剩下的任何差异都算越界 → 整单拒掉（返回 400，磁盘一字未动）。
        scope_before, scope_after = (json.loads(json.dumps(before)),
                                     json.loads(json.dumps(cfg)))
        for scope in (scope_before, scope_after):
            for p in scope.get("providers") or []:
                if str(p.get("name")) == provider:
                    p["models"] = []
        stray = self._config_diff(scope_before, scope_after)
        if stray:
            self.log(f"管理接口：模型 {action} 被拒（越界改动 {', '.join(stray[:5])}）")
            return 400, {"detail": f"内部守卫：本次改动越出了 providers.{provider}.models"
                                   f"（{', '.join(stray[:5])}）"}
        if not changed:
            return 200, {"action": action, "provider": provider, "model": changed_id,
                         "changed": [], "models_before": models_before,
                         "models_after": models_after, "backup": None, "reloaded": True,
                         "note": "无变化，未写盘"}
        backup = self._write_disk_config(cfg)
        self.log(f"管理接口：模型 {action} {provider}/{changed_id}"
                 f"（{len(models_before)}→{len(models_after)} 个），备份 {backup}，已热重载")
        return 200, {"action": action, "provider": provider, "model": changed_id,
                     "changed": changed, "models_before": models_before,
                     "models_after": models_after, "backup": backup, "reloaded": True}

    def admin_add_key(self, provider: str, env_name: str, secret: str,
                      new_provider: object = None) -> tuple[int, dict]:
        """POST /admin/key：按 sha256 去重后追加到 keys.env（600）→ 热重载。绝不回显明文。"""
        provider, env_name, secret = str(provider or ""), str(env_name or ""), str(secret or "")
        # 「新建厂商」：new_provider = {name, base_url, models}；校验通过就接管 provider 名字
        creating = False
        np_url = ""
        np_models: list[str] = []
        if new_provider:
            npv = new_provider if isinstance(new_provider, dict) else {}
            np_name = str(npv.get("name") or "").strip()
            np_url = str(npv.get("base_url") or "").strip()
            raw_models = npv.get("models") or []
            if isinstance(raw_models, str):
                raw_models = re.split(r"[,\s]+", raw_models)
            np_models = [str(m).strip() for m in raw_models if str(m).strip()]
            if not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", np_name or ""):
                return 400, {"detail": "厂商名只允许小写字母开头，含小写字母/数字/下划线/短横线，2-32 位"}
            if not re.match(r"^https?://", np_url or ""):
                return 400, {"detail": "base_url 必须以 http:// 或 https:// 开头"}
            if not np_models:
                return 400, {"detail": "至少要填一个模型 id（多个用逗号分隔）"}
            if len(np_models) > 8:
                return 400, {"detail": "模型最多填 8 个"}
            for m in np_models:
                if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,120}", m):
                    return 400, {"detail": f"模型 id 不合法：{m}"}
            if any(p.get("name") == np_name
                   for p in (self._read_disk_config().get("providers") or [])):
                return 400, {"detail": f"provider {np_name} 已存在，请直接在列表里选它"}
            provider, creating = np_name, True
        if not provider or not env_name or not secret:
            return 400, {"detail": "provider / env_name / secret 都不能为空"}
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", env_name):
            return 400, {"detail": "env_name 只允许大写字母、数字与下划线（如 SENSENOVA_KEY_3）"}
        pnames = [p.get("name") for p in (self._read_disk_config().get("providers") or [])]
        if provider not in pnames and not creating:
            return 400, {"detail": f"未知 provider：{provider}", "providers": pnames}
        digest = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()
        fp = digest[:8]
        for name, val in self.keys_raw.items():
            if val and hashlib.sha256(val.encode("utf-8", "replace")).hexdigest() == digest:
                return 200, {"duplicate": True, "env_name": env_name, "fp": fp,
                             "existing_env": name, "reloaded": False,
                             "detail": "这把 key 已在池子里（sha256 去重），未写入"}
        try:
            text = self.keys_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if text and not text.endswith("\n"):
            text += "\n"
        text += f'export {env_name}="{secret}"\n'
        tmp = self.keys_path.with_name(self.keys_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.keys_path)
        # 变量名还要进该 provider 的 keys 列表；「新建厂商」时连 provider 块一起建出来
        cfg = self._read_disk_config()
        added_to_provider = False
        target = None
        for p in cfg.get("providers", []):
            if p.get("name") == provider:
                target = p
                break
        if target is None and creating:
            target = {
                "name": provider, "enabled": True, "base_url": np_url,
                "wire": {"max_tokens_param": "max_tokens"},
                "keys": [],
                "rpm": 10, "max_concurrency": 2, "key_cooldown_s": 300,
                "provider_cooldown_s": 300,
                "cooldown_backoff": {"initial_s": 60, "max_s": 900, "factor": 2},
                "models": [{"id": m, "chain": 90,
                            "caps": {"json_schema": "degradable", "tools": False,
                                     "reasoning_only": False},
                            "params": {}} for m in np_models],
                "rpm_note": "面板新建：保守默认（rpm 10、并发 2、chain 90 最低优先级、"
                            "json_schema=degradable）；确认过能力后可在 config.json 把 caps 调成 native",
            }
            cfg.setdefault("providers", []).append(target)
        if target is not None:
            envs = list(target.get("keys") or [])
            if env_name not in envs:
                envs.append(env_name)
                target["keys"] = envs
                added_to_provider = True
        backup = atomic_write_json(self.config_path, cfg) if (added_to_provider or creating) else None
        self.reload(force=True)
        self.log(f"管理接口：provider={provider} 追加 key {env_name}（指纹 {fp}），"
                 f"provider keys 列表{'已更新' if added_to_provider else '无需更新'}"
                 + (f"，新建厂商（模型 {', '.join(np_models)}，chain 90）" if creating else "")
                 + (f"，备份 {backup}" if backup else "") + "，已热重载")
        return 200, {"env_name": env_name, "fp": fp, "duplicate": False,
                     "added_to_provider_list": added_to_provider,
                     "created_provider": provider if creating else None,
                     "provider_defaults": ("chain 90 最低优先级、rpm 10、并发 2、"
                                           "json_schema=degradable" if creating else None),
                     "backup": backup, "reloaded": True}

    # ------------------------------------------------ 一键重启（TASK-ADMIN-RESTART-KEY §A）
    RESTART_SERVICE = "com.user.llm-relay"   # 服务白名单：只允许中转站自己

    def _concurrency_in_use(self) -> int | None:
        """正在并发中的请求数 = Σ(各 provider 信号量已占用的许可)。取不到就 None，不编数字。"""
        used = 0
        try:
            for rt in self.providers.values():
                sem = getattr(rt, "sem", None)
                if sem is None or not hasattr(sem, "_value"):
                    return None
                used += max(0, int(getattr(sem, "_initial_value", 0)) - int(sem._value))
            return used
        except Exception:  # noqa: BLE001
            return None

    def restart_before(self) -> dict:
        """重启前的状态快照（pid / uptime / 健康候选数 / 并发）——不做任何写操作。"""
        now = time.time()
        return {"pid": os.getpid(), "uptime_s": round(now - self.started_at, 1),
                "candidates_healthy": self.healthy_candidates(),
                "concurrency_in_use": self._concurrency_in_use(),
                "concurrency_max": int(getattr(self.global_sem, "_initial_value", 0) or 0) or None}

    def _schedule_restart_exit(self) -> None:
        """后台延迟退出：HTTP 响应先 flush 出去，再 os._exit(0) 交给 launchd 拉起。"""
        def _go() -> None:
            try:
                self._exit_fn(0)
            except Exception as e:  # noqa: BLE001
                self.log(f"管理接口：重启退出回调失败（{type(e).__name__}: {e}）——进程未退出")

        timer = threading.Timer(max(0.0, float(self.restart_delay_s)), _go)
        timer.daemon = True
        self._restart_timer = timer
        timer.start()

    def admin_restart(self, confirm: object, service: object = None,
                      actor: str = "loopback") -> tuple[int, dict]:
        """POST /admin/restart：先回包再让进程自己退出（plist 的 KeepAlive 负责拉起）。

        访问控制由 Handler._admin_guard 负责（非 loopback 403）；这里只管
        服务白名单 + 二次确认词 + 防连点，并把重启前状态如实写进响应与留痕日志。
        """
        want = str(service or self.RESTART_SERVICE).strip()
        if want != self.RESTART_SERVICE:
            return 400, {"detail": f"服务白名单只允许 {self.RESTART_SERVICE}（收到：{want}）",
                         "allowed": [self.RESTART_SERVICE]}
        if str(confirm or "") != "RESTART":
            return 400, {"detail": "重启是不可逆操作，body 必须带 {\"confirm\": \"RESTART\"}"}
        before = self.restart_before()
        with self.lock:
            if self._restarting:
                # 防连点：已经在重启流程里，不再排第二次
                return 200, {"ok": True, "restarting": True, "already": True,
                             "service": self.RESTART_SERVICE, "before": before}
            self._restarting = True
            self.restart_calls.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "actor": actor,
                                       "pid": before["pid"], "uptime_s": before["uptime_s"]})
        # 留痕日志：谁点的 / 时间 / 重启前 pid 与 uptime —— 只允许出现变量名与指纹，绝不写 key
        self.log(f"管理接口：收到重启请求（actor={actor}，pid={before['pid']}，"
                 f"uptime_s={before['uptime_s']}，candidates_healthy={before['candidates_healthy']}）"
                 f"→ 约 {self.restart_delay_s}s 后进程自退，由 launchd KeepAlive 拉起")
        self._schedule_restart_exit()
        return 200, {"ok": True, "restarting": True, "service": self.RESTART_SERVICE,
                     "before": before,
                     "note": "进程将退出，launchd(KeepAlive) 会在 1-2 秒内拉起"}

    # ------------------------------------------------ key 替换 / 删除（§B）
    def _known_provider_names(self) -> list[str]:
        return [p.get("name") for p in (self._read_disk_config().get("providers") or [])]

    def _drop_key_state(self, cfg: dict, provider: str, env_name: str) -> bool:
        """只动 cfg 里 key_state[provider][env_name] 这一个键；返回是否真的删掉了东西。"""
        per = (cfg.get("key_state") or {}).get(provider) or {}
        if env_name not in per:
            return False
        per.pop(env_name, None)
        if not per:
            cfg.get("key_state", {}).pop(provider, None)
        return True

    def admin_replace_key(self, provider: str, env_name: str, secret: str) -> tuple[int, dict]:
        """POST /admin/key/replace：原地换掉 keys.env 里该变量名的值 → 热重载。绝不回显明文。"""
        provider, env_name, secret = str(provider or ""), str(env_name or ""), str(secret or "")
        if not provider or not env_name or not secret:
            return 400, {"detail": "provider / env_name / secret 都不能为空"}
        pnames = self._known_provider_names()
        if provider not in pnames:
            return 400, {"detail": f"未知 provider：{provider}", "providers": pnames}
        try:
            text = self.keys_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if key_line_index(split_key_lines(text), env_name) is None:
            return 404, {"detail": f"keys.env 里没有变量 {env_name}"
                                   f"（替换不会静默新增，要新增请用 /admin/key）"}
        digest = hashlib.sha256(secret.encode("utf-8", "replace")).hexdigest()
        fp = digest[:8]
        # sha256 去重：新值若与池子里别处（含同 provider 的其它 key）相同 → 不写盘
        for name, val in parse_keys_text(text).items():
            if name == env_name or not val:
                continue
            if hashlib.sha256(val.encode("utf-8", "replace")).hexdigest() == digest:
                return 200, {"duplicate": True, "env_name": env_name, "fp": fp,
                             "existing_env": name, "replaced": None, "backup": None,
                             "reloaded": False,
                             "detail": "新值与池子里已有的 key 相同（sha256 去重），未写入"}
        new_text, old_value = replace_key_line(text, env_name, secret)
        old_fp = sha8(old_value) if old_value else "-"
        backup = atomic_write_text(self.keys_path, new_text, mode=0o600)
        cfg = self._read_disk_config()
        cleared = self._drop_key_state(cfg, provider, env_name)
        if cleared:                    # 旧值上的冷却/禁用必须失效
            atomic_write_json(self.config_path, cfg)
        self.reload(force=True)
        self.log(f"管理接口：provider={provider} 替换 key {env_name}（指纹 {old_fp}→{fp}），"
                 f"备份 {backup}，key_state 残留{'已清理' if cleared else '无需清理'}，已热重载")
        return 200, {"env_name": env_name, "fp": fp, "replaced": old_fp, "duplicate": False,
                     "key_state_cleared": cleared, "backup": backup, "reloaded": True}

    def admin_remove_key(self, provider: str, env_name: str,
                         confirm: object = None) -> tuple[int, dict]:
        """POST /admin/key/remove：从 keys.env 删掉该变量名 → 清残留 → 热重载。"""
        provider, env_name = str(provider or ""), str(env_name or "")
        if str(confirm or "") != "REMOVE":
            return 400, {"detail": "删除是不可逆操作，body 必须带 {\"confirm\": \"REMOVE\"}"}
        pnames = self._known_provider_names()
        if provider not in pnames:
            return 400, {"detail": f"未知 provider：{provider}", "providers": pnames}
        try:
            text = self.keys_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        names = key_file_names(text)
        if env_name not in names:
            # keys.env 里没有这一行 —— 但 config 的 providers[].keys 里可能有这个「空槽」
            # （例如 NVIDIA_KEY / DEEPSEEK_KEY：声明了、值还没填）。空槽也要能删，且只动配置侧。
            cfg = self._read_disk_config()
            cfg_changed = False
            for p in cfg.get("providers", []):
                if p.get("name") == provider:
                    envs = list(p.get("keys") or [])
                    if env_name in envs:
                        p["keys"] = [e for e in envs if e != env_name]
                        cfg_changed = True
            cfg_changed = self._drop_key_state(cfg, provider, env_name) or cfg_changed
            if not cfg_changed:
                return 404, {"detail": f"keys.env 与 config 里都没有 {env_name}，没有可删的东西"}
            atomic_write_json(self.config_path, cfg)
            self.reload(force=True)
            left = len(self.providers[provider].keys) if provider in self.providers else 0
            self.log(f"管理接口：provider={provider} 删除空槽 {env_name}"
                     f"（keys.env 里本来就没有这一行，只清了配置侧），已热重载")
            body = {"removed": env_name, "fp": "-", "index": None, "line_absent": True,
                    "keys_before": len(names), "keys_after": len(names),
                    "provider_keys_left": left, "config_cleared": True, "backup": None,
                    "reloaded": True}
            if left == 0:
                body["warning"] = "该 provider 已无可用 key"
            return 200, body
        keys_before = len(names)
        index = names.index(env_name)
        rt = self.providers.get(provider)
        if rt is not None:      # 池子里的 index 以该 provider 的 keys 顺序为准（和 /status 一致）
            slot = next((k for k in rt.keys if k.env_name == env_name), None)
            if slot is not None:
                index = slot.index
        new_text, old_value = remove_key_line(text, env_name)
        old_fp = sha8(old_value) if old_value else "-"
        backup = atomic_write_text(self.keys_path, new_text, mode=0o600)
        cfg = self._read_disk_config()
        cfg_changed = False
        for p in cfg.get("providers", []):
            if p.get("name") == provider:
                envs = list(p.get("keys") or [])
                if env_name in envs:
                    # 池子是按 providers[].keys 建的；不清掉会留一行空值占位
                    p["keys"] = [e for e in envs if e != env_name]
                    cfg_changed = True
        cfg_changed = self._drop_key_state(cfg, provider, env_name) or cfg_changed
        if cfg_changed:
            atomic_write_json(self.config_path, cfg)
        self.reload(force=True)
        keys_after = len(key_file_names(new_text))
        left = len(self.providers[provider].keys) if provider in self.providers else 0
        self.log(f"管理接口：provider={provider} 删除 key {env_name}（指纹 {old_fp}，index {index}，"
                 f"keys {keys_before}→{keys_after}），备份 {backup}，"
                 f"key_state/池子残留{'已清' if cfg_changed else '无需清'}，已热重载")
        body = {"removed": env_name, "fp": old_fp, "index": index,
                "keys_before": keys_before, "keys_after": keys_after,
                "provider_keys_left": left, "config_cleared": cfg_changed,
                "backup": backup, "reloaded": True}
        if left == 0:
            body["warning"] = "该 provider 已无可用 key"
        return 200, body

    def admin_key_state(self, provider: str, env: str, enabled: object = None,
                        clear_cooldown: bool = False, reason: str = "") -> tuple[int, dict]:
        """POST /admin/key/state：改运行时状态并持久化到 config.json 的 key_state。"""
        provider, env = str(provider or ""), str(env or "")
        ks = self._find_key_slot(provider, env)
        if ks is None:
            return 404, {"detail": f"没找到 key：{provider} / {env}"}
        if enabled is None and not clear_cooldown:
            return 400, {"detail": "至少给一个操作：enabled=false|true 或 clear_cooldown=true"}
        if enabled is not None:
            ks.disabled = not bool(enabled)
            ks.disabled_reason = "" if bool(enabled) else (str(reason) or "管理面板手动禁用（已持久化）")
        if clear_cooldown:
            ks.cooldown_until = 0.0
            ks.cooldown_reason = ""
            ks.model_cooldown.clear()
        if enabled is not None:
            # 只动 config.json 的 key_state 这一个键（§9.2：其余键逐字节不变）
            cfg = self._read_disk_config()
            per_provider = cfg.setdefault("key_state", {}).setdefault(provider, {})
            per_provider[env] = {"disabled": bool(ks.disabled),
                                 "disabled_reason": ks.disabled_reason,
                                 "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self._write_disk_config(cfg)
        self.log(f"管理接口：{provider}/{env} enabled={enabled} "
                 f"clear_cooldown={bool(clear_cooldown)} → disabled={ks.disabled}")
        return 200, {"provider": provider, "env": env, "disabled": ks.disabled,
                     "disabled_reason": ks.disabled_reason,
                     "cooldown_left_s": round(ks.cooldown_left(), 1),
                     "model_cooldowns": ks.model_cooldown_details(), "reloaded": True}

    # ------------------------------------------------------------- 能力探测
    def admin_probe(self, provider: str, model_id: str) -> tuple[int, dict]:
        """POST /admin/probe：对指定模型实跑 chat + strict json_schema + tools，并计入 usage。"""
        provider, model_id = str(provider or ""), str(model_id or "")
        pcfg = self._provider_cfg(provider)
        rt = self.providers.get(provider)
        if not pcfg or rt is None:
            return 400, {"detail": f"未知 provider：{provider}"}
        if model_id not in [str(m.get("id")) for m in (pcfg.get("models") or [])]:
            return 400, {"detail": f"未知模型：{provider}/{model_id}"}
        results: list[dict] = []
        for kind, payload in probe_cases():
            payload = dict(payload, model=model_id)
            ks = rt.next_key_slot(model_id)
            if ks is None:
                results.append({"kind": kind, "http": 0, "verdict": "no_key", "latency_s": 0,
                                "detail": "该 provider 没有可用 key（冷却/禁用）"})
                continue
            opts = SendOpts(model_id=model_id, alias="probe", schema=PROBE_SCHEMA,
                            min_max_tokens=int((self.cfg.get("request") or {}).get(
                                "schema_min_max_tokens", 1024) or 0))
            body = self.build_upstream_body(payload, provider, opts)
            t0 = time.time()
            res = self._post_upstream(provider, ks.value, body, timeout=120)
            dt = res.latency_s or (time.time() - t0)
            ks.uses += 1
            verdict, detail = self._probe_verdict(kind, res)
            if res.status == 429:
                ks.n429 += 1
            if res.status in (401, 403):
                ks.n401 += 1
            results.append({"kind": kind, "http": res.status, "latency_s": round(dt, 2),
                            "verdict": verdict, "detail": str(detail)[:300],
                            "key_index": ks.index, "key_fp": ks.fp})
            self.usage.record({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "alias": "probe",
                "caller": "probe",  # 内部旁路：不计入任何调用方配额
                "provider": provider, "model": model_id, "key_index": ks.index,
                "http": res.status, "latency_ms": int(round(dt * 1000)),
                "attempts": [{"provider": provider, "model": model_id, "key_index": ks.index,
                              "http": res.status, "reason": kind, "degrade": False}],
                "degraded_json": False,
                "json_valid": bool(verdict in ("JSON_OK", "TOOLS_OK", "CHAT_OK")),
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "verdict": f"probe_{kind}",
            })
        summary = "，".join(f"{r['kind']}={r['verdict']}" for r in results)
        self.log(f"管理接口：能力探测 {provider}/{model_id} 完成（{summary}）")
        return 200, {"provider": provider, "model": model_id, "results": results}

    @staticmethod
    def _probe_verdict(kind: str, res: UpstreamResult) -> tuple[str, str]:
        """把一条探测结果翻成 verdict + 人能看的 detail（永不回显 key）。"""
        if res.status == 0:
            return "TIMEOUT", res.error or "连接失败/超时"
        if res.status != 200:
            return f"HTTP_{res.status}", res.text()[:200]
        try:
            body = json.loads(res.text())
        except Exception:  # noqa: BLE001
            return "BAD_BODY", res.text()[:200]
        msg = ((body.get("choices") or [{}])[0].get("message") or {}) if isinstance(body, dict) else {}
        content = msg.get("content") or ""
        if kind == "chat":
            return ("CHAT_OK", repr(content[:60])) if content else ("CHAT_EMPTY", str(body)[:200])
        if kind == "tools":
            tc = msg.get("tool_calls")
            return ("TOOLS_OK", json.dumps(tc, ensure_ascii=False)[:200]) if tc else \
                   ("TOOLS_NO", (str(content)[:200] or str(body)[:200]))
        try:
            parsed = json.loads(content)
        except Exception:  # noqa: BLE001
            return "JSON_UNPARSEABLE", repr(content[:200])
        if isinstance(parsed, dict) and isinstance(parsed.get("facts"), list) and "language" in parsed:
            if looks_like_schema_echo(parsed):
                return "JSON_ECHO", "上游把 schema 当结果回显"
            return "JSON_OK", f"facts={len(parsed['facts'])} {str(parsed['facts'][:1])[:120]}"
        return "JSON_SHAPE_BAD", str(parsed)[:200]

    # ------------------------------------------------------- usage.jsonl 读取/聚合
    def _usage_files(self) -> list[Path]:
        p = self.usage.path()
        keep = int(self.usage.cfg().get("keep") or 5)
        files = [p.with_name(p.name + f".{i}") for i in range(keep, 0, -1)]
        files.append(p)
        return [f for f in files if f.exists()]

    def _usage_rows(self) -> list[dict]:
        rows: list[dict] = []
        for f in self._usage_files():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if isinstance(obj, dict):
                    rows.append(obj)
        return rows

    def admin_requests(self, limit: int = 100, offset: int = 0, provider: str = "",
                       model: str = "", verdict: str = "", since: str = "",
                       include_probe: bool = False) -> dict:
        """GET /admin/requests：usage.jsonl 的请求明细，倒序分页 + 过滤。"""
        rows = self._usage_rows()
        if not include_probe:
            rows = [r for r in rows if r.get("alias") != "probe"]
        if provider:
            rows = [r for r in rows if str(r.get("provider") or "") == provider]
        if model:
            rows = [r for r in rows if str(r.get("model") or "") == model]
        if verdict:
            rows = [r for r in rows if str(r.get("verdict") or "") == verdict]
        if since:
            rows = [r for r in rows if str(r.get("ts") or "") >= since]
        rows.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
        limit = max(1, min(int(limit or 100), 2000))
        offset = max(0, int(offset or 0))
        return {"rows": rows[offset:offset + limit], "total": len(rows), "limit": limit,
                "offset": offset, "path": str(self.usage.path()), "enabled": self.usage.enabled()}

    def admin_usage(self, days: int = 7) -> dict:
        """GET /admin/usage：按天 × provider 聚合请求数 / token / 成功 / 429 / 401。"""
        days = max(1, min(int(days or 7), 365))
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - 1) * 86400))
        rows = [r for r in self._usage_rows()
                if str(r.get("ts") or "")[:10] >= cutoff and r.get("alias") != "probe"]

        def _blank() -> dict:
            return {"requests": 0, "ok": 0, "n429": 0, "n401": 0, "errors": 0,
                    "total_tokens": 0, "latency_ms_sum": 0}

        by_day: dict[str, dict] = {}
        by_prov: dict[str, dict] = {}
        by_day_prov: dict[str, dict] = {}   # 按天 × provider 的请求数（给面板画堆叠柱）
        day_order: list[str] = []
        for r in rows:
            d = str(r.get("ts") or "")[:10] or "-"
            prov = str(r.get("provider") or "-")
            http = int(r.get("http") or 0)
            tok = int(r.get("total_tokens") or 0)
            lat = int(r.get("latency_ms") or 0)
            by_day_prov.setdefault(d, {})
            by_day_prov[d][prov] = by_day_prov[d].get(prov, 0) + 1
            for bucket, key in ((by_day, d), (by_prov, prov)):
                b = bucket.setdefault(key, _blank())
                b["requests"] += 1
                b["ok"] += 1 if http == 200 else 0
                b["n429"] += 1 if http == 429 else 0
                b["n401"] += 1 if http == 401 else 0
                b["errors"] += 1 if http != 200 else 0
                b["total_tokens"] += tok
                b["latency_ms_sum"] += lat
            if d not in day_order:
                day_order.append(d)
        for bucket in (by_day, by_prov):
            for b in bucket.values():
                n = b["requests"] or 1
                b["success_rate"] = round(b["ok"] / n, 4)
                b["p50_latency_ms"] = int(b.pop("latency_ms_sum") / n)
        total = _blank()
        for r in rows:
            http = int(r.get("http") or 0)
            total["requests"] += 1
            total["ok"] += 1 if http == 200 else 0
            total["n429"] += 1 if http == 429 else 0
            total["n401"] += 1 if http == 401 else 0
            total["errors"] += 1 if http != 200 else 0
            total["total_tokens"] += int(r.get("total_tokens") or 0)
            total["latency_ms_sum"] += int(r.get("latency_ms") or 0)
        n = total["requests"] or 1
        total["success_rate"] = round(total["ok"] / n, 4)
        total["p50_latency_ms"] = int(total.pop("latency_ms_sum") / n)
        prov_requests = sum(b["requests"] for b in by_prov.values()) or 1
        for b in by_prov.values():
            b["share"] = round(b["requests"] / prov_requests, 4)
        return {"days": days, "since": cutoff, "by_day": {d: by_day[d] for d in sorted(day_order)},
                "by_day_provider": by_day_prov,
                "by_provider": by_prov, "totals": total, "enabled": self.usage.enabled(),
                "path": str(self.usage.path())}

    # ------------------------------------------------------- T1.3 用量 API / metrics
    def admin_usage_group(self, group: str = "caller", window: str = "24h") -> dict:
        """GET /admin/usage?group=&window=：按 caller/route/provider/model 分组的窗口聚合。

        数据来自 `UsageIndex`（惰性重建 + 增量计数，见其 docstring），不是每次读全量文件，
        口径与 usage.jsonl 逐条可对账（窗口内非 probe 行直接求和）。
        """
        return self.usage_index.snapshot(group, window)

    def metrics_enabled(self) -> bool:
        """`usage_log.metrics`（默认 false）控制 `/metrics` 是否存在；线上不打开。"""
        return bool((self.usage.cfg() or {}).get("metrics"))

    def cooling_providers(self) -> int:
        return sum(1 for rt in self.providers.values() if rt.cooldown_left() > 0)

    def metrics_text(self) -> str:
        """Prometheus 文本格式（前缀 llm_relay_），只在 usage_log.metrics=true 时由 /metrics 暴露。"""
        lines = [
            "# HELP llm_relay_up 中继进程存活（恒为 1）",
            "# TYPE llm_relay_up gauge",
            "llm_relay_up 1",
            "# HELP llm_relay_candidates_healthy 当前健康候选数（含本地兜底）",
            "# TYPE llm_relay_candidates_healthy gauge",
            f"llm_relay_candidates_healthy {self.healthy_candidates()}",
            "# HELP llm_relay_providers_cooling 处于冷却中的 provider 数",
            "# TYPE llm_relay_providers_cooling gauge",
            f"llm_relay_providers_cooling {self.cooling_providers()}",
        ]
        lines += self.usage_index.metrics_lines()
        return "\n".join(lines) + "\n"

    # ------------------------------------------- 上游集成（可选，配置驱动，P0/T0.3）
    # config.json -> integrations.upstream；这段缺失时整块功能自动隐藏：
    # /admin/upstream 回 {"configured": false}（HTTP 200），面板不渲染对应标签页。
    # 刻意不按厂商名硬编码：label / 健康地址 / 控制面地址 / start.sh / 回滚脚本 /
    # 环境变量前缀全部来自配置（env_prefix 为 T0.3 相对任务书 snippet 新增的键）。
    def upstream_integration(self) -> dict:
        integ = (self.cfg.get("integrations") or {}).get("upstream")
        return integ if isinstance(integ, dict) else {}

    def _integration_health(self, url: str) -> dict:
        if not url:
            return {"http": 0, "body": "未配置 health_url"}
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with OPENER.open(req, timeout=5) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return {"http": r.status, "body": json.loads(raw)}
                except Exception:  # noqa: BLE001
                    return {"http": r.status, "body": raw[:400]}
        except urllib.error.HTTPError as e:
            return {"http": e.code, "body": redact(e.read().decode("utf-8", "replace"))[:400]}
        except Exception as e:  # noqa: BLE001
            return {"http": 0, "body": f"{type(e).__name__}: {e}"}

    def _integration_env_lines(self, start_sh: Path | None, prefix: str) -> list[dict]:
        """从 start.sh 里回显 {prefix}PROVIDER/MODEL/BASE_URL/API_KEY 四行（API_KEY 只说有没有）。"""
        if start_sh is None or not prefix:
            return []
        try:
            text = start_sh.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        pat = re.compile(re.escape(prefix) + r"(PROVIDER|MODEL|BASE_URL|API_KEY)=([^\s\\]+)")
        lines: list[dict] = []
        for m in pat.finditer(text):
            name, value = m.group(1), m.group(2).strip('"').strip("'")
            if name == "API_KEY":
                value = "已设置" if value else "未设置"
            else:
                value = redact(value)
            lines.append({"key": prefix + name, "value": value})
        return lines

    def admin_upstream(self) -> dict:
        """GET /admin/upstream：只读探测 integrations.upstream（health + start.sh 的四行 LLM 配置）。

        未配置 integrations.upstream → `{"configured": false}`（HTTP 200），不摸任何上游。
        """
        integ = self.upstream_integration()
        if not integ:
            return {"configured": False}
        start_raw = str(integ.get("start_script") or "")
        start_sh = Path(os.path.expanduser(start_raw)) if start_raw else None
        mtime = ""
        backups: list[str] = []
        if start_sh is not None:
            try:
                mtime = time.strftime("%Y-%m-%dT%H:%M:%S",
                                      time.localtime(start_sh.stat().st_mtime))
            except OSError:
                mtime = ""
            try:
                backups = sorted(p.name for p in start_sh.parent.glob(
                    start_sh.name + ".bak.*"))[-5:]
            except OSError:
                backups = []
        listen = self.cfg.get("listen") or {}
        relay_url = f"http://{listen.get('host') or '127.0.0.1'}:{int(listen.get('port') or 9110)}/v1"
        return {"configured": True,
                "label": str(integ.get("label") or "upstream"),
                "usage_alias": str(integ.get("usage_alias") or ""),
                "health_url": str(integ.get("health_url") or ""),
                "control_plane_url": str(integ.get("control_plane_url") or ""),
                "health": self._integration_health(str(integ.get("health_url") or "")),
                "config_lines": self._integration_env_lines(
                    start_sh, str(integ.get("env_prefix") or "")),
                "start_sh": (str(start_sh) if start_sh else ""),
                "mtime": mtime, "backups": backups, "relay_url": relay_url}

    def admin_rollback_upstream(self, confirm: str) -> tuple[int, dict]:
        """POST /admin/upstream/rollback：必须带 {"confirm":"ROLLBACK"}，否则 400。"""
        if str(confirm or "") != "ROLLBACK":
            return 400, {"detail": "回滚是不可逆操作，body 必须带 {\"confirm\": \"ROLLBACK\"}"}
        integ = self.upstream_integration()
        if not integ:
            return 404, {"detail": "未配置 integrations.upstream，没有可回滚对象", "ok": False}
        raw = str(integ.get("rollback_script") or "")
        if not raw:
            return 404, {"detail": "integrations.upstream.rollback_script 未配置", "ok": False}
        script = Path(os.path.expanduser(raw))
        if not script.is_absolute():
            script = (HERE / script).resolve()
        try:
            proc = subprocess.run([str(script)], cwd=str(script.parent), capture_output=True,
                                  text=True, timeout=180)
            out = (proc.stdout or "") + (proc.stderr or "")
            # 脚本自己已把 sk-/nvapi- 打码；这里再把 API_KEY= 后面一律打成「已设置/未设置」
            out = re.sub(r"(API_KEY=)\S+", r"\1***已设置/未设置***", out)
            self.log(f"管理接口：执行 {script.name}（exit={proc.returncode}）")
            return 200, {"ok": proc.returncode == 0, "exit": proc.returncode,
                         "output": redact(out)[-4000:]}
        except subprocess.TimeoutExpired:
            return 504, {"detail": f"{script.name} 180s 未结束", "ok": False}
        except Exception as e:  # noqa: BLE001
            return 500, {"detail": f"{type(e).__name__}: {e}", "ok": False}

    def admin_reload(self) -> dict:
        self.reload(force=True)
        return {"reloaded": True, "providers": len(self.providers),
                "candidates_healthy": self.healthy_candidates()}

    def panel_html(self) -> str:
        st = self.status()
        rows = []
        for p in st["providers"]:
            def _key_desc(k: dict) -> str:
                parts = [f"#{k['index']} {k['env']} {k['fp']}"]
                if k["disabled"]:
                    parts.append("（禁用：" + html.escape(k["disabled_reason"]) + "）")
                elif k["cooldown_left_s"]:
                    parts.append(f"（整把 key 冷却 {k['cooldown_left_s']:.0f}s "
                                 f"{html.escape(k['cooldown_reason'])}）")
                # P0.6/R4：按模型的冷却明细也上面板，和 /status 保持一致
                for mc in (k.get("model_cooldowns") or []):
                    parts.append(f"（模型冷却 {mc['model']} {mc['left_s']:.0f}s "
                                 f"{html.escape(mc['reason'])}）")
                return " ".join(parts)

            keys = "；".join(_key_desc(k) for k in p["keys"]) or "—"
            models = "；".join(
                f"{html.escape(str(m['id']))}[chain {m['chain']}]"
                + ("(禁用)" if m["disabled"] else "")
                + " ok={ok} 429={n429} bad_json={bj} unusable={un} degraded={dg} dsr={dsr}".format(
                    ok=m["counts"].get("ok", 0), n429=m["counts"].get("ratelimit", 0),
                    bj=m["counts"].get("bad_json", 0), un=m["counts"].get("unusable", 0),
                    dg=m["counts"].get("degraded", 0),
                    dsr=m["counts"].get("degraded_schema_rejected", 0))
                for m in p["models"]
            ) or "—"
            cooldown = f"{p['cooldown_left_s']:.0f}s {html.escape(p['cooldown_reason'])}" if p["cooldown_left_s"] else "—"
            rows.append(
                "<tr><td>{name}{miss}</td><td>{active}</td><td>{cool}</td><td>{keys}</td>"
                "<td>{models}</td><td>{counts}</td></tr>".format(
                    name=html.escape(p["name"]),
                    miss="<br><b>missing_key</b>" if p["missing_key"] else "",
                    active="是" if p["active"] else "否",
                    cool=cooldown,
                    keys=html.escape(keys).replace("；", "<br>"),
                    models=html.escape(models).replace("；", "<br>"),
                    counts=html.escape(json.dumps(p["counts"], ensure_ascii=False)),
                )
            )
        recent_rows = "".join(
            "<tr><td>{ts}</td><td>{alias}</td><td>{p}/{m}</td><td>#{ki} {kf}</td><td>{stt}</td>"
            "<td>{lat}s</td><td>{tok}</td><td>{dg}</td><td>{kinds}</td></tr>".format(
                ts=html.escape(str(r["ts"])), alias=html.escape(str(r["alias"])),
                p=html.escape(str(r["provider"])), m=html.escape(str(r["model"])),
                ki=r["key_index"], kf=html.escape(str(r["key_fp"])), stt=r["status"],
                lat=r["latency_s"], tok=r["total_tokens"], dg="降级" if r["degraded"] else "",
                kinds=html.escape(",".join(str(k) for k in r["attempt_kinds"])),
            )
            for r in reversed(st["recent"])
        )
        return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>llm-relay 状态面板</title><style>
:root {{ color-scheme: dark; }}
body {{ background:#14161a; color:#e6e8eb; font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;
       margin:24px; font-size:13px; }}
h1 {{ font-size:18px; margin:0 0 6px; }} h2 {{ font-size:15px; margin:26px 0 8px; color:#9ecbff; }}
.meta {{ color:#9aa4b2; margin-bottom:14px; }} .meta b {{ color:#7ee787; }}
table {{ border-collapse:collapse; width:100%; margin-bottom:10px; }}
th,td {{ border:1px solid #2b3038; padding:6px 8px; text-align:left; vertical-align:top; }}
th {{ background:#1c2027; color:#9ecbff; font-weight:600; }}
tr:nth-child(even) td {{ background:#181b20; }}
.ok {{ color:#7ee787; }} .warn {{ color:#f0b429; }} .bad {{ color:#ff7b72; }}
</style></head><body>
<h1>llm-relay 状态面板</h1>
<div class="meta">状态 <b>{html.escape(st['status'])}</b> ｜ 可用候选 <b>{st['candidates_healthy']}</b> ｜
运行 {st['uptime_s']:.0f}s ｜ 监听 {html.escape(json.dumps(st['listen'], ensure_ascii=False))} ｜
默认别名 <b>{html.escape(str(st['default_alias']))}</b> ｜ 计数器 {html.escape(json.dumps(st['counters'], ensure_ascii=False))}</div>
<h2>Provider / key 池 / 模型</h2>
<table><tr><th>provider</th><th>可用</th><th>provider 冷却</th><th>key 池（index + sha256[:8]）</th><th>模型</th><th>provider 计数</th></tr>
{''.join(rows)}</table>
<h2>最近 20 条请求</h2>
<table><tr><th>时间</th><th>别名</th><th>provider/model</th><th>key</th><th>HTTP</th><th>延迟</th><th>token</th><th>降级</th><th>尝试链</th></tr>
{recent_rows}</table>
<p class="meta">密钥卫生：本页只显示 key_index 与 sha256 前 8 位指纹，永不显示 key 明文。</p>
</body></html>"""


class StreamBody:
    """流式响应转发的薄包装。"""

    def __init__(self, resp, rt: ProviderRuntime) -> None:
        self.resp = resp
        self.rt = rt


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, relay: Relay) -> None:
        super().__init__(addr, handler)
        self.relay = relay


class Handler(BaseHTTPRequestHandler):
    server_version = "llm-relay/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 由 relay.log 统一记录
        return

    # ---------------- 工具 ----------------
    def _json(self, code: int, obj: object, extra_headers: dict | None = None) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _html(self, code: int, text: str) -> None:
        raw = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _authed(self) -> bool:
        token = str((self.server.relay.cfg.get("local_token") or ""))
        if not token:
            return True
        got = self.headers.get("Authorization") or ""
        return got.strip() == f"Bearer {token}"

    def _is_loopback(self) -> bool:
        """来源是不是本机（与 _admin_guard 同一口径）。调用方鉴权靠它决定 loopback_trust。"""
        host = self.client_address[0] if self.client_address else ""
        return bool(host.startswith("127.") or host in ("::1", "localhost", ""))

    def _read_json(self) -> tuple[dict | None, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw.decode("utf-8", "replace") or "{}"), ""
        except Exception as e:  # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"

    # ------------------------------------------------------------ 管理接口（§2）
    def _admin_guard(self) -> bool:
        """访问控制：来源必须是 loopback（否则 403）；local_token 非空时要校验 Bearer（否则 401）。"""
        host = self.client_address[0] if self.client_address else ""
        if not (host.startswith("127.") or host in ("::1", "localhost", "")):
            self._json(403, {"detail": "管理接口仅限本机"})
            return False
        if not self._authed():
            self._json(401, {"error": {"message": "invalid local_token", "type": "relay_auth"}})
            return False
        return True

    @staticmethod
    def _int_q(q: dict, name: str, default: int) -> int:
        try:
            return int((q.get(name) or [default])[0])
        except (TypeError, ValueError):
            return default

    def _admin_get(self, path: str, q: dict) -> None:
        relay: Relay = self.server.relay
        if path == "/admin/config":
            self._json(200, relay.admin_config())
        elif path == "/admin/requests":
            self._json(200, relay.admin_requests(
                limit=self._int_q(q, "limit", 100), offset=self._int_q(q, "offset", 0),
                provider=(q.get("provider") or [""])[0], model=(q.get("model") or [""])[0],
                verdict=(q.get("verdict") or [""])[0], since=(q.get("since") or [""])[0],
                include_probe=str((q.get("probe") or ["0"])[0]) in ("1", "true", "yes")))
        elif path == "/admin/usage":
            group = (q.get("group") or [""])[0]
            window = (q.get("window") or [""])[0]
            if not group and not window and "days" in q:
                # 旧口径（面板首屏/老书签）：按天 × provider 聚合，参数 `days` 保留不变
                self._json(200, relay.admin_usage(days=self._int_q(q, "days", 7)))
                return
            g = group or "caller"          # 缺省 caller
            w = window or "24h"            # 缺省 24h
            if g not in UsageIndex.GROUPS:
                self._json(400, {"detail": f"非法 group「{g}」；可选：{list(UsageIndex.GROUPS)}"})
                return
            if w not in UsageIndex.WINDOWS:
                self._json(400, {"detail": f"非法 window「{w}」；可选：{list(UsageIndex.WINDOWS)}"})
                return
            self._json(200, relay.admin_usage_group(g, w))
        elif path == "/admin/upstream":
            self._json(200, relay.admin_upstream())
        elif path == "/admin/callers":
            # 调用方只读视图（T1.1）：只有「是否配置 key + 指纹」，永不回明文 key
            self._json(200, relay.admin_callers())
        elif path == "/admin/hindsight":  # deprecated 一行别名：等价 /admin/upstream（旧书签）
            self._json(200, relay.admin_upstream())
        else:
            self._json(404, {"detail": "not found"})

    def _admin_post(self, path: str, payload: dict) -> None:
        relay: Relay = self.server.relay
        if path == "/admin/config":
            code, body = relay.admin_patch_config(payload.get("patch"))
            self._json(code, body)
        elif path == "/admin/reorder":
            code, body = relay.admin_reorder(payload.get("order"))
            self._json(code, body)
        elif path == "/admin/model":
            code, body = relay.admin_model(payload)
            self._json(code, body)
        elif path == "/admin/key":
            code, body = relay.admin_add_key(payload.get("provider"), payload.get("env_name"),
                                             payload.get("secret"), payload.get("new_provider"))
            self._json(code, body)
        elif path == "/admin/key/state":
            code, body = relay.admin_key_state(
                payload.get("provider"), payload.get("env"), payload.get("enabled"),
                bool(payload.get("clear_cooldown")), payload.get("reason") or "")
            self._json(code, body)
        elif path == "/admin/key/replace":
            code, body = relay.admin_replace_key(payload.get("provider"),
                                                 payload.get("env_name"), payload.get("secret"))
            self._json(code, body)
        elif path == "/admin/key/remove":
            code, body = relay.admin_remove_key(payload.get("provider"),
                                                payload.get("env_name"), payload.get("confirm"))
            self._json(code, body)
        elif path == "/admin/restart":
            # 先把响应完整 flush 出去，进程才会有 0.3–0.5s 后才退出（交 launchd 拉起）
            code, body = relay.admin_restart(payload.get("confirm"), payload.get("service"),
                                             actor="loopback")
            self._json(code, body)
            try:
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass
        elif path == "/admin/probe":
            code, body = relay.admin_probe(payload.get("provider"), payload.get("model"))
            self._json(code, body)
        elif path == "/admin/reload":
            self._json(200, relay.admin_reload())
        elif path == "/admin/upstream/rollback":
            code, body = relay.admin_rollback_upstream(payload.get("confirm"))
            self._json(code, body)
        elif path == "/admin/hindsight/rollback":  # deprecated 一行别名：等价 /admin/upstream/rollback
            code, body = relay.admin_rollback_upstream(payload.get("confirm"))
            self._json(code, body)
        else:
            self._json(404, {"detail": "not found"})

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:
        relay: Relay = self.server.relay
        path = urllib.parse.urlparse(self.path).path
        if path == "/health":
            n = relay.healthy_candidates()
            body = {"status": "healthy" if n > 0 else "degraded", "candidates_healthy": n}
            self._json(200 if n > 0 else 503, body)
            return
        if path == "/metrics":
            # T1.3：可选 Prometheus 指标。与 /admin/* 同一访问控制（只许 loopback；
            # local_token 非空时校验 Bearer）；未开启 usage_log.metrics 时明确 404，不回空 200。
            if not self._is_loopback():
                self._json(403, {"detail": "metrics 仅限本机"})
                return
            if not self._authed():
                self._json(401, {"error": {"message": "invalid local_token", "type": "relay_auth"}})
                return
            if not relay.metrics_enabled():
                self._json(404, {"detail": "metrics 未开启（usage_log.metrics=false）"})
                return
            raw = relay.metrics_text().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path.startswith("/admin/"):
            if not self._admin_guard():
                return
            self._admin_get(path, urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query))
            return
        if not self._authed():
            self._json(401, {"error": {"message": "invalid local_token", "type": "relay_auth"}})
            return
        if path == "/status":
            self._json(200, relay.status())
        elif path == "/v1/models":
            self._json(200, relay.models_payload())
        elif path in ("/", "/panel"):
            self._html(200, relay.panel_html())
        else:
            self._json(404, {"error": {"message": "not found", "type": "relay_404"}})

    def do_POST(self) -> None:
        relay: Relay = self.server.relay
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/admin/"):
            if not self._admin_guard():
                return
            payload, err = self._read_json()
            if payload is None:
                self._json(400, {"detail": f"invalid JSON body: {err}"})
                return
            try:
                self._admin_post(path, payload)
            except Exception as e:  # noqa: BLE001  管理接口失败也要有清晰回包，别把异常打成空响应
                relay.log(f"管理接口 {path} 失败：{type(e).__name__}: {e}")
                self._json(500, {"detail": f"{type(e).__name__}: {redact(e)}"})
            return
        if path in ("/v1/chat/completions", "/chat/completions"):
            # 调用方鉴权（T1.1/T1.4）：Bearer 命中某个 caller → 记名调用方；
            # 本机且 auth.mode=loopback_trust 时允许匿名；require_key 或非本机无 key → 401。
            caller, ccode, cbody = relay.resolve_caller(
                self.headers.get("Authorization") or "", self._is_loopback())
            if ccode:
                self._json(ccode, cbody)
                return
            payload, err = self._read_json()
            if payload is None:
                self._json(400, {"error": {"message": f"invalid JSON body: {err}",
                                           "type": "relay_bad_request"}})
                return
            status, body, meta = relay.chat(payload, client=self.client_address[0], auth=caller)
            if isinstance(body, StreamBody):
                self._stream(body)
                return
            extra: dict = {}
            if isinstance(meta, dict) and meta.get("retry_after"):
                # 冷却/调用方限流类 429 必须带 reset 信息，调用方靠它决定退避多久
                extra["Retry-After"] = str(int(round(float(meta["retry_after"]))))
            self._json(status, body, extra)
            return
        if not self._authed():
            self._json(401, {"error": {"message": "invalid local_token", "type": "relay_auth"}})
            return
        self._json(404, {"error": {"message": "not found", "type": "relay_404"}})

    def _stream(self, sb: StreamBody) -> None:
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            for k, v in (sb.resp.headers or {}).items():
                if k.lower() in ("content-type", "content-length", "transfer-encoding", "connection"):
                    continue
                self.send_header(k, v)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = sb.resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                sb.resp.close()
            except Exception:  # noqa: BLE001
                pass
            self.server.relay._release(sb.rt)
            self.close_connection = True


def create_server(relay: Relay, host: str | None = None, port: int | None = None) -> RelayHTTPServer:
    listen = relay.cfg.get("listen") or {}
    return RelayHTTPServer((host or listen.get("host", "127.0.0.1"), int(port or listen.get("port", 9110))),
                           Handler, relay)


def init_config(target: Path | str | None = None, example: Path | str = CONFIG_EXAMPLE) -> int:
    """用 config.example.json 生成一份本地配置；目标已存在则**拒绝覆盖**（不碰已有文件）。

    新克隆的用法：`python3 llm_relay.py --init` → 填 keys.env → 起服务。
    """
    target, example = Path(target or DEFAULT_CONFIG), Path(example)
    if target.exists():
        print(f"拒绝覆盖：{target} 已存在（本机真实配置）。要重新生成请先自己备份/挪走它。")
        return 1
    if not example.exists():
        print(f"缺少模板：{example} 不存在，没法生成配置。")
        return 1
    try:
        data = example.read_text(encoding="utf-8")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)          # 原子落地：要么没有，要么是完整的一份
    except OSError as exc:
        print(f"生成失败：{target}（{type(exc).__name__}: {exc}）")
        return 1
    print(f"已生成配置：{target}（来自 {example}）")
    print(f"下一步：把各 provider 的密钥写进 {target.parent / 'keys.env'}（形如 KEY_NAME=值），"
          f"再跑 python3 {Path(__file__).name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="llm-relay：零依赖 · OpenAI 兼容 · 多厂商免费额度治理中继")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--keys", default=str(DEFAULT_KEYS))
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--check", action="store_true", help="只做配置体检并打印候选，不起服务")
    ap.add_argument("--init", action="store_true",
                    help="用 config.example.json 生成一份本地配置（已存在则拒绝覆盖），然后退出")
    args = ap.parse_args(argv)

    if args.init:
        return init_config(args.config)

    # 回环一律绕代理（launchd 里也会给 NO_PROXY，这里再兜一层）
    for var in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(var, "")
        if "127.0.0.1" not in cur:
            os.environ[var] = (cur + ",localhost,127.0.0.1,::1").strip(",")

    relay = Relay(args.config, args.keys)
    if args.check:
        cands = relay.build_candidates(False, False)
        print(f"配置体检：providers={len(relay.providers)} 可用候选={len(cands)} 全局并发={relay.cfg.get('concurrency')}")
        for c in cands:
            print(f"  - {c.provider}/{c.model_id} chain={c.chain} json_schema={c.json_schema}")
        return 0

    def _hup(signum, frame):  # noqa: ARG001
        relay.log(f"收到信号 {signum} → 下一次请求前强制热重载")
        relay.request_reload()

    signal.signal(signal.SIGHUP, _hup)
    server = create_server(relay, args.host, args.port)
    relay.log(f"llm-relay 监听 http://{server.server_address[0]}:{server.server_address[1]}"
              f"（别名 {relay.cfg.get('default_alias')}）")
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
