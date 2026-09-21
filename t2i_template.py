# -*- coding: utf-8 -*-
"""「烛之瘟疫」t2i 播报模板（移植自 point_games 烛之播报模板）

深色卡片风：头部品牌「烛之瘟疫」+ 标题 · 日期；每个群一个独立卡片段，
左侧色条按疫情状态着色（感染红 / 重症深红 / 疑似黄 / 健康绿 / 痊愈蓝）。
数据结构（tmpldata）：
{
  "title": "全服疫情",
  "date": "09月21日 18:30",
  "summary": "感染 3 · 健康 5 · 痊愈 2",
  "event": "病毒变异：……",
  "cards": [
    {"name": "群名", "count": "感染 · 健康 45", "cls": "infected",
     "events": ["🧪 解药进度 40/100", ...]},
  ],
}
"""

ZHUXI_PLAGUE_T2I_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>烛之瘟疫播报</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Regular.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Bold.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/misans@4.1.0/lib/Normal/MiSans-Medium.min.css">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { background: #101725; }
  body {
    width: max-content;
    min-width: 880px;
    background: linear-gradient(160deg, #101725 0%, #16233a 55%, #12303c 100%);
    color: #e8eef5;
    font-family: "MiSans", -apple-system, "PingFang SC", "Noto Sans CJK SC", "Microsoft YaHei", sans-serif;
    font-size: 23px;
    padding: 34px 40px 42px;
  }
  header {
    display: flex; align-items: baseline; justify-content: space-between;
    border-bottom: 2px solid rgba(255,255,255,.14);
    padding-bottom: 16px; margin-bottom: 22px;
  }
  .brand { font-size: 34px; font-weight: 700; letter-spacing: .04em; color: #ffd882; }
  .brand .dot { color: #ff7a70; }
  .sub { font-size: 19px; color: rgba(232,238,245,.55); }
  .notice {
    font-size: 20px; color: #ffd882; line-height: 1.7;
    background: rgba(255,216,130,.08);
    border: 1px solid rgba(255,216,130,.22);
    border-radius: 12px; padding: 12px 20px; margin-bottom: 22px;
    max-width: 880px; white-space: normal;
  }
  .notice .no-event { color: rgba(232,238,245,.5); }
  .card {
    background: rgba(255,255,255,.055);
    border: 1px solid rgba(255,255,255,.09);
    border-left: 4px solid #6fd3ff;
    border-radius: 14px;
    padding: 18px 24px 16px;
    margin-bottom: 20px;
    min-width: 820px;
  }
  .card.st-infected  { border-left-color: #f85149; }
  .card.st-severe    { border-left-color: #b6203a; }
  .card.st-suspect   { border-left-color: #d29922; }
  .card.st-healthy   { border-left-color: #3fb950; }
  .card.st-cured     { border-left-color: #58a6ff; }
  .name {
    font-size: 26px; font-weight: 700; color: #ffd882;
    margin-bottom: 10px; white-space: nowrap;
  }
  .count {
    font-size: 17px; font-weight: 400; color: rgba(232,238,245,.5);
    margin-left: 12px;
  }
  .count.st-infected  { color: #ff8f88; }
  .count.st-severe    { color: #ff7a95; }
  .count.st-suspect   { color: #e3b341; }
  .count.st-healthy   { color: #7ee787; }
  .count.st-cured     { color: #79c0ff; }
  ul { list-style: none; }
  li {
    font-size: 22px; line-height: 1.9; white-space: nowrap;
    color: #dfe9f2; padding-left: 26px; position: relative;
  }
  li::before {
    content: ""; position: absolute; left: 2px; top: 50%;
    width: 9px; height: 9px; margin-top: -4px;
    border-radius: 50%; background: #6fd3ff; opacity: .8;
  }
</style>
</head>
<body>
<header>
  <span class="brand"><span class="dot">烛</span>之瘟疫</span>
  <span class="sub">{{ title }} · {{ date }}</span>
</header>
<main>
  <div class="notice">
    {% if event %}📢 今日事件：{{ event }}{% else %}<span class="no-event">今日暂无全局事件</span>{% endif %}
    {% if summary %}<br>📊 {{ summary }}{% endif %}
  </div>
{%- for c in cards %}
  <section class="card st-{{ c.cls }}">
    <div class="name">{{ c.name }}<span class="count st-{{ c.cls }}">{{ c.count }}</span></div>
    <ul>
    {%- for e in c.events %}
      <li>{{ e }}</li>
    {%- endfor %}
    </ul>
  </section>
{%- endfor %}
</main>
</body>
</html>
"""

# t2i 直连端点（trust_env=False 先行，绕过系统代理）
T2I_DIRECT_ENDPOINTS = ["https://t2i.soulter.top/text2img"]


async def render_t2i_direct(tmpl: str, data: dict) -> bytes:
    """绕过系统代理直连官方 t2i 端点渲染，返回图片字节。

    依次尝试各端点 × trust_env(False, True)；失败抛最后一个异常。
    """
    try:
        import aiohttp
    except Exception:
        raise RuntimeError("aiohttp 不可用，无法直连 t2i")
    post = {
        "tmpl": tmpl, "json": True, "tmpldata": data,
        "options": {"full_page": True, "type": "jpeg", "quality": 70},
    }
    last_exc = None
    for ep in T2I_DIRECT_ENDPOINTS:
        for trust_env in (False, True):
            try:
                async with aiohttp.ClientSession(trust_env=trust_env) as session:
                    async with session.post(
                        f"{ep}/generate", json=post,
                        headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=90),
                    ) as resp:
                        if resp.status != 200:
                            raise RuntimeError(f"HTTP {resp.status}")
                        ret = await resp.json()
                    img_url = f"{ep}/{ret['data']['id']}"
                    async with session.get(
                        img_url, headers={"User-Agent": "AstrBot/t2i"},
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as img_resp:
                        if img_resp.status != 200:
                            raise RuntimeError(f"HTTP {img_resp.status}")
                        raw = await img_resp.read()
                if raw:
                    return raw
                raise RuntimeError("t2i 返回空图片")
            except Exception as e:
                last_exc = e
    raise last_exc or RuntimeError("t2i 直连渲染失败")


def _status_cls(g: dict) -> str:
    if g.get("is_false_alarm") and g.get("status") == "healthy":
        return "suspect"
    return g.get("status") or "healthy"


_STATUS_CN = {
    "healthy": "健康", "suspect": "疑似感染", "infected": "感染",
    "severe": "重症", "cured": "痊愈",
}


def _fmt_dur(seconds) -> str:
    if not seconds:
        return "-"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(seconds // 60, 0)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def build_world_tmpldata(world: dict, date_str: str) -> dict:
    """全服疫情 -> t2i 模板数据"""
    counts = world.get("counts", {})
    cards = []
    for g in world.get("groups", []):
        cls = _status_cls(g)
        label = _STATUS_CN.get(cls, cls)
        health = g.get("health")
        health = 100 if health is None else health
        events = [f"❤️ 健康值 {health}/100"]
        prog = g.get("antidote_progress") or 0
        if g.get("status") in ("infected", "severe"):
            events.append(f"🧪 解药进度 {min(prog, 100)}/100")
        if g.get("infected_at"):
            dur = _fmt_dur(_now() - g["infected_at"])
            events.append(f"⏱ 已感染 {dur}")
        if g.get("quarantined_until") and g["quarantined_until"] > _now():
            events.append("🛡️ 隔离中")
        if g.get("immunity_until") and g["immunity_until"] > _now():
            events.append("💙 免疫期")
        cards.append({
            "name": g.get("group_name") or g.get("group_id") or "?",
            "count": f"{label} · 健康 {health}",
            "cls": cls, "events": events,
        })
    return {
        "title": f"全服疫情（第 {world.get('day_count', 0)} 天）",
        "date": date_str,
        "summary": (f"感染 {counts.get('infected', 0) + counts.get('severe', 0)} · "
                    f"健康 {counts.get('healthy', 0)} · 痊愈 {counts.get('cured', 0)} · "
                    f"今日新增 {world.get('new_today', 0)} 例"),
        "event": world.get("current_event") or "",
        "cards": cards,
    }


def build_card_tmpldata(data: dict, date_str: str) -> dict:
    """本群疫情卡片 -> t2i 模板数据"""
    g = data.get("group") or {}
    label = data.get("label") or "健康"
    cls = g.get("status") or "healthy"
    health = g.get("health")
    health = 100 if health is None else health
    prog = min(g.get("antidote_progress") or 0, 100)

    events = [f"❤️ 健康值 {health}/100", f"🧪 解药进度 {prog}/100"]
    if data.get("infected_days") is not None:
        events.append(f"⏱ 已感染 {data['infected_days']} 天")
    if data.get("quarantined"):
        remain_h = max(0, ((g.get("quarantined_until") or 0) - _now()) // 3600)
        events.append(f"🛡️ 隔离中（剩余约 {remain_h} 小时）")
    if data.get("immune"):
        remain_d = max(0, ((g.get("immunity_until") or 0) - _now()) // 86400)
        events.append(f"💙 免疫期剩余约 {remain_d} 天")
    if data.get("opted_out"):
        events.append("⚠️ 本群已退出瘟疫模拟")

    cards = [{
        "name": g.get("group_name") or "本群",
        "count": label, "cls": cls, "events": events,
    }]

    contribs = data.get("contributors", [])
    ev2 = []
    if contribs:
        for i, c in enumerate(contribs):
            uid = str(c.get("user_id", "?"))
            masked = uid[:3] + "****" + uid[-2:] if len(uid) > 6 else uid
            medal = ["🥇", "🥈", "🥉"][i] if i < 3 else f"{i + 1}."
            ev2.append(f"{medal} {masked}：贡献 {c.get('total', 0)} 点")
    else:
        ev2.append("暂无贡献，快用 /瘟疫研发 投入解药研究！")
    cards.append({
        "name": "解药贡献榜", "count": "Top5", "cls": "cured", "events": ev2,
    })
    return {
        "title": "本群疫情卡片",
        "date": date_str,
        "summary": "",
        "event": "",
        "cards": cards,
    }


def _now() -> int:
    import time
    return int(time.time())
