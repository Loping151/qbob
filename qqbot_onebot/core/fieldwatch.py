"""平台下发字段监视: 文档与实测之外的字段一出现就落 unparsed.jsonl(kind=new_field).

结构抄自 api-v2 各事件页的字段表(2026-11-15), 加上实测有而文档没写的(注明日期).
逐层比对(含嵌套与数组元素), 路径形如 `author.member_nickname`、`msg_elements[].attachments[].url`;
每条新路径留 SAMPLES 份样本, 计数存 kv, 重启不重记. 全部 bot 共用一份, 免得同一新字段每个 bot 各记一遍.
发现新字段后把它补进下面的结构, 就不再记了.
"""

from __future__ import annotations

import asyncio
import json
import logging

logger = logging.getLogger("qqbot.fieldwatch")

KV_KEY = "fieldwatch"
SAMPLES = 3


def _leaf(*names: str) -> dict:
    return dict.fromkeys(names)


# 值为 None = 叶子(不往下看); 为 dict = 子结构; 数组元素按同一结构比对
_USER = _leaf(
    "id", "username", "bot", "union_openid", "union_user_account",
    "user_openid", "member_openid", "member_role",
    # 2026-11-13 起群消息带, 文档未写: 群名片 / 头像链接
    "member_nickname", "avatar",
)
_SCENE = _leaf("source", "ext")
_ATTACHMENT = _leaf("url", "filename", "width", "height", "size", "content_type",
                    "voice_wav_url", "asr_refer_text")
_ARK_FIELDS = _leaf(
    # 文档「常见键名」
    "tag", "tags", "title", "desc", "jump_url", "preview", "source", "source_logo",
    "tag_icon", "nickname", "avatar", "address",
    # 2026-09 实测(音乐/邀请/名片类卡片)
    "button", "contact", "cover", "inviteType", "songId", "type",
)
_ARK = {**_leaf("prompt", "ark_type", "ark_name"), "fields": _ARK_FIELDS}
_ELEMENT: dict = {
    **_leaf("msg_idx", "message_type", "content"),
    "author": _USER, "attachments": _ATTACHMENT, "ark_data": _ARK,
}
_ELEMENT["msg_elements"] = _ELEMENT
_MESSAGE = {
    **_leaf("id", "content", "timestamp", "message_type", "group_openid", "group_id"),
    "author": _USER, "message_scene": _SCENE, "attachments": _ATTACHMENT,
    "mentions": _USER, "ark_data": _ARK, "msg_elements": _ELEMENT,
}
_GROUP_OP = _leaf("timestamp", "group_openid", "op_member_openid")
_C2C_OP = _leaf("timestamp", "openid")

SCHEMAS: dict[str, dict] = {
    "GROUP_AT_MESSAGE_CREATE": _MESSAGE,
    "GROUP_MESSAGE_CREATE": _MESSAGE,
    "C2C_MESSAGE_CREATE": _MESSAGE,
    "GROUP_ADD_ROBOT": _GROUP_OP,
    "GROUP_DEL_ROBOT": _GROUP_OP,
    "GROUP_MSG_RECEIVE": _GROUP_OP,
    "GROUP_MSG_REJECT": _GROUP_OP,
    "C2C_MSG_RECEIVE": _C2C_OP,
    "C2C_MSG_REJECT": _C2C_OP,
    "FRIEND_ADD": {**_leaf("timestamp", "openid", "scene", "scene_param", "short_code"),
                   "author": _leaf("union_openid")},
    "FRIEND_DEL": {**_leaf("timestamp", "openid"), "author": _leaf("union_openid")},
    "GROUP_MEMBER_ADD": _leaf("timestamp", "group_openid", "member_openid"),
    "GROUP_MEMBER_REMOVE": _leaf("timestamp", "group_openid", "member_openid",
                                 "user_openid"),
    "GROUP_JOIN_REQUEST": {
        **_leaf("group_openid", "join_request_id", "risk_tips", "union_openid",
                "member_openid", "username", "apply_at", "apply_source",
                "invited_by", "bot"),
        "verify_info": {**_leaf("method", "verify_message"),
                        "review_qa_list": _leaf("question", "answer")},
        "auto_approved": _leaf("strategy_id"),
    },
    "INTERACTION_CREATE": {
        **_leaf("id", "type", "scene", "chat_type", "timestamp", "guild_id",
                "channel_id", "user_openid", "group_openid", "group_member_openid",
                "version", "application_id"),
        "data": {**_leaf("type"), "resolved": {
            **_leaf("button_data", "button_id", "user_id", "feature_id", "message_id",
                    "feedback_opt", "checked", "action"),
            "message_scene": _leaf("ext"),
            "authorize_data": _leaf("opt_scene", "scope"),
        }},
    },
    "SUBSCRIBE_MESSAGE_STATUS": {
        **_leaf("group_openid", "openid"),
        "result": _leaf("template_id", "custom_template_id", "op", "subscribe_id",
                        "subscribe_ts", "update_ts"),
    },
}


def unknown_paths(data, schema: dict, prefix: str = "") -> list[str]:
    """data 里 schema 没列的字段路径(新字段只报它自己, 不再展开它的子键)."""
    found: list[str] = []
    if isinstance(data, list):
        for item in data:
            for path in unknown_paths(item, schema, prefix + "[]"):
                if path not in found:
                    found.append(path)
        return found
    if not isinstance(data, dict):
        return found
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in schema:
            found.append(path)
            continue
        sub = schema[key]
        if sub is not None and isinstance(value, (dict, list)):
            for child in unknown_paths(value, sub, path):
                if child not in found:
                    found.append(child)
    return found


class FieldWatch:
    def __init__(self, db, unparsed) -> None:
        self.db = db
        self.unparsed = unparsed
        self._counts: dict[str, int] = {}   # "事件 路径" -> 已记样本数
        self._ready: asyncio.Future | None = None

    async def _load(self) -> None:
        try:
            raw = await self.db.kv_get(KV_KEY)
            loaded = json.loads(raw) if raw else {}
            if isinstance(loaded, dict):
                for key, count in loaded.items():
                    self._counts[key] = max(self._counts.get(key, 0), int(count))
        except Exception:
            logger.exception("读字段监视计数失败, 当作空的")

    async def check(self, event_type: str, data, appid: str = "") -> list[str]:
        """返回本次记下的新路径(测试用)."""
        schema = SCHEMAS.get(event_type)
        if schema is None or not isinstance(data, dict):
            return []
        if self._ready is None:
            self._ready = asyncio.ensure_future(self._load())
        await self._ready
        fresh = [path for path in unknown_paths(data, schema)
                 if self._counts.get(f"{event_type} {path}", 0) < SAMPLES]
        if not fresh:
            return []
        for path in fresh:
            key = f"{event_type} {path}"
            self._counts[key] = self._counts.get(key, 0) + 1
        self.unparsed.record("new_field", f"{event_type} 出现文档与实测之外的字段: {fresh}",
                             data, appid)
        try:
            await self.db.kv_set(KV_KEY, json.dumps(self._counts, ensure_ascii=False))
        except Exception:
            logger.exception("存字段监视计数失败")
        return fresh
