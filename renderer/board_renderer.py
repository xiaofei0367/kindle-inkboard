# -*- coding: utf-8 -*-
"""Kindle 看板 2.0 · 渲染器 v14（精简版）★ 2026-09-28 样板定稿

═══ v14 改动（作者 2026-09-28 七轮样板迭代拍板，13:40 定稿）═══

  以现役 v13 为骨架做减法 + 两个新数据源：

  删：
  * R1 右栏「会话/输入/输出」「缓存命中」「积分=平台计费｜¥=厂商价目估算」口径说明
  * 另外两台低用量站点（配额巨大永远用不完 = 永不异常 = 纯噪音）。
    ⚠️ 删显示 ≠ 删采集：remote.json 照常采全三站，只是 v14 不读；页脚
    「数据已过期>90分钟」横幅仍是采集链路挂机报警器。
  * R3 三机占比 / New API 分流（公司机 100% 是常量，零信息量）
  * 「预警」徽标、「深色＝已用」「未用 XX GB」「柱高/分段」说明行

  版式（600×800，自上而下）：
  * 顶栏：日期+星期（f20）+ 天气行（f16，wttr.in，城市由 BOARD_CITY 环境变量配置，30 分钟缓存）
    —— 右上角整片留白，专给 Kindle 侧 eips 电量叠加（y≥45 安全区）
  * R1 今日用量：7 日三段柱（柱高=token，分段=成本构成，v13 口径原样保留）
    右栏三数据统一「图标+数字」f30 同号：◆ 积分｜¥ 金额｜Ξ token（全数字千分位）
    辅助行：昨日 X 分 · 7 日均 X 分 · 7日token均（无标签）；7 日合计 tok
  * R2 中部：左＝VPS 流量环形图（单监控站，用量配比一目了然）；
    右＝今日日程（schedule.txt，由 pipeline/icloud_schedule.py 从 iCloud 日历拉取，
    每行 "HH:MM|事件" 或 "全天|事件"，≤5 条；缺失/过期 → 显示 今日无安排）
  * R3 下部：美文金句（quotes.txt，按「日期+小时」取条，同小时稳定一条）
  * 页脚：采集时间 + 心跳 + 过期横幅机制（v13 原样）

  数据源（DATA_DIR，由 --data 或 find_data_dir() 决定，默认 ~/board_data）：
  * schedule.txt —— iCloud 日历今日剩余日程（icloud_schedule.py 产出，1 小时新鲜度）；
    超过 3 小时未更新或缺失 → 显示 今日无安排（拉取失败不影响看板其他区块）
  * quotes.txt  —— 「正文|作者」每行一条（hitokoto 语句库清洗版 ≈1900 条 + 手选置顶）；
    缺失 → 内置兜底 8 条
  * weather_cache.json —— wttr.in 30 分钟缓存（自动生成，失败用旧值最长 6h，再败显示 —）

═══ 继承自 v11/v13 的机制（原样保留）═══
* SDraw 超采样代理（S=12 生产档；8× 是离群档别用；S=1 恒等自校验）
* 末句量化 round(p/17)*17；先 LANCZOS 缩回 1× 再量化（顺序不可反）
* 柱段口径：柱高=token（跨通道统一工作量），分段=成本构成（hit/unc/out）
"""

# --- UTF-8 stdout guard ----------------------------------------------------
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
import hashlib
import json
import math
import os
import re
import sys
import time

from PIL import Image, ImageDraw, ImageFont

W, H = 600, 800
M = 24

# ★ 超采样倍率：12 = 生产选档；⚠️ 8 是离群档别用；1 = 恒等自校验
S = int(os.environ.get("BOARD_SS", "12") or 12)
if S < 1:
    S = 1

INK = 0
DIM = 34
FAINT = 68
LINE = 136
TRACK = 204
BG = 255
HIT_GRAY = 136
OUT_GRAY = 85
SEG_GRAYS = [0, 136, 68]

FONT_FILE = "C:/Windows/Fonts/msyhbd.ttc"
HOME = os.path.expanduser("~")
DEFAULT_OUTDIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")

STALE_MIN = 90
EMBOLDEN = False

PIE_CY = 460
PIE_R = 92
PIE_RING = 34
RX = M + 232 + 16          # 280，右栏统一左缘（v14 定稿：比 v13 再右移 32）
CX2 = RX + 176

