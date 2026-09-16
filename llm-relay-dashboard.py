#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""llm-relay-dashboard.py —— llm-relay 管理前端（单文件、纯标准库、暗色）。

架构（TASK-DASHBOARD §1）：

    手机/局域网/Tailscale ─┐
                           ├─> 本面板 :9111（0.0.0.0，访问密钥门，读写分流）
    本机浏览器 ────────────┘        │  服务端代理（ProxyHandler({}) 绕代理）
                                    ↓
                              llm-relay :9110（127.0.0.1，状态唯一拥有者）
                                ├─ /status /health /v1/models /panel
                                ├─ /admin/*   （校验 + 备份 + 原子写 + 热重载都在中转站侧）
                                └─ usage.jsonl

安全模型（与已发布的 hindsight-dashboard 保持一致）：
  · 本机（loopback）免密；远程访问需要 ?k=<密钥> 或 Cookie（HttpOnly + SameSite=Lax + 30 天）；
    带 ?k= 进来后 302 把密钥从地址栏抹掉；无密钥只给登录页（不含任何状态数据），/api/* 一律 401。
  · 远程（非 loopback）只能读：任何 POST 一律 403。
  · 写操作全部转发给中转站的 loopback-only /admin/*。
  · 密钥只以「变量名 + sha256 前 8 位指纹」出现，任何地方都不打印明文。

用法：
    ~/hindsight-mac-env/bin/python llm-relay-dashboard.py            # 0.0.0.0:9111
    ~/hindsight-mac-env/bin/python llm-relay-dashboard.py --host 127.0.0.1 --port 9111
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import pwd
import re
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
RELAY_BASE = os.getenv("LLM_RELAY_BASE", "http://127.0.0.1:9110").rstrip("/")
DEFAULT_KEY_FILE = "~/.hermes/llm-relay/access-key.txt"
KEY_COOKIE = "llm_relay_key"

# ---- 面板联动（方案 C）：Hindsight 侧的三个只读上游 + 深链接目标 ----
# 全部只用于「服务端只读取数」，浏览器不会直连这些地址；地址可用环境变量覆盖（自检里指到假地址复现降级）。
HINDSIGHT_API_BASE = os.getenv("HINDSIGHT_API_BASE", "http://127.0.0.1:8988").rstrip("/")
HINDSIGHT_CP_BASE = os.getenv("HINDSIGHT_CP_BASE", "http://127.0.0.1:9999").rstrip("/")
HINDSIGHT_DASH_BASE = os.getenv("HINDSIGHT_DASH_BASE", "http://127.0.0.1:8990").rstrip("/")
# Hindsight 本体插件配置（只读它拿 bank_id）与官方 CP 的访问密钥文件。
# 官方 CP 的 /api/* 走 access_key 会话，服务端只读取数需要同一把钥匙；
# 只读文件、只在本机回环使用，绝不回显明文（报告/日志里只出现路径与指纹）。
HINDSIGHT_CONFIG_JSON = os.path.expanduser(
    os.getenv("HS_DASH_CONFIG_JSON", "~/.hermes/hindsight/config.json"))
HINDSIGHT_CP_KEY_FILE = os.path.expanduser(
    os.getenv("HINDSIGHT_CP_KEY_FILE", "~/.hermes/hindsight/access-key.txt"))
HINDSIGHT_CP_ACCESS_KEY = os.getenv("HINDSIGHT_CP_ACCESS_KEY", "")

# 关键：绕过系统代理。ClashX 之类会把回环请求接管成 502（本项目反复踩过的坑）。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ACCESS_KEY_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # A-Z 去掉 I/O + 2-9
ACCESS_KEY_LEN = 16


def key_fp(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:8]


def resolve_key_path(path: str) -> str:
    """把访问密钥路径解析成**绝对路径**。

    launchd 环境下若 HOME 缺失，`expanduser("~")` 有可能落到相对路径上，密钥文件就会被写进
    工作目录（仓库里）—— 那是不能接受的。所以这里显式保证：不是绝对路径就挂到用户主目录下。
    """
    p = os.path.expanduser(str(path))
    if p.startswith("~"):          # HOME 异常 → 用 pwd 兜底
        try:
            p = pwd.getpwuid(os.getuid()).pw_dir + p[1:]
        except (KeyError, ImportError):
            pass
    if not os.path.isabs(p):
        home = os.environ.get("HOME") or ""
        if not os.path.isabs(home):
            try:
                home = pwd.getpwuid(os.getuid()).pw_dir
            except (KeyError, ImportError):
                home = os.path.abspath(os.curdir)
        p = os.path.join(home, p)
    return os.path.abspath(p)


def load_or_create_access_key(path: str) -> str:
    """读访问密钥；文件不存在就按 hindsight-dashboard 同款字符表生成（600）。只打印指纹。"""
    path = resolve_key_path(path)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            key = fh.read().strip()
            if key:
                return key
    except OSError:
        pass
    key = "".join(secrets.choice(ACCESS_KEY_CHARS) for _ in range(ACCESS_KEY_LEN))
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key + "\n")
        print(f"[llm-relay-dashboard] 已生成访问密钥 {path}（长度 {len(key)}，"
              f"sha256[:8]={key_fp(key)}，权限 600）", flush=True)
    except OSError as e:
        print(f"[llm-relay-dashboard] 访问密钥写入失败：{type(e).__name__}: {e}", flush=True)
    return key


# ---------------------------------------------------------------- 外观（背景图）
# 面板自己拥有的 UI 偏好走单独的 ui.json，**不写进 config.json**（那里有实测调参的红线）。
# 存放目录由 --key-file 的目录决定：生产落在 ~/.hermes/llm-relay/，自检落在临时目录，不污染仓库。
UI_DIR = os.path.dirname(resolve_key_path(DEFAULT_KEY_FILE))
BG_MAX_BYTES = 8 * 1024 * 1024
BG_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
DEFAULT_UI = {"enabled": True, "file": "", "overlay": 60, "blur": 0,
              "pos": "center center", "fit": "cover", "cardA": 88}
CARD_A_MIN, CARD_A_MAX = 55, 100
SAFE_ASSET = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
BG_POS = re.compile(r"^(left|center|right|\d{1,3}%)[ ]+(top|center|bottom|\d{1,3}%)$")
BG_FITS = ("cover", "contain")


def norm_pos(v: object) -> str:
    """对齐方式：接受 "top left"（面板九宫格标签）与 "left top"（CSS 习惯）两种写法，
    数字夹到 0–100；无法识别的值退回 center center。

    教训：原实现只看「第一段必须是 left/center/right」，于是九宫格给的 "top left"、
    "center left" 全被判非法、静默退回 center center —— 九个按钮表面能用、实际无效。
    校验必须按关键字归轴（left/right→x，top/bottom→y），且 center 是歧义的：
    要先认掉明确关键字，再把 center / 百分比填进剩下的空槽。
    """
    parts = " ".join(str(v or "").split()).split()
    if len(parts) != 2:
        return str(DEFAULT_UI["pos"])
    x = y = None
    rest = []
    for tok in parts:
        if tok in ("left", "right") and x is None:
            x = tok
        elif tok in ("top", "bottom") and y is None:
            y = tok
        else:
            rest.append(tok)
    for tok in rest:
        if tok == "center":
            val = "center"
        else:
            m = re.fullmatch(r"(\d{1,3})%", tok)
            if not m:
                return str(DEFAULT_UI["pos"])
            val = f"{_clamp_num(m.group(1), 0, 100, 50)}%"
        if x is None:
            x = val
        elif y is None:
            y = val
        else:
            return str(DEFAULT_UI["pos"])
    if x is None or y is None:
        return str(DEFAULT_UI["pos"])
    return f"{x} {y}"


def norm_fit(v: object) -> str:
    """缩放：cover（铺满、裁掉多余）或 contain（完整、留边）。"""
    s = str(v or "").strip().lower()
    return s if s in BG_FITS else str(DEFAULT_UI["fit"])


def ui_path() -> str:
    return os.path.join(UI_DIR, "ui.json")


def assets_dir() -> str:
    return os.path.join(UI_DIR, "assets")


def bg_cache_token() -> str:
    """按壁纸文件的 mtime+size 做缓存令牌，换了图浏览器一定重新拉（避免看到旧图）。"""
    try:
        st = os.stat(os.path.join(assets_dir(), load_ui()["file"]))
        return f"{int(st.st_mtime)}-{st.st_size}"
    except Exception:  # noqa: BLE001
        return "0"


def _clamp_num(v, lo, hi, dflt):
    try:
        n = float(v)
    except (TypeError, ValueError):
        n = float(dflt)
    return int(min(hi, max(lo, n)))


def load_ui() -> dict:
    bg = dict(DEFAULT_UI)
    try:
        with open(ui_path(), "r", encoding="utf-8") as fh:
            raw = json.load(fh) or {}
        got = raw.get("background") or {}
        bg["enabled"] = bool(got.get("enabled", True))
        bg["file"] = str(got.get("file") or "")
        bg["overlay"] = _clamp_num(got.get("overlay", 60), 0, 95, 60)
        bg["blur"] = _clamp_num(got.get("blur", 0), 0, 24, 0)
        bg["pos"] = norm_pos(got.get("pos"))
        bg["fit"] = norm_fit(got.get("fit"))
        bg["cardA"] = _clamp_num(got.get("cardA", 88), CARD_A_MIN, CARD_A_MAX, 88)
    except (OSError, ValueError):
        pass
    # 图片被手工删掉时不要把一张载不到的图写进页面
    if bg["file"] and not os.path.isfile(os.path.join(assets_dir(), bg["file"])):
        bg["file"] = ""
    return bg


def save_ui(bg_patch: dict) -> dict:
    """只动传入的键，其余原样保留；tmp + os.replace 原子写。"""
    cur = load_ui()
    for k in ("enabled", "file", "overlay", "blur", "pos", "fit", "cardA"):
        if k in bg_patch:
            cur[k] = bg_patch[k]
    cur["enabled"] = bool(cur["enabled"])
    cur["file"] = str(cur["file"] or "")
    cur["overlay"] = _clamp_num(cur["overlay"], 0, 95, 60)
    cur["blur"] = _clamp_num(cur["blur"], 0, 24, 0)
    cur["pos"] = norm_pos(cur.get("pos"))
    cur["fit"] = norm_fit(cur.get("fit"))
    cur["cardA"] = _clamp_num(cur.get("cardA"), CARD_A_MIN, CARD_A_MAX, 88)
    os.makedirs(UI_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=UI_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"background": cur}, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, ui_path())
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cur


def sniff_image(raw: bytes) -> str | None:
    """按 magic bytes 认类型 —— 不信调用方声明的 mime，也不信文件名后缀。"""
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def parse_data_url(s: object) -> tuple[str, bytes] | None:
    if not isinstance(s, str) or not s.startswith("data:") or "," not in s:
        return None
    head, _, b64 = s.partition(",")
    if ";base64" not in head:
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    return (head[len("data:"):].split(";")[0] or "application/octet-stream"), raw


def safe_asset_path(name: str) -> str | None:
    """只允许 assets/ 下的单层文件名，且 realpath 必须仍在 assets/ 内。"""
    if not SAFE_ASSET.match(name or ""):
        return None
    base = os.path.realpath(assets_dir())
    full = os.path.realpath(os.path.join(base, name))
    if os.path.dirname(full) != base:
        return None
    return full


def upstream(method: str, path: str, params: dict | None = None, payload: object = None,
             timeout: float = 30.0) -> tuple[int, object]:
    """转发给 llm-relay:9110。返回 (http_status, json_or_detail)，绝不吞掉 4xx/5xx。"""
    url = RELAY_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    data = None
    headers = {"Accept": "application/json", "User-Agent": "llm-relay-dashboard/1.0"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, (json.loads(raw) if raw.strip() else {})
            except json.JSONDecodeError:
                return resp.status, {"detail": f"上游返回的不是 JSON：{raw[:200]}"}
    except urllib.error.HTTPError as e:
        raw = ""
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        try:
            return e.code, (json.loads(raw) if raw.strip() else {"detail": f"HTTP {e.code}"})
        except json.JSONDecodeError:
            return e.code, {"detail": raw[:400] or f"HTTP {e.code}"}
    except Exception as e:  # noqa: BLE001
        return 0, {"detail": f"连不上中转站 {RELAY_BASE}：{type(e).__name__}: {e}"}


def _ts_epoch(ts: object) -> float:
    try:
        return time.mktime(time.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:  # noqa: BLE001
        return 0.0


def _count(rows: list, field: str) -> dict:
    out: dict = {}
    for r in rows:
        if isinstance(r, dict):
            k = str(r.get(field) or "-")
            out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- 面板联动（方案 C）
# 「Hindsight」tab 的只读取数：服务端去摸 8988（本体）/ 9999（官方 CP），
# 再从 usage.jsonl（经中转站 /admin/requests）聚合 alias=hindsight 的今日与近 1h 指标。
# 任何一项上游挂掉都只把那一项标成 {"error": ...}，绝不让整个接口 500（任务书 §1 R1）。
def _hs_json(url: str, timeout: float = 8.0, cookie: str = "") -> tuple[int, object, str]:
    """GET 一个上游 JSON。返回 (http_status, 对象, 错误串)；连不上时 http_status=0。"""
    headers = {"Accept": "application/json", "User-Agent": "llm-relay-dashboard/1.0"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            if not raw.strip():
                return resp.status, {}, ""
            try:
                return resp.status, json.loads(raw), ""
            except json.JSONDecodeError:
                return resp.status, None, f"上游返回的不是 JSON：{raw[:160]}"
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return 0, None, f"{type(e).__name__}: {e}"


def hindsight_bank() -> str:
    """bank_id 从 ~/.hermes/hindsight/config.json 只读读出（读不到就退回 default）。"""
    try:
        with open(HINDSIGHT_CONFIG_JSON, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        bank = str((cfg or {}).get("bank_id") or "").strip()
        return bank or "default"
    except Exception:  # noqa: BLE001
        return "default"


def _cp_access_key() -> str:
    """官方 CP 的访问密钥：优先环境变量，其次密钥文件。只读，且只在本机回环用。"""
    if HINDSIGHT_CP_ACCESS_KEY:
        return HINDSIGHT_CP_ACCESS_KEY
    try:
        with open(HINDSIGHT_CP_KEY_FILE, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _cp_login_cookie() -> str:
    """官方 CP 的 /api/* 走访问密钥会话（POST /api/auth/login）。

    8988 的 /v1/... 免密，但 9999 的 /api/stats/<bank> 实测匿名是 401；服务端要读统计
    就得用同一把 CP 访问密钥换一个会话 cookie。这里只在本机回环发一次登录请求，
    不写 config、不写 usage、不碰任何文件；密钥明文绝不进响应体/日志。
    """
    key = _cp_access_key()
    if not key:
        return ""
    data = json.dumps({"key": key}).encode("utf-8")
    req = urllib.request.Request(
        HINDSIGHT_CP_BASE + "/api/auth/login", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "llm-relay-dashboard/1.0"})
    try:
        with OPENER.open(req, timeout=8) as resp:
            resp.read()
            for k, val in resp.headers.items():
                if k.lower() == "set-cookie":
                    return val.split(";", 1)[0]
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _cp_stats(bank: str) -> tuple[dict | None, str]:
    """官方 CP 的记忆规模：/api/stats/<bank>（记忆/链接/文档）。返回 (对象, 错误串)。"""
    url = f"{HINDSIGHT_CP_BASE}/api/stats/{urllib.parse.quote(bank)}"
    code, obj, err = _hs_json(url, timeout=10)
    if code == 200 and isinstance(obj, dict) and not obj.get("error"):
        return obj, ""
    if code in (401, 403):
        cookie = _cp_login_cookie()
        if cookie:
            code2, obj2, err2 = _hs_json(url, timeout=10, cookie=cookie)
            if code2 == 200 and isinstance(obj2, dict) and not obj2.get("error"):
                return obj2, ""
            return None, (err2 or f"HTTP {code2}")
    return None, (err or f"HTTP {code}")


def _usage_group(rows: list) -> dict:
    lats = [int(r.get("latency_ms") or 0) for r in rows if int(r.get("latency_ms") or 0) > 0]
    ok = sum(1 for r in rows if int(r.get("http") or 0) == 200)
    return {"n": len(rows), "ok": ok, "fail": len(rows) - ok,
            "rate": round(ok / len(rows), 4) if rows else None,
            "avg_ms": round(sum(lats) / len(lats)) if lats else None,
            "n429": sum(1 for r in rows if int(r.get("http") or 0) == 429)}


def build_upstream_usage(alias: str = "hindsight") -> dict:
    """usage.jsonl 里 alias=<调用方别名> 的今日 / 近 1h 次数与成功率（经中转站读，只读）。

    别名的来源是``integrations.upstream.usage_alias``（缺省回退到 ``UPSTREAM_USAGE_ALIAS`` 环境变量，
    再回退 "hindsight"）；中继核心不写死任何调用方名，这里只是这个集成的聚合口径。
    """
    code, body = upstream("GET", "/admin/requests", {"limit": 2000, "probe": 1}, timeout=20)
    if code != 200 or not isinstance(body, dict):
        return {"error": "读不到 usage.jsonl（中转站 "
                         + ("连不上" if not code else "HTTP " + str(code)) + "）"}
    rows = [r for r in (body.get("rows") or [])
            if isinstance(r, dict) and str(r.get("alias") or "") == alias]
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    hour = [r for r in rows if _ts_epoch(r.get("ts")) >= now - 3600]
    todays = [r for r in rows if str(r.get("ts") or "")[:10] == today]
    return {"today": _usage_group(todays), "hour": _usage_group(hour),
            "total_rows": len(rows), "source": f"usage.jsonl · alias={alias}"}


def upstream_meta() -> dict:
    """问中转站 /admin/upstream 要集成元数据（configured / label / usage_alias…）。

    连不上，或中转站还是旧版（没有该端点）→ `{}`：此时**不隐藏**标签页，保持旧行为可用。
    """
    code, body = upstream("GET", "/admin/upstream", timeout=8)
    return body if (code == 200 and isinstance(body, dict)) else {}


def build_upstream() -> dict:
    """GET /api/upstream：三张状态卡 + 「中转站为这个上游干了多少活」的紧凑 JSON。

    `integrations.upstream` 未配置（中转站回 configured:false）→ 直接回四组空对象 +
    `configured:false`，**不摸任何上游**；面板据此隐藏该标签页。旧 `/api/hindsight` 等价。
    """
    meta = upstream_meta()
    if meta.get("configured") is False:
        return {"configured": False, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "label": None, "bank": None, "dash": HINDSIGHT_DASH_BASE,
                "api": {}, "cp": {}, "stats": {}, "relay_usage": {}}
    alias = str(meta.get("usage_alias") or os.getenv("UPSTREAM_USAGE_ALIAS") or "hindsight")
    bank = hindsight_bank()
    out: dict = {"configured": (True if meta else None),
                 "label": meta.get("label") or "upstream",
                 "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "bank": bank,
                 "dash": HINDSIGHT_DASH_BASE, "api": {}, "cp": {}, "stats": {},
                 "relay_usage": build_upstream_usage(alias)}
    code, obj, err = _hs_json(HINDSIGHT_API_BASE + "/health", timeout=6)
    if code == 200 and isinstance(obj, dict):
        out["api"] = {"status": obj.get("status"), "database": obj.get("database"), "http": code}
    else:
        out["api"] = {"error": err or f"HTTP {code}"}
    code, obj, err = _hs_json(HINDSIGHT_CP_BASE + "/api/health", timeout=6)
    if code == 200 and isinstance(obj, dict):
        out["cp"] = {"status": obj.get("status"), "dataplane": obj.get("dataplane"), "http": code}
    else:
        out["cp"] = {"error": err or f"HTTP {code}"}
    st, serr = _cp_stats(bank)
    if st:
        out["stats"] = {"bank": bank, "memories": st.get("total_nodes"),
                        "links": st.get("total_links"), "documents": st.get("total_documents")}
    else:
        out["stats"] = {"error": serr or "取不到官方 CP 统计"}
    return out


def build_callers() -> dict:
    """GET /api/callers：中转站 /admin/callers 的只读代理（T1.1 的「调用方」标签页）。

    中转站没配 `callers`（configured:false）、或还是旧版没有该端点 → 回 configured:false，
    面板据此隐藏标签页。这里只透传「是否配置 key + 指纹 + 配额用量」，**不含任何明文 key**。
    """
    code, body = upstream("GET", "/admin/callers", timeout=10)
    if code == 200 and isinstance(body, dict):
        body.setdefault("configured", True)
        body["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return body
    return {"configured": False, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "auth_mode": None, "callers": [],
            "note": ("中转站没有 `callers` 段，或该中转站版本还不支持 /admin/callers"
                     if code == 404 else f"取不到调用方信息（HTTP {code}）")}


def build_summary() -> dict:
    """概览聚合：健康候选 / 近 1h 请求 / 成功率 / p50 延迟 / 今日 token / 告警 / 首选模型。"""
    hcode, health = upstream("GET", "/health", timeout=10)
    scode, status = upstream("GET", "/status", timeout=15)
    rcode, req = upstream("GET", "/admin/requests", {"limit": 2000, "probe": 1}, timeout=20)
    all_rows = (req.get("rows") or []) if isinstance(req, dict) else []
    # 概览的「请求数」只算真实业务请求；probe_* 是面板手动实测，单独计数（用于真源冒烟取证）
    rows = [r for r in all_rows if r.get("alias") != "probe"]
    probe_total = len(all_rows) - len(rows)
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    hour = [r for r in rows if _ts_epoch(r.get("ts")) >= now - 3600]
    todays = [r for r in rows if str(r.get("ts") or "")[:10] == today]
    lats = sorted(int(r.get("latency_ms") or 0) for r in (hour or rows))
    p50 = lats[len(lats) // 2] if lats else 0
    ok1 = sum(1 for r in hour if int(r.get("http") or 0) == 200)
    providers = (status.get("providers") or []) if isinstance(status, dict) else []

    # 首选模型 = chain 最小的「provider active + 未冷却 + 该模型未禁用 + 有可用 key」
    best, best_chain = None, 10 ** 9
    for p in providers:
        if not p.get("active") or float(p.get("cooldown_left_s") or 0) > 0:
            continue
        usable = [k for k in (p.get("keys") or [])
                  if not k.get("disabled") and not (k.get("model_cooldowns") or [])]
        if not usable:
            continue
        for m in (p.get("models") or []):
            ch = m.get("chain")
            if m.get("disabled") or ch is None:
                continue
            if int(ch) < best_chain:
                best, best_chain = f"{p.get('name')}/{m.get('id')}", int(ch)

    alerts: list[dict] = []
    if not isinstance(health, dict) or health.get("status") != "healthy":
        alerts.append({"level": "bad",
                       "text": f"中转站健康检查异常：{json.dumps(health, ensure_ascii=False)[:200]}"})
    for p in providers:
        if p.get("missing_key"):
            alerts.append({"level": "warn",
                           "text": f"{p.get('name')}：keys.env 里没有可用的 key（missing_key）"})
        if float(p.get("cooldown_left_s") or 0) > 0:
            alerts.append({"level": "warn",
                           "text": f"{p.get('name')} provider 冷却中，剩 "
                                   f"{round(float(p['cooldown_left_s']))}s（{p.get('cooldown_reason') or ''}）"})
        for k in (p.get("keys") or []):
            if k.get("disabled"):
                alerts.append({"level": "bad", "text": f"{p.get('name')} {k.get('env')} "
                                                       f"#{k.get('index')} 已禁用：{k.get('disabled_reason') or '-'}"})
            if float(k.get("cooldown_left_s") or 0) > 0:
                alerts.append({"level": "warn", "text": f"{p.get('name')} {k.get('env')} 整把 key 冷却 "
                                                       f"{round(float(k['cooldown_left_s']))}s"
                                                       f"（{k.get('cooldown_reason') or ''}）"})
            for mc in (k.get("model_cooldowns") or []):
                alerts.append({"level": "warn", "text": f"{p.get('name')} {k.get('env')} 模型 "
                                                       f"{mc.get('model')} 冷却 "
                                                       f"{round(float(mc.get('left_s') or 0))}s"
                                                       f"（{mc.get('reason') or ''}）"})
    n429 = sum(1 for r in hour if int(r.get("http") or 0) == 429)
    if n429:
        alerts.append({"level": "warn", "text": f"近 1 小时有 {n429} 次 429（上游限流，不只是代码问题）"})
    recent = list(status.get("recent") or [])[-8:] if isinstance(status, dict) else []
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "relay": RELAY_BASE,
        "health": health, "health_http": hcode, "status_http": scode, "requests_http": rcode,
        "status": (status.get("status") if isinstance(status, dict) else None),
        "candidates_healthy": (status.get("candidates_healthy") if isinstance(status, dict)
                               else (health or {}).get("candidates_healthy")),
        "uptime_s": (status.get("uptime_s") if isinstance(status, dict) else None),
        "requests_1h": len(hour), "requests_today": len(todays),
        "success_rate_1h": round(ok1 / len(hour), 4) if hour else None,
        "p50_latency_ms": p50,
        "tokens_today": sum(int(r.get("total_tokens") or 0) for r in todays),
        "n429_1h": n429,
        "verdicts_1h": _count(hour, "verdict"),
        "top_model": best,
        "alerts": alerts,
        "recent": list(reversed(recent)),
        "usage_enabled": bool(req.get("enabled")) if isinstance(req, dict) else False,
        "usage_total": (req.get("total") if isinstance(req, dict) else None),
        "probe_total": probe_total,
    }


def build_caps() -> dict:
    """能力矩阵：provider × 模型 × caps × 近期实测结果（usage.jsonl 里 alias=probe 的行）。"""
    scode, status = upstream("GET", "/status", timeout=15)
    pcode, probes = upstream("GET", "/admin/requests", {"limit": 500, "probe": 1}, timeout=20)
    rows = (probes.get("rows") or []) if isinstance(probes, dict) else []
    latest: dict = {}
    for r in rows:
        verdict = str(r.get("verdict") or "")
        if not verdict.startswith("probe_"):
            continue
        bucket = latest.setdefault((r.get("provider"), r.get("model")), {})
        bucket[verdict.replace("probe_", "")] = {"http": r.get("http"),
                                                 "latency_ms": r.get("latency_ms"),
                                                 "ts": r.get("ts")}
    models = []
    if isinstance(status, dict):
        for p in status.get("providers") or []:
            for m in p.get("models") or []:
                models.append({
                    "provider": p.get("name"), "model": m.get("id"), "chain": m.get("chain"),
                    "caps": m.get("caps") or {}, "params": m.get("params") or {},
                    "disabled": m.get("disabled"), "disabled_reason": m.get("disabled_reason"),
                    "counts": m.get("counts") or {}, "provider_active": p.get("active"),
                    "provider_cooldown_s": p.get("cooldown_left_s"),
                    "probes": latest.get((p.get("name"), m.get("id")), {}),
                })
    models.sort(key=lambda m: (m.get("chain") if m.get("chain") is not None else 9999,
                               str(m.get("provider")), str(m.get("model"))))
    return {"models": models, "status_http": scode, "probe_http": pcode,
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


LOGIN_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>llm-relay 管理面板 · 需要密钥</title><style>
:root{color-scheme:dark}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px 26px;max-width:440px;width:86%}
h1{font-size:17px;margin:0 0 10px}p{color:#8b949e;line-height:1.7;font-size:13px;margin:0 0 8px}
code{background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:2px 6px;color:#79c0ff}
</style></head><body><div class="card">
<h1>llm-relay 管理面板</h1>
<p>这个面板可以从局域网/手机访问，所以需要访问密钥。</p>
<p>请在网址后面加上 <code>?k=你的密钥</code>（密钥在 <code>~/.hermes/llm-relay/access-key.txt</code>），
打开一次之后浏览器会记住 30 天。</p>
<p>本机（loopback）访问免密。</p>
</div></body></html>"""

PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>llm-relay 管理面板</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--bg2:#161b22;--bg3:#1c2128;--bd:#30363d;--fg:#e6edf3;
--muted:#8b949e;--ok:#3fb950;--warn:#d29922;--bad:#f85149;--gap:12px;
--t:15px;--b:13px;--n:11.5px;--num:26px}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--fg);margin:0;font-size:var(--b);line-height:1.55;
font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif}
header{padding:14px 18px 10px;border-bottom:1px solid var(--bd);background:var(--bg2);
position:sticky;top:0;z-index:20}
h1{font-size:var(--t);margin:0 0 4px;display:inline-block;margin-right:10px}
header .sub{color:var(--muted);font-size:var(--n)}
nav{display:flex;gap:6px;overflow-x:auto;padding:8px 18px;border-bottom:1px solid var(--bd);
background:var(--bg);position:sticky;top:0;z-index:19}
nav a{color:var(--fg);text-decoration:none;padding:7px 12px;border:1px solid var(--bd);
border-radius:999px;background:var(--bg2);white-space:nowrap;font-size:var(--n);min-height:34px;display:inline-flex;
align-items:center}
nav a.active{border-color:#6e7681;color:var(--fg);background:var(--bg3)}
main{padding:14px 18px 60px;max-width:1400px}
h2{font-size:var(--t);margin:18px 0 8px;color:var(--fg);font-weight:600}
.card{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;
margin-bottom:var(--gap)}
.grid{display:grid;gap:var(--gap);grid-template-columns:repeat(auto-fit,minmax(150px,1fr))}
.metric{background:var(--bg2);border:1px solid var(--bd);border-radius:10px;padding:12px 14px}
.metric .k{color:var(--muted);font-size:var(--n)}
.metric .v{font-size:var(--num);font-weight:600;line-height:1.2}
.metric .x{color:var(--muted);font-size:var(--n)}
.oneliner{color:var(--muted);font-size:var(--b);margin:2px 0 var(--gap)}
.oneliner b{color:var(--fg);font-weight:600}
.pill{display:inline-block;border-radius:999px;padding:2px 9px;font-size:var(--n);border:1px solid var(--bd);
margin:2px 4px 2px 0;color:var(--muted)}
.ok{color:var(--muted);border-color:var(--bd)}.warn{color:var(--warn);border-color:#7a5b12}
.bad{color:var(--bad);border-color:#8b2c26}.muted{color:var(--muted)}.accent{color:var(--fg)}
.caps-line{color:var(--muted);font-size:var(--n);white-space:nowrap}
.caps-line.warn{color:var(--warn)}.caps-line.bad{color:var(--bad)}
.q{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;
border:1px solid var(--bd);color:var(--muted);font-size:var(--n);cursor:help;vertical-align:middle;
margin-left:6px;user-select:none}
.disc{cursor:pointer;color:var(--muted);font-size:var(--n);border:1px solid var(--bd);border-radius:7px;
padding:3px 9px;background:transparent;min-height:0}
.disc:hover{border-color:#6e7681;color:var(--fg)}
.banner{padding:9px 18px;font-size:var(--n);border-bottom:1px solid var(--bd)}
.banner.bad{background:#3d1418;color:#ffb3ad}.banner.warn{background:#3a2d09;color:#f0d38a}
.hidden{display:none}
table{border-collapse:collapse;width:100%;font-size:var(--b)}
th,td{border-bottom:1px solid var(--bd);padding:7px 8px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;background:var(--bg3);position:sticky;top:0}
tr:hover td{background:#12161c}
button,.btn{background:var(--bg3);color:var(--fg);border:1px solid var(--bd);border-radius:7px;
padding:6px 11px;font-size:var(--n);cursor:pointer;min-height:32px}
button:hover{border-color:#6e7681}
button.primary{background:#233043;border-color:#3d5573;color:var(--fg)}
button.danger{background:#5c1f1c;border-color:#8b2c26;color:#ffb3ad}
button:disabled{opacity:.45;cursor:not-allowed}
input,select{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:7px;
padding:7px 9px;font-size:var(--b);min-height:32px}
label{color:var(--muted);font-size:var(--n);display:block;margin-bottom:3px}
.row{display:flex;flex-wrap:wrap;gap:var(--gap);align-items:flex-end}
.fld{display:flex;flex-direction:column}
pre{background:var(--bg);border:1px solid var(--bd);border-radius:8px;padding:10px;overflow:auto;
font-size:var(--n);max-height:340px}
.chain-item{display:flex;gap:var(--gap);align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
.chain-1{border-left:3px solid var(--ok)}
.kv{color:var(--muted);font-size:var(--n)}
.num{text-align:right;font-variant-numeric:tabular-nums}
.toast{position:fixed;right:16px;bottom:16px;background:var(--bg3);border:1px solid #6e7681;
border-radius:9px;padding:10px 14px;max-width:min(92vw,460px);font-size:var(--n);z-index:50;
box-shadow:0 6px 24px #0008}
.toast.bad{border-color:var(--bad)}
@media (max-width:480px){
  main{padding:12px 10px 60px}header{padding:12px 12px 8px}nav{padding:8px 10px}
  button,.btn,nav a,input,select{min-height:44px}
  .grid{grid-template-columns:repeat(2,minmax(120px,1fr))}
  table.resp thead{display:none}
  table.resp tr{display:block;border:1px solid var(--bd);border-radius:9px;margin-bottom:9px;
  padding:6px 8px;background:var(--bg2)}
  table.resp td{display:flex;justify-content:space-between;gap:10px;border:none;padding:4px 0}
  table.resp td::before{content:attr(data-label);color:var(--muted);font-weight:600;flex:0 0 42%}
  .metric .v{font-size:22px}
}
/* 外观：自定义背景图（偏好存在 ui.json，不写 config.json） */
#bgimg,#bgmask{position:fixed;inset:0;z-index:0;pointer-events:none;display:none}
#bgimg{inset:-28px;background-size:cover;background-position:center;background-repeat:no-repeat;
  filter:blur(var(--bgblur,0px))}
