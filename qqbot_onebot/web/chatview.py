"""管理台的消息显示整理: 库里的 OneBot 段 -> 前端直接画的形状, 另出一行摘要.

只改给前端的副本, 不动库:
- face: 补 name(平台给的文案, 没有就查对照表)与 emoji 替身(有才给);
- json(ARK) / music / share: 抽成统一卡片 {label, title, desc, image, icon, source, url};
  ARK 前面平台渲染的「[卡片消息] …」文字与后面那段「换行+跳转链接」都是给解析类插件的,
  与卡片重复, 不再单独显示;
- 连续的 node 段(外发的合并转发)并成一个 forward 段, 节点随响应带出;
- 入站 forward 段的节点在 forwards 表, 这里先登记 id, 由调用方查表后补上条数与预览;
- 入库时截断过的文字(…<省略 N 字符>)标出原长;
- 早先存的合并转发节点里还留着平台的内联表情/附件标记(入库时没切), 这里补切.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from ..core import forwardpage
from ..onebot.faces import NAME_EMOJI, SYSFACE_NAMES
from ..onebot.segments import ATTACHMENT_TAG_RE, FACE_TAG_RE, inline_markup

PREVIEW_LINES = 4       # 聊天记录卡片上露几条
PREVIEW_CHARS = 60      # 每条露多少字
_CUT_RE = re.compile(r"…<省略 (\d+) 字符>$")
# 入站 ARK 的 content: 平台把卡片渲染成「[卡片消息] 小程序\n摘要: …\nkey: value…」, 紧跟 json 段
_CARD_RENDER_PREFIX = "[卡片消息]"
_PROMPT_RE = re.compile(r'"prompt":\s*"((?:[^"\\]|\\.)*)"')
_SPACE_RE = re.compile(r"\s+")
_MEDIA_LABEL = {"image": "图片", "mface": "表情", "video": "视频", "record": "语音",
                "audio": "语音", "file": "文件"}
# 摘要里不出现的: 消息列表另有类型徽章, 引用本身不是内容
_BADGED = frozenset({"image", "mface", "video", "record", "audio", "file", "forward",
                     "node", "at", "reply"})
_MUSIC_SITE = {"qq": "QQ音乐", "163": "网易云音乐", "kugou": "酷狗音乐",
               "kuwo": "酷我音乐", "migu": "咪咕音乐"}

NameOf = Callable[[int], str]


def face_name(data: dict) -> str:
    """平台在 ext 里给的文案(如 流泪 / [睡觉] / /赞)优先, 没有再查对照表."""
    name = str(data.get("summary") or "").strip().lstrip("/")
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name:
        return name
    fid = str(data.get("id") or "")
    return SYSFACE_NAMES.get(fid) or (f"表情{fid}" if fid else "表情")


def _https(url) -> str:
    """只放行 http(s); http 换成 https(管理台 CSP 的 img-src 只放 https)."""
    url = str(url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url if url.startswith("https://") else ""


def _link(url) -> str:
    url = str(url or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def _card(label="", title="", desc="", image="", icon="", source="", url="",
          prompt="") -> dict:
    """prompt 是平台/插件给的一句话摘要(如 [分享]标题), 只用于摘要, 不上卡片."""
    title, desc = str(title or "").strip(), str(desc or "").strip()
    if desc == title:
        desc = ""
    return {"label": str(label or "卡片"), "title": title, "desc": desc,
            "image": _https(image), "icon": _https(icon),
            "source": str(source or "").strip(), "url": _link(url),
            "prompt": str(prompt or "").strip()}


def _prompt_of(raw: str) -> str:
    match = _PROMPT_RE.search(raw)
    if not match:
        return ""
    try:
        return str(json.loads(f'"{match.group(1)}"'))
    except ValueError:
        return match.group(1)


def ark_card(raw) -> dict:
    """json 段 -> 卡片. 入站是平台给的 ark_data(ark_type + fields), 外发是插件的标准 ARK(meta.*)."""
    raw = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    try:
        ark = json.loads(raw)
    except ValueError:
        ark = None
    if not isinstance(ark, dict):
        # 老记录入库时截断过, JSON 不完整: 能捞出 prompt 就显示它
        prompt = _prompt_of(raw)
        return _card("卡片", prompt or "卡片", prompt=prompt)
    prompt = str(ark.get("prompt") or "")
    fields = ark.get("fields") if isinstance(ark.get("fields"), dict) else {}
    kind = str(ark.get("ark_type") or "")
    label = ark.get("ark_name") or "卡片"
    if not fields and isinstance(ark.get("meta"), dict):
        meta = next((v for v in ark["meta"].values() if isinstance(v, dict)), {})
        fields = {"title": meta.get("title"), "desc": meta.get("desc"),
                  "preview": meta.get("preview"),
                  "jump_url": (meta.get("jumpUrl") or meta.get("qqdocurl")
                               or meta.get("url")),
                  "source": meta.get("tag") or meta.get("source"),
                  "source_logo": meta.get("source_icon") or meta.get("icon")}
    if kind == "map":
        return _card(label, fields.get("desc") or prompt, fields.get("address"),
                     prompt=prompt)
    if kind == "contact_card":
        return _card(label, fields.get("nickname") or prompt, fields.get("contact"),
                     icon=fields.get("avatar"), source=fields.get("tag"), prompt=prompt)
    return _card(
        label, fields.get("title") or prompt or "卡片", fields.get("desc"),
        image=fields.get("preview") or fields.get("cover"),
        icon=fields.get("source_logo") or fields.get("tag_icon"),
        source=fields.get("source") or fields.get("tag"),
        url=fields.get("jump_url"), prompt=prompt)


def music_card(data: dict) -> dict:
    kind = str(data.get("type") or "")
    if kind == "custom":
        return _card("音乐", data.get("title") or "音乐", data.get("content"),
                     image=data.get("image"), url=data.get("url"))
    site = _MUSIC_SITE.get(kind, kind or "音乐")
    sid = str(data.get("id") or "")
    url = f"https://music.163.com/song?id={sid}" if kind == "163" and sid.isdigit() else ""
    return _card("音乐", f"{site} {sid}".strip(), source=site, url=url)


def share_card(data: dict) -> dict:
    return _card("分享", data.get("title") or data.get("url") or "分享",
                 data.get("content"), image=data.get("image"), url=data.get("url"))


def _card_text(card: dict) -> str:
    if card.get("prompt"):
        return card["prompt"]
    label, title = card.get("label") or "卡片", card.get("title") or ""
    return f"[{label}]{title if title != label else ''}"


def digest(segments, name_of: NameOf | None = None, media: bool = True) -> str:
    """一行摘要. 连续相同的表情并成「[流泪]×7」; media=False 时略过有类型徽章的段."""
    parts: list[str] = []
    last_face, run = "", 0
    segs = [seg for seg in segments or [] if isinstance(seg, dict)]
    for i, seg in enumerate(segs):
        if _card_render(segs, i):
            continue
        kind = str(seg.get("type") or "text")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if kind == "face":
            name = data.get("name") or face_name(data)
            if name == last_face and parts:
                run += 1
                parts[-1] = f"[{name}]×{run}"
            else:
                last_face, run = name, 1
                parts.append(f"[{name}]")
            continue
        last_face = ""
        if not media and kind in _BADGED:
            continue
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            name = data.get("name") or (
                name_of(int(qq)) if name_of and qq.isdigit() else "") or qq
            parts.append("@全体成员 " if qq == "all" else f"@{name} ")
        elif kind in _MEDIA_LABEL:
            parts.append(f"[{_MEDIA_LABEL[kind]}]")
        elif kind in ("forward", "node"):
            if parts and parts[-1] == "[聊天记录]" and kind == "node":
                continue                   # 一组 node 是同一份记录
            parts.append("[聊天记录]")
        elif kind == "json":
            card = data.get("card") or ark_card(data.get("data"))
            parts.append(_card_text(card))
        elif kind == "music":
            parts.append(_card_text(data.get("card") or music_card(data)))
        elif kind == "share":
            parts.append(_card_text(data.get("card") or share_card(data)))
        elif kind == "markdown":
            parts.append(str(data.get("content") or ""))
        elif kind in ("reply", "keyboard"):
            continue
        else:
            parts.append(f"[{kind}]")
    return _SPACE_RE.sub(" ", "".join(parts)).strip()


def clip(text: str, limit: int) -> str:
    return text[:limit] + ("…" if len(text) > limit else "")


class View:
    """一次响应内的整理上下文: 攒下合并转发, 入站的待调用方查表补全."""

    def __init__(self, name_of: NameOf | None = None) -> None:
        self.name_of = name_of
        self.forwards: dict[str, dict] = {}
        self.pending: set[str] = set()       # 入站 forward id

    def segments(self, segments, key: str) -> list[dict]:
        """key 是这组段在本次响应里的唯一前缀, 合并转发的 id 由它派生."""
        segs = _expand_markup(segments)
        out: list[dict] = []
        i = 0
        while i < len(segs):
            kind = str(segs[i].get("type") or "text")
            if kind == "node":
                j = i
                while j < len(segs) and segs[j].get("type") == "node":
                    j += 1
                fkey = f"{key}:{i}"
                nodes = self.nodes(segs[i:j], fkey)
                self.forwards[fkey] = {**preview(nodes), "nodes": nodes}
                out.append({"type": "forward", "data": {"id": fkey}})
                i = j
                continue
            data = dict(segs[i].get("data") or {}) if isinstance(segs[i].get("data"), dict) else {}
            i += 1
            if kind == "forward":
                fid = str(data.get("id") or "")
                if fid:
                    self.pending.add(fid)
                out.append({"type": "forward", "data": {"id": fid}})
                continue
            if kind == "face":
                data["name"] = face_name(data)
                data["emoji"] = NAME_EMOJI.get(data["name"], "")
            elif kind == "json":
                data["card"] = ark_card(data.get("data"))
            elif kind == "music":
                data["card"] = music_card(data)
            elif kind == "share":
                data["card"] = share_card(data)
            elif kind == "at":
                qq = str(data.get("qq") or "")
                if self.name_of and qq.isdigit() and not data.get("name"):
                    data["name"] = self.name_of(int(qq))
            elif kind == "text":
                if _card_render(segs, i - 1):
                    continue
                text = str(data.get("text") or "")
                prev = out[-1] if out else None
                if (prev and prev["type"] == "json" and prev["data"]["card"]["url"]
                        and text.strip() == prev["data"]["card"]["url"]):
                    continue
                cut = _CUT_RE.search(text)
                if cut:
                    data["text"] = text[:cut.start()]
                    data["cut"] = cut.start() + int(cut.group(1))
            out.append({"type": kind, "data": data})
        return out

    def nodes(self, raw, key: str) -> list[dict]:
        """各种 node 形状 -> [{name, content}], content 同样整理过(嵌套的合并转发也并好)."""
        out = []
        for n, node in enumerate(forwardpage.normalize_nodes(raw)):
            name = node["name"]
            if not name or name == str(node["uid"]):
                name = (self.name_of(node["uid"]) if self.name_of and node["uid"] else "") or name
            out.append({"name": name or "未知",
                        "content": self.segments(node["segments"], f"{key}/{n}")})
        return out


def _card_render(segs: list[dict], i: int) -> bool:
    """segs[i] 是不是平台给 ARK 渲染的那段文字(后面紧跟 json 段)."""
    seg = segs[i]
    data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
    return (seg.get("type") == "text" and i + 1 < len(segs)
            and segs[i + 1].get("type") == "json"
            and str(data.get("text") or "").startswith(_CARD_RENDER_PREFIX))


def _expand_markup(segments) -> list[dict]:
    segs: list[dict] = []
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        data = seg.get("data")
        text = str(data.get("text") or "") if isinstance(data, dict) else ""
        if seg.get("type") == "text" and (FACE_TAG_RE.search(text)
                                           or ATTACHMENT_TAG_RE.search(text)):
            segs.extend(inline_markup(text))
        else:
            segs.append(seg)
    return segs


def preview(nodes: list[dict]) -> dict:
    return {"count": len(nodes), "lines": [
        {"name": node["name"], "text": clip(digest(node["content"]), PREVIEW_CHARS)}
        for node in nodes[:PREVIEW_LINES]]}
