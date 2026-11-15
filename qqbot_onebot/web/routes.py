"""管理 REST API.

角色: user 只读; advanced 可管理 bot/名单/映射; admin 额外管理用户与删除 bot.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import secrets
import socket
import time
from urllib.parse import urljoin, urlsplit

import aiohttp
from fastapi import APIRouter, HTTPException, Request, Response

from ..core import avatars
from ..db import insert_bot, row_to_bot
from . import chatview
from .auth import SESSION_COOKIE, credential_error, hash_password, require_role

logger = logging.getLogger("qqbot.webapi")

_LOOSE_SEG_RE = re.compile(r'\{"type": "(\w+)", "data": \{(?:"text": "((?:[^"\\]|\\.)*))?')


def _loose_segments(raw: str) -> list[dict]:
    """消息列表只取 content 前 2000 字, 长消息会切在半截 JSON 里: 解析失败就按段头捞出
    类型与文字(最后一段文字可能不完整), 摘要和徽章不至于整行空掉."""
    try:
        segments = json.loads(raw)
        return [s for s in segments if isinstance(s, dict)] if isinstance(segments, list) else []
    except ValueError:
        pass
    out: list[dict] = []
    for match in _LOOSE_SEG_RE.finditer(raw):
        kind, text = match.group(1), match.group(2)
        data: dict = {}
        if kind == "text" and text is not None:
            try:
                data["text"] = json.loads(f'"{text}"')
            except ValueError:
                data["text"] = text
        out.append({"type": kind, "data": data})
    return out

_AUDIO_DOWNLOAD_LIMIT = 25 * 1024 * 1024
_AUDIO_REDIRECT_LIMIT = 3
_AUDIO_REMOTE_HOSTS = {"multimedia.nt.qq.com.cn"}


async def _is_public_http_url(candidate: str) -> bool:
    """防 SSRF: 仅放行白名单主机且解析结果全为公网地址的 HTTP(S) URL."""
    try:
        parsed = urlsplit(candidate)
        if parsed.scheme not in ("http", "https") or not parsed.hostname \
                or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.hostname.lower().rstrip(".") not in _AUDIO_REMOTE_HOSTS:
            return False
        try:
            return ipaddress.ip_address(parsed.hostname).is_global
        except ValueError:
            pass
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname, port, type=socket.SOCK_STREAM)
        return bool(infos) and all(
            ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except (OSError, ValueError):
        return False

BOT_EDITABLE_FIELDS = {
    "secret": str, "name": str, "enabled": int,
    "event_mode": str, "grp": str, "notes": str,
    "group_list_mode": str, "markdown_enabled": int,
    "report_self_message": int,
}
BOT_JSON_FIELDS = {"onebot_endpoints", "superusers", "tags", "passthrough_webhooks"}
BOT_ENUM_FIELDS = {
    "event_mode": {"webhook", "websocket"},
    "group_list_mode": {"white", "black"},
}


def _validate_bot_fields(body: dict, *, require_secret: bool) -> None:
    for field, valid in BOT_ENUM_FIELDS.items():
        if field in body and str(body[field]) not in valid:
            raise HTTPException(status_code=400, detail=f"{field} 取值非法")
    if "secret" in body:
        secret = str(body["secret"])
        if (not secret or "•" in secret) and require_secret:
            raise HTTPException(status_code=400, detail="secret 不能为空/掩码")


_PREVIEW_TYPES = {"image": "image", "mface": "image", "video": "video",
                  "record": "audio", "audio": "audio", "file": "file"}


def media_previews(manager, segments, depth: int = 0) -> list[dict]:
    """消息里的媒体 -> 管理台可看的地址.

    QQ CDN 直链失效的只是 rkey, 换上最近的同类 rkey 即可再开; 本地 /media
    过 TTL 标 expired. 合并转发展开至多 3 层.
    """
    from ..core import cdnkeys  # noqa: PLC0415
    out: list[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        kind = _PREVIEW_TYPES.get(str(seg.get("type")))
        if kind is None:
            content = data.get("content")
            if depth < 3 and isinstance(content, list):
                out.extend(media_previews(manager, content, depth + 1))
            continue
        url = str(data.get("url") or data.get("file") or "")
        name = str(data.get("name") or data.get("file_name") or "")
        local = manager.media.parse_token(url)
        if local:
            # 前端经 /api/media/{local} 看
            state = "ok" if manager.media.resolve(local) else "expired"
            out.append({"type": kind, "url": url, "local": local, "name": name,
                        "state": state})
            continue
        if not url.startswith(("http://", "https://")):
            out.append({"type": kind, "url": "", "name": name, "state": "unavailable"})
            continue
        if cdnkeys.is_cdn(url):
            rkey = cdnkeys.fresh().get(cdnkeys.appid_of(url))
            if rkey:
                url = cdnkeys.with_rkey(url, rkey)
        out.append({"type": kind, "url": url, "name": name, "state": "ok"})
    return out


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api")

    # ---------------- auth ----------------

    @router.post("/login")
    async def login(request: Request, response: Response):
        body = await request.json()
        auth = request.app.state.auth
        client_ip = request.client.host if request.client else ""
        username = str(body.get("username", ""))
        password = str(body.get("password", ""))
        if len(username) > 128 or len(password) > 1024:
            raise HTTPException(status_code=400, detail="用户名或密码过长")
        result = await auth.login(
            username, password, client_ip
        )
        if result is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误或已锁定")
        token, role = result
        # 仅经 https 到达时置 secure, 否则 http 直连收不到 cookie
        via_https = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "") == "https"
        )
        cookie_path = request.scope.get("root_path") or "/"
        response.set_cookie(
            SESSION_COOKIE, token, httponly=True, samesite="lax",
            max_age=7 * 86400,
            secure=via_https, path=cookie_path,
        )
        if cookie_path != "/":
            # 旧版 cookie 挂在 "/", 会和新的同名一起发; 清掉免得混淆
            response.delete_cookie(SESSION_COOKIE, path="/")
        return {"username": body.get("username"), "role": role}

    @router.post("/logout")
    async def logout(request: Request, response: Response):
        request.app.state.auth.logout(request.app.state.auth.session_token(request))
        response.delete_cookie(
            SESSION_COOKIE, path=request.scope.get("root_path") or "/")
        return {"ok": True}

    @router.get("/me")
    async def me(request: Request, session: dict = require_role("user")):
        auth = request.app.state.auth
        return {"username": session["username"], "role": session["role"],
                "initial_password": await auth.uses_initial_password(session["username"])}

    @router.post("/me/password")
    async def change_own_password(request: Request, session: dict = require_role("user")):
        """改自己的密码(验旧密码); 保留当前会话, 其它会话下线."""
        auth = request.app.state.auth
        body = await request.json()
        old, new = str(body.get("old", "")), str(body.get("new", ""))
        if problem := credential_error(None, new):
            raise HTTPException(status_code=400, detail=problem)
        if not await auth.check_password(session["username"], old):
            # 400 不是 403: 前端对 403 走"权限不足"的全局提示
            raise HTTPException(status_code=400, detail="当前密码不对")
        if secrets.compare_digest(old, new):
            raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
        await auth.set_password(session["username"], new,
                                keep=auth.session_token(request))
        logger.info("web user %s changed own password", session["username"])
        return {"ok": True}

    # ---------------- status ----------------

    @router.get("/status")
    async def status(request: Request, appids: str = "", with_groups: int = 0,
                     session: dict = require_role("user")):
        """运行态快照; appids 逗号分隔只查这几个.

        默认不带群列表(100 bot × 1000 群可达几十 MB), 由 /bots/{appid}/groups 按需取.
        """
        manager = request.app.state.manager
        from .. import __version__  # noqa: PLC0415
        wanted = {a for a in appids.split(",") if a} if appids else None
        bots = []
        for snap in manager.status(with_groups=bool(with_groups)):
            if wanted is not None and snap["appid"] not in wanted:
                continue
            if session["role"] == "user":
                # 只读账号不给端点地址
                snap["links"] = [{**link, "url": ""} for link in snap.get("links", [])]
            if not with_groups:
                snap.pop("groups", None)
            bots.append(snap)
        return {
            "version": __version__,
            "update": (manager.updater.snapshot()
                       if getattr(manager, "updater", None) else None),
            "public_base_url": manager.config.public_base_url,
            "webhook_path": "/qqbot/webhook/{appid}",
            "bots": bots,
        }

    @router.get("/stats")
    async def stats(request: Request, days: int = 7, appid: str = "",
                    session: dict = require_role("user")):
        """消息统计(走势/活跃会话/类型分布/配额), 全在 SQL 聚合, 按北京时间切天."""
        manager = request.app.state.manager
        days = max(1, min(days, 90))
        since = int(time.time()) - days * 86400
        where = "ts >= ?"
        args: list = [since]
        if appid:
            where += " AND bot_appid = ?"
            args.append(appid)
        day_expr = "date(ts + 28800, 'unixepoch')"

        daily = await manager.db.fetchall(
            f"SELECT {day_expr} AS day,"
            f" SUM(direction='in') AS incoming,"
            f" SUM(direction='out') AS outgoing"
            f" FROM messages WHERE {where} GROUP BY day ORDER BY day", args)
        by_kind = await manager.db.fetchall(
            f"SELECT chat_type, direction, COUNT(*) AS n"
            f" FROM messages WHERE {where} GROUP BY chat_type, direction", args)
        # 活跃会话另给日均与环比, 否则老大群永远霸榜
        top = await manager.db.fetchall(
            f"SELECT bot_appid, chat_type, peer_openid, peer_virtual, COUNT(*) AS n,"
            f" SUM(direction='out') AS outgoing, MAX(ts) AS last_ts,"
            f" COUNT(DISTINCT {day_expr}) AS active_days"
            f" FROM messages WHERE {where}"
            f" GROUP BY bot_appid, chat_type, peer_openid"
            f" ORDER BY n DESC LIMIT 30", args)
        # 上一等长周期, 算环比
        prev_args: list = [since - days * 86400, since]
        prev_where = "ts >= ? AND ts < ?"
        if appid:
            prev_where += " AND bot_appid = ?"
            prev_args.append(appid)
        prev_rows = await manager.db.fetchall(
            f"SELECT bot_appid, chat_type, peer_openid, COUNT(*) AS n"
            f" FROM messages WHERE {prev_where}"
            f" GROUP BY bot_appid, chat_type, peer_openid", prev_args)
        prev = {(r["bot_appid"], r["chat_type"], r["peer_openid"]): r["n"]
                for r in prev_rows}
        # 活跃时段(北京时间小时)
        hourly = await manager.db.fetchall(
            f"SELECT CAST(strftime('%H', ts + 28800, 'unixepoch') AS INTEGER) AS hour,"
            f" COUNT(*) AS n FROM messages WHERE {where} GROUP BY hour ORDER BY hour",
            args)
        by_bot = await manager.db.fetchall(
            f"SELECT bot_appid, COUNT(*) AS n, SUM(direction='out') AS outgoing"
            f" FROM messages WHERE {where} GROUP BY bot_appid ORDER BY n DESC", args)

        names = {b["appid"]: (b.get("name") or b["appid"])
                 for b in [row_to_bot(r) for r in
                           await manager.db.fetchall("SELECT * FROM bots")]}
        # 运行态配额(仅在跑的 bot)
        quota = {}
        for snap in manager.status(with_groups=False):
            q = snap.get("quota") or {}
            if q.get("proactive_24h_total") or q.get("blocked"):
                quota[snap["appid"]] = q
        return {
            "days": days,
            "daily": [dict(r) for r in daily],
            "by_kind": [dict(r) for r in by_kind],
            "hourly": [dict(r) for r in hourly],
            "by_bot": [{**dict(r), "name": names.get(r["bot_appid"], r["bot_appid"])}
                       for r in by_bot],
            "top_peers": [
                {**{k: v for k, v in dict(r).items() if k != "peer_openid"},
                 "name": _display_name(manager, r["peer_virtual"], ""),
                 "bot_name": names.get(r["bot_appid"], r["bot_appid"]),
                 "daily_avg": round(r["n"] / max(1, r["active_days"]), 1),
                 "prev": prev.get(
                     (r["bot_appid"], r["chat_type"], r["peer_openid"]), 0)}
                for r in top],
            "quota": quota,
        }

    @router.get("/bots/{appid}/groups")
    async def bot_groups(appid: str, request: Request, limit: int = 100,
                         offset: int = 0, session: dict = require_role("user")):
        """单个 bot 的群状态(分页), 卡片展开时才取."""
        manager = request.app.state.manager
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        groups = bot.status_snapshot(with_groups=True).get("groups") or []
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        page = groups[offset:offset + limit]
        # 直接带上虚拟号/名字/白名单状态(内存查询), 免得前端逐行请求
        for row in page:
            entry = manager.idmap.lookup_openid(appid, "group", row.get("openid", ""))
            row["virtual_id"] = entry.virtual_id if entry else 0
            row["name"] = entry.nickname if entry else ""
            row["enabled"] = bot.peer_enabled("group", row.get("openid", ""))
        return {"items": page, "total": len(groups)}

    # ---------------- bots ----------------

    def _mask(bot: dict, role: str) -> dict:
        out = dict(bot)
        if role != "admin":
            out["secret"] = "•••" if out.get("secret") else ""
        if role == "user":
            # 只读账号不给后端地址与反向 WS token
            out["onebot_endpoints"] = []
            out["passthrough_webhooks"] = []
        return out

    @router.get("/bots")
    async def list_bots(request: Request, q: str = "", grp: str = "", tag: str = "",
                        limit: int = 0, offset: int = 0,
                        session: dict = require_role("user")):
        """bot 列表(SQL 筛选); 传 limit 分页带 total, 否则回裸数组(兼容老调用)."""
        manager = request.app.state.manager
        wheres, args = [], []
        if q.strip():
            like = f"%{q.strip()}%"
            wheres.append("(appid LIKE ? OR name LIKE ? OR CAST(bot_qq AS TEXT) LIKE ?"
                          " OR notes LIKE ? OR tags LIKE ?)")
            args += [like] * 5
        if grp:
            wheres.append("grp=?")
            args.append(grp)
        if tag:
            wheres.append("tags LIKE ?")
            args.append(f'%"{tag}"%')
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        total_row = await manager.db.fetchone(
            "SELECT COUNT(*) AS n FROM bots" + where_sql, args)
        total = int(total_row["n"]) if total_row else 0
        sql = "SELECT * FROM bots" + where_sql + " ORDER BY grp, created_at"
        if limit:
            sql += " LIMIT ? OFFSET ?"
            args = args + [max(1, min(limit, 200)), max(0, offset)]
        rows = await manager.db.fetchall(sql, args)
        bots = []
        stale: list = []
        for row in rows:
            bot = _mask(row_to_bot(row), session["role"])
            live = manager.get_bot(bot["appid"])
            bot["running"] = live is not None and live.started
            bot["self_id"] = live.self_id if live else 0
            bots.append(bot)
            # 名字仍是占位名 -> 后台补同步, 不阻塞本次响应
            if live is not None and live.started \
                    and bot.get("name") == f"机器人{bot['appid']}":
                stale.append(live)
        for live in stale:
            asyncio.create_task(_sync_quietly(live))
        return {"items": bots, "total": total} if limit else bots

    @router.post("/bots")
    async def create_bot(request: Request, session: dict = require_role("advanced")):
        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", "")).strip()
        secret = str(body.get("secret", "")).strip()
        if not appid or not secret:
            raise HTTPException(status_code=400, detail="appid/secret 必填")
        _validate_bot_fields(body, require_secret=True)
        exists = await manager.db.fetchone(
            "SELECT appid FROM bots WHERE appid=?", (appid,)
        )
        if exists:
            raise HTTPException(status_code=409, detail="appid 已存在")
        fields = {k: body.get(k) for k in (
            "name", "bot_qq", "event_mode", "onebot_endpoints", "superusers",
            "tags", "grp", "notes", "group_list_mode", "passthrough_webhooks")}
        fields.update(
            appid=appid, secret=secret,
            enabled=1 if body.get("enabled", True) else 0,
            markdown_enabled=1 if body.get("markdown_enabled", True) else 0,
            report_self_message=1 if body.get("report_self_message", True) else 0)
        try:
            await insert_bot(manager.db, manager.config, fields)
        except ValueError as exc:           # 分组名不合规
            raise HTTPException(status_code=400, detail=str(exc))
        await manager.reload_bot(appid)
        await manager.mark_setup_done()
        logger.info("bot %s created by %s", appid, session["username"])
        return {"ok": True, "appid": appid}

    @router.put("/bots/{appid}")
    async def update_bot(appid: str, request: Request,
                         session: dict = require_role("advanced")):
        manager = request.app.state.manager
        body = await request.json()
        row = await manager.db.fetchone("SELECT * FROM bots WHERE appid=?", (appid,))
        if row is None:
            raise HTTPException(status_code=404, detail="bot 不存在")
        # 空/掩码 secret 视为"不修改"(前端编辑时留空)
        if "secret" in body and (not str(body["secret"]) or "•" in str(body["secret"])):
            body = {k: v for k, v in body.items() if k != "secret"}
        if str(body.get("grp") or "").strip():
            from ..core import groups  # noqa: PLC0415
            try:
                body["grp"] = groups.ensure(manager.config, str(body["grp"]))
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        _validate_bot_fields(body, require_secret=False)
        sets, args = [], []
        for field, caster in BOT_EDITABLE_FIELDS.items():
            if field in body:
                sets.append(f"{field}=?")
                value = body[field]
                if caster is int:
                    value = int(value) if not isinstance(value, bool) else int(value)
                args.append(caster(value))
        for field in BOT_JSON_FIELDS:
            if field in body:
                sets.append(f"{field}=?")
                args.append(json.dumps(body[field], ensure_ascii=False))
        if not sets:
            return {"ok": True, "changed": False}
        args.append(appid)
        await manager.db.execute(
            f"UPDATE bots SET {', '.join(sets)} WHERE appid=?", args
        )
        await manager.reload_bot(appid)
        await manager.mark_setup_done()
        logger.info("bot %s updated by %s", appid, session["username"])
        return {"ok": True, "changed": True}

    @router.delete("/bots/{appid}")
    async def delete_bot(appid: str, request: Request, drop_backend: int = 0,
                         session: dict = require_role("admin")):
        """删 bot; drop_backend=1 时先删 BS 连接(释放端口)再删记录, 反过来就查不到端点."""
        manager = request.app.state.manager
        backend: dict = {"attempted": bool(drop_backend)}
        if drop_backend:
            backend.update(await _drop_backend(manager, appid))
        await manager.remove_bot(appid)
        logger.info("bot %s deleted by %s (drop_backend=%s -> %s)", appid,
                    session["username"], bool(drop_backend), backend)
        return {"ok": True, "backend": backend}

    async def _drop_backend(manager, appid: str) -> dict:
        """按 bot 的 ws 端点反查 BS 连接并删除; 失败只记不抛(别挡住删 bot)."""
        from ..core.provision import (  # noqa: PLC0415
            ProvisionError, delete_from_bs, find_connection_by_endpoint,
            remove_connection_file,
        )
        row = await manager.db.fetchone(
            "SELECT onebot_endpoints FROM bots WHERE appid=?", (appid,))
        if row is None:
            return {"ok": False, "message": "bot 不存在"}
        endpoints = json.loads(row["onebot_endpoints"] or "[]")
        url = str((endpoints[0] or {}).get("url", "")) if endpoints else ""
        if not url:
            return {"ok": False, "message": "该 bot 没有配置端点"}
        try:
            cfg = manager.provision
            cfg.load()
            if cfg.mode != "botshepherd":
                # 直连模式无 BS 连接; 按端口反查可能误删别的连接
                return {"ok": True, "message": "直连模式，无需清理 BotShepherd"}
            connection_id = find_connection_by_endpoint(cfg, url)
            if connection_id is None:
                return {"ok": False, "message": f"BS 里找不到 {url} 对应的连接"}
            # 优先走 API: BS 先停连接再删, 端口才释放
            applied = await delete_from_bs(cfg, connection_id)
            if not applied:
                remove_connection_file(cfg, connection_id)
            port = int(connection_id) if connection_id.isdigit() else 0
            if port:
                manager.reserved_ports.discard(port)
            return {"ok": True, "connection_id": connection_id,
                    "applied": applied,
                    "message": ("已在 BotShepherd 删除连接" if applied
                                else "已删除连接配置文件，需重启 BS 生效")}
        except ProvisionError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:      # noqa: BLE001 删后端失败不该挡住删 bot
            logger.exception("删除 BS 连接失败")
            return {"ok": False, "message": f"删除后端出错: {exc}"}

    @router.post("/bots/{appid}/reload")
    async def reload_bot(appid: str, request: Request,
                         session: dict = require_role("advanced")):
        await request.app.state.manager.reload_bot(appid)
        return {"ok": True}

    async def _sync_quietly(live) -> None:
        try:
            await live.sync_identity()
        except Exception as exc:      # noqa: BLE001 后台补拉, 失败不影响任何人
            logger.warning("后台同步 bot 资料失败 %s: %s", live.appid, exc)

    @router.post("/bots/{appid}/sync_identity")
    async def sync_identity(appid: str, request: Request,
                            session: dict = require_role("advanced")):
        """重新拉取平台身份(名字/头像/QQ号)."""
        bot = request.app.state.manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        await bot.sync_identity()
        return {"ok": True, "name": bot.cfg.get("name", ""),
                "avatar": bot.cfg.get("avatar", ""),
                "bot_qq": bot.cfg.get("bot_qq", 0)}

    @router.post("/bots/{appid}/refresh_group_state")
    async def refresh_group_state(appid: str, request: Request,
                                  session: dict = require_role("advanced")):
        manager = request.app.state.manager
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        body = await request.json()
        # 也接受 openid: 退群残留行没有 id_map 映射, 只认虚拟号就永远清不掉
        openid = str(body.get("openid", "")).strip()
        if not openid:
            virtual = int(body.get("group_id", 0) or 0)
            entry = manager.idmap.lookup_virtual(virtual)
            if entry is None or entry.kind != "group" or entry.bot_appid != appid:
                raise HTTPException(status_code=404, detail="群不存在或不属于该 bot")
            openid = entry.openid
        state = await bot.group_state(openid, refresh=True, reason="web-ui")
        # bot 已不在群 -> 缓存已作废, 让前端删掉这行
        gone = ("group", openid) not in bot._peer_states
        return {"ok": True, "gone": gone,
                "state": {**state.__dict__, "compliant": state.compliant}}

    # ---------------- 扫码接入 ----------------

    @router.post("/qrconnect/start")
    async def qrconnect_start(request: Request,
                              session: dict = require_role("advanced")):
        """建绑定任务并返回二维码; 机器人主人用手机 QQ 扫码授权."""
        from ..qq.qrconnect import create_bind_task, qr_svg  # noqa: PLC0415

        manager = request.app.state.manager
        try:
            task = await create_bind_task(manager.http)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"创建绑定任务失败: {exc}")
        manager.bind_tasks[task.task_id] = task
        # 清掉 10 分钟前的旧任务
        cutoff = time.time() - 600
        for old in [t for t, v in manager.bind_tasks.items() if v.created_at < cutoff]:
            manager.bind_tasks.pop(old, None)
        logger.info("qrconnect task %s started by %s", task.task_id,
                    session["username"])
        return {"task_id": task.task_id, "connect_url": task.connect_url,
                "qr_svg": qr_svg(task.connect_url)}

    @router.get("/qrconnect/poll/{task_id}")
    async def qrconnect_poll(task_id: str, request: Request,
                             session: dict = require_role("advanced")):
        """轮询扫码结果: pending / expired / exists(不覆盖) / ready.

        ready 只回凭据供前端预填, 由人确认后再建 bot.
        """
        from ..qq.qrconnect import (  # noqa: PLC0415
            STATUS_COMPLETED, STATUS_EXPIRED, decrypt_secret, poll_bind_result,
        )

        manager = request.app.state.manager
        task = manager.bind_tasks.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="任务不存在或已清理")
        try:
            data = await poll_bind_result(manager.http, task_id)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"轮询失败: {exc}")

        status = int(data.get("status", 0))
        if status == STATUS_EXPIRED:
            manager.bind_tasks.pop(task_id, None)
            return {"status": "expired"}
        if status != STATUS_COMPLETED:
            return {"status": "pending"}

        appid = str(data.get("bot_appid", "")).strip()
        if not appid or appid == "0":
            return {"status": "pending"}
        try:
            secret = decrypt_secret(task.key_b64, data["bot_encrypt_secret"])
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc))
        manager.bind_tasks.pop(task_id, None)

        user_openid = str(data.get("user_openid", ""))
        existing = await manager.db.fetchone(
            "SELECT appid, name FROM bots WHERE appid=?", (appid,)
        )
        if existing is not None:
            # 不覆盖已存在的 bot(旧 secret 可能在用, 重绑会换新)
            logger.info("qrconnect: bot %s 已存在, 不覆盖", appid)
            return {"status": "exists", "appid": appid,
                    "name": existing["name"],
                    "message": "该 bot 已存在，未做任何改动"}

        # 扫码者是号主而非服务方, 不自动填 su
        logger.info("qrconnect: 新 bot %s 凭据已获取 (by %s)", appid,
                    session["username"])
        return {"status": "ready", "appid": appid, "secret": secret,
                "user_openid": user_openid}

    # ---------------- 一键配置后端 ----------------

    @router.get("/provision/config")
    async def provision_config(request: Request,
                               session: dict = require_role("advanced")):
        cfg = request.app.state.manager.provision
        cfg.load()
        return cfg.masked()

    @router.put("/provision/config")
    async def provision_config_update(request: Request,
                                      session: dict = require_role("admin")):
        cfg = request.app.state.manager.provision
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("bs"), dict):
            raise HTTPException(status_code=400, detail="bs 必须是对象")
        mode = str(body.get("mode") or cfg.mode)
        if mode not in ("onebot", "botshepherd"):
            raise HTTPException(status_code=400, detail="mode 取值非法")
        bs = {k: v for k, v in body["bs"].items()
              if k not in ("password", "username")}  # 凭据不落盘
        try:
            start, end = int(bs.get("port_start", 0)), int(bs.get("port_end", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="端口必须是整数")
        if not (1 <= start <= end <= 65535):
            raise HTTPException(status_code=400, detail="端口区间非法(需 1≤起≤止≤65535)")
        bs["port_start"], bs["port_end"] = start, end
        for key in ("dir", "web_base", "client_bind"):
            bs[key] = str(bs.get(key, "") or "")
        if mode == "botshepherd" and not bs["dir"]:
            raise HTTPException(status_code=400, detail="BotShepherd 根目录必填")

        profiles = body.get("profiles")
        if not isinstance(profiles, list):
            raise HTTPException(status_code=400, detail="profiles 必须是数组")
        clean: list[dict] = []
        for item in profiles:
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail="预设格式非法")
            name = str(item.get("name", "")).strip()
            targets = [str(t).strip() for t in (item.get("targets") or [])
                       if str(t).strip()]
            if not name or not targets:
                raise HTTPException(status_code=400, detail="预设需要名称与至少一个端点")
            entry = {"name": name, "targets": targets}
            if str(item.get("access_token") or "").strip():
                entry["access_token"] = str(item["access_token"]).strip()
            if item.get("default"):
                entry["default"] = True
            clean.append(entry)
        if sum(bool(p.get("default")) for p in clean) > 1:
            raise HTTPException(status_code=400, detail="只能设置一个默认预设")
        cfg.save({"mode": mode, "bs": bs, "profiles": clean})
        logger.info("provision config updated by %s", session["username"])
        return cfg.masked()

    @router.post("/provision/bs_credential")
    async def provision_set_credential(request: Request,
                                       session: dict = require_role("admin")):
        """设置 BotShepherd 面板密码(全局): 只存内存不落盘, 设置时真登录一次校验."""
        cfg = request.app.state.manager.provision
        body = await request.json()
        password = str(body.get("password", ""))
        if not password:
            cfg.set_runtime_password("")
            return {"ok": True, "password_source": cfg.password_source()}
        from ..core.provision import ProvisionError, bs_session  # noqa: PLC0415
        try:
            username, _ = cfg.bs_credentials(password)
            async with bs_session(cfg, password) as bs:
                if bs is None:
                    raise HTTPException(status_code=400, detail="读不到 BotShepherd 用户名")
        except ProvisionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except (aiohttp.ClientError, OSError) as exc:
            raise HTTPException(status_code=502, detail=f"连不上 BotShepherd: {exc}")
        cfg.set_runtime_password(password)
        logger.info("BS 密码已由 %s 设置(仅内存)", session["username"])
        return {"ok": True, "username": username,
                "password_source": cfg.password_source()}

    @router.post("/provision/retarget")
    async def provision_retarget(request: Request,
                                 session: dict = require_role("advanced")):
        """BS 模式: 按预设改 bot 已有连接的下游地址."""
        from ..core.provision import ProvisionError, retarget  # noqa: PLC0415

        body = await request.json()
        appid = str(body.get("appid", "")).strip()
        if not appid:
            raise HTTPException(status_code=400, detail="缺少 appid")
        try:
            result = await retarget(request.app.state.manager, appid,
                                    str(body.get("profile", "")))
        except ProvisionError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        logger.info("provision retarget: bot %s conn %s -> %s (by %s)", appid,
                    result["connection_id"], result["targets"], session["username"])
        message = ("已更新 BotShepherd 连接并重启生效" if result["applied"] else
                   "已写入 BotShepherd 配置文件，需重启 BS 或在其面板重启该连接后生效")
        return {"ok": True, "message": message, **result}

    @router.post("/provision/run")
    async def provision_run(request: Request,
                            session: dict = require_role("advanced")):
        """给 bot 配后端: 直连模式写端点, BS 模式建连接并指过去.

        仅限尚无端点的 bot, 免得误换在跑的连接; BS 密码取全局(内存/环境变量).
        """
        from ..core.provision import ProvisionError, apply_profile  # noqa: PLC0415

        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", "")).strip()
        if not appid:
            raise HTTPException(status_code=400, detail="缺少 appid")
        # 顺带设分组(留空不动): 先校验, 由 apply_profile 末尾的重载一并生效
        grp = str(body.get("grp", "")).strip()
        if grp:
            from ..core import groups  # noqa: PLC0415
            try:
                grp = groups.normalize_name(grp)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        try:
            result = await apply_profile(
                manager, appid, str(body.get("description", "")).strip(),
                str(body.get("profile", "")), grp=grp)
        except ProvisionError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc))
        logger.info("provision(%s): bot %s -> %s (by %s)", result["mode"], appid,
                    result["connection_id"] or result["targets"], session["username"])
        if result["mode"] == "onebot":
            message = f"已直连 {len(result['endpoints'])} 个 OneBot 端点"
        elif result["applied"]:
            message = "已在 BotShepherd 创建并生效"
        else:
            message = "已写入 BotShepherd 配置文件，需重启 BS 或在其面板重启该连接后生效"
        return {
            "ok": True,
            "mode": result["mode"],
            "port": result["port"],
            "connection_id": result["connection_id"],
            "endpoint": result["endpoints"][0]["url"] if result["endpoints"] else "",
            "targets": result["targets"],
            "applied": result["applied"],
            "message": message,
        }

    # ---------------- 分组 ----------------

    async def _groups_view(manager) -> dict:
        from ..core import groups  # noqa: PLC0415
        groups.ensure(manager.config)
        rows = await manager.db.fetchall("SELECT grp, COUNT(*) AS n FROM bots GROUP BY grp")
        counts = {r["grp"]: r["n"] for r in rows}
        return {"default": manager.config.default_bot_group,
                "groups": [{"name": g, "count": counts.get(g, 0)}
                           for g in manager.config.bot_groups],
                "ungrouped": counts.get("", 0)}

    @router.get("/groups")
    async def list_groups(request: Request, session: dict = require_role("user")):
        return await _groups_view(request.app.state.manager)

    @router.post("/groups")
    async def create_group(request: Request, session: dict = require_role("advanced")):
        from ..core import groups  # noqa: PLC0415
        manager = request.app.state.manager
        try:
            name = groups.normalize_name((await request.json()).get("name", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if name in manager.config.bot_groups:
            raise HTTPException(status_code=409, detail="分组已存在")
        groups.ensure(manager.config, name)
        return await _groups_view(manager)

    @router.put("/groups")
    async def update_group(request: Request, session: dict = require_role("admin")):
        """改名({old, new}) 或 设为默认({name, default: true})."""
        manager = request.app.state.manager
        body = await request.json()
        config = manager.config
        if body.get("default"):
            name = str(body.get("name", ""))
            if name not in config.bot_groups:
                raise HTTPException(status_code=404, detail="分组不存在")
            config.default_bot_group = name
            config.save()
            return await _groups_view(manager)
        from ..core import groups  # noqa: PLC0415
        old = str(body.get("old", ""))
        if old not in config.bot_groups:
            raise HTTPException(status_code=404, detail="分组不存在")
        try:
            new = groups.normalize_name(body.get("new", ""))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if new in config.bot_groups:
            raise HTTPException(status_code=409, detail="分组已存在")
        config.bot_groups = [new if g == old else g for g in config.bot_groups]
        if config.default_bot_group == old:
            config.default_bot_group = new
        config.save()
        await manager.db.execute("UPDATE bots SET grp=? WHERE grp=?", (new, old))
        for bot in manager.bots.values():
            if bot.cfg.get("grp") == old:
                bot.cfg["grp"] = new
        logger.info("group %s -> %s by %s", old, new, session["username"])
        return await _groups_view(manager)

    @router.delete("/groups/{name}")
    async def delete_group(name: str, request: Request,
                           session: dict = require_role("admin")):
        """删分组: 里面的 bot 挪进默认分组. 默认分组不能删."""
        manager = request.app.state.manager
        config = manager.config
        if name not in config.bot_groups:
            raise HTTPException(status_code=404, detail="分组不存在")
        if name == config.default_bot_group:
            raise HTTPException(status_code=409, detail="默认分组不能删，先把别的分组设为默认")
        config.bot_groups = [g for g in config.bot_groups if g != name]
        config.save()
        await manager.db.execute("UPDATE bots SET grp=? WHERE grp=?",
                                 (config.default_bot_group, name))
        for bot in manager.bots.values():
            if bot.cfg.get("grp") == name:
                bot.cfg["grp"] = config.default_bot_group
        logger.info("group %s deleted by %s", name, session["username"])
        return await _groups_view(manager)

    # ---------------- 存储 ----------------

    @router.get("/storage")
    async def storage_info(request: Request, session: dict = require_role("advanced")):
        from ..core import storage  # noqa: PLC0415
        return await storage.snapshot(request.app.state.manager)

    @router.post("/storage/cleanup")
    async def storage_cleanup(request: Request, session: dict = require_role("admin")):
        from ..core import storage  # noqa: PLC0415
        body = await request.json()
        target = str(body.get("target", ""))
        try:
            message = await storage.cleanup(request.app.state.manager, target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        logger.info("storage cleanup %s by %s: %s", target, session["username"], message)
        return {"ok": True, "message": message,
                "storage": await storage.snapshot(request.app.state.manager)}

    # ---------------- 更新 ----------------

    @router.post("/update/check")
    async def update_check(request: Request, session: dict = require_role("admin")):
        return await request.app.state.manager.updater.check()

    @router.post("/update/pull")
    async def update_pull(request: Request, session: dict = require_role("admin")):
        updater = request.app.state.manager.updater
        try:
            message = await updater.pull()
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        logger.info("update pulled by %s: %s", session["username"], message)
        return {"ok": True, "message": message}

    # ---------------- 全局选项 ----------------

    @router.get("/options")
    async def get_options(request: Request, session: dict = require_role("user")):
        from ..config import OPTION_FIELDS  # noqa: PLC0415
        manager = request.app.state.manager
        updater = getattr(manager, "updater", None)
        return {"options": {k: getattr(manager.config, k) for k in OPTION_FIELDS},
                "update": updater.snapshot() if updater else {},
                "bots": [{"appid": b.appid, "name": b.name, "self_id": b.self_id}
                         for b in manager.bots.values()],
                "groups": list(manager.config.bot_groups)}

    @router.put("/options")
    async def put_options(request: Request, session: dict = require_role("admin")):
        from ..config import OPTION_FIELDS  # noqa: PLC0415
        manager = request.app.state.manager
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="请求体必须是对象")
        updates: dict = {}
        for key, kind in OPTION_FIELDS.items():
            if key not in body:
                continue
            value = body[key]
            if kind is bool:
                value = bool(value)
            elif kind is list:
                try:
                    value = [int(x) for x in (value or [])]
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400, detail=f"{key} 必须是数字列表")
            else:
                value = str(value or "").strip()
            updates[key] = value
        if updates.get("default_group_list_mode", "white") not in ("white", "black"):
            raise HTTPException(status_code=400, detail="default_group_list_mode 取值非法")
        group = updates.get("default_bot_group")
        if group is not None and group not in manager.config.bot_groups:
            raise HTTPException(status_code=400, detail="分组不存在")
        for key, value in updates.items():
            setattr(manager.config, key, value)
        manager.config.save()
        manager.apply_options()
        await manager.mark_setup_done()
        logger.info("options updated by %s: %s", session["username"], sorted(updates))
        return await get_options(request, session)

    # ---------------- 插件 ----------------

    @router.get("/plugins")
    async def list_plugins(request: Request, session: dict = require_role("user")):
        from ..plugin import HOOK_POINTS, registry  # noqa: PLC0415
        # 只读账号看不到 secret 配置项
        return {**registry.describe(request.app.state.manager,
                                    reveal=session["role"] != "user"),
                "hook_points": {k: v[1] for k, v in HOOK_POINTS.items()}}

    @router.put("/plugins/{name}")
    async def update_plugin(name: str, request: Request,
                            session: dict = require_role("admin")):
        from ..plugin import registry  # noqa: PLC0415
        body = await request.json()
        config = body.get("config")
        try:
            plugin = registry.update(
                name, enabled=body.get("enabled"),
                config=config if isinstance(config, dict) else None)
        except KeyError:
            raise HTTPException(status_code=404, detail="插件不存在")
        logger.info("plugin %s updated by %s", name, session["username"])
        return plugin.describe(request.app.state.manager)

    @router.post("/plugins/reload")
    async def reload_plugins(request: Request, session: dict = require_role("admin")):
        from ..plugin import registry  # noqa: PLC0415
        registry.load()
        logger.info("plugins reloaded by %s", session["username"])
        return registry.describe(request.app.state.manager)

    # ---------------- access list ----------------

    @router.get("/access")
    async def list_access(request: Request, bot_appid: str = "", limit: int = 100,
                          offset: int = 0, q: str = "",
                          session: dict = require_role("user")):
        manager = request.app.state.manager
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        items = await manager.access.entries(bot_appid or None,
                                             limit=limit, offset=offset, q=q)
        total = await manager.access.count(bot_appid or None, q=q)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @router.post("/access")
    async def add_access(request: Request, session: dict = require_role("advanced")):
        manager = request.app.state.manager
        body = await request.json()
        bot_appid = str(body.get("bot_appid", ""))
        chat_type = str(body.get("chat_type", "group"))
        list_type = str(body.get("list_type", "white"))
        if chat_type not in ("group", "private") or list_type not in ("white", "black"):
            raise HTTPException(status_code=400, detail="chat_type/list_type 非法")
        openid = str(body.get("openid", "")).strip()
        virtual_id = int(body.get("virtual_id", 0) or 0)
        if not openid and virtual_id:
            entry = manager.idmap.lookup_virtual(virtual_id)
            if entry is None or entry.bot_appid != bot_appid:
                raise HTTPException(status_code=404, detail="虚拟号不存在或不属于该 bot")
            openid = entry.openid
        elif openid and not virtual_id:
            kind = "group" if chat_type == "group" else "user"
            found = manager.idmap.lookup_openid(bot_appid, kind, openid)
            virtual_id = found.virtual_id if found else 0
        if not openid:
            raise HTTPException(status_code=400, detail="需提供 openid 或 virtual_id")
        added = await manager.access.add(
            bot_appid, chat_type, list_type, openid, virtual_id,
            note=str(body.get("note", "")), added_by=f"web:{session['username']}",
        )
        return {"ok": True, "added": added}

    @router.post("/access/toggle")
    async def toggle_access(request: Request,
                            session: dict = require_role("advanced")):
        """按 (bot, 会话) 开关白名单; 群状态行只有 openid, 没有 access_list id."""
        manager = request.app.state.manager
        body = await request.json()
        bot_appid = str(body.get("bot_appid", ""))
        chat_type = str(body.get("chat_type", "group"))
        openid = str(body.get("openid", "")).strip()
        enable = bool(body.get("enable"))
        if chat_type not in ("group", "private") or not openid or not bot_appid:
            raise HTTPException(status_code=400, detail="参数不完整")
        if enable:
            entry = manager.idmap.lookup_openid(bot_appid, "group"
                                                if chat_type == "group" else "user",
                                                openid)
            await manager.access.add(
                bot_appid, chat_type, "white", openid,
                virtual_id=entry.virtual_id if entry else 0,
                note="群状态页快捷启用", added_by=session["username"])
        else:
            await manager.access.remove(bot_appid, chat_type, "white", openid)
        logger.info("access toggle %s %s/%s -> %s by %s", bot_appid, chat_type,
                    openid[:8], "启用" if enable else "禁用", session["username"])
        return {"ok": True, "enabled": enable}

    @router.delete("/access/{entry_id}")
    async def delete_access(entry_id: int, request: Request,
                            session: dict = require_role("advanced")):
        manager = request.app.state.manager
        row = await manager.db.fetchone(
            "SELECT * FROM access_list WHERE id=?", (entry_id,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="条目不存在")
        await manager.access.remove(
            row["bot_appid"], row["chat_type"], row["list_type"], row["openid"]
        )
        return {"ok": True}

    # ---------------- id map ----------------

    @router.get("/idmap")
    async def idmap_search(request: Request, q: str = "", limit: int = 50,
                           offset: int = 0, kind: str = "",
                           session: dict = require_role("user")):
        manager = request.app.state.manager
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        items = await manager.idmap.search(q, limit, offset=offset, kind=kind)
        total = await manager.idmap.search_count(q, kind=kind)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    # ---------------- messages (调试) ----------------

    @router.get("/messages")
    async def recent_messages(request: Request, appid: str = "", limit: int = 50,
                              offset: int = 0, q: str = "", chat_type: str = "",
                              session: dict = require_role("advanced")):
        """消息列表(offset 分页+总数), 只回摘要与段类型; q 模糊搜正文/虚拟号/消息 id.

        content 以 ensure_ascii=False 存, 中文可直接 LIKE.
        """
        manager = request.app.state.manager
        limit = max(1, min(limit, 100))
        offset = max(0, offset)
        wheres, args = [], []
        if appid:
            wheres.append("bot_appid=?"); args.append(appid)
        if chat_type in ("group", "private"):
            wheres.append("chat_type=?"); args.append(chat_type)
        if q:
            like = f"%{q}%"
            wheres.append("(content LIKE ? OR CAST(peer_virtual AS TEXT) LIKE ?"
                          " OR CAST(user_virtual AS TEXT) LIKE ? OR qq_msg_id LIKE ?"
                          " OR mid = ?)")
            args += [like, like, like, like, int(q) if q.strip().isdigit() else -1]
        where_sql = (" WHERE " + " AND ".join(wheres)) if wheres else ""
        # 带关键词的 COUNT 要全表扫(50 万行 1.4~2.8s, 独占 DB 线程), 封顶够翻页即可
        cap = 2000 if q else 0
        if cap:
            total_row = await manager.db.fetchone(
                "SELECT COUNT(*) AS n FROM (SELECT 1 FROM messages" + where_sql
                + " LIMIT ?)", args + [cap])
        else:
            total_row = await manager.db.fetchone(
                "SELECT COUNT(*) AS n FROM messages" + where_sql, args)
        total = int(total_row["n"]) if total_row else 0
        total_capped = bool(cap and total >= cap)
        # 只取前 2000 字, 单行可能有几 MB
        rows = await manager.db.fetchall(
            "SELECT mid, bot_appid, direction, chat_type, peer_virtual,"
            " user_virtual, qq_msg_id, substr(content, 1, 2000) AS content, ts"
            " FROM messages" + where_sql +
            " ORDER BY mid DESC LIMIT ? OFFSET ?", args + [limit, offset])

        items = []
        for row in rows:
            record = dict(row)
            segments = _loose_segments(record.pop("content") or "[]")
            # 表情/卡片进摘要; 图片、转发等由前端按 kinds 出徽章
            record["summary"] = chatview.clip(chatview.digest(segments, media=False), 200)
            kinds: list[str] = []
            for seg in segments:
                kind = str(seg.get("type", "text"))
                if not (kind == "node" and kinds and kinds[-1] == "node"):
                    kinds.append(kind)      # 一组 node 是一份聊天记录, 只算一个
            record["kinds"] = kinds
            record["peer_name"] = _display_name(manager, record["peer_virtual"], "")
            record["user_name"] = _display_name(manager, record["user_virtual"], "")
            items.append(record)
        return {"items": items, "total": total, "limit": limit,
                "offset": offset, "total_capped": total_capped}

    @router.get("/messages/{mid}")
    async def message_detail(mid: int, request: Request,
                             session: dict = require_role("advanced")):
        manager = request.app.state.manager
        record = await manager.store.get_by_mid(mid)
        if record is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        record["previews"] = media_previews(manager, record.get("content"))
        return record

    # ---------------- 聊天室 ----------------
    # 数据取自 messages 表 + id_map, 发送走 bot.sender 完整管线.

    # 排除影子记录: 引用库里没有的老消息时 bot._reply_segment 补登记的锚点
    # (direction=in / user_virtual 0 / 无平台 id), 按时间轴渲染会成幽灵消息.
    NOT_SHADOW = "(direction!='in' OR user_virtual!=0 OR qq_msg_id!='')"
    NOT_SHADOW_M = "(m.direction!='in' OR m.user_virtual!=0 OR m.qq_msg_id!='')"

    def _avatar_pair(manager, virtual_id: int) -> tuple[str, str]:
        """(头像, 备用): 用户优先平台下发的链接, 备用自拼 q.qlogo.cn/qqapp(首选加载失败时 app.js 的 onMediaError 换上)."""
        entry = manager.idmap.lookup_virtual(int(virtual_id or 0))
        if entry is None:
            return "", ""
        if entry.kind == "bot":
            row = manager.get_bot(entry.openid)
            return (str(row.cfg.get("avatar", "")) if row else ""), ""
        if entry.kind != "user":
            return "", ""
        return avatars.candidates(entry, "100")

    def _avatar_of(manager, virtual_id: int) -> str:
        return _avatar_pair(manager, virtual_id)[0]

    def _sender_role(record: dict) -> str:
        """落库时记下的群身份(owner/admin/member)."""
        sender = record.get("sender")
        return str(sender.get("role", "")) if isinstance(sender, dict) else ""

    def _display_name(manager, virtual_id: int, fallback: str = "") -> str:
        entry = manager.idmap.lookup_virtual(int(virtual_id or 0))
        if entry is not None and entry.nickname:
            return entry.nickname
        return fallback or str(virtual_id or "")

    @router.get("/chat/peers")
    async def chat_peers(request: Request, appid: str, q: str = "",
                         limit: int = 60, offset: int = 0,
                         session: dict = require_role("advanced")):
        """会话列表: 有过消息的群/私聊, 按最近活跃排序, 支持按名字/号搜索."""
        manager = request.app.state.manager
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        # join id_map 后在 SQL 里筛/分页; 取最近 N 条再筛会漏掉更早的匹配
        joined = (
            "FROM messages m LEFT JOIN id_map i"
            "  ON i.bot_appid = m.bot_appid AND i.virtual_id = m.peer_virtual"
            " WHERE m.bot_appid=? AND " + NOT_SHADOW_M
        )
        args: list = [appid]
        needle = q.strip()
        if needle:
            like = f"%{needle}%"
            joined += (" AND (i.nickname LIKE ?"
                       " OR CAST(m.peer_virtual AS TEXT) LIKE ?)")
            args += [like, like]
        group_by = " GROUP BY m.chat_type, m.peer_openid"
        total_row = await manager.db.fetchone(
            "SELECT COUNT(*) AS n FROM (SELECT 1 " + joined + group_by + ")", args)
        total = int(total_row["n"]) if total_row else 0
        rows = await manager.db.fetchall(
            "SELECT m.chat_type, m.peer_virtual, MAX(m.ts) AS last_ts,"
            " COUNT(*) AS n, i.nickname AS name " + joined + group_by +
            " ORDER BY last_ts DESC LIMIT ? OFFSET ?", args + [limit, offset])
        items = [{
            "chat_type": row["chat_type"],
            "peer_id": row["peer_virtual"],
            "name": row["name"] or (("群" if row["chat_type"] == "group" else "用户")
                                    + str(row["peer_virtual"])),
            "last_ts": row["last_ts"],
            "count": row["n"],
            "avatar": (_avatar_of(manager, row["peer_virtual"])
                       if row["chat_type"] == "private" else ""),
        } for row in rows]
        return {"items": items, "total": total}

    def _peer_openid(manager, appid: str, chat_type: str, peer_id: int) -> str:
        entry = manager.idmap.lookup_virtual(int(peer_id))
        want = "group" if chat_type == "group" else "user"
        if entry is None or entry.kind != want or entry.bot_appid != appid:
            raise HTTPException(status_code=404, detail="会话不存在")
        return entry.openid

    @router.get("/chat/messages")
    async def chat_messages(request: Request, appid: str, chat_type: str,
                            peer_id: int, before: int = 0, after: int = 0,
                            limit: int = 40,
                            session: dict = require_role("advanced")):
        """会话消息(时间正序); before=向上翻页游标, after=轮询增量."""
        manager = request.app.state.manager
        limit = max(1, min(limit, 100))
        openid = _peer_openid(manager, appid, chat_type, peer_id)
        # 进会话刷一次群资料(60s 节流), 群名/改名才能进 id_map
        peer_name = ""
        if chat_type == "group" and not before and not after:
            peer_name = await _try_fill_group_name(manager, appid, openid)
        sql = ("SELECT mid, direction, user_virtual, content, sender, ts,"
               " recalled_at, qq_msg_id FROM messages"
               " WHERE bot_appid=? AND chat_type=? AND peer_openid=? AND "
               + NOT_SHADOW)
        args: list = [appid, chat_type, openid]
        if before:
            sql += " AND mid < ?"
            args.append(int(before))
        if after:
            sql += " AND mid > ?"
            args.append(int(after))
        sql += " ORDER BY mid DESC LIMIT ?"
        args.append(limit + 1)
        rows = await manager.db.fetchall(sql, args)
        has_more = len(rows) > limit
        items = []
        quoted_ids: set[int] = set()
        name_of = _name_lookup(manager)
        view = chatview.View(name_of)
        for row in reversed(rows[:limit]):
            try:
                content = json.loads(row["content"] or "[]")
            except ValueError:
                content = []
            try:
                sender = json.loads(row["sender"] or "{}")
            except ValueError:
                sender = {}
            uid = int(sender.get("user_id") or row["user_virtual"] or 0)
            for seg in content:
                if isinstance(seg, dict) and seg.get("type") == "reply":
                    try:
                        quoted_ids.add(int((seg.get("data") or {}).get("id", 0)))
                    except (TypeError, ValueError):
                        pass
            shown = view.segments(content, f"m{row['mid']}")
            avatar, avatar_fb = _avatar_pair(manager, uid)
            items.append({
                "mid": row["mid"],
                "direction": row["direction"],
                "user_id": uid,
                "nickname": (sender.get("card") or sender.get("nickname")
                             or _display_name(manager, uid, "")),
                "avatar": avatar,
                "avatar_fb": avatar_fb,
                "role": sender.get("role", ""),
                "content": shown,
                # 引用这条时挂在输入框上方的那句
                "summary": chatview.clip(chatview.digest(shown, name_of), 60),
                "ts": row["ts"],
                "recalled": bool(row["recalled_at"]),
                # 无平台 id(转发子节点等)撤不了
                "recallable": bool(row["qq_msg_id"]),
            })
        # 被引用消息的摘要, 一次查回
        quotes = await _quote_previews(manager, quoted_ids) if quoted_ids else {}
        await _fill_forward_previews(manager, appid, view)
        # bot 在群里的角色(决定能否撤回他人/禁言). 进会话时强刷: 提升管理员平台
        # 不推事件, 缓存会一直停在旧值; 翻页/轮询走缓存.
        bot_role = ""
        if chat_type == "group":
            live = manager.get_bot(appid)
            if live is not None:
                bot_role = await live.group_role(
                    openid, force=not before and not after)
        return {"items": items, "has_more": has_more, "quotes": quotes,
                "forwards": view.forwards,
                "bot_role": bot_role, "peer_name": peer_name,
                "next_before": rows[limit - 1]["mid"] if has_more and rows else 0}

    def _name_lookup(manager):
        """虚拟号 -> 已知昵称, 不认识给空串(由前端退回显示号码)."""
        def name_of(virtual_id: int) -> str:
            entry = manager.idmap.lookup_virtual(int(virtual_id or 0))
            return entry.nickname if entry is not None and entry.nickname else ""
        return name_of

    async def _quote_previews(manager, mids: set[int]) -> dict:
        marks = ",".join("?" * len(mids))
        rows = await manager.db.fetchall(
            f"SELECT mid, user_virtual, content, sender FROM messages"
            f" WHERE mid IN ({marks})", list(mids))
        name_of = _name_lookup(manager)
        out: dict[str, dict] = {}
        for row in rows:
            try:
                content = json.loads(row["content"] or "[]")
            except ValueError:
                content = []
            try:
                sender = json.loads(row["sender"] or "{}")
            except ValueError:
                sender = {}
            uid = int(sender.get("user_id") or row["user_virtual"] or 0)
            out[str(row["mid"])] = {
                "nickname": (sender.get("card") or sender.get("nickname")
                             or _display_name(manager, uid, "")),
                "summary": chatview.clip(chatview.digest(content, name_of), 60),
            }
        return out

    # 入站合并转发的预览(条数 + 前几条): 内容收到后不再变, 按 id 缓存, 免得每次轮询都解析大段节点
    _forward_previews: dict[tuple[str, str], dict] = {}
    FORWARD_PREVIEW_CACHE = 512

    async def _fill_forward_previews(manager, appid: str, view: chatview.View) -> None:
        """把 view 里登记的入站 forward id 换成预览; 已清理(forwards 表只留 7 天)的标 gone."""
        missing = [fid for fid in view.pending if (appid, fid) not in _forward_previews]
        if missing:
            marks = ",".join("?" * len(missing))
            rows = await manager.db.fetchall(
                f"SELECT id, nodes FROM forwards WHERE bot_appid=? AND id IN ({marks})",
                [appid, *missing])
            found = {row["id"]: row["nodes"] for row in rows}
            if len(_forward_previews) + len(missing) > FORWARD_PREVIEW_CACHE:
                _forward_previews.clear()
            for fid in missing:
                if fid not in found:
                    view.forwards[fid] = {"count": 0, "lines": [], "gone": True}
                    continue          # 不缓存: 万一稍后才入库
                try:
                    raw = json.loads(found[fid] or "[]")
                except ValueError:
                    raw = []
                nodes = chatview.View(_name_lookup(manager)).nodes(raw, fid)
                _forward_previews[(appid, fid)] = chatview.preview(nodes)
        for fid in view.pending:
            if (appid, fid) in _forward_previews:
                view.forwards[fid] = dict(_forward_previews[(appid, fid)])

    @router.get("/chat/forward")
    async def chat_forward(request: Request, appid: str, id: str,
                           session: dict = require_role("advanced")):
        """点开入站的聊天记录: 全部节点(已整理成气泡能画的形状)."""
        manager = request.app.state.manager
        row = await manager.db.fetchone(
            "SELECT nodes FROM forwards WHERE id=? AND bot_appid=?", (id, appid))
        if row is None:
            raise HTTPException(status_code=404, detail="这份聊天记录已经清理")
        try:
            raw = json.loads(row["nodes"] or "[]")
        except ValueError:
            raw = []
        view = chatview.View(_name_lookup(manager))
        nodes = view.nodes(raw, id)
        await _fill_forward_previews(manager, appid, view)
        return {"nodes": nodes, "forwards": view.forwards}

    @router.post("/chat/send")
    async def chat_send(request: Request, session: dict = require_role("advanced")):
        """从管理台发消息, 走 bot.sender 完整管线."""
        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", ""))
        chat_type = str(body.get("chat_type", "group"))
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        openid = _peer_openid(manager, appid, chat_type, int(body.get("peer_id") or 0))
        message = body.get("message")
        if not message:
            raise HTTPException(status_code=400, detail="消息为空")
        from ..core.sender import SendError  # noqa: PLC0415

        try:
            mid = await bot.sender.send(chat_type, openid, message)
        except SendError as exc:
            raise HTTPException(status_code=400, detail=exc.message)
        logger.info("chat: %s -> %s/%s by %s", appid, chat_type,
                    body.get("peer_id"), session["username"])
        return {"ok": True, "message_id": mid}

    _group_name_checked: dict[tuple[str, str], float] = {}
    GROUP_NAME_TTL = 60      # 同一群 60 秒内只拉一次

    async def _try_fill_group_name(manager, appid: str, openid: str) -> str:
        """拉群资料入库并返回群名; 失败返回已知旧名."""
        bot = manager.get_bot(appid)
        entry = manager.idmap.lookup_openid(appid, "group", openid)
        known = entry.nickname if entry else ""
        now = time.time()
        key = (appid, openid)
        if bot is None or now - _group_name_checked.get(key, 0) < GROUP_NAME_TTL:
            return known
        _group_name_checked[key] = now
        try:
            info = await bot.api.group_info(openid)
        except Exception as exc:
            logger.debug("补拉群名失败 %s: %s", openid[:12], exc)
            return known
        name = str(info.get("group_name", ""))
        if name and name != known:
            await manager.idmap.to_virtual(appid, "group", openid, nickname=name)
        member_num = int(info.get("group_member_num") or 0)
        if member_num:
            await manager.db.execute(
                "UPDATE peer_states SET member_count=? WHERE bot_appid=?"
                " AND chat_type='group' AND peer_openid=?",
                (member_num, appid, openid))
        return name or known

    @router.post("/chat/refresh_peer")
    async def chat_refresh_peer(request: Request,
                               session: dict = require_role("advanced")):
        """按需补拉群资料入库(否则群名只在插件调 get_group_info 时才写入)."""
        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", ""))
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        chat_type = str(body.get("chat_type", "group"))
        peer_id = int(body.get("peer_id") or 0)
        openid = _peer_openid(manager, appid, chat_type, peer_id)
        if chat_type != "group":
            entry = manager.idmap.lookup_virtual(peer_id)
            return {"ok": True, "name": entry.nickname if entry else ""}
        try:
            info = await bot.api.group_info(openid)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"拉取失败: {exc}")
        name = str(info.get("group_name", ""))
        member_num = int(info.get("group_member_num") or 0)
        if name:
            await manager.idmap.to_virtual(appid, "group", openid, nickname=name)
        if member_num:
            await manager.db.execute(
                "UPDATE peer_states SET member_count=? WHERE bot_appid=?"
                " AND chat_type='group' AND peer_openid=?",
                (member_num, appid, openid))
        return {"ok": True, "name": name, "member_count": member_num,
                "memo": str(info.get("group_finger_memo") or "")}

    @router.post("/chat/recall")
    async def chat_recall(request: Request,
                          session: dict = require_role("advanced")):
        """撤回一条消息. 实测规则(与文档不符):

        - 自己发的: bot 是群管不限时, 否则仅 2 分钟(40064004)
        - 别人发的: 需 bot 是群管且对方是普通成员, 否则 40062003
        """
        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", ""))
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        record = await manager.store.get_by_mid(int(body.get("mid") or 0))
        if record is None or record["bot_appid"] != appid:
            raise HTTPException(status_code=404, detail="消息不存在")
        if not record.get("qq_msg_id"):
            raise HTTPException(status_code=400, detail="这条消息没有可撤回的平台 id")
        # 撤他人消息先确认群管权限, 免得白打注定 40062003 的接口
        if (record["chat_type"] == "group"
                and int(record.get("user_virtual") or 0) != bot.self_id):
            role = await bot.group_role(record["peer_openid"])
            if role not in ("admin", "owner"):
                raise HTTPException(
                    status_code=403,
                    detail="撤回他人消息需要 Bot 是群管理员，请先设置后重试")
            if _sender_role(record) in ("owner", "admin"):
                raise HTTPException(
                    status_code=403,
                    detail="群主和管理员的消息撤不了")
        from ..qq.api import QQApiError  # noqa: PLC0415

        # 一条逻辑消息可能拆成多条平台消息, 同 delete_msg 按 batch_mid 整批撤
        targets = [record]
        batch = record.get("batch_mid") or 0
        if batch:
            siblings = await manager.db.fetchall(
                "SELECT mid, chat_type, peer_openid, qq_msg_id FROM messages"
                " WHERE bot_appid=? AND batch_mid=? AND mid!=? AND qq_msg_id!=''",
                (appid, batch, record["mid"]))
            targets += [dict(r) for r in siblings
                        if not r["qq_msg_id"].startswith("unknown-")]
        recalled = 0
        first_error = None
        for target in targets:
            try:
                if target["chat_type"] == "group":
                    await bot.api.recall_group_message(
                        target["peer_openid"], target["qq_msg_id"])
                else:
                    await bot.api.recall_c2c_message(
                        target["peer_openid"], target["qq_msg_id"])
                recalled += 1
            except QQApiError as exc:
                if first_error is None:
                    first_error = exc
        if not recalled and first_error is not None:
            if first_error.code == 40062003:
                raise HTTPException(status_code=403, detail="没有操作权限")
            if first_error.code == 40064004:
                raise HTTPException(status_code=400,
                                    detail="超过 2 分钟，请设置Bot为管理员后重试")
            raise HTTPException(status_code=400,
                                detail=f"撤回失败: {first_error.message}")
        if batch:
            await manager.db.execute(
                "UPDATE messages SET recalled_at=? WHERE bot_appid=? AND batch_mid=?",
                (int(time.time()), appid, batch))
        else:
            await manager.db.execute(
                "UPDATE messages SET recalled_at=? WHERE mid=?",
                (int(time.time()), record["mid"]))
        logger.info("chat recall %s mid=%s by %s", appid, body.get("mid"),
                    session["username"])
        return {"ok": True}

    @router.post("/chat/mute")
    async def chat_mute(request: Request,
                        session: dict = require_role("advanced")):
        """禁言/解禁群成员(bot 须为群管). duration 秒, 0 = 解除."""
        manager = request.app.state.manager
        body = await request.json()
        appid = str(body.get("appid", ""))
        bot = manager.get_bot(appid)
        if bot is None:
            raise HTTPException(status_code=404, detail="bot 未运行")
        group_openid = _peer_openid(manager, appid, "group",
                                    int(body.get("peer_id") or 0))
        entry = manager.idmap.lookup_virtual(int(body.get("user_id") or 0))
        if entry is None or entry.kind != "user" or entry.bot_appid != appid:
            raise HTTPException(status_code=404, detail="用户不存在")
        duration = max(0, int(body.get("duration") or 0))
        role = await bot.group_role(group_openid)
        if role not in ("admin", "owner"):
            raise HTTPException(
                status_code=403,
                detail="Bot 当前不是群管理员，请先设置后重试")
        from ..qq.api import QQApiError  # noqa: PLC0415

        try:
            await bot.api.set_member_mute(
                group_openid, entry.openid,
                int(time.time()) + duration if duration else 0)
        except QQApiError as exc:
            if exc.code in (40062003, 11298):
                raise HTTPException(status_code=403,
                                    detail="没有操作权限，请先把 Bot 设为群管理员")
            raise HTTPException(status_code=400, detail=f"操作失败: {exc.message}")
        logger.info("chat mute %s %s %ss by %s", appid, body.get("user_id"),
                    duration, session["username"])
        return {"ok": True}

    @router.get("/media/{token}")
    async def local_media(token: str, request: Request,
                          session: dict = require_role("advanced")):
        """管理台看本地媒体(media:// 的唯一入口); nosniff+CSP sandbox 防存储型 XSS."""
        from fastapi.responses import FileResponse  # noqa: PLC0415
        path = request.app.state.manager.media.resolve(token)
        if path is None:
            raise HTTPException(status_code=404, detail="已过期")
        return FileResponse(path, headers={"X-Content-Type-Options": "nosniff",
                                           "Content-Security-Policy": "default-src 'none'; sandbox"})

    @router.get("/chat/audio")
    async def chat_audio(request: Request, url: str,
                         session: dict = require_role("advanced")):
        """把 silk/amr 语音转成浏览器能放的 mp3, 按源 URL 缓存进媒体库."""
        manager = request.app.state.manager
        if not (url.startswith(("http://", "https://")) or manager.media.is_local(url)):
            raise HTTPException(status_code=400, detail="非法地址")
        import hashlib  # noqa: PLC0415

        token = f"a{hashlib.sha256(url.encode()).hexdigest()[:22]}.mp3"
        if manager.media.resolve(token) is None:
            data = await _fetch_and_transcode(manager, url)
            if data is None:
                raise HTTPException(status_code=415, detail="这段语音无法转码")
            manager.media.put_named(token, data)
        path = manager.media.resolve(token)
        if path is None:
            raise HTTPException(status_code=404, detail="转码结果丢失")
        from fastapi.responses import FileResponse  # noqa: PLC0415

        return FileResponse(path, media_type="audio/mpeg")

    async def _fetch_and_transcode(manager, url: str) -> bytes | None:
        """下载并转 mp3: silk 走 pilk, 其余交给 ffmpeg; 远程只拉 QQ CDN, 限大小与重定向."""
        import asyncio as _asyncio  # noqa: PLC0415
        import subprocess  # noqa: PLC0415
        import tempfile  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        try:
            raw = bytearray()
            local_token = manager.media.token_of_url(url)
            local_path = manager.media.resolve(local_token) if local_token else None
            if local_path is not None:
                if local_path.stat().st_size > _AUDIO_DOWNLOAD_LIMIT:
                    return None
                raw = bytearray(await _asyncio.to_thread(local_path.read_bytes))
            else:
                current = url
                for redirect_no in range(_AUDIO_REDIRECT_LIMIT + 1):
                    if not await _is_public_http_url(current):
                        logger.warning("拒绝语音抓取的非 QQ CDN 地址: %s", current)
                        return None
                    async with manager.http.get(
                            current, allow_redirects=False,
                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        if resp.status in (301, 302, 303, 307, 308):
                            location = resp.headers.get("location", "")
                            if not location or redirect_no >= _AUDIO_REDIRECT_LIMIT:
                                return None
                            current = urljoin(current, location)
                            continue
                        if resp.status != 200:
                            return None
                        declared = resp.headers.get("content-length", "")
                        if declared and int(declared) > _AUDIO_DOWNLOAD_LIMIT:
                            return None
                        async for chunk in resp.content.iter_chunked(64 * 1024):
                            raw.extend(chunk)
                            if len(raw) > _AUDIO_DOWNLOAD_LIMIT:
                                return None
                        break
                else:
                    return None
        except Exception as exc:
            logger.info("语音下载失败: %s", exc)
            return None

        def convert() -> bytes | None:
            with tempfile.TemporaryDirectory() as tmp:
                src = _Path(tmp) / "in"
                out = _Path(tmp) / "out.mp3"
                src.write_bytes(raw)
                if raw[:16].lstrip(b"\x02").startswith(b"#!SILK_V3"):
                    try:
                        import pilk  # noqa: PLC0415
                        pcm = _Path(tmp) / "a.pcm"
                        pilk.decode(str(src), str(pcm))
                        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le",
                               "-ar", "24000", "-ac", "1", "-i", str(pcm), str(out)]
                    except Exception as exc:
                        logger.info("silk 解码失败: %s", exc)
                        return None
                else:
                    cmd = ["ffmpeg", "-y", "-loglevel", "error",
                           "-i", str(src), str(out)]
                proc = subprocess.run(cmd, capture_output=True, timeout=60)
                if proc.returncode != 0 or not out.exists():
                    logger.info("语音转码失败: %s", proc.stderr[-200:])
                    return None
                if out.stat().st_size > _AUDIO_DOWNLOAD_LIMIT:
                    logger.info("语音转码结果超过上限")
                    return None
                return out.read_bytes()

        return await _asyncio.to_thread(convert)

    @router.post("/chat/upload")
    async def chat_upload(request: Request,
                          session: dict = require_role("advanced")):
        """上传文件进媒体库(按 media_ttl_hours 过期), 返回可作消息段的本地 URL."""
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=400, detail="缺少文件")
        data = await upload.read()
        if not data:
            raise HTTPException(status_code=400, detail="空文件")
        if len(data) > 200 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="文件超过 200MB")
        manager = request.app.state.manager
        name = getattr(upload, "filename", "") or ""
        from pathlib import PurePath  # noqa: PLC0415

        token = manager.media.put_bytes(data, PurePath(name).suffix)
        return {"ok": True, "url": manager.media.ref(token, name),
                "name": name, "size": len(data)}

    # ---------------- users (admin) ----------------

    @router.get("/users")
    async def list_users(request: Request, session: dict = require_role("admin")):
        manager = request.app.state.manager
        rows = await manager.db.fetchall(
            "SELECT username, role, created_at FROM web_users"
        )
        return [dict(r) for r in rows]

    @router.post("/users")
    async def create_user(request: Request, session: dict = require_role("admin")):
        manager = request.app.state.manager
        body = await request.json()
        username = str(body.get("username", "")).strip()
        password = str(body.get("password", ""))
        role = str(body.get("role", "user"))
        if problem := credential_error(username, password):
            raise HTTPException(status_code=400, detail=problem)
        if role not in ("admin", "advanced", "user"):
            raise HTTPException(status_code=400, detail="角色非法")
        exists = await manager.db.fetchone(
            "SELECT username FROM web_users WHERE username=?", (username,)
        )
        if exists:
            raise HTTPException(status_code=409, detail="用户已存在")
        await manager.db.execute(
            "INSERT INTO web_users (username, password_hash, role, created_at)"
            " VALUES (?,?,?,?)",
            (username, await asyncio.to_thread(hash_password, password), role,
             int(time.time())),
        )
        logger.info("web user %s(%s) created by %s", username, role,
                    session["username"])
        return {"ok": True}

    @router.put("/users/{username}")
    async def update_user(username: str, request: Request,
                          session: dict = require_role("admin")):
        manager = request.app.state.manager
        body = await request.json()
        row = await manager.db.fetchone(
            "SELECT username FROM web_users WHERE username=?", (username,)
        )
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        password = str(body.get("password", ""))
        role = str(body.get("role", ""))
        # 先全部校验再落库, 免得改了一半才报错
        if password and (problem := credential_error(None, password)):
            raise HTTPException(status_code=400, detail=problem)
        if role and role not in ("admin", "advanced", "user"):
            raise HTTPException(status_code=400, detail="角色非法")
        if password:
            await request.app.state.auth.set_password(username, password)
        if role:
            await manager.db.execute(
                "UPDATE web_users SET role=? WHERE username=?", (role, username)
            )
            request.app.state.auth.drop_user_sessions(username)
        if body.get("password") or body.get("role"):
            logger.info("web user %s updated by %s (改密=%s 改角色=%s)",
                        username, session["username"],
                        bool(body.get("password")), body.get("role") or "-")
        return {"ok": True}

    @router.delete("/users/{username}")
    async def delete_user(username: str, request: Request,
                          session: dict = require_role("admin")):
        manager = request.app.state.manager
        if username == session["username"]:
            raise HTTPException(status_code=400, detail="不能删除自己")
        await manager.db.execute(
            "DELETE FROM web_users WHERE username=?", (username,)
        )
        request.app.state.auth.drop_user_sessions(username)
        request.app.state.auth.clear_initial_password(username)
        return {"ok": True}

    return router
