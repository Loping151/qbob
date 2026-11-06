"""OneBot 消息段 -> QQ 官方消息 发送管线.

平台硬约束(实测, 文档没写全):
- msg_type: 0 纯文本 / 2 markdown / 7 富媒体 / 8 图文卡片(见 cards); 7 可带 content -> 图文同一气泡.
- markdown 与 media 不能共存(图片被静默吞掉); 7 再带 markdown -> 22006.
- <qqbot-at-user/> 与 <qqbot-cmd-input/enter/> 只在 msg_type=2 生效, 进纯文本会坏
  => "@某人 + 图片" 必然是两条消息.
- 群聊不认 <qqbot-cmd-enter/>(整条 40034106) => 群聊出站换成 <qqbot-cmd-input/>.
- 引用回复的 message_reference.message_id 只认 msg_idx(REFIDX_xxx).

图文配对(见 group_items): 首段是文本 -> 文字归后面最近的图, 末尾多出的单独发;
首段是图片 -> 文字归前面最近的图.

降级:
- at(markdown 关闭/openid 未知/私聊) -> "@昵称"
- at all: 群聊走 markdown 的 <qqbot-at-everyone/>(2026-10-26 起, 10-28 实测可用; bot 须群管,
  每群每天 1 次); 非群管/当天用过/被拒 -> "@全体成员" 文本
- 指令标签(markdown 关闭/被拒) -> 指令原文
- share/music/json(小程序等) -> 图文卡片(要 https 跳转和封面; 卡片单独一条, 不带引用,
  被拒改发文字), 否则标题+链接; xml -> 标题+链接; face -> emoji/名字
- node/forward -> 交给 forward_out 插件(转网页, 发「聊天记录」卡片), 没人接手就摊平成正文+媒体;
  兑换码类(无媒体)摊平成纯文本留在群里复制
- record -> silk 直传, 其余 ffmpeg 解 pcm 后 pilk 编 silk
- markdown 图片 ![](url) -> 原生富媒体(不带尺寸渲染会坏)
- URL 被拦(40054010) -> '.' 换成 '．' 重试; markdown 被拒 -> 去装饰纯文本
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import re
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlparse

import aiohttp

from ..config import option
from ..idmap import is_virtual_id
from ..plugin import HookContext, registry as plugins
from ..qq.api import QQApiError
from ..onebot import events as ob_events
from ..onebot.segments import emoji_of, normalize_message
from . import cards, cdnkeys, forwardpage, textimg
from .quota import BEIJING
from .textimg import TEXT_IMAGE_MIN_CHARS, extract_links, looks_like_markdown
from .uploader import MediaTooLarge

if TYPE_CHECKING:
    from .bot import BotInstance

logger = logging.getLogger("qqbot.sender")

# 转发网页建页时同时上传几个媒体(瓶颈是每张 3 次 API 往返, 不是带宽)
PAGE_UPLOAD_CONCURRENCY = 8
# _send_one 的 slot 缺省: 现取被动回复名额
_ACQUIRE = object()
# 做卡片封面时拉原图的大小上限
COVER_FETCH_MAX = 30 * 1024 * 1024
# 平台语音时长上限 300 秒(2026-11-02 只上传不发送实测: 300 收, 301 起 40093013
# 「上传音频时长超过限制」); 留 1 秒余量, 超出的截掉, 别让整首歌发不出去
VOICE_MAX_SECONDS = 299
# ---- 平台错误码分类(对照官方错误码表; 标"表外"的是实测撞见的) ----
URL_BLOCK_CODES = {40054010, 304003, 40034104}       # 后两个表外
# 真频控(等一等有用). 11244/22009 表外
RATE_LIMIT_CODES = {40034100, 11244, 22009}
# 主动消息无权限: 要复查群状态
PROACTIVE_DENIED_CODES = {40034105}
# 被动凭据过期/无效/该事件不支持回复: 换主动或等下条消息
PASSIVE_EXPIRED_CODES = {40034128, 40034005, 304103,
                         40034024, 40034025, 40034026, 40034027}
# bot 在该群已无资格说话: 不重试, 但复查群状态
NOT_IN_GROUP_CODES = {40054003, 40034101}
MUTED_CODES = {40054002}
# 私聊: 不是好友 / 用户拒收. 私聊没有状态接口可复查, 不重试
PEER_REFUSED_CODES = {40054004, 40054013}
# 瞬时故障 / bot 短暂离线. 50055002/40054006/340067 是单聊表的
TRANSIENT_CODES = {50055001, 50055006, 40034004, 40054016,
                   50055002, 40054006, 340067}
# 消息本身有问题: 不复查也不重试. 后一行是单聊表的(召回/订阅按钮/输入中状态本服务不用)
BAD_MESSAGE_CODES = {304061, 340069, 305007, 304064, 304080,
                     40054007, 40054005, 40034006, 40034029,
                     40034106, 40034108, 40034109,
                     40054018, 40034122, 40034123, 304062, 50059}
# markdown 不被接受时降级为纯文本重发. 必须收全, 漏一个就整条发不出去
MARKDOWN_REJECT_CODES = {22006, 304004, 304036, 40034127,
                         40034008, 40034009, 40034010, 40034011, 40034124,
                         40034104, 40034021}          # 后两个表外
# 图文卡片被拒时改发文字. 表外实测: 封面平台拉不到 / 字段不合法 / 缺 card(HTTP 500);
# 别的 4xx 只要不是会话级错误也改发(见 _send_one)
CARD_REJECT_CODES = {40034141, 40011021, 305011}
FILE_TYPE_IMAGE, FILE_TYPE_VIDEO, FILE_TYPE_VOICE, FILE_TYPE_FILE = 1, 2, 3, 4

_MD_BLOCK_START = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+\.\s|>\s|\||```|(?:\*\s*){3,}$)")


# markdown 图片: ![说明](url) 或 QQ 的 ![说明 #123px #456px](url)
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*(https?://[^\s)]+)\s*\)")


def extract_md_images(text: str) -> tuple[str, list[str]]:
    """把 markdown 图片抽成原生富媒体图片(不带 #宽px #高px 渲染会坏), 正文只留说明."""
    urls: list[str] = []

    def replace(match: re.Match) -> str:
        urls.append(match.group(2))
        alt = re.sub(r"#\d+px", "", match.group(1)).strip()
        return alt

    return _MD_IMAGE_RE.sub(replace, text), urls


def to_markdown_content(text: str) -> str:
    """给非块级行补两个尾空格(硬换行), 让插件的单换行在 markdown 里生效; 代码块不动."""
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence or not stripped:
            out.append(line)
            continue
        next_line = lines[index + 1].strip() if index + 1 < len(lines) else ""
        needs_break = (
            next_line
            and not _MD_BLOCK_START.match(lines[index + 1])
            and not _MD_BLOCK_START.match(line)
            and not line.endswith("  ")
        )
        out.append(line + "  " if needs_break else line)
    return "\n".join(out)


def strip_markdown(text: str) -> str:
    """降级用: 去掉 markdown 装饰, 保留可读纯文本."""
    text = re.sub(r"^```.*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 \2", text)   # 图片 -> 说明+链接
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)    # 链接 -> 文字+链接
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:\*\s*){3,}$", "———", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE)      # 去硬换行尾空格
    return text.strip()


# 转发摊平成多条时附的提示, 引用它即整批撤回. 非群管时只能撤 2 分钟内的(40064004)
RECALL_HINT_MARK = "可回复本消息一次性撤回"   # recall.py 按这段认, 两种文案都含
RECALL_HINT = "为避免刷屏，可回复本消息一次性撤回"
RECALL_HINT_LIMITED = "为避免刷屏，2分钟内可回复本消息一次性撤回"


# 戳一戳: 平台不支持, 改发一条 @对方 的消息
POKE_TEXT = " 戳了戳你"


AT_ALL_TAG = "<qqbot-at-everyone />"
AT_ALL_TEXT = "@全体成员"
# 转图时要摘出来单发的 at 标签(只在 markdown 里生效, 塞不进图)
_AT_USER_TAG_RE = re.compile(r'<qqbot-at-user\s+id="[^"]*"\s*/?>|<qqbot-at-everyone\s*/?>')
_AT_ALL_TAG_RE = re.compile(r"<qqbot-at-everyone\s*/?>")
# 会话级错误: 换个消息形态也照样失败(@全体换文本、卡片换文字都不该重发)
_SESSION_CODES = (URL_BLOCK_CODES | RATE_LIMIT_CODES | PROACTIVE_DENIED_CODES
                  | PASSIVE_EXPIRED_CODES | NOT_IN_GROUP_CODES | MUTED_CODES
                  | PEER_REFUSED_CODES | TRANSIENT_CODES)


def _beijing_day() -> str:
    # 平台说「每天 1 次(自然日)」, 按北京时间算
    return datetime.now(BEIJING).strftime("%Y-%m-%d")


# 指令标签(input 填入输入框 / enter 直接发送), 只在 markdown 里生效
_CMD_TAG_RE = re.compile(r"<qqbot-cmd-(?:input|enter)\b[^>]*>", re.IGNORECASE)
# 文本改写要跳过标签: 带捕获组切开, 奇数段即标签
_CMD_TAG_SPLIT_RE = re.compile(f"({_CMD_TAG_RE.pattern})", re.IGNORECASE)
_CMD_TAG_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_CMD_ENTER_NAME_RE = re.compile(r"<qqbot-cmd-enter\b", re.IGNORECASE)


def downgrade_cmd_enter(text: str) -> str:
    """群聊里把回车指令换成参数指令(群聊带 enter 整条被 40034106 拒); 单聊不动."""
    return _CMD_ENTER_NAME_RE.sub("<qqbot-cmd-input", text)


def strip_cmd_tags(text: str) -> str:
    """降级用: 指令标签 -> 指令原文(优先 text 而非 show, 值是 urlencode 过的)."""
    def replace(match: re.Match) -> str:
        attrs = dict(_CMD_TAG_ATTR_RE.findall(match.group(0)))
        return unquote(attrs.get("text") or attrs.get("show") or "")

    return _CMD_TAG_RE.sub(replace, text)


# 已有的 markdown 链接: 角标要补进它的显示文字里
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")

