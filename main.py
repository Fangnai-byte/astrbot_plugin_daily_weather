# -*- coding: utf-8 -*-
"""每日天气推送（和风天气优先，没配 key 时自动用免密钥的 Open-Meteo）。

设计要点（都是从课表插件的实战教训里来的）：
- **订阅制**：`/天气 订阅 [城市]` 把当前会话加进推送目标，城市可每个会话不同；
  会话没设城市时用配置里的 `default_city`。
- **三种节奏都支持**：`push_times` 里每个时刻是一条推送；时刻早于 `evening_after`
  （默认 18 点）推**今天**，否则推**明天**。`alert_only=true` 时只在"值得提醒"的日子推
  （有雨 / 降温 / 大风 / 高温 / 严寒 / 强紫外线 / 气象预警）。
- **跨实例去重**：`push_state.json` 按 (日期, 时刻) 抢名额，重载/多实例都不会重复推；
  调度器登记表挂在 context 上，加载时先关掉残留调度器。
- **不写死任何角色名**：日志标签固定用 `[daily_weather]`，消息内容只有天气事实。
- **失败不打扰**：取不到数据就不推（并记日志），缓存 30 分钟内的成功结果兜底。
"""

import asyncio
import datetime as dt
import json
import os
import re
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

try:                      # AstrBot 一定带 aiohttp；这里只是让插件在被单独导入时不炸
    import aiohttp
except Exception:         # pragma: no cover
    aiohttp = None

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import AtAll, Plain
from astrbot.core.message.components import Image
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_STATE_FILE = os.path.join(PLUGIN_DIR, "push_state.json")


def _plugin_data_dir() -> str:
    """插件持久化目录：data/plugin_data/astrbot_plugin_daily_weather/。

    审核要求：状态文件不能放插件目录（更新/重装会跟着丢），也不能散在 data 根下。
    """
    try:
        from astrbot.core.star.star_tools import StarTools

        base = str(StarTools.get_data_dir("astrbot_plugin_daily_weather"))
    except Exception:
        base = os.path.join(os.path.dirname(os.path.dirname(PLUGIN_DIR)),
                            "plugin_data", "astrbot_plugin_daily_weather")
    os.makedirs(base, exist_ok=True)
    return base


def state_file() -> str:
    """状态文件路径；第一次用的时候把插件目录里的老文件复制过来。"""
    path = os.path.join(_plugin_data_dir(), "push_state.json")
    if not os.path.exists(path) and os.path.exists(LEGACY_STATE_FILE):
        try:
            import shutil

            shutil.copy2(LEGACY_STATE_FILE, path)
            logger.info("[daily_weather] 已把状态文件迁移到 %s（旧文件保留）", path)
        except OSError as exc:
            logger.warning("[daily_weather] 迁移状态文件失败: %s", exc)
    return path

TAG = "[daily_weather]"
WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

# WMO 天气代码 → (emoji, 中文)。Open-Meteo 用 WMO code，和风直接给中文。
WMO = {
    0: ("☀️", "晴"), 1: ("🌤️", "大致晴朗"), 2: ("⛅", "多云"), 3: ("☁️", "阴"),
    45: ("🌫️", "雾"), 48: ("🌫️", "冻雾"),
    51: ("🌦️", "细雨"), 53: ("🌦️", "毛毛雨"), 55: ("🌧️", "密集细雨"),
    56: ("🌧️", "冻毛毛雨"), 57: ("🌧️", "强冻毛毛雨"),
    61: ("🌦️", "小雨"), 63: ("🌧️", "中雨"), 65: ("🌧️", "大雨"),
    66: ("🌧️", "冻雨"), 67: ("🌧️", "强冻雨"),
    71: ("🌨️", "小雪"), 73: ("🌨️", "中雪"), 75: ("❄️", "大雪"), 77: ("🌨️", "雪粒"),
    80: ("🌦️", "阵雨"), 81: ("🌧️", "强阵雨"), 82: ("⛈️", "暴雨"),
    85: ("🌨️", "阵雪"), 86: ("❄️", "强阵雪"),
    95: ("⛈️", "雷阵雨"), 96: ("⛈️", "雷阵雨伴冰雹"), 99: ("⛈️", "强雷暴伴冰雹"),
}

# 中文天气词 → emoji（和风的 textDay/textNight 是中文，这里按关键词给个图标）
TEXT_EMOJI = [
    ("雷", "⛈️"), ("冰雹", "⛈️"), ("雪", "🌨️"), ("雨", "🌧️"),
    ("雾", "🌫️"), ("霾", "🌫️"), ("沙", "🌪️"), ("尘", "🌪️"),
    ("阴", "☁️"), ("多云", "⛅"), ("晴", "☀️"),
]


def emoji_for_text(text: str) -> str:
    """按中文天气描述挑一个图标（认不出就给个中性图标，不报错）。"""
    t = str(text or "")
    for key, emo in TEXT_EMOJI:
        if key in t:
            return emo
    return "🌡️"


def beaufort(kmh) -> str:
    """km/h → 蒲福风级（粗粒度，够展示用）。"""
    try:
        v = float(kmh)
    except (TypeError, ValueError):
        return "-"
    for limit, scale in ((1, "0"), (5, "1"), (11, "2"), (19, "3"), (28, "4"), (38, "5"),
                         (49, "6"), (61, "7"), (74, "8"), (88, "9"), (102, "10"), (117, "11")):
        if v < limit:
            return scale
    return "12"


