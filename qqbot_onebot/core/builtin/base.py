"""内置指令公共基建: 上下文、注册表、小工具."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Awaitable, Callable

from ..t2s import to_simplified

if TYPE_CHECKING:
    from ..bot import BotInstance

logger = logging.getLogger("qqbot.builtin")


@dataclass
class CommandContext:
    """一次内置指令调用的全部输入 + 回复能力."""

    bot: "BotInstance"
    command: str
    arg: str
    chat_type: str
    peer_openid: str
    user_openid: str
    user_virtual: int
    member_role: str
    reply_mid: int          # 触发消息 mid(引用回复用)
    quoted_mid: int = 0     # 触发消息引用的 mid
    state: dict = field(default_factory=dict)   # 跨指令共享态

    @property
    def is_su(self) -> bool:
        return self.user_virtual in set(self.bot.superusers)

    @property
    def is_group_admin(self) -> bool:
        return self.member_role in ("admin", "owner")

    async def reply(self, text: str) -> int | None:
        """引用触发消息回一条文本; 返回外发消息的 mid(拿不到则 None)."""
        try:
            # 不转图: 启用码要能复制, 链接要能点
            return await self.bot.sender.send(
                self.chat_type, self.peer_openid,
                [{"type": "reply", "data": {"id": str(self.reply_mid)}},
                 {"type": "text", "data": {"text": text}}],
                text_image=False,
            )
        except Exception as exc:
            logger.warning("builtin reply failed: %s", exc)
            return None

    async def say(self, text: str, image: str = "", quote: bool = False) -> int | None:
        """发一条, 默认不带引用(quote 则引用触发消息); 带 image 则图文同气泡."""
        segments: list[dict] = []
        if quote:
            segments.append({"type": "reply", "data": {"id": str(self.reply_mid)}})
        if image:
            segments.append({"type": "image", "data": {"file": image}})
        segments.append({"type": "text", "data": {"text": text}})
        try:
            return await self.bot.sender.send(
                self.chat_type, self.peer_openid, segments, text_image=False)
        except Exception as exc:
            if image:
                # 图挂了退回纯文本重试
                logger.warning("builtin say(图文) failed, 退回纯文本: %s", exc)
                return await self.say(text, quote=quote)
            logger.warning("builtin say failed: %s", exc)
            return None

    async def quoted_text(self) -> str:
        """被引用消息的纯文本(取不到返回空串)."""
        if not self.quoted_mid:
            return ""
        try:
            record = await self.bot.store.get_by_mid(self.quoted_mid)
        except Exception as exc:
            logger.warning("quoted message %s lookup failed: %s",
                           self.quoted_mid, exc)
            return ""
        if not record:
            return ""
        return "\n".join(
            seg["data"].get("text", "")
            for seg in record.get("content", [])
            if isinstance(seg, dict) and seg.get("type") == "text"
        )


Handler = Callable[[CommandContext], Awaitable[bool]]

# 触发词 -> 处理函数; 按长度倒序匹配, 免得短词抢了长词前缀
_REGISTRY: dict[str, Handler] = {}
# 靠引用消息激活的处理器(如号主回填)
_REPLY_HOOKS: list[Callable[[CommandContext], Awaitable[bool]]] = []


# 只接受完全匹配的触发词, 免得正常聊天被误当成指令
_EXACT_ONLY: set[str] = set()


def command(*names: str, exact: bool = False) -> Callable[[Handler], Handler]:
    def decorate(func: Handler) -> Handler:
        for name in names:
            _REGISTRY[name] = func
            if exact:
                _EXACT_ONLY.add(name.lower())
        return func
    return decorate


def reply_hook(func: Callable[[CommandContext], Awaitable[bool]]):
    """注册引用回复型处理器(无触发词)."""
    _REPLY_HOOKS.append(func)
    return func


def match_builtin(text: str) -> tuple[str, str] | None:
    """返回 (命令, 参数) 或 None; 不区分大小写, 繁体也认(獲取信息/啟用/創建bot)."""
    text = to_simplified(text.strip())
    if text.startswith("/"):
        text = text[1:].strip()
    lowered = text.lower()
    for name in sorted(_REGISTRY, key=len, reverse=True):
        key = name.lower()
        if lowered == key:
            return name, ""
        if key not in _EXACT_ONLY and lowered.startswith(key):
            return name, text[len(name):].strip()
    return None


def get_handler(name: str) -> Handler | None:
    return _REGISTRY.get(name)


def reply_hooks() -> list[Callable[[CommandContext], Awaitable[bool]]]:
    return list(_REPLY_HOOKS)