# 城市与数据目录（board_data 由 main() 解析后写入）
CITY = os.environ.get("BOARD_CITY", "")          # wttr.in 城市名（如 Shanghai）；留空=不显示天气行
CITY_CN = os.environ.get("BOARD_CITY_CN", "") or CITY
BOARD_DATA_DIR = None


def _fontpath():
    for p in (FONT_FILE, "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "/System/Library/Fonts/PingFang.ttc"):
        if os.path.exists(p):
            return p
    return None


def F(sz):
    return ImageFont.truetype(_fontpath(), sz)


f14, f16, f18, f20, f22, f30 = (F(s) for s in (14, 16, 18, 20, 22, 30))


# ============================================================ SDraw（v11 机制）
class SDraw(object):
    """1× 逻辑坐标 → S 倍画布的代理。算术留在 1× 空间，落笔才乘 S。"""

    _MISSING = set()

    def __init__(self, d, s):
        self._d = d
        self.S = s
        self._fc = {}

    def _big(self, font):
        if self.S == 1 or font is None:
            return font
        key = (getattr(font, "path", id(font)), font.size)
        f = self._fc.get(key)
        if f is None:
            f = ImageFont.truetype(font.path, int(round(font.size * self.S)))
            self._fc[key] = f
        return f

    def _xy(self, xy):
        s = self.S
        if isinstance(xy, (list, tuple)):
            if len(xy) and isinstance(xy[0], (list, tuple)):
                return [(p[0] * s, p[1] * s) for p in xy]
            return [v * s for v in xy]
        return xy

    def _w(self, width):
        return max(1, int(round((width or 1) * self.S)))

    def text(self, xy, text, font=None, fill=None, anchor=None, **kw):
        f = self._big(font)
        if kw.get("stroke_width"):
            kw["stroke_width"] = max(1, int(round(kw["stroke_width"] * self.S)))
        self._d.text(self._xy(xy), text, font=f, fill=fill, anchor=anchor, **kw)

    def line(self, xy, fill=None, width=1, **kw):
        self._d.line(self._xy(xy), fill=fill, width=self._w(width), **kw)

    def rectangle(self, xy, fill=None, outline=None, width=1, **kw):
        self._d.rectangle(self._xy(xy), fill=fill, outline=outline,
                          width=self._w(width), **kw)

    def ellipse(self, xy, fill=None, outline=None, width=1, **kw):
        self._d.ellipse(self._xy(xy), fill=fill, outline=outline,
                        width=self._w(width), **kw)

    def pieslice(self, xy, start, end, fill=None, outline=None, width=1, **kw):
        # ★ 角度不缩放
        self._d.pieslice(self._xy(xy), start=start, end=end, fill=fill,
                         outline=outline, width=self._w(width), **kw)

    def textbbox(self, xy, text, font=None, **kw):
        if self.S == 1:
            return self._d.textbbox(xy, text, font=font, **kw)
        bb = self._d.textbbox(self._xy(xy), text, font=self._big(font), **kw)
        return tuple(v / float(self.S) for v in bb)

    def textlength(self, text, font=None, **kw):
        if self.S == 1:
            return self._d.textlength(text, font=font, **kw)
        return self._d.textlength(text, font=self._big(font), **kw) / float(self.S)

    def __getattr__(self, name):
        SDraw._MISSING.add(name)
        return getattr(self._d, name)


def _missing_report():
    if SDraw._MISSING:
        print("[v14][警告] 以下 Draw 方法未代理（未缩放）: %s"
              % sorted(SDraw._MISSING), file=sys.stderr)


# ---------------------------------------------------------------- 格式化
def fmt_tok(n):
    n = n or 0
    if n >= 1e9:
        return "%.2fB" % (n / 1e9)
    if n >= 1e6:
        return "%.1fM" % (n / 1e6)
    if n >= 1e3:
        return "%.0fK" % (n / 1e3)
    return str(n)


def fmt_bytes(b):
    if b is None:
        return "—"
    gb = b / 1e9
    if gb >= 1000:
        return "%.2f TB" % (gb / 1000.0)
    if gb >= 100:
        return "%.0f GB" % gb
    return "%.2f GB" % gb


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


# ---------------------------------------------------------------- 天气（wttr.in）
_WIND8 = {"N": "北", "NE": "东北", "E": "东", "SE": "东南",
          "S": "南", "SW": "西南", "W": "西", "NW": "西北"}

# wttr.in j1 接口的 weatherDesc 只有英文，常用词汉化（兜底：原样显示英文）
_WX_CN = {
    "clear": "晴", "sunny": "晴", "partly cloudy": "局部多云",
    "cloudy": "多云", "overcast": "阴", "mist": "薄雾", "fog": "雾",
    "patchy rain nearby": "局部有雨", "patchy light rain": "零星小雨",
    "light rain": "小雨", "moderate rain": "中雨", "heavy rain": "大雨",
    "light rain shower": "小阵雨", "moderate or heavy rain shower": "阵雨",
    "thundery outbreaks possible": "可能有雷雨", "thunderstorm": "雷雨",
    "patchy light drizzle": "零星毛毛雨", "light drizzle": "毛毛雨",
    "light snow": "小雪", "moderate snow": "中雪", "heavy snow": "大雪",
    "light snow showers": "小阵雪", "blowing snow": "吹雪",
    "patchy light snow": "零星小雪", "haze": "霾", "sleet": "雨夹雪",
}


def _wx_cn(desc):
    d = (desc or "").strip()
    return _WX_CN.get(d.lower(), d)


def _beaufort(kmph):
    v = float(kmph or 0)
    for lim, b in ((1, 0), (6, 1), (12, 2), (20, 3), (29, 4), (39, 5),
                   (50, 6), (62, 7), (75, 8), (89, 9), (103, 10), (118, 11)):
        if v < lim:
            return b
    return 12


def _fetch_wttr():
    """拉 wttr.in 当前天气，拼成「城市  26° 多云转晴 · 东南风 2 级」。未配置城市或失败返回 None。"""
    if not CITY:
        return None
    try:
        import urllib.request
        url = "https://wttr.in/%s?format=j1&lang=zh" % CITY
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=6) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        cc = (data.get("current_condition") or [{}])[0]
        desc = ""
        lz = cc.get("lang_zh") or []
        if lz and lz[0].get("value"):
            desc = lz[0]["value"]
        if not desc:
            desc = ((cc.get("weatherDesc") or [{}])[0].get("value") or "").strip()
        temp = cc.get("temp_C", "")
        wd = (cc.get("winddir16Point") or "").upper()
        wind_txt = ""
        if wd in _WIND8:
            wind_txt = "%s风 %d 级" % (_WIND8[wd], _beaufort(cc.get("windspeedKmph")))
        parts = ["%s° %s" % (temp, _wx_cn(desc))]
        if wind_txt:
            parts.append(wind_txt)
        return "%s  %s" % (CITY_CN, " · ".join(parts))
    except Exception as e:
        print("[v14][天气] 拉取失败: %s" % e, file=sys.stderr)
        return None