#bgmask{background:rgba(13,17,23,var(--bgmaskAlpha,0.6))}
body.hasbg #bgimg,body.hasbg #bgmask{display:block}
main{position:relative;z-index:1}
.drop{margin:8px 0;padding:16px;border:1px dashed var(--bd);border-radius:9px;text-align:center;
  color:var(--muted);font-size:var(--n)}
body.hasbg header,body.hasbg .card,body.hasbg .metric{background:rgba(22,27,34,var(--cardA,1))}
body.hasbg th{background:rgba(28,33,40,var(--cardA,1))}
.posgrid{display:grid;grid-template-columns:46px 46px 46px;gap:6px;margin:6px 0 2px}
.posbtn{min-height:38px;padding:0;font-size:15px;line-height:1}
.posbtn.on{border-color:#8b949e;background:var(--bg3);color:var(--fg);
  box-shadow:inset 0 0 0 1px #8b949e}
.drop.over{border-color:#8b949e;color:var(--fg);background:var(--bg3)}
input[type=range]{width:100%;max-width:260px}
</style></head><body>
<div id="bgimg"></div><div id="bgmask"></div>
<div id="banner" class="banner bad hidden"></div>
<header>
  <h1>llm-relay 管理面板</h1><span id="ro" class="pill">…</span>
  <div class="sub">数据源 <span class="accent">__RELAY__</span> ｜ 面板 :9111<span class="q"
    title="写操作都转发到中转站 /admin/*（只允许 loopback）：校验 + 备份 + 原子写 + 热重载。">?</span></div>
</header>
<nav id="nav"></nav>
<main id="view"><div class="card">加载中…</div></main>
<div id="toast" class="toast hidden"></div>
<script>
const TABS=[["overview","概览"],["chain","链路"],["keys","Key 池"],["logs","请求日志"],
            ["usage","用量统计"],["upstream","上游"],["callers","调用方"],
            ["caps","能力矩阵"],["settings","设置"]];
// 面板联动（方案 C）：Hindsight 增强面板（8990）的深链接前缀，由服务端注入（默认 http://127.0.0.1:8990）
const HSDASH="__HSDASH__";
const DATA={};
const S={days:7, logOffset:0, logLimit:20, f:{provider:"",model:"",verdict:"",q:""}, remote:false,
         alertsOpen:false, recentFull:false, logFull:false, keyForm:null, restart:null,
         usageGroup:"caller", usageWindow:"24h",   // T1.3 用量页分组/窗口切换
         upstreamConfigured:null,   // 「上游」标签页是否渲染：false=integrations 未配置 → 隐藏
         callersConfigured:null,    // 「调用方」标签页是否渲染：false=config 没有 callers 段 → 隐藏
         // 「模型」区块（Key 池）的临时状态：展开的添加行 / 待删模型 / 未提交的输入
         modelAdd:{}, modelAsk:null, modelDraft:null};
const BOOT=(typeof window!=="undefined"&&window.__BOOT__)||null;
if(BOOT&&BOOT.data){Object.keys(BOOT.data).forEach(k=>{DATA[k]=BOOT.data[k];});S.bootSkip=true;S.remote=!BOOT.local;
  if(DATA.upstreamTab&&DATA.upstreamTab.configured===false)S.upstreamConfigured=false;
  if(DATA.callersTab&&DATA.callersTab.configured===false)S.callersConfigured=false;}
const VERDICT_CN={ok:"成功",ratelimit:"限流 429",cooldown:"冷却中",transport_timeout:"上游超时",
  upstream_error:"上游错误",auth:"鉴权失败",quota:"额度耗尽",schema_echo:"schema 回显",
  schema_mismatch:"不符 schema",degraded_schema_rejected:"降级判不过",relay_unusable_content:"内容不合格 502",
  bad_json:"JSON 坏",relay_all_failed:"全失败",no_candidates:"无候选",relay_error:"中转站错误",
  other:"其他",bad_request:"请求非法"};
const $=s=>document.querySelector(s);
const esc=s=>String(s===undefined||s===null?"":s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const v=(x,d="—")=>(x===undefined||x===null||x===""||Number.isNaN(x))?d:x;
const num=n=>(typeof n==="number"&&isFinite(n))?n.toLocaleString("en-US"):v(n);
const ms=x=>{const n=Number(x);if(!isFinite(n)||n<=0)return "—";return n>=1000?(n/1000).toFixed(1)+"s":Math.round(n)+"ms";};
const secs=x=>{const n=Number(x);return (!isFinite(n)||n<=0)?"—":(n>=60?(n/60).toFixed(1)+" 分":Math.round(n)+"s");};
const pct=x=>{const n=Number(x);return isFinite(n)?(n*100).toFixed(1)+"%":"—";};
function toast(msg,bad){const t=$("#toast");t.className="toast"+(bad?" bad":"");t.innerHTML=esc(msg);t.classList.remove("hidden");
  clearTimeout(t._h);t._h=setTimeout(()=>t.classList.add("hidden"),bad?9000:6000);}
function banner(msg,level){const b=$("#banner");b.className="banner "+(level||"bad");b.textContent=msg;b.classList.remove("hidden");}
function hideBanner(){const b=$("#banner");b.classList.add("hidden");}
window.addEventListener("error",e=>banner("脚本错误："+(e.message||e.type)+"（页面已降级显示，请刷新或看控制台）"));
window.addEventListener("unhandledrejection",e=>banner("脚本异常："+(e.reason&&e.reason.message?e.reason.message:e.reason)));
async function api(name,opt){
  opt=opt||{}; let url="/api/"+name;
  if(opt.q){const q=new URLSearchParams();Object.keys(opt.q).forEach(k=>{if(opt.q[k]!==""&&opt.q[k]!==undefined&&opt.q[k]!==null)q.set(k,opt.q[k]);});
    const s=q.toString(); if(s) url+="?"+s;}
  const init={method:opt.method||"GET",headers:{"Content-Type":"application/json"}};
  if(opt.body!==undefined) init.body=JSON.stringify(opt.body);
  const res=await fetch(url,init);
  let data=null; try{data=await res.json();}catch(e){data=null;}
  if(!res.ok){
    const d=data&&(data.detail||(data.error&&data.error.message)||data.error);
    const err=new Error(d||("HTTP "+res.status)); err.status=res.status; err.data=data; throw err;
  }
  return data===null?{}:data;
}
function badge(text,cls){return '<span class="pill '+cls+'">'+esc(text)+'</span>';}
// 能力徽章 → 一行灰色小字（只有 degradable / 缺能力 / cot_leak 才染色）
function capsLine(caps){
  caps=caps||{}; const js=caps.json_schema;
  const parts=["schema:"+v(js,"?"),caps.tools?"tools":"无 tools"];
  if(caps.reasoning_only)parts.push("思考");
  if(caps.cot_leak)parts.push("cot_leak");
  const cls=(js==="native")?"caps-line":(js==="degradable"?"caps-line warn":"caps-line bad");
  return '<span class="'+cls+'" title="caps 来自 config.json">'+esc(parts.join(" · "))+'</span>';
}
function capsBadges(caps){return capsLine(caps);}
function verdictTag(x){const k=String(v(x,""));const cn=VERDICT_CN[k]||k;
  const cls=(k==="ok")?"ok":(/echo|reject|unusable|mismatch|error|fail|timeout/.test(k)?"bad":"warn");
  return '<span class="pill '+cls+'" title="'+esc(k)+'">'+esc(cn)+'</span>';}
function loadSummary(){return api("summary").then(d=>{DATA.summary=d;});}
function loadStatus(){return api("status").then(d=>{DATA.status=d;});}
function loadConfig(){return api("config").then(d=>{DATA.config=d;});}
function loadRequests(extra){const q=Object.assign({limit:S.logLimit,offset:S.logOffset},S.f||{},extra||{});
  return api("requests",{q:q}).then(d=>{DATA.requests=d;});}
function loadUsage(){return api("usage",{q:{days:S.days}}).then(d=>{DATA.usage=d;});}
// T1.3 分组视图：/api/usage 透传 group/window 给中转站 /admin/usage（服务端惰性重建 + 增量计数）
function loadUsageGroup(){return api("usage",{q:{group:S.usageGroup,window:S.usageWindow}})
  .then(d=>{DATA.usageGroup=d;});}
function loadCaps(){return api("caps").then(d=>{DATA.caps=d;});}
// 设置 tab 的「上游（只读 + 回滚）」区块：中转站侧 /admin/upstream（旧 relay_hindsight 仍等价）
function loadUpstream(){return api("relay_upstream").then(d=>{DATA.upstream=d;});}
// 「上游」tab：面板本地的 /api/upstream（服务端代理 + 聚合 usage）；configured=false → 隐藏该 tab
function loadUpstreamTab(){return api("upstream").then(d=>{DATA.upstreamTab=d;
  if(d&&d.configured===false)S.upstreamConfigured=false;});}
// 「调用方」tab：中转站 /admin/callers 的只读代理；configured=false → 隐藏该 tab
function loadCallersTab(){return api("callers").then(d=>{DATA.callersTab=d;
  if(d&&d.configured===false)S.callersConfigured=false;});}
function currentTab(){const h=(location.hash||"#overview").slice(1);
  return TABS.some(t=>t[0]===h)?h:"overview";}
function renderNav(){const cur=currentTab();
  $("#nav").innerHTML=TABS.filter(t=>!(t[0]==="upstream"&&S.upstreamConfigured===false)
      &&!(t[0]==="callers"&&S.callersConfigured===false))
    .map(t=>'<a href="#'+t[0]+'" class="'+(t[0]===cur?"active":"")+'">'+t[1]+'</a>').join("");}
async function loadTab(name){
  if(name!=="keys")S.addKey=null;              // 离开 Key 池不在内存里留密码框内容
  renderNav();
  const skip=!!S.bootSkip; S.bootSkip=false;   // ?snapshot=1 首屏：数据已在 DATA 里，不再来回请求
  try{
    if(!DATA.summary||(name==="overview"&&!skip)) await loadSummary();
    if(name==="chain"){if(!DATA.status)await loadStatus();renderChain();}
    else if(name==="keys"){if(!DATA.status)await loadStatus();if(!DATA.config)await loadConfig();renderKeys();}
    else if(name==="logs"){if(!DATA.requests||!skip)await loadRequests();renderLogs();}
    else if(name==="usage"){if(!DATA.usage||!skip)await loadUsage();
      if(!DATA.usageGroup||!skip)await loadUsageGroup();renderUsage();}
    else if(name==="upstream"){if(!DATA.upstreamTab||!skip)await loadUpstreamTab();renderUpstream();}
    else if(name==="callers"){if(!DATA.callersTab||!skip)await loadCallersTab();renderCallers();}
    else if(name==="caps"){if(!DATA.caps||!skip)await loadCaps();renderCaps();}
    else if(name==="settings"){
      if(!DATA.status||!skip)await loadStatus();
      if(!DATA.config||!skip)await loadConfig();
      if(!DATA.upstream||!skip)await loadUpstream();
      if(!DATA.ui||!skip)await loadUi();
      renderSettings();}
    else renderOverview();
  }catch(e){
    $("#view").innerHTML='<div class="card">加载失败：'+esc(e.message)+
      (e.status===401?'<br><span class="muted">需要访问密钥（本机免密）。</span>':'')+'</div>';
    toast("加载失败："+e.message,true);
  }
}
function metric(k,val,extra){return '<div class="metric"><div class="k">'+esc(k)+'</div><div class="v">'+
  esc(val)+'</div><div class="x">'+esc(extra||"")+'</div></div>';}
function renderOverview(){
  const s=DATA.summary||{};
  const alerts=(s.alerts||[]);
  const recent=s.recent||[];
  // 顶部只留 4 个大数字；今日 token 等收进下面那一行小字
  let html='<div class="grid">';
  html+=metric("健康候选数",v(s.candidates_healthy),"状态 "+(s.status||"—"));
  html+=metric("近 1h 请求",num(s.requests_1h),"今日 "+num(s.requests_today)+" 条");
  html+=metric("近 1h 成功率",s.success_rate_1h===null||s.success_rate_1h===undefined?"—":pct(s.success_rate_1h),"429 "+num(s.n429_1h)+" 次");
  html+=metric("p50 延迟",ms(s.p50_latency_ms),"按 usage.jsonl");
  html+='</div>';
  // 一句话首选模型 + 其余信息一行小字
  html+='<div class="oneliner">当前首选模型 <b>'+esc(v(s.top_model,"无可用候选"))+'</b>'+
    '（chain 最小且可用）｜ 运行 '+secs(s.uptime_s)+' ｜ 今日 token '+num(s.tokens_today)+
    ' ｜ usage_log '+(s.usage_enabled?"已开启":"<span class=\"warn\">未开启</span>")+
    ' ｜ 生成于 '+esc(v(s.generated_at))+'</div>';
  // 告警：默认一行，点开才展开
  if(alerts.length){
    const worst=alerts.some(a=>a.level==="bad")?"bad":"warn";
    html+='<div class="card" style="padding:10px 14px"><button class="disc" onclick="toggleAlerts()">'+
      '<span class="'+worst+'">⚠ '+alerts.length+' 条告警</span> '+(S.alertsOpen?"收起":"展开")+'</button>'+
      (S.alertsOpen?('<div style="margin-top:8px">'+alerts.slice(0,14).map(a=>'<div class="'+
        (a.level==="bad"?"bad":"warn")+'">• '+esc(a.text)+'</div>').join("")+'</div>'):"")+'</div>';
  }else{html+='<div class="card" style="padding:10px 14px"><span class="muted">⚠ 0 条告警 · key 池、provider 冷却与健康检查都正常</span></div>';}
  // 最近请求：默认收起，只留 4 列
  const cols=[["时间",r=>esc(v(r.ts))],["模型",r=>esc(v(r.provider))+"/"+esc(v(r.model))],
    ["HTTP",r=>esc(v(r.status))],["耗时",r=>esc(secs(r.latency_s))],
    ["key",r=>"#"+esc(v(r.key_index))+" "+esc(v(r.key_fp))],["token",r=>num(v(r.total_tokens,0))],
    ["降级",r=>r.degraded?"降级":"—"],["尝试",r=>esc((r.attempt_kinds||[]).join(","))]];
  const shown=S.recentFull?cols:cols.slice(0,4);
  html+='<h2 style="display:flex;align-items:center;gap:10px">最近 8 条请求'+
    '<button class="disc" onclick="toggleRecent()">'+(S.recentFull?"收起全部列":"展开全部列")+'</button></h2>'+
    '<table class="resp"><thead><tr>'+shown.map(c=>'<th>'+c[0]+'</th>').join("")+'</tr></thead><tbody>'+
    (recent.length?recent.map(r=>'<tr>'+shown.map(c=>'<td data-label="'+c[0]+'">'+c[1](r)+'</td>').join("")+'</tr>').join("")
      :'<tr><td colspan="'+shown.length+'" class="muted">暂无请求</td></tr>')+
    '</tbody></table>';
  $("#view").innerHTML=html;
}
function toggleAlerts(){S.alertsOpen=!S.alertsOpen;renderOverview();}
function toggleRecent(){S.recentFull=!S.recentFull;renderOverview();}

// ---------------------------------------------------------------- 链路
function chainModels(){
  const st=DATA.status||{}; const out=[];
  (st.providers||[]).forEach(p=>{
    (p.models||[]).forEach(m=>{
      const mc429=(m.counts&&m.counts.ratelimit)||0;
      const mcool=[]; (p.keys||[]).forEach(k=>(k.model_cooldowns||[]).forEach(x=>{
        if(x.model===m.id) mcool.push(k.env+" "+Math.round(x.left_s||0)+"s");}));
      out.push({key:p.name+"/"+m.id,provider:p.name,id:m.id,chain:m.chain,
        caps:m.caps||{},counts:m.counts||{},disabled:m.disabled,reason:m.disabled_reason,
        providerActive:p.active,providerCooldown:p.cooldown_left_s,
        n429:mc429,ncool:mcool,saw_reasoning:m.saw_reasoning});
    });
  });
  out.sort((a,b)=>(a.chain===undefined||a.chain===null?9999:a.chain)-(b.chain===undefined||b.chain===null?9999:b.chain));
  return out;
}
function renderChain(){
  const models=chainModels();
  let html='<h2>候选链<span class="q" title="上移/下移会调 /admin/reorder：中转站按给定顺序重写 chain（1..N 连续）后立即热重载，不用重启 Hindsight。">?</span></h2>';
  if(!models.length)html+='<div class="card">没有候选（检查 keys.env 与 config.json）。</div>';
  models.forEach((m,i)=>{
    const bad429=m.n429>0, cool=m.ncool.length>0;
    html+='<div class="card chain-'+esc(v(m.chain,9))+'"><div class="chain-item"><div>'+
      '<div style="font-size:15px;font-weight:600">#'+esc(v(m.chain))+' '+esc(m.provider)+' / '+esc(m.id)+'</div>'+
      '<div style="margin:4px 0">'+capsLine(m.caps)+
        (m.disabled?badge("disabled "+(m.reason||""),"bad"):"")+
        (m.providerActive?"":badge("provider 不可用","bad"))+
        (m.providerCooldown>0?badge("provider 冷却 "+secs(m.providerCooldown),"warn"):"")+
        (m.saw_reasoning?badge("历史带 reasoning","warn"):"")+'</div>'+
      '<div class="kv">近期 429/超时：429='+esc(v(m.n429,0))+
        ' ｜ 内容不合格='+esc(v((m.counts||{}).unusable,0))+
        ' ｜ 降级判不过='+esc(v((m.counts||{}).degraded_schema_rejected,0))+
        ' ｜ schema 回显='+esc(v((m.counts||{}).schema_echo,0))+
        ' ｜ ok='+esc(v((m.counts||{}).ok,0))+'</div>'+
      (cool?'<div class="bad">模型冷却中：'+esc(m.ncool.join(" / "))+'</div>':'')+
      (bad429?'<div class="warn">这个模型近期吃过 429 —— 调序时优先把它往后放。</div>':'')+
      '</div><div class="row">'+
      '<button onclick="moveModel('+i+',-1)" '+(i===0?"disabled":"")+'>▲ 上移</button>'+
      '<button onclick="moveModel('+i+',1)" '+(i===models.length-1?"disabled":"")+'>▼ 下移</button>'+
      '</div></div></div>';
  });
  $("#view").innerHTML=html;
}
async function moveModel(i,dir){
  const models=chainModels(); const j=i+dir;
  if(j<0||j>=models.length)return;
  const tmp=models[i]; models[i]=models[j]; models[j]=tmp;
  try{
    const r=await api("reorder",{method:"POST",body:{order:models.map(m=>m.key)}});
    toast("已热重载，无需重启。顺序是按实测额度排的，调完请观察 10 分钟再决定要不要还原。"+
      "（连续="+v(r.continuous)+"）");
    DATA.status=null; await loadStatus(); renderChain();
  }catch(e){toast("调序失败："+e.message,true);}
}

// ---------------------------------------------------------------- Key 池
function renderKeys(){
  snapshotForms();                 // 重建 DOM 前先把用户已输入的内容存起来（否则一重建就丢）
  const st=DATA.status||{};
  const provs=st.providers||[];
  let html='<h2>Key 池<span class="q" title="只显示变量名与 sha256 前 8 位指纹，明文永不回显（响应/日志/截图都只有指纹）。禁用/启停会持久化到 config.json 的 key_state，重启后仍生效；替换/删除会先备份 keys.env 再原子写回（保持 600）。">?</span>'+
    '<span class="kv">'+(S.remote?'远程只读：替换/删除按钮已禁用':'替换 / 删除要手输 REPLACE / REMOVE')+'</span></h2>';
  provs.forEach(p=>{
    if(S.keyForm&&S.keyForm.provider===p.name)html+=keyFormHtml();   // 就地渲染：紧挨着被点的那张卡片
    html+='<div class="card"><div style="font-size:15px;font-weight:600">'+esc(p.name)+
      ' <span class="muted">'+esc(v(p.base_url))+'</span></div>'+
      '<div class="kv">rpm '+esc(v(p.rpm))+'（近 1 分钟 '+esc(v(p.rpm_used_last_min,0))+'+，被拦 '+esc(v(p.rpm_blocked,0))+'）'+
      ' ｜ 并发上限 '+esc(v(p.max_concurrency))+
      ' ｜ provider 冷却 '+secs(p.cooldown_left_s)+' '+esc(v(p.cooldown_reason,""))+
      ' ｜ 近窗口失败 '+esc(v(p.recent_provider_failures,0))+'</div>';
    html+='<table class="resp"><thead><tr><th>#</th><th>env</th><th>指纹</th><th>用量</th>'+
      '<th>冷却剩余</th><th>模型冷却</th><th>状态</th><th>操作</th></tr></thead><tbody>';
    (p.keys||[]).forEach(k=>{
      const mcool=(k.model_cooldowns||[]);
      const pv=esc(p.name), ev=esc(k.env);
      const ro=S.remote?'disabled title="远程只读，无法改 key"':"";
      const isEmpty=!k.fp||k.fp==="-";   // "-" = keys.env 里还没有这个变量（空槽）
      html+='<tr><td data-label="#">'+esc(v(k.index))+'</td><td data-label="env">'+esc(k.env)+'</td>'+
        '<td data-label="指纹">'+esc(v(k.fp))+'</td>'+
        // uses / 429 / 401 合成一列（紧凑写法）
        '<td data-label="用量" class="num">'+esc(v(k.uses,0))+'次 · '+esc(v(k.n429,0))+'×429 · '+esc(v(k.n401,0))+'×401</td>'+
        '<td data-label="冷却剩余">'+secs(k.cooldown_left_s)+'<div class="kv">'+esc(v(k.cooldown_reason,""))+'</div></td>'+
        '<td data-label="模型冷却">'+(mcool.length?mcool.map(x=>esc(x.model)+" "+Math.round(x.left_s)+"s <span class=\"muted\">"+
          esc(v(x.reason))+"</span>").join("<br>"):"—")+'</td>'+
        '<td data-label="状态">'+(k.disabled?badge("禁用"+(k.disabled_reason?"："+k.disabled_reason:""),"bad"):"<span class=\"muted\">启用</span>")+'</td>'+
        '<td data-label="操作"><div class="row">'+
          '<button onclick="keyAction(\''+pv+'\',\''+ev+'\',{enabled:'+(k.disabled?"true":"false")+'})">'+
            (k.disabled?"启用":"禁用")+'</button>'+
          '<button onclick="keyAction(\''+pv+'\',\''+ev+'\',{clear_cooldown:true})">清冷却</button>'+
          (isEmpty
            ? '<button '+ro+' onclick="openKeyForm(\''+pv+'\',\''+ev+'\',\'fill\')">填入 key</button>'
            : '<button '+ro+' onclick="openKeyForm(\''+pv+'\',\''+ev+'\',\'replace\')">替换</button>')+
          '<button class="danger" '+ro+' onclick="openKeyForm(\''+pv+'\',\''+ev+'\',\'remove\')">删除</button>'+
          '<button onclick="probeModel(\''+pv+'\',\''+esc((p.models||[])[0]?p.models[0].id:"")+'\')">立即探测</button>'+
          '</div></td></tr>';
    });
    if(!(p.keys||[]).length)html+='<tr><td colspan="8" class="muted">没有 key</td></tr>';
    html+='</tbody></table>';
    html+=modelsBlockHtml(p);           // R2：同一张卡片里，key 表格下面就是「模型」区块
    html+='</div>';
  });
  const names=provs.map(p=>p.name);
  const isNew=((S.addKey||{}).provider==="__new__");
  const newBox='<div id="new_prov_box"'+(isNew?"":' class="hidden"')+'>'+
    '<div class="kv">新建厂商会往 config.json 追加一个 provider 块，保守默认：'+
    '<b>chain 90</b>（最低优先级，不会抢现有源）、rpm 10、并发 2、json_schema=degradable。'+
    '确认过能力后可在 config.json 把 caps 调成 native。</div>'+
    '<div class="row"><div class="fld"><label>厂商名（小写，如 siliconflow）</label>'+
      '<input id="np_name" placeholder="siliconflow" oninput="snapshotForms()"></div>'+
    '<div class="fld"><label>base_url（OpenAI 兼容）</label>'+
      '<input id="np_url" placeholder="https://api.example.com/v1" oninput="snapshotForms()"></div>'+
    '<div class="fld"><label>模型 id（逗号分隔，可多个）</label>'+
      '<input id="np_models" placeholder="model-a,model-b" oninput="snapshotForms()"></div></div></div>';
  html+='<div class="card"><b>追加新 key</b><div class="row" style="margin-top:8px">'+
    '<div class="fld"><label>provider</label><select id="k_prov" onchange="snapshotForms();toggleNewProv()">'+
      names.map(n=>'<option>'+esc(n)+'</option>').join("")+
      '<option value="__new__">＋ 新建厂商…</option></select></div>'+
    '<div class="fld"><label>变量名（如 SENSENOVA_KEY_3）</label><input id="k_env" placeholder="SENSENOVA_KEY_3" oninput="snapshotForms()"></div>'+
    '<div class="fld"><label>key（type=password，明文不回显）</label>'+
      '<input id="k_secret" type="password" autocomplete="off" placeholder="粘贴 key（不回显，只回指纹）" oninput="snapshotForms()"></div>'+
    '<button class="primary" '+(S.remote?'disabled title="远程只读"':"")+' onclick="addKey()">追加并热重载</button></div>'+
    newBox+
    '<div class="kv">提交后只回显 sha256 前 8 位指纹；同一个 key（按 sha256 去重）不会重复写入。</div></div>';
  $("#view").innerHTML=html;
  restoreForms();                              // 把快照写回新 DOM（含确认词，按钮状态跟着恢复）
  if(S.keyForm)focusKeyForm();                 // 点了替换/删除就自动滚到表单并聚焦
}
// 表单状态保留：renderKeys() 会整体重建 #view，用户打到一半的内容必须活下来。
// 注意：secret 只写到 input.value（不进 HTML 字符串），离开 Key 池即从内存清掉。
function snapshotForms(){
  const p=document.getElementById("k_prov"), e=document.getElementById("k_env"), s=document.getElementById("k_secret");
  if(p||e||s){
    const n1=document.getElementById("np_name"), n2=document.getElementById("np_url"), n3=document.getElementById("np_models");
    S.addKey={provider:p?p.value:((S.addKey||{}).provider||""),
              env:e?e.value:"", secret:s?s.value:"",
              np:{name:n1?n1.value:(((S.addKey||{}).np||{}).name||""),
                  url:n2?n2.value:(((S.addKey||{}).np||{}).url||""),
                  models:n3?n3.value:(((S.addKey||{}).np||{}).models||"")}};
  }
  // 「模型」区块（Key 池每张卡片的模型表 + 添加行 + 删除确认）：整块 innerHTML 重建前先把
  // 每个带 data-keep 的输入/勾选框按 id 存下来，重建后由 restoreForms() 原样写回。
  // 不这么做就会踩 aad6055 修过的那类 bug：打字打到一半被重建冲掉。
  const md={};
  document.querySelectorAll("#view [data-keep]").forEach(el=>{
    if(el.id)md[el.id]=(el.type==="checkbox")?!!el.checked:el.value;});
  S.modelDraft=md;
  if(S.keyForm){
    const c=document.getElementById("kr_confirm"), sc=document.getElementById("kr_secret");
    if(c)S.keyForm.confirm=c.value;
    if(sc)S.keyForm.secretVal=sc.value;
  }
}
function restoreForms(){
  const a=S.addKey;
  if(a){
    const p=$("#k_prov"), e=$("#k_env"), s=$("#k_secret");
    if(p&&a.provider)p.value=a.provider;
    if(e)e.value=a.env||"";
    if(s)s.value=a.secret||"";
    const np=a.np||{};
    const n1=$("#np_name"), n2=$("#np_url"), n3=$("#np_models");
    if(n1)n1.value=np.name||""; if(n2)n2.value=np.url||""; if(n3)n3.value=np.models||"";
    if(typeof toggleNewProv==="function")toggleNewProv();
  }
  const f=S.keyForm;
  if(f){
    const c=$("#kr_confirm"), sc=$("#kr_secret");
    if(c&&f.confirm)c.value=f.confirm;
    if(sc&&f.secretVal)sc.value=f.secretVal;
    keyFormWatch();                             // 恢复后让提交按钮的可用状态也跟着恢复
  }
  const md=S.modelDraft;
  if(md){Object.keys(md).forEach(id=>{
    const el=document.getElementById(id); if(!el)return;
    if(el.type==="checkbox")el.checked=!!md[id]; else el.value=md[id];});}
  modelDelWatch();
}
function focusKeyForm(){
  const el=document.getElementById("kr_secret")||document.getElementById("kr_confirm");
  if(!el)return;
  try{el.scrollIntoView({block:"center",behavior:"smooth"});}catch(e){try{el.scrollIntoView();}catch(e2){}}
  try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(e2){}}
}
// 替换/删除：一律手输确认词（REPLACE / REMOVE）+ 密码框，任何地方都不回显明文
function openKeyForm(provider,env,mode){
  if(S.remote){toast("远程只读：不能替换/删除 key",true);return;}
  S.keyForm={provider:provider,env:env,mode:mode,confirm:"",secretVal:""};
  renderKeys();
}
function closeKeyForm(){S.keyForm=null;renderKeys();}
function keyFormHtml(){
  const f=S.keyForm; if(!f)return "";
  const isDel=f.mode==="remove", isFill=f.mode==="fill";
  const title=isDel?"删除":(isFill?"填入":"替换");
  const word=isDel?"REMOVE":"REPLACE";
  return '<div class="card" id="kr_form" style="border-color:#8b2c26"><b>'+title+
    ' '+esc(f.provider)+' / '+esc(f.env)+'</b>'+
    '<div class="row" style="margin-top:8px">'+
    (isDel?"":'<div class="fld"><label>新 key（type=password，明文不回显）</label>'+
      '<input id="kr_secret" type="password" autocomplete="off" placeholder="粘贴新 key（只回指纹）" oninput="snapshotForms()"></div>')+
    '<div class="fld"><label>输入 '+word+' 才能提交</label>'+
      '<input id="kr_confirm" autocomplete="off" placeholder="'+word+'" oninput="keyFormWatch();snapshotForms()"></div>'+
    '<button class="primary" id="kr_btn" disabled onclick="submitKeyForm()">'+title+'并热重载</button>'+
    '<button onclick="closeKeyForm()">取消</button></div>'+
    '<div class="kv">'+(isDel
      ?"删除不可逆：会先备份 keys.env.bak.&lt;ts&gt;，原子写回并保持 600，同时清掉 key_state 残留。"
      :(isFill
        ?"这个 env 是空槽（keys.env 里还没有值）：提交会给它新增一行。"
        :"替换只改这一行的值，其它行逐字节不变；新值按 sha256 去重；旧值的冷却/禁用状态会被清掉。"))+
    ' 只回指纹，不回明文。</div></div>';
}
function keyFormWatch(){
  const f=S.keyForm; if(!f)return;
  const word=f.mode==="remove"?"REMOVE":"REPLACE";
  const inp=$("#kr_confirm"), btn=$("#kr_btn");
  if(inp&&btn)btn.disabled=(inp.value!==word)||S.remote;
}
async function submitKeyForm(){
  const f=S.keyForm; if(!f)return;
  const word=f.mode==="remove"?"REMOVE":"REPLACE";
  if(S.remote){toast("远程只读：不能改 key",true);return;}
  const inp=$("#kr_confirm");
  if(!inp||inp.value!==word){toast("必须先输入 "+word,true);return;}
  if(!confirm((f.mode==="remove"?"删除":(f.mode==="fill"?"填入":"替换"))+" "+f.provider+" / "+f.env+"？"+
    (f.mode==="remove"?"删除不可逆。":"")))return;
  try{
    let r;
    if(f.mode==="fill"){
      const el0=$("#kr_secret"); const sec0=el0?el0.value:"";
      if(!sec0){toast("新 key 不能为空",true);return;}
      r=await api("key",{method:"POST",body:{provider:f.provider,env_name:f.env,secret:sec0}});
      if(r.duplicate)toast("未写入：这把 key 已在池子里（sha256 去重，"+v(r.existing_env)+" 指纹 "+v(r.fp)+"）");
      else toast("已填入 "+f.env+"：指纹 "+v(r.fp)+"，keys.env 新增一行，已热重载");
    }else if(f.mode==="replace"){
      const el=$("#kr_secret"); const secret=el?el.value:"";
      if(!secret){toast("新 key 不能为空",true);return;}
      r=await api("key_replace",{method:"POST",body:{provider:f.provider,env_name:f.env,secret:secret}});
      if(r.duplicate)toast("未写入：新值与池子里已有的 key 相同（sha256 去重，已有 "+v(r.existing_env)+" 指纹 "+v(r.fp)+"）");
      else toast(f.env+" 已替换：指纹 "+v(r.replaced)+" → "+v(r.fp)+"，备份 "+v(r.backup)+"，已热重载");
    }else{
      r=await api("key_remove",{method:"POST",body:{provider:f.provider,env_name:f.env,confirm:"REMOVE"}});
      if(r.line_absent)toast("已删除空槽 "+v(r.removed)+"（keys.env 里本来就没有这一行，只清了配置侧）"+
        (r.warning?" "+v(r.warning):""));
      else toast("已删除 "+v(r.removed)+"（指纹 "+v(r.fp)+"，index "+v(r.index)+"，key 数 "+
        v(r.keys_before)+"→"+v(r.keys_after)+"）"+(r.warning?" "+v(r.warning):""));
    }
    S.keyForm=null; DATA.status=null; await loadStatus(); renderKeys();
  }catch(e){toast("操作失败："+e.message,true);}
}
async function keyAction(provider,env,body){
  try{
    body.provider=provider; body.env=env;
    const r=await api("key_state",{method:"POST",body:body});
    toast(provider+" / "+env+" 已更新：disabled="+v(r.disabled)+"（已写回 config.json.key_state 并热重载）");
    DATA.status=null; await loadStatus(); renderKeys();
  }catch(e){toast("操作失败："+e.message,true);}
}
function toggleNewProv(){
  const p=$("#k_prov"), box=$("#new_prov_box");
  if(!p||!box)return;
  if(p.value==="__new__")box.classList.remove("hidden"); else box.classList.add("hidden");
}
async function addKey(){
  const sel=$("#k_prov").value, env=$("#k_env").value.trim(), secret=$("#k_secret").value;
  const isNew=sel==="__new__";
  if(!env||!secret){toast("变量名和 key 都要填",true);return;}
  const body={provider:isNew?"":sel,env_name:env,secret:secret};
  if(isNew){
    const name=(($("#np_name")||{}).value||"").trim(),
          url=(($("#np_url")||{}).value||"").trim(),
          models=(($("#np_models")||{}).value||"").trim();
    if(!name||!url||!models){toast("新建厂商要填：厂商名 / base_url / 模型 id",true);return;}
    body.new_provider={name:name,base_url:url,models:models};
  }
  try{
    const r=await api("key",{method:"POST",body:body});
    $("#k_secret").value="";
    S.addKey=null;                              // 提交完就不在内存里留明文了
    if(r.duplicate)toast("已存在同一把 key（sha256 去重）："+v(r.existing_env)+" 指纹 "+v(r.fp)+"，未写入");
    else toast("已追加 "+v(r.env_name)+"，指纹 "+v(r.fp)+
      (r.created_provider?"｜新建厂商 "+v(r.created_provider)+"（"+v(r.provider_defaults)+"）":"")+
      (r.added_to_provider_list?"｜已加入该 provider 的 keys 列表":"")+"，已热重载");
    DATA.status=null; DATA.config=null; await loadStatus(); renderKeys();
  }catch(e){toast("追加失败："+e.message,true);}
}

// ------------------------------------------------- Key 池 → 模型（加 / 单改优先级 / 改能力 / 删）
// 走 POST /admin/model（action=add/update/remove）。后端只改 config.json 的
// providers[].models：不动 request.* 的实测调参，也不重排别的模型的 chain ——
// 「改优先级」是**单改这一个**，40/45/50 这种档位设计因此得以保留（链路 tab 的
// /admin/reorder 才会把 chain 压成 1..N，两者是两件事）。
function modelsBlockHtml(p){
  const name=p.name, isLocal=(name==="__local__"), ms=p.models||[];
  const pk=String(name).replace(/[^a-z0-9]/gi,"_");
  const ro=(S.remote||isLocal)?'disabled title="'+(isLocal
      ?"本机兜底：模型在 config.json 的 local_fallback 里配，不归这里管"
      :"远程只读，无法改模型")+'"':"";
  let h='<div style="margin-top:12px;border-top:1px solid var(--bd);padding-top:10px">'+
    '<div class="row" style="justify-content:space-between;align-items:center">'+
    '<div><b>模型</b> <span class="muted">'+ms.length+' 个</span>'+
    '<span class="q" title="只改 config.json 的 providers[].models：加模型 / 单改优先级(chain，1..9999) / '+
    '改能力(json_schema、tools) / 删除(要输入 REMOVE)。不会动 request.* 的实测调参，也不会把其它模型的 '+
    'chain 压平（改优先级是单改这一个）。删空某个 provider 会被守卫 400 拒绝。">?</span></div>'+
    '<button '+ro+' onclick="toggleModelAdd(\''+esc(name)+'\')">＋ 添加模型</button></div>'+
    '<table class="resp" style="margin-top:8px"><thead><tr><th>#</th><th>模型 id</th>'+
    '<th>chain（优先级）</th><th>json_schema</th><th>tools</th><th>操作</th></tr></thead><tbody>';
  ms.forEach((m,i)=>{
    const mk=pk+"_"+String(m.id).replace(/[^a-z0-9]/gi,"_")+"_"+i;
    const caps=m.caps||{};
    const chv=(m.chain===undefined||m.chain===null)?"":m.chain;
    const pv=esc(name), mv=esc(m.id);
    h+='<tr><td data-label="#">'+esc(v(i+1))+'</td><td data-label="模型 id">'+esc(v(m.id))+
      (m.disabled?badge("disabled","bad"):"")+'<div class="kv">'+capsLine(caps)+'</div></td>'+
      '<td data-label="chain"><input id="m_chain_'+mk+'" data-keep="1" data-f="chain" type="number" '+
        'min="1" max="9999" style="width:92px" value="'+esc(chv)+'" '+ro+'></td>'+
      '<td data-label="json_schema"><select id="m_js_'+mk+'" data-keep="1" data-f="js" '+ro+'>'+
        ['native','degradable'].map(x=>'<option'+(caps.json_schema===x?" selected":"")+'>'+x+'</option>').join("")+
        '</select></td>'+
      '<td data-label="tools"><input id="m_tools_'+mk+'" data-keep="1" data-f="tools" type="checkbox"'+
        (caps.tools?" checked":"")+' '+ro+'></td>'+
      '<td data-label="操作"><div class="row">'+
        '<button '+ro+' onclick="modelSaveChain(this,\''+pv+'\',\''+mv+'\')">保存优先级</button>'+
        '<button '+ro+' onclick="modelSaveCaps(this,\''+pv+'\',\''+mv+'\')">改能力</button>'+
        '<button onclick="probeModel(\''+pv+'\',\''+mv+'\')">实测</button>'+
        '<button class="danger" '+ro+' onclick="modelAskRemove(\''+pv+'\',\''+mv+'\')">删除</button>'+
        '</div><div id="probe_'+mk+'" class="kv"></div></td></tr>';
  });
  if(!ms.length)h+='<tr><td colspan="6" class="muted">这个 provider 没有模型'+
    '（最后一个模型删不掉：守卫会 400，要下线整个源请用 enabled=false）</td></tr>';
  h+='</tbody></table>';
  // 添加行：默认收起，点「＋ 添加模型」展开；chain 默认填当前最大值 + 1
  const open=!!(S.modelAdd&&S.modelAdd[name]);
  const next=Math.min((ms.reduce((a,m)=>Math.max(a,Number(m.chain)||0),0)+1)||1,9999);
  h+='<div id="m_add_'+pk+'" data-model-add="1" class="row'+(open?"":" hidden")+
    '" style="margin-top:8px">'+
    '<div class="fld"><label>模型 id（字母/数字/._:/-）</label>'+
      '<input id="m_new_id_'+pk+'" data-keep="1" data-f="id" placeholder="model-id" '+ro+'></div>'+
    '<div class="fld"><label>chain（1..9999，默认 max+1）</label>'+
      '<input id="m_new_chain_'+pk+'" data-keep="1" data-f="chain" type="number" min="1" max="9999" '+
      'style="width:110px" value="'+esc(next)+'" '+ro+'></div>'+
    '<div class="fld"><label>json_schema</label><select id="m_new_js_'+pk+'" data-keep="1" data-f="js" '+ro+'>'+
      '<option>degradable</option><option>native</option></select></div>'+
    '<div class="fld"><label>tools</label>'+
      '<input id="m_new_tools_'+pk+'" data-keep="1" data-f="tools" type="checkbox" '+ro+'></div>'+
    '<button class="primary" '+ro+' onclick="modelAdd(this,\''+esc(name)+'\')">添加并热重载</button>'+
    '<button '+ro+' onclick="toggleModelAdd(\''+esc(name)+'\')">取消</button>'+
    '<span class="kv">没写全的 caps 由后端补保守默认（degradable / 无 tools）</span></div>';
  // 删除：危险按钮 + 输入 REMOVE 的二次确认（与删 key 同规矩；不弹 confirm() 对话框，
  // 因为无头 Chrome 里原生对话框会卡住 CDP 自检）
  if(S.modelAsk&&S.modelAsk.provider===name){
    const mid=S.modelAsk.model_id;
    h+='<div class="card" id="m_del_form" style="border-color:#8b2c26;margin-top:8px">'+
      '<b>删除模型 '+esc(name)+' / '+esc(mid)+'</b>'+
      '<div class="kv">只改 config.json 的 providers[].models；删完该 provider 一个模型都不剩会被守卫 400 拦住。</div>'+
      '<div class="row" style="margin-top:8px">'+
      '<div class="fld"><label>输入 REMOVE 才能提交</label>'+
        '<input id="m_del_confirm" data-keep="1" autocomplete="off" placeholder="REMOVE" '+
        'oninput="modelDelWatch();snapshotForms()"></div>'+
      '<button class="danger" id="m_del_btn" disabled onclick="modelRemove(this,\''+esc(name)+'\',\''+
        esc(mid)+'\')">确认删除</button>'+
      '<button onclick="modelAskRemove(null,null)">取消</button></div></div>';
  }
  return h+'</div>';
}
function rowField(el,f){const tr=el&&el.closest?el.closest("tr"):null;
  return tr?tr.querySelector('[data-f="'+f+'"]'):null;}
function toggleModelAdd(provider){
  if(S.remote){toast("远程只读：不能改模型",true);return;}
  S.modelAdd=S.modelAdd||{}; S.modelAdd[provider]=!S.modelAdd[provider];
  renderKeys();
}
function modelAskRemove(provider,model_id){
  if(provider===null){S.modelAsk=null;renderKeys();return;}
  if(S.remote){toast("远程只读：不能删模型",true);return;}
  S.modelAsk={provider:provider,model_id:model_id};
  renderKeys();
  const el=document.getElementById("m_del_confirm");
  if(el){try{el.scrollIntoView({block:"center",behavior:"smooth"});}catch(e){}
         try{el.focus({preventScroll:true});}catch(e){try{el.focus();}catch(e2){}}}
}
function modelDelWatch(){
  const inp=document.getElementById("m_del_confirm"), btn=document.getElementById("m_del_btn");
  if(inp&&btn)btn.disabled=(inp.value!=="REMOVE")||S.remote;
}
async function modelWrite(el,body,okf){
  if(S.remote){toast("远程只读：不能改模型",true);return;}
  if(el)el.disabled=true;
  try{
    const r=await api("model",{method:"POST",body:body});
    toast(okf(r));
    S.modelDraft=null; S.modelAsk=null; S.modelAdd={};   // 提交成功：丢弃草稿，以服务端为准
    DATA.status=null; DATA.config=null; await loadStatus(); renderKeys();
  }catch(e){
    if(el)el.disabled=false;
    toast("模型操作失败："+e.message,true);
  }
}
function checkChain(raw){
  const s=String(raw==null?"":raw).trim();
  if(!/^[0-9]{1,4}$/.test(s)||Number(s)<1||Number(s)>9999){
    toast("chain 必须是 1..9999 的整数（不接空值/小数/字符串）",true);return null;}
  return Number(s);
}
function modelSaveChain(el,provider,model_id){
  const f=rowField(el,"chain"); const chain=checkChain(f?f.value:"");
  if(chain===null)return;
  modelWrite(el,{action:"update",provider:provider,model_id:model_id,patch:{chain:chain}},
    r=>provider+"/"+model_id+" 的 chain 已改为 "+chain+"（只动这一个，其它模型档位不变），备份 "+v(r.backup));
}
function modelSaveCaps(el,provider,model_id){
  const js=rowField(el,"js"), tl=rowField(el,"tools");
  const caps={json_schema:(js&&js.value)||"degradable",tools:!!(tl&&tl.checked)};
  modelWrite(el,{action:"update",provider:provider,model_id:model_id,patch:{caps:caps}},
    r=>provider+"/"+model_id+" 的能力已改为 schema="+caps.json_schema+"、tools="+caps.tools+
      "，备份 "+v(r.backup));
}
function modelRemove(el,provider,model_id){
  const inp=document.getElementById("m_del_confirm");
  if(!inp||inp.value!=="REMOVE"){toast("必须先输入 REMOVE 才能删模型",true);return;}
  modelWrite(el,{action:"remove",provider:provider,model_id:model_id,confirm:"REMOVE"},
    r=>"已删除模型 "+provider+"/"+model_id+"（剩 "+(r.models_after||[]).length+" 个），备份 "+v(r.backup));
}
function modelAdd(el,provider){
  const box=el&&el.closest?el.closest("[data-model-add]"):null;
  if(!box){toast("找不到添加行",true);return;}
  const idEl=box.querySelector('[data-f="id"]'), jsEl=box.querySelector('[data-f="js"]'),
        tlEl=box.querySelector('[data-f="tools"]');
  const id=(idEl&&idEl.value||"").trim();
  if(!id){toast("模型 id 不能为空",true);return;}
  const chain=checkChain(box.querySelector('[data-f="chain"]').value);
  if(chain===null)return;
  const caps={json_schema:(jsEl&&jsEl.value)||"degradable",tools:!!(tlEl&&tlEl.checked)};
  modelWrite(el,{action:"add",provider:provider,model:{id:id,chain:chain,caps:caps}},
    r=>"已添加模型 "+provider+"/"+id+"（chain "+chain+"，现有 "+(r.models_after||[]).length+
      " 个模型），备份 "+v(r.backup));
}

// ---------------------------------------------------------------- 请求日志
function renderLogs(){
  const d=DATA.requests||{};
  const all=d.rows||[];
  let rows=all;
  if(S.f.q){const q=String(S.f.q).toLowerCase();
    rows=all.filter(r=>((r.provider||"")+" "+(r.model||"")+" "+(r.verdict||"")).toLowerCase().indexOf(q)>=0);}
  const provSet=new Set(), modelSet=new Set();
  all.forEach(r=>{if(r.provider)provSet.add(r.provider);if(r.model)modelSet.add(r.model);});
  let html='<h2>请求日志<span class="q" title="来自 usage.jsonl：'+esc(v(d.path))+'（共 '+num(v(d.total,0))+
    ' 条'+(d.enabled?"":"；usage_log 未开启")+'）。列表走 /admin/requests，默认只显示 6 列，其余进「展开全部列」。">?</span>'+
    (d.enabled?"":'<span class="kv bad">usage_log 未开启</span>')+'</h2><div class="card"><div class="row">'+
    sel("f_provider","provider",Array.from(provSet),S.f.provider)+
    sel("f_model","模型",Array.from(modelSet),S.f.model)+
    sel("f_verdict","verdict",Object.keys(VERDICT_CN),S.f.verdict)+
    '<div class="fld"><label>关键词（q）</label><input id="f_q" value="'+esc(S.f.q)+'"></div>'+
    '<button onclick="applyLogFilter()">过滤</button><button onclick="clearLogFilter()">清空过滤</button>'+
    '<button onclick="logPage(-1)">上一页</button><button onclick="logPage(1)">下一页</button></div>'+
     '<div class="kv">显示 '+(d.offset||0)+'–'+((d.offset||0)+rows.length)+' / 共 '+num(v(d.total,0))+' 条'+
    ' ｜ 一页 '+v(d.limit,20)+' 条</div>'+
    // 默认只显示 6 列，其余进「展开全部列」
    '<div class="row" style="margin-top:6px"><button class="disc" onclick="toggleLogCols()">'+
      (S.logFull?"收起为 6 列":"展开全部列")+'</button></div></div>';
  const cols=[["时间",r=>esc(v(r.ts))],["provider",r=>esc(v(r.provider))],["模型",r=>esc(v(r.model))],
    ["HTTP",r=>esc(v(r.http))],["耗时",r=>ms(r.latency_ms)],["verdict",r=>verdictTag(r.verdict)],
    ["key#",r=>"#"+esc(v(r.key_index))],
    ["重试链",r=>esc((r.attempts||[]).map(a=>(a.provider||"?")+"/"+(a.model||"?")+
      ":"+(a.http||"-")+"("+(a.reason||"-")+(a.degrade?",降级":"")+")").join(" → ")||"—")],
    ["JSON 降级",r=>r.degraded_json?"是":"否"],["token",r=>num(v(r.total_tokens,0))]];
  const shown=S.logFull?cols:cols.slice(0,6);
  html+='<table class="resp"><thead><tr>'+shown.map(c=>'<th>'+c[0]+'</th>').join("")+'</tr></thead><tbody>'+
    (rows.length?rows.map(r=>'<tr>'+shown.map(c=>'<td data-label="'+c[0]+'">'+c[1](r)+'</td>').join("")+'</tr>').join("")
      :'<tr><td colspan="'+shown.length+'" class="muted">没有记录（usage_log 关闭，或还没有请求）</td></tr>')+
    '</tbody></table>';
  $("#view").innerHTML=html;
}
function toggleLogCols(){S.logFull=!S.logFull;renderLogs();}
function sel(id,label,items,cur){return '<div class="fld"><label>'+esc(label)+'</label><select id="'+id+'">'+
  '<option value="">全部</option>'+items.map(i=>'<option '+(i===cur?"selected":"")+'>'+esc(i)+'</option>').join("")+'</select></div>';}
function applyLogFilter(){S.f={provider:$("#f_provider")?$("#f_provider").value:"",model:$("#f_model")?$("#f_model").value:"",
  verdict:$("#f_verdict")?$("#f_verdict").value:"",q:$("#f_q")?$("#f_q").value:""};S.logOffset=0;loadTab("logs");}
function clearLogFilter(){S.f={provider:"",model:"",verdict:"",q:""};S.logOffset=0;loadTab("logs");}
function logPage(dir){S.logOffset=Math.max(0,S.logOffset+dir*S.logLimit);loadTab("logs");}

// ---------------------------------------------------------------- 用量
// 降噪：图表只用来区分 provider 系列，用低饱和灰阶；正常态不用彩色（颜色留给异常）。
const PALETTE=["#8b949e","#6e7681","#a8b0b8","#565d66","#9aa4b2","#7c8794","#c2c8cf"];
function renderUsage(){
  const u=DATA.usage||{}; const byDay=u.by_day||{}; const byProv=u.by_provider||{};
  const days=Object.keys(byDay).sort();
  const provs=Object.keys(byProv);
  let html='<h2>用量统计<span class="q" title="来自 usage.jsonl 聚合：'+esc(v(u.path))+'（范围 '+esc(v(u.since))+
    ' 起 '+v(u.days)+' 天；'+(u.enabled?"usage_log 已开启":"usage_log 未开启")+'）。">?</span>'+
    (u.enabled?"":'<span class="kv bad">usage_log 未开启</span>')+'</h2><div class="card"><div class="row">'+
    [7,30,90].map(n=>'<button class="'+(S.days===n?"primary":"")+'" onclick="setDays('+n+')">'+n+' 天</button>').join("")+
    '</div></div>';
  html+='<div class="grid">'+metric("请求数",num(v((u.totals||{}).requests,0)))+
    metric("成功率",pct((u.totals||{}).success_rate))+
    metric("429 / 401",num(v((u.totals||{}).n429,0))+" / "+num(v((u.totals||{}).n401,0)))+
    metric("token 合计",num(v((u.totals||{}).total_tokens,0)))+
    metric("p50 延迟",ms((u.totals||{}).p50_latency_ms))+'</div>';
  html+='<h2>按天请求数（按 provider 堆叠）+ 成功率折线</h2><div class="card">'+svgUsage(days,byDay,u.by_day_provider||{})+'</div>';
  html+='<h2>按 provider 占比</h2><div class="card">'+svgShare(provs,byProv)+
    '<table class="resp"><thead><tr><th>provider</th><th>请求</th><th>成功</th><th>成功率</th><th>429</th><th>401</th>'+
    '<th>token</th><th>p50</th><th>占比</th></tr></thead><tbody>'+
    (provs.length?provs.map(p=>{const b=byProv[p]||{};return '<tr><td data-label="provider">'+esc(p)+'</td>'+
      '<td data-label="请求">'+num(v(b.requests,0))+'</td><td data-label="成功">'+num(v(b.ok,0))+'</td>'+
      '<td data-label="成功率">'+pct(b.success_rate)+'</td><td data-label="429">'+num(v(b.n429,0))+'</td>'+
      '<td data-label="401">'+num(v(b.n401,0))+'</td><td data-label="token">'+num(v(b.total_tokens,0))+'</td>'+
      '<td data-label="p50">'+ms(b.p50_latency_ms)+'</td><td data-label="占比">'+pct(b.share)+'</td></tr>';}).join("")
      :'<tr><td colspan="9" class="muted">没有数据</td></tr>')+'</tbody></table></div>';
  html+=renderUsageGroup();
  const vd=(DATA.summary&&DATA.summary.verdicts_1h)||{};
  const vkeys=Object.keys(vd);
  html+='<h2>近 1 小时 verdict 分布</h2><div class="card">'+
    (vkeys.length?vkeys.map(k=>verdictTag(k)+" "+num(vd[k])).join(" ｜ "):'<span class="muted">无请求</span>')+'</div>';
  $("#view").innerHTML=html;
}
function setDays(n){S.days=n;loadTab("usage");}
// T1.3：分组/窗口切换都是真按钮 → 真请求（重新拉 /api/usage?group=&window=）→ 真重渲染
function setUsageGroup(g){S.usageGroup=g;loadUsageGroup().then(renderUsage)
  .catch(e=>toast("加载失败："+e.message,true));}
function setUsageWindow(w){S.usageWindow=w;loadUsageGroup().then(renderUsage)
  .catch(e=>toast("加载失败："+e.message,true));}
const USAGE_GROUP_CN={caller:"调用方",route:"路由",provider:"provider",model:"模型"};
function renderUsageGroup(){
  const d=DATA.usageGroup||{};
  const groups=(d.options&&d.options.group)||["caller","route","provider","model"];
  const windows=(d.options&&d.options.window)||["1h","24h","7d"];
  const cur=USAGE_GROUP_CN[d.group]?d.group:S.usageGroup;
  const curW=windows.indexOf(d.window)>=0?d.window:S.usageWindow;
  let html='<h2 id="usage_group_switch">分组用量<span class="q" title="T1.3：按 '+
    esc(USAGE_GROUP_CN[cur]||cur)+' 分组的窗口聚合，口径与 usage.jsonl 逐条可对账（窗口内非 probe 行直接求和）。'+
    '索引在服务端惰性重建 + 增量计数，不逐请求重扫文件。">?</span></h2><div class="card"><div class="row" id="usage_group_row">'+
    groups.map(g=>'<button id="ug_'+g+'" class="'+(cur===g?"primary":"")+'" onclick="setUsageGroup(\''+g+'\')">'+
      esc(USAGE_GROUP_CN[g]||g)+'</button>').join("")+
    '<span class="muted" style="margin:0 8px">窗口</span>'+
    windows.map(w=>'<button id="uw_'+w+'" class="'+(curW===w?"primary":"")+'" onclick="setUsageWindow(\''+w+'\')">'+
      esc(w)+'</button>').join("")+'</div>';
  if(!d.groups){html+='<div class="muted">分组用量加载中…</div></div>';return html;}
  const tot=d.totals||{};
  html+='<div class="grid">'+metric("窗口请求数",num(v(tot.requests,0)),esc(v(d.since))+" → "+esc(v(d.until)))+
    metric("成功率",pct(tot.success_rate))+
    metric("成功 / 失败",num(v(tot.ok,0))+" / "+num(v(tot.failed,0)))+
    metric("token（prompt/completion/合计）",num(v(tot.prompt_tokens,0))+" / "+num(v(tot.completion_tokens,0))+
      " / "+num(v(tot.total_tokens,0)))+'</div>';
  const rows=d.groups||[];
  html+='<table class="resp" id="usage_group_table"><thead><tr><th>'+esc(USAGE_GROUP_CN[cur]||cur)+
    '</th><th>请求</th><th>成功</th><th>失败</th><th>成功率</th><th>prompt</th><th>completion</th>'+
    '<th>合计 token</th><th>请求占比</th><th>token 占比</th></tr></thead><tbody>'+
    (rows.length?rows.map(g=>'<tr><td data-label="'+esc(USAGE_GROUP_CN[cur]||cur)+'">'+esc(g.name)+'</td>'+
      '<td data-label="请求">'+num(v(g.requests,0))+'</td><td data-label="成功">'+num(v(g.ok,0))+'</td>'+
      '<td data-label="失败">'+num(v(g.failed,0))+'</td><td data-label="成功率">'+pct(g.success_rate)+'</td>'+
      '<td data-label="prompt">'+num(v(g.prompt_tokens,0))+'</td>'+
      '<td data-label="completion">'+num(v(g.completion_tokens,0))+'</td>'+
      '<td data-label="合计 token">'+num(v(g.total_tokens,0))+'</td>'+
      '<td data-label="请求占比">'+pct(g.share)+'</td><td data-label="token 占比">'+pct(g.token_share)+'</td></tr>')
      .join(""):'<tr><td colspan="10" class="muted">该窗口没有数据</td></tr>')+'</tbody></table>';
  html+='<div class="row" id="usage_metrics_state" style="margin-top:9px"><span class="muted">Prometheus /metrics：'+
    (d.metrics?'<span class="kv ok">已开启</span>（<code>/metrics</code>）':
     '<span class="kv">未开启</span>（config.json 的 usage_log.metrics=false，属正常默认）')+'</span></div></div>';
  return html;
}
function svgUsage(days,byDay,byDayProv){
  if(!days.length)return '<div class="muted">没有数据（先让中转站跑几个请求，或检查 usage_log）</div>';
  const W=820,H=250,pad={l:44,r:16,t:14,b:34};
  const iw=W-pad.l-pad.r, ih=H-pad.t-pad.b;
  const totals=days.map(d=>Number((byDay[d]||{}).requests||0));
  const maxT=Math.max(1,...totals);
  const pal={};
  days.forEach(d=>Object.keys(byDayProv[d]||{}).forEach(p=>{if(!(p in pal))pal[p]=PALETTE[Object.keys(pal).length%PALETTE.length];}));
  const bw=Math.max(6,Math.min(38,iw/Math.max(1,days.length)-6));
  let bars="",pts="",labels="";
  days.forEach((d,i)=>{
    const x=pad.l+(iw/days.length)*i+(iw/days.length-bw)/2;
    let y=pad.t+ih;
    const stack=byDayProv[d]||{[Object.keys(byDay).length?"":""]:totals[i]};
    let used=false;
    Object.keys(stack).forEach(p=>{
      const n=Number(stack[p]||0); if(!n)return; used=true;
      const h=(n/maxT)*ih; y-=h;
    bars+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+
        '" fill="'+(pal[p]||"#8b949e")+'" stroke="#0d1117" stroke-width="0.5"><title>'+
        esc(d)+" "+esc(p)+": "+n+'</title></rect>';
    });
    if(!used){const h=(totals[i]/maxT)*ih;y-=h;
      bars+='<rect x="'+x.toFixed(1)+'" y="'+y.toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+h.toFixed(1)+
        '" fill="#8b949e"><title>'+esc(d)+": "+totals[i]+'</title></rect>';}
    const rate=Number((byDay[d]||{}).success_rate||0);
    const cx=x+bw/2, cy=pad.t+ih-(rate*ih);
    pts+=(pts?" ":"")+cx.toFixed(1)+","+cy.toFixed(1);
    if(days.length<=31)labels+='<text x="'+(x+bw/2).toFixed(1)+'" y="'+(H-12)+'" fill="#8b949e" font-size="9" '+
      'text-anchor="middle" transform="rotate(-38 '+(x+bw/2).toFixed(1)+' '+(H-12)+')">'+esc(d.slice(5))+'</text>';
  });
  let grid="";
  [0,0.25,0.5,0.75,1].forEach(f=>{const y=pad.t+ih-f*ih;
    grid+='<line x1="'+pad.l+'" y1="'+y.toFixed(1)+'" x2="'+(W-pad.r)+'" y2="'+y.toFixed(1)+'" stroke="#30363d" stroke-dasharray="3 4"/>'+
      '<text x="'+(pad.l-6)+'" y="'+(y+3).toFixed(1)+'" fill="#8b949e" font-size="9" text-anchor="end">'+Math.round(f*maxT)+'</text>';});
  const legend=Object.keys(pal).map((p,i)=>'<rect x="'+(pad.l+i*92)+'" y="'+(H-6)+'" width="9" height="9" fill="'+pal[p]+'"/>'+
    '<text x="'+(pad.l+i*92+13)+'" y="'+(H+2)+'" fill="#8b949e" font-size="10">'+esc(p)+'</text>').join("");
  return '<svg viewBox="0 0 '+W+' '+(H+16)+'" width="100%" height="auto" role="img" aria-label="按天请求数与成功率">'+
    grid+bars+'<polyline points="'+pts+'" fill="none" stroke="#e6edf3" stroke-width="2"/>'+
    '<text x="'+(W-pad.r)+'" y="'+(pad.t+10)+'" fill="#8b949e" font-size="10" text-anchor="end">灰线=成功率(0–100%)</text>'+
    labels+legend+'</svg>';
}
function svgShare(provs,byProv){
  if(!provs.length)return '<div class="muted">没有数据</div>';
  const W=820,rh=26,H=provs.length*rh+16;
  let out="";
  provs.forEach((p,i)=>{
    const share=Number((byProv[p]||{}).share||0);
    const w=Math.max(2,share*(W-190));
    out+='<text x="0" y="'+(i*rh+18)+'" fill="#e6edf3" font-size="11">'+esc(p)+'</text>'+
      '<rect x="120" y="'+(i*rh+7)+'" width="'+(W-190)+'" height="14" rx="4" fill="#1c2128"/>'+
      '<rect x="120" y="'+(i*rh+7)+'" width="'+w.toFixed(1)+'" height="14" rx="4" fill="'+
      PALETTE[i%PALETTE.length]+'"/>'+
      '<text x="'+(W-64)+'" y="'+(i*rh+18)+'" fill="#8b949e" font-size="11">'+pct(share)+'</text>';
  });
  return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="auto" role="img" aria-label="provider 占比">'+out+'</svg>';
}

// ---------------------------------------------------------------- 调用方（T1.1）
function renderCallers(){
  const d=DATA.callersTab||{}; const rows=d.callers||[];
  const mode=v(d.auth_mode,"unknown");
  let html='<h2>调用方<span class="q" title="调用方 key 只用来识别「谁在调用」，和厂商 key 无关。这里只显示「是否配置 key」与指纹，永远不显示明文。rpm 是 60 秒滑动窗口；daily_tokens 是本地自然日累计（从 usage.jsonl 惰性重建 + 内存累加）。">?</span></h2>';
  html+='<div class="grid">';
  html+=metric("鉴权模式",mode,mode==="require_key"?"所有调用方都必须带 key":"本机免密，可匿名");
  html+=metric("已配置调用方",String(rows.length),"allow_routes + rpm + 日预算");
  html+='</div>';
  if(d.note)html+='<div class="muted" style="margin:4px 0 8px">'+esc(d.note)+'</div>';
  html+='<table class="resp"><thead><tr><th>调用方</th><th>key</th><th>rpm（近 1 分钟）</th>'+
    '<th>今日 token</th><th>allow_routes</th><th>近期请求</th><th>备注</th></tr></thead><tbody>';
  rows.forEach(c=>{
    const name=esc(v(c.name));
    const keyCell=c.key_set
      ? badge("已配置 · "+v(c.key_fp),"ok")+'<div class="kv">'+esc(v(c.key_source))+'</div>'
      : badge("未配置 key","warn");
    const rpmMax=v(c.rpm); const rpmUsed=v(c.rpm_used_last_min,0);
    const rpmCell=(rpmMax===undefined||rpmMax===null||rpmMax==="")?'<span class="muted">不限</span>'
      : esc(rpmUsed+" / "+rpmMax)+(Number(rpmUsed)>=Number(rpmMax)?' '+badge("已达上限","bad"):"");
    const tokMax=v(c.daily_tokens); const tokUsed=v(c.tokens_today,0);
    const tokCell=(tokMax===undefined||tokMax===null||tokMax==="")?num(tokUsed)+' <span class="muted">/ 不限</span>'
      : num(tokUsed)+" / "+num(tokMax)+(Number(tokUsed)>=Number(tokMax)?' '+badge("超预算","bad"):"");
    const ar=c.allow_routes;
    const arCell=(Array.isArray(ar)&&ar.length)?ar.map(x=>badge(x,"warn")).join(" "):'<span class="muted">不限</span>';
    html+='<tr><td data-label="调用方"><b>'+name+'</b></td><td data-label="key">'+keyCell+'</td>'+
      '<td data-label="rpm">'+rpmCell+'</td><td data-label="今日 token">'+tokCell+'</td>'+
      '<td data-label="allow_routes">'+arCell+'</td>'+
      '<td data-label="近期请求">'+num(v(c.recent_requests,0))+'</td>'+
      '<td data-label="备注">'+esc(v(c.note,"—"))+'</td></tr>';
  });
  if(!rows.length)html+='<tr><td colspan="7" class="muted">中转站 config.json 里还没有 `callers` 段</td></tr>';
  html+='</tbody></table>';
  html+='<div class="muted" style="margin-top:8px">本页只读：新增/修改调用方请编辑中转站 config.json 的 `auth` / `callers`（key_env 指向 keys.env），改完自动热加载。</div>';
  $("#view").innerHTML=html;
}

// ---------------------------------------------------------------- 能力矩阵
function renderCaps(){
  const d=DATA.caps||{}; const models=d.models||[];
  let html='<h2>能力矩阵<span class="q" title="caps 来自 config.json，实测来自 usage.jsonl 里 alias=probe 的记录。点「实测」会对该模型真跑 chat + strict json_schema + tools（会消耗额度、会计入 usage）。">?</span></h2>';
  html+='<table class="resp"><thead><tr><th>chain</th><th>provider</th><th>模型</th><th>caps</th><th>近期计数</th>'+
    '<th>最近实测</th><th>操作</th></tr></thead><tbody>';
  models.forEach(m=>{
    const pv=esc(m.provider), mv=esc(m.model);
    const counts=m.counts||{};
    html+='<tr><td data-label="chain">'+esc(v(m.chain))+'</td><td data-label="provider">'+esc(m.provider)+'</td>'+
      '<td data-label="模型">'+esc(m.model)+(m.disabled?badge("disabled","bad"):"")+'</td>'+
      '<td data-label="caps">'+capsLine(m.caps)+'</td>'+
      '<td data-label="近期计数"><span class="kv">ok='+esc(v(counts.ok,0))+' 429='+esc(v(counts.ratelimit,0))+
        ' bad_json='+esc(v(counts.bad_json,0))+' unusable='+esc(v(counts.unusable,0))+
        ' 降级判不过='+esc(v(counts.degraded_schema_rejected,0))+'</span></td>'+
      '<td data-label="最近实测">'+probeCell(m.probes)+'</td>'+
      '<td data-label="操作"><button class="primary" onclick="probeModel(\''+pv+'\',\''+mv+'\')">实测</button>'+
      '<div id="probe_'+pv.replace(/[^a-z0-9]/gi,"_")+'_'+mv.replace(/[^a-z0-9]/gi,"_")+'" class="kv"></div></td></tr>';
  });
  if(!models.length)html+='<tr><td colspan="7" class="muted">读不到 /status</td></tr>';
  html+='</tbody></table>';
  $("#view").innerHTML=html;
}
function probeCell(pr){
  pr=pr||{}; const ks=Object.keys(pr);
  if(!ks.length)return '<span class="muted">未实测</span>';
  return ks.map(k=>{const x=pr[k]||{};
    return badge(k+" "+(x.http||"-")+" "+ms(x.latency_ms),/OK$/.test(k)?"ok":"bad");}).join("<br>");
}
async function probeModel(provider,model){
  const id="probe_"+String(provider).replace(/[^a-z0-9]/gi,"_")+"_"+String(model).replace(/[^a-z0-9]/gi,"_");
  const el=document.getElementById(id);
  if(el)el.innerHTML="实测中…（最多 ~2 分钟）";
  toast("正在实测 "+provider+"/"+model+"（真打上游，会消耗额度）");
  try{
    const r=await api("probe",{method:"POST",body:{provider:provider,model:model}});
    const txt=(r.results||[]).map(x=>x.kind+"="+x.verdict+" ("+x.http+", "+ms(x.latency_s*1000)+")").join("<br>");
    if(el)el.innerHTML=txt;
    toast("实测完成："+provider+"/"+model+" → "+(r.results||[]).map(x=>x.kind+":"+x.verdict).join("，"));
    DATA.caps=null; if(currentTab()==="caps")await loadTab("caps");
  }catch(e){if(el)el.innerHTML="<span class='bad'>"+esc(e.message)+"</span>";toast("实测失败："+e.message,true);}
}

// ---------------------------------------------------------------- Hindsight（面板联动 · 方案 C）
// 只读视图：服务端已经替浏览器摸过 8988 / 9999 并从 usage.jsonl 聚合 alias=hindsight。
// 每张卡带一个跳 8990（Hindsight 增强面板）对应页的深链接，新窗口打开。
function hsDeep(hash,label){
  return '<a class="btn" target="_blank" rel="noopener" href="'+esc(HSDASH+"/"+hash)+'">'+esc(label)+' ↗</a>';
}
function hsCard(title,value,sub,hash,label){
  const bad=/(连不上|error|HTTP |失败)/.test(String(sub||""));
  return '<div class="metric"><div class="k">'+esc(title)+'</div><div class="v">'+esc(value)+
    '</div><div class="x'+(bad?" bad":"")+'" title="'+esc(sub||"")+'">'+esc(sub||"")+'</div>'+
    '<div class="row" style="margin-top:9px">'+hsDeep(hash,label)+'</div></div>';
}
function renderUpstream(){
  const d=DATA.upstreamTab||{};
  const label=v(d.label,"上游");
  if(d.configured===false){
    $("#view").innerHTML='<h2>'+esc(label)+'</h2><div class="card"><div class="kv">'+
      '中转站没有配置 <code>integrations.upstream</code>，这个可选集成未启用，标签页平时不会显示。'+
      '启用方式见 <code>examples/hindsight/README.md</code>。</div></div>';
    return;
  }
  const s=DATA.summary||{};
  const a=d.api||{}, cp=d.cp||{}, st=d.stats||{}, ru=d.relay_usage||{};
  const td=ru.today||{}, hr=ru.hour||{};
  let html='<h2>'+esc(label)+'<span class="q" title="upstream 的 health_url、control_plane_url 与 usage.jsonl 里调用方别名的中转指标，都由本面板服务端只读取数后聚合；浏览器不直连上游。每张卡右边的按钮跳到该上游自己的增强面板对应页。">?</span></h2>';
  html+='<div class="grid">';
  // 卡 1：上游本体（config.integrations.upstream.health_url）
  html+=hsCard("上游本体", a.error?"连不上":v(a.status),
    a.error?("health：" + a.error):("database "+v(a.database)), "#ops", "8990 运维页");
  // 卡 2：上游的控制面（control_plane_url）
  html+=hsCard("控制面", cp.error?"连不上":v(cp.status),
    cp.error?("control plane：" + cp.error):("dataplane "+v((cp.dataplane||{}).status)+" · "+v((cp.dataplane||{}).url)),
    "#usage", "8990 用量页");
  // 卡 3：记忆规模（官方 CP /api/stats/<bank>）
  html+=hsCard("记忆规模（bank "+v(d.bank)+"）", st.error?"—":num(st.memories),
    st.error?("统计：" + st.error):("链接 "+num(st.links)+" ｜ 文档 "+num(st.documents)), "#memories", "8990 记忆页");
  html+='</div>';

  // 「中转站为这个上游干了多少活」——本任务的价值所在
  html+='<h2>中转站为 '+esc(label)+' 干了多少活<span class="q" title="来源：usage.jsonl 里调用方别名的行（经 /admin/requests 只读聚合）；成功率按 HTTP 200 计。">?</span></h2>';
  if(ru.error){
    html+='<div class="card"><span class="bad">聚合失败：'+esc(ru.error)+'</span></div>';
  }else{
    html+='<div class="grid">'+
      metric("今日次数", num(td.n), "成功 "+num(td.ok)+" ｜ 失败 "+num(td.fail))+
      metric("今日成功率", td.rate===null||td.rate===undefined?"—":pct(td.rate), "429 "+num(td.n429)+" 次")+
      metric("近 1h 次数", num(hr.n), "成功 "+num(hr.ok)+" ｜ 失败 "+num(hr.fail))+
      metric("平均耗时", ms(td.avg_ms), "今日均值（近 1h "+ms(hr.avg_ms)+"）")+
      '</div>';
    html+='<div class="oneliner">中转站当前健康候选 <b>'+esc(v(s.candidates_healthy))+
      '</b> ｜ 首选模型 <b>'+esc(v(s.top_model,"无可用候选"))+'</b>（从既有 /api/status 汇总，不重复请求）'+
      ' ｜ 关联指标 '+(ru.total_rows===undefined?"—":(num(ru.total_rows)+" 行"))+' ｜ 生成于 '+esc(v(d.generated_at))+'</div>';
  }
  html+='<div class="card" style="margin-top:10px"><div class="kv">这条链路的意义：'+esc(label)+' 的每次调用都经中转站 9110 打出去（usage.jsonl 里调用方别名）。'+
    '所以中转站的 429 / 失败，会直接表现为「调用方没拿到结果」。两边从此能互看一眼：这里看中转站为 '+esc(label)+' 干了多少活，对方面板看中转站上游健康。'+
    (s.top_model?"":" 当前没有可用候选，调用方的请求会失败。")+'</div>'+
    '<div class="row" style="margin-top:9px">'+hsDeep("#usage","看对方用量页")+hsDeep("#memories","看对方记忆页")+'</div></div>';
  $("#view").innerHTML=html;
}

// ---------------------------------------------------------------- 设置
function reqKnobs(){
  return [["per_attempt_timeout_s","单次尝试超时（s）","实测真实 retain 要 16–50s"],
    ["total_budget_s","总预算（s）","Hindsight 侧上限 120s，留 10s 余量"],
    ["reasoning_min_timeout_s","思考模型超时硬下限（s）",""],
    ["schema_min_max_tokens","schema 请求 max_tokens 下限","防思考模型把预算烧在 reasoning 里"],
    ["provider_cooldown_cap_s","provider 冷却软上限（s）","防单次抖动把 provider 关 900s"],
    ["provider_cooldown_hard_cap_s","provider 冷却硬上限（s）",""],
    ["provider_failure_window_s","连续失败观察窗口（s）",""],
    ["provider_escalate_after_failures","抬到硬上限所需失败次数",""]];
}
function renderSettings(){
  const cfg=(DATA.config&&DATA.config.config)||{};
  const st=DATA.status||{};
  const req=cfg.request||{}, lf=cfg.local_fallback||{};
  const hs=DATA.upstream||{};
  let html='<h2>设置<span class="q" title="所有写操作都走中转站 POST /admin/config：读磁盘最新 config.json → 只应用你改的那几个键 → 备份 + 原子写回 → 热重载。实测调参（chain 顺序、超时/冷却/rpm）不会被顺手冲掉。">?</span></h2>'+
    '<div class="card"><div class="kv">config.json：'+esc(v((DATA.config||{}).path))+' ｜ mtime '+
    esc(v((DATA.config||{}).mtime))+'</div></div>';

  // ---- 外观 · 背景图（面板自己的偏好：写 ui.json 与 assets/，不动 config.json）----
  const bg=((DATA.ui||{}).ui||{}).background||{};
  const bgOff=S.remote?"disabled":"";
  html+='<h2>外观 · 背景<span class="q" title="壁纸只影响面板外观：配置存在 ~/.hermes/llm-relay/ui.json，图片存在同目录 assets/。'+
    '**不写进 config.json**（那里的 chain/超时/rpm 是实测调参，有红线）。"></span></h2><div class="card">'+
    '<div class="kv">'+(bg.file?('当前壁纸：<b>'+esc(bg.file)+'</b>'):'当前没有壁纸（纯色背景）')+
    ' ｜ 遮罩 '+esc(String(bg.overlay))+'% ｜ 模糊 '+esc(String(bg.blur))+'px</div>'+
    (S.remote?'<div class="warn">远程只读：换壁纸只允许在本机面板上操作。</div>':"")+
    '<div class="row"><div class="fld"><label>选一张图（PNG / JPEG / WebP / GIF，≤8MB）</label>'+
    '<input type="file" id="bg_file" accept="image/png,image/jpeg,image/webp,image/gif" '+
    'onchange="uploadBg(this.files[0])" '+bgOff+'></div></div>'+
    '<div id="bg_drop" class="drop">也可以把图片拖到这里</div>'+
    '<div class="row"><div class="fld"><label>遮罩浓度 <b id="bg_ov_v">'+esc(String(bg.overlay==null?60:bg.overlay))+
    '</b>%（越大字越清）</label><input type="range" id="bg_ov" min="0" max="95" '+
    'value="'+esc(String(bg.overlay==null?60:bg.overlay))+'" oninput="previewBg()" '+bgOff+'></div>'+
    '<div class="fld"><label>模糊 <b id="bg_bl_v">'+esc(String(bg.blur==null?0:bg.blur))+
    '</b> px</label><input type="range" id="bg_bl" min="0" max="24" value="'+
    esc(String(bg.blur==null?0:bg.blur))+'" oninput="previewBg()" '+bgOff+'></div>'+
    '<div class="fld"><label>卡片不透明度 <b id="bg_ca_v">'+
    esc(String(bg.cardA==null?88:bg.cardA))+'</b>%（100=完全不透明；调低让壁纸从卡片后透出来）</label>'+
    '<input type="range" id="bg_ca" min="55" max="100" value="'+
    esc(String(bg.cardA==null?88:bg.cardA))+'" oninput="previewBg()" '+bgOff+'></div></div>'+
    '<div class="kv" style="margin-top:10px">对齐方式（横图当竖页壁纸时用得上）</div>'+
    '<div class="posgrid">'+["top left","top center","top right","center left","center center",
     "center right","bottom left","bottom center","bottom right"].map(function(p,i){
       const cur=((DATA.ui||{}).ui||{}).background||{};
       const on=sameBgPos(cur.pos||"center center",p)?" on":"";
       return '<button class="posbtn'+on+'" '+(S.remote?"disabled":"")+
         ' title="'+esc(p)+'" onclick="setBgPos(\''+p+'\')">'+["↖","↑","↗","←","•","→","↙","↓","↘"][i]+'</button>';
     }).join("")+'</div>'+
    '<div class="row" style="margin-top:8px"><div class="fld"><label>缩放方式</label>'+
    '<select id="bg_fit" onchange="setBgFit(this.value)" '+(S.remote?"disabled":"")+'>'+
    '<option value="cover"'+( (((DATA.ui||{}).ui||{}).background||{}).fit==="cover"?" selected":"")+
    '>铺满（裁掉多余，画感强）</option>'+
    '<option value="contain"'+( (((DATA.ui||{}).ui||{}).background||{}).fit==="contain"?" selected":"")+
    '>完整（整张都看得见，会留边）</option></select></div></div>'+
    '<div class="row"><button onclick="saveBg()" '+bgOff+'>保存外观</button>'+
    '<button class="danger" onclick="clearBg()" '+bgOff+'>清除壁纸</button></div></div>';

  // ---- 重启中转站（只允许局域回环；远程只读时禁用）----
  const R=S.restart||{};
  const rsDisabled=S.remote?"disabled":"";
  let rsState="";
  if(R.state==="running"){
    rsState='<div class="kv">正在重启…（每 1 秒轮询 /api/status，最多 30 秒）｜ 重启前 pid '+
      esc(v(R.before&&R.before.pid))+' uptime '+secs(R.before&&R.before.uptime_s)+'</div>';
  }else if(R.state==="done"){
    rsState='<div class="kv">已恢复：pid '+esc(v(R.before&&R.before.pid))+' → '+esc(v(R.after&&R.after.pid))+
      '（已换进程）｜ uptime '+secs(R.before&&R.before.uptime_s)+' → '+secs(R.after&&R.after.uptime_s)+'（变小）</div>';
  }else if(R.state==="failed"){
    rsState='<div class="kv bad">30 秒内没等到 healthy。手工命令：launchctl kickstart -k gui/$(id -u)/com.user.llm-relay</div>';
  }
  html+='<h2>重启中转站</h2><div class="card" style="border-color:#8b2c26">'+
    (S.remote?'<div class="warn">远程只读，无法重启（一键重启只允许在本机面板上执行）</div>':"")+
    '<div class="row"><div class="fld"><label>输入 RESTART 才能点重启</label>'+
      '<input id="rs_confirm" '+rsDisabled+' autocomplete="off" placeholder="RESTART" '+
      'oninput="document.getElementById(\'rs_btn\').disabled=(this.value!==\'RESTART\'||'+
      (S.remote?"true":"false")+')"></div>'+
    '<button id="rs_btn" class="danger" disabled '+rsDisabled+' onclick="restartRelay()">重启中转站</button></div>'+
    '<div class="kv">重启 = 让中转站进程自己退出（先 flush 响应再 os._exit），launchd 的 KeepAlive 会在 1–2 秒内拉起。'+
    '只影响 com.user.llm-relay：不碰 Hindsight，也不碰面板自己。</div>'+rsState+'</div>';

  html+='<h2>本地兜底窗口（local_fallback）</h2><div class="card"><div class="row">'+
    '<div class="fld"><label>启用</label><select id="lf_enabled"><option value="true"'+(lf.enabled?" selected":"")+'>是</option>'+
      '<option value="false"'+(!lf.enabled?" selected":"")+'>否</option></select></div>'+
    '<div class="fld"><label>时间窗（HH:MM-HH:MM，支持跨午夜）</label><input id="lf_window" value="'+esc(v(lf.window,""))+'"></div>'+
    '<div class="fld"><label>模型</label><input id="lf_model" value="'+esc(v(lf.model,""))+'"></div>'+
    '<button class="primary" onclick="saveFallback()">保存</button></div>'+
    '<div class="kv">本地 MLX：'+localMlx(st,lf)+'</div></div>';

  html+='<h2>provider 限流 / 并发</h2><div class="card">';
  (cfg.providers||[]).forEach(p=>{
    html+='<div class="row" style="margin-bottom:8px"><b style="min-width:110px">'+esc(v(p.name))+'</b>'+
      '<div class="fld"><label>rpm</label><input id="rpm_'+esc(p.name)+'" type="number" min="0" value="'+esc(v(p.rpm,0))+'"></div>'+
      '<div class="fld"><label>max_concurrency</label><input id="mc_'+esc(p.name)+'" type="number" min="1" value="'+esc(v(p.max_concurrency,1))+'"></div>'+
      '<button onclick="saveProvider(\''+esc(p.name)+'\')">保存</button>'+
      '<span class="kv">'+esc(v(p.rpm_note,""))+'</span></div>';
  });
  html+='</div>';

  html+='<h2>请求预算与冷却（request）</h2><div class="card"><div class="row">';
  reqKnobs().forEach(k=>{
    html+='<div class="fld"><label>'+esc(k[1])+'</label><input id="rq_'+esc(k[0])+'" type="number" step="1" min="0" value="'+
      esc(v(req[k[0]],""))+'"><span class="kv">'+esc(k[2])+'</span></div>';
  });
  html+='</div><div class="row" style="margin-top:8px"><button class="primary" onclick="saveRequest()">保存 request 设置</button>'+
    '<span class="kv">当前生效：'+esc(JSON.stringify(st.request||{}))+'</span></div></div>';

  const upLabel=v(hs.label,"上游");
  html+='<h2>'+esc(upLabel)+'（可选集成 · 只读 + 回滚）</h2><div class="card">'+
    '<div class="kv">health：'+esc(JSON.stringify(v(hs.health,"—")))+' ｜ start.sh mtime '+esc(v(hs.mtime))+
    ' ｜ 中转站地址 '+esc(v(hs.relay_url))+'</div>'+
    '<table class="resp"><thead><tr><th>配置项</th><th>值</th></tr></thead><tbody>'+
    ((hs.config_lines||[]).map(l=>'<tr><td data-label="配置项">'+esc(l.key)+'</td><td data-label="值">'+esc(l.value)+'</td></tr>').join("")
      ||'<tr><td colspan="2" class="muted">读不到 start.sh（integrations.upstream.start_script 未配置或不可读）</td></tr>')+'</tbody></table>'+
    '<div class="row" style="margin-top:10px"><div class="fld"><label>输入 ROLLBACK 才能点回滚（不可逆）</label>'+
    '<input id="rb_confirm" oninput="document.getElementById(\'rb_btn\').disabled=(this.value!==\'ROLLBACK\'||'+
    (S.remote?"true":"false")+')" placeholder="ROLLBACK"></div>'+
    '<button id="rb_btn" class="danger" disabled onclick="rollbackUpstream()">一键回滚 '+esc(upLabel)+'</button></div>'+
    '<div class="kv">回滚会把 start.sh 还原成最新备份并重启上游；只在追查「中转站把它弄坏」时才用，'+
    '优先修 relay。'+(S.remote?"（远程只读：回滚只在本机可用）":"")+'</div></div>';

  $("#view").innerHTML=html;
}
function localMlx(st,lf){
  const p=(st.providers||[]).filter(x=>x.name==="__local__")[0];
  if(!p)return "未配置（local_fallback 关闭）";
  const m=(p.models||[])[0]||{};
  return "窗口 "+esc(v(lf.window))+" ｜ 现在"+(m.in_window?"在窗口内":"不在窗口内")+
    " ｜ 健康检查 "+(m.disabled?"未通过/不可用":"通过");
}
async function saveFallback(){
  const patch={local_fallback:{enabled:$("#lf_enabled").value==="true",window:$("#lf_window").value.trim()}};
  const model=$("#lf_model").value.trim(); if(model)patch.local_fallback.model=model;
  await postConfig(patch);
}
async function saveProvider(name){
  const patch={providers_by_name:{}};
  patch.providers_by_name[name]={rpm:Number($("#rpm_"+name).value),max_concurrency:Number($("#mc_"+name).value)};
  await postConfig(patch);
}
async function saveRequest(){
  const patch={request:{}};
  reqKnobs().forEach(k=>{const el=$("#rq_"+k[0]); if(el&&el.value!=="")patch.request[k[0]]=Number(el.value);});
  await postConfig(patch);
}
async function postConfig(patch){
  try{
    const r=await api("config",{method:"POST",body:{patch:patch}});
    toast("已写入 config.json 并热重载（无需重启 Hindsight）。改动键："+((r.changed||[]).join("，")||"无")+
      (r.backup?"；备份 "+r.backup:""));
    DATA.config=null; DATA.status=null; DATA.summary=null;
    await loadTab("settings");
  }catch(e){toast("保存失败："+e.message,true);}
}
async function rollbackUpstream(){
  if($("#rb_confirm").value!=="ROLLBACK"){toast("必须先输入 ROLLBACK",true);return;}
  const label=v((DATA.upstream||{}).label,"上游");
  if(!confirm("确认回滚 "+label+" 的 start.sh 并重启上游？"))return;
  try{
    const r=await api("rollback",{method:"POST",body:{confirm:"ROLLBACK"}});
    toast("回滚执行完成（exit="+v(r.exit)+"）："+String(v(r.output,"")).slice(0,200));
  }catch(e){toast("回滚失败："+e.message,true);}
}

// ---------------------------------------------------------------- 重启中转站
async function restartRelay(){
  if(S.remote){toast("远程只读：不能重启中转站",true);return;}
  const inp=$("#rs_confirm");
  if(!inp||inp.value!=="RESTART"){toast("必须先输入 RESTART",true);return;}
  if(!confirm("确认重启中转站？9110 会有几秒不可用（正在跑的 Hindsight 请求会失败）。"))return;
  const btn=$("#rs_btn"); if(btn)btn.disabled=true;
  let before=null;
  try{const st=await api("status");before={pid:st.pid,uptime_s:st.uptime_s};}catch(e){}
  try{
    const r=await api("restart",{method:"POST",body:{confirm:"RESTART"}});
    before=r.before||before;                       // 响应体里的 before 是重启前权威快照
    S.restart={state:"running",before:before};
    banner("中转站正在重启，数据可能短暂不可用","warn");
    if(currentTab()==="settings")renderSettings();
    const t0=Date.now(); let after=null;
    while(Date.now()-t0<30000){
      await new Promise(res=>setTimeout(res,1000));   // 每 1 秒轮询
      try{
        const st=await api("status");
        if(st&&st.status==="healthy"&&Number(st.pid)!==Number(before&&before.pid)&&
           Number(st.uptime_s)<Number((before&&before.uptime_s)??1e9)){after=st;break;}
      }catch(e){/* 重启中连不上是预期：继续轮询 */}
    }
    if(after){
      hideBanner();                                 // 恢复后横幅自动消失
      S.restart={state:"done",before:before,after:{pid:after.pid,uptime_s:after.uptime_s}};
      DATA.status=null;DATA.summary=null;
      toast("中转站已重启并恢复 healthy（pid "+v(before.pid)+" → "+v(after.pid)+"）");
    }else{
      S.restart={state:"failed",before:before};
      toast("重启后 30 秒内没有恢复 healthy，请用面板上的手工命令",true);
    }
  }catch(e){
    hideBanner();
    S.restart={state:"failed",before:before,error:String(e.message||e)};
    toast("重启失败："+e.message,true);
  }
  if(currentTab()==="settings"){try{await loadTab("settings");}catch(e){renderSettings();}}
}

// ---------------------------------------------------------------- 启动
async function init(){
  // 深链接：?tab=&days=&provider=&q= 直进（巡检脚本用无头浏览器检查时也走这里）
  try{
    const Q=new URLSearchParams(location.search);
    const tabQ=Q.get("tab");
    if(tabQ&&TABS.some(t=>t[0]===tabQ))history.replaceState(null,"","#"+tabQ);
    const daysQ=parseInt(Q.get("days"),10); if(daysQ) S.days=daysQ;
    const provQ=Q.get("provider"); if(provQ) S.f.provider=provQ;
    const qQ=Q.get("q"); if(qQ) S.f.q=qQ;
  }catch(e){}
  try{
    const w=BOOT?{local:BOOT.local}:await api("whoami");
    S.remote=!w.local;
    const ro=$("#ro");
    ro.className="pill "+(w.local?"ok":"warn");
    ro.textContent=w.local?"本机 · 可写":"远程只读（写操作一律 403）";
  }catch(e){}
  renderNav();
  await loadTab(currentTab());
  setInterval(async()=>{
    if(document.hidden)return;
    const t=currentTab();
    if(t==="overview"||t==="chain"){try{await loadTab(t);}catch(e){}}
  },10000);
  let wasHidden=false;
  document.addEventListener("visibilitychange",()=>{
    if(document.hidden){wasHidden=true;return;}
    if(wasHidden){wasHidden=false;loadTab(currentTab());}
  });
}
window.addEventListener("hashchange",()=>loadTab(currentTab()));
init();
// ---------------------------------------------------------------- 外观：背景图
function loadUi(){return api("ui").then(d=>{DATA.ui=d;return d;});}
function applyUI(bg){
  bg=bg||{};
  const on=!!(bg.enabled&&bg.file);
  document.body.classList.toggle("hasbg",on);
  const img=document.getElementById("bgimg");
  if(on&&img){
    img.style.backgroundImage='url("/assets/'+encodeURIComponent(bg.file)+'?v='+Date.now().toString(36)+'")';
    img.style.backgroundPosition=bg.pos||"center center";
    img.style.backgroundSize=(bg.fit||"cover");
    document.documentElement.style.setProperty("--bgmaskAlpha",(Number(bg.overlay||0)/100).toFixed(2));
    document.documentElement.style.setProperty("--bgblur",Number(bg.blur||0)+"px");
    document.documentElement.style.setProperty("--cardA",(Number(bg.cardA==null?88:bg.cardA)/100).toFixed(2));
  }
}
/* 位置按钮的标签是「top left」这种顺序，后端存的是 CSS 习惯的「left top」——
   直接 === 永远对不上，高亮会一直停在中心那个。两段排序后再比即可。 */
function sameBgPos(a,b){
  const k=function(s){ return String(s||"").split(" ").sort().join(" "); };
  return k(a)===k(b);
}
async function setBgPos(p){
  try{
    const r=await api("ui",{method:"POST",body:{background:{pos:p}}});
    DATA.ui=DATA.ui||{}; DATA.ui.ui=r; applyUI(r.background); renderSettings();
    toast("对齐方式：已切到 "+p+"（记得点「保存外观」固化遮罩/模糊）");
  }catch(e){toast("改对齐失败："+e.message,true);}
}
async function setBgFit(v){
  try{
    const r=await api("ui",{method:"POST",body:{background:{fit:v}}});
    DATA.ui=DATA.ui||{}; DATA.ui.ui=r; applyUI(r.background); renderSettings();
    toast(v==="contain"?"已改为「完整」：整张图都看得见":"已改为「铺满」：裁掉多余部分");
  }catch(e){toast("改缩放失败："+e.message,true);}
}
function previewBg(){
  const ov=Number((document.getElementById("bg_ov")||{}).value||0);
  const bl=Number((document.getElementById("bg_bl")||{}).value||0);
  const ca=Number((document.getElementById("bg_ca")||{}).value||88);
  const a=document.getElementById("bg_ov_v"), b=document.getElementById("bg_bl_v"), c=document.getElementById("bg_ca_v");
  if(a)a.textContent=String(ov); if(b)b.textContent=String(bl); if(c)c.textContent=String(ca);
  document.documentElement.style.setProperty("--bgmaskAlpha",(ov/100).toFixed(2));
  document.documentElement.style.setProperty("--bgblur",bl+"px");
  document.documentElement.style.setProperty("--cardA",(ca/100).toFixed(2));
}
async function uploadBg(file){
  if(!file){toast("没选到文件",true);return;}
  if(file.size>8*1024*1024){toast("图片太大：上限 8MB",true);return;}
  if(!/^image\//.test(file.type||"")){toast("只支持 PNG / JPEG / WebP / GIF",true);return;}
  const fr=new FileReader();
  fr.onerror=()=>toast("读不出这个文件",true);
  fr.onload=async()=>{
    try{
      const r=await api("ui/background",{method:"POST",body:{filename:file.name,data_url:fr.result}});
      toast("壁纸已更新（"+Math.round((r.bytes||0)/1024)+" KB）");
      await loadUi(); applyUI(DATA.ui.ui.background); renderSettings();
    }catch(e){toast("上传失败："+e.message,true);}
  };
  fr.readAsDataURL(file);
}
async function saveBg(){
  try{
    const r=await api("ui",{method:"POST",body:{background:{
      overlay:Number((document.getElementById("bg_ov")||{}).value||60),
      blur:Number((document.getElementById("bg_bl")||{}).value||0),
      cardA:Number((document.getElementById("bg_ca")||{}).value||88),enabled:true}}});
    DATA.ui=DATA.ui||{}; DATA.ui.ui=r;
    applyUI(r.background); toast("外观已保存"); renderSettings();
  }catch(e){toast("保存失败："+e.message,true);}
}
async function clearBg(){
  try{
    const was=(((DATA.ui||{}).ui||{}).background||{}).file;
    const r=await api("ui/background/clear",{method:"POST",body:{}});
    DATA.ui=DATA.ui||{}; DATA.ui.ui=r;
    applyUI({file:"",enabled:false});
    toast(was?("已清除壁纸（图片文件仍留在 assets/"+was+"，没删）"):"当前本来就没有壁纸");
    renderSettings();
  }catch(e){toast("清除失败："+e.message,true);}
}
// 拖放（用事件委托，设置页重渲染后依然有效）
document.addEventListener("dragover",e=>{
  const d=e.target&&e.target.closest?e.target.closest("#bg_drop"):null;
  if(d){e.preventDefault();d.classList.add("over");}
});
document.addEventListener("drop",e=>{
  const d=e.target&&e.target.closest?e.target.closest("#bg_drop"):null;
  if(d){e.preventDefault();d.classList.remove("over");
    const f=e.dataTransfer&&e.dataTransfer.files&&e.dataTransfer.files[0];
    if(f)uploadBg(f); else toast("拖进来的不是文件",true);}
});
// 启动时套用已保存的外观（快照模式下直接用 __BOOT__，保证截图里就是最终样子）
(function(){
  try{
    const b=(BOOT&&BOOT.ui&&BOOT.ui.background)||null;
    if(b){DATA.ui={ui:{background:b}};applyUI(b);return;}
    loadUi().then(d=>applyUI((d.ui||{}).background)).catch(()=>{});
  }catch(e){}
})();
// 非快照启动：先问一次 /api/upstream / /api/callers；未配置（configured:false）就把对应 tab 从导航去掉
if(!BOOT){loadUpstreamTab().then(renderNav).catch(()=>{});
  loadCallersTab().then(renderNav).catch(()=>{});}
</script></body></html>"""


def build_bootstrap(local: bool) -> dict:
    """?snapshot=1 用：把首屏数据一次性塞进页面，巡检/无头浏览器不用等异步请求。

    数据与正常路径同一来源（/status、/admin/*），因此不放松任何口径；只是少了几个来回，
    页面在被 dump 时就已经是渲染好的状态（无头 Chrome 的 --dump-dom 只在 load 时抓一次）。
    """
    scode, status = upstream("GET", "/status", timeout=15)
    ccode, cfg = upstream("GET", "/admin/config", timeout=15)
    rcode, req = upstream("GET", "/admin/requests", {"limit": 20, "probe": 1}, timeout=20)
    ucode, usage = upstream("GET", "/admin/usage", {"days": 7}, timeout=20)
    # T1.3：分组视图的首屏数据一起塞进来（否则 ?snapshot=1 dump 时用量页停在「加载中…」）
    ugcode, usage_group = upstream("GET", "/admin/usage",
                                   {"group": "caller", "window": "24h"}, timeout=20)
    hcode, hs = upstream("GET", "/admin/upstream", timeout=10)
    # 「上游」tab 的首屏数据也一起塞进来，否则无头浏览器只会 dump 到「加载中…」
    up_tab = build_upstream()
    # 「调用方」tab 同理（T1.1）；没配 callers 时 configured:false → 首屏就隐藏该 tab
    callers_tab = build_callers()
    return {
        "local": local, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ui": {"background": load_ui()},
        "http": {"status": scode, "config": ccode, "requests": rcode, "usage": ucode,
                 "usage_group": ugcode, "upstream": hcode},
        "data": {"summary": build_summary(), "status": status, "config": cfg,
                 "requests": req, "usage": usage, "usageGroup": usage_group, "caps": build_caps(),
                 "upstream": hs, "upstreamTab": up_tab, "callersTab": callers_tab},
    }


def render_page(boot: dict | None = None) -> str:
    page = PAGE.replace("__RELAY__", RELAY_BASE).replace("__HSDASH__", HINDSIGHT_DASH_BASE)
    if boot is not None:
        blob = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
        page = page.replace("</head>", f"<script>window.__BOOT__={blob};</script></head>", 1)
    return page


# 只读的 /api/*：直接转发中转站；写操作走 /admin/*（中转站侧只允许 loopback）
# 注意：面板本地的 /api/upstream（可选集成联动）不走这里，见 build_upstream()。
GET_API = {
    "status": ("/status", None),
    "config": ("/admin/config", None),
    "requests": ("/admin/requests", ("limit", "offset", "provider", "model", "verdict", "since")),
    "usage": ("/admin/usage", ("days", "group", "window")),
    # 「调用方」tab（T1.1）：中转站的 /admin/callers 只回「是否配置 key + 指纹」，
    # 内联 key 在服务端已被摘掉，面板这一侧永远看不到明文。
    "callers": ("/admin/callers", None),
    # 「设置」tab 的上游只读+回滚区块（T0.3 改名；relay_hindsight 保留为 deprecated 等价别名）
    "relay_upstream": ("/admin/upstream", None),
    "relay_hindsight": ("/admin/upstream", None),
}
# 面板本地实现的只读 GET（不转发中转站）；这些路径收到 POST 一律 405。
LOCAL_GET_API = ("summary", "caps", "ui", "whoami", "upstream", "hindsight")
POST_API = {
    "probe": "/admin/probe",
    "model": "/admin/model",
    "key": "/admin/key",
    "key_replace": "/admin/key/replace",
    "key_remove": "/admin/key/remove",
    "key_state": "/admin/key/state",
    "reorder": "/admin/reorder",
    "reload": "/admin/reload",
    "restart": "/admin/restart",
    "rollback": "/admin/upstream/rollback",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "llm-relay-dashboard/1.0"
    protocol_version = "HTTP/1.1"
    access_key = ""   # 非空时，非本机访问必须携带密钥（?k= 或 cookie）
    allow_remote_write = False   # 默认电话/局域网只读

    def log_message(self, fmt, *args):
        # 只记到 stderr 的默认行为关掉，避免把请求行/参数写进日志
        return

    def _is_local(self) -> bool:
        host = self.client_address[0] if self.client_address else ""
        return host.startswith("127.") or host in ("::1", "localhost", "")

    def _check_key(self, q: dict) -> tuple[bool, bool]:
        """返回 (是否放行, 是否要种 cookie)。本机或未配密钥时一律放行。"""
        if self._is_local() or not self.access_key:
            return True, False
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            if "=" in part:
                k, val = part.strip().split("=", 1)
                if k == KEY_COOKIE and hmac.compare_digest(val, self.access_key):
                    return True, False
        given = (q.get("k") or [""])[0]
        if given and hmac.compare_digest(given, self.access_key):
            return True, True   # 通过 ?k=密钥 进来，顺便种 cookie
        return False, False

    def _send(self, code: int, body: object, ctype: str = "application/json; charset=utf-8",
              extra_headers: list | None = None) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, val in (extra_headers or []):
            self.send_header(k, val)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _parse(self) -> tuple[str, dict]:
        p = urllib.parse.urlparse(self.path)
        return p.path, urllib.parse.parse_qs(p.query)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            obj = json.loads(raw.decode("utf-8", "replace") or "{}")
            return obj if isinstance(obj, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    # -------------------------------------------------------------- GET
    def do_GET(self) -> None:
        path, q = self._parse()
        ok, set_cookie = self._check_key(q)
        if not ok:
            if path.startswith("/api/"):
                return self._send(401, {"detail": "需要访问密钥：请在网址后加 ?k=密钥"})
            return self._send(200, LOGIN_PAGE, "text/html; charset=utf-8")
        extra = [("Set-Cookie",
                  f"{KEY_COOKIE}={self.access_key}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax")]
        if set_cookie and not path.startswith("/api/"):
            # ?k= 打开页面：种好 cookie 后 302 把密钥从地址栏抹掉（手机历史里也别留）
            rest = {k: v for k, v in q.items() if k != "k"}
            loc = path + ("?" + urllib.parse.urlencode(rest, doseq=True) if rest else "")
            self.send_response(302)
            self.send_header("Location", loc)
            for k, val in extra:
                self.send_header(k, val)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/assets/"):
            # 壁纸图片：远程也要能加载（已过密钥门），因此单独一条分支
            qs = urllib.parse.urlparse(self.path)
            file = safe_asset_path(os.path.basename(qs.path))
            if not file or not os.path.isfile(file):
                return self._send(404, {"detail": "没有这张图"})
            try:
                with open(file, "rb") as fh:
                    raw = fh.read(BG_MAX_BYTES + 1)
            except OSError as e:
                return self._send(500, {"detail": f"读图失败：{type(e).__name__}"})
            ctype = sniff_image(raw) or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path in ("/", "/index.html"):
            snap = str((q.get("snapshot") or [""])[0]) in ("1", "true", "yes")
            boot = build_bootstrap(self._is_local()) if snap else None
            return self._send(200, render_page(boot), "text/html; charset=utf-8",
                              extra_headers=(extra if set_cookie else None))
        if path == "/api/whoami":
            return self._send(200, {"local": self._is_local(), "relay": RELAY_BASE,
                                    "writable": self._is_local(),
                                    "key_gate": bool(self.access_key)})
        if path in ("/api/upstream", "/api/hindsight"):
            # 可选集成联动：服务端去摸 integrations.upstream 的 health / 控制面 + 聚合
            # usage.jsonl（调用方别名）。只读；任一上游挂掉只把那一项标成 error，整体仍 200；
            # integrations 未配置时回 configured:false（不摸上游）。
            # /api/hindsight 是 T0.3 之前的旧路径，保留为等价别名（下面 GET_API/LOCAL_GET_API 同理）。
            return self._send(200, build_upstream())
        if path == "/api/callers":
            # 「调用方」tab：中转站 /admin/callers 的只读代理；中转站没配 callers → configured:false
            return self._send(200, build_callers())
        if path == "/api/summary":
            return self._send(200, build_summary())
        if path == "/api/caps":
            return self._send(200, build_caps())
        if path == "/api/ui":
            bg = load_ui()
            return self._send(200, {
                "ui": {"background": bg},
                "image_url": (f"/assets/{bg['file']}?v={bg_cache_token()}" if bg["file"] else ""),
                "writable": self._is_local(),
                "max_bytes": BG_MAX_BYTES,
                "types": sorted(BG_TYPES),
            })
        name = path[len("/api/"):]
        if name in GET_API:
            up, allowed = GET_API[name]
            params = {k: (q.get(k) or [""])[0] for k in allowed} if allowed else None
            code, body = upstream("GET", up, params)
            if not code or code <= 0:
                # 中转站正在重启/连不上：给个合法状态码（0 不是合法 HTTP 状态，浏览器会当成协议错误）
                return self._send(502, {"detail": "连不上中转站（可能正在重启）",
                                        "upstream": body})
            return self._send(code, body)
        return self._send(404, {"detail": "Not Found"})

    # -------------------------------------------------------------- POST
    def do_POST(self) -> None:
        path, q = self._parse()
        if not self._is_local():
            # 电话/局域网只读：写操作一律拒绝（§1 读写分流）。
            # 这里先于密钥门返回 403：远程写是「规则上不允许」，不是「你没带钥匙」。
            if not self.allow_remote_write:
                return self._send(403, {"detail": "写操作仅限本机（loopback）：远程只能只读查看。"
                                                  "要改配置请在 Mac 上打开面板。"})
            ok, _ = self._check_key(q)
            if not ok:
                return self._send(401, {"detail": "需要访问密钥：请在网址后加 ?k=密钥"})
        name = path[len("/api/"):] if path.startswith("/api/") else ""
        if name == "ui":
            payload = self._read_json()
            patch = payload.get("background") if isinstance(payload.get("background"), dict) else payload
            try:
                bg = save_ui(patch if isinstance(patch, dict) else {})
            except OSError as e:
                return self._send(500, {"detail": f"写 ui.json 失败：{type(e).__name__}"})
            return self._send(200, {"ok": True, "ui": {"background": bg}})
        if name == "ui/background":
            payload = self._read_json()
            parsed = parse_data_url(payload.get("data_url"))
            if not parsed:
                return self._send(400, {"detail": "需要 data_url（data:image/...;base64,...）"})
            _declared, raw = parsed
            if not raw:
                return self._send(400, {"detail": "图片内容为空"})
            if len(raw) > BG_MAX_BYTES:
                return self._send(400, {"detail": f"图片太大：{len(raw)} 字节，上限 {BG_MAX_BYTES}"})
            mime = sniff_image(raw)
            if not mime:
                return self._send(400, {"detail": "只接受 PNG / JPEG / WebP / GIF（按文件头判断，不看后缀）"})
            try:
                os.makedirs(assets_dir(), exist_ok=True)
                name_on_disk = "bg" + BG_TYPES[mime]
                tmp = os.path.join(assets_dir(), f".{name_on_disk}.tmp")
                with open(tmp, "wb") as fh:
                    fh.write(raw)
                os.chmod(tmp, 0o600)
                os.replace(tmp, os.path.join(assets_dir(), name_on_disk))
                # 旧格式的图清掉，避免 assets/ 里留一张同名的旧图
                for other in BG_TYPES.values():
                    if other != BG_TYPES[mime]:
                        try:
                            os.unlink(os.path.join(assets_dir(), "bg" + other))
                        except OSError:
                            pass
                bg = save_ui({"file": name_on_disk, "enabled": True})
            except OSError as e:
                return self._send(500, {"detail": f"保存图片失败：{type(e).__name__}"})
            return self._send(200, {"ok": True, "file": name_on_disk, "bytes": len(raw), "mime": mime,
                                    "ui": {"background": bg}})
        if name == "ui/background/clear":
            # 不清除磁盘上的图片文件（那属于不可逆操作），只是把「启用哪张」置空
            try:
                bg = save_ui({"file": "", "enabled": False})
            except OSError as e:
                return self._send(500, {"detail": f"写 ui.json 失败：{type(e).__name__}"})
            return self._send(200, {"ok": True, "ui": {"background": bg}})
        if name in LOCAL_GET_API:
            # 只读接口：GET 可以，POST 一律 405（明确「这里没有写能力」，不落到 404）
            return self._send(405, {"detail": "只读接口：仅支持 GET"})
        if name not in POST_API:
            return self._send(404, {"detail": "Not Found"})
        payload = self._read_json()
        code, body = upstream("POST", POST_API[name], None, payload, timeout=200)
        # 400/401/403/404 一律按错误透传（§4.1 的坑：别把后端错误显示成「已入队」）
        return self._send(code, body)


def main() -> int:
    ap = argparse.ArgumentParser(description="llm-relay 管理前端（单文件、纯标准库）")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0，手机/局域网可看）")
    ap.add_argument("--port", type=int, default=9111)
    ap.add_argument("--key-file", default=DEFAULT_KEY_FILE, help="访问密钥文件（不存在则生成，600）")
    ap.add_argument("--relay", default=None, help="中转站地址，默认 http://127.0.0.1:9110")
    ap.add_argument("--allow-remote-write", action="store_true",
                    help="放开远程写操作（默认远程只读；不建议，写操作会改中转站配置）")
    args = ap.parse_args()

    global RELAY_BASE
    if args.relay:
        RELAY_BASE = args.relay.rstrip("/")
    key_path = resolve_key_path(args.key_file)
    # 外观（ui.json / assets/）跟着密钥文件走：换个 --key-file 就换一整套面板状态。
    # 这样自检脚本把密钥文件指向临时目录时，壁纸文件也会落在临时目录，不会污染仓库。
    globals()["UI_DIR"] = os.path.dirname(key_path)
    Handler.access_key = load_or_create_access_key(key_path).strip()
    Handler.allow_remote_write = bool(args.allow_remote_write)
    for var in ("NO_PROXY", "no_proxy"):
        cur = os.environ.get(var, "")
        if "127.0.0.1" not in cur:
            os.environ[var] = (cur + ",localhost,127.0.0.1,::1").strip(",")
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[llm-relay-dashboard] http://{args.host}:{args.port} → 中转站 {RELAY_BASE} ｜ "
          f"访问密钥门 {'开' if Handler.access_key else '关'}"
          f"（长度 {len(Handler.access_key)}，sha256[:8]={key_fp(Handler.access_key) if Handler.access_key else '-'}）｜ "
          f"密钥文件 {key_path} ｜ "
          f"远程写 {'允许' if args.allow_remote_write else '只读（POST → 403）'}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
