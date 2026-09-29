#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Kindle 看板 2.0 · 本机数据采集（三台机器各自运行这一个脚本）

产出（写进 OneDrive 同步区，各机各写自己的文件，永不冲突）：
    <DATA_DIR>/local_<host>.json

采集三样：
  1) 积分消耗   <- ~/.workbuddy/workbuddy.db  session_usage.credit_json  （权威"花费"口径）
  2) Token 明细 <- ~/.workbuddy/traces/*/trace_*.json  trace.modelInfo   （输入/缓存/输出）
  3) 余额       <- DeepSeek 官方 /user/balance（key 取自 ~/.workbuddy/models.json）

用法：
    python collect_local.py                   # 默认回溯 8 天
    python collect_local.py --days 14
    python collect_local.py --print           # 采集后把结果打到屏幕
    python collect_local.py --rebuild-ledger  # 丢弃积分账本、按历史重建（排障用）

设计说明：
  * 各机互不互通状态目录，所以必须各自导出，靠共享同步盘汇总。
  * token 只有含 generation span 的 trace 才带 modelInfo，其余是空壳，跳过是正确的。

★ 积分归日（2026-09-23 重做）------------------------------------------------
  旧实现按 `session_usage.updated_at` 归日，这个字段只是「会话最后一次被触碰的时间」，
  而 `credit_json` 是「会话终生累计」—— 粒度不匹配。后果：一个 8 月 21 日创建、
  攒了 266 分的老会话，今天只要被碰一下，它一辈子的账就整体搬到今天。
  （实测虚增 29.6 倍，见 _输出/AUDIT_20260923_xiaoxin_credit.md）

  新实现按 **requestId 逐个记账**，归日优先级：
    1) requestId 内嵌时间戳 —— 新版 requestId 是 UUIDv7 布局，前 48 bit 就是
       Unix 毫秒，可直接解出精确到毫秒的发生时间（`rid_born()`）。
    2) audit-log 首次出现日 —— 仅用于账本首次建立时兜底历史柱子。近似源：
       日文件名的日期是「审计事件写入日」而非请求发生日，且只覆盖触发过安全
       审计的请求（实测约 59% 笔数 / 81% 金额）。绝不用它定「今天」以外的精度。
    3) session.updated_at —— 最后的兜底，且**禁止落到今天**（见下）。

  ★ 核心护栏：无据可依的老账（非 v7、audit 未命中、updated_at 落在今天）一律
    记为 undated，**不计入任何一天**。宁可少算，也不让老账污染今天。
    唯一例外：该会话今天存在 v7 请求（说明它今天确实活着）才认。

  账本落本机状态文件（不进同步盘）：
    entries   {requestId: [归日, 已计金额, session8]}   查重 + 补差额
    daily     {日期: 当日积分}                          ★ 权威日账，只增不改
    daily_sids{日期: [session8]}                        当日活跃会话
  一旦入账，"过去的日子"就固化了 —— 老会话再被碰也不会回填到今天。
  首次运行的存量用上面 1~3 逐笔定日；此后新出现的 requestId 才计入当天，
  精度 = 采集频率（本机 2 小时）。
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
import collections
import datetime
import glob
import json
import os
import re
import socket
import sqlite3
import sys
import urllib.request

BJ = datetime.timezone(datetime.timedelta(hours=8))
HOME = os.path.expanduser("~")
WB = os.path.join(HOME, ".workbuddy")
TRACES = os.path.join(WB, "traces")
DB = os.path.join(WB, "workbuddy.db")
MODELS = os.path.join(WB, "models.json")
AUDIT_LOG = os.path.join(WB, "audit-log")

SCHEMA = 1

# 积分账本（本机私有状态，绝不进同步盘）
LEDGER_NAME = "board_credit_ledger.json"
LEDGER_SCHEMA = 1
LEDGER_KEEP_DAYS = 400          # 日账保留期
CREDIT_EPS = 1e-9               # 浮点比较容差
MAX_AUDIT_FILE_MB = 40          # 单日 audit-log 超过这个体积就跳过（主力机保护）

# ---------------------------------------------------------------- 定价
# 2026-09-23 核实自 DeepSeek 官方文档 api-docs.deepseek.com/zh-cn/quick_start/pricing/
# 单位：元 / 百万 token，(缓存命中, 缓存未命中, 输出)
# 高峰 = 北京时间 周一至周五 9:00-12:00 / 14:00-18:00（不含法定节假日）
PRICING_CNY = {
    "deepseek-flash":  {"peak": (0.04, 2.0, 8.0),  "off": (0.02, 1.0, 4.0)},
    "deepseek-v4-pro": {"peak": (0.30, 9.0, 27.0), "off": (0.15, 4.5, 13.5)},
}

# 模型名别名 -> 定价键。官方说明：旧名 deepseek-v4-flash / -vision-exp 已下线，
# 请求由 V4.1-Flash 提供服务并按 Flash 价格计费。
MODEL_ALIAS = {
    "deepseek-v4.1-flash": "deepseek-flash",
    "deepseek-flash": "deepseek-flash",
    "deepseek-v4-flash": "deepseek-flash",
    "deepseek-v4-flash-vision-exp": "deepseek-flash",
    "deepseek-v4-pro": "deepseek-v4-pro",
}


def is_peak(dt_bj):
    """DeepSeek 高峰判定（北京时间）。法定节假日无法离线判定，按高峰计 -> 会轻微高估。"""
    if dt_bj.weekday() >= 5:
        return False
    return (9 <= dt_bj.hour < 12) or (14 <= dt_bj.hour < 18)


# ---------------------------------------------------------------- 环境
def find_onedrive():
    """定位数据根目录（OneDrive 等同步盘；也可用 BOARD_DATA 环境变量覆盖）。"""
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


# 看板固定认这三台机器（与 merge.py 的 ROSTER 一致）
CANON = ("company", "xiaoxin", "home")

# hostname 兜底别名（只在上面几级都拿不到身份时才用，宽松子串匹配）
HOST_ALIASES = (
    ("macbook", "home"), ("imac", "home"), ("mac-mini", "home"), ("darwin", "home"),
    ("xiaoxin", "xiaoxin"), ("lenovo", "xiaoxin"), ("82dm", "xiaoxin"),
    ("fei-pc", "company"), ("company", "company"),
)


def host_id(explicit=None):
    """机器身份。优先级：
        1) 命令行 --host 显式指定（最可靠，不依赖任何本机配置）
        2) ~/.workbuddy/settings.json 的 env.WORKBUDDY_HOST
        3) ~/machine_id.txt
        4) hostname（先查别名表，再原样返回）
    ★ 看板的机器名单是固定三台（CANON）。返回名单外的值时，merge 会把它当成
      额外的第 4 台机器，R3 那一条就会画错 —— 所以 main() 里会对名单外值报警。
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip().lower()

    try:
        with open(os.path.join(WB, "settings.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        h = (cfg.get("env") or {}).get("WORKBUDDY_HOST")
        if h and str(h).strip():
            return str(h).strip().lower()
    except Exception:
        pass
    p = os.path.join(HOME, "machine_id.txt")
    try:
        with open(p, encoding="utf-8") as f:
            t = f.read().strip()
        if t:
            return t.lower()
    except Exception:
        pass

    hn = socket.gethostname().lower()
    for pat, hid in HOST_ALIASES:
        if pat in hn:
            return hid
    return hn


# ------------------------------------------------- requestId 内嵌时间戳
def rid_born(rid):
    """★ 从 requestId 里解出「请求发生时间」，解不出返回 None。

    WorkBuddy 的 requestId 是 32 位 hex 的 UUID：
      * 新版（UUIDv7 布局）第 13 个字符是版本号 '7'，前 48 bit = Unix 毫秒
        -> 精确到毫秒的发生时间
      * 旧版（UUIDv4）纯随机，无任何时间信息

    版本号 + 时间区间 双重校验：只认第 13 位是 '7'、且前 48 bit 落在
    2014-05 ~ 2036 之间的，避免把随机 hex 误当成时间戳。
    """
    r = (rid or "").replace("-", "")
    if len(r) != 32:
        return None
    try:
        if int(r[12], 16) != 7:                    # version 半字节
            return None
        ms = int(r[:12], 16)                       # 前 48 bit = unix 毫秒
    except ValueError:
        return None
    if not (1_400_000_000_000 <= ms <= 2_100_000_000_000):
        return None
    try:
        return datetime.datetime.fromtimestamp(ms / 1000.0, BJ)
    except Exception:
        return None


def _day_from_ms(ms):
    try:
        return datetime.datetime.fromtimestamp((ms or 0) / 1000.0, BJ).date().isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------- 积分账本
def _ledger_path():
    return os.path.join(WB, LEDGER_NAME)


def load_ledger():
    try:
        with open(_ledger_path(), encoding="utf-8") as f:
            d = json.load(f)
        if (isinstance(d, dict) and isinstance(d.get("entries"), dict)
                and isinstance(d.get("daily"), dict)):
            return d
    except Exception:
        pass
    return None


def save_ledger(d):
    """原子写：先落 .tmp 再 replace，避免中途崩了留下半截账本。"""
    p = _ledger_path()
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, p)


def build_audit_index(days_back=12):
    """扫近 N 天的 ~/.workbuddy/audit-log/*.jsonl → ({requestId: 首次出现日}, 跳过文件数)。

    ⚠️ 近似源，只在账本首建时兜底历史柱子：
      * 日文件名的日期是「审计事件写入日」，跨午夜会与真实发生日错位；
      * 只覆盖触发过安全审计的请求（实测约 59% 笔数 / 81% 金额）。
    所以它只用来给"已经过去的日子"找个体面的落点，绝不用来定今天。

    主力机的 audit-log 可能很大，超过 MAX_AUDIT_FILE_MB 的单日文件直接跳过
    （只影响历史柱子的近似精度，不影响"今天"）。
    """
    idx = {}
    if not os.path.isdir(AUDIT_LOG):
        return idx, 0
    cut = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    pat = re.compile(r'"requestId":"([0-9a-zA-Z\-]{8,64})"')
    skipped = 0
    for f in sorted(glob.glob(os.path.join(AUDIT_LOG, "*.jsonl"))):
        day = os.path.basename(f)[:-len(".jsonl")]
        if day == "manifest" or day < cut:
            continue
        try:
            if os.path.getsize(f) > MAX_AUDIT_FILE_MB * 1e6:
                skipped += 1
                continue
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for ln in fh:
                    m = pat.search(ln)
                    if not m:
                        continue
                    r = m.group(1)
                    if r not in idx or day < idx[r]:
                        idx[r] = day
        except Exception:
            continue
    return idx, skipped


# ---------------------------------------------------------------- 1) 积分
def collect_credits(days_back=8, host=None, rebuild=False):
    """按 requestId 记账归日。

    返回 (每日积分, 每日会话集合, 每日明细, 错误, 诊断)
    """
    agg = collections.defaultdict(float)
    cnt = collections.defaultdict(set)
    detail = collections.defaultdict(list)
    stats = {
        "bootstrapped": False, "new": 0, "delta": 0.0,
        "v7": 0, "audit": 0, "updated_at": 0, "first_seen": 0,
        "undated": 0, "undated_amount": 0.0,
        "ledger_entries": 0, "dropped": 0, "audit_scanned": 0, "audit_skipped": 0,
    }

    if not os.path.exists(DB):
        return agg, cnt, detail, "workbuddy.db 不存在", stats

    # ---- 1) 读库，展开到 requestId 级 ----
    raw = {}          # rid -> (value, session8, updated_ms, title)
    uri = "file:" + DB.replace("\\", "/") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
    except Exception as e:
        return agg, cnt, detail, "打开 db 失败: %s" % e
    try:
        q = ("select u.session_id, u.updated_at, u.credit_json, s.custom_title, s.title "
             "from session_usage u left join sessions s on s.id = u.session_id "
             "where u.credit_json is not null")
        for sid, ua, cj, ctitle, title in con.execute(q):
            try:
                d = json.loads(cj)
            except Exception:
                continue
            if not d:
                continue
            ttl = (ctitle or title or "")
            s8 = (sid or "")[:8]
            for rid, v in d.items():
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                prev = raw.get(rid)
                # 同一 requestId 只取其最大金额，防止跨行重复累计
                if prev is None or fv > prev[0]:
                    raw[rid] = (fv, s8, ua or 0, ttl)
    except Exception as e:
        return agg, cnt, detail, "查询失败: %s" % e
    finally:
        con.close()

    today = datetime.date.today().isoformat()

    # ---- 2) 载入 / 首建账本 ----
    led = None if rebuild else load_ledger()
    boot = led is None
    if boot:
        led = {
            "schema": LEDGER_SCHEMA,
            "host": host,
            "bootstrapped_at": datetime.datetime.now(BJ).isoformat(timespec="seconds"),
            "entries": {}, "daily": {}, "daily_sids": {},
        }
        audit, audit_skipped = build_audit_index(max(days_back + 3, 12))
        stats["audit_scanned"] = len(audit)
        stats["audit_skipped"] = audit_skipped
    else:
        audit = {}
        led.setdefault("entries", {})
        led.setdefault("daily", {})
        led.setdefault("daily_sids", {})
    entries, daily, daily_sids = led["entries"], led["daily"], led["daily_sids"]
    stats["bootstrapped"] = boot

    # 今天确实"活着"的会话（含 v7 请求）—— 用于判断 updated_at=今天 的老账是真是假
    live_sids = set()
    if boot:
        for rid, rec in raw.items():
            if rid_born(rid):
                live_sids.add(rec[1])

    # ---- 3) 逐 requestId 归日 ----
    for rid, (v, s8, ua, ttl) in raw.items():
        rec = entries.get(rid)

        if rec is not None:                       # 已入账
            try:
                old = float(rec[1])
            except (TypeError, ValueError, IndexError):
                old = v
            if v > old + CREDIT_EPS:              # 同一请求二次计费：只补差额
                delta = v - old
                daily[today] = daily.get(today, 0.0) + delta
                if s8 and s8 not in daily_sids.setdefault(today, []):
                    daily_sids[today].append(s8)
                rec[1] = v
                stats["delta"] += delta
            continue

        # --- 新 requestId：定日 ---
        rt = rid_born(rid)
        if rt is not None:
            day, src = rt.date().isoformat(), "v7"
        elif boot:
            if rid in audit:
                day, src = audit[rid], "audit"
            else:
                d2 = _day_from_ms(ua)
                # ★ 护栏：老账不许落到今天（除非该会话今天确实有 v7 请求）
                if d2 and (d2 != today or s8 in live_sids):
                    day, src = d2, "updated_at"
                else:
                    day, src = None, None
        else:
            # 账本已建立：新出现的 requestId 就是这一次采集窗口内发生的
            day, src = today, "first_seen"

        if day is None:
            entries[rid] = [None, v, s8]          # 占位，防止下轮被当成"新账"
            stats["undated"] += 1
            stats["undated_amount"] += v
            continue

        entries[rid] = [day, v, s8]
        daily[day] = daily.get(day, 0.0) + v
        if s8 and s8 not in daily_sids.setdefault(day, []):
            daily_sids[day].append(s8)
        stats[src] = stats.get(src, 0) + 1

    # ---- 4) 清理：库里已消失的 requestId（会话被删）不再占位 ----
    gone = [r for r in entries if r not in raw]
    for r in gone:
        del entries[r]
    stats["dropped"] = len(gone)

    cut_keep = (datetime.date.today() - datetime.timedelta(days=LEDGER_KEEP_DAYS)).isoformat()
    for d in [k for k in daily if k < cut_keep]:
        daily.pop(d, None)
        daily_sids.pop(d, None)

    # ---- 5) 汇总 ----
    for d, v in daily.items():
        agg[d] = v
        cnt[d] = set(daily_sids.get(d) or [])
    for rid, rec in entries.items():
        if not rec or rec[0] is None or rec[0] not in daily:
            continue
        ttl = (raw.get(rid) or (0, "", 0, ""))[3]
        detail[rec[0]].append((round(float(rec[1]), 2), ttl[:44]))

    stats["ledger_entries"] = len(entries)
    led["last_run_at"] = datetime.datetime.now(BJ).isoformat(timespec="seconds")
    led["last_host"] = host
    try:
        save_ledger(led)
    except Exception as e:
        return agg, cnt, detail, "写账本失败: %s" % e

    return agg, cnt, detail, None, stats


# ---------------------------------------------------------------- 2) Token
def collect_tokens(cut):
    """按北京时间日期聚合 token 与估算费用。cut 是 ISO 日期字符串。"""
    agg = collections.defaultdict(lambda: {
        "uncached": 0, "cached": 0, "out": 0, "calls": 0,
        "cost_cny": 0.0, "cost_hit": 0.0, "cost_unc": 0.0, "cost_out": 0.0, "traces": 0,
        "models": collections.Counter(), "unpriced": set(),
    })
    n_files = n_used = 0
    for f in glob.glob(os.path.join(TRACES, "*", "trace_*.json")):
        n_files += 1
        try:
            if datetime.datetime.fromtimestamp(os.path.getmtime(f)).date().isoformat() < cut:
                continue
            with open(f, encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        tr = d.get("trace") or {}
        mi = tr.get("modelInfo")
        if not mi:
            continue
        started = tr.get("startedAt") or ""
        try:
            bj = datetime.datetime.fromisoformat(started.replace("Z", "+00:00")).astimezone(BJ)
        except Exception:
            continue
        day = bj.date().isoformat()
        if day < cut:
            continue
        n_used += 1
        models = mi.get("models") or ["(unknown)"]
        tin = int(mi.get("totalInputTokens") or 0)
        tc = int(mi.get("totalCachedTokens") or 0)
        tout = int(mi.get("totalOutputTokens") or 0)
        a = agg[day]
        a["uncached"] += max(0, tin - tc)
        a["cached"] += tc
        a["out"] += tout
        a["calls"] += int(mi.get("callCount") or 0)
        a["traces"] += 1
        # 多模型会话无法拆分明细：整条记在主模型名下，避免双计
        main = models[0]
        a["models"][main] += 1
        key = MODEL_ALIAS.get(main)
        cfg = PRICING_CNY.get(key) if key else None
        if cfg:
            p = cfg["peak"] if is_peak(bj) else cfg["off"]
            a["cost_hit"] += tc * p[0] / 1e6
            a["cost_unc"] += max(0, tin - tc) * p[1] / 1e6
            a["cost_out"] += tout * p[2] / 1e6
            a["cost_cny"] += (tc * p[0] + max(0, tin - tc) * p[1] + tout * p[2]) / 1e6
        else:
            a["unpriced"].add(main)
    return agg, n_files, n_used


# ---------------------------------------------------------------- 3) 余额
def fetch_deepseek_balance():
    try:
        with open(MODELS, encoding="utf-8") as f:
            ms = json.load(f)
    except Exception as e:
        return None, "读 models.json 失败: %s" % e
    lst = ms if isinstance(ms, list) else ms.get("models", [])
    key = None
    for m in lst:
        if "deepseek.com" in (m.get("url") or ""):
            key = m.get("apiKey")
            break
    if not key:
        return None, "models.json 里没有 DeepSeek 的 key"
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + key},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        for info in data.get("balance_infos", []):
            if info.get("currency") == "CNY":
                return float(info["total_balance"]), None
        infos = data.get("balance_infos") or []
        return (float(infos[0]["total_balance"]), None) if infos else (None, "无 balance_infos")
    except Exception as e:
        return None, "查询失败: %s" % e


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=8, help="回溯天数（默认 8，够画 7 日柱）")
    ap.add_argument("--print", dest="show", action="store_true", help="采集后打印摘要")
    ap.add_argument("--host", default=None,
                    help="机器身份：company / xiaoxin / home（不填则自动识别）")
    ap.add_argument("--rebuild-ledger", dest="rebuild", action="store_true",
                    help="丢弃积分账本并按历史重建（仅在账本损坏/归日明显不对时用）")
    args = ap.parse_args()

    today = datetime.date.today()
    cut = (today - datetime.timedelta(days=args.days)).isoformat()
    host = host_id(args.host)
    if host not in CANON:
        print("=" * 64, file=sys.stderr)
        print("警告：本机身份识别为 '%s'，不在标准名单 %s 内。" % (host, list(CANON)), file=sys.stderr)
        print("      这样看板会把它当成第 4 台机器，三机占比那一条会画错。", file=sys.stderr)
        print("      请改用：--host company | xiaoxin | home", file=sys.stderr)
        print("=" * 64, file=sys.stderr)

    credits, cses, detail, cerr, cstats = collect_credits(args.days, host, args.rebuild)
    tokens, n_files, n_used = collect_tokens(cut)
    balance, berr = fetch_deepseek_balance()

    # 近 N 天日期轴（含今天），缺数据补 0，方便渲染器直接用
    days = []
    for i in range(args.days, -1, -1):
        d = (today - datetime.timedelta(days=i)).isoformat()
        t = tokens.get(d) or {}
        unc = t.get("uncached", 0)
        cac = t.get("cached", 0)
        out = t.get("out", 0)
        tot = unc + cac + out
        days.append({
            "date": d,
            "credit": round(credits.get(d, 0.0), 2),
            "sessions": len(cses.get(d) or ()),
            "tokens_total": tot,
            "tokens_uncached": unc,
            "tokens_cached": cac,
            "tokens_out": out,
            "cache_hit_rate": (round(cac / (unc + cac), 4) if (unc + cac) else None),
            "calls": t.get("calls", 0),
            "traces": t.get("traces", 0),
            "cost_cny_est": round(t.get("cost_cny", 0.0), 3),
            # 成本构成（用于柱状图分段）：命中输入 / 未命中输入 / 输出
            # 按 token 切分没有意义 —— 命中率常年 95%+，黑段只有 1~2px 看不见。
            "cost_parts": {
                "hit": round(t.get("cost_hit", 0.0), 4),
                "unc": round(t.get("cost_unc", 0.0), 4),
                "out": round(t.get("cost_out", 0.0), 4),
            },
            "unpriced_models": sorted(t.get("unpriced") or []),
        })

    t_today = days[-1]
    out = {
        "schema": SCHEMA,
        "host": host,
        "hostname": socket.gethostname(),
        "generated_at": datetime.datetime.now(BJ).isoformat(timespec="seconds"),
        "days": days,
        "today": {
            "date": today.isoformat(),
            "credit": t_today["credit"],
            "sessions": t_today["sessions"],
            "tokens_total": t_today["tokens_total"],
            "tokens_uncached": t_today["tokens_uncached"],
            "tokens_cached": t_today["tokens_cached"],
            "tokens_out": t_today["tokens_out"],
            "cache_hit_rate": t_today["cache_hit_rate"],
            "calls": t_today["calls"],
            "cost_cny_est": t_today["cost_cny_est"],
            "unpriced_models": t_today["unpriced_models"],
            "sessions_detail": sorted(detail.get(today.isoformat(), []), reverse=True)[:8],
        },
        "balance": {"deepseek_cny": balance, "error": berr},
        "diagnostics": {
            "trace_files_scanned": n_files,
            "trace_files_used": n_used,
            "credit_error": cerr,
            # ★ 积分归日账本（本机 ~/.workbuddy/board_credit_ledger.json）
            #   v7/audit/updated_at = 首建时各来源定日的笔数；first_seen = 增量归今天的笔数
            #   undated = 无据可依、拒绝计入任何一天的老账（防污染今天）
            "credit_ledger": {
                "bootstrapped": cstats["bootstrapped"],
                "entries": cstats["ledger_entries"],
                "dropped": cstats["dropped"],
                "new_this_run": cstats["new"],
                "delta_this_run": round(cstats["delta"], 4),
                "by_source": {k: cstats[k] for k in ("v7", "audit", "updated_at", "first_seen")},
                "undated_count": cstats["undated"],
                "undated_amount": round(cstats["undated_amount"], 2),
                "audit_index_size": cstats["audit_scanned"],
                "audit_files_skipped": cstats["audit_skipped"],
            },
        },
        "notes": [
            "credit = WorkBuddy 平台积分（权威花费口径），按 requestId 逐个记账归日："
            "优先取 requestId 内嵌时间戳（UUIDv7），其次 audit-log 近似定日，"
            "账本建立后新请求计入当天；无据可依的老账拒绝计入任何一天。",
            "cost_cny_est = 按 DeepSeek 官方价目对 token 的估算，仅供参照，非实际扣费。",
            "balance.deepseek_cny = DeepSeek 官方账户余额（仅当该机在用官方 API 时才有意义）。",
        ],
    }

    od = find_onedrive()
    if not od:
        print("ERROR: 找不到数据目录", file=sys.stderr)
        return 2
    outdir = os.path.join(od, "WorkBuddy", "_共享", "board_data")
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "local_%s.json" % host)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("host=%s  ->  %s" % (host, path))
    print("  扫描 trace %d 个，其中含 modelInfo %d 个" % (n_files, n_used))
    lg = out["diagnostics"]["credit_ledger"]
    print("  积分账本：%s  entries=%d  新建=%d  补差=%.2f"
          % ("首建" if lg["bootstrapped"] else "增量", lg["entries"],
             lg["new_this_run"], lg["delta_this_run"]))
    print("            定日来源 %s   audit索引 %d 条"
          % (lg["by_source"], lg["audit_index_size"]))
    if lg["undated_count"]:
        print("            ⚠️ 未定日（拒绝计入任何一天）：%d 笔 / %.2f 分"
              % (lg["undated_count"], lg["undated_amount"]))
    print("  近 %d 天积分：" % len(days))
    for d in days[-8:]:
        print("    %s  积分 %8.2f  会话 %2d  token %10s  命中 %s  估算 ¥%.3f"
              % (d["date"], d["credit"], d["sessions"], format(d["tokens_total"], ","),
                 ("%.1f%%" % (d["cache_hit_rate"] * 100)) if d["cache_hit_rate"] is not None else "-",
                 d["cost_cny_est"]))
    print("  DeepSeek 余额: %s" % (("¥%.2f" % balance) if balance is not None else ("失败(%s)" % berr)))
    if cerr:
        print("  积分采集异常: %s" % cerr)
    if args.show:
        print(json.dumps(out["today"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
