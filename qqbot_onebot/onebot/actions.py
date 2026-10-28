"""OneBot v11 动作分发器: 标准动作全量 + NapCat 扩展 + OneBotAdditional.

降级矩阵:
- 平台支持 -> 真实现(发消息/撤回/禁言/入群审批/群信息...)
- 查询拿不到数据 -> ok + 空列表/空字段(群公告/精华/荣誉/群文件...)
- 平台无能力 -> failed 1404 "QQ 官方平台不支持"(踢人/改名片/退群/OCR...)
- 能用消息顶上 -> 代发并返回 ok(戳一戳 -> @对方; 表情回应 -> 引用回 emoji)

所有 ID 参数同时接受 int/str; 响应中的 ID 一律 int(15 位虚拟号).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .. import __version__
from ..core.sender import POKE_TEXT, SendError
from ..qq.api import QQApiError
from .segments import emoji_of, segments_to_cq

if TYPE_CHECKING:
    from ..core.bot import BotInstance

logger = logging.getLogger("qqbot.actions")

Handler = Callable[["BotInstance", dict], Awaitable[dict]]
_REGISTRY: dict[str, Handler] = {}


def action(*names: str) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        for name in names:
            _REGISTRY[name] = fn
        return fn
    return wrap


def ok(data: Any = None) -> dict:
    return {"status": "ok", "retcode": 0, "data": data}


def failed(retcode: int, message: str) -> dict:
    return {"status": "failed", "retcode": retcode, "data": None,
            "message": message, "wording": message}


def todo(what: str) -> dict:
    # TODO(qq-official): 平台无对应能力, 统一回报 1404
    return failed(1404, f"QQ 官方平台不支持 - {what}")


def _int_of(value: Any) -> int:
    """宽容取整: 先按 int(15 位虚拟号需精确), 再退 float.

    否则 "1800.5" 会成 0, 而 set_group_ban 的 duration=0 是解禁.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return 0


