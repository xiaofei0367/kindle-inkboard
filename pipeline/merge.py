#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kindle 看板 2.0 · 合并（只在公司机跑）

输入：<DATA_DIR>/ 下的
        local_<host>.json   （三机各自 collect_local.py 的产物）
        remote.json         （collect_remote.py 的产物）
输出：同目录 board_latest.json —— 渲染器唯一需要的文件

用法：
    python merge.py
    python merge.py --days 7
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
import sys

BJ = datetime.timezone(datetime.timedelta(hours=8))
HOME = os.path.expanduser("~")


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


def load_json(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def sum_opt(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def parse_size(txt):
    """把 "560.39 GB" / "2.93 TB" 解析成字节数（十进制单位）。"""
    import re
    m = re.search(r"([\d.]+)\s*(TB|GB|MB|KB)?", str(txt or ""), re.I)
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    unit = (m.group(2) or "GB").upper()
    return v * {"KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12}[unit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="时间轴长度（含今天）")
    args = ap.parse_args()

    od = find_onedrive()
    if not od:
        print("ERROR: 找不到数据目录", file=sys.stderr)
        return 2
    data_dir = os.path.join(od, "WorkBuddy", "_共享", "board_data")

    locals_ = []
    for p in sorted(glob.glob(os.path.join(data_dir, "local_*.json"))):
        d = load_json(p)
        if d:
            locals_.append(d)
    remote = load_json(os.path.join(data_dir, "remote.json")) or {}

    now = datetime.datetime.now(BJ)
    today = now.date()

    # ---------------- 合并三机 token/积分 ----------------
    hosts = []
    for d in locals_:
        t = d.get("today") or {}
        hosts.append({
            "host": d.get("host"),
            "hostname": d.get("hostname"),
            "generated_at": d.get("generated_at"),
            "today_date": t.get("date"),
            # 该机这条 today 是不是"看板当天"报的？不是＝今日未报数（多半没开机）
            "reported_today": (str(today) == (t.get("date") or "")),
            "credit": t.get("credit"),
            "sessions": t.get("sessions"),
            "tokens_total": t.get("tokens_total"),
            "cache_hit_rate": t.get("cache_hit_rate"),
            "balance_cny": (d.get("balance") or {}).get("deepseek_cny"),
        })
    hosts.sort(key=lambda h: -(h.get("credit") or 0))

    # 日轴：以「今天回溯 N 天」重建，保证三机缺数据的日期补 0 而不是错位
    day_map = {}
    for d in locals_:
        for row in (d.get("days") or []):
            k = row.get("date")
            if not k:
                continue
            m = day_map.setdefault(k, {
                "date": k, "credit": 0.0, "sessions": 0, "tokens_total": 0,
                "tokens_uncached": 0, "tokens_cached": 0, "tokens_out": 0,
                "calls": 0, "cost_cny": 0.0, "hosts_present": 0,
                "cost_parts": {"hit": 0.0, "unc": 0.0, "out": 0.0},
            })
            m["credit"] += row.get("credit") or 0.0
            m["sessions"] += row.get("sessions") or 0
            m["tokens_total"] += row.get("tokens_total") or 0
            m["tokens_uncached"] += row.get("tokens_uncached") or 0
            m["tokens_cached"] += row.get("tokens_cached") or 0
            m["tokens_out"] += row.get("tokens_out") or 0
            m["calls"] += row.get("calls") or 0
            m["cost_cny"] += row.get("cost_cny_est") or 0.0
            for kk, vv in (row.get("cost_parts") or {}).items():
                if kk in m["cost_parts"]:
                    m["cost_parts"][kk] += vv or 0.0
            m["hosts_present"] += 1

    days = []
    for i in range(args.days - 1, -1, -1):
        k = (today - datetime.timedelta(days=i)).isoformat()
        m = day_map.get(k) or {
            "date": k, "credit": 0.0, "sessions": 0, "tokens_total": 0,
            "tokens_uncached": 0, "tokens_cached": 0, "tokens_out": 0,
            "calls": 0, "cost_cny": 0.0, "hosts_present": 0,
            "cost_parts": {"hit": 0.0, "unc": 0.0, "out": 0.0},
        }
        m["credit"] = round(m["credit"], 2)
        m["cost_cny"] = round(m.get("cost_cny") or 0.0, 3)
        m["cost_parts"] = {k2: round(v2 or 0.0, 4) for k2, v2 in (m.get("cost_parts") or {}).items()}
        inx = m["tokens_uncached"] + m["tokens_cached"]
        m["cache_hit_rate"] = (round(m["tokens_cached"] / inx, 4) if inx else None)
        # split_known=False 表示该日拿不到 token 明细（traces.modelInfo 缺失）
        # → 柱状图不能做成本分段，渲染器改画斜纹，不得凭猜填充。
        m["split_known"] = bool(inx > 0 and sum(m["cost_parts"].values()) > 0)
        days.append(m)

    t_today = days[-1]
    balance = None
    for h in hosts:
        if h.get("balance_cny") is not None:
            balance = h["balance_cny"]
            break

    # 积分余额：WorkBuddy 平台侧没有本地接口，只能由人工填 <data_dir>/manual.json
    # {"credit_balance": 1234.5, "credit_balance_at": "2026-09-23T11:00"}
    # 超过 7 天视为过期（旧数字比没数字更危险），渲染器会显示 "—"。
    manual = load_json(os.path.join(data_dir, "manual.json")) or {}
    credit_balance, credit_balance_at, credit_balance_stale = None, None, True
    cb = manual.get("credit_balance")
    if cb is not None:
        credit_balance = cb
        credit_balance_at = manual.get("credit_balance_at")
        try:
            at = datetime.datetime.fromisoformat(credit_balance_at).date()
            credit_balance_stale = (today - at).days > 7
        except Exception:
            credit_balance_stale = True

    # ---------------- 三机占比（R3） ----------------
    # 未接入的机器也要出现在名单里，否则屏上"只有一台"看不出是缺数据还是真只有一台。
    ROSTER = [("company", "公司机"), ("xiaoxin", "小新"), ("home", "Mac")]
    by_host = {h.get("host"): h for h in hosts}
    # 今日用量口径：只接纳"今天报过数"的机器（host.today_date == 看板日期）。
    # 没开机/没采集的机器，今天真实用量 = 0；其最后已知 credit 只作"最后已知"标注，
    # 绝不混入今日占比，也绝不被渲染器当当前值摆出来。
    def _today_vals(h):
        rep = bool(h.get("reported_today"))
        return ((h.get("credit") or 0.0) if rep else 0.0,
                (h.get("tokens_total") or 0.0) if rep else 0.0,
                rep)
    tot_credit = 0.0
    tot_tokens = 0.0
    for h in hosts:
        tc, tt, _ = _today_vals(h)
        tot_credit += tc
        tot_tokens += tt
    # 份额口径：优先积分；今天全是 0 分（例如整天走自定义模型）时退化为 token
    basis = "credit" if tot_credit > 0 else "tokens"
    denom = tot_credit if basis == "credit" else tot_tokens
    machines = []
    for hid, label in ROSTER:
        h = by_host.get(hid)
        if not h:
            machines.append({"host": hid, "label": label, "present": False})
            continue
        tc, tt, rep = _today_vals(h)
        val = tc if basis == "credit" else tt
        machines.append({
            "host": hid, "label": label, "present": True,
            "hostname": h.get("hostname"),
            "reported_today": rep,
            "today_credit": tc,
            "today_tokens": tt,
            "last_known_credit": h.get("credit"),
            "last_known_at": h.get("generated_at"),
            # 兼容旧读者：credit/tokens_total 也给出"今日"值（诚实的当前值）
            "credit": tc,
            "tokens_total": tt,
            "sessions": h.get("sessions"),
            "share": (val / denom) if denom else 0.0,
            "generated_at": h.get("generated_at"),
        })
    # 名单外的机器（改过 host 名）也不能丢
    for h in hosts:
        if h.get("host") not in [r[0] for r in ROSTER]:
            tc, tt, rep = _today_vals(h)
            val = tc if basis == "credit" else tt
            machines.append({
                "host": h.get("host"), "label": h.get("host"), "present": True,
                "hostname": h.get("hostname"), "reported_today": rep,
                "today_credit": tc, "today_tokens": tt,
                "last_known_credit": h.get("credit"),
                "last_known_at": h.get("generated_at"),
                "credit": tc, "tokens_total": tt, "sessions": h.get("sessions"),
                "share": (val / denom) if denom else 0.0,
                "generated_at": h.get("generated_at"),
            })

    token_sec = {
        "today": {
            "date": t_today["date"],
            "credit": t_today["credit"],
            "sessions": t_today["sessions"],
            "tokens_total": t_today["tokens_total"],
            "tokens_uncached": t_today["tokens_uncached"],
            "tokens_cached": t_today["tokens_cached"],
            "tokens_out": t_today["tokens_out"],
            "cache_hit_rate": t_today["cache_hit_rate"],
            "calls": t_today["calls"],
            "cost_cny": t_today["cost_cny"],
            "balance_cny": balance,
            "credit_balance": credit_balance,
            "credit_balance_at": credit_balance_at,
            "credit_balance_stale": credit_balance_stale,
        },
        "days": days,
        "hosts": hosts,
        "machines": machines,
        "share_basis": basis,
        "host_count": len(hosts),
        "caveat": ("token 明细来自 traces.modelInfo，历史覆盖率约 60%（部分会话未写入该字段）；"
                   "积分（credit）来自 workbuddy.db，完整可靠，故柱状图以积分为准。"
                   "cost_cny 是按 DeepSeek 官方价目对 token 的估算，非实际扣费。"),
    }

    # ---------------- VPS ----------------
    vps = remote.get("vps") or {}
    sites = []
    for s in (vps.get("sites") or []):
        pct = s.get("pct")
        lk = s.get("last_known") or {}
        # 停机站点没有实时值：用「末次已知用量 ÷ 额度」补算百分比，否则屏上只剩缺省值
        if pct is None and lk.get("used_txt") and s.get("quota_bytes"):
            ub = parse_size(lk["used_txt"])
            if ub:
                pct = round(ub / s["quota_bytes"] * 100, 2)
        sites.append({
            "id": s.get("id"),
            "name": s.get("name"),
            "short": (s.get("name") or "").split(" · ")[0],
            "provider": s.get("provider"),
            "quota_bytes": s.get("quota_bytes"),
            "quota_label": s.get("quota_label"),
            "reset_label": s.get("reset_label"),
            "state": s.get("state"),
            "used_bytes": s.get("used_bytes"),
            "pct": pct,                                 # 百分数（0.68 = 0.68%）
            "pct_estimated": s.get("pct") is None and pct is not None,
            "level": s.get("level"),
            "days_to_reset": s.get("days_to_reset"),
            "last_known": lk,
        })

    out = {
        "schema": 1,
        "generated_at": now.isoformat(timespec="seconds"),
        "stale_after_minutes": 90,
        "token": token_sec,
        "vps": {
            "source_generated_at": vps.get("generated_at"),
            "collected_at": remote.get("collected_at"),
            "sites": sites,
        },
        "newapi": remote.get("newapi") or {},
        "errors": (remote.get("errors") or []),
        "sources": {
            "locals": [d.get("host") for d in locals_],
            "remote": remote.get("source_host"),
        },
    }

    path = os.path.join(data_dir, "board_latest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("合并完成 -> %s" % path)
    print("  来源: locals=%s  remote=%s" % (out["sources"]["locals"], out["sources"]["remote"]))
    print("  今日: 积分 %.2f | 会话 %d | token %s | 命中 %s | 估算 ¥%.2f | 余额 %s"
          % (t_today["credit"], t_today["sessions"], format(t_today["tokens_total"], ","),
             ("%.1f%%" % (t_today["cache_hit_rate"] * 100)) if t_today["cache_hit_rate"] is not None else "-",
             t_today["cost_cny"], ("¥%.2f" % balance) if balance is not None else "—"))
    print("  积分余额: %s" % (("%.2f 分 (%s)" % (credit_balance, credit_balance_at))
                             if credit_balance is not None and not credit_balance_stale
                             else "无（等 manual.json 或平台接口）"))
    print("  三机占比（按%s）: %s" % (basis, " ".join(
        "%s %s" % (m["label"], ("%.0f%%" % (m["share"] * 100)) if m["present"] else "未接入") for m in machines)))
    print("  7 日积分: %s" % "  ".join("%.0f" % d["credit"] for d in days))
    for s in sites:
        print("  VPS %-20s %s%%  %s" % (s["name"], s["pct"] if s["pct"] is not None else "—", s["state"]))
    if out["errors"]:
        print("  警告: %s" % "; ".join(out["errors"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
