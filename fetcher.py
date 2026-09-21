# -*- coding: utf-8 -*-
"""astrbot_plugin_cross_plague — LLM 文案生成 + 数据聚合（LLM 失败降级本地模板）"""

import asyncio
import random
from typing import Any, Awaitable, Callable, Dict, Optional

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("cross_plague.fetcher")


# ---------------- 本地降级模板 ----------------

_TPL_NOTICE = [
    "【疫情快报】{to_name} 出现首例感染者！疫情据信由跨群流动人员带入。"
    "该群健康值开始下降，请群友注意防护，尽快投入解药研发！",
    "【紧急通报】病毒已突破防线进入 {to_name}！群内咳嗽声此起彼伏（也可能是空气太干）。"
    "请群友注意防护，戴上口罩（ metaphor 意义上），一起研发解药！",
    "【突发】{to_name} 中招了！匿名用户把病毒当伴手礼捎了过来。"
    "解药研发刻不容缓，请群友注意防护！",
]

_TPL_NOTICE_SUPER = [
    "【红色警报】超级传播者出没！{to_name} 被一举击穿，感染来势汹汹。"
    "请群友注意防护，别慌，解药就在我们手中！",
    "【最高级别疫情】一位移动的病毒库路过，{to_name} 当场沦陷。"
    "请群友注意防护，并立刻开始解药研发！",
]

_TPL_EVENT = {
    "病毒变异": "【全球快讯】病毒发生变异，所有感染群健康值应声下跌。科学家警告：研发速度必须更快。请群友注意防护。",
    "疫苗突破": "【重大喜讯】某感染群解药研发取得突破性进展，进度大幅提升！黎明就在眼前。请群友注意防护，继续加油。",
    "群体免疫": "【奇迹发生】一个感染群在无解药的情况下实现群体免疫，全员痊愈！这是希望的信号。请群友注意防护。",
    "封城": "【防疫升级】一个感染群宣布当日封城，暂停一切对外往来。以空间换时间。请群友注意防护。",
    "谣言四起": "【辟谣进行时】某健康群被误报为感染，实为虚惊一场。权威机构呼吁：不信谣不传谣。请群友注意防护。",
}

_TPL_REPORT = (
    "瘟疫第 {day_count} 天 · 全服日报\n"
    "当前感染群 {infected_count} 个，累计痊愈 {cured_count} 个，今日新增感染 {new_infections} 例。\n"
    "今日事件：{today_event}\n"
    "解药贡献榜第一：某位热心群友（贡献 {top_amount} 点）。\n"
    "疫情仍在蔓延，但希望的火种没有熄灭——快召唤你的群友，一起研发解药！"
)

_CURE_BROADCAST = [
    "【全服喜报】🎉 {group_name} 成功研发出解药，全员痊愈！从感染到痊愈，他们证明了团结就是免疫力。",
    "【痊愈通报】🎉 解药在 {group_name} 正式生效！又一个群走出瘟疫阴影，全服为之振奋。",
]