def prepare_image_text(body: str) -> tuple[str, list[str]]:
    """转图前的链接处理: 返回 (渲染用 md 源, 链接 URL 列表).

    裸 URL 一律收成 [请点击 域名](url); 链接编角标 ⟦n⟧(渲染成 <sup>), 与单发的
    链接消息里的 [n] 对应, 同一 URL 共用一个.
    """
    links = extract_links(body)
    index = {url: i + 1 for i, url in enumerate(links)}

    def mark(url: str) -> str:
        n = index.get(url)
        return f"⟦{n}⟧" if n else ""

    def replace_md(match: re.Match) -> str:
        label, url = match.group(1), match.group(2)
        return f"[{label}{mark(url)}]({url})"

    def replace_bare(match: re.Match) -> str:
        url, tail = _split_url_tail(match.group(0))
        if not urlparse(url).hostname:
            return match.group(0)
        return f"[请点击 {domain_label(url)}{mark(url)}]({url}){tail}"

    source = _MD_LINK_RE.sub(replace_md, body)
    source = _SHORTEN_URL_RE.sub(replace_bare, source)
    return source, links


def recall_hint_for(bot_role: str) -> str:
    return (RECALL_HINT if bot_role in ("admin", "owner")
            else RECALL_HINT_LIMITED)


class SendError(Exception):
    def __init__(self, retcode: int, message: str):
        self.retcode = retcode
        self.message = message
        super().__init__(message)


# 长链接一律收成 [请点击 域名主体](url), 不设域名/路径白名单
SHORTEN_URL_MIN = 45
# (?<!\]\(): 已是 markdown 链接目标的 URL 不能再包一层
_SHORTEN_URL_RE = re.compile(r"(?<!\]\()https?://[^\s<>\"')\]]+")
# URL 末尾粘着的标点不算链接
_URL_TAIL_PUNCT = "。，、；：！？…）】》」』.,;:!?"
# 公共后缀里带两段的(com.cn 这类), 取主体时要多退一级
_MULTI_TLD = {"com", "net", "org", "gov", "edu", "co", "ac"}
_IP_HOST_RE = re.compile(r"[\d.]+")


def domain_label(url: str) -> str:
    """从 URL 取"域名主体"当显示文字: bot.example.com -> example."""
    host = urlparse(url).hostname or ""
    if not host:
        return "链接"
    # IP 原样显示
    if ":" in host or _IP_HOST_RE.fullmatch(host):
        return host
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    # 去掉 TLD; 若是 xxx.com.cn 这类再退一级
    if len(parts) >= 3 and parts[-2] in _MULTI_TLD:
        return parts[-3]
    return parts[-2]


def _split_url_tail(url: str) -> tuple[str, str]:
    """句末标点不算 URL 的一部分, 摘出来放回链接后面."""
    tail = ""
    while url and url[-1] in _URL_TAIL_PUNCT:
        url, tail = url[:-1], url[-1] + tail
    return url, tail


def shorten_urls_md(text: str, min_len: int = SHORTEN_URL_MIN) -> str:
    """把长链接换成 [请点击 域名主体](url) 的 markdown 形式.

    一条消息里要么全换要么全不换(按最长那条判), 免得参差. 只用于 markdown 版.
    """
    if not any(len(_split_url_tail(m.group(0))[0]) >= min_len
               for m in _SHORTEN_URL_RE.finditer(text)):
        return text

    def replace(match: re.Match) -> str:
        url, tail = _split_url_tail(match.group(0))
        if not urlparse(url).hostname:
            return match.group(0)       # 残缺 URL(裸 scheme 之类)不硬包
        return f"[请点击 {domain_label(url)}]({url}){tail}"

    # 指令标签属性里的 URL 被包成 [x](y) 会让标签失效, 整段跳过
    parts = _CMD_TAG_SPLIT_RE.split(text)
    return "".join(part if index % 2 else _SHORTEN_URL_RE.sub(replace, part)
                   for index, part in enumerate(parts))


# 兑换码类内容要能复制: 命中即绕开转网页与转图, 以纯文本发. 简繁都收
_COPY_CODE_RE = re.compile(r"兑换码|兌換碼|礼包码|禮包碼|激活码|激活碼|启用码|啟用碼|CDK",
                           re.IGNORECASE)


def _scan_segments(segments, found: list[str], media: list[bool]) -> None:
    """递归收集段里的可见文字与"有没有媒体"(嵌套节点也算)."""
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        kind = str(seg.get("type") or "text")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "text":
            found.append(str(data.get("text") or ""))
        elif kind in _MEDIA_SEG_TYPES:
            media.append(True)
        elif isinstance(data.get("content"), list):
            _scan_segments(data["content"], found, media)


def forward_has_copy_codes(nodes: list[dict]) -> bool:
    """不含媒体且有兑换码类关键词的转发: 摊平留在群里给人复制, 别进网页."""
    texts: list[str] = []
    media: list[bool] = []
    for node in nodes:
        _scan_segments(node.get("segments"), texts, media)
    return not media and bool(_COPY_CODE_RE.search("\n".join(texts)))


def _defang_urls(text: str) -> str:
    return re.sub(
        r"(https?://\S+|[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?:/\S*)?)",
        lambda m: m.group(0).replace(".", "．"),
        text,
    )


class MessagePlan:
    """拆解后的发送计划. items 保序(图文配对依赖); 文本同时维护 plain 与 md 两版."""

    def __init__(self) -> None:
        self.items: list[dict] = []  # {"kind":"text"|"media"|"card", ...}
        self.prefer_reply_mid: int | None = None
        self.degraded: list[str] = []
        # 合并转发的节点先攒着: 能建网页就整批换成一条链接, 建不了才摊平
        self.forward_nodes: list[dict] = []
        # 原始 file 字段 -> 本地媒体引用; 落库时替换 base64(否则被截断成残串)
        self.file_urls: dict[str, str] = {}
        # at all 能否用真 @全体(群聊 + markdown + bot 群管 + 当天没用过)
        self.at_all_ok = False
        # 发给谁: 卡片封面要传到这个群换 CDN 直链
        self.chat_type = ""
        self.peer_openid = ""
        # 摊平的转发节点里卡片不会单独成条, 不必做封面
        self.allow_cards = True

    def add_text(self, plain: str, md: str | None = None) -> None:
        if not plain and not md:
            return
        md = plain if md is None else md
        if self.items and self.items[-1]["kind"] == "text":
            self.items[-1]["plain"] += plain
            self.items[-1]["md"] += md
        else:
            self.items.append({"kind": "text", "plain": plain, "md": md,
                               "has_at": False})

    def add_at(self, openid: str, display: str) -> None:
        self.add_text(f"@{display} ", f'<qqbot-at-user id="{openid}" /> ')
        self.items[-1]["has_at"] = True

    def add_at_all(self) -> None:
        self.add_text(f"{AT_ALL_TEXT} ", f"{AT_ALL_TAG} ")
        self.items[-1]["has_at"] = True

    def add_media(self, file_type: int, url: str, name: str = "",
                  token: str | None = None) -> None:
        # token 非空 = 文件在本地媒体库, 可走分片直传
        self.items.append({"kind": "media", "file_type": file_type, "url": url,
                           "name": name, "token": token})

    def add_card(self, card: dict, plain: str, md: str | None = None) -> None:
        """图文卡片单独成一条; plain/md 是卡片被拒时改发的文字."""
        self.items.append({"kind": "card", "card": card, "plain": plain,
                           "md": md or plain})

    @property
    def plain_text(self) -> str:
        return "".join(i["plain"] for i in self.items
                       if i["kind"] == "text").strip()

    @property
    def media(self) -> list[tuple[int, str, str]]:
        return [(i["file_type"], i["url"], i.get("name", ""))
                for i in self.items if i["kind"] == "media"]

    def is_empty(self) -> bool:
        return (not self.plain_text and not self.media
                and not any(i["kind"] == "card" for i in self.items))


def merge_text_items(first: dict | None, second: dict) -> dict:
    if first is None:
        return dict(second)
    return {
        "kind": "text",
        "plain": first["plain"] + second["plain"],
        "md": first["md"] + second["md"],
        "has_at": first["has_at"] or second["has_at"],
    }


_MEDIA_SEG_TYPES = ("image", "mface", "record", "video", "file")


