"""QQ 图文卡片(msg_type=8, card.type="tuwen"): 卡片体、封面图、引用回查口径.

平台口径(2026-11-01 在群里实测, 文档没写全):
- title/description/url/pic_url 四项必填; title ≤50、description ≤200, 按字符计.
- url 必须 https(http 报 40011021); pic_url 文档说 https, 实际 http 也收.
- pic_url 由平台在发送时当场下载转存, 下不到报 40034141(对方 429、COS 签名链接都会);
  字段不合法一律 40011021(message 里点名字段); 缺 card 报 305011.
- 带 message_reference 时接口照样回 200, 客户端却显示「[群机器人]请使用新版手机QQ进行查看。」
  (同样的卡片不带引用就正常) -> 卡片一律不带引用, 见 sender._send_one.
- description 里的换行客户端不显示(前后字粘在一起), 发送前换成空格; emoji 正常.
- 卡片跳到一般网站时, QQ 内置浏览器可能只显示「如需浏览，请使用浏览器访问」, 要复制到浏览器;
  同一网址发成 markdown 链接则提示「无法确认该网页的安全性」, 点继续可打开. 经腾讯文档的外链跳转页
  docs.qq.com/scenario/link.html?url=… 打开, 可在 QQ 内直接浏览(docs_jump, 2026-11-02 实测).
  短链还会把客户端带的 ?qq_aio_chat_type=N 原样接到目标后面.
- 被引用时平台只给渲染文本(没有附件), 跳转链接换成 bot.q.qq.com/e/ 短链(302 回原链接):
  "[卡片消息] 图文H5\\n摘要: [链接]{title}\\ntag: {域名}\\ntitle: {title}\\ndesc: {desc}\\njump_url: …"
  -> 引用回查按 title + desc 认(quote_key / key_from_quote).

封面给平台的地址(sender._cover_url): 群聊手里有新鲜 1407 rkey 时上传给平台, 用 CDN 直链
(实测 4/4 成功); 否则由本服务公网面的 /f/c/{name} 提供(与转发网页同一个地址, 只活媒体库 TTL)
—— 经 frp 隧道时平台拉取时好时坏(2026-11-02 实测 1/3, 同一地址隔一分钟一败一成), 败了退回文字.

只用标准库 + Pillow: xiaoweibotQ 也 import 本模块(build/make_cover/chat_preview), 改接口要当心.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from urllib.parse import quote, urlsplit

logger = logging.getLogger("qqbot.cards")

TITLE_MAX = 50
DESC_MAX = 200
# 封面高宽比夹在这个范围: 再长只取顶部(聊天记录里长图很常见, 不硬塞全图), 再宽取中间
COVER_MAX_RATIO = 4 / 3
COVER_MIN_RATIO = 1 / 2
# 封面压小些: 平台拉取超时很短, 自托管经隧道时字节越少越稳
COVER_MAX_WIDTH = 800
COVER_PATH = "/f/c/"
COVER_NAME_RE = re.compile(r"card-[0-9a-f]{24}\.jpg")   # 用 fullmatch

# 腾讯文档的外链跳转页(见模块头)
DOCS_JUMP = "https://docs.qq.com/scenario/link.html?url="

_QUOTE_TITLE_RE = re.compile(r"^title: (.*)$", re.MULTILINE)
_QUOTE_DESC_RE = re.compile(r"^desc: (.*?)\njump_url:", re.MULTILINE | re.DOTALL)


def _clip(text: str, limit: int) -> str:
    """压成一行(换行在卡片上不显示), 超长截断补省略号."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def build(title: str, desc: str, url: str, pic_url: str) -> dict | None:
    """-> 请求体里的 card 字段; 发不成卡片(跳转非 https、没有图、没有标题)返回 None."""
    url = str(url or "").strip()
    pic_url = str(pic_url or "").strip()
    if not url.startswith("https://") or not pic_url.startswith(("https://", "http://")):
        return None
    if len(url) > 1024 or len(pic_url) > 1024:
        return None
    title = _clip(title, TITLE_MAX)
    if not title:
        return None
    # 简介必填: 没有就写跳转的域名
    desc = _clip(desc, DESC_MAX) or domain_of(url) or title
    return {"type": "tuwen", "content": {"title": title, "description": desc,
                                         "url": url, "pic_url": pic_url}}


def docs_jump(url: str) -> str:
    """网址改为经腾讯文档的外链跳转页打开, 卡片点开可在 QQ 内直接浏览.

    已经是 docs.qq.com 的(包括已经是跳转页的)原样返回.
    """
    if domain_of(url) == "docs.qq.com":
        return url
    return DOCS_JUMP + quote(url, safe="")


def quote_key(card: dict) -> str:
    """发出去的卡片 -> 记进 sent_text 的文字, 被引用时 key_from_quote 还原出同一串."""
    content = card.get("content") or {}
    return f"[卡片] {content.get('title', '')}\n{content.get('description', '')}"


