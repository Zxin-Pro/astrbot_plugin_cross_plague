# -*- coding: utf-8 -*-
"""astrbot_plugin_cross_plague — 跨群瘟疫模拟游戏（插件入口）"""

import asyncio
import base64
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

try:  # 三级兼容导入：astrbot.api → astrbot.api.star → astrbot.core.star
    from astrbot.api import Star, logger
except ImportError:
    try:
        from astrbot.api.star import Star
        from astrbot.api import logger
    except ImportError:
        from astrbot.core.star import Star, Context
        import logging

        logger = logging.getLogger("cross_plague")

try:
    from astrbot.api.event import AstrMessageEvent, filter
except ImportError:
    from astrbot.core.platform.astr_message_event import AstrMessageEvent
    from astrbot.api import filter

try:
    from astrbot.api.event import EventMessageType
except ImportError:
    try:
        from astrbot.api.event.filter import EventMessageType
    except ImportError:
        from astrbot.core.platform.message_type import EventMessageType

try:
    from astrbot.api.star import register
except ImportError:
    from astrbot.api import register

try:
    from astrbot.core.message.message_event_result import MessageChain
except ImportError:
    from astrbot.api.event import MessageChain

try:
    from astrbot.api.message_components import Image
except ImportError:
    from astrbot.core.message.components import Image

from .database import Database
from .fetcher import TextGen
from .plague import PlagueCore, _day_start, _hm_to_seconds, _today_str
from .renderer import render_card, render_map
from .t2i_template import (
    ZHUXI_PLAGUE_T2I_TEMPLATE,
    build_card_tmpldata,
    build_world_tmpldata,
    render_t2i_direct,
)


def _data_dir() -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    os.makedirs(d, exist_ok=True)
    return d


@register("astrbot_plugin_cross_plague", "Zxin_Pro",
          "跨群瘟疫模拟游戏：感染随群友跨群发言传播，群友合作研发解药",
          "v1.0.3",
          "https://github.com/Zxin-Pro/astrbot_plugin_cross_plague")
