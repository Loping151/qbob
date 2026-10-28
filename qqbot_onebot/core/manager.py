"""多 bot 管理器: 装配共享组件, 管理 BotInstance 生命周期."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp

from ..config import ServerConfig
from ..db import Database, insert_bot, row_to_bot
from ..idmap import IdMap
from ..plugin import registry as plugins
from . import cdnkeys, groups
from .access import AccessControl
from .bot import BotInstance, PeerState
from .media import MediaStore
from .messages import MessageStore
from .provision import ProvisionConfig
from .updater import Updater

logger = logging.getLogger("qqbot.manager")


class BotManager:
    def __init__(self, config: ServerConfig):
        self.config = config
        self.db = Database(config.db_path)
        self.idmap = IdMap(self.db)
        self.access = AccessControl(self.db)
        self.store = MessageStore(self.db)
        self.media = MediaStore(config.media_dir, config.media_ttl_hours)
        self.http: aiohttp.ClientSession | None = None
        self.bots: dict[str, BotInstance] = {}
        self._cleanup_task: asyncio.Task | None = None
        self._reload_locks: dict[str, asyncio.Lock] = {}
        # task_id -> BindTask(含解密 key, 仅内存)
        self.bind_tasks: dict = {}
        self.provision = ProvisionConfig(
            Path(config.db_path).parent / "provision.json"
        )
        # 串行配置后端, 防挑到同一端口
        self.provision_lock = asyncio.Lock()
        self.reserved_ports: set[int] = set()   # 已分配未落盘
        self.updater = Updater(self)

    def _reload_lock(self, appid: str) -> asyncio.Lock:
        if appid not in self._reload_locks:
            self._reload_locks[appid] = asyncio.Lock()
        return self._reload_locks[appid]

    def apply_options(self) -> None:
        """把全局选项推到运行态."""
        PeerState.require_recv_all = bool(self.config.require_recv_all)

    async def start(self) -> None:
        self.apply_options()
        plugins.set_state_path(Path(self.config.db_path).parent / "plugins.json")
        plugins.ensure_loaded()
        await self.db.open()
        cdnkeys.load(await self.db.kv_get("cdn_rkeys"))
        await self.idmap.load()
        await self.access.load()
        await self.store.load()
        self.http = aiohttp.ClientSession()
        await self._cleanup_bogus_idmap()
        await self._seed_bots()
        await groups.adopt_existing(self.db, self.config)
        await self.mark_setup_done()
        rows = await self.db.fetchall("SELECT * FROM bots ORDER BY created_at")
        for row in rows:
            bot_cfg = row_to_bot(row)
            if bot_cfg["enabled"]:
                await self._launch(bot_cfg)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self.updater.start()
        logger.info("manager started with %d bot(s)", len(self.bots))

    async def stop(self) -> None:
        self.updater.stop()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
        for bot in list(self.bots.values()):
            await bot.stop()
        self.bots.clear()
        if self.http is not None:
            await self.http.close()
        await self.db.close()

    async def _cleanup_bogus_idmap(self) -> None:
        """清掉把 <@all> 等当成 openid 的假用户映射."""
        cur = await self.db.execute(
            "DELETE FROM id_map WHERE kind='user' AND openid IN"
            " ('all', 'everyone', '全体成员')"
        )
        if cur.rowcount:
            logger.info("清理假 id_map 映射 %d 条(@全体成员 曾被当成用户)",
                        cur.rowcount)
            self.idmap._by_key.clear()
            self.idmap._by_virtual.clear()
            await self.idmap.load()

    async def _seed_bots(self) -> None:
        row = await self.db.fetchone("SELECT COUNT(*) AS n FROM bots")
        if row and row["n"] > 0:
            return
        for seed in self.config.seed_bots:
            if not seed.get("appid") or not seed.get("secret"):
                continue
            await insert_bot(self.db, self.config,
                             {**seed, "appid": str(seed["appid"]),
                              "secret": str(seed["secret"])}, ignore_existing=True)
            logger.info("seeded bot %s from config", seed["appid"])

    def _build(self, bot_cfg: dict) -> BotInstance:
        assert self.http is not None
        bot = BotInstance(
            bot_cfg,
            db=self.db, idmap=self.idmap, access=self.access,
            store=self.store, media=self.media, http=self.http,
            token_urls=self.config.token_urls, api_bases=self.config.api_bases,
        )
        bot.on_fatal = (lambda reason, appid=bot.appid:
                        asyncio.create_task(self.auto_disable_bot(appid, reason)))
        # 内置指令需要回引 manager
        bot.manager = self
        return bot

    async def _launch(self, bot_cfg: dict, muted: bool = False) -> None:
        bot = self._build(bot_cfg)
        bot.muted = muted
        self.bots[bot.appid] = bot
        try:
            await bot.start()
        except Exception:
            logger.exception("bot %s failed to start", bot.appid)
            return
        if not muted:
            self._maybe_set_default(bot)

    async def mark_setup_done(self) -> None:
        """出现第二个 bot 或任何 su 即置 setup_done; 只会变 true."""
        if self.config.setup_done:
            return
        rows = await self.db.fetchall("SELECT superusers FROM bots")
        if (len(rows) > 1 or self.config.superusers
                or any(json.loads(r["superusers"] or "[]") for r in rows)):
            self.config.setup_done = True
            self.config.save()

    def _maybe_set_default(self, bot: BotInstance) -> None:
        """无默认 bot 时, 首个取到身份(凭据有效)的 bot 成为默认."""
        if self.config.default_bot or not bot.me_info:
            return
        self.config.default_bot = bot.appid
        try:
            self.config.save()
        except OSError as exc:
            logger.warning("默认 bot 写回配置失败: %s", exc)
        logger.info("默认 bot: %s", bot.appid)

    async def auto_disable_bot(self, appid: str, reason: str) -> None:
        """不可自愈的错误(如凭据无效): 置 enabled=0 并停实例, 不删配置."""
        logger.error("bot %s 自动禁用: %s", appid, reason)
        await self.db.execute("UPDATE bots SET enabled=0 WHERE appid=?", (appid,))
        await self.reload_bot(appid)

    def get_bot(self, appid: str) -> BotInstance | None:
        return self.bots.get(appid)

    async def reload_bot(self, appid: str, pending: bool = False) -> None:
        """重启单个 bot(串行化, 防双实例抢 BS 连接).

        pending: 号主确认前, 未启用也连网关(扫码页要等 bot 在线才显示已连接),
        但不处理任何事件.
        """
        async with self._reload_lock(appid):
            existing = self.bots.pop(appid, None)
            if existing is not None:
                await existing.stop()
            row = await self.db.fetchone("SELECT * FROM bots WHERE appid=?", (appid,))
            if row is None:
                return
            bot_cfg = row_to_bot(row)
            if bot_cfg["enabled"]:
                await self._launch(bot_cfg)
            elif pending:
                await self._launch(bot_cfg, muted=True)

    async def remove_bot(self, appid: str) -> None:
        """删 bot 并级联清掉其所有数据(否则 id_map 等孤儿常驻内存)."""
        async with self._reload_lock(appid):
            bot = self.bots.pop(appid, None)
            if bot is not None:
                await bot.stop()
            for table in ("messages", "forwards", "forward_pages",
                          "passive_events", "media_cache",
                          "group_members", "peer_states", "access_list", "id_map"):
                await self.db.execute(
                    f"DELETE FROM {table} WHERE bot_appid=?", (appid,))
            await self.db.execute("DELETE FROM bots WHERE appid=?", (appid,))
            self.idmap.forget_bot(appid)
            logger.info("bot %s 及其数据已删除", appid)

    def status(self, with_groups: bool = False) -> list[dict]:
        return [bot.status_snapshot(with_groups) for bot in self.bots.values()]

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                await self.db.cleanup(self.config.message_ttl_days)
                removed = self.media.cleanup()
                if removed:
                    logger.info("media cleanup: removed %d file(s)", removed)
            except Exception:
                logger.exception("cleanup failed")