def key_from_quote(text: str) -> str:
    """引用里平台渲染的卡片文本 -> quote_key 口径; 不是卡片返回空串."""
    if not text.startswith("[卡片消息]"):
        return ""
    title = _QUOTE_TITLE_RE.search(text)
    desc = _QUOTE_DESC_RE.search(text)
    if not title or not desc:
        return ""
    return f"[卡片] {title.group(1).strip()}\n{desc.group(1).strip()}"


def make_cover(blob: bytes, text: bool = False) -> bytes | None:
    """任意图片 -> 卡片封面 JPEG: 动图取首帧, 高宽比夹到 [MIN, MAX], 宽度压到 COVER_MAX_WIDTH.

    text=True(文字渲染图): 先裁掉四周空白, 宽度贴着字走(渲染器最窄 500px, 短字会空一大片);
    太宽时上下补白而不是裁两边, 免得把字裁掉. 认不出的图返回 None.
    """
    try:
        from PIL import Image, ImageChops, ImageOps  # noqa: PLC0415
        with Image.open(io.BytesIO(blob)) as src:
            src.seek(0)
            img = ImageOps.exif_transpose(src) or src
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                img = Image.new("RGB", rgba.size, (255, 255, 255))
                img.paste(rgba, mask=rgba.getchannel("A"))
            elif img.mode != "RGB":
                img = img.convert("RGB")
            if text:
                white = Image.new("RGB", img.size, (255, 255, 255))
                box = ImageChops.difference(img, white).getbbox()
                if box:
                    margin = max(16, img.width // 40)
                    img = img.crop((max(0, box[0] - margin), max(0, box[1] - margin),
                                    min(img.width, box[2] + margin),
                                    min(img.height, box[3] + margin)))
            width, height = img.size
            if not width or not height:
                return None
            if height > width * COVER_MAX_RATIO:
                img = img.crop((0, 0, width, round(width * COVER_MAX_RATIO)))
            elif height < width * COVER_MIN_RATIO:
                if text:
                    canvas = Image.new("RGB", (width, round(width * COVER_MIN_RATIO)),
                                       (255, 255, 255))
                    canvas.paste(img, (0, (canvas.height - height) // 2))
                    img = canvas
                else:
                    keep = round(height / COVER_MIN_RATIO)
                    left = (width - keep) // 2
                    img = img.crop((left, 0, left + keep, height))
            if img.width > COVER_MAX_WIDTH:
                img = img.resize((COVER_MAX_WIDTH,
                                  max(1, round(img.height * COVER_MAX_WIDTH / img.width))),
                                 Image.LANCZOS)
            out = io.BytesIO()
            img.save(out, "JPEG", quality=82, optimize=True)
            return out.getvalue()
    except Exception as exc:
        logger.info("封面图处理失败: %r", exc)
        return None


def host(media, base: str, jpeg: bytes) -> str:
    """封面放进媒体库, 返回平台能来拉的地址 {base}/f/c/card-xxx.jpg."""
    name = f"card-{hashlib.sha256(jpeg).hexdigest()[:24]}.jpg"
    media.put_named(name, jpeg)
    return f"{base.rstrip('/')}{COVER_PATH}{name}"


def chat_preview(nodes: list[dict], max_chars: int = 300, max_nodes: int = 8) -> str:
    """纯文字聊天记录 -> 渲染封面用的 markdown: 「**昵称**: 内容」逐条, 攒够就停."""
    lines: list[str] = []
    used = 0
    for node in nodes[:max_nodes]:
        text = _node_text(node.get("segments") or [])
        if not text:
            continue
        name = str(node.get("name") or "").strip()
        text = text[:max(0, max_chars - used)]
        # 昵称里的 markdown 记号会把加粗搞坏
        safe_name = re.sub(r"([*_`\[\]])", r"\\\1", name)
        lines.append(f"**{safe_name}**: {text}" if name else text)
        used += len(text)
        if used >= max_chars:
            break
    return "\n\n".join(lines)


def _node_text(segments: list) -> str:
    parts: list[str] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        kind, data = seg.get("type"), seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append(f"@{data.get('name') or data.get('qq') or ''} ")
        elif kind in ("image", "mface"):
            parts.append("[图片]")
        elif kind == "video":
            parts.append("[视频]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "file":
            parts.append("[文件]")
        elif kind in ("node", "forward"):
            parts.append("[聊天记录]")
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def first_images(nodes: list[dict], limit: int = 3) -> list[dict]:
    """按出现顺序取前几个图片段的 data(含嵌套节点), 给封面用."""
    found: list[dict] = []

    def walk(segments) -> None:
        for seg in segments or []:
            if len(found) >= limit or not isinstance(seg, dict):
                return
            data = seg.get("data") or {}
            if seg.get("type") == "image":
                found.append(data)
            elif isinstance(data.get("content"), list):
                walk(data["content"])

    for node in nodes:
        walk(node.get("segments"))
        if len(found) >= limit:
            break
    return found