class CrossPlaguePlugin(Star):
    def __init__(self, context: Any, config: Any = None):
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        self._cfg_cache: Dict[str, Any] = {}
        self.db: Optional[Database] = None
        self.core: Optional[PlagueCore] = None
        self.textgen: Optional[TextGen] = None
        self._sched_task: Optional[asyncio.Task] = None
        self._running = False
        self._last_flush = 0.0

    # ================= 配置 =================

    def _cfg(self, key: str, default=None):
        if key in self._cfg_cache:
            return self._cfg_cache[key]
        v = None
        if isinstance(self.config, dict) and key in self.config:
            v = self.config[key]
        else:
            try:  # 兼容：context.get_config()
                gc = self.context.get_config()
                if isinstance(gc, dict):
                    v = gc.get(key, None)
            except Exception:
                pass
        if v is None or v == "":
            v = default
        self._cfg_cache[key] = v
        return v

    def _invalidate_cfg(self) -> None:
        self._cfg_cache.clear()

    # ================= 生命周期 =================

    async def initialize(self):
        self._invalidate_cfg()
        self.db = Database(os.path.join(_data_dir(), "cross_plague.db"))
        await self.db.init()
        self.textgen = TextGen(lambda: self._get_provider())
        self.core = PlagueCore(self.db, self._all_cfg(), self.textgen,
                               self._send_text_to_group,
                               fetch_name=self._fetch_group_name_api)
        await self.core.load()
        self._running = True
        self._sched_task = asyncio.create_task(self._scheduler_loop())
        logger.info("[cross_plague] 插件加载完成 v1.0.3")

    async def terminate(self):
        self._running = False
        if self._sched_task:
            self._sched_task.cancel()
            try:
                await self._sched_task
            except (asyncio.CancelledError, Exception):
                pass
            self._sched_task = None
        if self.core:
            try:
                await self.core.flush_speak_cache()
            except Exception:
                pass
        if self.db:
            await self.db.close()
        logger.info("[cross_plague] 插件已卸载，资源已释放")

    def _get_provider(self):
        try:
            return self.context.get_using_provider()
        except Exception:
            return None

    def _all_cfg(self) -> Dict[str, Any]:
        return {
            "auto_start_enabled": self._cfg("auto_start_enabled", True),
            "auto_start_time": self._cfg("auto_start_time", "10:00"),
            "daily_report_time": self._cfg("daily_report_time", "21:00"),
            "push_target": self._cfg("push_target", ""),
            "spread_cooldown_hours": self._cfg("spread_cooldown_hours", 1),
            "enable_super_spreader": self._cfg("enable_super_spreader", True),
            "notify_daily_limit": self._cfg("notify_daily_limit", 3),
            "speaking_research_chance": self._cfg("speaking_research_chance", 8),
            "immunity_days": self._cfg("immunity_days", 7),
        }

    # ================= 消息发送 =================

    def _resolve_umo(self, group_id: str) -> Optional[str]:
        """群号 -> unified_msg_origin（遍历平台实例，优先 aiocqhttp）"""
        if ":" in str(group_id):
            return str(group_id)  # 已是 UMO
        try:
            insts = self.context.platform_manager.get_insts()
            insts = sorted(insts, key=lambda i: 0 if "aiocqhttp" in i.meta().name else 1)
            for inst in insts:
                try:
                    name = inst.meta().name
                except Exception:
                    name = ""
                return f"{name}:GroupMessage:{group_id}"
        except Exception as e:
            logger.warning("[cross_plague] 解析平台实例失败: %s", e)
        return None

    async def _send_text_to_group(self, group_id: str, text: str) -> None:
        umo = self._resolve_umo(group_id)
        if not umo:
            logger.warning("[cross_plague] 无法解析群 %s 的 UMO，通知丢弃", group_id)
            return
        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)
        logger.info("[cross_plague] 已推送通知到群 %s", group_id)

    # ================= 定时调度（单循环，30s 一跳） =================

    async def _scheduler_loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[cross_plague] 调度循环异常: {e}")
            await asyncio.sleep(30)

    async def _tick(self):
        now = int(time.time())
        state = await self.db.get_state()
        today = _today_str(now)
        secs_of_day = now - _day_start(now)

        # 1) 用户跨群行为批量落库（30s 一次）
        if now - self._last_flush >= 30:
            self._last_flush = now
            await self.core.flush_speak_cache()

        # 2) 每日零号病人投放
        if self._cfg("auto_start_enabled", True):
            start_hm = self._cfg("auto_start_time", "10:00")
            if (state.get("last_reset_date") != today
                    and secs_of_day >= _hm_to_seconds(start_hm, (10, 0))):
                victim = await self.core.daily_reset()
                if victim:
                    try:
                        name = self.core._groups.get(victim, {}).get("group_name", victim)
                        text = await self.textgen.patient_zero_text(name)
                        await self._send_text_to_group(victim, text)
                    except Exception as e:
                        logger.warning(f"[cross_plague] 零号通知失败: {e}")
                self._cfg_cache.clear()

        # 3) 每日事件（随机排期时间触发，每日一次）
        ev_hm = state.get("event_scheduled_hm")
        if (ev_hm and state.get("event_fired_date") != today
                and secs_of_day >= _hm_to_seconds(ev_hm, (15, 0))):
            await self.core.fire_daily_event()

        # 4) 健康值衰减（每小时）
        last_decay = state.get("last_decay_at") or 0
        if now - last_decay >= 3600:
            await self.core.hourly_decay()
            # 顺带清理过期谣言
            cleared = await self.core.clear_false_alarms()
            if cleared:
                for gid in cleared:
                    try:
                        await self._send_text_to_group(
                            gid, "【辟谣通报】本群 24 小时前的感染警报被证实为谣言，"
                                 "病毒检测结果一切正常。请继续做好防护～")
                    except Exception:
                        pass

        # 5) 每日日报推送
        report_hm = self._cfg("daily_report_time", "21:00")
        if (state.get("last_report_date") != today
                and secs_of_day >= _hm_to_seconds(report_hm, (21, 0))):
            await self._send_daily_report()
            await self.db.update_state(last_report_date=today)

    async def _send_daily_report(self):
        try:
            stats = await self.core.daily_report_stats()
            text = await self.textgen.report_text(stats)
            push_target = str(self._cfg("push_target", "") or "")
            targets: List[str] = []
            if push_target.strip():
                targets = [g.strip() for g in push_target.split(",") if g.strip()]
            else:
                now = int(time.time())
                targets = [gid for gid, g in self.core._groups.items()
                           if not g.get("opted_out")
                           and (g.get("last_active_at") or 0) > now - 7 * 86400]
            for gid in targets:
                try:
                    await self._send_text_to_group(gid, f"📰 瘟疫日报\n{text}")
                except Exception as e:
                    logger.warning(f"[cross_plague] 日报推送群 {gid} 失败: {e}")
        except Exception as e:
            logger.error(f"[cross_plague] 日报生成失败: {e}")

    # ================= 群消息监听 =================

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        try:
            group_id = self._get_group_id(event)
            user_id = self._get_sender_id(event)
            if not group_id or not user_id:
                return
            group_name = self._get_group_name(event)
            if not group_name:
                # 事件没带群名 → OneBot get_group_info 主动拉取
                group_name = await self._fetch_group_name_api(group_id)
            await self.core.on_group_message(group_id, user_id, group_name)
        except Exception as e:
            logger.error(f"[cross_plague] 群消息处理异常: {e}")

    @staticmethod
    def _get_group_id(event: AstrMessageEvent) -> str:
        fn = getattr(event, "get_group_id", None)
        if fn:
            try:
                v = fn()
                if v:
                    return str(v)
            except Exception:
                pass
        try:  # 兜底：从 UMO 解析
            parts = str(event.unified_msg_origin).split(":")
            if len(parts) >= 3 and "Group" in parts[1]:
                return parts[2]
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_sender_id(event: AstrMessageEvent) -> str:
        try:
            v = event.get_sender_id()
            return str(v) if v else ""
        except Exception:
            return ""

    @staticmethod
    def _get_group_name(event: AstrMessageEvent) -> str:
        fn = getattr(event, "get_group_name", None)
        if fn:
            try:
                v = fn()
                if v:
                    return str(v)
            except Exception:
                pass
        return ""

    def _find_cqhttp_client(self):
        """遍历平台实例找 aiocqhttp 客户端（支持 OneBot API 调用）"""
        try:
            for inst in self.context.platform_manager.get_insts():
                try:
                    meta_name = inst.meta().name or ""
                except Exception:
                    meta_name = ""
                if "aiocqhttp" not in meta_name:
                    continue
                for attr in ("bot", "client"):
                    c = getattr(inst, attr, None)
                    if c is not None and hasattr(c, "call_action"):
                        return c
        except Exception:
            pass
        return None

    async def _fetch_group_name_api(self, group_id: str) -> str:
        """通过 OneBot get_group_info 接口主动获取群名（事件拿不到时的兜底）"""
        try:
            client = self._find_cqhttp_client()
            if client is None:
                return ""
            info = await asyncio.wait_for(
                client.call_action("get_group_info", group_id=int(group_id)),
                timeout=10,
            )
            if isinstance(info, dict):
                return str(info.get("group_name") or "")
        except Exception as e:
            logger.debug("[cross_plague] get_group_info 获取群名失败 %s: %s",
                         group_id, e)
        return ""

    # ================= 通用辅助 =================

    async def _is_admin(self, event: AstrMessageEvent) -> bool:
        # 1) AstrBot 内置判定（群主/管理员或 bot 管理员）
        for name in ("is_admin", "is_admin_id"):
            fn = getattr(event, name, None)
            if fn:
                try:
                    r = fn()
                    if asyncio.iscoroutine(r):
                        r = await r
                    if r:
                        return True
                except Exception:
                    continue
        # 2) 兜底：全局配置 admins_id
        try:
            cfg = self.context.get_config()
            admins = cfg.get("admins_id", []) if isinstance(cfg, dict) else []
            sender = self._get_sender_id(event)
            return str(sender) in [str(a) for a in admins]
        except Exception:
            return False

    def _stop(self, event: AstrMessageEvent):
        try:
            event.stop_event()
        except Exception:
            pass

    def _image_or_text(self, event: AstrMessageEvent, png: Optional[bytes],
                       text: str):
        """渲染成功返回图片 chain，失败返回文本结果"""
        if png:
            try:
                b64 = base64.b64encode(png).decode()
                return event.chain_result([Image.fromBase64(b64)])
            except Exception as e:
                logger.warning(f"[cross_plague] 图片消息构建失败: {e}")
        return event.plain_result(text)

    # ================= 指令：瘟疫状态 =================

    @filter.command("瘟疫状态")
    async def cmd_status(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令～")
            self._stop(event)
            return
        data = await self.core.group_status(group_id)
        g = data.get("group")
        if not g:
            yield event.plain_result("本群尚未加入瘟疫模拟（暂无发言记录）。")
            self._stop(event)
            return

        text = (
            f"🏥 {g.get('group_name') or '未知群'} 疫情卡片\n"
            f"状态：{data['label']}\n"
            f"健康值：{g.get('health') if g.get('health') is not None else 100}/100\n"
            f"解药进度：{min(g.get('antidote_progress') or 0, 100)}/100\n"
        )
        if data.get("infected_days") is not None:
            text += f"已感染：{data['infected_days']} 天\n"
        if data.get("quarantined"):
            remain_h = max(0, ((g.get("quarantined_until") or 0) - int(time.time())) // 3600)
            text += f"🛡️ 隔离中（剩余约 {remain_h} 小时）\n"
        if data.get("immune"):
            remain_d = max(0, ((g.get("immunity_until") or 0) - int(time.time())) // 86400)
            text += f"💙 免疫期剩余约 {remain_d} 天\n"
        if data.get("opted_out"):
            text += "⚠️ 本群已退出瘟疫模拟\n"
        contribs = data.get("contributors", [])
        if contribs:
            text += "—— 解药贡献榜前五 ——\n"
            for i, c in enumerate(contribs):
                uid = str(c.get("user_id", "?"))
                masked = uid[:3] + "****" + uid[-2:] if len(uid) > 6 else uid
                text += f"{i + 1}. {masked}：{c.get('total', 0)} 点\n"

        state = await self.db.get_state()
        card = {
            "group_name": g.get("group_name") or "未知群",
            "label": data["label"], "status": g.get("status") or "healthy",
            "health": g.get("health") or 0,
            "progress": g.get("antidote_progress") or 0,
            "infected_days": data.get("infected_days"),
            "quarantined": data.get("quarantined"), "immune": data.get("immune"),
            "contributors": contribs,
        }
        png = await self._render_t2i_or_pillow(
            build_card_tmpldata(data, self._now_str()),
            lambda: render_card(card))
        yield self._image_or_text(event, png, text)
        self._stop(event)

    # ================= 指令：瘟疫全服 =================

    @filter.command("瘟疫全服")
    async def cmd_world(self, event: AstrMessageEvent):
        data = await self.core.world_status()
        c = data["counts"]
        text = (
            f"🌍 全服疫情（瘟疫第 {data['day_count']} 天）\n"
            f"🔴 感染/重症：{c.get('infected', 0) + c.get('severe', 0)} 个群\n"
            f"🟢 健康：{c.get('healthy', 0)} 个群\n"
            f"🔵 痊愈：{c.get('cured', 0)} 个群\n"
            f"今日新增感染：{data['new_today']} 例\n"
            f"今日事件：{data['current_event']}\n"
        )
        if data.get("super_spreader_today"):
            text += "⚠️ 今日存在超级传播者，传播风险极高！\n"
        text += "发送 /瘟疫排行榜 查看排行榜，/瘟疫研发 参与解药研发！"

        map_data = {
            "day_count": data["day_count"],
            "current_event": data["current_event"],
            "counts": c, "new_today": data["new_today"],
            "groups": [
                {**g, "group_name": g.get("group_name") or "未知群"}
                for gid, g in list(self.core._groups.items())[:32]
            ],
        }
        png = await self._render_t2i_or_pillow(
            build_world_tmpldata(data, self._now_str()), lambda: render_map(map_data))
        yield self._image_or_text(event, png, text)
        self._stop(event)

    @staticmethod
    def _now_str() -> str:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%m月%d日 %H:%M")
        except Exception:
            return datetime.now().strftime("%m月%d日 %H:%M")

    async def _render_t2i_or_pillow(self, tmpldata: dict, pillow_fn):
        """渲染链：烛之瘟疫 t2i 模板（主）→ 本地 Pillow（兜底）→ None（文本）"""
        try:
            return await render_t2i_direct(ZHUXI_PLAGUE_T2I_TEMPLATE, tmpldata)
        except Exception as e:
            logger.warning(f"[cross_plague] 烛之瘟疫 t2i 渲染失败，改用本地 Pillow: {e}")
        try:
            return pillow_fn()
        except Exception as e:
            logger.warning(f"[cross_plague] 本地 Pillow 渲染也失败: {e}")
            return None

    # ================= 指令：瘟疫研发 =================

    @filter.command("瘟疫研发")
    async def cmd_research(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        user_id = self._get_sender_id(event)
        if not group_id or not user_id:
            yield event.plain_result("请在群聊中使用该指令～")
            self._stop(event)
            return
        result = await self.core.research(group_id, user_id)
        yield event.plain_result(result)
        self._stop(event)

    # ================= 指令：瘟疫隔离 =================

    @filter.command("瘟疫隔离")
    async def cmd_quarantine(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令～")
            self._stop(event)
            return
        result = await self.core.quarantine(group_id)
        yield event.plain_result(result)
        self._stop(event)

    # ================= 指令：瘟疫排行榜 =================

    @filter.command("瘟疫排行榜")
    async def cmd_rank(self, event: AstrMessageEvent):
        data = await self.core.rankings()
        text = "🏆 瘟疫全服排行榜\n"

        text += "\n—— ⚡ 痊愈最快 ——\n"
        fc = data.get("fastest_cure", [])
        if fc:
            for i, r in enumerate(fc[:5]):
                dur = (r.get("duration") or 0)
                text += (f"{i + 1}. {r.get('group_name') or '未知群'}"
                         f"：{_fmt_duration_(dur)}\n")
        else:
            text += "暂无痊愈群\n"

        text += "\n—— 🔥 感染最久 ——\n"
        li = data.get("longest_infection", [])
        if li:
            now = int(time.time())
            for i, r in enumerate(li[:5]):
                dur = now - (r.get("infected_at") or now)
                text += (f"{i + 1}. {r.get('group_name') or '未知群'}"
                         f"：已感染 {_fmt_duration_(dur)}（健康 {r.get('health', 0)}）\n")
        else:
            text += "当前无感染群\n"

        text += "\n—— 🧪 解药贡献榜前十 ——\n"
        tc = data.get("top_contributors", [])
        if tc:
            for i, r in enumerate(tc[:10]):
                uid = str(r.get("user_id", "?"))
                masked = uid[:3] + "****" + uid[-2:] if len(uid) > 6 else uid
                text += f"{i + 1}. {masked}：{r.get('total', 0)} 点\n"
        else:
            text += "暂无贡献记录\n"

        yield event.plain_result(text)
        self._stop(event)

    # ================= 指令：瘟疫日报 =================

    @filter.command("瘟疫日报")
    async def cmd_report(self, event: AstrMessageEvent):
        stats = await self.core.daily_report_stats()
        text = await self.textgen.report_text(stats)
        yield event.plain_result(f"📰 瘟疫日报\n{text}")
        self._stop(event)

    # ================= 指令：瘟疫退出 =================

    @filter.command("瘟疫退出")
    async def cmd_opt_out(self, event: AstrMessageEvent):
        group_id = self._get_group_id(event)
        if not group_id:
            yield event.plain_result("请在群聊中使用该指令～")
            self._stop(event)
            return
        if not await self._is_admin(event):
            yield event.plain_result("只有群管理员可以使用 /瘟疫退出 哦～")
            self._stop(event)
            return
        result = await self.core.opt_out(group_id)
        yield event.plain_result(result)
        self._stop(event)

    # ================= 指令：瘟疫重置（管理员） =================

    @filter.command("瘟疫重置")
    async def cmd_reset(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("只有管理员可以使用 /瘟疫重置 哦～")
            self._stop(event)
            return
        result = await self.core.admin_reset()
        yield event.plain_result("♻️ " + result)
        self._stop(event)

    # ================= 指令：瘟疫投放（管理员） =================

    @filter.command("瘟疫投放")
    async def cmd_infect(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("只有管理员可以使用 /瘟疫投放 哦～")
            self._stop(event)
            return
        parts = event.message_str.split()
        if len(parts) < 2 or not parts[-1].strip().lstrip("#").isdigit():
            yield event.plain_result("用法：/瘟疫投放 <群号>")
            self._stop(event)
            return
        target = parts[-1].strip().lstrip("#")
        result = await self.core.admin_infect(target)
        yield event.plain_result(result)
        self._stop(event)

    # ================= 指令：瘟疫清除（管理员） =================

    @filter.command("瘟疫清除")
    async def cmd_clear(self, event: AstrMessageEvent):
        if not await self._is_admin(event):
            yield event.plain_result("只有管理员可以使用 /瘟疫清除 哦～")
            self._stop(event)
            return
        parts = event.message_str.split()
        if len(parts) < 2:
            # 无参数默认清除本群
            group_id = self._get_group_id(event)
            if group_id:
                result = await self.core.admin_clear(group_id)
                yield event.plain_result(result)
                self._stop(event)
                return
            yield event.plain_result("用法：/瘟疫清除 <群号>")
            self._stop(event)
            return
        target = parts[-1].strip().lstrip("#")
        result = await self.core.admin_clear(target)
        yield event.plain_result(result)
        self._stop(event)

    # ================= 指令：瘟疫帮助 =================

    @filter.command("瘟疫帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        text = (
            "🦠 跨群瘟疫模拟 · 指令帮助\n"
            "感染会通过群友的跨群发言在不同群之间传播！\n"
            "——————————————\n"
            "/瘟疫状态 —— 查看本群疫情卡片\n"
            "/瘟疫全服 —— 查看全服疫情地图\n"
            "/瘟疫研发 —— 为本群研发解药（贡献 1-5 点）\n"
            "/瘟疫隔离 —— 隔离本群 24 小时（不传也不被传）\n"
            "/瘟疫排行榜 —— 痊愈最快 / 感染最久 / 贡献榜\n"
            "/瘟疫日报 —— 查看今日瘟疫日报\n"
            "/瘟疫退出 —— 本群退出瘟疫模拟（管理员）\n"
            "——————————————\n"
            "管理员：/瘟疫重置 /瘟疫投放 <群号> /瘟疫清除 <群号>\n"
            "提示：感染群内正常发言也有概率助力解药研发哦～"
        )
        yield event.plain_result(text)
        self._stop(event)


def _fmt_duration_(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 3600:
        return f"{max(seconds // 60, 0)} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"
