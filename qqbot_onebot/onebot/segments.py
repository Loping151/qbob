"""OneBot v11 消息段编解码.

入参可为 CQ 字符串 / 单个 segment / segment 数组(元素可为字符串), 统一归一为
[{type, data}]; 上报时同时给数组与 raw_message CQ 串.
"""

from __future__ import annotations

import re
from typing import Any

from .faces import EMOJI_BY_CODE, NAME_EMOJI, SYSFACE_NAMES


def cq_escape(text: str, in_param: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    if in_param:
        text = text.replace(",", "&#44;")
    return text


def cq_unescape(text: str) -> str:
    return (
        text.replace("&#44;", ",")
        .replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&amp;", "&")
    )


def segments_to_cq(segments: list[dict]) -> str:
    parts: list[str] = []
    for seg in segments:
        seg_type = seg.get("type", "text")
        data = seg.get("data") or {}
        if seg_type == "text":
            parts.append(cq_escape(str(data.get("text", ""))))
            continue
        params = ",".join(
            f"{key}={cq_escape(str(value), in_param=True)}"
            for key, value in data.items()
            if value is not None and not isinstance(value, (dict, list))
        )
        parts.append(f"[CQ:{seg_type}{',' + params if params else ''}]")
    return "".join(parts)


_CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_.-]+)((?:,[^,\]]*)*)\]")


def cq_to_segments(text: str) -> list[dict]:
    segments: list[dict] = []
    pos = 0
    for match in _CQ_PATTERN.finditer(text):
        if match.start() > pos:
            segments.append(
                {"type": "text", "data": {"text": cq_unescape(text[pos:match.start()])}}
            )
        data: dict[str, str] = {}
        for param in match.group(2).split(","):
            if not param or "=" not in param:
                continue
            key, _, value = param.partition("=")
            data[key] = cq_unescape(value)
        segments.append({"type": match.group(1), "data": data})
        pos = match.end()
    if pos < len(text):
        segments.append({"type": "text", "data": {"text": cq_unescape(text[pos:])}})
    return segments


def normalize_message(message: Any) -> list[dict]:
    """任意插件传参形态 -> [{type, data}] (深拷贝语义, 不改原对象)."""
    if message is None:
        return []
    if isinstance(message, str):
        return cq_to_segments(message)
    if isinstance(message, dict):
        message = [message]
    segments: list[dict] = []
    for item in message:
        if isinstance(item, str):
            segments.extend(cq_to_segments(item))
        elif isinstance(item, dict):
            seg_type = item.get("type", "text")
            data = dict(item.get("data") or {})
            segments.append({"type": seg_type, "data": data})
    return segments


# 收到的消息里这几种段都带 file_id(见 core/bot.py 解析附件); 后端按它认文件(nonebot-plugin-alconna 缺了直接 KeyError)
_FILE_ID_SEG_TYPES = ("image", "record", "video", "file")


def with_file_ids(segments: list[dict]) -> list[dict]:
    """给缺 file_id 的媒体段补上(取 url, 没有就取 file), bot 自己发的消息与收到的同一口径."""
    out: list[dict] = []
    for seg in segments:
        data = seg.get("data") if isinstance(seg, dict) else None
        if isinstance(data, dict) and seg.get("type") in _FILE_ID_SEG_TYPES and not data.get("file_id"):
            seg = {**seg, "data": {**data, "file_id": data.get("url") or data.get("file") or ""}}
        out.append(seg)
    return out


def plaintext(segments: list[dict]) -> str:
    return "".join(
        str(seg.get("data", {}).get("text", ""))
        for seg in segments
        if seg.get("type") == "text"
    )


# emoji_id 的两套编号见 faces.py
_EMOJI_FALLBACK = "👍"
# 表外且不小于此值的整数当 unicode 码点
_EMOJI_MIN_CODEPOINT = 0x2000


def emoji_of(emoji_id: object) -> str:
    """emoji_id -> 可直接发送的文字; 小整数是系统表情(无替身显示 [名字]), 大整数是码点."""
    text = str(emoji_id or "").strip()
    if not text:
        return _EMOJI_FALLBACK
    if not text.isdigit():
        return text                        # 本来就是 emoji 字符
    if text in EMOJI_BY_CODE:
        return EMOJI_BY_CODE[text]
    name = SYSFACE_NAMES.get(text)
    if name:
        return NAME_EMOJI.get(name) or f"[{name}]"
    code = int(text)
    if code >= _EMOJI_MIN_CODEPOINT:
        try:
            return chr(code)
        except (ValueError, OverflowError):
            pass
    return _EMOJI_FALLBACK
