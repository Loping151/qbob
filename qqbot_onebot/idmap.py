"""openid <-> 15 位虚拟号([1e14, 1e15)) 映射.

虚拟号 = blake2b(kind:appid:openid) 散列进号段, 同一 openid 在不同 bot 下号不同;
撞号靠唯一约束检测后加盐重试. 虚拟号全局唯一, 反查无需 bot 上下文(「启用」跨 bot 加白靠它).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field

from .db import Database

VIRTUAL_BASE = 10**14
VIRTUAL_SPAN = 9 * 10**14  # [1e14, 1e15)
# author.avatar 的签名 key 每条消息都换, 内存随时更新, 落库限频(旧 key 照样能用)
AVATAR_SAVE_INTERVAL = 3600


def _hash_candidate(kind: str, bot_appid: str, openid: str, salt: int) -> int:
    payload = f"{kind}:{bot_appid}:{openid}:{salt}".encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return VIRTUAL_BASE + int.from_bytes(digest, "big") % VIRTUAL_SPAN


def is_virtual_id(value: int) -> bool:
    return VIRTUAL_BASE <= value < VIRTUAL_BASE * 10


@dataclass
class MapEntry:
    virtual_id: int
    bot_appid: str
    kind: str  # 'user' | 'group' | 'bot'
    openid: str
    nickname: str = ""
    avatar: str = ""          # 平台下发的头像链接, 空=没下发过
    avatar_saved: float = field(default=0.0, compare=False)


class IdMap:
    def __init__(self, db: Database):
        self.db = db
        self._by_key: dict[tuple[str, str, str], MapEntry] = {}
        self._by_virtual: dict[int, MapEntry] = {}

    async def load(self) -> None:
        rows = await self.db.fetchall(
            "SELECT virtual_id, bot_appid, kind, openid, nickname, avatar FROM id_map"
        )
        for row in rows:
            entry = MapEntry(
                row["virtual_id"], row["bot_appid"], row["kind"],
                row["openid"], row["nickname"], row["avatar"] or "",
            )
            self._cache(entry)

    def forget_bot(self, bot_appid: str) -> int:
        """删 bot 时清内存缓存(库里由 manager 删)."""
        dead = [e for e in self._by_virtual.values() if e.bot_appid == bot_appid]
        for entry in dead:
            self._by_virtual.pop(entry.virtual_id, None)
            self._by_key.pop((entry.bot_appid, entry.kind, entry.openid), None)
        return len(dead)

    def _cache(self, entry: MapEntry) -> None:
        self._by_key[(entry.bot_appid, entry.kind, entry.openid)] = entry
        self._by_virtual[entry.virtual_id] = entry

    async def to_virtual(
        self,
        bot_appid: str,
        kind: str,
        openid: str,
        nickname: str = "",
        union_openid: str | None = None,
        avatar: str = "",
    ) -> int:
        """取(或建)openid 的虚拟号, 顺带更新昵称与头像链接."""
        key = (bot_appid, kind, openid)
        cached = self._by_key.get(key)
        now = int(time.time())
        if cached is not None:
            renamed = bool(nickname) and nickname != cached.nickname
            save_avatar = bool(avatar) and avatar != cached.avatar and (
                renamed or not cached.avatar
                or now - cached.avatar_saved >= AVATAR_SAVE_INTERVAL)
            if renamed:
                cached.nickname = nickname
            if avatar:
                cached.avatar = avatar
            if renamed or save_avatar:
                if save_avatar:
                    cached.avatar_saved = now
                await self.db.execute(
                    "UPDATE id_map SET nickname=?, avatar=?, last_seen=? WHERE virtual_id=?",
                    (cached.nickname, cached.avatar, now, cached.virtual_id),
                )
            return cached.virtual_id

        # 不在缓存 -> 插入; 唯一约束兜住并发/撞号
        for salt in range(64):
            candidate = _hash_candidate(kind, bot_appid, openid, salt)
            try:
                await self.db.execute(
                    "INSERT INTO id_map (virtual_id, bot_appid, kind, openid,"
                    " union_openid, nickname, first_seen, last_seen, avatar)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (candidate, bot_appid, kind, openid, union_openid, nickname,
                     now, now, avatar),
                )
            except Exception:
                # virtual_id 撞号 -> 换盐重试; (bot,kind,openid) 已存在 -> 读回
                row = await self.db.fetchone(
                    "SELECT virtual_id, nickname, avatar FROM id_map"
                    " WHERE bot_appid=? AND kind=? AND openid=?",
                    key,
                )
                if row is not None:
                    entry = MapEntry(row["virtual_id"], bot_appid, kind, openid,
                                     row["nickname"], avatar or row["avatar"] or "")
                    self._cache(entry)
                    return entry.virtual_id
                continue
            entry = MapEntry(candidate, bot_appid, kind, openid, nickname, avatar,
                             avatar_saved=now if avatar else 0.0)
            self._cache(entry)
            return candidate
        raise RuntimeError(f"id_map: exhausted salts for {key}")

    def lookup_virtual(self, virtual_id: int) -> MapEntry | None:
        return self._by_virtual.get(virtual_id)

    def lookup_openid(self, bot_appid: str, kind: str, openid: str) -> MapEntry | None:
        return self._by_key.get((bot_appid, kind, openid))

    async def resolve_for_bot(
        self, bot_appid: str, kind: str, virtual_id: int
    ) -> str | None:
        """虚拟号 -> openid, 校验属于指定 bot 与类型."""
        entry = self._by_virtual.get(virtual_id)
        if entry is None or entry.bot_appid != bot_appid or entry.kind != kind:
            return None
        return entry.openid

    @staticmethod
    def _search_filters(query: str, kind: str) -> tuple[list[str], list]:
        wheres, args = [], []
        if query:
            like = f"%{query}%"
            wheres.append("(CAST(virtual_id AS TEXT) LIKE ? OR openid LIKE ?"
                          " OR nickname LIKE ? OR bot_appid LIKE ?)")
            args += [like, like, like, like]
        if kind in ("user", "group", "bot"):
            wheres.append("kind=?"); args.append(kind)
        return wheres, args

    async def search(self, query: str, limit: int = 50, offset: int = 0,
                     kind: str = "") -> list[dict]:
        wheres, args = self._search_filters(query, kind)
        sql = "SELECT * FROM id_map"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        sql += " ORDER BY last_seen DESC, virtual_id DESC LIMIT ? OFFSET ?"
        args += [limit, offset]
        rows = await self.db.fetchall(sql, args)
        return [dict(r) for r in rows]

    async def search_count(self, query: str = "", kind: str = "") -> int:
        wheres, args = self._search_filters(query, kind)
        sql = "SELECT COUNT(*) AS n FROM id_map"
        if wheres:
            sql += " WHERE " + " AND ".join(wheres)
        row = await self.db.fetchone(sql, args)
        return int(row["n"]) if row else 0
