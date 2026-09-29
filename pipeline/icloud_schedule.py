#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
icloud_schedule.py — 从 iCloud CalDAV 拉取今日剩余日程 → board_data/schedule.txt
运行: venv python (caldav 3.3.1)  由 run_board.py 渲染前 best-effort 调用
产物: 每行 "HH:MM|事件名"（全天事件为 "全天|事件名"），最多 5 条，按时间排序
新鲜度: schedule.txt < 55 分钟则跳过（渲染周期 15 分钟，一小时拉一次足够）
凭据: %LOCALAPPDATA%\\board_icloud\\creds.env (APPLE_ID= / APP_PW=) —— 本机文件，不进同步盘
协议坑(2026-09-28 实测): principal 必须用 caldav 库（Apple 认其握手）；
  枚举日历必须手写最小属性 PROPFIND（库 children() 被确定性掐断）；
  207 响应 href 是根相对路径，URL = scheme://netloc + href。
"""
import sys, os, datetime, traceback, socket, time
import xml.etree.ElementTree as ET
import requests
import caldav
from zoneinfo import ZoneInfo

socket.setdefaulttimeout(40)
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser(os.path.join("~", "AppData", "Local")))
BASE = os.path.join(LOCALAPPDATA, "board_icloud")
OUT = os.environ.get(
    "BOARD_DATA",
    os.path.expanduser(os.path.join("~", "board_data")))
SCHED = os.path.join(OUT, "schedule.txt")
LOG = os.path.join(BASE, "schedule.log")
TZ = ZoneInfo("Asia/Shanghai")
NS_D = "DAV:"
NS_C = "urn:ietf:params:xml:ns:caldav"
MAX_EVENTS = 5

PROPFIND_BODY = ('<?xml version="1.0"?>'
 '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
 '<d:prop><d:resourcetype/><d:displayname/></d:prop></d:propfind>')


def log(msg):
    os.makedirs(BASE, exist_ok=True)
    lines = []
    if os.path.exists(LOG):
        lines = open(LOG, encoding="utf-8", errors="replace").read().splitlines()[-199:]
    lines.append("%s %s" % (datetime.datetime.now(TZ).strftime("%F %T"), msg))
    open(LOG, "w", encoding="utf-8").write("\n".join(lines) + "\n")


def load_creds():
    d = {}
    p = os.path.join(BASE, "creds.env")
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    return d.get("APPLE_ID", ""), d.get("APP_PW", "")


def atomic(path, text):
    tmp = path + ".tmp"
    open(tmp, "w", encoding="utf-8").write(text)
    os.replace(tmp, path)


def list_calendars(user, pw, home_url):
    """手写最小属性 PROPFIND 枚举日历集合（绕开库 children() 被 Apple 掐）"""
    r = requests.request("PROPFIND", home_url, auth=(user, pw),
                         headers={"Depth": "1", "Content-Type": "application/xml"},
                         data=PROPFIND_BODY, timeout=40)
    if r.status_code not in (207, 200):
        raise RuntimeError("propfind home HTTP %s" % r.status_code)
    cals = []
    root = ET.fromstring(r.content)
    for resp in root.findall("{%s}response" % NS_D):
        rt = resp.find("{%s}propstat/{%s}prop/{%s}resourcetype" % (NS_D, NS_D, NS_D))
        if rt is None or rt.find("{%s}calendar" % NS_C) is None:
            continue
        href = resp.find("{%s}href" % NS_D)
        url = href.text if href is not None else None
        if url and not url.startswith("http"):
            from urllib.parse import urlsplit
            base = "{0.scheme}://{0.netloc}".format(urlsplit(home_url))
            url = (base + url) if url.startswith("/") else (home_url.rstrip("/") + "/" + url)
        name = resp.findtext("{%s}propstat/{%s}prop/{%s}displayname" % (NS_D, NS_D, NS_D)) or "?"
        cals.append((url, name))
    return cals


def fresh_enough():
    try:
        age = time.time() - os.path.getmtime(SCHED)
        return age < 55 * 60
    except OSError:
        return False


def main():
    if fresh_enough():
        log("SKIP: schedule.txt fresh (%.0f min)" % ((time.time() - os.path.getmtime(SCHED)) / 60))
        return 0
    user, pw = load_creds()
    if not user or not pw:
        log("SKIP: creds not set (%s)" % BASE)
        return 0
    now = datetime.datetime.now(TZ)
    tonight = now.replace(hour=23, minute=59, second=59, microsecond=0)

    client = caldav.DAVClient(url="https://caldav.icloud.com/", username=user, password=pw)
    principal = client.principal()
    home_url = str(principal.calendar_home_set.url)
    log("step: home=%s" % home_url)

    cals = None
    for attempt in range(3):
        try:
            cals = list_calendars(user, pw, home_url)
            break
        except Exception as e:
            log("list_calendars attempt %d fail: %r" % (attempt + 1, e))
            time.sleep(4 * (attempt + 1))
    if cals is None:
        log("FAIL: 枚举日历 3 次全灭（保留旧 schedule.txt）")
        return 1
    log("step: %d calendars" % len(cals))

    events = []
    for url, name in cals:
        try:
            cal = caldav.Calendar(client, url=url, parent=principal.calendar_home_set)
            for ev in cal.date_search(start=now, end=tonight, expand=True):
                s = ev.vobject_instance
                if s is None or s.vevent is None:
                    continue
                node = (s.vevent.contents.get("summary") or [None])[0]
                dt_node = (s.vevent.contents.get("dtstart") or [None])[0]
                if node is None or dt_node is None:
                    continue
                summary = str(node.value).strip().replace("\n", " ")
                if not summary:
                    continue
                dt = dt_node.value
                if isinstance(dt, datetime.date) and not isinstance(dt, datetime.datetime):
                    label, sortkey = "全天", datetime.datetime.combine(dt, datetime.time(0, 0), tzinfo=TZ)
                else:
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=TZ)
                    else:
                        dt = dt.astimezone(TZ)
                    if dt < now - datetime.timedelta(minutes=30):   # 刚开场的仍显示
                        continue
                    label, sortkey = dt.strftime("%H:%M"), dt
                events.append((sortkey, label, summary))
        except Exception as e:
            log("events %r fail: %r" % (name, e))

    events.sort(key=lambda x: x[0])
    lines = ["%s|%s" % (label, summ) for _, label, summ in events[:MAX_EVENTS]]
    atomic(SCHED, "\n".join(lines) + ("\n" if lines else ""))
    log("OK: %d events (of %d found)" % (len(lines), len(events)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        log("FAIL:\n" + traceback.format_exc(limit=4))
        sys.exit(1)
