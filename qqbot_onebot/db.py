"""SQLite storage (aiosqlite, WAL).

Single writer process. All timestamps are unix seconds (int).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

logger = logging.getLogger("qqbot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS id_map (
    virtual_id   INTEGER PRIMARY KEY,          -- 15 位虚拟号, 全局唯一
    bot_appid    TEXT NOT NULL,
    kind         TEXT NOT NULL,                -- 'user' | 'group' | 'bot'
    openid       TEXT NOT NULL,                -- bot 自身用 appid 充当 openid
    union_openid TEXT,
    nickname     TEXT NOT NULL DEFAULT '',
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    avatar       TEXT NOT NULL DEFAULT '',     -- 平台下发的头像链接(author.avatar)
    UNIQUE (bot_appid, kind, openid)
);
CREATE INDEX IF NOT EXISTS idx_idmap_openid ON id_map (openid);
-- 管理台 ID 映射页按 last_seen 倒序分页; 没索引会全表扫 + 临时 B 树排序
CREATE INDEX IF NOT EXISTS idx_idmap_seen ON id_map (last_seen DESC, virtual_id DESC);

CREATE TABLE IF NOT EXISTS group_members (
    bot_appid    TEXT NOT NULL,
    group_openid TEXT NOT NULL,
    user_openid  TEXT NOT NULL,
    nickname     TEXT NOT NULL DEFAULT '',
    role         TEXT NOT NULL DEFAULT 'member',
    last_seen    INTEGER NOT NULL,
    card         TEXT NOT NULL DEFAULT '',     -- 群名片(author.member_nickname)
    PRIMARY KEY (bot_appid, group_openid, user_openid)
);
-- 按 last_seen 清理早已退群的成员(这张表原本只减不增靠成员退群事件)
CREATE INDEX IF NOT EXISTS idx_members_seen ON group_members (last_seen);

CREATE TABLE IF NOT EXISTS messages (
    mid            INTEGER PRIMARY KEY,        -- OneBot int32 message_id
    bot_appid      TEXT NOT NULL,
    direction      TEXT NOT NULL,              -- 'in' | 'out'
    chat_type      TEXT NOT NULL,              -- 'group' | 'private'
    peer_openid    TEXT NOT NULL,              -- group_openid 或对端 user_openid
    peer_virtual   INTEGER NOT NULL,
    user_virtual   INTEGER NOT NULL,           -- 发送者虚拟号(out 时为 bot 虚拟号)
    qq_msg_id      TEXT NOT NULL DEFAULT '',   -- ROBOT1.0_xxx, 用于被动回复/撤回
    msg_idx        TEXT NOT NULL DEFAULT '',   -- REFIDX_xxx, 引用回复只认这个
    content        TEXT NOT NULL DEFAULT '[]', -- OneBot segment JSON
    sender         TEXT NOT NULL DEFAULT '{}',
    ts             INTEGER NOT NULL,
    passive_expire INTEGER NOT NULL DEFAULT 0, -- 被动回复窗口截止, 0=不可用
    passive_budget INTEGER NOT NULL DEFAULT 0,
    seq_used       INTEGER NOT NULL DEFAULT 0,
    media_sizes    TEXT NOT NULL DEFAULT '',    -- 外发媒体字节数(引用兜底匹配用)
    batch_mid      INTEGER NOT NULL DEFAULT 0,  -- 同一逻辑消息的分片共享首片 mid
    recalled_at    INTEGER NOT NULL DEFAULT 0,  -- 撤回时间(0=未撤回)
    sent_text      TEXT NOT NULL DEFAULT ''     -- 外发实际发出的文字(插件改写后与 content 不同)
);
-- 按 qq_msg_id 的查找(去重 / 作废被动凭据)都只找近期入站消息, 走 idx_msg_ts 限定时间窗;
-- 原来的 (bot_appid, qq_msg_id) 索引键是随机串, 每条消息都改一个随机页, 已删(见 DROPPED_INDEXES)
CREATE INDEX IF NOT EXISTS idx_msg_peer ON messages (bot_appid, chat_type, peer_openid, ts);
CREATE INDEX IF NOT EXISTS idx_msg_ts ON messages (ts);
-- 引用回复入站要按 msg_idx 反查(每条带引用的消息一次), 没索引就是全扫该 bot
CREATE INDEX IF NOT EXISTS idx_msg_ref ON messages (bot_appid, msg_idx);
-- 平台引用时可能换 idx: 按内容找回后把观测到的 idx 记成别名, 不覆盖消息本来的 idx
CREATE TABLE IF NOT EXISTS msg_aliases (
    bot_appid  TEXT NOT NULL,
    msg_idx    TEXT NOT NULL,
    mid        INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    PRIMARY KEY (bot_appid, msg_idx)
);
-- 撤回时取同一批的分片; 只有真被拆过的行有值, 部分索引足够
CREATE INDEX IF NOT EXISTS idx_msg_batch ON messages (bot_appid, batch_mid)
    WHERE batch_mid != 0;

-- 合并转发/聊天记录: 内容随消息事件一起下发(msg_elements), 存下来供
-- get_forward_msg 按 id 回查.
-- 通知类事件(入群/加bot/收到主动消息许可等)自带 event_id, 可当被动回复凭据用:
-- 用它回消息不吃主动消息配额, 群里还会自动 @ 触发者(入群欢迎就靠这个).
CREATE TABLE IF NOT EXISTS passive_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_appid   TEXT NOT NULL,
    chat_type   TEXT NOT NULL,
    peer_openid TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    event_type  TEXT NOT NULL DEFAULT '',
    ts          INTEGER NOT NULL,
    expire      INTEGER NOT NULL,
    budget      INTEGER NOT NULL DEFAULT 5,
    seq_used    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_passive_events ON passive_events
    (bot_appid, chat_type, peer_openid, expire);

CREATE TABLE IF NOT EXISTS forwards (
    id        TEXT PRIMARY KEY,
    bot_appid TEXT NOT NULL,
    nodes     TEXT NOT NULL DEFAULT '[]',
    raw       TEXT NOT NULL DEFAULT '',
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forwards_ts ON forwards (ts);

-- 出站合并转发渲染成的网页(core/forwardpage.py): 平台发不了合并转发, 摊平又
-- 刷屏且每张图都要我们出流量, 所以存一页 HTML 的料, 群里只发链接.
-- token = 内容哈希, 同一份转发广播到多个群只存一行.
CREATE TABLE IF NOT EXISTS forward_pages (
    token     TEXT PRIMARY KEY,
    bot_appid TEXT NOT NULL,
    nodes     TEXT NOT NULL DEFAULT '[]',
    ts        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_forward_pages_ts ON forward_pages (ts);

-- 富媒体直传拿到的 file_info 按内容哈希缓存(实测 ttl 86400s 且跨群通用),
-- 同一张图广播到 N 个群只需上传一次
CREATE TABLE IF NOT EXISTS media_cache (
    bot_appid  TEXT NOT NULL,
    digest     TEXT NOT NULL,
    file_type  INTEGER NOT NULL,
    scene      TEXT NOT NULL,          -- group / private: 群传的只能发群
    file_info  TEXT NOT NULL,
    file_uuid  TEXT NOT NULL DEFAULT '',
    raw_url    TEXT NOT NULL DEFAULT '',    -- COS 签名下载链接, 1 小时(见 raw_until)
    raw_until  INTEGER NOT NULL DEFAULT 0,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (bot_appid, digest, file_type, scene)
);
CREATE INDEX IF NOT EXISTS idx_media_cache_exp ON media_cache (expires_at);

CREATE TABLE IF NOT EXISTS bots (
    appid              TEXT PRIMARY KEY,
    secret             TEXT NOT NULL,
    name               TEXT NOT NULL DEFAULT '',
    bot_qq             INTEGER NOT NULL DEFAULT 0,   -- 平台展示的机器人QQ号(仅展示用)
    avatar             TEXT NOT NULL DEFAULT '',     -- /users/@me 给的头像直链
    enabled            INTEGER NOT NULL DEFAULT 1,
    event_mode         TEXT NOT NULL DEFAULT 'websocket', -- 'websocket'(默认,免配回调) | 'webhook'
    onebot_endpoints   TEXT NOT NULL DEFAULT '[]',   -- [{"url":..., "access_token":...}]
    superusers         TEXT NOT NULL DEFAULT '[]',   -- 本 bot 的 su 虚拟号列表(int)
    tags               TEXT NOT NULL DEFAULT '[]',
    grp                TEXT NOT NULL DEFAULT '',     -- 前端分组名
    notes              TEXT NOT NULL DEFAULT '',
    group_list_mode    TEXT NOT NULL DEFAULT 'white',
    private_list_mode  TEXT NOT NULL DEFAULT 'black',
    markdown_enabled   INTEGER NOT NULL DEFAULT 1,   -- 允许用 markdown 实现 @用户
    -- bot 自己发的消息也当成事件下发一份(平台不回显, 由本框架合成).
    -- 注意: 对每条消息都作答的插件会把自己的输出再吃回去, 有自我对话的风险.
    report_self_message INTEGER NOT NULL DEFAULT 1,
    -- 透传回调地址: 平台原始事件签名后 POST 过去(见 core/passthrough.py)
    passthrough_webhooks TEXT NOT NULL DEFAULT '[]',
    created_at         INTEGER NOT NULL
);

-- 每 (bot, 群/私聊对端) 的平台侧状态缓存.
-- 群: GET /v2/groups/{group_openid}/bot_state (allow_proactive_msg /
--     recv_msg_setting: all|only_mention|mention_and_context / bot 的 member_role).
-- 私聊: 无查询接口, 仅由 C2C_MSG_RECEIVE / C2C_MSG_REJECT 事件更新 allow_proactive.
CREATE TABLE IF NOT EXISTS peer_states (
    bot_appid        TEXT NOT NULL,
    chat_type        TEXT NOT NULL,               -- 'group' | 'private'
    peer_openid      TEXT NOT NULL,
    allow_proactive  INTEGER NOT NULL DEFAULT 0,
    recv_msg_setting TEXT NOT NULL DEFAULT '',
    bot_role         TEXT NOT NULL DEFAULT '',    -- bot 在群内角色
    bot_openid       TEXT NOT NULL DEFAULT '',    -- bot 在该群的 member_openid
    joined_at        TEXT NOT NULL DEFAULT '',
    checked_at       INTEGER NOT NULL DEFAULT 0,
    api_ok           INTEGER NOT NULL DEFAULT 0,  -- 上次 bot_state 查询是否成功
    proactive_known  INTEGER NOT NULL DEFAULT 0,  -- allow_proactive 是否有可信来源
    inferred_recv_all INTEGER NOT NULL DEFAULT 0, -- 收到过非@群消息 => recv=all
    member_count     INTEGER NOT NULL DEFAULT 0,  -- group_info 查到的人数缓存
    PRIMARY KEY (bot_appid, chat_type, peer_openid)
);

CREATE TABLE IF NOT EXISTS access_list (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_appid  TEXT NOT NULL,
    chat_type  TEXT NOT NULL,                  -- 'group' | 'private'
    list_type  TEXT NOT NULL,                  -- 'white' | 'black'
    openid     TEXT NOT NULL,
    virtual_id INTEGER NOT NULL DEFAULT 0,
    note       TEXT NOT NULL DEFAULT '',
    added_by   TEXT NOT NULL DEFAULT '',
    added_at   INTEGER NOT NULL,
    UNIQUE (bot_appid, chat_type, list_type, openid)
);

CREATE TABLE IF NOT EXISTS web_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user', -- 'admin' | 'advanced' | 'user'
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# (表, 列, 列定义) —— 老库启动时自动 ALTER 补齐
MIGRATIONS = [
    ("messages", "msg_idx", "msg_idx TEXT NOT NULL DEFAULT ''"),
    ("bots", "avatar", "avatar TEXT NOT NULL DEFAULT ''"),
    ("peer_states", "bot_openid", "bot_openid TEXT NOT NULL DEFAULT ''"),
    ("peer_states", "api_ok", "api_ok INTEGER NOT NULL DEFAULT 0"),
    ("peer_states", "proactive_known", "proactive_known INTEGER NOT NULL DEFAULT 0"),
    ("peer_states", "inferred_recv_all",
     "inferred_recv_all INTEGER NOT NULL DEFAULT 0"),
    # 外发媒体字节数(JSON 数组): 平台投递时可能换 idx, 引用对不上时按大小归属
    ("messages", "media_sizes", "media_sizes TEXT NOT NULL DEFAULT ''"),
    # 一条逻辑消息拆成多条时各分片共享首片 mid, 撤回整批撤
    ("messages", "batch_mid", "batch_mid INTEGER NOT NULL DEFAULT 0"),
    # 群人数缓存, 供 get_group_list 回填
    ("peer_states", "member_count", "member_count INTEGER NOT NULL DEFAULT 0"),
    # 撤回标记, 免得刷新后又完整显示
    ("messages", "recalled_at", "recalled_at INTEGER NOT NULL DEFAULT 0"),
    # 自身消息上报(同 NapCat report-self-message): 平台不回显, 由框架合成事件
    ("bots", "report_self_message",
     "report_self_message INTEGER NOT NULL DEFAULT 1"),
    # merge 回的 file_uuid = CDN 直链 fileid, 转发网页热链用
    ("media_cache", "file_uuid", "file_uuid TEXT NOT NULL DEFAULT ''"),
    # raw_url 只活 1 小时, 无 rkey 时的备用链接
    ("media_cache", "raw_url", "raw_url TEXT NOT NULL DEFAULT ''"),
    ("media_cache", "raw_until", "raw_until INTEGER NOT NULL DEFAULT 0"),
    ("bots", "passthrough_webhooks",
     "passthrough_webhooks TEXT NOT NULL DEFAULT '[]'"),
    # 引用按内容找回时比对: 转发转网页等改写后, content 记的是原消息
    ("messages", "sent_text", "sent_text TEXT NOT NULL DEFAULT ''"),
    # 2026-11-13 起群消息 author 带 member_nickname(群名片) 与 avatar(头像链接)
    ("group_members", "card", "card TEXT NOT NULL DEFAULT ''"),
    ("id_map", "avatar", "avatar TEXT NOT NULL DEFAULT ''"),
]

# 老库里已不再需要的索引, 启动时删掉
DROPPED_INDEXES = ("idx_msg_qqid",)


class Database:
    # 写入攒批: execute/executemany 不再每条语句单独提交, 最多等 COMMIT_DELAY 秒或攒满
    # COMMIT_MAX 条再一起提交, 消息表和它的几个索引的尾页一次写完. 同一连接上的读看得到
    # 未提交的写; 正常关闭会先提交, 进程被强杀最多丢这段时间的写入. COMMIT_DELAY<=0 即逐条提交.
    # 消息约 1 条/秒时 1 秒窗口一批只有 2 条左右, 3 秒能让热页(消息表尾页/时间索引/活跃会话)多合并几次
    COMMIT_DELAY = 3.0
    COMMIT_MAX = 500

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._pending = 0
        self._commit_task: asyncio.Task | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        assert self._conn is not None, "Database not opened"
        return self._conn

    async def open(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        # 默认 1000 页就 checkpoint; 放到约 16MB, 两次 checkpoint 之间反复改的页只回写一次
        await self._conn.execute("PRAGMA wal_autocheckpoint=4000")
        await self._conn.execute("PRAGMA journal_size_limit=67108864")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        for name in DROPPED_INDEXES:
            await self._conn.execute(f"DROP INDEX IF EXISTS {name}")
        await self._conn.commit()

    async def _migrate(self) -> None:
        """补齐已存在表缺失的列(CREATE TABLE IF NOT EXISTS 不会改老表)."""
        for table, column, ddl in MIGRATIONS:
            cur = await self._conn.execute(f"PRAGMA table_info({table})")
            names = {row[1] for row in await cur.fetchall()}
            await cur.close()
            if column not in names:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    async def close(self) -> None:
        task, self._commit_task = self._commit_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._conn is not None:
            async with self._lock:
                await self._commit_locked()
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, args: Iterable[Any] = ()) -> aiosqlite.Cursor:
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(args))
            await self._wrote_locked()
            return cur

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        async with self._lock:
            await self.conn.executemany(sql, [tuple(r) for r in rows])
            await self._wrote_locked()

    async def flush(self) -> None:
        """立即提交攒着的写入."""
        async with self._lock:
            await self._commit_locked()

    async def _wrote_locked(self) -> None:
        self._pending += 1
        if self.COMMIT_DELAY <= 0 or self._pending >= self.COMMIT_MAX:
            await self._commit_locked()
        elif self._commit_task is None:
            self._commit_task = asyncio.create_task(self._delayed_commit())

    async def _commit_locked(self) -> None:
        if self._pending:
            self._pending = 0
            await self.conn.commit()

    async def _delayed_commit(self) -> None:
        try:
            await asyncio.sleep(self.COMMIT_DELAY)
            async with self._lock:
                self._commit_task = None
                await self._commit_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            # 提交失败时写入仍留在事务里, 下一次写入会重新安排提交
            self._commit_task = None
            logger.exception("deferred commit failed")

    async def fetchone(self, sql: str, args: Iterable[Any] = ()) -> aiosqlite.Row | None:
        cur = await self.conn.execute(sql, tuple(args))
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, args: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        cur = await self.conn.execute(sql, tuple(args))
        rows = await cur.fetchall()
        await cur.close()
        return list(rows)

    async def vacuum(self) -> None:
        """VACUUM 回收磁盘空间. 读不走锁, 撞上进行中的查询会失败, 转成 RuntimeError."""
        import sqlite3  # noqa: PLC0415
        async with self._lock:
            try:
                await self._commit_locked()     # VACUUM 不能在事务里跑
                await self.conn.execute("VACUUM")
                await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError as exc:
                raise RuntimeError(f"数据库正忙, 稍后再试({exc})") from exc

    # ---- kv ----

    async def kv_get(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM kv WHERE key=?", (key,))
        return row["value"] if row else default

    async def kv_set(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- maintenance ----

    async def _delete_chunked(self, sql: str, args: Iterable[Any],
                              chunk: int = 5000) -> int:
        """分块删除, 批间让出事件循环: 大 DELETE 会独占 aiosqlite 唯一工作线程数秒."""
        removed = 0
        while True:
            cur = await self.execute(sql, list(args) + [chunk])
            count = cur.rowcount or 0
            removed += count
            if count < chunk:
                return removed
            await asyncio.sleep(0.05)     # 让出线程

    async def cleanup(self, message_ttl_days: int) -> None:
        now = int(time.time())
        cutoff = now - message_ttl_days * 86400
        await self._delete_chunked(
            "DELETE FROM messages WHERE mid IN ("
            " SELECT mid FROM messages WHERE ts < ? LIMIT ?)", (cutoff,))
        await self._delete_chunked(
            "DELETE FROM msg_aliases WHERE rowid IN ("
            " SELECT rowid FROM msg_aliases WHERE ts < ? LIMIT ?)", (cutoff,))
        # 合并转发内容随消息过期
        await self._delete_chunked(
            "DELETE FROM forwards WHERE id IN ("
            " SELECT id FROM forwards WHERE ts < ? LIMIT ?)", (cutoff,))
        await self.execute(
            "DELETE FROM forwards WHERE id IN ("
            " SELECT id FROM forwards ORDER BY ts DESC LIMIT -1 OFFSET 50000)"
        )
        # 转发网页随消息过期, 不让聊天记录永久挂在公网
        await self._delete_chunked(
            "DELETE FROM forward_pages WHERE token IN ("
            " SELECT token FROM forward_pages WHERE ts < ? LIMIT ?)", (cutoff,))
        # 总量兜底, 防止 TTL 内异常爆量
        await self._delete_chunked(
            "DELETE FROM messages WHERE mid IN (SELECT mid FROM ("
            "  SELECT mid FROM messages ORDER BY ts DESC LIMIT -1 OFFSET 500000"
            ") LIMIT ?)", ())
        # 过期 file_info 平台已失效
        await self.execute("DELETE FROM media_cache WHERE expires_at < ?", (now,))
        # 过期的被动回复凭据
        await self._delete_chunked(
            "DELETE FROM passive_events WHERE id IN ("
            " SELECT id FROM passive_events WHERE expire < ? LIMIT ?)",
            (now - 3600,))
        # 久未出现(多半已退群)的成员缓存
        await self._delete_chunked(
            "DELETE FROM group_members WHERE rowid IN ("
            " SELECT rowid FROM group_members WHERE last_seen < ? LIMIT ?)",
            (now - 90 * 86400,))


BOT_JSON_COLUMNS = ("onebot_endpoints", "superusers", "tags", "passthrough_webhooks")


async def insert_bot(db: Database, config, fields: dict, ignore_existing: bool = False) -> None:
    """新建 bot 行, 各建 bot 入口共用默认值. config 可为空(测试); 私聊恒为黑名单模式."""
    from .core import groups  # noqa: PLC0415 避免循环导入

    row = {
        "name": "", "bot_qq": 0, "enabled": 1, "event_mode": "websocket",
        "onebot_endpoints": [], "superusers": [], "tags": [], "notes": "",
        "group_list_mode": getattr(config, "default_group_list_mode", "white"),
        "markdown_enabled": 1, "report_self_message": 1, "passthrough_webhooks": [],
        **{k: v for k, v in fields.items() if v is not None},
    }
    if row["group_list_mode"] not in ("white", "black"):
        row["group_list_mode"] = "white"
    if row["event_mode"] not in ("webhook", "websocket"):
        row["event_mode"] = "websocket"
    row["grp"] = (groups.ensure(config, str(fields.get("grp") or ""))
                  if config is not None and hasattr(config, "bot_groups")
                  else str(fields.get("grp") or "默认"))
    columns = ["appid", "secret", "name", "bot_qq", "enabled", "event_mode",
               "onebot_endpoints", "superusers", "tags", "grp", "notes",
               "group_list_mode", "private_list_mode", "markdown_enabled",
               "report_self_message", "passthrough_webhooks", "created_at"]
    values = {**row, "private_list_mode": "black", "created_at": int(time.time())}
    for key in BOT_JSON_COLUMNS:
        values[key] = json.dumps(values[key], ensure_ascii=False)
    for key in ("bot_qq", "enabled", "markdown_enabled", "report_self_message"):
        values[key] = int(values[key] or 0)
    await db.execute(
        f"INSERT {'OR IGNORE ' if ignore_existing else ''}INTO bots"
        f" ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [values[c] for c in columns])


def row_to_bot(row: aiosqlite.Row) -> dict:
    bot = dict(row)
    for key in BOT_JSON_COLUMNS:
        try:
            bot[key] = json.loads(bot.get(key) or "[]")
        except (TypeError, ValueError):
            bot[key] = []
    bot["enabled"] = bool(bot["enabled"])
    bot["markdown_enabled"] = bool(bot["markdown_enabled"])
    bot["report_self_message"] = bool(bot.get("report_self_message"))
    return bot