def _swap_media_urls(segments: list[dict], file_urls: dict[str, str]) -> list[dict]:
    """落库与自身消息回报前把媒体段的 file 换成本地媒体引用(公网 URL 或 media://).

    base64 直接落库会被 sanitize_segments 截断, get_msg / 引用回查就读不回图; 回报带整包
    base64 一条就能上十几 MB, 超过后端 ws 的单帧上限会被断线. 没对上的由 _stash_inline_media 兜底.
    file_urls 里记成空串的(base64 坏了)也照换, 不带出去.
    """
    if not file_urls:
        return segments
    out: list[dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        kind = seg.get("type")
        data = dict(seg.get("data") or {})
        new = {**seg, "data": data}
        if kind in _MEDIA_SEG_TYPES:
            url = _mapped(file_urls, data.get("file"), data.get("url"))
            if url is not None:
                data["file"] = url
                data["url"] = url
        elif kind in ("share", "music"):
            # 卡片封面: base64 换成给平台的地址
            url = _mapped(file_urls, data.get("image"))
            if url is not None:
                data["image"] = url
        else:
            for key, nested in _nested_messages(seg, data):
                swapped = _swap_media_urls(nested, file_urls)
                if key == "seg.message":
                    new["message"] = swapped
                else:
                    data[key] = swapped
        out.append(new)
    return out


def _mapped(file_urls: dict[str, str], *values) -> str | None:
    for value in values:
        if isinstance(value, str) and value in file_urls:
            return file_urls[value]
    return None


def _nested_messages(seg: dict, data: dict) -> list[tuple[str, list]]:
    """段里嵌着的消息: node 的 data.content / data.message(单个 dict 补成数组),
    以及入库形节点(无 type, 顶层 sender + message, 只出现在 node 内层)."""
    found = []
    for key in ("content", "message"):
        value = data.get(key)
        if isinstance(value, (list, dict)):
            found.append((key, value if isinstance(value, list) else [value]))
    if seg.get("type") is None and isinstance(seg.get("message"), list):
        found.append(("seg.message", seg["message"]))
    return found


def _inline_media(segments: list) -> list[str]:
    """消息里所有 base64:// 媒体值(含 node 内层、卡片封面), 口径同 _swap_media_urls."""
    found: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind = seg.get("type")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind in _MEDIA_SEG_TYPES:
            values = [data.get("file"), data.get("url")]
        elif kind in ("share", "music"):
            values = [data.get("image")]
        else:
            values = []
            for _, nested in _nested_messages(seg, data):
                found.extend(_inline_media(nested))
        found.extend(v for v in values if isinstance(v, str) and v.startswith("base64://"))
    return found


def _page_address(data: dict, now: float) -> str:
    """建页后的媒体段 -> 落库与回报给的地址: CDN 直链补新鲜 rkey; 没 rkey 用还没过期的
    raw_url; 都没有给不带 rkey 的直链(网页/管理台读的时候补 rkey). 没有地址返回空串."""
    url = str(data.get("url") or "")
    if cdnkeys.is_cdn(url):
        rkey = cdnkeys.fresh(now).get(cdnkeys.appid_of(url))
        if rkey:
            return cdnkeys.with_rkey(url, rkey)
    raw_url = str(data.get("raw_url") or "")
    if raw_url and int(data.get("raw_until") or 0) > now:
        return raw_url
    return url if url.startswith(("http://", "https://")) else ""


def _note_page_media(before: list, after: list, file_urls: dict[str, str],
                     now: float) -> None:
    """建页前后的节点段逐个对上(_upload_for_page 保序保形): 原始 base64 / 本地文件 ->
    建页换来的地址, 落库与回报用(见 _swap_media_urls)."""
    if not isinstance(before, list) or not isinstance(after, list):
        return
    for old, new in zip(before, after):
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        old_data = old.get("data") if isinstance(old.get("data"), dict) else {}
        new_data = new.get("data") if isinstance(new.get("data"), dict) else {}
        if old.get("type") in _MEDIA_SEG_TYPES:
            for raw in (old_data.get("file"), old_data.get("url")):
                if not raw or not isinstance(raw, str) or raw.startswith(("http://", "https://")):
                    continue
                url = _page_address(new_data, now)
                if url:
                    file_urls[raw] = url
        else:
            _note_page_media(old_data.get("content"), new_data.get("content"), file_urls, now)


def describe_inbound(message, plan: "MessagePlan") -> str:
    """顶层段构成 + 降级清单, 给报错和日志用: "in: node×33; degraded: []"."""
    counts: dict[str, int] = {}
    for seg in normalize_message(message):
        kind = str(seg.get("type") or "text")
        counts[kind] = counts.get(kind, 0) + 1
    shape = ", ".join(f"{k}×{n}" for k, n in counts.items()) or "nothing"
    return f"in: {shape}; degraded: {plan.degraded}"


def group_items(items: list[dict]) -> list[dict]:
    """按原始顺序把文本配到图片上(规则见模块文档).

    返回 [{"text": item|None, "media": item|None, "text_first": bool}]
    """
    if not items:
        return []
    if not any(i["kind"] == "media" for i in items):
        merged = None
        for item in items:
            merged = merge_text_items(merged, item)
        return [{"text": merged, "media": None, "text_first": True}]

    groups: list[dict] = []
    if items[0]["kind"] == "text":
        pending: dict | None = None
        for item in items:
            if item["kind"] == "text":
                pending = merge_text_items(pending, item)
            else:
                groups.append({"text": pending, "media": item, "text_first": True})
                pending = None
        if pending is not None and pending["plain"].strip():
            groups.append({"text": pending, "media": None, "text_first": True})
    else:
        for item in items:
            if item["kind"] == "media":
                groups.append({"text": None, "media": item, "text_first": False})
            elif groups:
                groups[-1]["text"] = merge_text_items(groups[-1]["text"], item)
            else:
                groups.append({"text": item, "media": None, "text_first": True})
    return groups


class Sender:
    def __init__(self, bot: "BotInstance"):
        self.bot = bot
        self._chat_locks = [asyncio.Lock() for _ in range(512)]
        # 群 openid -> 最近一次用掉 @全体 的北京日期(重启丢了无妨, 最多多被拒一次)
        self._at_all_day: dict[str, str] = {}

    def _lock_for(self, chat_type: str, peer_openid: str) -> asyncio.Lock:
        """同一会话串行发送. 用固定数量的分片锁, 免得每会话一把锁只增不减."""
        index = hash((chat_type, peer_openid)) % len(self._chat_locks)
        return self._chat_locks[index]

    # ---------------- 计划构建 ----------------

    async def build_plan(self, message, chat_type: str,
                         peer_openid: str = "") -> MessagePlan:
        segments = normalize_message(message)
        plan = MessagePlan()
        plan.chat_type, plan.peer_openid = chat_type, peer_openid
        if chat_type == "group" and any(
                isinstance(seg, dict) and seg.get("type") == "at"
                and str((seg.get("data") or {}).get("qq", "")) == "all"
                for seg in segments):
            plan.at_all_ok = await self.at_all_remain(peer_openid) > 0
        for seg in segments:
            await self._plan_segment(plan, seg, chat_type)
        if plan.forward_nodes:
            await self._plan_forward(plan, chat_type, peer_openid)
        return plan

    async def _plan_segment(self, plan: MessagePlan, seg: dict, chat_type: str) -> None:
        seg_type = seg.get("type", "text")
        data = seg.get("data") or {}
        media = self.bot.media

        if seg_type == "text":
            raw_text = str(data.get("text", ""))
            # markdown 版交给 text_out 插件改写
            shortened = await plugins.pipe(
                "text_out", HookContext(self.bot, chat_type), raw_text)
            # 只有真被改过才分叉 md 版本, 否则保持 md==plain 免得误判 markdown
            plan.add_text(raw_text,
                          shortened if shortened != raw_text else None)
        elif seg_type == "reply":
            try:
                plan.prefer_reply_mid = int(data.get("id", 0))
            except (TypeError, ValueError):
                pass
        elif seg_type == "at":
            qq = str(data.get("qq", ""))
            if qq == "all":
                if plan.at_all_ok:
                    plan.add_at_all()
                else:
                    plan.add_text(f"{AT_ALL_TEXT} ")
                return
            openid = None
            try:
                entry = self.bot.idmap.lookup_virtual(int(qq))
                if entry and entry.kind == "user" and entry.bot_appid == self.bot.appid:
                    openid = entry.openid
            except (TypeError, ValueError):
                pass
            # <qqbot-at-user/> 仅群聊场景支持(单聊无 @ 语义)
            if openid and chat_type == "group" and self.bot.markdown_enabled:
                plan.add_at(openid, data.get("name") or await self.bot.display_name(qq))
            else:
                name = data.get("name") or await self.bot.display_name(qq)
                plan.add_text(f"@{name} ")
        elif seg_type in ("image", "mface"):
            raw = str(data.get("file") or data.get("url") or "")
            url, token = media.ingest_file_field(raw)
            if url is None and data.get("url"):
                url = str(data["url"])
            if url:
                plan.file_urls[raw] = url
                plan.add_media(FILE_TYPE_IMAGE, url, token=token)
            else:
                plan.degraded.append("image")
                plan.add_text("[图片]")
        elif seg_type == "record":
            raw = str(data.get("file") or data.get("url") or "")
            url, token = await self._prepare_voice(raw)
            if url:
                plan.file_urls[raw] = url
                plan.add_media(FILE_TYPE_VOICE, url, token=token)
            else:
                plan.degraded.append("record")
                plan.add_text("[语音]")
        elif seg_type == "video":
            raw = str(data.get("file") or data.get("url") or "")
            url, token = media.ingest_file_field(raw)
            if url:
                plan.file_urls[raw] = url
                plan.add_media(FILE_TYPE_VIDEO, url, token=token)
            else:
                plan.degraded.append("video")
                plan.add_text("[视频]")
        elif seg_type == "file":
            name = str(data.get("name") or data.get("file_name") or "")
            raw = str(data.get("file") or data.get("url") or "")
            url, token = media.ingest_file_field(raw, name_hint=name)
            if url:
                plan.file_urls[raw] = url
                plan.add_media(FILE_TYPE_FILE, url, name=name, token=token)
            else:
                plan.degraded.append("file")
                plan.add_text(f"[文件]{name or '文件'}")
        elif seg_type == "node":
            plan.forward_nodes.append(seg)
        elif seg_type == "forward":
            # 转发原样再发: 内容在 forwards 表里(入站时存的)
            nodes = await self._stored_forward_nodes(str(data.get("id") or ""))
            if nodes:
                plan.forward_nodes.extend(nodes)
            else:
                plan.degraded.append("forward")
                plan.add_text("[聊天记录]")
        elif seg_type in ("music", "share"):
            await self._plan_link_card(plan, seg_type, data)
        elif seg_type in ("json", "xml"):
            text, url = _extract_card_text(data)
            if not url:
                plan.add_text(text)
                return
            # 卡片带跳转链接 -> markdown 链接(md 关闭/被拒时降级为 文本 url)
            line = self._linkify(text, url) + "\n"
            fields = _ark_card_fields(data) if seg_type == "json" else None
            card = (await self._link_card(plan, *fields)
                    if fields and plan.allow_cards else None)
            if card:
                plan.add_card(card, line)
            else:
                plan.add_text(line)
        elif seg_type == "face":
            # emoji, 拿不到就用官方名字([赞])
            plan.add_text(emoji_of(data.get("id")))
        elif seg_type == "poke":
            # 与 poke 动作同口径: @对方 + 一句话
            target = str(data.get("qq") or data.get("id") or "")
            if target:
                await self._plan_segment(
                    plan, {"type": "at", "data": {"qq": target}}, chat_type)
                plan.add_text(POKE_TEXT)
            else:
                plan.add_text("[戳一戳]")
        else:
            # 未知段: 丢弃并记录, 保持消息可发
            plan.degraded.append(seg_type)
            logger.info("drop unsupported outbound segment type=%s", seg_type)

    async def at_all_remain(self, group_openid: str) -> int:
        """今天还能真 @全体 几次: markdown 开着、bot 是群管且今天没用过才是 1."""
        if not group_openid or not self.bot.markdown_enabled:
            return 0
        if self._at_all_day.get(group_openid) == _beijing_day():
            return 0
        role = await self.bot.group_role(group_openid)
        return 1 if role in ("admin", "owner") else 0

    def _linkify(self, label: str, url: str) -> str:
        """markdown 链接; bot 关闭 md 时退回 '文本 url'."""
        label = re.sub(r"[\[\]()]", " ", label).strip()[:60] or "链接"
        if self.bot.markdown_enabled:
            return f"[{label}]({url})"
        return f"{label} {url}"

    async def _plan_link_card(self, plan: MessagePlan, seg_type: str, data: dict) -> None:
        """music/share 段 -> 图文卡片(有 https 跳转和图时), 否则 markdown 链接;
        只有 id 的平台音乐按模板拼 url(没有图, 发链接)."""
        subtype = str(data.get("type") or "")
        title = str(data.get("title") or "")
        content = str(data.get("content") or "")
        url = str(data.get("url") or "")
        if not url and seg_type == "music" and data.get("id"):
            template = _MUSIC_URL_TEMPLATES.get(subtype)
            if template:
                url = template.format(id=data["id"])
        icon = "🎵" if seg_type == "music" else "🔗"
        label = title or ("音乐" if seg_type == "music" else "链接")
        if url:
            line = f"{icon} " + self._linkify(label, url)
        else:
            plan.degraded.append(seg_type)
            line = f"{icon}{label}"
        if content and content != label:
            line += f" — {content}"
        image = data.get("image") or data.get("cover")
        card = (await self._link_card(plan, label, content, url, image)
                if url and plan.allow_cards else None)
        if card:
            plan.add_card(card, line + "\n")
        else:
            plan.add_text(line + "\n")

    async def _link_card(self, plan: MessagePlan, title: str, desc: str, url: str,
                         image) -> dict | None:
        """链接类卡片. image: http(s) 直接交给平台拉; base64:// / file:// / 本地路径
        处理成封面后给平台一个能拉的地址(见 _cover_url), 给不出就不发卡片."""
        image = str(image or "").strip()
        if image.startswith("//"):
            image = "https:" + image
        if not str(url or "").startswith("https://") or not image:
            return None
        if image.startswith(("http://", "https://")):
            return cards.build(title, desc, url, image)
        blob, _ = await asyncio.to_thread(self.bot.media.load_bytes, image)
        jpeg = blob and await asyncio.to_thread(cards.make_cover, blob)
        pic = jpeg and await self._cover_url(plan, jpeg)
        if not pic:
            return None
        plan.file_urls[image] = pic             # 落库/回报别带 base64(见 _swap_media_urls)
        return cards.build(title, desc, url, pic)

    async def _cover_url(self, plan: MessagePlan, jpeg: bytes, base: str | None = None) -> str:
        """封面给平台拉的地址: 群聊有新鲜 rkey 就传给平台用 CDN 直链(实测稳), 否则经
        转发网页地址(base, 不给就按配置取)的 /f/c/ 自托管(隧道慢时平台会拉超时); 都不行返回空串."""
        rkey = cdnkeys.fresh().get("1407") if plan.chat_type == "group" else ""
        if rkey and plan.peer_openid:
            try:
                done = await self.bot.uploader.upload_media(
                    "group", plan.peer_openid, FILE_TYPE_IMAGE, jpeg, "cover.jpg",
                    need_uuid=True)
                if done["file_uuid"]:
                    return cdnkeys.with_rkey(cdnkeys.cdn_url("1407", done["file_uuid"]), rkey)
            except Exception as exc:
                logger.info("[%s] 封面上传失败, 改自托管: %r", self.bot.appid, exc)
        if base is None:
            config = getattr(getattr(self.bot, "manager", None), "config", None)
            base = forwardpage.page_base(config)[0]
        return cards.host(self.bot.media, base, jpeg) if base else ""

    def _option(self, name: str, default):
        return option(self.bot, name, default)

    async def _stored_forward_nodes(self, fid: str) -> list[dict]:
        if not fid:
            return []
        row = await self.bot.db.fetchone(
            "SELECT nodes FROM forwards WHERE id=? AND bot_appid=?",
            (fid, self.bot.appid))
        if row is None:
            return []
        try:
            nodes = json.loads(row["nodes"])
        except ValueError:
            return []
        return nodes if isinstance(nodes, list) else []

    async def _expand_nested(self, segments: list, depth: int = 0) -> list[dict]:
        """递归展开嵌套的 node / forward(限 NEST_MAX_DEPTH 层, 防环), 统一成 node 段并填头像."""
        out: list[dict] = []
        deeper = depth < forwardpage.NEST_MAX_DEPTH
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            kind = seg.get("type")
            data = dict(seg["data"]) if isinstance(seg.get("data"), dict) else None
            if kind == "forward" and data is not None:
                inner = (await self._stored_forward_nodes(str(data.get("id") or ""))
                         if deeper else [])
                if inner:
                    out.extend(await self._expand_nested(inner, depth + 1))
                else:
                    out.append(seg)         # 查不到: 页面上只剩一个胶囊
                continue
            if kind == "node" and data is not None:
                if isinstance(data.get("content"), dict):
                    # 裸 MessageSegment 会编成单个对象, 补成数组(下游只认 list)
                    data["content"] = [data["content"]]
                if isinstance(data.get("content"), list) and deeper:
                    data["content"] = await self._expand_nested(data["content"], depth + 1)
                try:
                    uid = int(data.get("user_id") or data.get("uin") or 0)
                except (TypeError, ValueError):
                    uid = 0
                data["avatar"] = self._avatar_of(uid)
                if not (data.get("nickname") or data.get("name")):
                    # 只给了 uin 不给名字: 反查出来
                    data["nickname"] = self._display_of(uid)
                out.append({**seg, "data": data})
                continue
            if kind is None and isinstance(seg.get("message"), list):
                sender = seg.get("sender") if isinstance(seg.get("sender"), dict) else {}
                try:
                    uid = int(sender.get("user_id") or 0)
                except (TypeError, ValueError):
                    uid = 0
                out.append({"type": "node", "data": {
                    "nickname": str(sender.get("nickname") or ""),
                    "user_id": uid, "time": seg.get("time") or 0,
                    "avatar": self._avatar_of(uid),
                    "content": (await self._expand_nested(seg["message"], depth + 1)
                                if deeper else seg["message"]),
                }})
                continue
            out.append(seg)
        return out

    async def _plan_forward(self, plan: MessagePlan, chat_type: str,
                            peer_openid: str = "") -> None:
        """转发节点交给 forward_out 插件, 没人接手就摊平成正文 + 逐个媒体."""
        nodes = forwardpage.normalize_nodes(await self._expand_nested(plan.forward_nodes))
        if not nodes:
            # 节点全部解析不出: 记错误并降级发一行, 别让 plan 变空后报不相干的 1400
            count = len(plan.forward_nodes)
            logger.error("[%s] 合并转发 %d 个节点全部无法解析, 降级为一行文本; 首个: %s",
                         self.bot.appid, count,
                         json.dumps(plan.forward_nodes[0], ensure_ascii=False)[:300])
            plan.degraded.append("forward")
            plan.add_text(f"[聊天记录] {count} 个节点无法解析")
            return
        handled = await plugins.first(
            "forward_out", HookContext(self.bot, chat_type, peer_openid, plan), nodes)
        if isinstance(handled, dict):
            if handled.get("card"):
                plan.add_card(handled["card"], handled["plain"], handled.get("md"))
            else:
                plan.add_text(handled["plain"], handled.get("md") or None)
            return
        if handled is not None:
            plain, md = handled if isinstance(handled, tuple) else (handled, handled)
            plan.add_text(plain, md)
            return
        # 没人接手: 摊平成正文 + 逐个媒体
        for node in nodes:
            await self._plan_forward_node(
                plan, {"content": node["segments"]}, chat_type)

    async def build_forward_page(self, plan: MessagePlan, nodes: list[dict],
                                 peer_openid: str, base: str,
                                 jump: bool = False) -> tuple[str, str]:
        """转发节点建成一页网页, 返回要发的 (纯文本, markdown); jump: 链接经腾讯文档跳转.

        媒体一张传不上就整页不建, 抛 SendError(1400/1500).
        """
        url = await self.create_forward_page(plan, nodes, peer_openid, base)
        return self._forward_link_text(nodes, cards.docs_jump(url) if jump else url)

    def _forward_link_text(self, nodes: list[dict], url: str) -> tuple[str, str]:
        head = f"📄 {forwardpage.summarize(nodes)} "
        return head + f"点击查看 {url}", head + self._linkify("点击查看", url)

    async def build_forward_card(self, plan: MessagePlan, nodes: list[dict],
                                 base: str, jump: bool = False) -> dict:
        """转发节点建成本服务的网页, 返回 {"card", "plain", "md"} 给 forward_out(群聊).

        卡片见 _forward_card; jump: 卡片和链接都经腾讯文档的外链跳转页打开(见 cards.docs_jump).
        做不成卡片时 card 为 None, 发 plain/md 链接.
        """
        # 建页会把节点里的媒体换成 CDN 地址, 先把原图的来路拿到手
        candidates = cards.first_images(nodes)
        url = await self.create_forward_page(plan, nodes, plan.peer_openid, base)
        return await self._forward_card(
            plan, nodes, candidates, cards.docs_jump(url) if jump else url, base)

    async def _forward_card(self, plan: MessagePlan, nodes: list[dict], candidates: list[dict],
                            url: str, base: str) -> dict:
        """「聊天记录」卡片: 简介是条数与图片数, 封面取第一张图(长图只取顶部), 没图就把前几条
        文字渲染成图, 封面地址见 _cover_url. 做不成卡片时 card 为 None, 发 plain/md 链接."""
        plain, md = self._forward_link_text(nodes, url)
        if not url.startswith("https://"):      # 卡片跳转只认 https, 别白做封面
            return {"card": None, "plain": plain, "md": md}
        jpeg = None
        for data in candidates:
            blob = await self._image_bytes(data)
            jpeg = blob and await asyncio.to_thread(cards.make_cover, blob)
            if jpeg:
                break
        preview = "" if jpeg else cards.chat_preview(nodes)
        if preview:
            png = await textimg.render_text_image(preview)
            jpeg = png and await asyncio.to_thread(cards.make_cover, png, text=True)
        pic = jpeg and await self._cover_url(plan, jpeg, base)
        card = cards.build("聊天记录", forwardpage.summarize(nodes), url, pic) if pic else None
        if card is None:
            logger.info("[%s] 聊天记录做不出卡片封面, 发链接", self.bot.appid)
        return {"card": card, "plain": plain, "md": md}

    async def _image_bytes(self, data: dict) -> bytes | None:
        """图片段 -> 字节: QQ 直链换新 rkey 再拉, 外链直接拉, base64/本地读出来."""
        raw = str(data.get("url") or data.get("file") or "")
        if raw.startswith(("http://", "https://")):
            if cdnkeys.is_cdn(raw):
                rkey = cdnkeys.fresh().get(cdnkeys.appid_of(raw))
                if rkey:
                    raw = cdnkeys.with_rkey(raw, rkey)
            return await self._fetch_bytes(raw)
        if not raw:
            return None
        blob, _ = await asyncio.to_thread(self.bot.media.load_bytes, raw)
        return blob

    async def _fetch_bytes(self, url: str, limit: int = COVER_FETCH_MAX) -> bytes | None:
        try:
            async with self.bot.http.get(
                    url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.content.read(limit + 1)
        except Exception as exc:
            logger.info("[%s] 拉图失败 %s: %r", self.bot.appid, url[:80], exc)
            return None
        return data if len(data) <= limit else None

    async def create_forward_page(self, plan: MessagePlan, nodes: list[dict],
                                  peer_openid: str, base: str) -> str:
        """转发节点建成一页网页(媒体上传给平台换 CDN 地址), 返回网页地址."""
        for node in nodes:
            node["avatar"] = self._avatar_of(node.pop("uid", 0))
        # token 按上传前的内容算: 同一份转发再发时页面已在, 不再上传
        token = forwardpage.page_token(self.bot.appid, nodes)
        before = [node["segments"] for node in nodes]
        row = await self.bot.db.fetchone(
            "SELECT nodes FROM forward_pages WHERE token=?", (token,))
        if row is not None:
            # 页面已在: 原始值 -> 地址按存下的节点对回, 落库与回报跟第一次发一样
            try:
                stored = json.loads(row["nodes"])
            except ValueError:
                stored = []
            after = [node.get("segments") if isinstance(node, dict) else None
                     for node in (stored if isinstance(stored, list) else [])]
        else:
            # 所有节点一起传, 并发由信号量卡
            sem = asyncio.Semaphore(PAGE_UPLOAD_CONCURRENCY)
            uploaded = await asyncio.gather(*(
                self._upload_for_page(node["segments"], peer_openid, sem)
                for node in nodes), return_exceptions=True)
            failed = [u for u in uploaded if isinstance(u, BaseException)]
            if failed:
                # 不建缺图的页: 页面按内容哈希复用, 存下去就一直缺
                exc = failed[0]
                if isinstance(exc, MediaTooLarge):
                    raise SendError(1400, str(exc)) from exc
                if isinstance(exc, SendError):
                    raise exc
                logger.warning("[%s] 转发网页媒体上传失败 %d/%d: %r", self.bot.appid,
                               len(failed), len(nodes), exc)
                raise SendError(1500, f"转发网页媒体上传失败: {exc}") from exc
            for node, segments in zip(nodes, uploaded):
                node["segments"] = segments
            after = list(uploaded)
            # OR IGNORE: ts 停在第一次(直链可用窗口按它算, 不能刷新)
            await self.bot.db.execute(
                "INSERT OR IGNORE INTO forward_pages (token, bot_appid, nodes, ts)"
                " VALUES (?,?,?,?)",
                (token, self.bot.appid, json.dumps(nodes, ensure_ascii=False),
                 int(time.time())),
            )
        now = time.time()
        for old, new in zip(before, after):
            _note_page_media(old, new, plan.file_urls, now)
        return f"{base.rstrip('/')}/f/{token}"

    def _avatar_of(self, virtual_id: int) -> str:
        """节点发送者头像: idmap 用户走 q.qlogo.cn/qqapp/{appid}/{openid}/100, bot 用配置里的;
        5~11 位当普通 QQ 号走 q.qlogo.cn/g?b=qq&nk=; 都不是退回本 bot 头像."""
        uid = int(virtual_id or 0)
        entry = self.bot.idmap.lookup_virtual(uid) if uid else None
        if entry is None:
            if 10000 <= uid < 10 ** 11 and not is_virtual_id(uid):
                return f"https://q.qlogo.cn/g?b=qq&nk={uid}&s=100"
            return str(self.bot.cfg.get("avatar") or "")
        if entry.kind == "bot":
            if entry.openid == self.bot.appid:
                return str(self.bot.cfg.get("avatar") or "")
            manager = getattr(self.bot, "manager", None)
            other = manager.get_bot(entry.openid) if manager else None
            return str(other.cfg.get("avatar") or "") if other else ""
        if entry.kind != "user":
            return ""
        return f"https://q.qlogo.cn/qqapp/{entry.bot_appid}/{entry.openid}/100"

    def _display_of(self, virtual_id: int) -> str:
        """节点发送者的显示名(uin 反查): bot 用配置里的名字, 用户用 idmap 记的昵称."""
        entry = self.bot.idmap.lookup_virtual(int(virtual_id or 0)) if virtual_id else None
        if entry is None:
            return ""
        if entry.kind == "bot":
            if entry.openid == self.bot.appid:
                return str(self.bot.cfg.get("name") or "")
            manager = getattr(self.bot, "manager", None)
            other = manager.get_bot(entry.openid) if manager else None
            return str(other.cfg.get("name") or "") if other else ""
        return str(entry.nickname or "")

    async def _upload_for_page(self, segments: list[dict], group_openid: str,
                               sem: asyncio.Semaphore) -> list[dict]:
        """节点里的媒体 -> QQ CDN 地址, 不托管媒体字节(见 forwardpage).

        - QQ 直链 / 外站链接: 原样(渲染时给 QQ 直链换新 rkey);
        - 本地/base64: 上传给平台(不发出去), 用 file_uuid 拼直链, raw_url(1 小时)兜底;
        - 语音(平台转 silk, 浏览器放不了)只留 raw_url; 文件只留名字. 失败直接抛.
        保序保形(只换段内 data), 落库与回报靠前后对位找地址(_note_page_media).
        """
        async def one(seg: dict) -> dict:
            data = dict(seg.get("data") or {})
            kind = seg.get("type")
            if kind in _MEDIA_SEG_TYPES:
                async with sem:
                    await self._page_media(kind, data, group_openid)
            elif isinstance(data.get("content"), list):
                data["content"] = await self._upload_for_page(
                    data["content"], group_openid, sem)
            return {**seg, "data": data}

        return list(await asyncio.gather(*(one(s) for s in segments)))

    async def _page_media(self, kind: str, data: dict, group_openid: str) -> None:
        raw = str(data.get("url") or data.get("file") or "")
        media = self.bot.media
        if raw.startswith(("http://", "https://")):
            data["url"] = data.get("url") or raw
            return
        data["url"] = data["file"] = ""
        file_type = {"image": FILE_TYPE_IMAGE, "mface": FILE_TYPE_IMAGE, "video": FILE_TYPE_VIDEO,
                     "record": FILE_TYPE_VOICE}.get(str(kind))
        blob, name = (await asyncio.to_thread(media.load_bytes, raw)
                      if raw and file_type else (None, ""))
        if blob is None:
            return
        done = await self.bot.uploader.upload_media(
            "group", group_openid, file_type, blob,
            str(data.get("name") or name or ""), need_uuid=True)
        appid = cdnkeys.APPID_BY_UPLOAD.get(("group", done["file_type"]))
        if appid and done["file_uuid"] and done["file_type"] != FILE_TYPE_VOICE:
            data["url"] = data["file"] = cdnkeys.cdn_url(appid, done["file_uuid"])
        if done["raw_url"]:
            data["raw_url"] = done["raw_url"]
            data["raw_until"] = done["raw_until"]
        if blob.startswith(b"GIF8") and not data.get("name"):
            data["name"] = "image.gif"          # 页面对 .gif 不取 spec 变体(会压成单帧)

    async def _plan_forward_node(self, plan: MessagePlan, data: dict, chat_type: str) -> None:
        """合并转发节点 -> 逐行正文(不带发送者名); 节点内媒体全部照发."""
        content = data.get("content")
        sub_plan = MessagePlan()
        sub_plan.allow_cards = False            # 卡片在这里只会变回链接文字
        for seg in normalize_message(content):
            await self._plan_segment(sub_plan, seg, chat_type)
        # 嵌套 node 就地继续摊平(排在本节点正文之后)
        for nested in forwardpage.normalize_nodes(sub_plan.forward_nodes):
            await self._plan_forward_node(sub_plan, {"content": nested["segments"]}, chat_type)
        text = sub_plan.plain_text
        if text:
            # md 版单独取, 保留已改写的链接
            md = "".join(i["md"] for i in sub_plan.items
                         if i["kind"] == "text").strip()
            plan.add_text(text + "\n", (md or text) + "\n")
        # 媒体全发, 不设上限
        for item in sub_plan.items:
            if item["kind"] == "media":
                plan.add_media(item["file_type"], item["url"],
                               item.get("name", ""), token=item.get("token"))
        # 节点内 base64 -> 媒体引用也要带回去, 落库与回报才不带整包(见 _swap_media_urls)
        plan.file_urls.update(sub_plan.file_urls)

    async def _prepare_voice(self, file_field: str) -> tuple[str | None, str | None]:
        """音频落本地并转 silk -> (引用 URL, 本地 token); 超过平台时长上限的截掉尾巴."""
        url, token = self.bot.media.ingest_file_field(file_field)
        if token is None and url is None:
            return None, None
        if token is None and url:
            # 外链音频: 拉回本地统一转码(平台对非 silk 外链兼容性不稳)
            src = await self._download_temp(url)
        else:
            src = self.bot.media.resolve(token)
        if src is None:
            return None, None
        try:
            import pilk  # noqa: PLC0415
            is_silk = src.read_bytes()[:16].startswith((b"\x02#!SILK_V3", b"#!SILK_V3"))
            if is_silk and pilk.get_duration(str(src)) <= VOICE_MAX_SECONDS * 1000:
                silk_token = token if token and src.suffix == ".silk" else \
                    self.bot.media.put_bytes(src.read_bytes(), ".silk")
                return self.bot.media.ref(silk_token), silk_token
            with tempfile.TemporaryDirectory() as tmp_dir:
                pcm = Path(tmp_dir) / "audio.pcm"
                silk = Path(tmp_dir) / "audio.silk"
                if is_silk:
                    # 太长的 silk: 解回 pcm 再截
                    await asyncio.to_thread(pilk.decode, str(src), str(pcm), pcm_rate=24000)
                    with open(pcm, "r+b") as fh:
                        fh.truncate(VOICE_MAX_SECONDS * 24000 * 2)
                else:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["ffmpeg", "-y", "-i", str(src), "-t", str(VOICE_MAX_SECONDS),
                         "-f", "s16le", "-ar", "24000", "-ac", "1", str(pcm)],
                        capture_output=True, timeout=60,
                    )
                    if proc.returncode != 0:
                        logger.warning("ffmpeg decode failed: %s", proc.stderr[-300:])
                        return None, None
                if pcm.stat().st_size >= VOICE_MAX_SECONDS * 24000 * 2:
                    logger.info("[%s] 语音超过 %d 秒上限, 截掉尾巴发送",
                                self.bot.appid, VOICE_MAX_SECONDS)
                await asyncio.to_thread(
                    pilk.encode, str(pcm), str(silk), pcm_rate=24000, tencent=True
                )
                silk_token = self.bot.media.put_bytes(silk.read_bytes(), ".silk")
            return self.bot.media.ref(silk_token), silk_token
        except Exception as exc:
            logger.warning("voice convert failed: %s", exc)
            return None, None

    async def _download_temp(self, url: str | None) -> Path | None:
        if not url:
            return None
        try:
            async with self.bot.http.get(url) as resp:
                data = await resp.read()
            token = self.bot.media.put_bytes(data)
            return self.bot.media.resolve(token)
        except Exception:
            return None

    # ---------------- 发送 ----------------

    async def send(self, chat_type: str, peer_openid: str, message,
                   text_image: bool = True) -> int:
        """发送一条 OneBot 消息, 返回首条物理消息的 mid.

        text_image=False 时不跑 payload_out 插件(内置指令要保持原样).
        """
        message = await plugins.pipe(
            "message_out", HookContext(self.bot, chat_type, peer_openid),
            normalize_message(message))
        plan = await self.build_plan(message, chat_type, peer_openid)
        if plan.is_empty():
            # 带上段构成和降级清单, 插件才分得清原因
            detail = describe_inbound(message, plan)
            logger.warning("[%s] 转换后为空: %s", self.bot.appid, detail)
            raise SendError(1400, f"empty message after conversion ({detail})")
        is_forward = any(
            isinstance(seg, dict) and seg.get("type") in ("forward", "node")
            for seg in normalize_message(message))

        # 出站合规闸: 不合规群一律不发(与入站静默同一口径)
        if chat_type == "group":
            state = await self.bot.group_state(peer_openid, reason="outbound")
            if not state.compliant:
                raise SendError(1503, "该群未开启主动消息+全量消息, 出站静默")

        peer_virtual = await self.bot.virtual_for_peer(chat_type, peer_openid)
        # reply 段 -> message_reference, 须属同一会话. 只认 msg_idx(REFIDX_xxx),
        # 传消息 id 平台返回 200 但不显示引用.
        reference: dict | None = None
        if plan.prefer_reply_mid is not None:
            ref_record = await self.bot.store.get_by_mid(plan.prefer_reply_mid)
            if (
                ref_record
                and ref_record.get("msg_idx")
                and ref_record["bot_appid"] == self.bot.appid
                and ref_record["chat_type"] == chat_type
                and ref_record["peer_openid"] == peer_openid
            ):
                reference = {"message_id": ref_record["msg_idx"]}

        # markdown 图片改走原生富媒体, 插到该段文字之后
        items: list[dict] = []
        for item in plan.items:
            if item["kind"] != "text":
                items.append(item)
                continue
            plain, urls = extract_md_images(item["plain"])
            md, _ = extract_md_images(item["md"])
            if plain.strip():
                items.append({**item, "plain": plain, "md": md})
            for url in urls:
                items.append({"kind": "media", "file_type": FILE_TYPE_IMAGE,
                              "url": url})
        await self._stash_inline_media(normalize_message(message), plan.file_urls)

        async with self._lock_for(chat_type, peer_openid):
            payloads: list[dict] = []
            # 卡片单独成条, 把前后的文字/图片隔成各自配对
            for is_card, run in itertools.groupby(items, key=lambda i: i["kind"] == "card"):
                if is_card:
                    payloads.extend(self._card_payload(i, chat_type) for i in run)
                    continue
                for group in group_items(list(run)):
                    payloads.extend(self._group_to_payloads(group, chat_type))
            if reference and payloads:
                payloads[0]["message_reference"] = reference

            first_mid: int | None = None
            recorded = _swap_media_urls(normalize_message(message), plan.file_urls)
            # 媒体上传后拿到的 QQ CDN 地址: 托管 URL -> 带新鲜 rkey 的直链
            cdn_urls: dict[str, str] = {}
            if text_image:
                # 单条消息体交给 payload_out 插件
                ctx = HookContext(self.bot, chat_type, peer_openid)
                expanded: list[dict] = []
                for payload in payloads:
                    out = await plugins.first("payload_out", ctx, payload)
                    expanded.extend(out if out is not None else [payload])
                payloads = expanded
            for index, payload in enumerate(payloads):
                qq_id, ref_idx = await self._send_one(
                    chat_type, peer_openid, payload,
                    prefer_mid=plan.prefer_reply_mid if index == 0 else None,
                    cdn_urls=cdn_urls,
                )
                # 记下媒体字节大小: 引用 idx 对不上时按大小归属
                sizes: list[int] = []
                if payload.get("_media"):
                    size = self.bot.media.size_of_url(payload["_media"][1])
                    if size:
                        sizes.append(size)
                # 首条记完整逻辑消息; 后续分片记本片实际内容(卡片被拒就是改发的文字)
                segments = (recorded if index == 0
                            else self._payload_segments(payload.get("_fell_back", payload)))
                mid = await self.bot.store.record_outgoing(
                    self.bot.appid, chat_type, peer_openid, peer_virtual,
                    self.bot.self_id, qq_id, segments,
                    msg_idx=ref_idx,  # 让 bot 自己发的消息也能被引用
                    media_sizes=sizes,
                    batch_mid=first_mid,  # 同一逻辑消息的分片串在一起, 便于整体撤回
                    batch_self=first_mid is None,   # 首片指向自己, 免一次回写
                    sent_text=_payload_text(payload),
                )
                if first_mid is None:
                    first_mid = mid
            assert first_mid is not None
            if cdn_urls:
                # 落库与自身消息回报给 QQ CDN 直链而非自家 /media(省回源流量);
                # 首片在上传前就落库了, 这里回写
                swap = {raw: cdn_urls.get(url, url) for raw, url in plan.file_urls.items()}
                swap.update(cdn_urls)
                recorded = _swap_media_urls(normalize_message(message), swap)
                await self.bot.store.update_content(first_mid, recorded)
            # 转发摊平成多条时附一条撤回提示, 引用它可整批撤回(见 builtin/recall.py)
            if is_forward and len(payloads) > 2 and self._option("recall_hint", True):
                role = (await self.bot.group_role(peer_openid)
                        if chat_type == "group" else "")
                await self._append_recall_hint(
                    chat_type, peer_openid, peer_virtual, first_mid,
                    recall_hint_for(role))
            await self._report_self(chat_type, peer_openid, peer_virtual,
                                    first_mid, recorded)
            return first_mid

    async def _stash_inline_media(self, segments: list[dict],
                                  file_urls: dict[str, str]) -> None:
        """兜底: 还没有对应地址的 base64 媒体存进媒体库, 落库与自身消息回报改带 media:// 引用.

        正常路径(_plan_segment、建页、卡片封面)都会记进 file_urls, 走到这里说明有漏, 记日志;
        解不开的 base64 记空串, 照样不带出去.
        """
        missed = list(dict.fromkeys(
            raw for raw in _inline_media(segments) if raw not in file_urls))
        for raw in missed:
            url, _ = await asyncio.to_thread(self.bot.media.ingest_file_field, raw)
            file_urls[raw] = url or ""
        if missed:
            logger.info("[%s] %d 个 base64 媒体没对上地址, 落库与回报改用媒体库引用",
                        self.bot.appid, len(missed))

    async def _report_self(self, chat_type: str, peer_openid: str,
                           peer_virtual: int, mid: int,
                           segments: list[dict]) -> None:
        """把 bot 自己发的这条合成为 message 事件下发(平台不回显自身消息).

        user_id 是 bot 的虚拟号; 整个逻辑消息只发一条, 不按分片.
        """
        if not self.bot.report_self_message:
            return
        if not self.bot.peer_enabled(chat_type, peer_openid):
            return
        ts = int(time.time())
        nickname = str(self.bot.cfg.get("name") or "")
        if chat_type == "group":
            role = await self.bot.group_role(peer_openid)
            sender = ob_events.make_sender(self.bot.self_id, nickname, role)
            event = ob_events.group_message_event(
                self.bot.self_id, peer_virtual, self.bot.self_id,
                mid, segments, sender, ts)
        else:
            sender = ob_events.make_sender(self.bot.self_id, nickname, "member")
            event = ob_events.private_message_event(
                self.bot.self_id, self.bot.self_id, mid, segments, sender, ts)
        await self.bot.emit(event, raw=False)

    async def _maybe_imagify(self, payload: dict,
                             min_chars: int = TEXT_IMAGE_MIN_CHARS) -> list[dict]:
        """单条文本超过阈值 -> 渲染成图片, 链接另发一条(图里点不了).

        @ 标签摘出来单发; 含兑换码或指令标签的不转图; 渲染失败原样发文本.
        """
        if payload.get("_media") or payload.get("msg_type") not in (0, 2):
            return [payload]
        if payload.get("msg_type") == 2:
            source = str((payload.get("markdown") or {}).get("content", ""))
        else:
            source = str(payload.get("content", ""))
        at_tags = _AT_USER_TAG_RE.findall(source)
        body = _AT_USER_TAG_RE.sub("", source).strip()
        if len(body) <= min_chars:
            return [payload]
        # 兑换码进了图就复制不了
        if _COPY_CODE_RE.search(body):
            return [payload]
        # 指令标签进了图就点不动
        if _CMD_TAG_RE.search(body):
            return [payload]
        source, links = prepare_image_text(body)
        png = await textimg.render_text_image(source)
        if not png:
            return [payload]
        token = self.bot.media.put_bytes(png, ".png")
        url = self.bot.media.ref(token, "text.png")
        image = {"msg_type": 7, "_media": (FILE_TYPE_IMAGE, url, "", token)}
        out: list[dict] = []
        if at_tags:
            out.append({"msg_type": 2,
                        "markdown": {"content": " ".join(at_tags)}})
        out.append(image)
        if links:
            # 链接单发, [n] 与图里的角标对应; markdown 不可用就发裸链接
            plain = "\n".join(f"[{i + 1}] {u}" for i, u in enumerate(links))
            if self.bot.markdown_enabled:
                md = "\n".join(
                    f"[{i + 1}] [请点击 {domain_label(u)}]({u})"
                    for i, u in enumerate(links))
                out.append({"msg_type": 2,
                            "markdown": {"content": to_markdown_content(md)},
                            "_plain_fallback": plain})
            else:
                out.append({"msg_type": 0, "content": plain})
        if "message_reference" in payload:
            out[0]["message_reference"] = payload["message_reference"]
        logger.info("[%s] 长文本(%d 字)转图片发送, 链接单发 %d 个%s",
                    self.bot.appid, len(body), len(links),
                    ", @ 单发" if at_tags else "")
        return out

    async def _append_recall_hint(self, chat_type: str, peer_openid: str,
                                  peer_virtual: int, batch_mid: int,
                                  hint: str = RECALL_HINT) -> None:
        try:
            qq_id, ref_idx = await self._send_one(
                chat_type, peer_openid,
                {"msg_type": 0, "content": hint}, prefer_mid=None)
        except Exception as exc:
            logger.info("[%s] 撤回提示未发出: %s", self.bot.appid, exc)
            return
        await self.bot.store.record_outgoing(
            self.bot.appid, chat_type, peer_openid, peer_virtual,
            self.bot.self_id, qq_id,
            [{"type": "text", "data": {"text": hint}}],
            msg_idx=ref_idx, batch_mid=batch_mid, sent_text=hint,
        )

    @staticmethod
    def _payload_segments(payload: dict) -> list[dict]:
        """物理消息 payload -> 可读的 OneBot 段(用于第 2+ 分片的落库)."""
        segments: list[dict] = []
        if payload.get("msg_type") == 8:
            content = (payload.get("card") or {}).get("content") or {}
            return [{"type": "share", "data": {
                "url": content.get("url", ""), "title": content.get("title", ""),
                "content": content.get("description", ""),
                "image": content.get("pic_url", "")}}]
        if payload.get("_media"):
            file_type, url, name = payload["_media"][:3]
            seg_type = {FILE_TYPE_IMAGE: "image", FILE_TYPE_VIDEO: "video",
                        FILE_TYPE_VOICE: "record", FILE_TYPE_FILE: "file"}.get(
                file_type, "file")
            segments.append({"type": seg_type,
                             "data": {"file": name or url, "url": url}})
        text = payload.get("content") or ""
        if isinstance(payload.get("markdown"), dict):
            text = text or payload["markdown"].get("content", "")
        if text.strip():
            segments.append({"type": "text", "data": {"text": text}})
        return segments

    def _card_payload(self, item: dict, chat_type: str) -> dict:
        """卡片条目 -> msg_type=8; 卡片被拒时改发的文字一并备好(_card_fallback)."""
        text = {"kind": "text", "plain": item["plain"], "md": item["md"], "has_at": False}
        fallback = self._group_to_payloads(
            {"text": text, "media": None, "text_first": True}, chat_type)
        payload = {"msg_type": 8, "card": item["card"]}
        if fallback:
            payload["_card_fallback"] = fallback[0]
        return payload

    def _group_to_payloads(self, group: dict, chat_type: str) -> list[dict]:
        """一个"文字+图片"分组 -> 1~2 个平台消息(markdown 与 media 不能共存).

        纯文本 + 图片 -> 一条 msg_type=7 带 content; 需要 markdown 的 -> 两条, 保序.
        """
        text_item, media_item = group["text"], group["media"]
        media_payload = (
            {"msg_type": 7, "_media": (media_item["file_type"], media_item["url"],
                                       media_item.get("name", ""),
                                       media_item.get("token"))}
            if media_item else None
        )
        if text_item is None:
            return [media_payload] if media_payload else []

        plain = text_item["plain"].strip()
        md = text_item["md"].strip()
        if not plain:
            return [media_payload] if media_payload else []

        # @ 只能靠 markdown 标签实现, 且仅群聊有效
        use_at = (text_item["has_at"] and chat_type == "group"
                  and self.bot.markdown_enabled)
        # 指令标签同理, 值得为它拆气泡
        has_cmd = bool(_CMD_TAG_RE.search(plain))
        # 收短过链接(md != plain)也要走 markdown, 但同组有图时不值得为此拆气泡
        shortened = md != plain
        use_md = self.bot.markdown_enabled and (
            use_at or has_cmd or looks_like_markdown(plain)
            or (shortened and media_payload is None))

        if use_md:
            # 没收短过的话 md 就等于 plain
            body_md = downgrade_cmd_enter(md) if chat_type == "group" else md
            text_payload = {
                "msg_type": 2,
                "markdown": {"content": to_markdown_content(body_md)},
                # 降级重发时标签也还原成指令原文
                "_plain_fallback": strip_markdown(strip_cmd_tags(plain)),
            }
        else:
            # markdown 关着时标签点不了, 还原成指令原文
            text_payload = {"msg_type": 0, "content": strip_cmd_tags(plain)}

        if media_payload is None:
            return [text_payload]
        if text_payload["msg_type"] == 0:
            # 图文合并: 文字塞进富媒体的 content, 一个气泡
            media_payload["content"] = text_payload["content"]
            return [media_payload]
        # markdown/@ 与图片互斥 -> 拆两条, 顺序照原文
        return ([text_payload, media_payload] if group["text_first"]
                else [media_payload, text_payload])

    async def _send_one(
        self, chat_type: str, peer_openid: str, payload: dict,
        prefer_mid: int | None, cdn_urls: dict[str, str] | None = None,
        slot=_ACQUIRE,
    ) -> tuple[str, str]:
        """返回 (平台消息id, ref_idx); cdn_urls 传了就记下 托管 URL -> QQ CDN 地址.

        卡片被拒改发文字时在 payload 上记 _fell_back(落库按实际发出的记).
        slot: 已拿到的被动回复名额(None = 主动); 默认现取.
        """
        body = dict(payload)
        media_info = body.pop("_media", None)
        plain_fallback = body.pop("_plain_fallback", None)
        card_fallback = body.pop("_card_fallback", None)
        # 卡片带引用: 接口照样回 200, 客户端却只显示「请使用新版手机QQ进行查看」
        # (2026-11-01 实测, 同一张卡片不带引用就正常). 后端传了 reply 也剥掉, 保证卡片能看;
        # 卡片被拒改发文字时引用再挂回去
        card_reference = (body.pop("message_reference", None)
                          if body.get("msg_type") == 8 else None)
        if media_info is not None:
            file_type, url, file_name, token = media_info
            try:
                upload = await self._upload_media(
                    chat_type, peer_openid, file_type, url, file_name, token
                )
            except QQApiError as exc:
                if file_type == FILE_TYPE_FILE:
                    # 文件上传被拒 -> 文本兜底; 本地引用只报名字
                    shown = url if url.startswith(("http://", "https://")) else ""
                    body = {"msg_type": 0,
                            "content": f"[文件] {shown or file_name or '文件'}"}
                    return await self._send_one(chat_type, peer_openid, body, prefer_mid)
                raise SendError(1500, f"media upload failed: {exc}") from exc
            body["media"] = {"file_info": upload.get("file_info", "")}
            if cdn_urls is not None and url:
                shown = _cdn_address(chat_type, upload)
                if shown:
                    cdn_urls[url] = shown
            # content 可与 media 共存; 没有文字时别塞空格, 否则图上多一行空文本

        if slot is _ACQUIRE:
            slot = await self.bot.store.acquire_reply_slot(
                self.bot.appid, chat_type, peer_openid, prefer_mid=prefer_mid
            )
        proactive = slot is None          # 只有主动消息吃日配额
        if slot is not None:
            credential, seq, kind = slot
            # 事件凭据用 event_id 字段(不吃主动配额, 群里会自动 @ 触发者)
            body["event_id" if kind == "event" else "msg_id"] = credential
            body["msg_seq"] = seq
        elif not self.bot.peer_allows_proactive(chat_type, peer_openid):
            raise SendError(1503, "无被动回复窗口且该会话主动消息未开通")

        try:
            resp = await self._post_at_all(chat_type, peer_openid, body)
        except QQApiError as exc:
            if body.get("msg_type") == 8 and card_fallback is not None and (
                exc.code in CARD_REJECT_CODES
                or (exc.status == 400 and exc.code not in _SESSION_CODES)
            ):
                # 卡片被拒(多半是封面平台拉不到) -> 同一名额改发备好的链接文字
                logger.info("[%s] card rejected (%s %s), 改发文字",
                            self.bot.appid, exc.code, exc.message)
                fallback = dict(card_fallback)
                if card_reference:
                    fallback["message_reference"] = card_reference
                payload["_fell_back"] = fallback
                return await self._send_one(chat_type, peer_openid, fallback,
                                            prefer_mid, cdn_urls, slot=slot)
            if body.get("msg_type") == 2 and plain_fallback is not None and (
                exc.code in MARKDOWN_REJECT_CODES
                or (exc.status == 400 and exc.code not in _SESSION_CODES)
            ):
                # markdown 被拒 -> 同一 msg_id/msg_seq 名额改发纯文本
                logger.info("[%s] markdown rejected (%s), 降级纯文本",
                            self.bot.appid, exc.code)
                body.pop("markdown", None)
                body["msg_type"] = 0
                body["content"] = plain_fallback or " "
                try:
                    resp = await self._post_message(chat_type, peer_openid, body)
                except QQApiError as exc2:
                    if exc2.code in URL_BLOCK_CODES:
                        body["content"] = _defang_urls(body["content"])
                        resp = await self._post_message(chat_type, peer_openid, body)
                    elif exc2.code in PASSIVE_EXPIRED_CODES and slot is not None:
                        # 纯文本这次才暴露凭据不行: 作废后照纯文本重发(markdown 已经被拒过)
                        await self.bot.store.revoke_reply_slot(self.bot.appid, slot)
                        plain = {k: v for k, v in payload.items()
                                 if k not in ("markdown", "_plain_fallback")}
                        plain.update(msg_type=0, content=body["content"])
                        payload["_fell_back"] = plain
                        return await self._send_one(chat_type, peer_openid, plain,
                                                    prefer_mid, cdn_urls)
                    else:
                        raise SendError(
                            1500 if exc2.status >= 500 else 1404,
                            f"qq api error {exc2.code}: {exc2.message}",
                        ) from exc2
            elif exc.code == 22006 and body.get("msg_type") == 7 \
                    and "content" not in body:
                # 个别场景要求 media 也带 content, 补个空格重试
                body["content"] = " "
                resp = await self._post_message(chat_type, peer_openid, body)
            elif exc.code in URL_BLOCK_CODES and (
                body.get("content") or body.get("markdown")
            ):
                # 图文合并的 content 与 markdown 同样会被 URL 拦
                if body.get("content"):
                    body["content"] = _defang_urls(body["content"])
                if isinstance(body.get("markdown"), dict):
                    body["markdown"]["content"] = _defang_urls(
                        body["markdown"].get("content", ""))
                resp = await self._post_message(chat_type, peer_openid, body)
            elif exc.code in RATE_LIMIT_CODES:
                # 限频/配额: 复查 bot_state(权限可能被调整)
                asyncio.create_task(
                    self.bot.refresh_peer_state(chat_type, peer_openid, "rate-limit")
                )
                self.bot.note_send_error("rate_limit", exc.code, peer_openid)
                raise SendError(1502, f"rate limited: {exc.code} {exc.message}") from exc
            elif exc.code in PROACTIVE_DENIED_CODES:
                # 主动发言被关: 复查状态
                self.bot.note_send_error("proactive_denied", exc.code, peer_openid)
                asyncio.create_task(
                    self.bot.refresh_peer_state(chat_type, peer_openid, "no-proactive")
                )
                raise SendError(1503, f"主动消息无权限: {exc.message}") from exc
            elif exc.code in TRANSIENT_CODES:
                # 给可重试的 retcode
                self.bot.note_send_error("transient", exc.code, peer_openid)
                raise SendError(1502, f"平台暂时性故障: {exc.code} {exc.message}") from exc
            elif exc.code in NOT_IN_GROUP_CODES:
                # 被踢/退群: 复查以作废群状态
                self.bot.note_send_error("not_in_group", exc.code, peer_openid)
                asyncio.create_task(
                    self.bot.refresh_peer_state(chat_type, peer_openid, "not-member")
                )
                raise SendError(1404, f"bot 不在该群: {exc.code} {exc.message}") from exc
            elif exc.code in MUTED_CODES:
                self.bot.note_send_error("muted", exc.code, peer_openid)
                raise SendError(1503, f"bot 已被禁言: {exc.message}") from exc
            elif exc.code in PEER_REFUSED_CODES:
                self.bot.note_send_error("peer_refused", exc.code, peer_openid)
                raise SendError(1503, f"对方不收消息: {exc.code} {exc.message}") from exc
            elif exc.code in PASSIVE_EXPIRED_CODES:
                self.bot.note_send_error("passive_expired", exc.code, peer_openid)
                if slot is not None:
                    # 凭据被拒(多是入群/开主动消息这类通知事件的 event_id: 无效或不能回复):
                    # 作废它再发, 自动换下一个凭据或走主动; 每轮作废一个, 必然收敛
                    await self.bot.store.revoke_reply_slot(self.bot.appid, slot)
                    return await self._send_one(chat_type, peer_openid, payload,
                                                prefer_mid, cdn_urls)
                raise SendError(1503, f"被动回复窗口已过: {exc.message}") from exc
            elif exc.code in BAD_MESSAGE_CODES:
                self.bot.note_send_error("bad_message", exc.code, peer_openid)
                raise SendError(1400, f"消息不合法: {exc.code} {exc.message}") from exc
            else:
                self.bot.note_send_error("other", exc.code, peer_openid)
                raise SendError(
                    1500 if exc.status >= 500 else 1404,
                    f"qq api error {exc.code}: {exc.message}",
                ) from exc
        self.bot.note_sent(chat_type, peer_openid, proactive)
        qq_id = str(resp.get("id") or resp.get("message_id")
                    or f"unknown-{int(time.time())}")
        ref_idx = str((resp.get("ext_info") or {}).get("ref_idx", ""))
        return qq_id, ref_idx

    async def _post_at_all(self, chat_type: str, peer_openid: str, body: dict) -> dict:
        """带 @全体 标签的消息: 发出去就记下今天已用; 被拒换成文本 @全体成员 再发一次.

        被拒的错误码平台没写(非群管/次数用完), 除会话级错误外一律按被拒处理.
        换掉标签后的错误照常交给调用方分类. body 原地改, 调用方的降级重发要接着用.
        """
        markdown = body.get("markdown")
        content = str(markdown.get("content") or "") if isinstance(markdown, dict) else ""
        if chat_type != "group" or not _AT_ALL_TAG_RE.search(content):
            return await self._post_message(chat_type, peer_openid, body)
        try:
            resp = await self._post_message(chat_type, peer_openid, body)
        except QQApiError as exc:
            if exc.code in _SESSION_CODES or exc.status >= 500:
                raise
            self._at_all_day[peer_openid] = _beijing_day()
            logger.info("[%s] @全体 被拒(%s %s), 改为文本 %s", self.bot.appid,
                        exc.code, exc.message, AT_ALL_TEXT)
            body["markdown"] = {**markdown, "content": _AT_ALL_TAG_RE.sub(AT_ALL_TEXT, content)}
            return await self._post_message(chat_type, peer_openid, body)
        self._at_all_day[peer_openid] = _beijing_day()
        return resp

    async def _upload_media(
        self, chat_type: str, peer_openid: str, file_type: int, url: str,
        file_name: str = "", token: str | None = None,
    ) -> dict:
        """拿 file_info. 本地文件只走分片直传(按内容哈希缓存复用), 外链才让平台来下载."""
        path = self.bot.media.resolve(token) if token else None
        if path is not None:
            data = await asyncio.to_thread(path.read_bytes)
            try:
                return await self.bot.uploader.upload_media(
                    chat_type, peer_openid, file_type, data,
                    file_name or path.name, need_uuid=True)
            except MediaTooLarge as exc:
                raise SendError(1400, str(exc)) from exc
        if not url.startswith(("http://", "https://")):
            raise SendError(1500, "本地媒体已过期, 无法发送")
        if chat_type == "group":
            done = await self.bot.api.upload_group_media(
                peer_openid, file_type, url, file_name)
        else:
            done = await self.bot.api.upload_c2c_media(
                peer_openid, file_type, url, file_name)
        # URL 上传也回 file_uuid, 带上类型好拼直链
        return {**done, "file_type": file_type}

    async def _post_message(self, chat_type: str, peer_openid: str, body: dict) -> dict:
        if chat_type == "group":
            return await self.bot.api.send_group_message(peer_openid, body)
        return await self.bot.api.send_c2c_message(peer_openid, body)


def _payload_text(payload: dict) -> str:
    """实际发出的文字; 引用 idx 对不上时靠它按内容找回(插件改写后与记录的原消息不同)."""
    if payload.get("_fell_back") is not None:
        return _payload_text(payload["_fell_back"])
    if payload.get("msg_type") == 8:
        return cards.quote_key(payload.get("card") or {})
    if payload.get("_plain_fallback") is not None:
        return str(payload["_plain_fallback"])
    if payload.get("msg_type") == 2:
        return str((payload.get("markdown") or {}).get("content") or "")
    return str(payload.get("content") or "")


def _cdn_address(chat_type: str, upload: dict) -> str:
    """上传结果 -> 给插件的地址: file_uuid 直链 + 新鲜 rkey, 没 rkey 用 COS 的 raw_url(1 小时).
    私聊的 rkey 太少, 没登记 appid, 直接给 raw_url."""
    appid = cdnkeys.APPID_BY_UPLOAD.get((chat_type, int(upload.get("file_type") or 0)))
    rkey = cdnkeys.fresh().get(appid or "")
    if appid and rkey and upload.get("file_uuid"):
        return cdnkeys.with_rkey(cdnkeys.cdn_url(appid, str(upload["file_uuid"])), rkey)
    return str(upload.get("raw_url") or "")


# 平台音乐只给 id: 能拼出网页直链的拼, 其余文本降级
_MUSIC_URL_TEMPLATES = {
    "163": "https://music.163.com/#/song?id={id}",
    "qq": "https://y.qq.com/n/ryqq/songDetail/{id}",
}

# 卡片 JSON 里常见的跳转链接键(与入站 ARK 解析同一套口径)
_CARD_LINK_RE = re.compile(
    r'"(?:jumpUrl|jump_url|qqdocurl|url|link|web_url|weburl|detail_url)"'
    r'\s*:\s*"(https?:[^"]{1,300})"')


# json 卡片里能转成图文卡片的几种: meta 下的键 -> (标题, 简介, 跳转, 图) 取哪个字段.
# 小程序(B 站分享等)的 detail_1.title 是小程序名, 内容标题在 desc
_ARK_FIELDS = {
    "detail_1": ("desc", "title", "qqdocurl", "preview"),
    "news": ("title", "desc", "jumpUrl", "preview"),
    "music": ("title", "desc", "jumpUrl", "preview"),
}


def _ark_card_fields(data: dict) -> tuple[str, str, str, str] | None:
    """json 段(小程序/新闻/音乐 ark) -> (标题, 简介, 跳转, 图); 认不出返回 None."""
    try:
        ark = json.loads(str(data.get("data") or ""))
    except ValueError:
        return None
    meta = ark.get("meta") if isinstance(ark, dict) else None
    if not isinstance(meta, dict):
        return None
    for key, (title_key, desc_key, url_key, pic_key) in _ARK_FIELDS.items():
        item = meta.get(key)
        if not isinstance(item, dict):
            continue
        pic = str(item.get(pic_key) or "").strip()
        # 签名版小程序的 preview 不带协议头
        if pic and not pic.startswith(("http://", "https://", "//")):
            pic = "https://" + pic
        return (str(item.get(title_key) or item.get(desc_key) or ""),
                str(item.get(desc_key) or "") if item.get(title_key) else "",
                str(item.get(url_key) or "").strip(), pic)
    return None


def _extract_card_text(data: dict) -> tuple[str, str]:
    """json/xml 卡片段 -> (可读文本, 跳转链接); 提不出链接时第二项为空."""
    raw = data.get("data", "")
    if not isinstance(raw, str):
        return "[卡片]", ""
    texts = re.findall(r'"(?:title|desc|text|prompt)"\s*:\s*"([^"]{1,120})"', raw)
    match = _CARD_LINK_RE.search(raw)
    url = match.group(1).replace("\\/", "/") if match else ""
    text = " ".join(dict.fromkeys(texts)) if texts else "[卡片]"
    return text, url
