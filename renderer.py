# -*- coding: utf-8 -*-
"""astrbot_plugin_cross_plague — Pillow 渲染：全服疫情地图 / 本群疫情卡片（2x，宽 1200px）"""

import base64
import glob
import os
import time
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

# ---------------- 配色（深色主题） ----------------

BG = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TXT_MAIN = "#e6edf3"
TXT_SUB = "#8b949e"
GOLD = "#e3b341"

STATUS_COLOR = {
    "healthy": "#3fb950",    # 绿
    "suspect": "#d29922",    # 黄（疑似感染）
    "infected": "#f85149",   # 红
    "severe": "#8b1a1a",     # 深红
    "cured": "#58a6ff",      # 蓝
}

BAR_BG = "#21262d"
BAR_GREEN = "#3fb950"
BAR_RED = "#f85149"
BAR_BLUE = "#58a6ff"

STATUS_LABEL = {
    "healthy": "健康", "suspect": "疑似感染", "infected": "感染",
    "severe": "重症", "cured": "痊愈",
}

_FONT_CACHE: Dict[tuple, ImageFont.FreeTypeFont] = {}
_FONT_INDEX: Optional[List[str]] = None


def _font_file_index() -> List[str]:
    global _FONT_INDEX
    if _FONT_INDEX is not None:
        return _FONT_INDEX
    patterns = []
    # 插件自带 fonts/ 优先
    plugin_fonts = os.path.join(os.path.dirname(__file__), "fonts", "*")
    patterns.append(plugin_fonts)
    keywords = ["wqy", "noto*cjk", "noto*sans*cjk", "source*han*sans",
                "misans", "msyh*", "simhei", "simsun", "pingfang", "dengxian"]
    for kw in keywords:
        patterns.append(f"/usr/share/fonts/**/*{kw}*")
    files: List[str] = []
    for p in patterns:
        try:
            files.extend(glob.glob(p, recursive=True))
        except Exception:
            pass
    files = sorted({f for f in files if f.lower().endswith(
        (".ttf", ".ttc", ".otf"))})
    _FONT_INDEX = files
    return files


