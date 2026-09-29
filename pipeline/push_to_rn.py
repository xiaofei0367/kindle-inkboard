# -*- coding: utf-8 -*-
"""把渲染好的看板 PNG 推送到 Web 服务器的静态目录（任意静态托管均可）。

凭证不进同步盘：读 ~/.board_push_creds.json。
（若该文件不存在 → 报错退出，绝不内置密码。）

用法：
    python push_to_rn.py                        # 默认推 _共享/board_data/board.png
    python push_to_rn.py <local.png>            # 指定文件
    python push_to_rn.py <local.png> board.png  # 指定远端文件名
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

import io
import json
import os
import sys

try:
    import paramiko
except ImportError:
    print("ERROR: 需要 paramiko：pip install paramiko")
    sys.exit(2)

CREDS = os.path.join(os.path.expanduser("~"), ".workbuddy", "board_push_creds.json")
HOME = os.path.expanduser("~")


def find_onedrive():
    import glob
    for k in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        p = os.environ.get(k)
        if p and os.path.isdir(p):
            return p
    for c in [os.path.join(HOME, "OneDrive")] + \
             glob.glob(os.path.join(HOME, "Library", "CloudStorage", "OneDrive*")):
        if os.path.isdir(c):
            return c
    return None


def main():
    if not os.path.exists(CREDS):
        print("ERROR: 凭证文件不存在: %s" % CREDS, file=sys.stderr)
        print("       期望格式：{\"host\":..,\"user\":..,\"password\":..,\"web_root\":..}", file=sys.stderr)
        return 3
    with io.open(CREDS, encoding="utf-8") as f:
        c = json.load(f)

    local = sys.argv[1] if len(sys.argv) > 1 else None
    if not local:
        od = find_onedrive()
        if not od:
            print("ERROR: 找不到数据目录，请显式传本地 PNG 路径", file=sys.stderr)
            return 4
        local = os.path.join(od, "WorkBuddy", "_共享", "board_data", "board.png")
    if not os.path.exists(local):
        print("ERROR: 本地文件不存在: %s" % local, file=sys.stderr)
        return 5

    remote_name = sys.argv[2] if len(sys.argv) > 2 else "board.png"
    remote = c["web_root"].rstrip("/") + "/" + remote_name
    size = os.path.getsize(local)

    t = paramiko.Transport((c["host"], 22))
    try:
        t.connect(username=c["user"], password=c["password"])
        sftp = paramiko.SFTPClient.from_transport(t)
        sftp.put(local, remote)
        sftp.chmod(remote, 0o644)
        sftp.close()
        print("PUSHED %s (%d bytes) -> %s:%s" % (local, size, c["host"], remote))
        print("URL    %s" % c.get("public_url", "http://%s/%s" % (c["host"], remote_name)))
        return 0
    finally:
        t.close()


if __name__ == "__main__":
    sys.exit(main())
