#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开树「个人痕迹 / 明文密钥」守卫（纯标准库；将来 CI 直接 `python3 scripts/sanitize_check.py`）。

扫描集（= 会进公开树的文件）：
  · ``**/*.py`` / ``*.plist`` / ``*.sh``        —— 全文
  · ``*.md``（除历史任务书 ``TASK*.md`` 与 ``验证结果*.md``）
  · ``*.json``（除本机真实配置 ``config.json`` / ``keys.env``）
  · ``docs/`` 下的图片                          —— 二进制：只把**文件名**当文本查，不读内容

命中就打印 ``文件:行: 命中规则`` 并 exit 1；干净 exit 0。
``--verbose`` 会把**每一个被跳过的文件与原因、每一行被白名单放行的原因**都打出来
（豁免必须显式，绝不允许悄悄跳过）。

用法：
    python3 scripts/sanitize_check.py            # CI 用：只看命中
    python3 scripts/sanitize_check.py --verbose  # 连豁免清单一起看
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SELF_REL = "scripts/sanitize_check.py"

# ---------------------------------------------------------------- 扫描集
TASK_BOOK_RE = re.compile(r"^TASK.*\.md$")          # TASK-P0.md / TASK.md 等历史任务书
VERIFY_RE = re.compile(r"^验证结果.*\.md$")
JSON_EXEMPT = {"config.json", "keys.env"}           # 本机真实文件，不进公开树
TEXT_SUFFIXES = {".py", ".md", ".json", ".plist", ".sh"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".bmp", ".svg"}

# ---------------------------------------------------------------- 规则
# 注：/Users/ 与 gerate 用字符串拼接，避免扫描器源码里出现规则样例（它同时被显式豁免，双保险）。
RULES: list[tuple[re.Pattern, str]] = [
    (re.compile("/" + "Users/[A-Za-z0-9._-]+"), "个人绝对路径 /Users/<name>"),
    (re.compile(r"C:" + r"\\Users", re.I), "个人绝对路径 C:\\Users"),
    (re.compile(r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "私网 IP 10.0.0.0/8"),
    (re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"), "私网 IP 192.168.0.0/16"),
    (re.compile(r"\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"), "Tailscale IP 100.64.0.0/10"),
    (re.compile("千" + "咲" + "酱"), "个人标识（bank 名）"),
    (re.compile(r"sk-[A-Za-z0-9]"), "疑似明文 key（sk-）"),
    (re.compile(r"nvapi-"), "疑似明文 key（nvapi-）"),
]
NAME_RE = re.compile("gera" + "te|GerateGuo", re.I)
# 公开仓库地址/署名白名单：同一行里 `gerate` 紧跟在公开代码托管域名后面就放行。
# 徽章同理：img.shields.io 的徽章 URL 必须内嵌 owner/repo（`img.shields.io/<平台>/.../<owner>`），
# 那是指向**公开仓**的地址，不是个人痕迹 —— 不白名单掉它就只能砍掉徽章。
NAME_OK_RE = re.compile(
    r"(github\.com|gitlab\.com|gitee\.com|bitbucket\.org)/" + "gera" + "te"
    + r"|img\.shields\.io/[^\s)\"']*" + "gera" + "te", re.I)
SECRET_RULE_PREFIX = "疑似明文 key"
# 假密钥/脱敏自检的白名单构造（每条都会在 --verbose 里打印原因）
ALLOW_MARKERS = ("FAKE", "assertNotIn", "re.findall", ".count(", "_KEY_PAT")

SKIPPED: list[str] = []       # 被跳过的文件与原因
ALLOWED: list[str] = []       # 被放行的行与原因


def classify(rel: str) -> tuple[str, str]:
    """返回 (处理方式, 原因)：'scan-text' / 'scan-name' / 'skip'。"""
    path = Path(rel)
    name, suffix = path.name, path.suffix.lower()
    if rel == SELF_REL:
        return "skip", "扫描器自身：正则/文案里必然含规则样例"
    if TASK_BOOK_RE.match(name):
        return "skip", "历史任务书 TASK*.md（公开前需人工复核；本守卫不代替人工）"
    if VERIFY_RE.match(name):
        return "skip", "验证结果*.md（历史存档）"
    if suffix in IMAGE_SUFFIXES:
        if path.parts and path.parts[0] == "docs":
            return "scan-name", "docs/ 图片：只查文件名，不读二进制内容"
        return "skip", "图片且不在 docs/ 下"
    if suffix in (".py", ".plist", ".sh", ".md"):
        return "scan-text", ""
    if suffix == ".json":
        if name in JSON_EXEMPT:
            return "skip", f"{name} 是本机真实配置（不进公开树）"
        return "scan-text", ""
    return "skip", f"不在扫描集（后缀 {suffix or '无后缀'}）"


def secret_allowed(line: str) -> str | None:
    """疑似密钥行的白名单：返回放行原因；None = 不放行。"""
    if "FAKE" in line:
        return "含 FAKE 标记（测试用假密钥）"
    if all(p in line for p in ("sk-", "nvapi-")):
        return "同一行同时出现 sk- 与 nvapi-（脱敏/自检代码）"
    for marker in ALLOW_MARKERS[1:]:
        if marker in line:
            return f"含脱敏自检构造 {marker!r}"
    return None


def scan_text(rel: str) -> list[str]:
    hits: list[str] = []
    try:
        lines = (ROOT / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [f"{rel}:0: 读不了文件（{type(exc).__name__}）"]
    for lineno, line in enumerate(lines, 1):
        flagged = False
        for rx, label in RULES:
            if not rx.search(line):
                continue
            if label.startswith(SECRET_RULE_PREFIX):
                why = secret_allowed(line)
                if why:
                    ALLOWED.append(f"{rel}:{lineno}: {label} —— 放行：{why}")
                    continue
            hits.append(f"{rel}:{lineno}: {label}")
            flagged = True
        if flagged:
            continue          # 该行已被规则命中，不必再报「个人用户名」重复一条
        if NAME_RE.search(line):
            if NAME_OK_RE.search(line):
                ALLOWED.append(f"{rel}:{lineno}: 个人用户名 —— 放行：公开仓库地址/署名")
            else:
                hits.append(f"{rel}:{lineno}: 个人用户名 gerate/GerateGuo")
    return hits


def scan_name(rel: str) -> list[str]:
    for rx, label in RULES + [(NAME_RE, "个人用户名 gerate/GerateGuo")]:
        if rx.search(rel):
            return [f"{rel}:0: 文件名命中：{label}"]
    return []


def candidates() -> tuple[list[str], str]:
    """列出候选文件：优先 git 追踪列表（= 会进公开树的文件），git 不可用则遍历目录。"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"],
                             capture_output=True, text=True, check=True).stdout
        files = [p for p in out.split("\0") if p]
        if files:
            return files, "git ls-files（只查会被提交/进公开树的文件）"
    except Exception as exc:  # noqa: BLE001
        note = f"git 不可用（{type(exc).__name__}），回退到目录遍历"
    else:
        note = "git 仓库为空，回退到目录遍历"
    files = []
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and ".git/" not in p.as_posix() and "__pycache__" not in p.parts:
            files.append(p.relative_to(ROOT).as_posix())
    return files, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="公开树个人痕迹 / 明文密钥守卫")
    ap.add_argument("--verbose", action="store_true", help="打印被跳过的文件与放行原因（豁免必须显式）")
    args = ap.parse_args(argv)

    files, source = candidates()
    hits: list[str] = []
    for rel in files:
        how, why = classify(rel)
        if how == "skip":
            SKIPPED.append(f"{rel}: 跳过 —— {why}")
        elif how == "scan-name":
            SKIPPED.append(f"{rel}: 只查文件名 —— {why}")
            hits += scan_name(rel)
        else:
            hits += scan_text(rel)

    if args.verbose:
        print(f"[sanitize_check] 仓库根目录：{ROOT}")
        print(f"[sanitize_check] 候选来源：{source}（共 {len(files)} 个文件）")
        print(f"[sanitize_check] 跳过/只查文件名（{len(SKIPPED)}）——豁免是显式的：")
        for line in SKIPPED:
            print("  · " + line)
        print(f"[sanitize_check] 白名单放行（{len(ALLOWED)}）——每条都给了原因：")
        for line in ALLOWED:
            print("  · " + line)

    if hits:
        print(f"[sanitize_check] 命中 {len(hits)} 处，公开前必须清理：")
        for line in hits:
            print("  " + line)
        return 1
    print(f"[sanitize_check] OK：{len(files)} 个候选文件里没有个人痕迹 / 明文密钥"
          f"（显式豁免 {len(SKIPPED)} 个文件、放行 {len(ALLOWED)} 行；--verbose 看清单）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