class TextGen:
    """LLM 文案生成。LLM 调用失败或超时时，降级为本地随机模板。"""

    def __init__(self, provider_getter: Callable[[], Any]):
        self._provider_getter = provider_getter

    # ---------- 底层 LLM 调用 ----------

    async def _chat(self, prompt: str, timeout: float = 25.0) -> Optional[str]:
        try:
            provider = self._provider_getter()
            if provider is None:
                return None
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=prompt, session_id="cross_plague_gen"),
                timeout=timeout,
            )
            if resp is None:
                return None
            for attr in ("result_text", "completion", "text", "message"):
                val = getattr(resp, attr, None)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            # 兼容 dict 形式
            if isinstance(resp, dict):
                for key in ("result_text", "text", "content"):
                    if str(resp.get(key, "")).strip():
                        return str(resp[key]).strip()
            return None
        except asyncio.TimeoutError:
            logger.warning("[cross_plague] LLM 文案生成超时，降级本地模板")
        except Exception as e:
            logger.warning("[cross_plague] LLM 文案生成失败: %s，降级本地模板", e)
        return None

    # ---------- 对外接口 ----------

    async def notice_text(self, from_name: str, to_name: str,
                          is_super: bool) -> str:
        prompt = (
            "你是瘟疫模拟游戏的主持人。请用 50 字以内、新闻播报风格的文案，描述以下事件：\n"
            f"- 来源群：{from_name}\n"
            f"- 被感染群：{to_name}\n"
            "- 传播者：匿名用户\n"
            f"- 是否超级传播者：{'是' if is_super else '否'}\n"
            "要求：1. 语气紧张但不恐怖，带点黑色幽默；2. 不要出现真实用户名；"
            "3. 结尾加一句\"请群友注意防护\"。只输出文案本身。"
        )
        text = await self._chat(prompt)
        if text:
            return text
        tpl = _TPL_NOTICE_SUPER if is_super else _TPL_NOTICE
        return random.choice(tpl).format(to_name=to_name)

    async def event_text(self, event_type: str, detail: str = "") -> str:
        prompt = (
            "你是瘟疫模拟游戏的主持人。今天的全球事件是：" + event_type + "\n"
            "请用 80 字以内、新闻稿风格描述这个事件对全服的影响。\n"
            "事件类型说明：\n"
            "- 病毒变异：所有感染群健康值下降\n"
            "- 疫苗突破：随机一个感染群解药进度提升\n"
            "- 群体免疫：随机一个感染群直接痊愈\n"
            "- 封城：随机一个感染群当天不传播\n"
            "- 谣言四起：随机一个健康群被误判为感染\n"
            f"本次事件详情：{detail or '无'}\n"
            "只输出文案本身，不要出现真实用户名。"
        )
        text = await self._chat(prompt)
        if text:
            return text
        return _TPL_EVENT.get(event_type, f"【全球快讯】今日事件：{event_type}。{detail} 请群友注意防护。")

    async def cure_text(self, group_name: str) -> str:
        prompt = (
            "你是瘟疫模拟游戏的主持人。请用 60 字以内、新闻播报风格的文案，"
            f"庆祝群 \"{group_name}\" 解药研发成功、全群痊愈并获得 7 天免疫。"
            "带点喜庆和黑色幽默，不要出现真实用户名。只输出文案本身。"
        )
        text = await self._chat(prompt)
        if text:
            return text
        return random.choice(_CURE_BROADCAST).format(group_name=group_name)

    async def report_text(self, stats: Dict[str, Any]) -> str:
        prompt = (
            "你是瘟疫模拟游戏的战地记者。请根据以下数据写一份 200 字以内的瘟疫日报：\n"
            f"- 瘟疫第 {stats.get('day_count', 1)} 天\n"
            f"- 感染群数：{stats.get('infected_count', 0)}\n"
            f"- 痊愈群数：{stats.get('cured_count', 0)}\n"
            f"- 今日新增感染：{stats.get('new_infections', 0)}\n"
            f"- 今日事件：{stats.get('today_event', '无')}\n"
            f"- 贡献最多的用户：{stats.get('top_contributor', '暂无')}"
            f"（贡献 {stats.get('top_amount', 0)} 点）\n"
            "要求：1. 新闻播报风格，带数据感和紧迫感；"
            "2. 结尾加一句鼓励群友合作研发解药的话；"
            "3. 不要出现真实用户名，用\"某位热心群友\"代替。只输出日报正文。"
        )
        text = await self._chat(prompt)
        if text:
            return text
        return _TPL_REPORT.format(
            day_count=stats.get("day_count", 1),
            infected_count=stats.get("infected_count", 0),
            cured_count=stats.get("cured_count", 0),
            new_infections=stats.get("new_infections", 0),
            today_event=stats.get("today_event", "无"),
            top_contributor=stats.get("top_contributor", "暂无"),
            top_amount=stats.get("top_amount", 0),
        )

    async def patient_zero_text(self, group_name: str) -> str:
        prompt = (
            "你是瘟疫模拟游戏的主持人。今日\"零号病人\"出现在群 \"" + group_name + "\"。"
            "请用 50 字以内、新闻播报风格宣布疫情开始，带点黑色幽默，"
            "结尾加一句\"请群友注意防护\"。只输出文案本身。"
        )
        text = await self._chat(prompt)
        if text:
            return text
        return (f"【疫情爆发】零号病人在 {group_name} 确诊，瘟疫正式开始蔓延！"
                "解药尚未面世，请群友注意防护。")
