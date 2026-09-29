#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kindle 看板 2.0 · 远端数据采集（只在公司机跑）

做两件事：
  1) ssh 到达拉斯 hub 执行 remote_probe.py，取回 VPS 流量 + New API 用量
  2) 结果写进 <DATA_DIR>/remote.json

用法：
    python collect_remote.py
    python collect_remote.py --host root@1.2.3.4 --timeout 20
"""

# --- UTF-8 stdout guard (2026-09-23) ---------------------------------------
# 任务计划程序的 console 是 CP936，输出 '¥'(U+00A5) 会抛 UnicodeEncodeError。
# 自己把 stdout/stderr 强制成 UTF-8，不依赖调用方设环境变量。
import sys as _sys
for _s in ("stdout", "stderr"):
    try:
        getattr(_sys, _s).reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
# ---------------------------------------------------------------------------


import argparse
import datetime
import glob
import json
import os
import subprocess
import sys

BJ = datetime.timezone(datetime.timedelta(hours=8))
HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
PROBE = os.path.join(HERE, "remote_probe.py")

DEFAULT_HOST = os.environ.get("BOARD_REMOTE_HOST", "user@your-server-ip")


def find_onedrive():
    for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        p = os.environ.get(k)
        if p and os.path.isdir(p):
            return p
    cands = [os.path.join(HOME, "OneDrive")]
    cands += glob.glob(os.path.join(HOME, "Library", "CloudStorage", "OneDrive*"))
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--timeout", type=int, default=25)
    args = ap.parse_args()

    if not os.path.exists(PROBE):
        print("ERROR: 找不到 %s" % PROBE, file=sys.stderr)
        return 2
    with open(PROBE, "rb") as f:
        script = f.read()

    cmd = ["ssh", "-o", "ConnectTimeout=%d" % args.timeout,
           "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
           args.host, "python3 -"]
    try:
        p = subprocess.run(cmd, input=script, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=args.timeout + 20)
    except subprocess.TimeoutExpired:
        print("ERROR: ssh 超时（%ss）" % (args.timeout + 20), file=sys.stderr)
        return 3

    raw = p.stdout.decode("utf-8", "replace").strip()
    if p.returncode != 0 or not raw:
        print("ERROR: 远端执行失败 rc=%s\n%s" % (p.returncode, p.stderr.decode("utf-8", "replace")[:800]),
              file=sys.stderr)
        return 4
    try:
        data = json.loads(raw)
    except Exception as e:
        print("ERROR: 远端返回的不是 JSON: %s\n前 400 字: %s" % (e, raw[:400]), file=sys.stderr)
        return 5

    data["collected_at"] = datetime.datetime.now(BJ).isoformat(timespec="seconds")
    data["source_host"] = args.host

    od = find_onedrive()
    if not od:
        print("ERROR: 找不到数据目录", file=sys.stderr)
        return 6
    outdir = os.path.join(od, "WorkBuddy", "_共享", "board_data")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "remote.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print("远端采集完成 -> %s" % path)
    v = data.get("vps") or {}
    print("  VPS snapshot 生成于: %s" % v.get("generated_at"))
    for s in (v.get("sites") or []):
        used = s.get("used_bytes")
        quota = s.get("quota_bytes") or 0
        print("    %-22s %-9s %s / %s  (%s%%)  重置 %s天"
              % (s.get("name"), s.get("state"),
                 ("%.2f GB" % (used / 1e9)) if used else (s.get("last_known", {}).get("used_txt") or "—"),
                 s.get("quota_label"),
                 s.get("pct") if s.get("pct") is not None else "—",
                 s.get("days_to_reset")))
    na = data.get("newapi") or {}
    print("  New API: 今日 %s 次 / %s token / quota %s（库内共 %s 条，最后 %s）"
          % (na.get("today_calls"), na.get("today_tokens"), na.get("today_quota"),
             na.get("logs_total"), na.get("last_log_at")))
    if data.get("errors"):
        print("  警告: %s" % "; ".join(data["errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