async def dispatch_action(bot: "BotInstance", raw_action: str, params: dict) -> dict:
    name = raw_action
    for suffix in ("_async", "_rate_limited"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    handler = _REGISTRY.get(name)
    if handler is None:
        logger.info("[%s] unknown action %s", bot.appid, raw_action)
        return failed(1400, f"unsupported action: {raw_action}")
    try:
        return await handler(bot, params or {})
    except SendError as exc:
        logger.warning("[%s] %s%s 失败 %s: %s", bot.appid, name, _target(params),
                       exc.retcode, exc.message)
        return failed(exc.retcode, exc.message)
    except QQApiError as exc:
        logger.warning("[%s] %s%s 失败 qq api %s: %s", bot.appid, name, _target(params),
                       exc.code, exc.message)
        return failed(1500 if exc.status >= 500 else 1404,
                      f"qq api {exc.code}: {exc.message}")
    except Exception as exc:
        logger.exception("[%s] action %s crashed", bot.appid, name)
        return failed(1500, f"internal error: {exc}")


def _target(params: dict) -> str:
    """日志里标出会话, 方便对照用户反馈."""
    params = params or {}
    for key, label in (("group_id", "群"), ("user_id", "用户")):
        if params.get(key):
            return f" {label} {params[key]}"
    return ""


# ---------------- ID 解析辅助 ----------------

def _resolve_group(bot: "BotInstance", params: dict) -> str | None:
    virtual = _int_of(params.get("group_id"))
    entry = bot.idmap.lookup_virtual(virtual)
    if entry and entry.kind == "group" and entry.bot_appid == bot.appid:
        return entry.openid
    return None


def _resolve_user(bot: "BotInstance", params: dict, key: str = "user_id") -> str | None:
    virtual = _int_of(params.get(key))
    if virtual == bot.self_id:
        return bot.appid
    entry = bot.idmap.lookup_virtual(virtual)
    if entry and entry.kind == "user" and entry.bot_appid == bot.appid:
        return entry.openid
    return None


def _peer_allowed(bot: "BotInstance", chat_type: str, openid: str) -> bool:
    """出站也过名单: 禁用即对该会话整体失声. 只拦插件的 OneBot 动作(管理台/内置指令不走这里)."""
    return bot.peer_enabled(chat_type, openid)


def _blocked(chat_type: str, target) -> dict:
    return failed(1403, f"该{'群' if chat_type == 'group' else '用户'}未启用"
                        f"({target}), 主动消息已拦截")


# ================= 消息收发 =================

@action("send_msg")
async def send_msg(bot: "BotInstance", params: dict) -> dict:
    message_type = params.get("message_type")
    if message_type == "group" or (not message_type and params.get("group_id")):
        return await send_group_msg(bot, params)
    return await send_private_msg(bot, params)


@action("send_group_msg")
async def send_group_msg(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {params.get('group_id')}")
    if not _peer_allowed(bot, "group", group_openid):
        return _blocked("group", params.get("group_id"))
    mid = await bot.sender.send("group", group_openid, params.get("message"))
    return ok({"message_id": mid})


@action("send_private_msg")
async def send_private_msg(bot: "BotInstance", params: dict) -> dict:
    user_openid = _resolve_user(bot, params)
    if user_openid is None:
        return failed(1404, f"unknown user_id {params.get('user_id')}")
    if not _peer_allowed(bot, "private", user_openid):
        return _blocked("private", params.get("user_id"))
    mid = await bot.sender.send("private", user_openid, params.get("message"))
    return ok({"message_id": mid})


@action("send_group_forward_msg", "send_private_forward_msg", "send_forward_msg")
async def send_forward(bot: "BotInstance", params: dict) -> dict:
    # 合并转发 -> Sender 内将 node 渲染为长文本+媒体
    messages = params.get("messages") or params.get("message") or []
    if params.get("group_id"):
        group_openid = _resolve_group(bot, params)
        if group_openid is None:
            return failed(1404, f"unknown group_id {params.get('group_id')}")
        if not _peer_allowed(bot, "group", group_openid):
            return _blocked("group", params.get("group_id"))
        mid = await bot.sender.send("group", group_openid, messages)
    else:
        user_openid = _resolve_user(bot, params)
        if user_openid is None:
            return failed(1404, f"unknown user_id {params.get('user_id')}")
        if not _peer_allowed(bot, "private", user_openid):
            return _blocked("private", params.get("user_id"))
        mid = await bot.sender.send("private", user_openid, messages)
    return ok({"message_id": mid, "forward_id": ""})


@action("delete_msg")
async def delete_msg(bot: "BotInstance", params: dict) -> dict:
    mid = _int_of(params.get("message_id"))
    record = await bot.store.get_by_mid(mid)
    if record is None or record["bot_appid"] != bot.appid:
        return failed(1404, f"unknown message_id {mid}")
    if not record.get("qq_msg_id") or record["qq_msg_id"].startswith("unknown-"):
        return failed(1404, "message has no recallable platform id")
    # 一条 OneBot 消息可能拆成多条平台消息, 需整批撤
    targets = [(record["chat_type"], record["peer_openid"], record["qq_msg_id"])]
    batch = record.get("batch_mid") or 0
    if batch:
        siblings = await bot.db.fetchall(
            "SELECT chat_type, peer_openid, qq_msg_id FROM messages"
            " WHERE bot_appid=? AND batch_mid=? AND mid!=? AND qq_msg_id!=''",
            (bot.appid, batch, mid),
        )
        targets += [(r["chat_type"], r["peer_openid"], r["qq_msg_id"])
                    for r in siblings if not r["qq_msg_id"].startswith("unknown-")]
    first_error: QQApiError | None = None
    recalled = 0
    for chat_type, peer_openid, qq_msg_id in targets:
        try:
            if chat_type == "group":
                await bot.api.recall_group_message(peer_openid, qq_msg_id)
            else:
                await bot.api.recall_c2c_message(peer_openid, qq_msg_id)
            recalled += 1
        except QQApiError as exc:
            # 40064004: 超过 2 分钟不可撤回
            if first_error is None:
                first_error = exc
    if not recalled and first_error is not None:
        return failed(1404, f"recall failed {first_error.code}: {first_error.message}")
    await _mark_recalled(bot, batch or mid, mid)
    return ok()


async def _mark_recalled(bot: "BotInstance", batch: int, mid: int) -> None:
    """标记已撤回."""
    now = int(time.time())
    if batch:
        await bot.db.execute(
            "UPDATE messages SET recalled_at=? WHERE bot_appid=? AND batch_mid=?",
            (now, bot.appid, batch))
    else:
        await bot.db.execute(
            "UPDATE messages SET recalled_at=? WHERE mid=?", (now, mid))


@action("get_msg")
async def get_msg(bot: "BotInstance", params: dict) -> dict:
    mid = _int_of(params.get("message_id"))
    record = await bot.store.get_by_mid(mid)
    if record is None or record["bot_appid"] != bot.appid:
        return failed(1404, f"unknown message_id {mid}")
    is_group = record["chat_type"] == "group"
    sender = _record_sender(bot, record)
    data = {
        "time": record["ts"],
        "message_type": "group" if is_group else "private",
        "message_id": mid,
        "real_id": mid,
        "sender": sender,
        "message": record["content"],
        "raw_message": segments_to_cq(record["content"]),
        "message_seq": mid,
        # napcat 口径顶层恒带 user_id, 否则插件 reply.user_id 会 AttributeError
        "user_id": int(sender.get("user_id") or record["user_virtual"] or 0),
    }
    if is_group:
        data["group_id"] = record["peer_virtual"]
    return ok(data)


@action("get_forward_msg")
async def get_forward_msg(bot: "BotInstance", params: dict) -> dict:
    """合并转发内容随消息事件下发(msg_elements), 收到时已存库, 此处按 id 回查."""
    fid = str(params.get("id") or params.get("message_id") or "")
    if fid:
        row = await bot.db.fetchone(
            "SELECT nodes FROM forwards WHERE id=? AND bot_appid=?",
            (fid, bot.appid),
        )
        if row is not None:
            return ok({"messages": json.loads(row["nodes"])})
        # 也可能给的是 OneBot int message_id, 用其平台 id 再查一次
        record = await bot.store.get_by_mid(_int_of(fid))
        if record and record["bot_appid"] == bot.appid:
            row = await bot.db.fetchone(
                "SELECT nodes FROM forwards WHERE id=? AND bot_appid=?",
                (record["qq_msg_id"], bot.appid),
            )
            if row is not None:
                return ok({"messages": json.loads(row["nodes"])})
    # 未命中回空集合, 别让插件流程炸
    logger.info("[%s] get_forward_msg 未命中 id=%s", bot.appid, fid[:48])
    return ok({"messages": []})


@action("mark_msg_as_read", "mark_private_msg_as_read", "mark_group_msg_as_read",
        "_mark_all_as_read", "set_msg_read")
async def mark_read(bot: "BotInstance", params: dict) -> dict:
    return ok()


@action("get_group_msg_history")
async def get_group_msg_history(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {params.get('group_id')}")
    count = min(_int_of(params.get("count")) or 20, 100)
    before = _int_of(params.get("message_seq")) or None
    records = await bot.store.recent_for_chat(
        bot.appid, "group", group_openid, limit=count, before_mid=before
    )
    return ok({"messages": [_history_item(bot, r) for r in records]})


@action("get_friend_msg_history")
async def get_friend_msg_history(bot: "BotInstance", params: dict) -> dict:
    user_openid = _resolve_user(bot, params)
    if user_openid is None:
        return failed(1404, f"unknown user_id {params.get('user_id')}")
    count = min(_int_of(params.get("count")) or 20, 100)
    before = _int_of(params.get("message_seq")) or None
    records = await bot.store.recent_for_chat(
        bot.appid, "private", user_openid, limit=count, before_mid=before
    )
    return ok({"messages": [_history_item(bot, r) for r in records]})


def _record_sender(bot: "BotInstance", record: dict) -> dict:
    """记录 -> OneBot sender; 外发消息补 bot 身份(nickname 须非空, 插件常取 card or nickname)."""
    sender = record["sender"] or {}
    if sender:
        return sender
    if record["direction"] == "out":
        return {"user_id": bot.self_id, "nickname": bot.name, "card": "",
                "role": "member"}
    return {"user_id": record["user_virtual"], "nickname": "", "card": "",
            "role": "member"}


def _history_item(bot: "BotInstance", record: dict) -> dict:
    sender = _record_sender(bot, record)
    item = {
        "time": record["ts"],
        "message_type": record["chat_type"] if record["chat_type"] == "group" else "private",
        "message_id": record["mid"],
        "real_id": record["mid"],
        "message_seq": record["mid"],
        "sender": sender,
        "message": record["content"],
        "raw_message": segments_to_cq(record["content"]),
        "user_id": record["user_virtual"],
        "self_id": bot.self_id,
        "post_type": "message",
    }
    if record["chat_type"] == "group":
        item["group_id"] = record["peer_virtual"]
    return item


# ================= 资料查询 =================

@action("get_login_info")
async def get_login_info(bot: "BotInstance", params: dict) -> dict:
    return ok({"user_id": bot.self_id, "nickname": bot.name})


@action("get_stranger_info")
async def get_stranger_info(bot: "BotInstance", params: dict) -> dict:
    virtual = _int_of(params.get("user_id"))
    if virtual == bot.self_id:
        return ok({"user_id": bot.self_id, "nickname": bot.name,
                   "sex": "unknown", "age": 0, "qid": "", "level": 0,
                   "login_days": 0})
    entry = bot.idmap.lookup_virtual(virtual)
    if entry is None or entry.kind != "user":
        return failed(1404, f"unknown user_id {virtual}")
    # TODO(qq-official): 无用户资料接口, city/area 等扩展字段恒为空
    return ok({"user_id": virtual, "nickname": entry.nickname, "sex": "unknown",
               "age": 0, "qid": "", "level": 0, "login_days": 0,
               "city": "", "area": "", "province": "", "country": ""})


@action("get_friend_list")
async def get_friend_list(bot: "BotInstance", params: dict) -> dict:
    rows = await bot.db.fetchall(
        "SELECT DISTINCT peer_virtual FROM messages"
        " WHERE bot_appid=? AND chat_type='private'", (bot.appid,)
    )
    friends = []
    for row in rows:
        entry = bot.idmap.lookup_virtual(row["peer_virtual"])
        friends.append({
            "user_id": row["peer_virtual"],
            "nickname": entry.nickname if entry else "",
            "remark": "",
        })
    return ok(friends)


@action("get_unidirectional_friend_list", "get_friends_with_category")
async def get_friend_list_variants(bot: "BotInstance", params: dict) -> dict:
    return ok([])


@action("get_group_info", "get_group_detail_info", "get_group_info_ex")
async def get_group_info(bot: "BotInstance", params: dict) -> dict:
    virtual = _int_of(params.get("group_id"))
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {virtual}")
    name, member_num = "", 0
    try:
        info = await bot.api.group_info(group_openid)
        name = str(info.get("group_name", ""))
        member_num = _int_of(info.get("group_member_num"))
        if name:
            await bot.idmap.to_virtual(bot.appid, "group", group_openid, nickname=name)
        # 缓存人数给 get_group_list 用
        await bot.db.execute(
            "UPDATE peer_states SET member_count=? WHERE bot_appid=?"
            " AND chat_type='group' AND peer_openid=?",
            (member_num, bot.appid, group_openid),
        )
    except Exception as exc:
        logger.info("[%s] group_info failed: %s", bot.appid, exc)
        entry = bot.idmap.lookup_virtual(virtual)
        name = entry.nickname if entry else ""
    return ok({"group_id": virtual, "group_name": name or f"群{virtual}",
               "group_memo": "", "group_create_time": 0, "group_level": 0,
               "member_count": member_num,
               # TODO(qq-official): 平台无群容量; 拿人数充数会让插件误判群满
               "max_member_count": 0})


@action("get_group_list")
async def get_group_list(bot: "BotInstance", params: dict) -> dict:
    rows = await bot.db.fetchall(
        "SELECT m.virtual_id, m.nickname, COALESCE(p.member_count, 0) AS member_count"
        " FROM id_map m LEFT JOIN peer_states p"
        "   ON p.bot_appid=m.bot_appid AND p.chat_type='group'"
        "  AND p.peer_openid=m.openid"
        " WHERE m.bot_appid=? AND m.kind='group' ORDER BY m.last_seen DESC",
        (bot.appid,),
    )
    # 只列启用的群, 免得广播类插件遍历到禁用群
    allowed_rows = []
    for row in rows:
        entry = bot.idmap.lookup_virtual(row["virtual_id"])
        if entry and _peer_allowed(bot, "group", entry.openid):
            allowed_rows.append(row)
    rows = allowed_rows
    # member_count 取 get_group_info 的缓存(没查过为 0)
    groups = [
        {"group_id": row["virtual_id"],
         "group_name": row["nickname"] or f"群{row['virtual_id']}",
         "member_count": row["member_count"], "max_member_count": 0}
        for row in rows
    ]
    return ok(groups)


async def _self_member_role(bot: "BotInstance", group_openid: str) -> tuple[str, int]:
    """bot 在该群的 (角色, 入群时间), 走 peer_state 缓存."""
    state = await bot.group_state(group_openid, reason="member-info")
    join_time = 0
    if state.joined_at:
        try:
            from datetime import datetime  # noqa: PLC0415
            join_time = int(datetime.fromisoformat(state.joined_at).timestamp())
        except ValueError:
            pass
    return state.bot_role or "member", join_time


@action("get_group_member_info")
async def get_group_member_info(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {params.get('group_id')}")
    virtual = _int_of(params.get("user_id"))
    group_virtual = _int_of(params.get("group_id"))

    if virtual == bot.self_id:
        role, join_time = await _self_member_role(bot, group_openid)
        return ok(_member_dict(group_virtual, bot.self_id, bot.name, "",
                               role, join_time))

    user_openid = _resolve_user(bot, params)
    if user_openid is None:
        return failed(1404, f"unknown user_id {params.get('user_id')}")
    row = await bot.db.fetchone(
        "SELECT nickname, role, last_seen FROM group_members"
        " WHERE bot_appid=? AND group_openid=? AND user_openid=?",
        (bot.appid, group_openid, user_openid),
    )
    entry = bot.idmap.lookup_virtual(virtual)
    nickname = (row["nickname"] if row else "") or (entry.nickname if entry else "")
    role = (row["role"] if row else "") or "member"
    last_seen = row["last_seen"] if row else 0
    # TODO(qq-official): 无成员详情接口, 数据来自事件缓存(card 恒等于昵称)
    return ok(_member_dict(group_virtual, virtual, nickname, nickname, role, 0,
                           last_sent=last_seen))


def _member_dict(group_id: int, user_id: int, nickname: str, card: str,
                 role: str, join_time: int, last_sent: int = 0) -> dict:
    return {
        "group_id": group_id, "user_id": user_id,
        "nickname": nickname or "", "card": card or "",
        "sex": "unknown", "age": 0, "area": "",
        "join_time": join_time, "last_sent_time": last_sent,
        "level": "0", "role": role or "member", "unfriendly": False,
        "title": "", "title_expire_time": 0, "card_changeable": False,
        "shut_up_timestamp": 0,
    }


@action("get_group_member_list")
async def get_group_member_list(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {params.get('group_id')}")
    group_virtual = _int_of(params.get("group_id"))
    rows = await bot.db.fetchall(
        "SELECT user_openid, nickname, role, last_seen FROM group_members"
        " WHERE bot_appid=? AND group_openid=? ORDER BY last_seen DESC",
        (bot.appid, group_openid),
    )
    members = []
    for row in rows:
        virtual = await bot.idmap.to_virtual(bot.appid, "user", row["user_openid"])
        members.append(_member_dict(
            group_virtual, virtual, row["nickname"], row["nickname"],
            row["role"], 0, last_sent=row["last_seen"],
        ))
    # bot 用真实角色, 写死 member 会让统计群管的逻辑漏掉 bot
    self_role, self_join = await _self_member_role(bot, group_openid)
    members.append(_member_dict(group_virtual, bot.self_id, bot.name, "",
                                self_role, self_join))
    # TODO(qq-official): 无全量成员接口, 仅返回发言缓存过的成员(90 天没出现的会被清掉)
    return ok(members)


@action("get_group_honor_info")
async def get_group_honor_info(bot: "BotInstance", params: dict) -> dict:
    # TODO(qq-official): 无群荣誉数据 -> 空结构
    return ok({"group_id": _int_of(params.get("group_id")),
               "current_talkative": None, "talkative_list": [],
               "performer_list": [], "legend_list": [],
               "strong_newbie_list": [], "emotion_list": []})


@action("get_group_at_all_remain")
async def get_group_at_all_remain(bot: "BotInstance", params: dict) -> dict:
    # 平台: bot 须群管, 每群每天 1 次; 不在名单里的群不打 bot_state
    group_openid = _resolve_group(bot, params)
    remain = 0
    if group_openid is not None and _peer_allowed(bot, "group", group_openid):
        remain = await bot.sender.at_all_remain(group_openid)
    return ok({"can_at_all": remain > 0, "remain_at_all_count_for_group": remain,
               "remain_at_all_count_for_uin": remain})


@action("get_group_system_msg")
async def get_group_system_msg(bot: "BotInstance", params: dict) -> dict:
    return ok({"invited_requests": [], "join_requests": []})


# ================= 群管理 =================

@action("set_group_ban")
async def set_group_ban(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    user_openid = _resolve_user(bot, params)
    if group_openid is None or user_openid is None:
        return failed(1404, "unknown group_id/user_id")
    duration = _int_of(params.get("duration", 1800))
    expire = int(time.time()) + duration if duration > 0 else 0
    await bot.api.set_member_mute(group_openid, user_openid, expire)
    return ok()


@action("set_group_whole_ban")
async def set_group_whole_ban(bot: "BotInstance", params: dict) -> dict:
    # api-v2 无全员禁言(restrict_chat_setting POST 只收 members[], global_rule 只读),
    # 明确报未实现, 免得插件以为成功
    return todo("set_group_whole_ban: 群聊官方接口不支持全员禁言")


@action("set_group_add_request")
async def set_group_add_request(bot: "BotInstance", params: dict) -> dict:
    flag = str(params.get("flag", ""))
    if "|" not in flag:
        return failed(1400, "invalid flag")
    # flag = group|member|join_request_id(老 flag 无第三段)
    parts = flag.split("|")
    group_openid, member_openid = parts[0], parts[1]
    join_request_id = parts[2] if len(parts) > 2 else ""
    approve = params.get("approve", True)
    approve = approve if isinstance(approve, bool) else str(approve).lower() != "false"
    await bot.api.approve_join_request(
        group_openid, member_openid, approve,
        reject_reason=str(params.get("reason", "")),
        join_request_id=join_request_id,
    )
    return ok()


@action("set_friend_add_request")
async def set_friend_add_request(bot: "BotInstance", params: dict) -> dict:
    # QQ 官方 bot 无好友申请流(FRIEND_ADD 是结果通知), 静默成功
    return ok()


@action("set_group_kick", "set_group_kick_members")
async def set_group_kick(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_kick: 官方接口不支持移除群成员")


@action("set_group_admin")
async def set_group_admin(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_admin: 官方接口不支持设置管理员")


@action("set_group_card")
async def set_group_card(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_card: 官方接口不支持改群名片")


@action("set_group_name")
async def set_group_name(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_name: 官方接口不支持改群名")


@action("set_group_leave")
async def set_group_leave(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_leave: 官方接口不支持主动退群(需群主移除)")


@action("set_group_special_title")
async def set_group_special_title(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_special_title: 官方接口不支持头衔")


@action("set_group_anonymous", "set_group_anonymous_ban")
async def set_group_anonymous(bot: "BotInstance", params: dict) -> dict:
    return todo("匿名相关: 官方群聊无匿名体系")


@action("set_group_portrait")
async def set_group_portrait(bot: "BotInstance", params: dict) -> dict:
    return todo("set_group_portrait: 官方接口不支持设置群头像")


@action("_get_group_notice", "get_group_notice")
async def get_group_notice(bot: "BotInstance", params: dict) -> dict:
    # TODO(qq-official): 无群公告接口 -> 空列表
    return ok([])


@action("_send_group_notice", "send_group_notice", "_del_group_notice",
        "del_group_notice")
async def group_notice_write(bot: "BotInstance", params: dict) -> dict:
    return todo("群公告读写: 官方接口不支持")


@action("get_essence_msg_list", "get_group_essence")
async def get_essence_msg_list(bot: "BotInstance", params: dict) -> dict:
    # TODO(qq-official): 无精华消息 -> 空列表
    return ok([])


@action("set_essence_msg", "delete_essence_msg")
async def essence_write(bot: "BotInstance", params: dict) -> dict:
    return todo("精华消息: 官方接口不支持")


# ================= 文件 =================

@action("upload_group_file")
async def upload_group_file(bot: "BotInstance", params: dict) -> dict:
    group_openid = _resolve_group(bot, params)
    if group_openid is None:
        return failed(1404, f"unknown group_id {params.get('group_id')}")
    mid = await bot.sender.send("group", group_openid, [{
        "type": "file",
        "data": {"file": str(params.get("file", "")),
                 "name": str(params.get("name", "文件"))},
    }])
    return ok({"message_id": mid})


@action("upload_private_file")
async def upload_private_file(bot: "BotInstance", params: dict) -> dict:
    user_openid = _resolve_user(bot, params)
    if user_openid is None:
        return failed(1404, f"unknown user_id {params.get('user_id')}")
    mid = await bot.sender.send("private", user_openid, [{
        "type": "file",
        "data": {"file": str(params.get("file", "")),
                 "name": str(params.get("name", "文件"))},
    }])
    return ok({"message_id": mid})


def _local_token(bot: "BotInstance", file_id: str) -> str | None:
    """本地媒体的 token: media://token, 或直接给的 token."""
    token = bot.media.parse_token(file_id) or file_id
    return token if bot.media.resolve(token) is not None else None


@action("get_group_file_url", "get_private_file_url")
async def get_group_file_url(bot: "BotInstance", params: dict) -> dict:
    file_id = str(params.get("file_id") or params.get("file") or "")
    if file_id.startswith(("http://", "https://")):
        return ok({"url": file_id})
    if _local_token(bot, file_id) is not None:
        return failed(1404, "本地文件不对外提供下载地址, 请用 get_file")
    return failed(1404, f"unknown file_id {file_id[:64]}")


@action("get_file")
async def get_file(bot: "BotInstance", params: dict) -> dict:
    file_id = str(params.get("file_id") or params.get("file") or "")
    want_base64 = str(params.get("type", "")) == "base64"
    name = file_id.rsplit("/", 1)[-1][:120]
    if file_id.startswith(("http://", "https://")):
        data: dict = {"url": file_id, "file": file_id, "file_name": name,
                      "file_size": 0}
        if want_base64:
            try:
                async with bot.http.get(file_id) as resp:
                    raw = await resp.read()
                if len(raw) <= 20 * 1024 * 1024:
                    data["base64"] = base64.b64encode(raw).decode()
                    data["file_size"] = len(raw)
            except Exception as exc:
                logger.info("get_file download failed: %s", exc)
        return ok(data)
    token = _local_token(bot, file_id)
    if token is not None:
        path = bot.media.resolve(token)
        data = {"file": str(path), "path": str(path), "file_name": path.name,
                "file_size": path.stat().st_size}
        if want_base64:
            raw = await asyncio.to_thread(path.read_bytes)
            data["base64"] = base64.b64encode(raw).decode()
        return ok(data)
    return failed(1404, f"unknown file {file_id[:64]}")


@action("get_image", "get_record")
async def get_media_file(bot: "BotInstance", params: dict) -> dict:
    return await get_file(bot, params)


@action("download_file")
async def download_file(bot: "BotInstance", params: dict) -> dict:
    url = str(params.get("url", ""))
    if not url.startswith(("http://", "https://")):
        return failed(1400, "invalid url")
    try:
        async with bot.http.get(url) as resp:
            raw = await resp.read()
    except Exception as exc:
        return failed(1500, f"download failed: {exc}")
    token = bot.media.put_bytes(raw)
    path = bot.media.resolve(token)
    return ok({"file": str(path)})


@action("get_group_root_files", "get_group_files_by_folder")
async def get_group_files(bot: "BotInstance", params: dict) -> dict:
    # TODO(qq-official): 无群文件系统 -> 空结构
    return ok({"files": [], "folders": []})


@action("get_group_file_system_info")
async def get_group_file_system_info(bot: "BotInstance", params: dict) -> dict:
    return ok({"file_count": 0, "limit_count": 0, "used_space": 0,
               "total_space": 0})


@action("create_group_file_folder", "delete_group_folder", "delete_group_file",
        "trans_group_file", "rename_group_file")
async def group_file_write(bot: "BotInstance", params: dict) -> dict:
    return todo("群文件管理: 官方接口不支持")


# ================= 状态/元信息 =================

@action("get_status")
async def get_status(bot: "BotInstance", params: dict) -> dict:
    return ok({"online": True, "good": True,
               "stat": {"packet_received": 0, "packet_sent": 0,
                        "message_received": 0, "message_sent": 0}})


@action("get_version_info", "get_version")
async def get_version_info(bot: "BotInstance", params: dict) -> dict:
    return ok({"app_name": "qqbot-onebot", "app_version": __version__,
               "protocol_version": "v11"})


# 媒体走分片直传, 不依赖公网地址, 恒可发
@action("can_send_image")
async def can_send_image(bot: "BotInstance", params: dict) -> dict:
    return ok({"yes": True})


@action("can_send_record")
async def can_send_record(bot: "BotInstance", params: dict) -> dict:
    return ok({"yes": True})


@action("set_restart", "clean_cache")
async def noop_ok(bot: "BotInstance", params: dict) -> dict:
    return ok()


@action("get_cookies", "get_csrf_token", "get_credentials")
async def get_cookies(bot: "BotInstance", params: dict) -> dict:
    return todo("get_cookies: 官方 bot 无 web 态")


@action("get_online_clients")
async def get_online_clients(bot: "BotInstance", params: dict) -> dict:
    return ok({"clients": []})


@action("get_robot_uin_range")
async def get_robot_uin_range(bot: "BotInstance", params: dict) -> dict:
    return ok([])


@action("send_like")
async def send_like(bot: "BotInstance", params: dict) -> dict:
    return todo("send_like: 官方接口不支持点赞")


@action("group_poke", "friend_poke", "send_poke")
async def poke(bot: "BotInstance", params: dict) -> dict:
    """戳一戳: 平台无接口, 改发 @对方 消息(群里走 markdown, 私聊降级为 "@昵称")."""
    # 认不出人就不发, 否则群里刷出 "@ 戳了戳你"
    user_openid = _resolve_user(bot, params)
    if user_openid is None:
        return failed(1404, f"unknown user_id {params.get('user_id')}")
    message = [{"type": "at", "data": {"qq": str(params.get("user_id", ""))}},
               {"type": "text", "data": {"text": POKE_TEXT}}]
    if params.get("group_id"):
        group_openid = _resolve_group(bot, params)
        if group_openid is None:
            # 群认不出也不改走私聊
            return failed(1404, f"unknown group_id {params.get('group_id')}")
        if not _peer_allowed(bot, "group", group_openid):
            return _blocked("group", params.get("group_id"))
        mid = await bot.sender.send("group", group_openid, message)
    else:
        if not _peer_allowed(bot, "private", user_openid):
            return _blocked("private", params.get("user_id"))
        mid = await bot.sender.send("private", user_openid, message)
    return ok({"message_id": mid})


@action("set_msg_emoji_like")
async def set_msg_emoji_like(bot: "BotInstance", params: dict) -> dict:
    """表情回应: 平台无接口, 改为引用原消息回一个 emoji.

    失败一律返回 ok: 插件常裸调它, failed 会触发 ActionFailed 炸断整个 handler.
    """
    if not params.get("set", True):
        return ok()                      # 取消回应: 无对应动作
    mid = _int_of(params.get("message_id"))
    record = await bot.store.get_by_mid(mid)
    if record is None or record["bot_appid"] != bot.appid:
        logger.debug("[%s] set_msg_emoji_like: 找不到消息 %s", bot.appid, mid)
        return ok()
    message = [{"type": "reply", "data": {"id": str(mid)}},
               {"type": "text", "data": {"text": emoji_of(params.get("emoji_id"))}}]
    try:
        await bot.sender.send(record["chat_type"], record["peer_openid"], message)
    except Exception as exc:             # noqa: BLE001 装饰性动作不该炸断 handler
        logger.warning("[%s] set_msg_emoji_like 代发失败: %s", bot.appid, exc)
    return ok()


@action("ocr_image", ".ocr_image")
async def ocr_image(bot: "BotInstance", params: dict) -> dict:
    return todo("ocr_image: 官方接口不支持 OCR")


@action("get_mini_app_ark")
async def get_mini_app_ark(bot: "BotInstance", params: dict) -> dict:
    return todo("get_mini_app_ark: 官方接口不支持小程序签名")


@action("set_online_status", "set_qq_avatar", "set_self_longnick",
        "set_input_status", "set_diy_online_status")
async def self_profile_write(bot: "BotInstance", params: dict) -> dict:
    return todo("资料/状态设置: 官方接口不支持")


@action("get_ai_characters", "get_ai_record", "send_group_ai_record")
async def ai_record(bot: "BotInstance", params: dict) -> dict:
    return todo("AI 声聊: 官方接口不支持")


@action("delete_friend", "delete_unidirectional_friend")
async def delete_friend(bot: "BotInstance", params: dict) -> dict:
    return todo("delete_friend: 官方接口不支持")


@action(".handle_quick_operation", "handle_quick_operation")
async def handle_quick_operation(bot: "BotInstance", params: dict) -> dict:
    context = params.get("context") or {}
    operation = params.get("operation") or {}
    if operation.get("reply") is not None:
        send_params = {
            "message": operation["reply"],
            "group_id": context.get("group_id"),
            "user_id": context.get("user_id"),
            "message_type": context.get("message_type"),
        }
        return await send_msg(bot, send_params)
    if operation.get("delete"):
        return await delete_msg(bot, {"message_id": context.get("message_id")})
    if operation.get("ban"):
        return await set_group_ban(bot, {
            "group_id": context.get("group_id"),
            "user_id": context.get("user_id"),
            "duration": operation.get("ban_duration", 1800),
        })
    if operation.get("approve") is not None and context.get("flag"):
        return await set_group_add_request(bot, {
            "flag": context.get("flag"),
            "approve": operation.get("approve"),
        })
    return ok()


# ================= OneBotAdditional 私有扩展 =================

@action("get_additional_user_detail")
async def get_additional_user_detail(bot: "BotInstance", params: dict) -> dict:
    """OneBotAdditional 约定(见 docs/api.md); data 顶层另附真实 openid/appid."""
    raw_user_id = params.get("user_id")
    if raw_user_id is None:
        return failed(1400, "missing user_id")
    virtual = _int_of(raw_user_id)
    size = _int_of(params.get("size", 640)) if params.get("size") is not None else 640

    if virtual == bot.self_id:
        openid, nickname, kind = bot.appid, bot.name, "bot"
    else:
        entry = bot.idmap.lookup_virtual(virtual)
        if entry is None or entry.bot_appid != bot.appid:
            return failed(1404, "user not found")
        openid, nickname, kind = entry.openid, entry.nickname, entry.kind

    spec = "0" if size == 0 else ("100" if size <= 100 else "640")
    if kind == "bot":
        # /users/@me 的头像优先
        avatar_url = bot.me_info.get("avatar") or (
            f"https://q.qlogo.cn/qqapp/{bot.appid}/{openid}/{spec}"
        )
    else:
        avatar_url = f"https://q.qlogo.cn/qqapp/{bot.appid}/{openid}/{spec}"

    union = None
    row = await bot.db.fetchone(
        "SELECT union_openid FROM id_map WHERE virtual_id=?", (virtual,)
    )
    if row is not None:
        union = row["union_openid"]

    return ok({
        "user_id": virtual,
        "platform": "qqbot",
        "nickname": nickname or "",
        "avatar": {
            "type": "url",
            "data": avatar_url,
            "size": 0 if spec == "0" else int(spec),
        },
        # 扩展字段: 真实平台标识
        "openid": openid,
        "union_openid": union or "",
        "appid": bot.appid,
        "kind": kind,
    })


@action("get_additional_group_detail")
async def get_additional_group_detail(bot: "BotInstance", params: dict) -> dict:
    """群详情(OneBotAdditional 预留): 群名/人数 + 真实 openid."""
    virtual = _int_of(params.get("group_id"))
    entry = bot.idmap.lookup_virtual(virtual)
    if entry is None or entry.kind != "group" or entry.bot_appid != bot.appid:
        return failed(1404, "group not found")
    name, member_num = entry.nickname, 0
    try:
        info = await bot.api.group_info(entry.openid)
        name = str(info.get("group_name", "")) or name
        member_num = _int_of(info.get("group_member_num"))
    except Exception:
        pass
    return ok({
        "group_id": virtual,
        "platform": "qqbot",
        "group_name": name or "",
        "member_count": member_num,
        "openid": entry.openid,
        "appid": bot.appid,
    })
