"""OneBot v11 事件构造.

BotShepherd 用 pydantic 严格校验 message 事件(各 id 须 int, raw_message 须 str,
须有 font), 不合格整条丢弃, 故字段须完备.
"""

from __future__ import annotations

import time

from .segments import segments_to_cq


def lifecycle_event(self_id: int) -> dict:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": "connect",
    }


def heartbeat_event(self_id: int, interval_ms: int) -> dict:
    return {
        "time": int(time.time()),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"online": True, "good": True},
        "interval": interval_ms,
    }


def make_sender(user_id: int, nickname: str, role: str = "member",
                card: str = "") -> dict:
    """nickname=QQ 昵称(author.username), card=群名片(author.member_nickname)."""
    return {
        "user_id": user_id,
        "nickname": nickname or "",
        "card": card or "",
        "sex": "unknown",
        "age": 0,
        "area": "",
        "level": "0",
        "role": role if role in ("owner", "admin", "member") else "member",
        "title": "",
    }


def group_message_event(
    self_id: int,
    group_id: int,
    user_id: int,
    message_id: int,
    segments: list[dict],
    sender: dict,
    ts: int,
) -> dict:
    return {
        "time": ts,
        "self_id": self_id,
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": message_id,
        "group_id": group_id,
        "user_id": user_id,
        "anonymous": None,
        "message": segments,
        "raw_message": segments_to_cq(segments),
        "font": 0,
        "sender": sender,
    }


def private_message_event(
    self_id: int,
    user_id: int,
    message_id: int,
    segments: list[dict],
    sender: dict,
    ts: int,
) -> dict:
    return {
        "time": ts,
        "self_id": self_id,
        "post_type": "message",
        "message_type": "private",
        "sub_type": "friend",
        "message_id": message_id,
        "user_id": user_id,
        "message": segments,
        "raw_message": segments_to_cq(segments),
        "font": 0,
        "sender": sender,
    }


def notice_event(self_id: int, notice_type: str, ts: int, **fields) -> dict:
    event = {
        "time": ts,
        "self_id": self_id,
        "post_type": "notice",
        "notice_type": notice_type,
    }
    event.update(fields)
    return event
