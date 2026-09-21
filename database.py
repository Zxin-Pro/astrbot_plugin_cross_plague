# -*- coding: utf-8 -*-
"""astrbot_plugin_cross_plague 数据层封装（aiosqlite）"""

import asyncio
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiosqlite

logger_name = "cross_plague.db"
try:
    from astrbot.api import logger
except Exception:  # pragma: no cover
    import logging

    logger = logging.getLogger(logger_name)

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_id           TEXT PRIMARY KEY,
    group_name         TEXT,
    status             TEXT DEFAULT 'healthy',
    health             INTEGER DEFAULT 100,
    infected_at        INTEGER,
    cured_at           INTEGER,
    antidote_progress  INTEGER DEFAULT 0,
    last_spread_at     INTEGER,
    is_false_alarm     INTEGER DEFAULT 0,
    false_alarm_at     INTEGER,
    quarantined_until  INTEGER,
    immunity_until     INTEGER,
    opted_out          INTEGER DEFAULT 0,
    last_active_at     INTEGER
);
CREATE TABLE IF NOT EXISTS user_groups (
    user_id        TEXT,
    group_id       TEXT,
    last_speak_at  INTEGER,
    PRIMARY KEY (user_id, group_id)
);
CREATE TABLE IF NOT EXISTS infections (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_group       TEXT,
    to_group         TEXT,
    user_id          TEXT,
    infected_at      INTEGER,
    is_super_spreader INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS antidote_contributions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    TEXT,
    group_id   TEXT,
    amount     INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS plague_state (
    id                INTEGER PRIMARY KEY CHECK (id = 1),
    day_count         INTEGER DEFAULT 0,
    last_daily_reset  INTEGER,
    current_event     TEXT,
    super_spreader_user TEXT,
    super_spreader_date TEXT,
    event_type        TEXT,
    event_scheduled_hm TEXT,
    event_fired_date  TEXT,
    last_reset_date   TEXT,
    last_report_date  TEXT,
    last_decay_at     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_groups_status ON groups(status);
CREATE INDEX IF NOT EXISTS idx_user_groups_user ON user_groups(user_id);
CREATE INDEX IF NOT EXISTS idx_infections_time ON infections(infected_at);
CREATE INDEX IF NOT EXISTS idx_antidote_user_group
    ON antidote_contributions(user_id, group_id, created_at);
"""

# groups 表允许动态更新的字段白名单（防注入，值全部走参数化）
_GROUP_FIELDS = {
    "group_name", "status", "health", "infected_at", "cured_at",
    "antidote_progress", "last_spread_at", "is_false_alarm",
    "false_alarm_at", "quarantined_until", "immunity_until",
    "opted_out", "last_active_at",
}


class Database:
    """SQLite 异步封装：群状态 / 用户跨群行为 / 感染记录 / 解药贡献 / 全局状态"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    # ---------------- 基础 ----------------

    async def init(self) -> None:
        last_err = None
        for attempt in range(3):
            try:
                self._conn = await aiosqlite.connect(self.db_path)
                self._conn.row_factory = aiosqlite.Row
                # 注：不启用 WAL——PRoot/容器环境下 WAL 的 -shm/-wal 文件
                # 可能触发间歇性 "disk I/O error"，默认 journal 模式足够稳定
                await self._conn.executescript(SCHEMA)
                await self._conn.execute(
                    "INSERT OR IGNORE INTO plague_state (id, day_count) VALUES (1, 0)"
                )
                await self._conn.commit()
                logger.info("[cross_plague] 数据库初始化完成: %s", self.db_path)
                return
            except Exception as e:
                last_err = e
                logger.warning("[cross_plague] 数据库初始化失败(第%d次): %s",
                               attempt + 1, e)
                await self.close()
                await asyncio.sleep(0.5 * (attempt + 1))
        raise last_err

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.commit()
                await self._conn.close()
            except Exception:
                pass
            self._conn = None

    async def execute(self, sql: str, params: Iterable = ()) -> None:
        async with self._lock:
            await self._conn.execute(sql, tuple(params))
            await self._conn.commit()

    async def fetchall(self, sql: str, params: Iterable = ()) -> List[aiosqlite.Row]:
        async with self._lock:
            cur = await self._conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            await cur.close()
            return rows

    async def fetchone(self, sql: str, params: Iterable = ()) -> Optional[aiosqlite.Row]:
        async with self._lock:
            cur = await self._conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            await cur.close()
            return row

    # ---------------- 群状态 ----------------

    async def ensure_group(self, group_id: str, group_name: str = "") -> None:
        await self.execute(
            "INSERT OR IGNORE INTO groups (group_id, group_name, status, health, "
            "antidote_progress, last_active_at) VALUES (?, ?, 'healthy', 100, 0, ?)",
            (group_id, group_name or group_id, int(time.time())),
        )

    async def update_group(self, group_id: str, **fields) -> None:
        keys = [k for k in fields if k in _GROUP_FIELDS]
        if not keys:
            return
        sets = ", ".join(f"{k} = ?" for k in keys)
        await self.execute(
            f"UPDATE groups SET {sets} WHERE group_id = ?",
            [fields[k] for k in keys] + [group_id],
        )

    async def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetchone(
            "SELECT * FROM groups WHERE group_id = ?", (group_id,)
        )
        return dict(row) if row else None

    async def get_all_groups(self) -> List[Dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM groups")
        return [dict(r) for r in rows]

    async def pick_patient_zero(self, active_since: int) -> Optional[Dict[str, Any]]:
        row = await self.fetchone(
            "SELECT * FROM groups WHERE opted_out = 0 "
            "AND status IN ('healthy', 'cured') "
            "AND (immunity_until IS NULL OR immunity_until < ?) "
            "AND last_active_at >= ? "
            "ORDER BY RANDOM() LIMIT 1",
            (int(time.time()), active_since),
        )
        return dict(row) if row else None

    async def false_alarm_expired(self, before: int) -> List[str]:
        rows = await self.fetchall(
            "SELECT group_id FROM groups WHERE is_false_alarm = 1 AND false_alarm_at < ?",
            (before,),
        )
        return [r["group_id"] for r in rows]

    # ---------------- 用户跨群行为 ----------------

    async def upsert_user_speak(self, user_id: str, group_id: str, ts: int) -> None:
        await self.execute(
            "INSERT INTO user_groups (user_id, group_id, last_speak_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, group_id) DO UPDATE SET last_speak_at = excluded.last_speak_at",
            (user_id, group_id, ts),
        )

    async def bulk_upsert_user_speak(self, items: List[Tuple[str, str, int]]) -> None:
        if not items:
            return
        async with self._lock:
            await self._conn.executemany(
                "INSERT INTO user_groups (user_id, group_id, last_speak_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id, group_id) DO UPDATE SET last_speak_at = excluded.last_speak_at",
                [(u, g, t) for u, g, t in items],
            )
            await self._conn.commit()

    async def recent_groups_of_user(self, user_id: str, since: int) -> List[Dict[str, Any]]:
        """该用户 since 之后发言过的群（含群状态），用于跨群传播判定"""
        rows = await self.fetchall(
            "SELECT ug.group_id, ug.last_speak_at, g.status, g.health, g.quarantined_until, "
            "g.immunity_until, g.opted_out, g.last_active_at, g.group_name "
            "FROM user_groups ug LEFT JOIN groups g ON g.group_id = ug.group_id "
            "WHERE ug.user_id = ? AND ug.last_speak_at >= ?",
            (user_id, since),
        )
        return [dict(r) for r in rows]

    async def recent_active_users(self, since: int, limit: int = 50) -> List[str]:
        rows = await self.fetchall(
            "SELECT DISTINCT user_id FROM user_groups WHERE last_speak_at >= ? "
            "ORDER BY RANDOM() LIMIT ?",
            (since, limit),
        )
        return [r["user_id"] for r in rows]

    # ---------------- 感染记录 ----------------

    async def record_infection(self, from_group: str, to_group: str,
                               user_id: str, is_super: bool) -> None:
        await self.execute(
            "INSERT INTO infections (from_group, to_group, user_id, infected_at, "
            "is_super_spreader) VALUES (?, ?, ?, ?, ?)",
            (from_group, to_group, user_id, int(time.time()), 1 if is_super else 0),
        )

    async def count_new_infections(self, since: int) -> int:
        row = await self.fetchone(
            "SELECT COUNT(*) AS c FROM infections WHERE infected_at >= ?", (since,)
        )
        return int(row["c"]) if row else 0

    async def clear_infection_records(self) -> None:
        await self.execute("DELETE FROM infections")

    # ---------------- 解药贡献 ----------------

    async def add_contribution(self, user_id: str, group_id: str,
                               amount: int, ts: int) -> None:
        await self.execute(
            "INSERT INTO antidote_contributions (user_id, group_id, amount, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, group_id, amount, ts),
        )

    async def daily_contribution_sum(self, user_id: str, group_id: str,
                                     day_start: int) -> int:
        row = await self.fetchone(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM antidote_contributions "
            "WHERE user_id = ? AND group_id = ? AND created_at >= ?",
            (user_id, group_id, day_start),
        )
        return int(row["s"]) if row else 0

    async def group_contributors(self, group_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT user_id, SUM(amount) AS total FROM antidote_contributions "
            "WHERE group_id = ? GROUP BY user_id ORDER BY total DESC LIMIT ?",
            (group_id, limit),
        )
        return [dict(r) for r in rows]

    async def top_contributors(self, limit: int = 10,
                               since: Optional[int] = None) -> List[Dict[str, Any]]:
        if since is not None:
            rows = await self.fetchall(
                "SELECT user_id, SUM(amount) AS total FROM antidote_contributions "
                "WHERE created_at >= ? GROUP BY user_id ORDER BY total DESC LIMIT ?",
                (since, limit),
            )
        else:
            rows = await self.fetchall(
                "SELECT user_id, SUM(amount) AS total FROM antidote_contributions "
                "GROUP BY user_id ORDER BY total DESC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    # ---------------- 全局状态 ----------------

    async def get_state(self) -> Dict[str, Any]:
        row = await self.fetchone("SELECT * FROM plague_state WHERE id = 1")
        return dict(row) if row else {}

    async def update_state(self, **fields) -> None:
        keys = list(fields.keys())
        if not keys:
            return
        sets = ", ".join(f"{k} = ?" for k in keys)
        await self.execute(
            f"UPDATE plague_state SET {sets} WHERE id = 1",
            [fields[k] for k in keys],
        )

    # ---------------- 排行榜 / 统计 ----------------

    async def rank_fastest_cure(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT group_id, group_name, infected_at, cured_at, "
            "(cured_at - infected_at) AS duration FROM groups "
            "WHERE cured_at IS NOT NULL AND infected_at IS NOT NULL "
            "ORDER BY duration ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def rank_longest_infection(self, limit: int = 10) -> List[Dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT group_id, group_name, infected_at, health, status FROM groups "
            "WHERE status IN ('infected', 'severe') AND infected_at IS NOT NULL "
            "ORDER BY infected_at ASC LIMIT ?",
            (limit,),
        )
        return [dict(r) for r in rows]

    async def count_by_status(self) -> Dict[str, int]:
        rows = await self.fetchall(
            "SELECT status, COUNT(*) AS c FROM groups GROUP BY status"
        )
        result = {"healthy": 0, "infected": 0, "severe": 0, "cured": 0}
        for r in rows:
            result[r["status"]] = int(r["c"])
        return result

    # ---------------- 管理 ----------------

    async def reset_all(self) -> None:
        """重置全服瘟疫状态（保留用户跨群发言记录，便于继续游玩）"""
        await self.execute("DELETE FROM groups")
        await self.execute("DELETE FROM infections")
        await self.execute("DELETE FROM antidote_contributions")
        await self.execute(
            "UPDATE plague_state SET day_count = 0, last_daily_reset = NULL, "
            "current_event = NULL, super_spreader_user = NULL, super_spreader_date = NULL, "
            "event_type = NULL, event_scheduled_hm = NULL, event_fired_date = NULL, "
            "last_reset_date = NULL, last_report_date = NULL, last_decay_at = NULL "
            "WHERE id = 1"
        )
