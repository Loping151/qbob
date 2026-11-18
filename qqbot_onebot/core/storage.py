"""存储占用一览与手动清理(管理台「设置 → 存储」).

SQLite 删行后文件不缩小, 需 VACUUM 归还磁盘(会锁库几秒)。
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from pathlib import Path

from . import unparsed as unparsed_log

logger = logging.getLogger("qqbot.storage")

# 展示行数的表, 前三张占大头
TABLES = ("messages", "forwards", "forward_pages", "id_map", "group_members",
          "media_cache", "passive_events", "peer_states", "access_list")


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_stats(directory: Path) -> tuple[int, int]:
    total = count = 0
    if directory.is_dir():
        for path in directory.rglob("*"):
            if path.is_file():
                total += _size(path)
                count += 1
    return total, count


def file_log_handler() -> logging.handlers.RotatingFileHandler | None:
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return handler
    return None


def _log_files() -> list[Path]:
    handler = file_log_handler()
    if handler is None:
        return []
    base = Path(handler.baseFilename)
    return sorted(p for p in base.parent.glob(base.name + "*") if p.is_file())


async def snapshot(manager) -> dict:
    config = manager.config
    db_path = Path(config.db_path)
    page_size = (await manager.db.fetchone("PRAGMA page_size"))[0]
    pages = (await manager.db.fetchone("PRAGMA page_count"))[0]
    free = (await manager.db.fetchone("PRAGMA freelist_count"))[0]
    rows = {}
    for table in TABLES:
        row = await manager.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}")
        rows[table] = int(row["n"]) if row else 0
    media_bytes, media_files = await asyncio.to_thread(_dir_stats, Path(config.media_dir))
    log_files = await asyncio.to_thread(_log_files)
    unparsed = unparsed_log.files(db_path.parent)
    handler = file_log_handler()
    return {
        "database": {
            "path": str(db_path),
            "file": _size(db_path),
            "wal": _size(db_path.with_name(db_path.name + "-wal")),
            "used": (pages - free) * page_size,
            "reclaimable": free * page_size,
            "rows": rows,
            "message_ttl_days": config.message_ttl_days,
        },
        "media": {"path": str(config.media_dir), "bytes": media_bytes,
                  "files": media_files, "ttl_hours": config.media_ttl_hours},
        "logs": {
            "path": handler.baseFilename if handler else "",
            "bytes": sum(_size(p) for p in log_files),
            "files": len(log_files),
            "limit": (handler.maxBytes * (handler.backupCount + 1)) if handler else 0,
        },
        "unparsed": {"path": str(unparsed[0]), "bytes": sum(_size(p) for p in unparsed),
                     "limit": 2 * unparsed_log.MAX_BYTES},
    }


async def cleanup(manager, target: str) -> str:
    """target: expired / logs / unparsed / vacuum; 返回结果文案."""
    if target == "expired":
        await manager.db.cleanup(manager.config.message_ttl_days)
        removed = await asyncio.to_thread(manager.media.cleanup)
        return f"已清理过期数据, 删除 {removed} 个过期媒体文件"
    if target == "logs":
        handler = file_log_handler()
        if handler is None:
            return "没有启用日志文件"
        handler.doRollover()
        removed = 0
        for path in _log_files():
            if path != Path(handler.baseFilename):
                path.unlink(missing_ok=True)
                removed += 1
        return f"已清空日志(删除 {removed} 个文件)"
    if target == "unparsed":
        for path in unparsed_log.files(Path(manager.config.db_path).parent):
            path.unlink(missing_ok=True)
        return "已清空未解析记录"
    if target == "vacuum":
        db_path = Path(manager.config.db_path)
        wal = db_path.with_name(db_path.name + "-wal")
        before = _size(db_path) + _size(wal)
        started = time.monotonic()
        try:
            await manager.db.vacuum()       # 期间收发排队
        except RuntimeError as exc:
            return str(exc)
        after = _size(db_path) + _size(wal)
        elapsed = time.monotonic() - started
        logger.info("VACUUM: %d -> %d bytes, %.1fs", before, after, elapsed)
        return (f"数据库已压缩: {before / 1048576:.1f}MB → {after / 1048576:.1f}MB"
                f", 用时 {elapsed:.1f}s")
    raise ValueError(f"未知的清理目标: {target}")
