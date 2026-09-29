# -*- coding: utf-8 -*-
"""Kindle 看板 2.0 · 公司机全链路一键脚本

    采集本机 → 采集远端(VPS+NewAPI) → 合并 → 渲染 → 推送圣何塞 RN

用法：
    python run_board.py            # 全链路
    python run_board.py --no-push  # 只到渲染，不推 RN

日志：<DATA_DIR>/run_board.log（追加）
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

import datetime
import glob
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BOARD_DIR = os.path.dirname(HERE)
# 渲染器候选序：取第一个能真实打开的版本（防网盘同步占位导致打开失败）。
# 版式：单站流量环 + 今日 iCloud 日程 + 天气 + 每小时金句。
# 日程来源：iCloud CalDAV 拉取当日剩余事件（icloud_schedule.py，非致命步骤）。
#            



# ⚠️ 渲染器文件走网盘同步时可能晚几分钟才可读（云占位/未水合），
#    曾出现 render rc=2「can't open v13: Errno 22」导致整轮中止（2026-09-24 21:30 实测）。
#    兜底：按候选序取第一个能真实打开的版本，绝不让看板停摆。
_RENDERER_CANDIDATES = ["board_renderer.py"]
_RENDERER_NOTE = ""


def _pick_renderer():
    global _RENDERER_NOTE
    for n in _RENDERER_CANDIDATES:
        p = os.path.join(BOARD_DIR, n)
        try:
            with open(p, "rb") as f:   # 读 64 字节做水合探针，顺带验证可读
                f.read(64)
            _RENDERER_NOTE = n
            return p
        except OSError as e:
            _RENDERER_NOTE = "%s 不可读(%s)" % (n, e)
    return os.path.join(BOARD_DIR, _RENDERER_CANDIDATES[0])


RENDERER = _pick_renderer()
#   v12 = v11 去掉标语徽标（DOSE/dose_slogan/badge_right + 图标字体机制）；
#   起因：右上角 y=40 落在 Kindle 系统状态栏 45px 危险区内，会被飞行模式/电量图标遮住。
#   验证：v12 与 v11 的像素差异 bbox = (462,40,577,71)，恰为标语矩形，其余逐像素一致。
#   回退：把本行改回 v11 即可（v10/v11 原文件均保留）。
#   ⚠️ 历史脉络：v11 = v10 + 超采样 12×（修小字"漏笔画/一行不齐"）；
#      v10 = 版式定稿（R2 合计甜甜圈）。倍率默认 12（BOARD_SS 可覆盖）。
HOME = os.path.expanduser("~")


def find_onedrive():
    for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        p = os.environ.get(k)
        if p and os.path.isdir(p):
            return p
    for c in [os.path.join(HOME, "OneDrive")] + \
             glob.glob(os.path.join(HOME, "Library", "CloudStorage", "OneDrive*")):
        if os.path.isdir(c):
            return c
    return None


def run(cmd, timeout=600):
    try:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"   # 任务计划程序下 console 是 CP936
        env["PYTHONUTF8"] = "1"
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout,
                           env=env)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT after %ds" % timeout
    except Exception as e:
        return 125, "EXC %s" % e


def main():
    py = sys.executable
    od = find_onedrive()
    if not od:
        print("ERROR: 找不到数据目录")
        return 2
    shared = os.path.join(od, "WorkBuddy", "_共享", "board_data")
    if not os.path.isdir(shared):
        os.makedirs(shared)
    png = os.path.join(shared, "board.png")
    logp = os.path.join(shared, "run_board.log")

    no_push = "--no-push" in sys.argv
    steps = [
        ("collect_local", [py, os.path.join(HERE, "collect_local.py")], True),
        ("collect_remote", [py, os.path.join(HERE, "collect_remote.py")], False),
        ("merge", [py, os.path.join(HERE, "merge.py")], True),
        ("render", [py, RENDERER, "--out", png], True),
    ]
    # iCloud 今日日程（v14 消费 schedule.txt；venv python 才有 caldav；失败不致命）
    _venv_py = os.path.join(HOME, ".workbuddy", "binaries", "python", "envs",
                            "default", "Scripts", "python.exe")
    if os.path.exists(_venv_py):
        steps.insert(3, ("icloud_schedule", [_venv_py, os.path.join(HERE, "icloud_schedule.py")], False))
    if not no_push:
        steps.append(("push_rn", [py, os.path.join(HERE, "push_to_rn.py"), png], False))

    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out = []
    out.append("")
    out.append("===== run_board %s =====" % stamp)
    out.append("[INFO] renderer=%s" % _RENDERER_NOTE)
    fatal_fail = False
    for name, cmd, fatal in steps:
        rc, txt = run(cmd)
        tail = [x for x in txt.strip().splitlines() if x.strip()][-3:]
        out.append("[%s] %s rc=%d" % ("OK  " if rc == 0 else "FAIL", name, rc))
        for x in tail:
            out.append("       | " + x[:160])
        if rc != 0 and fatal:
            fatal_fail = True
            out.append("       ⚠️ 致命步骤失败，中止")
            break

    if not fatal_fail and os.path.exists(png):
        out.append("[INFO] PNG %d 字节 %s" % (os.path.getsize(png), png))
    out.append("===== end =====")

    text = "\n".join(out) + "\n"
    with io.open(logp, "a", encoding="utf-8") as f:
        f.write(text)
    print(text)
    return 1 if fatal_fail else 0


if __name__ == "__main__":
    sys.exit(main())