def _font(size: int):
    key = ("cjk", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    for path in _font_file_index():
        try:
            f = ImageFont.truetype(path, size)
            _FONT_CACHE[key] = f
            return f
        except Exception:
            continue
    try:
        f = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
        _FONT_CACHE[key] = f
        return f
    except Exception:
        f = ImageFont.load_default()
        _FONT_CACHE[key] = f
        return f


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except Exception:
        return len(text) * font.size


def _truncate(draw, text: str, font, max_w: int) -> str:
    if _text_w(draw, text, font) <= max_w:
        return text
    while text and _text_w(draw, text + "…", font) > max_w:
        text = text[:-1]
    return text + "…"


def _fmt_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(seconds // 60, 0)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def _group_color(g: Dict[str, Any]) -> str:
    if g.get("is_false_alarm") and g.get("status") == "healthy":
        return STATUS_COLOR["suspect"]
    return STATUS_COLOR.get(g.get("status", "healthy"), STATUS_COLOR["healthy"])


def _group_label(g: Dict[str, Any]) -> str:
    if g.get("is_false_alarm") and g.get("status") == "healthy":
        return STATUS_LABEL["suspect"]
    return STATUS_LABEL.get(g.get("status", "healthy"), "未知")


# ================= 全服疫情地图 =================

def render_map(data: Dict[str, Any]) -> Optional[bytes]:
    """
    data: {day_count, current_event, counts: {healthy, infected, severe, cured},
           groups: [group rows], new_today}
    """
    try:
        W = 1200
        groups: List[Dict[str, Any]] = data.get("groups", [])
        cols = 4
        rows = max(1, (len(groups) + cols - 1) // cols)
        header_h = 220
        cell_w, cell_h = 280, 150
        grid_w = cols * cell_w + 40
        H = header_h + rows * cell_h + 120

        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)

        # 顶部
        d.text((40, 30), f"瘟疫第 {data.get('day_count', 0)} 天",
               font=_font(52), fill=GOLD)
        d.text((40, 104), "全服疫情地图 · Cross Plague",
               font=_font(28), fill=TXT_SUB)
        c = data.get("counts", {})
        stats_line = (f"感染 {c.get('infected', 0) + c.get('severe', 0)} · "
                      f"健康 {c.get('healthy', 0)} · 痊愈 {c.get('cured', 0)} · "
                      f"今日新增 {data.get('new_today', 0)}")
        d.text((W - 40 - _text_w(d, stats_line, _font(28)), 44),
               stats_line, font=_font(28), fill=TXT_MAIN)
        event = f"今日事件：{_truncate(d, data.get('current_event') or '暂无', _font(26), W - 120)}"
        d.text((40, 150), event, font=_font(26), fill="#d29922")
        d.line([(40, 200), (W - 40, 200)], fill=BORDER, width=2)

        # 图例
        lx = 40
        for key in ("healthy", "suspect", "infected", "severe", "cured"):
            d.ellipse([lx, 208, lx + 16, 224], fill=STATUS_COLOR[key])
            d.text((lx + 24, 206), STATUS_LABEL[key], font=_font(22), fill=TXT_SUB)
            lx += 24 + _text_w(d, STATUS_LABEL[key], _font(22)) + 30

        # 群点位
        f_name = _font(26)
        f_sub = _font(21)
        for i, g in enumerate(groups):
            r, col = divmod(i, cols)
            x0 = 20 + col * cell_w
            y0 = header_h + 40 + r * cell_h
            color = _group_color(g)
            label = _group_label(g)
            name = g.get("group_name") or "未知群"
            d.rounded_rectangle([x0, y0, x0 + cell_w - 24, y0 + cell_h - 30],
                                radius=14, fill=PANEL, outline=BORDER, width=1)
            d.ellipse([x0 + 18, y0 + 22, x0 + 38, y0 + 42], fill=color)
            d.text((x0 + 50, y0 + 16),
                   _truncate(d, name, f_name, cell_w - 110),
                   font=f_name, fill=TXT_MAIN)
            health = g.get("health") if g.get("health") is not None else 100
            d.text((x0 + 50, y0 + 56),
                   f"{label} · 健康 {health}", font=f_sub, fill=TXT_SUB)
            # 健康值进度条
            bx0, bx1 = x0 + 18, x0 + cell_w - 42
            by = y0 + cell_h - 58
            d.rounded_rectangle([bx0, by, bx1, by + 12], radius=6, fill=BAR_BG)
            frac = max(0, min(100, health)) / 100
            if frac > 0:
                bar_color = BAR_GREEN if frac > 0.5 else (GOLD if frac > 0.25 else BAR_RED)
                d.rounded_rectangle([bx0, by, bx0 + int((bx1 - bx0) * frac), by + 12],
                                    radius=6, fill=bar_color)

        buf = _to_png(img)
        return buf
    except Exception as e:
        try:
            from astrbot.api import logger
            logger.error(f"[cross_plague] 渲染疫情地图失败: {e}")
        except Exception:
            pass
        return None


# ================= 本群疫情卡片 =================

def render_card(data: Dict[str, Any]) -> Optional[bytes]:
    """
    data: {group_name, label, status, health, progress, infected_days,
           quarantined, immune, contributors: [{user_id, total}]}
    """
    try:
        W, H = 1200, 780
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)

        name = data.get("group_name") or "本群"
        label = data.get("label") or "健康"
        status_key = data.get("status") or "healthy"
        if data.get("quarantined"):
            label += " · 隔离中"
        if data.get("immune"):
            label += " · 免疫期"
        color = STATUS_COLOR.get(status_key, STATUS_COLOR["healthy"])

        d.rounded_rectangle([30, 30, W - 30, H - 30], radius=20,
                            fill=PANEL, outline=BORDER, width=2)
        d.text((70, 64), _truncate(d, name, _font(48), W - 400),
               font=_font(48), fill=TXT_MAIN)
        d.rounded_rectangle([W - 320, 66, W - 70, 122], radius=28,
                            fill=BG, outline=color, width=3)
        lw = _text_w(d, label, _font(30))
        d.text((W - 195 - lw // 2, 76), label, font=_font(30), fill=color)

        # 健康值
        health = data.get("health")
        health = 100 if health is None else health
        d.text((70, 180), "健康值", font=_font(28), fill=TXT_SUB)
        _bar(d, 70, 224, W - 70, health / 100,
             BAR_GREEN if health > 50 else (GOLD if health > 25 else BAR_RED))
        d.text((W - 170, 180), f"{health}/100", font=_font(28), fill=TXT_MAIN)

        # 解药进度
        prog = data.get("progress") or 0
        d.text((70, 300), "解药研发进度", font=_font(28), fill=TXT_SUB)
        _bar(d, 70, 344, W - 70, prog / 100, BAR_BLUE)
        d.text((W - 170, 300), f"{min(prog, 100)}/100", font=_font(28), fill=TXT_MAIN)

        # 感染时长等元信息
        days = data.get("infected_days")
        meta = f"感染时长：{_fmt_duration(days * 86400) if days is not None else '未感染'}"
        d.text((70, 420), meta, font=_font(26), fill=TXT_SUB)

        # 贡献者 Top5
        d.text((70, 470), "解药贡献榜 Top5", font=_font(30), fill=GOLD)
        d.line([(70, 516), (W - 70, 516)], fill=BORDER, width=1)
        contributors = data.get("contributors", [])
        f_c = _font(26)
        if not contributors:
            d.text((70, 536), "暂无贡献，快用 /瘟疫研发 投入解药研究吧！",
                   font=f_c, fill=TXT_SUB)
        else:
            for i, c in enumerate(contributors):
                y = 536 + i * 40
                uid = str(c.get("user_id", "?"))
                masked = uid[:3] + "****" + uid[-2:] if len(uid) > 6 else uid
                medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."
                d.text((70, y), medal, font=f_c, fill=TXT_MAIN)
                d.text((140, y), masked, font=f_c, fill=TXT_MAIN)
                total = str(c.get("total", 0))
                d.text((W - 70 - _text_w(d, total, f_c), y), total,
                       font=f_c, fill=BAR_BLUE)

        d.text((70, H - 70), f"生成于 {time.strftime('%Y-%m-%d %H:%M')}",
               font=_font(20), fill=TXT_SUB)

        return _to_png(img)
    except Exception as e:
        try:
            from astrbot.api import logger
            logger.error(f"[cross_plague] 渲染疫情卡片失败: {e}")
        except Exception:
            pass
        return None


def _bar(d: ImageDraw.ImageDraw, x0: int, y: int, x1: int,
         frac: float, color: str) -> None:
    frac = max(0.0, min(1.0, frac))
    d.rounded_rectangle([x0, y, x1, y + 26], radius=13, fill=BAR_BG)
    if frac > 0:
        d.rounded_rectangle([x0, y, x0 + int((x1 - x0) * frac), y + 26],
                            radius=13, fill=color)


def _to_png(img: Image.Image) -> bytes:
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