def _num(v):
    """把可能是字符串/None 的数值转成 float；失败返回 None。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f


def _hhmm_to_cron(text: str):
    """'7:00' / '07:00' → '0 7 * * *'；非法返回 None。"""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*", str(text or ""))
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return f"{mm} {hh} * * *"


def wmo_text(code):
    """WMO 代码 → (emoji, 中文)。"""
    try:
        c = int(code)
    except (TypeError, ValueError):
        return ("🌡️", "未知")
    return WMO.get(c, ("🌡️", "未知"))


# ---------------- 数据归一化（各家 API → 统一结构） ----------------
# 统一结构（后面渲染与判定只看它，与数据源无关）：
#   {"date","weekday","city","tmax","tmin","text_day","text_night","precip_prob","precip_mm",
#    "wind_kmh","wind_scale","wind_dir","uv","humidity","sunrise","sunset",
#    "temp_now","feels_now","text_now","emoji_now","warnings":[..],"hourly_peak":(概率, 时刻)}


def _hourly_by_day(hourly: dict, key: str) -> dict:
    """把逐时数组按日期分组：{"2026-10-20": [值, ...], ...}（None 会被跳过）。"""
    out = {}
    times = hourly.get("time") or []
    vals = hourly.get(key) or []
    for t, v in zip(times, vals):
        if v is None:
            continue
        day = str(t)[:10]
        out.setdefault(day, []).append(v)
    return out


def normalize_openmeteo(payload: dict, city: str) -> list:
    """Open-Meteo /v1/forecast → 统一结构（3 天）。"""
    daily = payload.get("daily") or {}
    cur = payload.get("current") or {}
    hourly = payload.get("hourly") or {}
    hourly_prob = _hourly_by_day(hourly, "precipitation_probability")
    days = []
    dates = daily.get("time") or []
    # 逐时降水概率里挑"最高的一段"（只在今天有意义）
    peak = None
    try:
        hts = hourly.get("time") or []
        hps = hourly.get("precipitation_probability") or []
        today = dates[0] if dates else None
        best = (-1, None)
        for t, p in zip(hts, hps):
            if today and not str(t).startswith(str(today)):
                continue
            if p is not None and float(p) > best[0]:
                best = (float(p), str(t)[-5:])
        if best[1]:
            peak = best
    except Exception:
        peak = None
    for i, d in enumerate(dates):
        def g(key, idx=i):
            arr = daily.get(key) or []
            return arr[idx] if idx < len(arr) else None
        emo, text = wmo_text(g("weather_code"))
        wind = _num(g("wind_speed_10m_max"))
        days.append({
            "date": str(d),
            "weekday": weekday_cn(str(d)),
            "city": city,
            "tmax": _num(g("temperature_2m_max")),
            "tmin": _num(g("temperature_2m_min")),
            "text_day": text, "text_night": text, "emoji": emo,
            # ★ 概率取「当天逐时的最大值」，而不是日聚合字段：
            #   实测 Open-Meteo 的 daily.precipitation_probability_max 会与逐时数据不一致
            #   （实测同一地点 07:00 拿到 3%，同一时刻逐时里已经在报雨；08:30 再查变成 78%），
            #   用逐时算出来的值至少和"峰值时段/雨量"是同一套数据。
            "precip_prob": (_num(max(hourly_prob.get(str(d)) or []))
                            if hourly_prob.get(str(d)) else _num(g("precipitation_probability_max"))),
            "precip_mm": _num(g("precipitation_sum")),
            "wind_kmh": wind, "wind_scale": beaufort(wind), "wind_dir": "",
            "uv": _num(g("uv_index_max")),
            "humidity": _num(cur.get("relative_humidity_2m")) if i == 0 else None,
            "sunrise": str(g("sunrise") or "")[-5:],
            "sunset": str(g("sunset") or "")[-5:],
            "temp_now": _num(cur.get("temperature_2m")) if i == 0 else None,
            "feels_now": _num(cur.get("apparent_temperature")) if i == 0 else None,
            "text_now": wmo_text(cur.get("weather_code"))[1] if i == 0 else "",
            "emoji_now": wmo_text(cur.get("weather_code"))[0] if i == 0 else "",
            "warnings": [],
            "hourly_peak": peak if i == 0 else None,
        })
    return days


def weekday_cn(date_str: str) -> str:
    """'2026-10-12' → '周日'；解析不了返回空串。"""
    try:
        y, m, d = (int(x) for x in str(date_str).split("-")[:3])
        return WEEKDAY_CN[dt.date(y, m, d).weekday()]
    except Exception:
        return ""


# 「查哪天」的解析：先认自然语言词，再认 9月28日 / 9-28 / 9/28 / 28号 这类日期写法。
# 注意只做**整词/整段**匹配：城市名里含「明」的地方很多（昆明、昆明市），不能只看单个字。
DAY_OFFSETS = {
    "今天": 0, "今日": 0, "今儿": 0, "本日": 0,
    "明天": 1, "明日": 1, "明儿": 1, "次日": 1,
    "后天": 2, "后日": 2, "後天": 2,
    "大后天": 3, "大后日": 3,
}

_DATE_RE = re.compile(
    r"(?:(?P<y>20\d{2})\s*[-/年]\s*)?(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*[日号]?"
    r"|(?P<d_only>\d{1,2})\s*[日号]"
)
# 日期词后面常跟这些尾巴，一起剔掉，别把它们当成城市名
_DATE_TAIL_RE = re.compile(r"^(?:的)?(?:天气|预报|气温|温度)?$")


def parse_day_query(text: str) -> tuple[int | None, tuple[int, int] | None, str]:
    """从一条查询里解析「问的是哪一天」。

    返回 (天数偏移, (月, 日) 或 None, 去掉日期词之后的剩余文本)：
    - `明天` → (1, None, "")
    - `9月28日` → (None, (9, 28), "")
    - `北京 明天` → (1, None, "北京")
    - 没有日期词 → (None, None, 原文)
    """
    raw = str(text or "")
    offset: int | None = None
    month_day: tuple[int, int] | None = None
    rest = raw

    for word, off in sorted(DAY_OFFSETS.items(), key=lambda kv: -len(kv[0])):
        if word in rest:
            offset = off
            rest = rest.replace(word, " ")
            break

    m = _DATE_RE.search(rest)
    if m:
        try:
            if m.group("d_only"):
                month_day = (dt.date.today().month, int(m.group("d_only")))
            else:
                month_day = (int(m.group("m")), int(m.group("d")))
        except (TypeError, ValueError):
            month_day = None
        if month_day:
            rest = rest[:m.start()] + " " + rest[m.end():]

    # 清掉「的天气」这类尾巴和多余空格
    parts = [p for p in rest.split() if p.strip()]
    parts = [p for p in parts if not _DATE_TAIL_RE.match(p)]
    return offset, month_day, " ".join(parts)


def resolve_day_index(days: list, offset: int | None, month_day: tuple[int, int] | None) -> int:
    """把「天数偏移 / 月日」落到 days 里的下标；查不到返回 -1。"""
    if month_day is not None:
        want = "-%02d-%02d" % month_day
        for i, d in enumerate(days or []):
            if str(d.get("date") or "").endswith(want):
                return i
        return -1
    if offset is None:
        return 0
    return offset if 0 <= offset < len(days or []) else -1


def _alert_text(w: dict) -> str:
    """把和风的预警条目拼成一行：级别 + 标题。字段名两版接口不完全一样，都试一遍。"""
    if not isinstance(w, dict):
        return str(w or "")
    title = str(w.get("title") or w.get("headline") or "").strip()
    if not title:
        return ""
    sev = str(w.get("severity") or w.get("severityColor") or "").strip()
    typ = str(w.get("typeName") or "").strip()
    bits = [b for b in (sev, typ) if b and b not in title]
    return "·".join(bits + [title]) if bits else title


def normalize_qweather(daily: dict, now: dict, city: str, warnings=None) -> list:
    """和风 /v7/weather/3d（+ /now、/warning/now）→ 统一结构。"""
    days = []
    for i, d in enumerate((daily or {}).get("daily") or []):
        text_day = str(d.get("textDay") or "")
        wind = _num(d.get("windSpeedDay"))
        days.append({
            "date": str(d.get("fxDate") or ""),
            "weekday": weekday_cn(str(d.get("fxDate") or "")),
            "city": city,
            "tmax": _num(d.get("tempMax")),
            "tmin": _num(d.get("tempMin")),
            "text_day": text_day,
            "text_night": str(d.get("textNight") or ""),
            "emoji": emoji_for_text(text_day),
            # 和风 3d 的 precip 是降水量(mm)，不是概率。以前把它同时塞进 precip_prob，
            # 结果按百分比阈值判断时把 7.3mm 当成 7.3%，既不该提醒也提醒不了，
            # 文案还会写成「降水概率 7%」。概率留空，让下游走 mm 那一路。
            "precip_prob": None,
            "precip_mm": _num(d.get("precip")),
            "wind_kmh": wind, "wind_scale": str(d.get("windScaleDay") or beaufort(wind)),
            "wind_dir": str(d.get("windDirDay") or ""),
            "uv": _num(d.get("uvIndex")),
            "humidity": _num((now or {}).get("humidity")) if i == 0 else None,
            "sunrise": str(d.get("sunrise") or "")[-5:],
            "sunset": str(d.get("sunset") or "")[-5:],
            "temp_now": _num((now or {}).get("temp")) if i == 0 else None,
            "feels_now": _num((now or {}).get("feelsLike")) if i == 0 else None,
            "text_now": str((now or {}).get("text") or "") if i == 0 else "",
            "emoji_now": emoji_for_text((now or {}).get("text")) if i == 0 else "",
            "warnings": [_alert_text(w) for w in (warnings or []) if _alert_text(w)] if i == 0 else [],
            "hourly_peak": None,
        })
    return days


# ---------------- 「值得提醒」判定 ----------------
def alert_reasons(days: list, idx: int, cfg: dict) -> list:
    """返回提醒文案列表；空列表表示"没什么值得说的"。纯函数，方便离线测试。"""
    out = []
    if not (0 <= idx < len(days)):
        return out
    d = days[idx]
    nxt = days[idx + 1] if idx + 1 < len(days) else None
    nxt_label = "明天" if idx == 0 else "后天"
    cur_label = "今天" if idx == 0 else "明天"

    prob = d.get("precip_prob")
    mm = d.get("precip_mm") or 0
    rain_prob = int(cfg.get("alert_rain_prob", 40) or 40)
    # 毫米阈值：0.2mm 是零星几滴，够不上带伞；1mm 以上才算真会湿
    rain_mm = float(cfg.get("alert_rain_mm", 1.0) or 1.0)
    trace_mm = 0.1
    rain_text = "雨" in str(d.get("text_day") or "") or "雪" in str(d.get("text_day") or "")

    if prob is not None and prob >= rain_prob:
        # 概率够，就看雨量决定措辞
        if mm >= rain_mm:
            out.append(f"{cur_label}有雨（{mm:.1f}mm），降水概率 {prob:.0f}%，记得带伞")
        else:
            out.append(f"{cur_label}降水概率 {prob:.0f}%，记得带伞")
    elif mm >= rain_mm:
        # 和风没有概率，只给雨量
        out.append(f"{cur_label}有雨（{mm:.1f}mm），记得带伞")
    elif mm >= trace_mm:
        # 有量但不值得带伞
        out.append(f"{cur_label}可能飘几滴（{mm:.1f}mm），出门看一眼天")
    elif prob is not None and rain_text and prob >= 20:
        out.append(f"{cur_label}可能下雨，出门带把伞更稳")

    # 降温：下一天最高温比当前这天低
    drop = cfg.get("alert_temp_drop", 5) or 0
    if nxt and d.get("tmax") is not None and nxt.get("tmax") is not None:
        diff = d["tmax"] - nxt["tmax"]
        if drop and diff >= drop:
            out.append(f"{nxt_label}最高温比{cur_label}低 {diff:.0f}℃，注意加衣")

    wind_alert = cfg.get("alert_wind_kmh", 39) or 0
    if wind_alert and d.get("wind_kmh") is not None and d["wind_kmh"] >= wind_alert:
        out.append(f"{cur_label}风力 {d.get('wind_scale', beaufort(d['wind_kmh']))} 级"
                   f"（{d['wind_kmh']:.0f}km/h），注意防风")

    heat = cfg.get("alert_heat", 35) or 0
    if heat and d.get("tmax") is not None and d["tmax"] >= heat:
        out.append(f"{cur_label}最高 {d['tmax']:.0f}℃，注意防暑补水")
    cold = cfg.get("alert_cold", 0)
    if cold is not None and d.get("tmin") is not None and d["tmin"] <= float(cold):
        out.append(f"{cur_label}最低 {d['tmin']:.0f}℃，注意保暖")

    uv_alert = cfg.get("alert_uv", 8) or 0
    if uv_alert and d.get("uv") is not None and d["uv"] >= uv_alert:
        out.append(f"{cur_label}紫外线 {d['uv']:.0f}（很强），注意防晒")

    if d.get("tmax") is not None and d.get("tmin") is not None:
        swing = d["tmax"] - d["tmin"]
        if swing >= 10:
            out.append(f"{cur_label}昼夜温差 {swing:.0f}℃，早晚加件外套")

    for w in d.get("warnings") or []:
        out.append(f"⚠️ 气象预警：{w}")
    return out


# ---------------- 渲染 ----------------
def build_message(days: list, idx: int, city: str, cfg: dict) -> str:
    """把统一结构渲染成推送文本。纯函数。"""
    if not (0 <= idx < len(days)):
        return f"没有拿到 {city} 的天气数据"
    d = days[idx]
    title = "今日天气" if idx == 0 else ("明日天气" if idx == 1 else "后日天气")
    head = f"【{title} · {city}】{d.get('date', '')} {d.get('weekday', '')}".strip()
    emo = d.get("emoji") or emoji_for_text(d.get("text_day"))
    lines = [head]

    now_bits = []
    if d.get("temp_now") is not None:
        now_emo = d.get("emoji_now") or emo
        bit = f"{now_emo} {d.get('text_now') or d.get('text_day') or ''}　现在 {d['temp_now']:.1f}℃"
        if d.get("feels_now") is not None:
            bit += f"（体感 {d['feels_now']:.1f}℃）"
        now_bits.append(bit)
    else:
        now_bits.append(f"{emo} {d.get('text_day') or '未知'}")
    if d.get("humidity") is not None:
        now_bits.append(f"湿度 {d['humidity']:.0f}%")
    lines.append("　".join(now_bits))

    mid = []
    if d.get("tmin") is not None and d.get("tmax") is not None:
        mid.append(f"气温 {d['tmin']:.0f}~{d['tmax']:.0f}℃")
    if d.get("precip_prob") is not None:
        seg = f"降水概率 {d['precip_prob']:.0f}%"
        if d.get("precip_mm"):
            seg += f"（{d['precip_mm']:.1f}mm）"
        mid.append(seg)
    elif d.get("precip_mm"):
        mid.append(f"降水量 {d['precip_mm']:.1f}mm")
    if d.get("wind_kmh") is not None or d.get("wind_scale"):
        w = f"{d.get('wind_dir', '')}{d.get('wind_scale', '')} 级".strip()
        if d.get("wind_kmh") is not None:
            w += f"（{d['wind_kmh']:.0f}km/h）"
        mid.append(f"风 {w}")
    if mid:
        lines.append("　".join(mid))

    extra = []
    if d.get("sunrise") and d.get("sunset"):
        extra.append(f"日出 {d['sunrise']}　日落 {d['sunset']}")
    if d.get("uv") is not None:
        uv = d["uv"]
        level = "弱" if uv < 3 else ("中等" if uv < 6 else ("强" if uv < 8 else "很强"))
        extra.append(f"紫外线 {uv:.0f}（{level}）")
    if d.get("hourly_peak") and d["hourly_peak"][1]:
        peak_prob, peak_time = d["hourly_peak"]      # 顺序是 (概率, 时刻)
        if peak_prob and peak_prob >= 30:
            extra.append(f"降水概率最高在 {peak_time}（{peak_prob:.0f}%）")
    if extra:
        lines.append("　".join(extra))

    if d.get("text_night") and d["text_night"] != d.get("text_day"):
        lines.append(f"夜间：{d['text_night']}")

    for r in alert_reasons(days, idx, cfg):
        lines.append("· " + r)

    footer = str(cfg.get("footer", "") or "").strip()
    if footer:
        lines.append(footer)
    return "\n".join(lines)


class _CfgNoFooter:
    """包一层配置，把 footer 屏蔽掉（多城市渲染时 footer 只加一次）。"""

    def __init__(self, base):
        self._base = base

    def get(self, key, default=None):
        if key == "footer":
            return ""
        try:
            return self._base.get(key, default)
        except Exception:
            return default


def build_multi_message(per_city: list, idx: int, cfg) -> str:
    """把**多个城市**渲染进同一条推送。

    per_city: [(城市, days), ...]；只有一个城市时输出与 build_message 完全一致。
    """
    if not per_city:
        return ""
    if len(per_city) == 1:
        city, days = per_city[0]
        return build_message(days, idx, city, cfg)
    blocks = [build_message(days, idx, city, _CfgNoFooter(cfg)) for city, days in per_city]
    text = "\n\n".join(blocks)
    footer = ""
    try:
        footer = str(cfg.get("footer", "") or "").strip()
    except Exception:
        footer = ""
    return text + ("\n" + footer if footer else "")


CARD_W_DEFAULT = 760
PAD = 28

THEMES = {
    "clear": {"top": (255, 246, 228), "bot": (255, 226, 198),
              "fg": (74, 59, 42), "sub": (146, 120, 90), "accent": (222, 132, 16),
              "chip": (255, 255, 255), "alert": (255, 243, 226), "alert_fg": (150, 88, 20)},
    "cloudy": {"top": (238, 242, 248), "bot": (216, 226, 238),
               "fg": (44, 52, 64), "sub": (110, 122, 138), "accent": (86, 118, 164),
               "chip": (255, 255, 255), "alert": (238, 244, 252), "alert_fg": (56, 92, 140)},
    "rain": {"top": (224, 234, 246), "bot": (200, 216, 236),
             "fg": (32, 46, 66), "sub": (90, 108, 132), "accent": (56, 104, 172),
             "chip": (255, 255, 255), "alert": (228, 240, 255), "alert_fg": (34, 84, 150)},
    "snow": {"top": (236, 246, 252), "bot": (216, 238, 250),
             "fg": (36, 56, 76), "sub": (96, 120, 142), "accent": (72, 136, 188),
             "chip": (255, 255, 255), "alert": (232, 246, 255), "alert_fg": (40, 96, 150)},
}

_FONT_CACHE = {}
EMOJI_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
CJK_BOLD_CANDIDATES = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
]

# 中文字体画不出来的符号（emoji / 变体选择符），画之前先去掉，避免豆腐块
_DROP_RE = re.compile("[\ufe0e\ufe0f\u26a0\u2757\u2753]")


def sanitize(text: str) -> str:
    """去掉中文字体支持不好的符号（emoji 单独用 emoji 字体画）。"""
    s = _DROP_RE.sub("", str(text or ""))
    return re.sub(r"\s{2,}", " ", s).strip()


def _font(size: int, bold: bool = False, emoji: bool = False):
    key = (size, bold, emoji)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    try:
        from PIL import ImageFont
        cands = EMOJI_FONT_CANDIDATES if emoji else (CJK_BOLD_CANDIDATES if bold else CJK_FONT_CANDIDATES)
        for p in cands:
            if os.path.exists(p):
                font = ImageFont.truetype(p, size)
                break
        if font is None:
            font = ImageFont.load_default()
    except Exception as exc:
        logger.debug("[daily_weather] 取字体失败: %s", exc)
        font = None
    _FONT_CACHE[key] = font
    return font


def theme_key(text: str) -> str:
    t = str(text or "")
    if "雪" in t:
        return "snow"
    if "雨" in t or "雷" in t:
        return "rain"
    if "晴" in t:
        return "clear"
    return "cloudy"


def _size(draw, text: str, font) -> tuple:
    if not text or font is None:
        return (0, 0)
    try:
        b = draw.textbbox((0, 0), text, font=font)
        return (b[2] - b[0], b[3] - b[1])
    except Exception:
        return (len(text) * 12, 18)


def _wrap(draw, text: str, font, max_w: int) -> list:
    text = str(text or "")
    if not text:
        return []
    lines, cur = [], ""
    for tok in re.findall(r"[A-Za-z0-9\.\-%:,/]+|\s+|.", text):
        cand = cur + tok
        if _size(draw, cand, font)[0] <= max_w or not cur:
            cur = cand
        else:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def _draw_emoji(canvas, xy, emoji: str, size: int):
    font = _font(size, emoji=True)
    if font is None or not emoji:
        return
    try:
        from PIL import ImageDraw
        d = ImageDraw.Draw(canvas)
        try:
            d.text(xy, emoji, font=font, embedded_color=True)
        except Exception:
            d.text(xy, emoji, font=font)
    except Exception:
        pass


def build_block(d: dict, alerts: list, city: str, idx: int) -> dict:
    """把一个城市的数据整理成"要画什么"（与绘制解耦，便于测试）。"""
    title = "今日天气" if idx == 0 else ("明日天气" if idx == 1 else "后日天气")
    now = f"{d['temp_now']:.0f}°C" if d.get("temp_now") is not None else ""
    now_extra = f"体感 {d['feels_now']:.0f}°C" if d.get("feels_now") is not None else ""
    rng = (f"{d['tmin']:.0f}~{d['tmax']:.0f}°C"
           if d.get("tmin") is not None and d.get("tmax") is not None else "")
    chips = []
    if d.get("precip_prob") is not None:
        chip = f"降水 {d['precip_prob']:.0f}%"
        if d.get("precip_mm"):
            chip += f" / {d['precip_mm']:.1f}mm"
        chips.append(chip)
    elif d.get("precip_mm"):
        chips.append(f"降水 {d['precip_mm']:.1f}mm")
    if d.get("wind_scale"):
        chips.append(f"风 {d['wind_scale']} 级")
    if d.get("humidity") is not None:
        chips.append(f"湿度 {d['humidity']:.0f}%")
    if d.get("sunrise") and d.get("sunset"):
        chips.append(f"日出 {d['sunrise']} / 日落 {d['sunset']}")
    if d.get("uv") is not None:
        chips.append(f"紫外线 {d['uv']:.0f}")
    return {
        "head": f"{city}　{title}",
        "sub": f"{d.get('date', '')} {d.get('weekday', '')}".strip(),
        "desc": sanitize(d.get("text_now") or d.get("text_day") or ""),
        "emoji": d.get("emoji_now") or d.get("emoji") or "",
        "now": now, "now_extra": now_extra, "rng": rng, "chips": chips,
        "alerts": [sanitize("· " + a) for a in (alerts or []) if sanitize(a)],
        # 配色和显示出来的描述要用同一个来源：描述优先用实时天气，
        # 主题也按实时天气挑（否则会出现"晴"配一片蓝底）
        "theme": theme_key(d.get("text_now") or d.get("text_day") or ""),
    }


def render_card(items: list, cfg: dict, out_path: str) -> str:
    """把若干城市的天气画成一张 PNG，返回文件路径。

    items: [{"city", "days", "idx", "alerts"}, ...]（多城市纵向堆叠）
    任何异常都往上抛，由调用方退回文本。
    """
    from PIL import Image, ImageDraw

    width = int(cfg.get("card_width", CARD_W_DEFAULT) or CARD_W_DEFAULT)
    width = max(420, min(1200, width))
    inner_w = width - PAD * 2
    f_city, f_sub = _font(36, bold=True), _font(18)
    f_now, f_side = _font(42, bold=True), _font(19)
    f_rng, f_chip = _font(26, bold=True), _font(18)
    f_alert = _font(18)

    probe = Image.new("RGB", (width, 10))
    pd = ImageDraw.Draw(probe)
    prepared, total = [], PAD
    for it in items:
        d = it["days"][it["idx"]]
        blk = build_block(d, it.get("alerts"), it["city"], it["idx"])
        chip_lines, buf, buf_w = [], [], 0
        for chip in blk["chips"]:
            w = _size(pd, chip, f_chip)[0] + 26
            if buf_w + w > inner_w and buf:
                chip_lines.append(buf)
                buf, buf_w = [], 0
            buf.append(chip)
            buf_w += w
        if buf:
            chip_lines.append(buf)
        alert_lines = []
        for a in blk["alerts"]:
            alert_lines += _wrap(pd, a, f_alert, inner_w - 30)
        blk["chip_lines"] = chip_lines
        blk["alert_lines"] = alert_lines
        h = 50 + 30 + (72 if blk["now"] else 8) + (36 if blk["rng"] else 0) \
            + len(chip_lines) * 36 + (10 + len(alert_lines) * 30 if alert_lines else 0) + 16
        blk["height"] = h
        total += h
        prepared.append(blk)
    total += PAD
    height = max(200, total)

    card = Image.new("RGB", (width, height), THEMES["cloudy"]["top"])
    draw = ImageDraw.Draw(card)
    # 每个城市一段自己的渐变底色
    y0 = PAD
    for blk in prepared:
        th = THEMES[blk["theme"]]
        for yy in range(y0, y0 + blk["height"]):
            t = (yy - y0) / max(1, blk["height"] - 1)
            col = tuple(int(th["top"][i] * (1 - t) + th["bot"][i] * t) for i in range(3))
            draw.line([(0, yy), (width, yy)], fill=col)
        y0 += blk["height"]

    y = PAD
    for i, blk in enumerate(prepared):
        th = THEMES[blk["theme"]]
        x = PAD
        draw.text((x, y), blk["head"], font=f_city, fill=th["fg"])
        if blk["emoji"]:
            _draw_emoji(card, (width - PAD - 66, y - 6), blk["emoji"], 58)
        y += 50
        line = f"{blk['sub']}　{blk['desc']}".strip()
        draw.text((x, y), line, font=f_sub, fill=th["sub"])
        y += 30
        if blk["now"]:
            draw.text((x, y), blk["now"], font=f_now, fill=th["accent"])
            w_now = _size(draw, blk["now"], f_now)[0]
            if blk["now_extra"]:
                draw.text((x + w_now + 16, y + 20), blk["now_extra"], font=f_side, fill=th["sub"])
        y += 72 if blk["now"] else 8
        if blk["rng"]:
            draw.text((x, y), blk["rng"], font=f_rng, fill=th["fg"])
            draw.text((x + _size(draw, blk["rng"], f_rng)[0] + 12, y + 7),
                      "最低~最高", font=f_side, fill=th["sub"])
            y += 36
        for line_buf in blk["chip_lines"]:
            cx = x
            for chip in line_buf:
                w = _size(draw, chip, f_chip)[0] + 22
                draw.rounded_rectangle([cx, y, cx + w, y + 30], radius=15, fill=th["chip"])
                draw.text((cx + 11, y + 4), chip, font=f_chip, fill=th["fg"])
                cx += w + 8
            y += 36
        if blk["alert_lines"]:
            y += 4
            box_h = len(blk["alert_lines"]) * 30 + 10
            draw.rounded_rectangle([x, y, x + inner_w, y + box_h], radius=12, fill=th["alert"])
            ty = y + 5
            for al in blk["alert_lines"]:
                draw.text((x + 14, ty), al, font=f_alert, fill=th["alert_fg"])
                ty += 30
            y += box_h
        y += 16
        if i < len(prepared) - 1:
            draw.line([(PAD, y - 8), (width - PAD, y - 8)], fill=(190, 200, 212), width=1)

    footer = sanitize(str(cfg.get("footer", "") or ""))
    bottom = y
    if footer:
        draw.text((PAD, y), footer, font=f_sub, fill=(120, 128, 140))
        bottom = y + 26
    # 裁到内容高度，别留一大片空白
    card = card.crop((0, 0, width, min(height, max(160, bottom + PAD // 2))))
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    card.save(out_path, format="PNG")
    return out_path


# ---------------- 插件本体 ----------------
@register("astrbot_plugin_daily_weather", "Fangnai-byte",
          "每天定时推送天气预报：和风天气优先，没配 key 自动用免密钥的 Open-Meteo", "1.4.1")
class DailyWeather(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.scheduler = None
        self._lock = asyncio.Lock()
        self._state = {}
        self._geo_mem = {}          # 城市 → {lat,lon,id,name}（进程内缓存）
        self._data_cache = {}       # (city_key) → (ts, days) 取数缓存，防抖
        self._last_query = {}       # 「会话#人」→ 上次立即查询时间（非管理员轻量冷却）
        self._session = None        # aiohttp 会话（懒建）

    # ---------- 生命周期 ----------
    async def initialize(self):
        self._state = self._load_json(state_file(), {})
        if not isinstance(self._state.get("cities"), dict):
            self._state["cities"] = {}
        if not isinstance(self._state.get("session_cities"), dict):
            self._state["session_cities"] = {}
        if not isinstance(self._state.get("geo"), dict):
            self._state["geo"] = {}
        self._migrate_cities()          # 老状态文件：会话键 → session_cities

        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        # 先关掉"残留的旧调度器"：加载失败 / 连续重载时框架可能来不及 terminate，
        # 旧实例的调度器会留在进程里继续推（课表插件踩过这个坑：同一个群收到两条）。
        reg = getattr(self.context, "_daily_weather_schedulers", None)
        if not isinstance(reg, list):
            reg = []
            try:
                setattr(self.context, "_daily_weather_schedulers", reg)
            except Exception:
                pass
        for old in list(reg):
            try:
                old.remove_all_jobs()
                old.shutdown(wait=False)
                logger.warning("%s 已关闭一个残留的调度器（上次加载没被 terminate）", TAG)
            except Exception as exc:
                logger.warning("%s 关闭残留调度器失败: %s", TAG, exc)
        reg.clear()
        reg.append(self.scheduler)

        times = [t for t in (self.config.get("push_times") or []) if str(t).strip()]
        added = []
        for item in times:
            cron = _hhmm_to_cron(item)
            if not cron:
                logger.warning("%s 推送时刻格式不对，已跳过: %s", TAG, item)
                continue
            self.scheduler.add_job(
                self._tick,
                CronTrigger.from_crontab(cron, timezone="Asia/Shanghai"),
                args=[str(item)],
                id="daily_weather_" + str(item).replace(":", ""),
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=600,
            )
            added.append(str(item))
        self.scheduler.start()
        mode = "只在值得提醒时推" if self.config.get("alert_only", False) else "每天照推"
        provider = self._pick_provider_name()
        targets = self._targets()
        logger.info(
            "%s 已启动；推送时刻: %s；数据源: %s；模式: %s；目标 %d 个",
            TAG, ", ".join(added) or "（未配置，不会自动推）", provider, mode, len(targets),
        )

    async def terminate(self):
        if self.scheduler:
            try:
                self.scheduler.remove_all_jobs()
                self.scheduler.shutdown(wait=False)
            except Exception as exc:
                logger.warning("%s 调度器关闭时出错: %s", TAG, exc)
            reg = getattr(self.context, "_daily_weather_schedulers", None)
            if isinstance(reg, list) and self.scheduler in reg:
                reg.remove(self.scheduler)
            self.scheduler = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        logger.info("%s 已停止", TAG)

    # ---------- 小工具 ----------
    def _load_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as fp:
                return json.load(fp)
        except FileNotFoundError:
            return default
        except Exception as exc:
            logger.warning("%s 读取 %s 失败: %s", TAG, os.path.basename(path), exc)
            return default

    def _save_state(self):
        """读-合并-写：两个实例同时活着时，别把对方刚写的台账擦掉。"""
        try:
            disk = self._load_json(state_file(), {}) or {}
            merged = dict(disk)
            merged.update(self._state)
            if disk.get("claimed"):
                merged["claimed"] = disk["claimed"]
            for key in ("cities", "session_cities", "geo"):
                d = disk.get(key) or {}
                m = merged.get(key) or {}
                if d or m:
                    merged[key] = {**d, **m}
            merged["updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            tmp = state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(merged, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, state_file())
            self._state = merged
        except Exception as exc:
            logger.error("%s 写入状态文件失败: %s", TAG, exc)

    def _claim_once(self, key: str) -> bool:
        """跨实例去重：抢「今天这一次推送」的名额。写盘失败按抢到处理（宁可重一条也别漏）。"""
        today = dt.date.today().isoformat()
        try:
            st = self._load_json(state_file(), {}) or {}
            claimed = st.get("claimed") or {}
            if claimed.get("date") != today:
                claimed = {"date": today, "keys": []}
            keys = list(claimed.get("keys") or [])
            if key in keys:
                return False
            keys.append(key)
            claimed["keys"] = keys
            st["claimed"] = claimed
            tmp = state_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(st, fp, ensure_ascii=False, indent=2)
            os.replace(tmp, state_file())
            self._state["claimed"] = claimed
            return True
        except Exception as exc:
            logger.warning("%s 去重台账写入失败（按已抢到处理）: %s", TAG, exc)
            return True

    def _targets(self):
        out = []
        for item in self.config.get("target_sessions") or []:
            item = str(item).strip()
            if item and item not in out:
                out.append(item)
        return out

    def _save_targets(self, targets):
        self.config["target_sessions"] = list(targets)
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                saver()
                return True
            except Exception as exc:
                logger.warning("%s 保存配置失败: %s", TAG, exc)
        return False

    # ---------- 城市：跟着「人」走，不跟会话走 ----------
    # 为什么：一个群里的人来自不同城市，按会话记一个城市是错的。
    #   cities          : {用户QQ: 城市}  ← 每个人自己的城市（谁都能设）
    #   session_cities  : {会话标识: 城市} ← 群里**推送**用哪个城市（管理员用 /天气 订阅 设）
    @staticmethod
    def _uid_of_private(umo: str) -> str:
        """私聊会话标识里取出对方 QQ（'平台:FriendMessage:123' → '123'）。"""
        parts = str(umo or "").split(":")
        return parts[-1] if len(parts) >= 2 else ""

    def _personal_city(self, uid: str) -> str:
        return str((self._state.get("cities") or {}).get(str(uid)) or "").strip()

    def _session_cities(self, umo: str) -> list:
        """本会话（群）的**推送城市列表**。老状态文件里存的是单个字符串，这里归一成列表。"""
        raw = (self._state.get("session_cities") or {}).get(str(umo))
        if raw is None or raw == "":
            return []
        if isinstance(raw, str):
            return [raw.strip()] if raw.strip() else []
        if isinstance(raw, (list, tuple)):
            out = []
            for c in raw:
                c = str(c).strip()
                if c and c not in out:
                    out.append(c)
            return out
        return []

    def _session_city(self, umo: str) -> str:
        """只取会话城市的第一个（立即查询等单城市场景用）。"""
        cities = self._session_cities(umo)
        return cities[0] if cities else ""

    def _query_city(self, umo: str, uid: str = "") -> str:
        """立即查询用哪个城市：**本人的城市** > 本会话（群）的城市 > 配置里的 default_city。"""
        return (self._personal_city(uid) or self._session_city(umo)
                or str(self.config.get("default_city", "") or "").strip())

    def _push_cities(self, umo: str) -> list:
        """推送用哪些城市（**可以是多个**）。

        顺序：会话（群）自己设的城市列表 > 私聊里那个人的城市 > default_city。
        为什么要多个：一个群里的人来自不同城市，一条推送里分城市列出来才够用。
        """
        cities = self._session_cities(umo)
        if not cities and ":FriendMessage:" in str(umo):
            one = self._personal_city(self._uid_of_private(umo))
            cities = [one] if one else []
        if not cities:
            d = str(self.config.get("default_city", "") or "").strip()
            cities = [d] if d else []
        return cities

    def _push_city(self, umo: str) -> str:
        """单城市场景（/天气 测试 的城市检查等）：取推送城市列表的第一个。"""
        cities = self._push_cities(umo)
        return cities[0] if cities else ""

    def _set_personal_city(self, uid: str, city: str):
        cities = self._state.setdefault("cities", {})
        if city and uid:
            cities[str(uid)] = str(city).strip()
        elif uid:
            cities.pop(str(uid), None)
        self._save_state()

    def _set_session_cities(self, umo: str, cities: list):
        """设本会话的推送城市列表（去重、去空）。"""
        sc = self._state.setdefault("session_cities", {})
        clean = []
        for c in cities or []:
            c = str(c).strip()
            if c and c not in clean:
                clean.append(c)
        if clean:
            sc[str(umo)] = clean
        else:
            sc.pop(str(umo), None)
        self._save_state()

    def _set_session_city(self, umo: str, city: str):
        """兼容旧签名：设成只含一个城市的列表。"""
        self._set_session_cities(umo, [city] if city else [])

    def _migrate_cities(self):
        """老状态文件（把会话标识当城市键）迁移：含 ':' 的键搬到 session_cities。"""
        cities = self._state.get("cities") or {}
        sc = self._state.setdefault("session_cities", {})
        moved = 0
        for k in list(cities.keys()):
            if ":" in str(k):
                sc[str(k)] = cities.pop(k)
                moved += 1
        if moved:
            self._save_state()
            logger.info("%s 已迁移 %d 条旧的城市记录（会话键 → 会话城市表）", TAG, moved)

    # ---------- 卡片 ----------
    def _card_enabled(self) -> bool:
        return bool(self.config.get("card_enabled", True))

    def _card_dir(self) -> str:
        """卡片输出目录：优先 AstrBot 的临时目录，取不到就放插件目录下。"""
        try:
            base = get_astrbot_temp_path()
        except Exception:
            base = os.path.join(get_astrbot_temp_path(), "daily_weather_cards")
        d = os.path.join(base, "daily_weather")
        os.makedirs(d, exist_ok=True)
        return d

    def _cleanup_cards(self, keep_seconds: int = 86400):
        """顺手清掉一天前的卡片，别让临时目录长胖。"""
        try:
            d = self._card_dir()
            now = time.time()
            for name in os.listdir(d):
                if not name.endswith(".png"):
                    continue
                p = os.path.join(d, name)
                try:
                    if now - os.path.getmtime(p) > keep_seconds:
                        os.remove(p)
                except OSError:
                    pass
        except Exception:
            pass

    async def _render_card(self, per_city: list, idx: int):
        """把 per_city 渲染成一张卡片，返回路径；关掉/失败都返回 None（调用方退回文本）。

        渲染是 CPU 活（一两个城市约 100~300ms），丢到线程里做，别卡住事件循环。
        """
        if not self._card_enabled() or not per_city:
            return None
        try:
            items = [{"city": city, "days": days, "idx": idx,
                      "alerts": alert_reasons(days, idx, self.config)}
                     for city, days in per_city]
            self._cleanup_cards()
            out = os.path.join(self._card_dir(),
                               "wx_%d_%s.png" % (int(time.time() * 1000), os.urandom(3).hex()))
            return await asyncio.to_thread(render_card, items, self.config, out)
        except Exception as exc:
            logger.debug("%s 渲染卡片失败，退回文本: %s", TAG, exc)
            return None

    @staticmethod
    def _weather_inconsistent(days: list) -> bool:
        """数据是否自相矛盾：有降水量，但降水概率几乎没有（说明概率那一路是坏的）。"""
        for d in (days or [])[:2]:
            mm = d.get("precip_mm") or 0
            prob = d.get("precip_prob")
            if mm >= 0.5 and (prob is None or prob < 10):
                return True
        return False

    def _pick_provider_name(self) -> str:
        prov = str(self.config.get("provider", "auto") or "auto").strip().lower()
        key = str(self.config.get("qweather_key", "") or "").strip()
        if prov == "qweather":
            return "和风天气" if key else "和风天气（没填 key → 实际会回落到 Open-Meteo）"
        if prov == "openmeteo":
            return "Open-Meteo"
        return "和风天气" if key else "Open-Meteo（自动：没配和风 key）"

    # ---------- HTTP ----------
    async def _http_json(self, url: str, params: dict) -> dict:
        """GET 一个 JSON；失败抛异常（调用方兜住）。"""
        timeout = float(self.config.get("timeout", 15) or 15)
        if self._session is None:
            self._session = aiohttp.ClientSession()
        async with self._session.get(url, params=params,
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            text = await resp.text()
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}: {text[:120]}")
            return json.loads(text)

    async def _geocode(self, city: str, force: bool = False) -> dict:
        """城市名 → 坐标/ID。和风用 city/lookup，Open-Meteo 用 geocoding。带缓存（7 天）。

        force=True 时跳过内存/磁盘缓存，直接重查（用于修正「缓存里没有和风 id」的旧条目）。
        """
        city = str(city or "").strip()
        if not city:
            raise ValueError("没给城市名")
        if not force:
            cached = self._geo_mem.get(city)
            if cached:
                return cached
            geo_disk = (self._state.get("geo") or {}).get(city)
            if geo_disk and time.time() - float(geo_disk.get("ts") or 0) < 7 * 86400:
                self._geo_mem[city] = geo_disk
                return geo_disk

        key = str(self.config.get("qweather_key", "") or "").strip()
        prov = str(self.config.get("provider", "auto") or "auto").strip().lower()
        use_q = bool(key) and prov in ("auto", "qweather")
        try:
            if use_q:
                host = str(self.config.get("qweather_host", "") or "").strip() or "devapi.qweather.com"
                host = re.sub(r"^https?://", "", host).strip("/")
                data = await self._http_json(
                    f"https://{host}/geo/v2/city/lookup",
                    {"location": city, "key": key, "lang": "zh"},
                )
                if str(data.get("code")) != "200" or not data.get("location"):
                    raise RuntimeError(f"和风城市查询失败 code={data.get('code')}")
                loc = data["location"][0]
                geo = {"id": loc.get("id"), "name": loc.get("name"),
                       "lat": _num(loc.get("lat")), "lon": _num(loc.get("lon")),
                       "adm": loc.get("adm1") or "", "ts": time.time(), "src": "qweather"}
            else:
                data = await self._http_json(
                    "https://geocoding-api.open-meteo.com/v1/search",
                    {"name": city, "count": 1, "language": "zh", "format": "json"},
                )
                if not data.get("results"):
                    raise RuntimeError(f"没找到城市「{city}」")
                loc = data["results"][0]
                geo = {"id": None, "name": loc.get("name"),
                       "lat": _num(loc.get("latitude")), "lon": _num(loc.get("longitude")),
                       "adm": loc.get("admin1") or "", "ts": time.time(), "src": "openmeteo"}
        except Exception as exc:
            logger.warning("%s 城市查询失败（%s）: %s", TAG, city, exc)
            raise
        self._geo_mem[city] = geo
        (self._state.setdefault("geo", {}))[city] = geo
        self._save_state()
        return geo

    async def fetch_days(self, city: str, use_cache: bool = True):
        """取某城市 3 天预报（统一结构）。失败返回 None（调用方决定怎么降级）。"""
        city = str(city or "").strip()
        if not city:
            return None
        ck = city.lower()
        if use_cache:
            hit = self._data_cache.get(ck)
            if hit and time.time() - hit[0] < float(self.config.get("cache_minutes", 30) or 30) * 60:
                return hit[1]
        try:
            geo = await self._geocode(city)
        except Exception:
            return None
        key = str(self.config.get("qweather_key", "") or "").strip()
        prov = str(self.config.get("provider", "auto") or "auto").strip().lower()
        use_q = bool(key) and prov in ("auto", "qweather")
        # 缓存里的坐标可能是「没配和风 key 时」用 Open-Meteo 存下来的（id 为空）。
        # 那种条目直接拿去查和风只会得到 400 missing-parameter，所以先强制重查一次。
        if use_q and not str(geo.get("id") or ""):
            try:
                geo = await self._geocode(city, force=True)
            except Exception:
                pass
        use_q = use_q and bool(str(geo.get("id") or ""))
        display = str(geo.get("name") or city)
        days = None
        if use_q:
            try:
                host = str(self.config.get("qweather_host", "") or "").strip() or "devapi.qweather.com"
                host = re.sub(r"^https?://", "", host).strip("/")
                loc = str(geo.get("id") or "")
                base = {"location": loc, "key": key, "lang": "zh"}
                d3 = await self._http_json(f"https://{host}/v7/weather/3d", base)
                now = await self._http_json(f"https://{host}/v7/weather/now", base)
                # 和风把预警接口换成了 weatheralert/v1/current/{lat}/{lon}，
                # 老的 /v7/warning/now 已废弃，返回 403 deprecated。
                warn = {}
                try:
                    warn = await self._http_json(
                        f"https://{host}/weatheralert/v1/current/{geo.get('lat')}/{geo.get('lon')}",
                        {"key": key, "lang": "zh"},
                    )
                except Exception as exc:
                    logger.info("%s 和风预警取不到（%s），这条按无预警处理", TAG, exc)
                    warn = {}
                if str(d3.get("code")) != "200":
                    raise RuntimeError(f"和风天气查询失败 code={d3.get('code')}")
                # 和风的实时天气嵌在 "now" 里（3d 嵌在 "daily"、预警嵌在 "warning"）
                now_data = (now or {}).get("now") or {} if str((now or {}).get("code")) == "200" else {}
                alerts = warn.get("alerts") or warn.get("warning") or []
                days = normalize_qweather(d3, now_data, display, alerts)
            except Exception as exc:
                logger.warning("%s 和风取数失败，尝试回落 Open-Meteo: %s", TAG, exc)
                days = None
        if days is None:
            try:
                params = {
                    "latitude": geo.get("lat"), "longitude": geo.get("lon"),
                    "timezone": "Asia/Shanghai", "forecast_days": 3,
                    "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code",
                    "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                              "precipitation_probability_max,precipitation_sum,wind_speed_10m_max,"
                              "uv_index_max,sunrise,sunset"),
                    "hourly": "precipitation_probability",
                }
                fc = await self._http_json("https://api.open-meteo.com/v1/forecast", params)
                days = normalize_openmeteo(fc, display)
                # ★ Open-Meteo 偶尔给"退化"的响应：逐时概率整段是 null、
                #   日聚合 probability_max 也很小，但 precipitation_sum 明明 >0
                #   （实测同一地点 5 分钟内两次请求：3%/2.8mm vs 78%/2.3mm）。
                #   遇到这种就重发一次，别把明显自相矛盾的数推给用户。
                if self._weather_inconsistent(days):
                    logger.info("%s Open-Meteo 返回的数据自相矛盾（%s），重取一次",
                                TAG, "、".join("%s %s%%/%.1fmm" % (d.get("date"), d.get("precip_prob") or 0,
                                                                  d.get("precip_mm") or 0)
                                              for d in days[:2]))
                    fc2 = await self._http_json("https://api.open-meteo.com/v1/forecast", params)
                    days2 = normalize_openmeteo(fc2, display)
                    if days2 and not self._weather_inconsistent(days2):
                        days = days2
            except Exception as exc:
                logger.warning("%s Open-Meteo 取数失败: %s", TAG, exc)
                return None
        if days:
            self._data_cache[ck] = (time.time(), days)
        return days

    # ---------- 推送 ----------
    async def _tick(self, slot: str, targets=None, dedupe: bool = True):
        """到点推送：slot='07:00' 这种时刻。时刻早于 evening_after 推今天，否则推明天。"""
        slot = str(slot or "").strip() or dt.datetime.now().strftime("%H:%M")
        if dedupe and not self._claim_once(f"push:{dt.date.today().isoformat()}:{slot}"):
            logger.info("%s %s 今天已推过，跳过（跨实例去重）", TAG, slot)
            return
        try:
            hh = int(slot.split(":")[0])
        except Exception:
            hh = dt.datetime.now().hour
        after = int(self.config.get("evening_after", 18) or 18)
        idx = 0 if hh < after else 1
        targets = list(targets) if targets else self._targets()
        if not targets:
            logger.info("%s 没有推送目标（用 /天气 订阅 订阅，或在配置里填 target_sessions）", TAG)
            return
        alert_only = bool(self.config.get("alert_only", False))
        at_all = bool(self.config.get("at_all", False))
        cache = {}
        cap = self._cfg_int("max_push_cities", 3, 1, 10)
        async with self._lock:
            for umo in targets:
                cities = self._push_cities(umo)
                if not cities:
                    logger.info("%s 跳过 %s：没设城市（/天气 订阅 <城市> 或配 default_city）", TAG, umo)
                    continue
                shown = cities[:cap]
                per_city = []
                for city in shown:
                    if city not in cache:
                        cache[city] = await self.fetch_days(city)
                    days = cache[city]
                    if not days:
                        logger.warning("%s %s：没取到「%s」的天气，这条略过", TAG, umo, city)
                        continue
                    # alert_only：只保留"确实有值得提醒的事"的城市
                    if alert_only and not alert_reasons(days, idx, self.config):
                        continue
                    per_city.append((city, days))
                if not per_city:
                    logger.info("%s %s 没有可推的城市内容（%s）", TAG, slot, "、".join(shown))
                    continue
                text = build_multi_message(per_city, idx, self.config)
                note = ""
                if len(cities) > len(shown):
                    note = (f"（本群共设了 {len(cities)} 个城市，这条只列了前 {len(shown)} 个；"
                            f"想调整用 /天气 移除城市 <城市>）")
                is_group = ":GroupMessage:" in str(umo)
                img_path = await self._render_card(per_city, idx)
                parts = []
                if at_all and is_group:
                    parts.append(AtAll())
                if img_path:
                    parts.append(Image.fromFileSystem(img_path))
                want_text = (img_path is None) or bool(self.config.get("card_with_text", False))
                if want_text:
                    # @全体成员 必须与正文同一个 Plain（单独一段换行不生效）
                    prefix = "\n" if (at_all and is_group and img_path is None) else ""
                    parts.append(Plain(prefix + text + (("\n" + note) if note else "")))
                elif note:
                    parts.append(Plain(note))
                try:
                    ok = await self.context.send_message(umo, MessageChain(parts))
                    logger.info("%s 推送(%s) -> %s: %s（%d 个城市：%s%s）%s", TAG, slot, umo, ok,
                                len(per_city), "、".join(c for c, _d in per_city),
                                "，卡片" if img_path else "，文本",
                                "（群内 @全体成员）" if (at_all and is_group) else "")
                except Exception as exc:
                    logger.error("%s 推送到 %s 失败: %s", TAG, umo, exc)

    # ---------- 指令 ----------
    @staticmethod
    def _tokens(event) -> list:
        raw = re.sub(r"^/+\s*", "", str(getattr(event, "message_str", "") or "").strip())
        parts = [p for p in re.split(r"\s+", raw) if p]
        if parts and parts[0] in ("天气", "weather", "天气预报"):
            parts = parts[1:]
        return parts

    @filter.command("天气", alias={"weather", "天气预报"})
    async def cmd_weather(self, event: AstrMessageEvent, action: str = ""):
        """`/天气 [城市]` 立即查（**所有人**可用）；订阅/退订/测试这类会改全局推送目标的仅管理员。

        权限划分的理由：查天气、设本会话城市是无害的自助操作，谁都能用；
        而「订阅/退订」会改全局的推送目标、「订阅列表」会列出所有会话标识、
        「测试 全部」会往别的会话发消息——这些只给管理员。
        """
        toks = self._tokens(event)
        arg = toks[0].lower() if toks else ""
        umo = event.unified_msg_origin
        admin = False
        try:
            admin = bool(event.is_admin())
        except Exception:
            admin = False
        ADMIN_ONLY = ("订阅", "开", "on", "启用", "退订", "关", "off", "取消",
                      "订阅列表", "列表", "list", "测试", "test",
                      "添加城市", "加城市", "addcity", "移除城市", "删城市", "removecity")
        if arg in ADMIN_ONLY and not admin:
            yield event.plain_result(
                "这条指令只有管理员能用哦～\n"
                "查天气谁都可以：直接发 /天气 ，或 /天气 北京 查指定城市"
            )
            return

        if arg in ("诊断", "检查", "diag"):
            cities = self._session_cities(umo) or self._push_cities(umo)
            if not cities:
                yield event.plain_result("先告诉我城市：/天气 订阅 南京")
                return
            hh = dt.datetime.now().hour
            after = int(self.config.get("evening_after", 18) or 18)
            idx = 0 if hh < after else 1
            cap = self._cfg_int("max_push_cities", 3, 1, 10)
            on = bool(self.config.get("alert_only", False))
            lines = [f"alert_only {'开着' if on else '关着'}，现在算的是{'今天' if idx == 0 else '明天'}"]
            for city in cities[:cap]:
                days = await self.fetch_days(city)
                if not days or idx >= len(days):
                    lines.append(f"{city}：取不到数据")
                    continue
                d = days[idx]
                reasons = alert_reasons(days, idx, self.config)
                prob = d.get("precip_prob")
                mm = d.get("precip_mm") or 0
                lines.append(
                    f"{city} {d.get('date')} {d.get('text_day')} "
                    f"{d.get('tmin')}~{d.get('tmax')}℃ "
                    f"概率{'-' if prob is None else f'{prob:.0f}%'} "
                    f"量{mm:.1f}mm 风{d.get('wind_kmh')}km/h uv{d.get('uv')}"
                )
                warns = d.get("warnings") or []
                if warns:
                    lines.append(f"　预警 {len(warns)} 条：" + "；".join(warns))
                else:
                    lines.append("　预警 0 条")
                plain_reasons = [r for r in reasons if not r.startswith("⚠️")]
                lines.append("　" + ("；".join(plain_reasons) if plain_reasons else "没到阈值，这条不会推"))
            yield event.plain_result("\n".join(lines))
            return

        if arg in ("订阅", "开", "on", "启用"):
            uid_sub = str(event.get_sender_id() or "")
            # 支持一次订阅多个城市：/天气 订阅 上海 北京 广州
            cities = [c for c in (toks[1:] or []) if str(c).strip()]
            if not cities:
                cities = self._session_cities(umo) or self._push_cities(umo)
            if not cities:
                yield event.plain_result("要告诉我城市哦，例如：/天气 订阅 上海（也可以一次写多个：/天气 订阅 上海 北京）")
                return
            cap = self._cfg_int("max_push_cities", 3, 1, 10)
            targets = self._targets()
            already = umo in targets
            if not already:
                targets.append(umo)
                self._save_targets(targets)
            self._set_session_cities(umo, cities)
            if ":FriendMessage:" in str(umo):      # 私聊：顺手把第一个设成本人城市
                self._set_personal_city(uid_sub or self._uid_of_private(umo), cities[0])
            times = [str(t) for t in (self.config.get("push_times") or []) if str(t).strip()]
            head = "推送城市已更新为" if already else "订阅成功"
            extra = ""
            if len(cities) > cap:
                extra = f"\n（一条消息最多列 {cap} 个城市，多余的可以用 /天气 移除城市 调整）"
            yield event.plain_result(
                f"{head}：{'、'.join(cities)}（共 {len(cities)} 个）\n"
                f"推送时刻：{'、'.join(times) if times else '（还没配置 push_times）'}{extra}"
            )
            return

        if arg in ("添加城市", "加城市", "addcity"):
            add = [str(c).strip() for c in (toks[1:] or []) if str(c).strip()]
            if not add:
                yield event.plain_result("用法：/天气 添加城市 北京（可一次写多个）")
                return
            cities = self._session_cities(umo) or self._push_cities(umo)
            for c in add:
                if c not in cities:
                    cities.append(c)
            self._set_session_cities(umo, cities)
            cap = self._cfg_int("max_push_cities", 3, 1, 10)
            yield event.plain_result(
                f"现在推送城市：{'、'.join(cities)}（共 {len(cities)} 个，一条消息最多列 {cap} 个）"
            )
            return

        if arg in ("移除城市", "删城市", "removecity"):
            rm = [str(c).strip() for c in (toks[1:] or []) if str(c).strip()]
            if not rm:
                yield event.plain_result("用法：/天气 移除城市 北京（可一次写多个）")
                return
            left = [c for c in self._session_cities(umo) if c not in rm]
            self._set_session_cities(umo, left)
            yield event.plain_result(
                f"现在推送城市：{'、'.join(left) if left else '（空了，推送时会退回 default_city 或个人城市）'}"
            )
            return

        if arg in ("退订", "关", "off", "取消"):
            targets = self._targets()
            if umo not in targets:
                yield event.plain_result("这个会话本来就没订阅哦")
                return
            targets = [t for t in targets if t != umo]
            self._save_targets(targets)
            yield event.plain_result("已退订每日天气推送")
            return

        if arg in ("订阅列表", "列表", "list"):
            targets = self._targets()
            if not targets:
                yield event.plain_result("还没有任何会话订阅")
                return
            lines = [f"共 {len(targets)} 个会话订阅："]
            for i, u in enumerate(targets, 1):
                cs = self._push_cities(u)
                lines.append(f"{i}. {u}　推送城市：{'、'.join(cs) if cs else '（未设置）'}"
                             f"{'（共 %d 个）' % len(cs) if len(cs) > 1 else ''}")
            yield event.plain_result("\n".join(lines))
            return

        if arg in ("城市", "设置城市"):
            uid_c = str(event.get_sender_id() or "")
            city = " ".join(toks[1:]).strip()
            if not city:
                cur = self._personal_city(uid_c)
                yield event.plain_result(
                    f"你的城市：{cur or '（未设置）'}\n"
                    "改城市：/天气 城市 上海（**每个人各设各的**，群里互不影响）"
                )
                return
            self._set_personal_city(uid_c, city)
            yield event.plain_result(
                f"你的城市已设为「{city}」\n"
                "（这是你个人的设置：同一个群里别人设的城市不会影响你）"
            )
            return

        if arg in ("测试", "test"):
            send_all = len(toks) > 1 and toks[1] in ("全部", "all", "所有")
            targets = self._targets() if send_all else [umo]
            if send_all and not targets:
                yield event.plain_result("还没有任何推送目标（先用 /天气 订阅）")
                return
            if not self._push_city(umo) and not send_all:
                yield event.plain_result("本会话还没设城市：/天气 城市 上海")
                return
            await self._tick(dt.datetime.now().strftime("%H:%M"), targets=targets, dedupe=False)
            yield event.plain_result(
                f"已往{'全部 %d 个目标' % len(targets) if send_all else '本会话'}发了一条，看看收到没"
            )
            return

        if arg in ("帮助", "help", "?"):
            yield event.plain_result(self._help_text())
            return

        # 默认：立即查（可用 /天气 北京 指定城市，不改变订阅）—— 所有人可用
        # 也能带日期：/天气 明天、/天气 9月28日 北京、/天气 北京 后天
        day_off, month_day, rest = parse_day_query(" ".join(toks))
        city = rest.strip() or self._query_city(umo, str(event.get_sender_id() or ""))
        if not city:
            yield event.plain_result(
                "要查哪里呀？用法：\n" + self._help_text()
            )
            return
        # 轻量冷却：防有人刷 /天气 把接口打满（管理员不受限，0 表示关闭）。
        # ★ 按「会话+人」计，不按会话计——否则一个群里 A 查完会把 B 也挡住。
        cd = self._cfg_int("query_cooldown_seconds", 10, 0, 600)
        if not admin and cd:
            ck = "%s#%s" % (umo, str(event.get_sender_id() or ""))
            if time.time() - self._last_query.get(ck, 0.0) < cd:
                yield event.plain_result(f"刚查过啦，{int(cd)} 秒后再试～")
                return
            self._last_query[ck] = time.time()
        # 立即查询默认看"今天"；说了「明天/后天/9月28日」就看那一天（接口只给未来 3 天）
        days = await self.fetch_days(city)
        if not days:
            yield event.plain_result(f"没取到「{city}」的天气，稍后再试（也可能是城市名不认识）")
            return
        idx = resolve_day_index(days, day_off, month_day)
        if idx < 0:
            have = "、".join(
                f"{d.get('date', '')} {d.get('weekday', '')}".strip() for d in days
            )
            want = (
                f"{month_day[0]}月{month_day[1]}日" if month_day
                else (f"{day_off} 天后" if day_off is not None else "")
            )
            yield event.plain_result(
                f"「{city}」只能查到这 {len(days)} 天：{have}\n"
                f"你说的「{want}」不在范围内（和风 3 天预报的上限）"
            )
            return
        img_path = await self._render_card([(city, days)], idx)
        if img_path:
            yield event.image_result(img_path)
            if not bool(self.config.get("card_with_text", False)):
                return
        yield event.plain_result(build_message(days, idx, city, self.config))

    def _cfg_int(self, key: str, default: int, lo: int, hi: int) -> int:
        """读一个整数配置并夹到 [lo, hi]，坏值回退默认。"""
        try:
            val = int(self.config.get(key, default))
        except (TypeError, ValueError):
            val = default
        return max(lo, min(hi, val))

    @staticmethod
    def _help_text() -> str:
        return (
            "每日天气插件（谁都可以用）：\n"
            "/天气 [城市]　— 立即查天气（不改变设置；不填城市就用你自己的城市）\n"
            "/天气 明天 [城市]　— 看明天（也可以 今天 / 后天）\n"
            "/天气 9月28日 [城市]　— 看指定日期（接口只给未来 3 天）\n"
            "/天气 城市 <城市>　— 设你自己的城市（**每个人各设各的**，同一个群里互不影响）\n"
            "/天气 帮助　— 这条帮助\n"
            "\n以下仅管理员可用：\n"
            "/天气 订阅 [城市…]　— 订阅每日推送；**可以写多个城市**（如 /天气 订阅 上海 北京）\n"
            "/天气 添加城市 <城市…>　— 往本会话的推送城市里加\n"
            "/天气 移除城市 <城市…>　— 从本会话的推送城市里去掉\n"
            "/天气 退订　— 取消订阅\n"
            "/天气 订阅列表　— 看订阅了哪些会话\n"
            "/天气 测试 [全部]　— 立刻推一条试试（默认只发当前会话）"
        )