def load_weather(now):
    """30 分钟缓存；拉取失败用旧值（最长 6h）；再不行显示 —。绝不让渲染崩。"""
    if not BOARD_DATA_DIR:
        return "—"
    cache = os.path.join(BOARD_DATA_DIR, "weather_cache.json")
    stale_sum = None
    if os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as f:
                c = json.load(f)
            ft = datetime.datetime.fromisoformat(c["fetched_at"])
            age = (now - ft).total_seconds()
            if age < 30 * 60:
                return c["summary"]
            if age < 6 * 3600:
                stale_sum = c["summary"]
        except Exception:
            pass
    fresh = _fetch_wttr()
    if fresh:
        try:
            with open(cache, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": now.isoformat(), "summary": fresh}, f)
        except Exception:
            pass
        return fresh
    return stale_sum or "—"


# ---------------------------------------------------------------- 今日日程
def load_schedule(maxn=5, maxlen=16):
    """schedule.txt：每行 "HH:MM|事件" 或 "全天|事件"（icloud_schedule.py 产出）。
    文件缺失或超过 3 小时未更新 → 返回空（渲染层显示 今日无安排）。"""
    out = []
    if BOARD_DATA_DIR:
        p = os.path.join(BOARD_DATA_DIR, "schedule.txt")
        if os.path.exists(p):
            try:
                if time.time() - os.path.getmtime(p) > 3 * 3600:
                    return out
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if not ln or ln.startswith("#") or "|" not in ln:
                            continue
                        tm, ev = ln.split("|", 1)
                        ev = ev.strip()
                        if len(ev) > maxlen:
                            ev = ev[:maxlen - 1] + "…"
                        out.append((tm.strip(), ev))
                        if len(out) >= maxn:
                            break
            except Exception as e:
                print("[v14][日程] 读取失败: %s" % e, file=sys.stderr)
    return out


# ---------------------------------------------------------------- 金句
_FALLBACK_QUOTES = [
    "凡是过往，皆为序章。|莎士比亚",
    "路漫漫其修远兮，吾将上下而求索。|屈原",
    "千里之行，始于足下。|老子",
    "不积跬步，无以至千里。|荀子",
    "海纳百川，有容乃大。|林则徐",
    "纸上得来终觉浅，绝知此事要躬行。|陆游",
    "山重水复疑无路，柳暗花明又一村。|陆游",
    "长风破浪会有时，直挂云帆济沧海。|李白",
]


def load_quote(now):
    """按「日期+小时」取条：同一小时内渲染多次也是同一条（渲染周期 15 分钟）。"""
    lines = []
    if BOARD_DATA_DIR:
        p = os.path.join(BOARD_DATA_DIR, "quotes.txt")
        if os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as f:
                    for ln in f:
                        ln = ln.strip()
                        if ln and not ln.startswith("#"):
                            lines.append(ln)
            except Exception:
                pass
    if not lines:
        lines = _FALLBACK_QUOTES
    seed = now.strftime("%Y%m%d%H")
    idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(lines)
    raw = lines[idx]
    if "|" in raw:
        body, author = raw.split("|", 1)
    elif "——" in raw:
        body, author = raw.split("——", 1)
    else:
        body, author = raw, ""
    return body.strip(), author.strip()


# ---------------------------------------------------------------- 渲染
def render(data, out, now=None):
    now = now or datetime.datetime.now()

    img = Image.new("L", (W * S, H * S), BG)
    d = SDraw(ImageDraw.Draw(img), S)

    def text(xy, s, fnt, fill=INK, anchor="la"):
        kw = {}
        if EMBOLDEN and fnt.size <= 16:
            kw = {"stroke_width": 1, "stroke_fill": fill}
        d.text(xy, s, font=fnt, fill=fill, anchor=anchor, **kw)

    def hline(y):
        d.line([(M, y), (W - M, y)], fill=LINE, width=1)

    def hatch_rect(x0, y0, x1, y1, step=7, lw=2):
        w, h = max(1, int(x1 - x0)), max(1, int(y1 - y0))
        lay = Image.new("L", (w * S, h * S), BG)
        ld = SDraw(ImageDraw.Draw(lay), S)
        for k in range(0, w + h, step):
            ld.line([(k, 0), (k - h, h)], fill=INK, width=lw)
        img.paste(lay, (int(x0 * S), int(y0 * S)))

    def swatch(x, y, size, fill, outline=None, hatched=False):
        if hatched:
            hatch_rect(x, y, x + size, y + size)
        elif fill is not None:
            d.rectangle([x, y, x + size, y + size], fill=fill)
        if outline is not None:
            d.rectangle([x, y, x + size, y + size], outline=outline, width=2)

    tok = data.get("token") or {}
    t = tok.get("today") or {}
    days = tok.get("days") or []

    WEEK = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

    # ================= 顶栏：日期星期 + 天气 =================
    text((M, 26), "%d月%d日 %s" % (now.month, now.day, WEEK[now.weekday()]), f20, INK)
    text((M, 58), load_weather(now), f16, DIM)
    # （右上角整片留白：Kindle 侧 eips 电量叠加区，坐标须 ≥45 避开状态栏禁区）

    hline(88)

    # ================= R1  今日用量 =================
    BX, BY, BW, BH = M, 104, 208, 170
    base = BY + BH - 26
    top = BY + 8

    bars = []
    for x in days:
        parts = x.get("cost_parts") or {}
        s = sum(v for v in parts.values() if v)
        bars.append({
            "lab": (x.get("date") or "")[-2:],
            "v": x.get("tokens_total") or 0.0,
            "sh": ([parts.get("hit", 0.0) / s, parts.get("unc", 0.0) / s,
                    parts.get("out", 0.0) / s] if (s > 0 and x.get("split_known")) else None),
        })
    n = max(1, len(bars))
    bar_w = 20
    gap = (BW - n * bar_w) // max(1, n - 1)
    maxv = max([b["v"] for b in bars] + [1e-9])

    for i, b in enumerate(bars):
        x = BX + i * (bar_w + gap)
        hh = int((b["v"] / maxv) * (base - top)) if b["v"] > 0 else 0
        if b["v"] > 0 and hh < 3:
            hh = 3
        if hh <= 0:
            continue
        if b["sh"] is None:
            hatch_rect(x, base - hh, x + bar_w, base)
        else:
            h_hit = int(round(hh * b["sh"][0]))
            h_unc = int(round(hh * b["sh"][1]))
            h_out = max(0, hh - h_hit - h_unc)
            if h_hit > 0:
                d.rectangle([x, base - h_hit, x + bar_w, base], fill=HIT_GRAY)
            if h_unc > 0:
                d.rectangle([x, base - h_hit - h_unc, x + bar_w, base - h_hit], fill=INK)
            if h_out > 0:
                d.rectangle([x, base - hh, x + bar_w, base - h_hit - h_unc], fill=OUT_GRAY)
        d.rectangle([x, base - hh, x + bar_w, base], outline=INK, width=1)

    d.line([(BX, base), (BX + BW, base)], fill=INK, width=2)
    for i, b in enumerate(bars):
        cx = BX + i * (bar_w + gap) + bar_w // 2
        text((cx, base + 6), b["lab"], f14, FAINT, anchor="ma")

    # 图例（三段柱要能读懂，保留）
    swatch(BX, 278, 12, HIT_GRAY, outline=INK)
    text((BX + 16, 274), "命中", f14, DIM)
    swatch(BX + 66, 278, 12, INK, outline=INK)
    text((BX + 82, 274), "未命中", f14, DIM)
    swatch(BX + 140, 278, 12, OUT_GRAY, outline=INK)
    text((BX + 156, 274), "输出", f14, DIM)

    # ---- 右栏三数据：◆ 积分｜¥ 金额｜Ξ token（f30 同号，图标+数字，无文字标签）
    credit = t.get("credit") or 0.0
    cost = t.get("cost_cny")
    tok_today = t.get("tokens_total")
    if tok_today is None:
        tok_today = ((t.get("tokens_cached") or 0) + (t.get("tokens_uncached") or 0)
                     + (t.get("tokens_out") or 0))
    wk = sum(b["v"] for b in bars)
    avg_credit = (sum((x.get("credit") or 0.0) for x in days) / len(days)) if days else 0.0
    avg_tok = (wk / len(bars)) if bars else 0.0
    prev = days[-2] if len(days) >= 2 else None

    text((RX, 92), "今日用量", f20, DIM)
    text((RX, 122), "◆ %.2f" % credit, f30, INK)
    text((CX2, 122), ("¥%.2f" % cost) if cost is not None else "—", f30, INK)
    tok_s = "Ξ %s" % format(int(round(tok_today or 0)), ",")
    tf = f30
    try:
        if d.textlength(tok_s, font=f30) > (W - M - RX):
            tf = F(22)          # 9 位数以上自动缩号，防顶边
    except Exception:
        pass
    text((RX, 176), tok_s, tf, INK)
    tail = []
    if prev:
        tail.append("昨日 %.0f 分" % (prev.get("credit") or 0.0))
    if days:
        tail.append("7 日均 %.0f 分" % avg_credit)
        tail.append(fmt_tok(avg_tok))          # 7 日均 token（作者拍板：不带标签）
    if tail:
        text((RX, 240), " · ".join(tail), f16, FAINT)
    text((RX, 270), "7 日合计 %s tok" % fmt_tok(wk), f16, DIM)

    hline(304)

    # ================= R2  中部：左流量环 + 右今日日程 =================
    text((M, 322), "VPS 流量", f18, DIM)

    site = None
    for x in (data.get("vps") or {}).get("sites") or []:
        if x.get("id") == "tokyo":
            site = x
            break

    px0, py0 = M, PIE_CY - PIE_R
    px1, py1 = M + PIE_R * 2, PIE_CY + PIE_R
    rin = PIE_R - PIE_RING
    cxp = M + PIE_R

    used = None
    pct = None
    state = None
    if site:
        used = site.get("used_bytes")
        pct = site.get("pct")
        state = site.get("state")

    d.ellipse([px0, py0, px1, py1], fill=TRACK)
    if used is not None and pct is not None and state != "suspended":
        frac = min(max(float(pct) / 100.0, 0.0), 1.0)
        if frac > 0:
            d.pieslice([px0, py0, px1, py1], start=-90, end=-90 + 360 * frac, fill=INK)
        text((cxp, PIE_CY - 18), fmt_bytes(used), f22, INK, anchor="mm")
        text((cxp, PIE_CY + 8), "已用 %.0f%%" % float(pct), f14, DIM, anchor="mm")
    else:
        # 停机态（10-03 前）：空环 + 已停机 + 复活日
        dl = (site or {}).get("days_to_reset")
        if dl is not None:
            rv = (now + datetime.timedelta(days=float(dl))).strftime("%m-%d")
            sub = "%s 复活" % rv
        else:
            sub = (site or {}).get("reset_label") or "待恢复"
        text((cxp, PIE_CY - 14), "已停机", f22, DIM, anchor="mm")
        text((cxp, PIE_CY + 14), sub, f14, FAINT, anchor="mm")
    d.ellipse([px0, py0, px1, py1], outline=INK, width=2)
    d.ellipse([cxp - rin, PIE_CY - rin, cxp + rin, PIE_CY + rin], outline=INK, width=2)

    # ---- 右列：今日日程 ----
    text((RX, 322), "今日日程", f18, DIM)
    sched = load_schedule()
    sy = 366
    if not sched:
        text((RX, sy), "今日无安排", f16, FAINT)
    for tm, ev in sched:
        d.ellipse([RX + 1, sy + 4, RX + 11, sy + 14], outline=INK, width=2)   # 圆点=日程
        text((RX + 22, sy - 2), tm, f16, DIM)
        text((RX + 74, sy - 2), ev, f16, INK)
        sy += 44

    hline(580)

    # ================= R3  下部：美文金句（每小时一条） =================
    q_body, q_author = load_quote(now)
    if len(q_body) > 22:
        q_body = q_body[:21] + "…"
    text((M, 614), "「%s」" % q_body, f22, INK)
    if q_author:
        text((M, 658), "—— %s" % q_author, f16, FAINT)

    # ---------------- 页脚 + 过期横幅（挂机报警器，v13 机制原样） ----------------
    gen = data.get("generated_at")
    stale = False
    try:
        gt = datetime.datetime.fromisoformat(gen)
        if gt.tzinfo:
            gt = gt.replace(tzinfo=None)
        stale = (now - gt).total_seconds() > STALE_MIN * 60
    except Exception:
        pass

    ts = now.strftime("%m-%d %H:%M")
    text((M, 760), "采集 %s · 心跳正常" % ts, f14, FAINT)

    if stale:
        d.rectangle([1, 1, W - 2, H - 2], outline=INK, width=3)
        d.rectangle([W - M - 132, 8, W - M - 8, 34], fill=INK)
        bb = d.textbbox((0, 0), "数据已过期 >%d 分钟" % STALE_MIN, font=f14)
        d.text((W - M - 132 + 7 - bb[0], 8 + 7 - bb[1]),
               "数据已过期 >%d 分钟" % STALE_MIN, font=f14, fill=BG)

    # ★ 先缩回 1×，再量化（顺序不可反，v11 结论）
    img = img.convert("L")
    if S > 1:
        img = img.resize((W, H), Image.LANCZOS)
    img = Image.eval(img, lambda p: int(round(p / 17.0)) * 17)
    img.save(out)
    print("SAVED", out, img.size, "S=%d" % S)
    return out


def main():
    global EMBOLDEN, S, BOARD_DATA_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--embolden", action="store_true")
    ap.add_argument("--scale", type=int, default=None,
                    help="超采样倍率，覆盖环境变量 BOARD_SS（默认 12）")
    args = ap.parse_args()
    EMBOLDEN = bool(args.embolden)
    if args.scale:
        S = max(1, args.scale)

    od = find_onedrive()
    if od:
        BOARD_DATA_DIR = os.path.join(od, "WorkBuddy", "_共享", "board_data")

    path = args.data
    if not path:
        path = os.path.join(BOARD_DATA_DIR or "", "board_latest.json")
    if path:
        # 数据目录自包含：日程/金句/天气缓存与 JSON 同目录
        BOARD_DATA_DIR = os.path.dirname(os.path.abspath(path))
    if not path or not os.path.exists(path):
        print("ERROR: 数据文件不存在: %s（请用 --data 指定）" % path, file=sys.stderr)
        return 3
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    out = args.out or os.path.join(DEFAULT_OUTDIR, "预览_v14_S%d.png" % S)
    render(data, out)
    _missing_report()
    return 0


if __name__ == "__main__":
    sys.exit(main())
