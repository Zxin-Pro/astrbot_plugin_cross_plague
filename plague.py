# -*- coding: utf-8 -*-
"""astrbot_plugin_cross_plague — 核心逻辑：传播判定 / 健康值衰减 / 解药研发 / 事件触发"""

import asyncio
import random
import time
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger("cross_plague.core")

EVENT_TYPES = ["病毒变异", "疫苗突破", "群体免疫", "封城", "谣言四起"]

STATUS_LABEL = {
    "healthy": "健康",
    "infected": "感染",
    "severe": "重症",
    "cured": "痊愈",
}

DAY = 86400


def _today_str(ts: Optional[int] = None) -> str:
    return datetime.fromtimestamp(ts or time.time()).strftime("%Y-%m-%d")


def _day_start(ts: Optional[int] = None) -> int:
    dt = datetime.fromtimestamp(ts or time.time())
    return int(datetime(dt.year, dt.month, dt.day).timestamp())


def _hm_to_seconds(hm: str, default: Tuple[int, int]) -> int:
    try:
        parts = str(hm).strip().split(":")
        h, m = int(parts[0]), int(parts[1])
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 3600 + m * 60
    except Exception:
        pass
    return default[0] * 3600 + default[1] * 60


class PlagueCore:
    """瘟疫核心逻辑。群状态内存缓存 + 异步落库；传播判定带用户级冷却。"""

    def __init__(self, db, cfg: Dict[str, Any], textgen,
                 send_text: Callable[[str, str], Awaitable[None]],
                 fetch_name: Optional[Callable[[str], Awaitable[str]]] = None):
        """
        :param db: database.Database
        :param cfg: 配置 dict（_conf_schema 注入）
        :param textgen: fetcher.TextGen
        :param send_text: async (group_id, text) -> None  群消息发送回调
        :param fetch_name: async (group_id) -> str  群名获取回调（平台 API 兜底）
        """
        self.db = db
        self.cfg = cfg
        self.textgen = textgen
        self._send_text = send_text
        self._fetch_name = fetch_name

        # 内存缓存
        self._groups: Dict[str, Dict[str, Any]] = {}      # group_id -> group row
        self._speak: Dict[Tuple[str, str], int] = {}      # (user_id, group_id) -> ts（批量落库）
        self._speak_dirty = False
        self._spread_check_at: Dict[str, int] = {}        # user_id -> 上次传播判定时间
        self._notify_count: Dict[Tuple[str, str], int] = {}  # (group_id, ymd) -> 已发通知数
        self._name_resolving: set = set()                 # 正在补拉群名的群
        self._state: Dict[str, Any] = {}

        self._lock = asyncio.Lock()   # 传播/痊愈/重置等关键段串行化

    # ================= 初始化 =================

    async def load(self) -> None:
        for g in await self.db.get_all_groups():
            self._groups[g["group_id"]] = g
        self._state = await self.db.get_state()
        logger.info("[cross_plague] 缓存加载完成：%d 个群，瘟疫第 %s 天",
                    len(self._groups), self._state.get("day_count", 0))

    # ================= 工具 =================

    def _cfg(self, key: str, default=None):
        v = self.cfg.get(key, default)
        return v if v is not None else default

    def _now(self) -> int:
        return int(time.time())

    def _g(self, group_id: str, group_name: str = "") -> Dict[str, Any]:
        """取群缓存，不存在则建（内存 + 异步落库）；群名缺失时异步补拉"""
        g = self._groups.get(group_id)
        if g is None:
            g = {
                "group_id": group_id, "group_name": group_name or "未知群",
                "status": "healthy", "health": 100, "infected_at": None,
                "cured_at": None, "antidote_progress": 0, "last_spread_at": None,
                "is_false_alarm": 0, "false_alarm_at": None,
                "quarantined_until": None, "immunity_until": None,
                "opted_out": 0, "last_active_at": self._now(),
            }
            self._groups[group_id] = g
            asyncio.create_task(self._safe_ensure_group(group_id, g["group_name"]))
            if not group_name:
                self._resolve_name_later(group_id)
        elif group_name and g.get("group_name") != group_name:
            g["group_name"] = group_name
            asyncio.create_task(self._safe_update_group(group_id, group_name=group_name))
        elif (not group_name and g.get("group_name") in ("", None, "未知群")
              and group_id not in self._name_resolving):
            self._resolve_name_later(group_id)
        return g

    def _resolve_name_later(self, group_id: str) -> None:
        """群名缺失：异步调平台 API 补拉，补到后写缓存 + 落库"""
        if group_id in self._name_resolving:
            return
        if self._fetch_name is None:
            return
        self._name_resolving.add(group_id)

        async def _job():
            try:
                name = await self._fetch_name(group_id)
                if name and group_id in self._groups:
                    self._groups[group_id]["group_name"] = name
                    await self._safe_update_group(group_id, group_name=name)
                    logger.info("[cross_plague] 群 %s 名称补拉成功：%s", group_id, name)
            except Exception as e:
                logger.warning("[cross_plague] 群 %s 名称补拉失败: %s", group_id, e)
            finally:
                self._name_resolving.discard(group_id)

        asyncio.create_task(_job())

    def _sync_group(self, group_id: str, **fields) -> None:
        """更新缓存 + 异步落库"""
        g = self._groups.get(group_id)
        if g is None:
            return
        g.update(fields)
        asyncio.create_task(self._safe_update_group(group_id, **fields))

    async def _safe_ensure_group(self, group_id: str, name: str) -> None:
        try:
            await self.db.ensure_group(group_id, name)
        except Exception as e:
            logger.error("[cross_plague] ensure_group 失败 %s: %s", group_id, e)

    async def _safe_update_group(self, group_id: str, **fields) -> None:
        try:
            await self.db.update_group(group_id, **fields)
        except Exception as e:
            logger.error("[cross_plague] update_group 失败 %s: %s", group_id, e)

    def _ymd(self) -> str:
        return _today_str()

    def _can_notify(self, group_id: str) -> bool:
        """同群每日最多 notify_daily_limit 条瘟疫通知（防刷屏）"""
        limit = int(self._cfg("notify_daily_limit", 3) or 3)
        key = (group_id, self._ymd())
        return self._notify_count.get(key, 0) < limit

    def _mark_notified(self, group_id: str) -> None:
        key = (group_id, self._ymd())
        self._notify_count[key] = self._notify_count.get(key, 0) + 1

    def _is_dead(self, g: Dict[str, Any]) -> bool:
        """死群保护：超过 7 天无发言的群不参与传播"""
        last = g.get("last_active_at") or 0
        return (self._now() - last) > 7 * DAY

    def _is_quarantined(self, g: Dict[str, Any]) -> bool:
        q = g.get("quarantined_until") or 0
        return q > self._now()

    def _is_immune(self, g: Dict[str, Any]) -> bool:
        imm = g.get("immunity_until") or 0
        return imm > self._now()

    def _status_label(self, g: Dict[str, Any]) -> str:
        if g.get("is_false_alarm") and g.get("status") == "healthy":
            return "疑似感染"
        return STATUS_LABEL.get(g.get("status", "healthy"), g.get("status", "healthy"))

    def _is_super_spreader(self, user_id: str) -> bool:
        if not self._cfg("enable_super_spreader", True):
            return False
        return (self._state.get("super_spreader_date") == self._ymd()
                and str(self._state.get("super_spreader_user") or "") == str(user_id))

    # ================= 消息入口 =================

    async def on_group_message(self, group_id: str, user_id: str,
                               group_name: str = "") -> None:
        """群消息事件入口：记录跨群行为 + 触发传播判定 + 发言助力研发"""
        if not group_id or not user_id:
            return
        now = self._now()
        g = self._g(group_id, group_name)
        g["last_active_at"] = now

        # 1) 跨群行为：内存缓存 + 批量落库（性能优化，不逐条写库）
        self._speak[(user_id, group_id)] = now
        self._speak_dirty = True

        if g.get("opted_out"):
            return

        # 2) 感染群内发言有概率助力解药研发（模拟"互动研发"）
        if g.get("status") in ("infected", "severe"):
            chance = int(self._cfg("speaking_research_chance", 8) or 0)
            if chance > 0 and random.random() * 100 < chance:
                try:
                    await self._add_progress(group_id, user_id, 1, announce=False)
                except Exception as e:
                    logger.warning("[cross_plague] 发言助力研发失败: %s", e)

        # 3) 传播判定：先做内存预检（无跨群活动直接返回），再按用户级冷却判定
        recent_gids = {gid for (uid, gid) in self._speak if uid == user_id}
        if len(recent_gids) < 2:
            return  # 本插件启动以来该用户只在一个群发过言，不可能跨群传播
        cooldown_h = max(0, int(self._cfg("spread_cooldown_hours", 1) or 1))
        last_check = self._spread_check_at.get(user_id, 0)
        if cooldown_h > 0 and now - last_check < cooldown_h * 3600:
            return

        try:
            judged = await self._spread_check(group_id, user_id, now, cooldown_h)
            if judged:
                # 只有真实存在传播风险时才消耗冷却，避免"空转判定"吞掉冷却窗口
                self._spread_check_at[user_id] = now
        except Exception as e:
            logger.error("[cross_plague] 传播判定异常: %s", e)

    # ================= 传播 =================

    async def _spread_check(self, cur_group: str, user_id: str,
                            now: int, cooldown_h: int) -> bool:
        """返回 True 表示发生过一次带传播风险的判定（计入冷却）"""
        async with self._lock:
            since = now - cooldown_h * 3600
            # 内存缓存优先，DB 兜底（插件重启后仍可判定）
            recent: Dict[str, int] = {
                gid: ts for (uid, gid), ts in self._speak.items()
                if uid == user_id and ts >= since
            }
            try:
                for row in await self.db.recent_groups_of_user(user_id, since):
                    gid = row["group_id"]
                    if gid not in recent:
                        recent[gid] = int(row["last_speak_at"] or 0)
            except Exception as e:
                logger.warning("[cross_plague] 查询用户跨群记录失败: %s", e)

            if len(recent) < 2:
                return False  # 最近没跨群活动，不可能传播

            tg = self._groups.get(cur_group)
            cur_ok = bool(
                tg and not tg.get("opted_out") and tg.get("status") == "healthy"
                and not self._is_quarantined(tg) and not self._is_dead(tg)
                and not self._is_immune(tg)
            )
            is_super = self._is_super_spreader(user_id)
            if not is_super and not cur_ok:
                return False  # 普通用户：当前群不满足被感染条件则无需判定

            # 传染源：冷却窗口内去过的感染/重症群（含当前群——正在感染群内发言即带毒）
            sources = [
                gid for gid in recent
                if gid in self._groups
                and self._groups[gid].get("status") in ("infected", "severe")
                and not self._is_dead(self._groups[gid])
                and not self._is_quarantined(self._groups[gid])
            ]
            if not sources:
                return False

            # 目标群列表：普通用户只判当前群；超级传播者一次最多感染 3 个群
            if is_super:
                targets = [cur_group] if cur_ok else []
                for gid in recent:
                    if len(targets) >= 3:
                        break
                    if gid in targets:
                        continue
                    og = self._groups.get(gid)
                    if (og and not og.get("opted_out")
                            and og.get("status") == "healthy"
                            and not self._is_quarantined(og)
                            and not self._is_dead(og)
                            and not self._is_immune(og)):
                        targets.append(gid)
                if not targets:
                    return False
            else:
                targets = [cur_group]

            for target in targets:
                source = random.choice(sources)
                if self._roll_spread(source, user_id, is_super, len(recent)):
                    await self._infect(source, target, user_id, is_super)
            return True

    def _spread_prob(self, source: Dict[str, Any], is_super: bool,
                     user_recent_groups: int) -> float:
        """传播概率：基础 0.3（超级传播者 0.9），来源群健康值 <30 加 0.1，
        用户跨群活跃（近 3 群以上）加 0.05，封顶 0.95"""
        if is_super:
            base = 0.9
        else:
            base = 0.3
            if user_recent_groups >= 3:
                base += 0.05
        if (source.get("health") or 0) < 30:
            base += 0.1
        return min(base, 0.95)

    def _roll_spread(self, source_id: str, user_id: str, is_super: bool,
                     user_recent_groups: int) -> bool:
        src = self._groups.get(source_id)
        if src is None:
            return False
        prob = self._spread_prob(src, is_super, user_recent_groups)
        hit = random.random() < prob
        logger.debug("[cross_plague] 传播判定 %s->%s prob=%.2f hit=%s",
                     source_id, user_id, prob, hit)
        return hit

    async def _infect(self, from_group: str, to_group: str, user_id: str,
                      is_super: bool) -> None:
        now = self._now()
        tg = self._groups.get(to_group)
        if tg is None:
            return
        src_name = (self._groups.get(from_group) or {}).get("group_name") or "未知群"
        to_name = tg.get("group_name") or "未知群"

        self._sync_group(
            to_group, status="infected", health=100, infected_at=now,
            cured_at=None, antidote_progress=0, last_spread_at=now,
            is_false_alarm=0, false_alarm_at=None,
        )
        try:
            await self.db.record_infection(from_group, to_group, user_id, is_super)
        except Exception as e:
            logger.error("[cross_plague] 记录感染失败: %s", e)

        logger.info("[cross_plague] 感染：%s -> %s (user=%s, super=%s)",
                    from_group, to_group, user_id, is_super)

        if self._can_notify(to_group):
            self._mark_notified(to_group)
            text = await self.textgen.notice_text(src_name, to_name, is_super)
            asyncio.create_task(self._safe_send(to_group, text))

    async def _safe_send(self, group_id: str, text: str) -> None:
        try:
            await self._send_text(group_id, text)
        except Exception as e:
            logger.error("[cross_plague] 向群 %s 发送通知失败: %s", group_id, e)

    # ================= 解药研发 =================

    async def research(self, group_id: str, user_id: str) -> str:
        """/瘟疫研发：贡献 1-5 点解药进度（随机），每日每人每群上限 20 点"""
        async with self._lock:
            g = self._groups.get(group_id)
            if g is None or g.get("opted_out"):
                return "本群没有加入瘟疫模拟哦～"
            if g.get("status") not in ("infected", "severe"):
                if g.get("status") == "cured":
                    return "本群已经痊愈啦，暂时不需要研发解药。保持警惕，防止二次感染！"
                return "本群目前是健康状态，没有疫情，无需研发解药～"

            day_start = _day_start()
            used = await self.db.daily_contribution_sum(user_id, group_id, day_start)
            daily_cap = 20
            if used >= daily_cap:
                return (f"你今天的研发贡献已达上限（{used}/{daily_cap} 点），"
                        "休息一下，明天继续！")
            amount = min(random.randint(1, 5), daily_cap - used)

            await self.db.add_contribution(user_id, group_id, amount, self._now())
            new_progress = (g.get("antidote_progress") or 0) + amount
            msg_tail = ""
            if new_progress >= 100:
                await self._cure(group_id)
                msg_tail = ("\n🎉 解药研发成功！本群痊愈，获得 7 天免疫期！")
            else:
                self._sync_group(group_id, antidote_progress=new_progress)
            return (f"🧪 解药研发 +{amount} 点！"
                    f"本群进度：{min(new_progress, 100)}/100"
                    f"（你今日已贡献 {used + amount}/{daily_cap} 点）{msg_tail}")

    async def _add_progress(self, group_id: str, user_id: str, amount: int,
                            announce: bool = False) -> None:
        """发言/事件带来的进度增长（同样计入贡献与每日上限）"""
        g = self._groups.get(group_id)
        if g is None or g.get("status") not in ("infected", "severe"):
            return
        day_start = _day_start()
        used = await self.db.daily_contribution_sum(user_id, group_id, day_start)
        if used >= 20:
            return
        amount = min(amount, 20 - used)
        if amount <= 0:
            return
        await self.db.add_contribution(user_id, group_id, amount, self._now())
        new_progress = (g.get("antidote_progress") or 0) + amount
        if new_progress >= 100:
            await self._cure(group_id)
        else:
            self._sync_group(group_id, antidote_progress=new_progress)

    async def _cure(self, group_id: str, by_event: bool = False) -> None:
        """痊愈：health=100，7 天免疫，全服广播"""
        now = self._now()
        immunity_days = int(self._cfg("immunity_days", 7) or 7)
        g = self._groups.get(group_id)
        group_name = (g or {}).get("group_name") or "未知群"
        self._sync_group(
            group_id, status="cured", health=100, cured_at=now,
            antidote_progress=100, immunity_until=now + immunity_days * DAY,
        )
        logger.info("[cross_plague] 群 %s 痊愈%s", group_id,
                    "（事件触发）" if by_event else "")
        if self._can_notify(group_id):
            self._mark_notified(group_id)
            text = await self.textgen.cure_text(group_name)
            asyncio.create_task(self._safe_send(group_id, text))

    # ================= 健康值衰减（每小时） =================

    async def hourly_decay(self) -> None:
        async with self._lock:
            now = self._now()
            for gid, g in list(self._groups.items()):
                status = g.get("status")
                if status == "infected":
                    new_health = max(0, (g.get("health") or 0) - 5)
                    if new_health <= 0:
                        self._sync_group(gid, health=0, status="severe")
                        logger.info("[cross_plague] 群 %s 转入重症", gid)
                    else:
                        self._sync_group(gid, health=new_health)
                elif status == "severe":
                    # 重症持续 24h 后维持重症（不死亡，避免挫败感）
                    self._sync_group(gid, health=0)
            await self.db.update_state(last_decay_at=now)
            self._state["last_decay_at"] = now

    # ================= 谣言解除 =================

    async def clear_false_alarms(self) -> List[str]:
        ids = await self.db.false_alarm_expired(self._now() - DAY)
        for gid in ids:
            self._sync_group(gid, is_false_alarm=0, false_alarm_at=None)
            logger.info("[cross_plague] 群 %s 谣言解除", gid)
        return ids

    # ================= 每日重置（零号病人 + 超级传播者 + 事件排期） =================

    async def daily_reset(self) -> Optional[str]:
        """每日零号病人投放。返回被投放群号（无则 None）"""
        async with self._lock:
            now = self._now()
            day = (self._state.get("day_count") or 0) + 1

            # 随机排期今日事件时间
            ev_sec = random.randint(0, 23) * 3600 + random.randint(0, 59) * 60
            ev_hm = f"{ev_sec // 3600:02d}:{(ev_sec % 3600) // 60:02d}"

            await self.db.update_state(
                day_count=day, last_daily_reset=now, last_reset_date=self._ymd(),
                current_event=None, event_type=None,
                event_scheduled_hm=ev_hm, event_fired_date=None,
            )
            self._state.update({
                "day_count": day, "last_daily_reset": now,
                "last_reset_date": self._ymd(), "current_event": None,
                "event_type": None, "event_scheduled_hm": ev_hm,
                "event_fired_date": None,
            })

            # 超级传播者：从 24h 内活跃用户中随机挑选
            if self._cfg("enable_super_spreader", True):
                try:
                    users = await self.db.recent_active_users(now - DAY)
                    if users:
                        uid = random.choice(users)
                        await self.db.update_state(
                            super_spreader_user=uid, super_spreader_date=self._ymd())
                        self._state["super_spreader_user"] = uid
                        self._state["super_spreader_date"] = self._ymd()
                except Exception as e:
                    logger.warning("[cross_plague] 选择超级传播者失败: %s", e)

            # 零号病人：随机一个活跃、健康（或免疫期已过）的群
            victim = None
            try:
                row = await self.db.pick_patient_zero(now - 7 * DAY)
                if row:
                    victim = row["group_id"]
                    g = self._g(victim, row.get("group_name") or "")
                    self._groups[victim] = {**g, **row}
                    await self._infect("SYSTEM", victim, "system", is_super=False)
            except Exception as e:
                logger.error("[cross_plague] 零号病人投放失败: %s", e)

            logger.info("[cross_plague] 每日重置完成：第 %d 天，零号=%s，事件时间=%s",
                        day, victim or "无", ev_hm)
            return victim

    # ================= 每日事件 =================

    async def fire_daily_event(self) -> Optional[str]:
        """从事件池随机抽取并结算今日全局事件，返回事件类型"""
        async with self._lock:
            event_type = random.choice(EVENT_TYPES)
            detail = await self._apply_event(event_type)
            await self.db.update_state(
                current_event=f"{event_type}：{detail}",
                event_type=event_type, event_fired_date=self._ymd(),
            )
            self._state["current_event"] = f"{event_type}：{detail}"
            self._state["event_type"] = event_type
            self._state["event_fired_date"] = self._ymd()

            text = await self.textgen.event_text(event_type, detail)
            await self._broadcast(text)
            logger.info("[cross_plague] 每日事件触发：%s（%s）", event_type, detail)
            return event_type

    async def _apply_event(self, event_type: str) -> str:
        now = self._now()
        if event_type == "病毒变异":
            affected = [gid for gid, g in self._groups.items()
                        if g.get("status") in ("infected", "severe")]
            for gid in affected:
                g = self._groups[gid]
                new_health = max(0, (g.get("health") or 0) - 10)
                if new_health <= 0:
                    self._sync_group(gid, health=0, status="severe")
                else:
                    self._sync_group(gid, health=new_health)
            return f"{len(affected)} 个感染群健康值下降"

        if event_type == "疫苗突破":
            candidates = [gid for gid, g in self._groups.items()
                          if g.get("status") in ("infected", "severe")]
            if not candidates:
                return "无感染群，事件空转"
            gid = random.choice(candidates)
            await self._add_progress(gid, "event", 20)
            name = self._groups[gid].get("group_name") or "未知群"
            return f"{name} 解药进度 +20"

        if event_type == "群体免疫":
            candidates = [gid for gid, g in self._groups.items()
                          if g.get("status") in ("infected", "severe")]
            if not candidates:
                return "无感染群，事件空转"
            gid = random.choice(candidates)
            await self._cure(gid, by_event=True)
            name = self._groups[gid].get("group_name") or "未知群"
            return f"{name} 直接痊愈"

        if event_type == "封城":
            candidates = [gid for gid, g in self._groups.items()
                          if g.get("status") in ("infected", "severe")
                          and not self._is_quarantined(g)]
            if not candidates:
                return "无符合条件的群，事件空转"
            gid = random.choice(candidates)
            dt = datetime.fromtimestamp(now)
            end_of_day = int(datetime(dt.year, dt.month, dt.day, 23, 59, 59).timestamp())
            self._sync_group(gid, quarantined_until=end_of_day)
            name = self._groups[gid].get("group_name") or "未知群"
            return f"{name} 今日封城，暂停传播"

        if event_type == "谣言四起":
            candidates = [gid for gid, g in self._groups.items()
                          if g.get("status") == "healthy"
                          and not g.get("is_false_alarm")
                          and not g.get("opted_out")]
            if not candidates:
                return "无健康群，事件空转"
            gid = random.choice(candidates)
            self._sync_group(gid, is_false_alarm=1, false_alarm_at=now)
            name = self._groups[gid].get("group_name") or "未知群"
            return f"{name} 被误判为感染（24 小时后自动解除）"

        return event_type

    async def _broadcast(self, text: str) -> None:
        """向活跃且未退出的群广播（受每日通知上限约束）"""
        now = self._now()
        for gid, g in list(self._groups.items()):
            if g.get("opted_out") or self._is_dead(g):
                continue
            if not self._can_notify(gid):
                continue
            self._mark_notified(gid)
            asyncio.create_task(self._safe_send(gid, text))

    # ================= 主动操作 =================

    async def quarantine(self, group_id: str) -> str:
        g = self._groups.get(group_id)
        if g is None:
            return "本群尚未加入瘟疫模拟，无需隔离～"
        if self._is_quarantined(g):
            remain_h = ((g.get("quarantined_until") or 0) - self._now()) // 3600
            return f"本群已在隔离中，剩余约 {remain_h} 小时。"
        until = self._now() + DAY
        self._sync_group(group_id, quarantined_until=until)
        return ("🛡️ 隔离申请已通过！本群 24 小时内不传播也不被传播，"
                "但健康值仍会衰减。注意：隔离不是解药，抓紧研发！")

    async def opt_out(self, group_id: str) -> str:
        self._sync_group(group_id, opted_out=1, status="healthy",
                         health=100, infected_at=None, antidote_progress=0,
                         is_false_alarm=0, false_alarm_at=None,
                         quarantined_until=None, immunity_until=None)
        return "本群已退出瘟疫模拟，之后不会再被感染。重新加入可联系管理员重置。"

    async def opt_in(self, group_id: str) -> str:
        self._sync_group(group_id, opted_out=0)
        return "本群已重新加入瘟疫模拟，祝好运！"

    async def admin_infect(self, group_id: str) -> str:
        g = self._g(group_id)
        if g.get("opted_out"):
            return f"群 {group_id} 已退出瘟疫模拟，先让该群重新加入。"
        await self._infect("ADMIN", group_id, "admin", is_super=False)
        return f"已对群 {group_id} 手动投放零号病人。"

    async def admin_clear(self, group_id: str) -> str:
        if group_id not in self._groups:
            return f"群 {group_id} 不在记录中。"
        self._sync_group(group_id, status="healthy", health=100,
                         infected_at=None, antidote_progress=0,
                         is_false_alarm=0, false_alarm_at=None,
                         quarantined_until=None, immunity_until=None)
        return f"已手动清除群 {group_id} 的感染状态。"

    async def admin_reset(self) -> str:
        await self.db.reset_all()
        self._groups.clear()
        self._notify_count.clear()
        self._spread_check_at.clear()
        self._state = await self.db.get_state()
        return "全服瘟疫状态已重置（跨群发言记录保留）。"

    # ================= 批量落库（定时调用） =================

    async def flush_speak_cache(self) -> bool:
        if not self._speak_dirty:
            return False
        items = [(u, g, t) for (u, g), t in list(self._speak.items())]
        try:
            await self.db.bulk_upsert_user_speak(items)
            self._speak_dirty = False
            return True
        except Exception as e:
            logger.error("[cross_plague] 批量落库用户发言失败: %s", e)
            return False

    # ================= 查询聚合 =================

    async def world_status(self) -> Dict[str, Any]:
        counts = await self.db.count_by_status()
        day_start = _day_start()
        new_today = await self.db.count_new_infections(day_start)
        return {
            "day_count": self._state.get("day_count") or 0,
            "current_event": self._state.get("current_event") or "暂无",
            "counts": counts,
            "new_today": new_today,
            "groups": list(self._groups.values()),
            "super_spreader_today": (
                self._state.get("super_spreader_date") == self._ymd()
                and bool(self._state.get("super_spreader_user"))
            ),
        }

    async def group_status(self, group_id: str) -> Dict[str, Any]:
        g = self._groups.get(group_id)
        now = self._now()
        data: Dict[str, Any] = {
            "group": g, "label": self._status_label(g) if g else "未记录",
            "infected_days": None, "contributors": [],
            "quarantined": self._is_quarantined(g) if g else False,
            "immune": self._is_immune(g) if g else False,
            "opted_out": bool(g.get("opted_out")) if g else False,
        }
        if g and g.get("infected_at"):
            data["infected_days"] = round((now - g["infected_at"]) / DAY, 1)
        if g:
            try:
                data["contributors"] = await self.db.group_contributors(group_id, 5)
            except Exception as e:
                logger.warning("[cross_plague] 查询贡献者失败: %s", e)
        return data

    async def rankings(self) -> Dict[str, Any]:
        fastest = await self.db.rank_fastest_cure(10)
        longest = await self.db.rank_longest_infection(10)
        top = await self.db.top_contributors(10)
        return {"fastest_cure": fastest, "longest_infection": longest,
                "top_contributors": top}

    async def daily_report_stats(self) -> Dict[str, Any]:
        counts = await self.db.count_by_status()
        day_start = _day_start()
        new_today = await self.db.count_new_infections(day_start)
        top = await self.db.top_contributors(1, since=day_start)
        top_user = top[0]["user_id"] if top else "暂无"
        top_amount = int(top[0]["total"]) if top else 0
        return {
            "day_count": self._state.get("day_count") or 0,
            "infected_count": counts.get("infected", 0) + counts.get("severe", 0),
            "cured_count": counts.get("cured", 0),
            "new_infections": new_today,
            "today_event": self._state.get("current_event") or "无",
            "top_contributor": "某位热心群友",
            "top_amount": top_amount,
            "_top_user_id": top_user,
        }
