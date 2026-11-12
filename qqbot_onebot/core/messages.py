"""消息存储与被动回复台账.

主动消息限量, 优先被动回复: 群聊每条 msg_id 5 分钟内可回 5 条, 单聊 60 分钟内 4 条,
同一 msg_id 的多条回复用递增 msg_seq 区分. OneBot message_id 为 int32 自增, 起点随机.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time

from ..db import Database
from ..onebot.segments import with_file_ids

logger = logging.getLogger("qqbot.messages")

GROUP_PASSIVE_WINDOW = 300       # 5 min
GROUP_PASSIVE_BUDGET = 5
C2C_PASSIVE_WINDOW = 3600        # 60 min
C2C_PASSIVE_BUDGET = 4
MID_MIN, MID_MAX = 10**8, 2**31 - 10**6  # int32 内, 起始随机
MID_BLOCK = 200   # 每次预留多少个 id(减少写库次数)


MAX_FIELD_CHARS = 512   # 单字段截断长度(base64 图片可达几 MB)
MAX_NESTED_ITEMS = 50   # 嵌套 list 保留项数
MAX_TOTAL_CHARS = 32768  # 单条消息落库总上限


def _shrink(value, depth: int = 0):
    """递归截断长字符串; 须下钻 list/dict, node 段的 data.content 里也有 base64 图."""
    if isinstance(value, str):
        if len(value) > MAX_FIELD_CHARS:
            return f"{value[:32]}…<省略 {len(value) - 32} 字符>"
        return value
    if depth >= 4:                      # 防御畸形/自引用结构
        return "…"
    if isinstance(value, list):
        return [_shrink(v, depth + 1) for v in value[:MAX_NESTED_ITEMS]]
    if isinstance(value, dict):
        return {k: _shrink(v, depth + 1) for k, v in value.items()}
    return value


def sanitize_segments(segments) -> list[dict]:
    """入库前截断超长字段并限制总长; 媒体本体已在 data/media, 库里只留标记.
    媒体段缺 file_id 的补上: get_msg / 引用回查读的就是库里这份."""
    out: list[dict] = []
    for seg in with_file_ids([seg for seg in segments or [] if isinstance(seg, dict)]):
        data = {k: _shrink(v) for k, v in (seg.get("data") or {}).items()}
        out.append({"type": seg.get("type", "text"), "data": data})
    # 兜底: 段数极多时总量仍可能超限
    encoded = json.dumps(out, ensure_ascii=False)
    if len(encoded) > MAX_TOTAL_CHARS:
        kept, size = [], 0
        for seg in out:
            size += len(json.dumps(seg, ensure_ascii=False))
            if size > MAX_TOTAL_CHARS:
                kept.append({"type": "text", "data": {
                    "text": f"…<超长消息, 省略 {len(out) - len(kept)} 段>"}})
                break
            kept.append(seg)
        return kept
    return out


class MessageStore:
    def __init__(self, db: Database):
        self.db = db
        self._mid_next = 0
        self._mid_reserved = 0
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        stored = await self.db.kv_get("mid_next")
        if stored is None:
            self._mid_next = MID_MIN + secrets.randbelow(10**9)
        else:
            self._mid_next = int(stored)   # 水位线, 从这继续
        self._mid_reserved = self._mid_next

    async def _alloc_mid(self) -> int:
        """按块预留 id, 库里只存水位线; 崩溃最多浪费一块, 省去每条一次写事务."""
        async with self._lock:
            if self._mid_next >= self._mid_reserved:
                self._mid_reserved = self._mid_next + MID_BLOCK
                if self._mid_reserved >= MID_MAX:
                    self._mid_next = MID_MIN
                    self._mid_reserved = MID_MIN + MID_BLOCK
                await self.db.kv_set("mid_next", str(self._mid_reserved))
            mid = self._mid_next
            self._mid_next += 1
            return mid

    async def record_incoming(
        self,
        bot_appid: str,
        chat_type: str,
        peer_openid: str,
        peer_virtual: int,
        user_virtual: int,
        qq_msg_id: str,
        segments: list[dict],
        sender: dict,
        ts: int,
        msg_idx: str = "",
    ) -> int:
        mid = await self._alloc_mid()
        window = GROUP_PASSIVE_WINDOW if chat_type == "group" else C2C_PASSIVE_WINDOW
        budget = GROUP_PASSIVE_BUDGET if chat_type == "group" else C2C_PASSIVE_BUDGET
        await self.db.execute(
            "INSERT INTO messages (mid, bot_appid, direction, chat_type, peer_openid,"
            " peer_virtual, user_virtual, qq_msg_id, msg_idx, content, sender, ts,"
            " passive_expire, passive_budget, seq_used)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                mid, bot_appid, "in", chat_type, peer_openid, peer_virtual,
                user_virtual, qq_msg_id, msg_idx,
                json.dumps(sanitize_segments(segments), ensure_ascii=False),
                json.dumps(sender, ensure_ascii=False), ts, ts + window, budget,
            ),
        )
        return mid

    async def record_outgoing(
        self,
        bot_appid: str,
        chat_type: str,
        peer_openid: str,
        peer_virtual: int,
        bot_virtual: int,
        qq_msg_id: str,
        segments: list[dict],
        ts: int | None = None,
        msg_idx: str = "",
        media_sizes: list[int] | None = None,
        batch_mid: int | None = None,
        batch_self: bool = False,
        sent_text: str = "",
    ) -> int:
        """batch_self=True: 本行是批次首片, batch_mid 直接取自己的 mid."""
        mid = await self._alloc_mid()
        if batch_self:
            batch_mid = mid
        await self.db.execute(
            "INSERT INTO messages (mid, bot_appid, direction, chat_type, peer_openid,"
            " peer_virtual, user_virtual, qq_msg_id, msg_idx, content, sender, ts,"
            " media_sizes, batch_mid, sent_text)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                mid, bot_appid, "out", chat_type, peer_openid, peer_virtual,
                bot_virtual, qq_msg_id, msg_idx,
                json.dumps(sanitize_segments(segments), ensure_ascii=False), "{}",
                ts or int(time.time()),
                json.dumps(media_sizes) if media_sizes else "",
                batch_mid or 0,
                sent_text,
            ),
        )
        return mid

    async def update_content(self, mid: int, segments: list[dict]) -> None:
        await self.db.execute(
            "UPDATE messages SET content=? WHERE mid=?",
            (json.dumps(sanitize_segments(segments), ensure_ascii=False), mid))

    async def seen(self, bot_appid: str, qq_msg_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT mid FROM messages WHERE bot_appid=? AND qq_msg_id=? AND direction='in'",
            (bot_appid, qq_msg_id),
        )
        return row is not None

    async def get_by_mid(self, mid: int) -> dict | None:
        row = await self.db.fetchone("SELECT * FROM messages WHERE mid=?", (mid,))
        if row is None:
            return None
        record = dict(row)
        record["content"] = json.loads(record["content"])
        record["sender"] = json.loads(record["sender"])
        return record

    async def recent_for_chat(
        self, bot_appid: str, chat_type: str, peer_openid: str, limit: int = 20,
        before_mid: int | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT * FROM messages WHERE bot_appid=? AND chat_type=? AND peer_openid=?"
        )
        args: list = [bot_appid, chat_type, peer_openid]
        if before_mid:
            sql += " AND mid < ?"
            args.append(before_mid)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        rows = await self.db.fetchall(sql, args)
        out = []
        for row in rows:
            record = dict(row)
            record["content"] = json.loads(record["content"])
            record["sender"] = json.loads(record["sender"])
            out.append(record)
        return list(reversed(out))

    # ---------------- 被动回复选取 ----------------

    async def record_event_credential(
        self, bot_appid: str, chat_type: str, peer_openid: str,
        event_id: str, event_type: str = "", ts: int | None = None,
    ) -> None:
        """记下通知事件的 event_id 作被动凭据(如入群欢迎, 不占主动配额)."""
        if not event_id:
            return
        ts = ts or int(time.time())
        window = GROUP_PASSIVE_WINDOW if chat_type == "group" else C2C_PASSIVE_WINDOW
        budget = GROUP_PASSIVE_BUDGET if chat_type == "group" else C2C_PASSIVE_BUDGET
        await self.db.execute(
            "INSERT INTO passive_events (bot_appid, chat_type, peer_openid,"
            " event_id, event_type, ts, expire, budget, seq_used)"
            " VALUES (?,?,?,?,?,?,?,?,0)",
            (bot_appid, chat_type, peer_openid, event_id, event_type,
             ts, ts + window, budget),
        )

    async def revoke_reply_slot(self, bot_appid: str, slot: tuple) -> None:
        """平台拒了这个被动凭据(过期/无效/该事件不能回复): 作废, 之后不再选它."""
        credential, _, kind = slot
        sql = ("UPDATE passive_events SET expire=0 WHERE bot_appid=? AND event_id=?"
               if kind == "event" else
               "UPDATE messages SET passive_expire=0 WHERE bot_appid=? AND qq_msg_id=?")
        async with self._lock:
            await self.db.execute(sql, (bot_appid, credential))

    async def acquire_reply_slot(
        self,
        bot_appid: str,
        chat_type: str,
        peer_openid: str,
        prefer_mid: int | None = None,
    ) -> tuple[str, int, str] | None:
        """原子占用一个被动回复名额, 返回 (id, msg_seq, 'msg'|'event'), 无可用则 None.

        消息与事件凭据取较新者; 指定 prefer_mid 时消息凭据优先.
        """
        async with self._lock:
            now = int(time.time())
            row = None
            if prefer_mid is not None:
                row = await self.db.fetchone(
                    "SELECT mid, qq_msg_id, seq_used, passive_budget, ts FROM messages"
                    " WHERE mid=? AND direction='in' AND passive_expire > ?"
                    " AND seq_used < passive_budget"
                    " AND bot_appid=? AND chat_type=? AND peer_openid=?",
                    (prefer_mid, now, bot_appid, chat_type, peer_openid),
                )
            if row is None:
                row = await self.db.fetchone(
                    "SELECT mid, qq_msg_id, seq_used, passive_budget, ts FROM messages"
                    " WHERE bot_appid=? AND chat_type=? AND peer_openid=?"
                    " AND direction='in' AND passive_expire > ?"
                    " AND seq_used < passive_budget"
                    " ORDER BY ts DESC LIMIT 1",
                    (bot_appid, chat_type, peer_openid, now),
                )
            # 事件凭据
            event_row = await self.db.fetchone(
                "SELECT id, event_id, seq_used, budget, ts FROM passive_events"
                " WHERE bot_appid=? AND chat_type=? AND peer_openid=?"
                " AND expire > ? AND seq_used < budget"
                " ORDER BY ts DESC LIMIT 1",
                (bot_appid, chat_type, peer_openid, now),
            )
            # 显式指定了要回哪条消息时, 消息凭据优先; 否则取时间更近的
            use_event = event_row is not None and (
                row is None or (prefer_mid is None
                                and event_row["ts"] >= row["ts"])
            )
            if use_event:
                seq = event_row["seq_used"] + 1
                await self.db.execute(
                    "UPDATE passive_events SET seq_used=? WHERE id=?",
                    (seq, event_row["id"]),
                )
                return event_row["event_id"], seq, "event"
            if row is None:
                return None
            seq = row["seq_used"] + 1
            await self.db.execute(
                "UPDATE messages SET seq_used=? WHERE mid=?", (seq, row["mid"])
            )
            return row["qq_msg_id"], seq, "msg"
