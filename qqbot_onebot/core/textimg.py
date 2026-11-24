"""长文本 -> 图片(markdown 排版, 纯 Pillow).

超过 TEXT_IMAGE_MIN_CHARS 的文本由 sender 渲染成图: 样式仿 github-markdown(htmlrender 的 md_to_pic),
宽 500 起、2x, 字多时加宽到 800 内, 超高截断; 链接点不了, 由 sender 摘前几个放进 content.

- md_to_html: web/static/chat.js renderMarkdown 的移植, 现只给转发网页用.
- 转图另有一套解析(_parse, 比 md_to_html 多认嵌套列表、条目内代码块、粗斜体嵌套等), 两边
  不共用; 排版后直接画, 不起浏览器(无头 chrome 在 NVIDIA 机器上锁死过驱动).
- 字体: 中日韩按全文挑地区字形(简/繁/日/韩, Noto CJK 同一个 ttc 的不同序号), 缺字依次查
  回退链、再按文字区段问 fontconfig, 彩色 emoji 另贴图; 阿拉伯/希伯来/天城文/泰文等有 raqm
  时整形并按双向算法排序. 查找顺序见 _find_fonts; 环境变量 QBOB_TEXTIMG_FONT / _BOLD /
  _MONO / _EMOJI 可指定(文件路径, ttc 可加 #序号).

任何失败返回 None, 调用方发原文.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("qqbot.textimg")

# 原文超过此字符数转图片
TEXT_IMAGE_MIN_CHARS = 300
# 摘进 content 的链接数上限
TEXT_IMAGE_MAX_LINKS = 5
# 渲染只看前这么多字(再长也画不下), sender 在事件循环上做链接处理前也先截到这里
TEXT_IMAGE_MAX_SOURCE = 40000

# 同 md_to_pic: 宽 500(内边距 15px), 2x.
# 字多时宽度在 [_WIDTH, _MAX_WIDTH] 内自适应, 使高宽比不超过 _TARGET_RATIO.
_WIDTH = 500
_MAX_WIDTH = 800
_TARGET_RATIO = 1.5
_SCALE = 2
_MAX_HEIGHT = 8000
_SOURCE_MAX = TEXT_IMAGE_MAX_SOURCE

_TIMEOUT = 15
# 一次只渲染一张: 字体对象共用, FreeType 字面不保证线程安全. 信号量管协程排队,
# 锁管线程(超时后线程还在画, 下一张的线程要等它放锁)
_sem = asyncio.Semaphore(1)
_render_lock = threading.Lock()
# 同文本不重复渲染; 按字节封顶, 大图不缓存
_cache: OrderedDict[str, bytes] = OrderedDict()
_CACHE_MAX = 16
_CACHE_BYTES = 8 << 20
_CACHE_ITEM_MAX = 1 << 20

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_MD_LINK_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s)]+)\)")
# URL 末尾粘着的句末标点, 不算进链接
_URL_TAIL_PUNCT = "。，、；：！？…）】》」』.,;:!?"


def extract_links(text: str, limit: int = TEXT_IMAGE_MAX_LINKS) -> list[str]:
    """按出现顺序取前 limit 个链接(markdown 链接取目标), 去重."""
    seen: list[str] = []
    for match in _URL_RE.finditer(text):
        url = match.group(0).rstrip(_URL_TAIL_PUNCT)
        # 裸 URL 正则同样能命中 markdown 链接目标
        if url and url not in seen:
            seen.append(url)
        if len(seen) >= limit:
            break
    return seen


# markdown 语法探测: 出站是否走 msg_type=2 与转发网页是否按 markdown 渲染共用, 须保持一致
_MD_PATTERNS = [
    r"^#{1,6}\s+\S",              # 标题
    r"\*\*[^\s*][^*]*\*\*",       # 粗体
    r"~~[^\s~][^~]*~~",           # 删除线
    r"!\[[^\[\]]*\]\([^)]+\)",    # 图片(说明里不含方括号, 免得一串 [ 回溯成平方)
    r"(?<!!)\[[^\[\]]+\]\([^)]+\)",  # 链接
    r"^\s*[-*+]\s+\S",            # 无序列表
    r"^\s*\d+\.\s+\S",            # 有序列表
    r"^\s*>\s+\S",                # 引用
    r"^\s*(?:\*\s*){3,}$",        # 分割线
    r"`[^`\n]+`",                 # 行内代码
    r"^```",                      # 代码块
    r"^\s*\|.+\|\s*$",            # 表格
]
_MD_RE = re.compile("|".join(_MD_PATTERNS), re.MULTILINE)


def looks_like_markdown(text: str) -> bool:
    return bool(_MD_RE.search(text))


# ---------------- markdown -> HTML(chat.js renderMarkdown 的移植, 转发网页用) ----------------

_ESC_MAP = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def _esc(text: str) -> str:
    return "".join(_ESC_MAP.get(ch, ch) for ch in text)


_BLOCK_START = re.compile(r"^\s{0,3}(?:```|#{1,6}\s|[-*+]\s|\d+\.\s|>|\|)")
_HR = re.compile(r"^\s{0,3}([-*_])\s*(?:\1\s*){2,}$")
_TABLE_SPLIT = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*)$")
_OL_ITEM = re.compile(r"^\s*\d+\.\s+(.*)$")
_UL_ITEM = re.compile(r"^\s*[-*+]\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_QUOTE = re.compile(r"^\s{0,3}>")


def _emphasis(escaped: str) -> str:
    """强调替换, 只作用于已转义的串."""
    out = re.sub(r"~~([^~]+)~~", r"<del>\1</del>", escaped)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(^|[^*\w])\*([^*\n]+)\*(?!\*)", r"\1<em>\2</em>", out)
    return out


def _inline(raw: str) -> str:
    """行内: 代码/图片/链接先占位, 其余转义+强调."""
    slots: list[str] = []

    def hold(rendered: str) -> str:
        slots.append(rendered)
        return f"\x00{len(slots) - 1}\x00"

    s = raw.replace("\x00", "")
    s = re.sub(r"`([^`\n]+)`",
               lambda m: hold(f"<code>{_esc(m.group(1))}</code>"), s)
    # 图片已被 sender 抽走, 兜底显示说明文字
    s = re.sub(r"!\[([^\]]*)\]\(\s*[^)]*\)", lambda m: hold(_esc(m.group(1))), s)
    s = re.sub(r"\[([^\]]+)\]\(\s*(https?://[^\s)]+)[^)]*\)",
               lambda m: hold(f'<a href="{_esc(m.group(2))}">'
                              f"{_emphasis(_esc(m.group(1)))}</a>"), s)
    # 句末标点放在链接外, 与 extract_links 一致
    def bare(match: re.Match) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in _URL_TAIL_PUNCT:
            url, tail = url[:-1], url[-1] + tail
        if not url:
            return match.group(0)
        return hold(f'<a href="{_esc(url)}">{_esc(url)}</a>') + tail
    s = re.sub(r"https?://[^\s<>\"'）】]+", bare, s)
    s = _emphasis(_esc(s))
    return re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], s)


def md_to_html(source: str) -> str:
    """极简 markdown -> HTML: 标题/列表/引用/代码块/表格/分割线/段落."""
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            code: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            out.append(f"<pre><code>{_esc(chr(10).join(code))}</code></pre>")
            continue
        if not line.strip():
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _HR.match(line):
            out.append("<hr>")
            i += 1
            continue
        if _QUOTE.match(line):
            quoted: list[str] = []
            while i < len(lines) and _QUOTE.match(lines[i]):
                quoted.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            out.append(f"<blockquote>{md_to_html(chr(10).join(quoted))}</blockquote>")
            continue
        if _TABLE_ROW.match(line) and i + 1 < len(lines) \
                and _TABLE_SPLIT.match(lines[i + 1] or ""):
            def cells(row: str) -> list[str]:
                return [c.strip() for c in row.strip().strip("|").split("|")]
            head = cells(line)
            i += 2
            body: list[list[str]] = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                body.append(cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in head)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in row) + "</tr>"
                for row in body)
            out.append(f"<table><thead><tr>{head_html}</tr></thead>"
                       f"<tbody>{body_html}</tbody></table>")
            continue
        ordered = bool(_OL_ITEM.match(line))
        item_re = _OL_ITEM if ordered else _UL_ITEM
        if item_re.match(line):
            items: list[str] = []
            while i < len(lines):
                m = item_re.match(lines[i])
                if not m:
                    break
                items.append(f"<li>{_inline(m.group(1))}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        # 段落: 单换行也断行(插件按纯文本习惯)
        para: list[str] = []
        while i < len(lines) and lines[i].strip() and not _BLOCK_START.match(lines[i]):
            para.append(lines[i])
            i += 1
        if not para:
            para.append(lines[i])
            i += 1
        out.append("<p>" + "<br>".join(_inline(p) for p in para) + "</p>")
    return "".join(out)


# ---------------- 转图: 样式(设备像素 = CSS 像素 x _SCALE, 数值抄 github-markdown-light) ----------------

_S = _SCALE
_FS = 16 * _S                       # 正文字号
_PAD = 15 * _S
_CODE_PX = round(_FS * 0.85)
_BG = (255, 255, 255)
_TEXT = (0x24, 0x29, 0x2F)
_MUTED = (0x57, 0x60, 0x6A)
_LINK = (0x09, 0x69, 0xDA)
_BORDER = (0xD0, 0xD7, 0xDE)
_RULE = (0xD8, 0xDE, 0xE4)
_PRE_BG = (0xF6, 0xF8, 0xFA)
_CODE_BG = (0xEF, 0xF1, 0xF3)       # rgba(175,184,193,.2) 叠白底
_HEAD_EM = {1: 2.0, 2: 1.5, 3: 1.25, 4: 1.0, 5: 0.875, 6: 0.85}
_ITALIC_SKEW = 0.2                  # 中文没有斜体字面, 错切仿斜(浏览器也是这么合成的)
_TRUNCATED_NOTE = "……（内容过长，以下省略）"


@dataclass(frozen=True, slots=True)
class _Style:
    size: int                       # 块基准字号
    region: str = "SC"              # 中日韩字形地区, 见 _region
    color: tuple = _TEXT
    bold: bool = False
    italic: bool = False
    strike: bool = False
    code: bool = False              # 行内代码: 等宽 85% + 灰底
    pre: bool = False               # 代码块: 等宽, size 即字号
    sup: bool = False
    link: bool = False


# ---------------- 字体 ----------------

_LAYOUT = ImageFont.Layout.BASIC    # 中日韩与拉丁不需要整形, BASIC 快且宽度可逐字相加
try:
    from PIL import features as _features
    # 阿拉伯/希伯来/天城文/泰文等要 raqm 整形; 没有就逐字画(字形不连、不重排, 但不崩)
    _RAQM = bool(_features.check("raqm"))
except Exception:                           # noqa: BLE001
    _RAQM = False


class _Face:
    """一个字体面(ttc 取某个序号): 按字号取 ImageFont, 查字形覆盖."""

    def __init__(self, path: str, index: int = 0):
        self.path, self.index = path, index
        self._fonts: dict[tuple, ImageFont.FreeTypeFont] = {}
        self._covered: dict[str, bool] = {}
        self._probe = ImageFont.truetype(path, 16, index=index, layout_engine=_LAYOUT)
        # 缺字时 FreeType 画 .notdef, 与其掩码相同即缺
        self._notdef = self._mask(chr(0x10FFFF))

    def _mask(self, ch: str):
        m = self._probe.getmask(ch)
        return m.size, bytes(m)

    def at(self, px: int, shaped: bool = False) -> ImageFont.FreeTypeFont:
        key = (px, shaped and _RAQM)
        font = self._fonts.get(key)
        if font is None:
            engine = ImageFont.Layout.RAQM if key[1] else _LAYOUT
            font = self._fonts[key] = ImageFont.truetype(
                self.path, px, index=self.index, layout_engine=engine)
        return font

    def covers(self, unit: str) -> bool:
        for ch in unit:
            if ch in "\u200c\u200d":
                continue
            hit = self._covered.get(ch)
            if hit is None:
                hit = self._covered[ch] = self._mask(ch) != self._notdef
            if not hit:
                return False
        return True

    def name(self) -> str:
        return " ".join(self._probe.getname())


class _Emoji:
    """彩色 emoji: 位图字体只认固定字号(Noto CBDT 109), 按字号缩放后贴图."""

    def __init__(self, path: str, index: int = 0):
        self.path = path
        # raqm 才能把 ZWJ 序列/国旗/肤色/键帽合成一个字
        engine = ImageFont.Layout.RAQM if _RAQM else _LAYOUT
        for size in (109, 160, 96, 64, 128, 72, 48, 32):
            try:
                self.font = ImageFont.truetype(path, size, index=index, layout_engine=engine)
                break
            except OSError:
                continue
        else:
            raise OSError(f"emoji 字体无可用字号: {path}")
        self.size = size
        self.asc, desc = self.font.getmetrics()
        self.height = self.asc + desc
        self._covered: dict[str, bool] = {}
        self._notdef = self._mask(chr(0x10FFFF))
        self._imgs: OrderedDict[tuple, Image.Image] = OrderedDict()
        self._adv: dict[str, float] = {}
        if self._raw("😀").getchannel("A").getbbox() is None:
            raise OSError(f"emoji 字体画不出彩色字形: {path}")

    def _mask(self, ch: str):
        m = self.font.getmask(ch, mode="RGBA")
        return m.size, m.getbbox()

    def _shape(self, cluster: str) -> tuple[str, float]:
        """(实际画的串, 本字号下的前进宽)."""
        hit = self._adv.get(cluster)
        if hit is None:
            text = cluster
            if not _RAQM:
                # 不能合字: ZWJ 序列只画第一个, 去掉变体符和肤色
                text = re.sub("[\ufe0f\U0001F3FB-\U0001F3FF]", "", cluster).split("\u200d")[0]
            adv = self.font.getlength(text or cluster)
            if adv > 8 * self.size:         # 字体合不成一个字, 摊开太宽: 只画第一个
                text = cluster[0]
                adv = self.font.getlength(text)
            if len(self._adv) > 4096:
                self._adv.clear()
            hit = self._adv[cluster] = (text or cluster, adv)
        return hit

    def covers(self, cluster: str) -> bool:
        ch = cluster[0]
        hit = self._covered.get(ch)
        if hit is None:
            hit = self._covered[ch] = self._mask(ch) != self._notdef
        return hit

    def advance(self, cluster: str, px: int) -> float:
        return self._shape(cluster)[1] * px / self.size

    def _raw(self, cluster: str) -> Image.Image:
        text, adv = self._shape(cluster)
        width = max(1, math.ceil(adv))
        img = Image.new("RGBA", (width, self.height), (0, 0, 0, 0))
        ImageDraw.Draw(img).text((0, self.asc), text, font=self.font,
                                 embedded_color=True, anchor="ls")
        return img

    def image(self, cluster: str, px: int) -> Image.Image:
        key = (cluster, px)
        img = self._imgs.get(key)
        if img is None:
            raw = self._raw(cluster)
            scale = px / self.size
            img = raw.resize((max(1, round(raw.width * scale)),
                              max(1, round(raw.height * scale))), Image.Resampling.LANCZOS)
            if raw.width <= 2 * self.size:  # 合不成一个字的宽条不缓存
                self._imgs[key] = img
                if len(self._imgs) > 512:
                    self._imgs.popitem(last=False)
        return img

    def top(self, baseline: float, px: int) -> float:
        return baseline - self.asc * px / self.size


@dataclass
class _Fonts:
    base: _Face                     # 简中常规体; 其他地区从同一个 ttc 里挑
    base_bold: _Face | None         # 没有粗体字面就描边仿粗
    mono: _Face | None
    chain: list[_Face]              # 主字体缺字时依次查
    emoji: _Emoji | None


_SANS_PATHS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",        # Debian/Ubuntu
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",              # Arch
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",       # Fedora
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/wqy-microhei/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
)
_MONO_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/TTF/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansMono-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "C:/Windows/Fonts/consola.ttf",
)
# 回退链: 拉丁扩展(越南文等) > 通用符号 > 数学 > 杂项符号
_CHAIN_FAMILIES = ("Noto Sans", "DejaVu Sans", "Noto Sans Math", "Noto Sans Symbols",
                   "Noto Sans Symbols2", "Symbola")
_CHAIN_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/System/Library/Fonts/Apple Symbols.ttf",
    "C:/Windows/Fonts/seguisym.ttf",
)
_EMOJI_PATHS = (
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto-emoji/NotoColorEmoji.ttf",
    "/System/Library/Fonts/Apple Color Emoji.ttc",
    "C:/Windows/Fonts/seguiemj.ttf",
)
# 地区: fontconfig 语言, 探测字, ttc 里认字面名
_REGIONS = {
    "SC": ("zh-cn", "中", r"\b(SC|GB|CN|Hans)\b"),
    "TC": ("zh-tw", "中", r"\b(TC|TW|Hant)\b"),
    "JP": ("ja", "あ", r"\b(JP|J)\b"),
    "KR": ("ko", "한", r"\b(KR|K)\b"),
}
# 按文字区段找字体时用的 fontconfig 语言(charset 查询常落到覆盖不全的 DejaVu)
_SCRIPT_LANGS = (
    (0x0370, 0x03FF, "el"), (0x0400, 0x052F, "ru"), (0x0530, 0x058F, "hy"),
    (0x0590, 0x05FF, "he"), (0x0600, 0x06FF, "ar"), (0x0700, 0x074F, "syr"),
    (0x0750, 0x077F, "ar"), (0x0780, 0x07BF, "dv"), (0x08A0, 0x08FF, "ar"),
    (0x0900, 0x097F, "hi"), (0x0980, 0x09FF, "bn"), (0x0A00, 0x0A7F, "pa"),
    (0x0A80, 0x0AFF, "gu"), (0x0B00, 0x0B7F, "or"), (0x0B80, 0x0BFF, "ta"),
    (0x0C00, 0x0C7F, "te"), (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml"),
    (0x0D80, 0x0DFF, "si"), (0x0E00, 0x0E7F, "th"), (0x0E80, 0x0EFF, "lo"),
    (0x0F00, 0x0FFF, "bo"), (0x1000, 0x109F, "my"), (0x10A0, 0x10FF, "ka"),
    (0x1200, 0x139F, "am"), (0x1780, 0x17FF, "km"), (0x1800, 0x18AF, "mn-cn"),
    (0xFB1D, 0xFB4F, "he"), (0xFB50, 0xFDFF, "ar"), (0xFE70, 0xFEFF, "ar"),
)


@lru_cache(maxsize=1)
def _fc_exe() -> str | None:
    return shutil.which("fc-match")


@lru_cache(maxsize=1024)
def _fc_match(pattern: str) -> tuple[str, int] | None:
    exe = _fc_exe()
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "-f", "%{file}\t%{index}", pattern],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    path, _, index = out.partition("\t")
    if not path or not os.path.isfile(path):
        return None
    return path, int(index) if index.strip().isdigit() else 0


def _env_spec(name: str) -> tuple[str, int | None] | None:
    value = os.environ.get(name, "").strip() if name else ""
    if not value:
        return None
    path, _, index = value.partition("#")
    return path, int(index) if index.strip().isdigit() else None


@lru_cache(maxsize=64)
def _faces_in(path: str) -> tuple[str, ...]:
    """ttc 各序号的字族名."""
    if not path.lower().endswith((".ttc", ".otc")):
        return ()
    names = []
    for index in range(32):
        try:
            names.append(ImageFont.truetype(path, 16, index=index).getname()[0])
        except OSError:
            break
    return tuple(names)


def _region_index(path: str, region: str) -> int | None:
    tag = _REGIONS[region][2]
    for index, family in enumerate(_faces_in(path)):
        if re.search(tag, family):
            return index
    return None


@lru_cache(maxsize=128)
def _load(path: str, index: int) -> _Face | None:
    try:
        return _Face(path, index)
    except OSError:
        return None


def _try_face(spec, probe: str, region: str = "SC", exact: bool = False) -> _Face | None:
    if not spec or not os.path.isfile(spec[0]):
        return None
    path, index = spec
    # 多地区 ttc(Noto CJK 的 0 号是日文字形)按字面名挑; 环境变量写明序号的照用
    named = _region_index(path, region) if region and not exact else None
    face = _load(path, named if named is not None else (index or 0))
    return face if face is not None and face.covers(probe) else None


def _env_face(env: str, probe: str, region: str) -> _Face | None:
    spec = _env_spec(env)
    return _try_face(spec, probe, region, exact=spec is not None and spec[1] is not None)


def _first_face(env: str, patterns: tuple, paths: tuple, probe: str,
                region: str = "") -> _Face | None:
    face = _env_face(env, probe, region)
    for spec in [_fc_match(p) for p in patterns] + [(p, None) for p in paths]:
        if face is not None:
            break
        face = _try_face(spec, probe, region)
    return face


def _heavier(path: str) -> tuple:
    """常规体文件名 -> 可能的粗体/中粗体文件(GitHub 的 600 在浏览器里落到 Bold)."""
    out = []
    for heavy in ("Bold", "Medium"):
        for light in ("Regular", "Book", ""):
            if light and light in path:
                out.append(path.replace(light, heavy))
    out.append(re.sub(r"msyh\.ttc$", "msyhbd.ttc", path))
    out.append(re.sub(r"(\.tt[fc])$", r"-Bold\1", path))
    return tuple(p for p in dict.fromkeys(out) if p != path and os.path.isfile(p))


@lru_cache(maxsize=1)
def _find_fonts() -> _Fonts | None:
    """环境变量 > fc-match > 常见路径; 没有中文字体就不转图."""
    base = _first_face("QBOB_TEXTIMG_FONT", ("sans-serif:lang=zh-cn",), _SANS_PATHS, "中", "SC")
    if base is None:
        logger.warning("textimg: 找不到中文字体(可装 fonts-noto-cjk 或设 QBOB_TEXTIMG_FONT), "
                       "长文本保持原样发送")
        return None
    bold = _bold_of(base, "SC", env="QBOB_TEXTIMG_FONT_BOLD")
    mono = _first_face("QBOB_TEXTIMG_FONT_MONO", ("monospace",), _MONO_PATHS, "M")
    chain: list[_Face] = []
    for spec in [_fc_match(f) for f in _CHAIN_FAMILIES] + [(p, 0) for p in _CHAIN_PATHS]:
        face = _try_face(spec, " ")
        if face is not None and face not in chain and face is not base:
            chain.append(face)
    emoji = None
    for spec in [_env_spec("QBOB_TEXTIMG_FONT_EMOJI"), _fc_match("emoji")] \
            + [(p, 0) for p in _EMOJI_PATHS]:
        if spec and os.path.isfile(spec[0]):
            try:
                emoji = _Emoji(spec[0], spec[1] or 0)
                break
            except OSError:
                continue
    logger.info("textimg 字体: %s / 粗体 %s / 等宽 %s / 回退 %s / emoji %s / raqm %s",
                base.name(), bold.name() if bold else "描边仿粗",
                mono.name() if mono else "无", ", ".join(f.name() for f in chain) or "无",
                emoji.path if emoji else "无", _RAQM)
    return _Fonts(base, bold, mono, chain, emoji)


def _bold_of(face: _Face, region: str, env: str = "") -> _Face | None:
    """同字族的粗体面; 找不到返回 None(描边仿粗)."""
    probe = _REGIONS[region][1] if region else " "
    family = face.name().rsplit(" ", 1)[0]
    cands = [_env_face(env, probe, region)]
    # 同名粗体文件同序号(Noto CJK 各字重 ttc 序号一致), 再按地区名挑
    for path in _heavier(face.path):
        cands += [_try_face((path, face.index), probe, region, exact=True),
                  _try_face((path, None), probe, region)]
    if region:
        bold = _try_face(_fc_match(f"sans-serif:lang={_REGIONS[region][0]}:weight=bold"),
                         probe, region)
        # 只认同字族的, 别给文泉驿配上思源的粗体
        if bold is not None and bold.name().startswith(family):
            cands.append(bold)
    for cand in cands:
        if cand is not None and (cand.path, cand.index) != (face.path, face.index):
            return cand
    return None


@lru_cache(maxsize=8)
def _cjk(region: str) -> tuple[_Face, _Face | None]:
    """某地区的 (常规, 粗体) 中日韩字面: 优先主字体 ttc 里的同族地区面."""
    fs = _fonts()
    if region == "SC":
        return fs.base, fs.base_bold
    lang, probe, _ = _REGIONS[region]
    named = _region_index(fs.base.path, region)
    face = _load(fs.base.path, named) if named is not None else None
    if face is None:
        face = fs.base if fs.base.covers(probe) else (
            _try_face(_fc_match(f"sans-serif:lang={lang}"), probe, region) or fs.base)
    if face is fs.base:
        return fs.base, fs.base_bold
    bold = None
    if fs.base_bold is not None:
        index = _region_index(fs.base_bold.path, region)
        if index is not None:
            bold = _load(fs.base_bold.path, index)
    return face, bold or _bold_of(face, region)


@lru_cache(maxsize=256)
def _bold_sibling(face: _Face) -> _Face | None:
    for path in _heavier(face.path):
        cand = _load(path, face.index)
        if cand is not None:
            return cand
    return None


def _fonts() -> _Fonts:
    fonts = _find_fonts()
    assert fonts is not None
    return fonts


# 回退链也没有的字: 按文字区段问 fontconfig, 查过没有的 128 码位段不再问;
# 每张图最多问 _FC_BUDGET 次(一次起一个子进程, 东一个西一个的生僻字会拖很久)
_dyn_faces: dict[tuple, _Face] = {}
_dyn_missed: set[int] = set()
_FC_BUDGET = 32
_fc_left = _FC_BUDGET


def _script_lang(cp: int) -> str | None:
    for lo, hi, lang in _SCRIPT_LANGS:
        if lo <= cp <= hi:
            return lang
    return None


def _dyn_face(unit: str) -> _Face | None:
    global _fc_left
    ch = next((c for c in unit if not _is_mark(c) and c not in "\u200c\u200d"), unit[0])
    cp = ord(ch)
    # 未分配/私用区/代理项: 没有字体可找
    if cp >> 7 in _dyn_missed or unicodedata.category(ch) in ("Cn", "Co", "Cs"):
        return None
    for face in list(_dyn_faces.values()):
        if face.covers(unit):
            return face
    lang = _script_lang(cp)
    patterns = ([f"sans-serif:lang={lang}"] if lang else []) + [f"sans-serif:charset={cp:x}"]
    for pattern in patterns:
        if _fc_left <= 0:
            return None                     # 本张额度用完, 不记 missed, 下一张还能查
        _fc_left -= 1
        spec = _fc_match(pattern)
        if spec is None:
            continue
        face = _dyn_faces.get(spec) or _load(*spec)
        if face is not None and face.covers(unit):
            _dyn_faces[spec] = face
            return face
    _dyn_missed.add(cp >> 7)
    return None


@lru_cache(maxsize=128)
def _metrics(px: int, mono: bool, region: str) -> tuple[int, int]:
    fs = _fonts()
    face = fs.mono if mono and fs.mono else _cjk(region)[0]
    return face.at(px).getmetrics()


# ---------------- 地区判定: 字形按主要文字挑 ----------------

# 常用字里只出现在一边的(繁体 / 简体), 数量多的一边胜
_TRAD_ONLY = frozenset(
    "這們說個為對時會學國體經來過還沒發麼樣話問題現關應機動開進長門見讓認東車頭電報點實種業無與書兩萬"
    "號寫買賣錢聽氣記講論邊請謝愛覺難級網絡際給結統線紀約紅綠藍圖場處務員幫廣張當總戰歷華語讀課試識設"
    "計許訂議誰該詳調談選運遠遞達連遲適隨鐘鐵銀錯鏡閉間聞閱隊陽陰陸險隱霧靜頁頂項順須顧預領頻顏風飛飯"
    "館馬駕驗魚鳥雞黃齊龍龜樂亞產親僅從價眾優傳傷償儲兒黨蘭興養冊農決況凍淨幾擊劃則剛創刪別劇勸辦勞勢"
    "區醫單衛卻廠廳壓縣參雙變嗎啟響團園圍圓聖壞塊堅壇備復夠夾奪奮婦媽孫寶寵審憲寬賓尋導將爾塵嘗層屬歲"
    "島嶺幣師帶莊慶庫廢異棄彈強歸錄徹憶憂懷態戀惡懸驚懼慣願戲戶撲執擴掃揚擾撫搶護擔擁擇掛擠揮損換據攝"
    "擺搖敵數斷舊顯曉暫術殺雜權條楊極構槍櫃標欄樹橋檔夢檢樓歡歐殘畢毀匯漢湯溝淚澤潔淺測濟濃塗潤漲漸溫"
    "灣濕滿滾滅燈靈災爐煉煩燒熱爺牽猶獅獨獵獻環畫暢療瘋蓋盤礦碼礎確禮積稱穩窮競筆簽簡類糧緊純納紙練組"
    "細織終紹繼續維綜緩編縮羅罰職聯聰肅腸膚脅腦腳臉艦藝節蘇藥榮獲營慮雖蝦補裝觀規視覽觸訓討訪證評訴詞"
    "譯詩誠詢誤謀貝負財責敗貨質購貼貴費資賞賠賽贊趕趨躍踐蹤輪軟輕較輛輩輸辭遷違邏遺郵鄰醬釋鑒針鋼鑰鈴"
    "鏈銷鎖鍋鍵閃閒鬧階陳隸韓頑頓額飄飢飲飾飽餅驅駐騎騙鮮鳴鵝麥齒齡裡餘並製衝雲範葉徵準鬥後")
_SIMP_ONLY = frozenset(
    "这们说个为对时会学国体经来过还没发么样话问题现关应机动开进长门见让认东车头电报点实种业无与书两万"
    "号写买卖钱听气记讲论边请谢爱觉难级网络际给结统线纪约红绿蓝图场处务员帮广张当总战历华语读课试识设"
    "计许订议谁该详调谈选运远递达连迟适随钟铁银错镜闭间闻阅队阳阴陆险隐雾静页顶项顺须顾预领频颜风飞饭"
    "馆马驾验鱼鸟鸡黄齐龙龟乐亚产亲仅从价众优传伤偿储儿党兰兴养册农决况冻净几击划则刚创删别剧劝办劳势"
    "区医单卫却厂厅压县参双变吗启响团园围圆圣坏块坚坛备复够夹夺奋妇妈孙宝宠审宪宽宾寻导将尔尘尝层属岁"
    "岛岭币师带庄庆库废异弃弹强归录彻忆忧怀态恋恶悬惊惧惯愿戏户扑执扩扫扬扰抚抢护担拥择挂挤挥损换据摄"
    "摆摇敌数断旧显晓暂术杀杂权条杨极构枪柜标栏树桥档梦检楼欢欧残毕毁汇汉汤沟泪泽洁浅测济浓涂润涨渐温"
    "湾湿满滚灭灯灵灾炉炼烦烧热爷牵犹狮独猎献环画畅疗疯盖盘矿码础确礼积称稳穷竞笔签简类粮紧纯纳纸练组"
    "细织终绍继续维综缓编缩罗罚职联聪肃肠肤胁脑脚脸舰艺节苏药荣获营虑虽虾补装观规视览触训讨访证评诉词"
    "译诗诚询误谋贝负财责败货质购贴贵费资赏赔赛赞赶趋跃践踪轮软轻较辆辈输辞迁违逻遗邮邻酱释鉴针钢钥铃"
    "链销锁锅键闪闲闹阶陈隶韩顽顿额飘饥饮饰饱饼驱驻骑骗鲜鸣鹅麦齿龄")


def _region(text: str) -> str:
    """有假名 -> 日文字形, 谚文为主 -> 韩文, 繁体专用字多于简体专用字 -> 繁中, 否则简中."""
    kana = hangul = han = trad = simp = 0
    for ch in text:
        o = ord(ch)
        if o < 0x3040:
            continue
        if 0x4E00 <= o <= 0x9FFF:
            han += 1
            if ch in _TRAD_ONLY:
                trad += 1
            elif ch in _SIMP_ONLY:
                simp += 1
        elif 0x3041 <= o <= 0x30FA or 0x31F0 <= o <= 0x31FF or 0xFF66 <= o <= 0xFF9D:
            kana += 1                       # 不含 ・ ー, 中文里也用
        elif 0xAC00 <= o <= 0xD7A3 or 0x1100 <= o <= 0x11FF or 0x3131 <= o <= 0x318E:
            hangul += 1
    if kana >= 2 and kana * 5 >= han and kana >= hangul:
        return "JP"
    if hangul >= 2 and hangul * 2 >= han:
        return "KR"
    return "TC" if trad > simp else "SC"


# ---------------- 字符切分与断行规则 ----------------

_EMOJI_BASE = ("\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u2199\u21a9\u21aa\u231a\u231b"
               "\u2328\u23cf\u23e9-\u23f3\u23f8-\u23fa\u24c2\u25aa\u25ab\u25b6\u25c0"
               "\u25fb-\u25fe\u2600-\u27bf\u2934\u2935\u2b05-\u2b07\u2b1b\u2b1c\u2b50\u2b55"
               "\u3030\u303d\u3297\u3299\U0001F000-\U0001FAFF")
_EMOJI_RE = re.compile(
    "[\U0001F1E6-\U0001F1FF]{2}"                        # 国旗
    "|[0-9#*]\ufe0f?\u20e3"                             # 键帽
    f"|[{_EMOJI_BASE}][\ufe0e\ufe0f]?[\U0001F3FB-\U0001F3FF]?[\U000E0020-\U000E007F]{{0,31}}"
    # 现有最长的序列是 4 人家庭(3 个 ZWJ); 封顶防超长串拖垮整形
    f"(?:\u200d[{_EMOJI_BASE}]\ufe0f?[\U0001F3FB-\U0001F3FF]?){{0,7}}")
# 零宽/格式字符: 单独出现时不画(BASIC 排版会画成方框); ZWJ/ZWNJ 留给复杂文字的词内
_ZERO_WIDTH_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u00ad\ud800-\udfff\u200b\u200e\u200f\u2028\u2029"
                            "\u202a-\u202e\u2060-\u2069\ufe00-\ufe0f\ufeff\U000E0000-\U000E007F]")
# BMP 里默认按 emoji 显示的(Emoji_Presentation=Yes), 其余符号不带 FE0F 时先用文字字体
_EMOJI_DEFAULT_BMP = frozenset(
    cp for a, b in (
        (0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0), (0x23F3, 0x23F3),
        (0x25FD, 0x25FE), (0x2614, 0x2615), (0x2648, 0x2653), (0x267F, 0x267F),
        (0x2693, 0x2693), (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
        (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4), (0x26EA, 0x26EA),
        (0x26F2, 0x26F3), (0x26F5, 0x26F5), (0x26FA, 0x26FA), (0x26FD, 0x26FD),
        (0x2705, 0x2705), (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
        (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757), (0x2795, 0x2797),
        (0x27B0, 0x27B0), (0x27BF, 0x27BF), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50),
        (0x2B55, 0x2B55))
    for cp in range(a, b + 1))
# 避头尾: 不放行首 / 不放行尾
_NO_START = frozenset("!%),.:;?]}¢°’”‰′″℃、。〃々〉》」』】〕〗〙〛〞ゝゞ・ー"
                      "！％），．：；？］｝～｡｣､･…‥—–·")
_NO_END = frozenset("([{£¥‘“〈《「『【〔〖〘〚〝（［｛｢")
# 右到左段里成对括号镜像
_MIRROR = dict(zip("()[]{}<>«»", ")(][}{><»«"))


def _emoji_first(cluster: str) -> bool:
    if len(cluster) > 1:
        return "\ufe0e" not in cluster
    cp = ord(cluster)
    if cp >= 0x1F200:
        return True
    if cp >= 0x1F000:       # 麻将/扑克/带圈字母多数默认按文字显示
        return cp in (0x1F004, 0x1F0CF, 0x1F18E) or 0x1F191 <= cp <= 0x1F19A
    return cp in _EMOJI_DEFAULT_BMP


def _is_mark(ch: str) -> bool:
    return unicodedata.category(ch)[0] == "M"


def _is_rtl(ch: str) -> bool:
    o = ord(ch)
    return (0x0590 <= o <= 0x08FF or 0xFB1D <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF
            or 0x10800 <= o <= 0x10FFF or 0x1E800 <= o <= 0x1EFFF)


def _is_word_script(ch: str) -> bool:
    """按空格分词、词内要整形的: 南亚(天城文等)、藏文、蒙古文."""
    o = ord(ch)
    return (0x0900 <= o <= 0x0DFF or 0x0F00 <= o <= 0x0FFF or 0x1800 <= o <= 0x18AF
            or 0xA8E0 <= o <= 0xA8FF or 0x11000 <= o <= 0x11FFF)


def _is_tight_script(ch: str) -> bool:
    """词间不写空格的: 泰/老/缅/高棉. 没有词典, 按字素簇单位, 只在一行放不下时断."""
    o = ord(ch)
    return (0x0E00 <= o <= 0x0EFF or 0x1000 <= o <= 0x109F or 0x1780 <= o <= 0x17FF
            or 0x1A20 <= o <= 0x1AAF or 0xAA60 <= o <= 0xAA7F or 0xA9E0 <= o <= 0xA9FF)


# 单位标记: 要整形 / 右到左 / 泰文类(彼此不断行)
_U_SHAPE, _U_RTL, _U_TIGHT = 1, 2, 4


def _plain_units(s: str):
    n, i = len(s), 0
    while i < n:
        ch = s[i]
        if ch in "\u200c\u200d":
            i += 1
            continue
        j = i + 1
        if _is_rtl(ch):
            while j < n and (_is_rtl(s[j]) or _is_mark(s[j]) or s[j] in "\u200c\u200d"):
                j += 1
            flags = _U_SHAPE | _U_RTL
        elif _is_word_script(ch):
            block = ord(ch) >> 7
            while j < n and (ord(s[j]) >> 7 == block or _is_mark(s[j])
                             or s[j] in "\u200c\u200d"):
                j += 1
            flags = _U_SHAPE
        elif _is_tight_script(ch):
            # 高棉/缅文的下加字记号后面跟的字属于同一簇
            while j < n and (_is_mark(s[j]) or s[j] in "\u200c\u200d"
                             or s[j - 1] in "\u17d2\u1039\u1a60"):
                j += 1
            flags = _U_SHAPE | _U_TIGHT
        else:
            while j < n and _is_mark(s[j]):
                j += 1
            flags = _U_SHAPE if j - i > 1 else 0     # 带组合附加符的要 raqm 定位
        yield s[i:j], None, flags
        i = j


def _units(text: str):
    """切成 (单位, emoji 倾向, 标记): 单位是一个字(含组合附加符)、一个复杂文字的词/簇、
    或一整个 emoji 序列; 倾向 True=先用 emoji 字体, False=先文字后 emoji, None=普通字."""
    if not text.isascii():
        text = unicodedata.normalize("NFC", text)   # 越南文等拆开写的附加符先合成
    pos = 0
    for m in _EMOJI_RE.finditer(text):
        yield from _plain_units(_ZERO_WIDTH_RE.sub("", text[pos:m.start()]))
        cluster = m.group(0)
        if _emoji_first(cluster):
            yield cluster, True, 0
        else:
            yield cluster[0], False, 0
        pos = m.end()
    yield from _plain_units(_ZERO_WIDTH_RE.sub("", text[pos:]))


def _wide(ch: str) -> bool:
    o = ord(ch)
    return (0x2E80 <= o <= 0xA4CF or 0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF
            or 0xFE30 <= o <= 0xFE4F or 0xFF00 <= o <= 0xFF60 or 0xFFE0 <= o <= 0xFFE6
            or 0x1100 <= o <= 0x115F or 0x20000 <= o <= 0x3FFFD)


# ---------------- 行内: 解析成 (文字, 样式) 片段 ----------------

# 强调内容: 不是星号的字(反斜杠转义的也算); 粗体里可嵌一层斜体, 斜体里可嵌一层粗体.
# 长度封顶, 没闭合的星号多也是线性
_EC = r"(?:\\.|[^*\\\n])"
_EM_IN = rf"\*(?![\s*]){_EC}{{1,500}}?(?<!\s)\*(?!\*)"
_STRONG_IN = rf"\*\*(?![\s*]){_EC}{{1,500}}?(?<!\s)\*\*"
_INLINE_RE = re.compile(
    r"\\(?P<esc>[\\`*_{}\[\]()#+\-.!~|<>])"
    r"|`(?P<code>[^`\n]+)`"
    r"|!\[(?P<alt>[^\[\]]*)\]\([^)]*\)"
    r"|\[(?P<label>[^\[\]]+)\]\(\s*(?P<href>https?://[^\s)]+)[^)]*\)"
    rf"|(?P<url>{_URL_RE.pattern})"
    r"|~~(?P<del>[^\s~](?:[^~]*[^\s~])?)~~"
    rf"|\*\*\*(?P<strongem>(?![\s*]){_EC}{{1,500}}?)(?<!\s)\*\*\*(?!\*)"
    rf"|\*\*(?P<strong>(?![\s*])(?:{_EC}|{_EM_IN}){{1,500}}?)(?<!\s)\*\*(?!\*)"
    rf"|(?<![*A-Za-z0-9_\\])\*(?P<em>(?![\s*])(?:{_EC}|{_STRONG_IN}){{1,500}}?)(?<!\s)\*(?!\*)"
    r"|⟦(?P<sup>\d+)⟧")         # sender 给链接编的角标
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)


def _inline_pieces(raw: str, st: _Style, out: list) -> None:
    pos = 0
    for m in _INLINE_RE.finditer(raw):
        if m.start() > pos:
            out.append((raw[pos:m.start()], st))
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "esc":
            out.append((value, st))
        elif kind == "code":
            out.append((value, replace(st, code=True)))
        elif kind == "alt":
            # 图片已被 sender 抽走, 兜底显示说明文字
            out.append((f"[{value.strip() or '图片'}]", replace(st, color=_MUTED)))
        elif kind == "href":
            _inline_pieces(m.group("label"), replace(st, link=True), out)
        elif kind == "url":
            url = value.rstrip(_URL_TAIL_PUNCT)
            out.append((url or value, replace(st, link=True)))
            if url and len(url) < len(value):
                out.append((value[len(url):], st))
        elif kind == "del":
            _inline_pieces(value, replace(st, strike=True), out)
        elif kind == "strongem":
            _inline_pieces(value, replace(st, bold=True, italic=True), out)
        elif kind == "strong":
            _inline_pieces(value, replace(st, bold=True), out)
        elif kind == "em":
            _inline_pieces(value, replace(st, italic=True), out)
        elif kind == "sup":
            out.append((f"[{value}]", replace(st, sup=True)))
    if pos < len(raw):
        out.append((raw[pos:], st))


# ---------------- 排版单位 ----------------

_K_TEXT, _K_EMOJI, _K_SPACE = 0, 1, 2
_BR = object()                      # 强制换行


class _It:
    """一个排版单位(字/词/簇/emoji/空格); pl/pr 是行内代码两端的内边距."""
    __slots__ = ("k", "s", "st", "font", "face", "adv", "pl", "pr", "cid", "sw", "dy",
                 "px", "flags")


_resolved: dict[tuple, tuple] = {}


def _px(st: _Style) -> int:
    if st.code:
        return round(st.size * 0.85)
    if st.sup:
        return round(st.size * 0.75)
    return st.size


def _direction(flags: int) -> str | None:
    if not _RAQM or not flags & _U_SHAPE:
        return None
    return "rtl" if flags & _U_RTL else "ltr"


def _with_face(face: _Face, unit: str, px: int, bold: bool, flags: int, region: str) -> tuple:
    """(种类, 字体, 字面, 前进宽, 描边宽): 粗体用同族粗体面, 没有就描边."""
    sw = 0
    if bold:
        reg, reg_bold = _cjk(region)
        heavy = reg_bold if face is reg else _bold_sibling(face)
        if heavy is not None and heavy.covers(unit):
            face = heavy
        else:
            sw = max(1, round(px / 32))
    font = face.at(px, bool(flags & _U_SHAPE))
    adv = font.getlength(unit, direction=_direction(flags)) if _direction(flags) \
        else font.getlength(unit)
    kind = _K_SPACE if unit == " " else _K_TEXT
    return kind, font, face, adv + sw, sw


def _resolve(unit: str, emo, flags: int, px: int, bold: bool, mono: bool, region: str) -> tuple:
    fs = _fonts()
    if emo and fs.emoji and fs.emoji.covers(unit):
        return _K_EMOJI, None, None, fs.emoji.advance(unit, px), 0
    main = _cjk(region)[0]
    faces = ([fs.mono] if mono and fs.mono else []) + [main]
    # 未分配/私用区/代理项: 只看主字体, 不挨个字体查
    blank = len(unit) == 1 and unicodedata.category(unit) in ("Cn", "Co", "Cs")
    # 带 emoji 倾向的符号(☀ ❤ 等): 主字体没有时彩色优先于黑白符号
    if emo is None and not blank:
        faces += fs.chain
    face = next((f for f in faces if f.covers(unit)), None)
    if face is None and blank:
        face = main                         # 方框
    if face is None and emo is None:
        face = _dyn_face(unit)
    if face is None:
        if fs.emoji and fs.emoji.covers(unit):
            return _K_EMOJI, None, None, fs.emoji.advance(unit, px), 0
        face = next((f for f in fs.chain if f.covers(unit)), None) or main   # 都没有: 方框
    return _with_face(face, unit, px, bold, flags, region)


def _item(unit: str, emo, flags: int, st: _Style, cid: int = 0) -> _It:
    px = _px(st)
    mono = st.code or st.pre
    key = (unit, emo, flags, px, st.bold, mono, st.region)
    res = _resolved.get(key)
    if res is None:
        if len(_resolved) > 50000:
            _resolved.clear()
        res = _resolved[key] = _resolve(unit, emo, flags, px, st.bold, mono, st.region)
    it = _It()
    it.k, it.font, it.face, it.adv, it.sw = res
    it.s, it.st, it.px, it.cid, it.flags = unit, st, px, cid, flags
    it.pl = it.pr = 0.0
    it.dy = -round(px * 0.5) if st.sup else 0
    return it


def _unify(items: list) -> None:
    """一个拉丁/希腊/西里尔词里混了两种字体(主字体缺几个越南文字母等): 整词换成都有的那个."""
    run: list[_It] = []
    for it in items + [None]:
        if it is not None and it is not _BR and it.k == _K_TEXT and not it.flags \
                and not it.st.code and not it.st.pre and it.s.isalpha() and not _wide(it.s):
            run.append(it)
            continue
        if len(run) > 1 and len({r.face for r in run}) > 1:
            region = run[0].st.region
            fs = _fonts()
            cands = [_cjk(region)[0]] + fs.chain + list(_dyn_faces.values())
            face = next((f for f in cands if all(f.covers(r.s) for r in run)), None)
            if face is not None:
                for r in run:
                    r.k, r.font, r.face, r.adv, r.sw = _with_face(
                        face, r.s, r.px, r.st.bold, 0, region)
        run = []


def _items(pieces: list) -> list:
    out: list = []
    cid = 0
    for text, st in pieces:
        if text == "\n":
            out.append(_BR)
            continue
        start = len(out)
        if st.code:
            cid += 1
        for unit, emo, flags in _units(text.replace("\t", " ").replace("\n", " ")):
            if unit == " " and (not out or out[-1] is _BR or out[-1].k == _K_SPACE):
                continue                    # 连续空白折叠, 同 HTML
            out.append(_item(unit, emo, flags, st, cid if st.code else 0))
        if st.code and len(out) > start:
            pad = round(_px(st) * 0.4)
            out[start].pl = pad
            out[-1].pr = pad
    _unify(out)
    return out


def _width(it: _It) -> float:
    return it.adv + it.pl + it.pr


def _can_break(a: _It, b: _It) -> bool:
    if b.k == _K_SPACE:
        return False
    if a.k == _K_SPACE:
        return True
    if b.st.sup and not a.st.sup:           # 角标贴着前文
        return False
    if a.flags & b.flags & _U_TIGHT:
        return False
    ca, cb = a.s[-1], b.s[0]
    if cb in _NO_START or ca in _NO_END:
        return False
    return a.k == _K_EMOJI or b.k == _K_EMOJI or _wide(ca) or _wide(cb)


def _make_word(items: list) -> tuple:
    total = sum(_width(it) for it in items)
    core = total
    for it in reversed(items):
        if it.k != _K_SPACE:
            break
        core -= _width(it)
    return items, core, total


def _strong(it: _It) -> str | None:
    if it.flags & _U_RTL:
        return "R"
    if it.k != _K_TEXT:
        return None
    cls = unicodedata.bidirectional(it.s[0])
    return "R" if cls in ("R", "AL") else "L" if cls == "L" else None


class _Text:
    """一段可折行的行内内容: 词序列(None=强制换行), 不折行时最宽行, 最长不可断词,
    段落方向(首个强方向字是右到左就整段右到左, 同 Unicode 双向算法 P2)."""
    __slots__ = ("words", "size", "lh", "natural", "min_w", "region", "rtl")

    def __init__(self, items: list, size: int, lh: float, region: str):
        self.size, self.lh, self.region = size, lh, region
        self.rtl = False
        for it in items:
            if it is not _BR:
                d = _strong(it)
                if d:
                    self.rtl = d == "R"
                    break
        words: list = []
        cur: list = []
        prev = None
        for it in items:
            if it is _BR:
                if cur:
                    words.append(_make_word(cur))
                    cur = []
                words.append(None)
                prev = None
                continue
            if cur and _can_break(prev, it):
                words.append(_make_word(cur))
                cur = []
            cur.append(it)
            prev = it
        if cur:
            words.append(_make_word(cur))
        self.words = words
        natural = line = 0.0
        min_w = 0.0
        for word in words:
            if word is None:
                line = 0.0
                continue
            natural = max(natural, line + word[1])
            line += word[2]
            min_w = max(min_w, word[1])
        self.natural, self.min_w = natural, min_w


def _text(raw_lines: list[str], st: _Style, lh: float = 1.5) -> _Text:
    pieces: list = []
    for n, raw in enumerate(raw_lines):
        if n:
            pieces.append(("\n", st))
        for k, part in enumerate(_BR_TAG_RE.split(raw)):
            if k:
                pieces.append(("\n", st))
            _inline_pieces(part, st, pieces)
    return _Text(_items(pieces), st.size, lh, st.region)


def _graphemes(text: str) -> list[str]:
    """粗略的字素簇: 附加符、ZWJ/ZWNJ 及其后一字、virama(合成类 9)后的辅音都并进前一簇."""
    out: list[str] = []
    for ch in text:
        if out and (_is_mark(ch) or ch in "\u200c\u200d" or out[-1][-1] in "\u200c\u200d"
                    or unicodedata.combining(out[-1][-1]) == 9):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _split_wide(it: _It, max_w: float) -> list[_It]:
    """比整行还宽的整形单位(不带空格的长阿拉伯文/天城文等)按字素簇切成放得下的几段,
    每段仍整体整形(段内照样连写)."""
    parts = _graphemes(it.s)
    if len(parts) < 2:
        return [it]
    per = max(1, int(len(parts) * max_w / max(_width(it), 1.0) * 0.9))
    out: list[_It] = []
    for k in range(0, len(parts), per):
        sub = _item("".join(parts[k:k + per]), None, it.flags, it.st, it.cid)
        out.extend(_split_wide(sub, max_w) if per > 1 and _width(sub) > max_w else (sub,))
    return out


def _fit_units(items: list, max_w: float):
    for it in items:
        if it.k == _K_TEXT and len(it.s) > 1 and _width(it) > max_w:
            yield from _split_wide(it, max_w)
        else:
            yield it


def _flow(words: list, max_w: float) -> list[list]:
    """贪心折行; 行尾空格悬挂, 行首空格丢掉, 比整行还长的词逐单位断."""
    lines: list[list] = []
    cur: list = []
    cur_w = 0.0
    for word in words:
        if word is None:
            lines.append(cur)
            cur, cur_w = [], 0.0
            continue
        items, core, total = word
        if cur and cur_w + core > max_w + 0.01:
            lines.append(cur)
            cur, cur_w = [], 0.0
        if not cur:
            skip = 0
            while skip < len(items) and items[skip].k == _K_SPACE:
                skip += 1
            if skip == len(items):
                continue
            if skip:
                items, core, total = _make_word(items[skip:])
            if core > max_w:
                for it in _fit_units(items, max_w):
                    w = _width(it)
                    if cur and cur_w + w > max_w + 0.01 and it.k != _K_SPACE:
                        lines.append(cur)
                        cur, cur_w = [], 0.0
                    if not cur and it.k == _K_SPACE:
                        continue
                    cur.append(it)
                    cur_w += w
                continue
        cur.extend(items)
        cur_w += total
    if cur or not lines:
        lines.append(cur)
    return lines


def _trim(line: list) -> int:
    end = len(line)
    while end and line[end - 1].k == _K_SPACE:
        end -= 1
    return end


def _line_width(line: list) -> float:
    return sum(_width(it) for it in line[:_trim(line)])


def _visual(line: list, rtl: bool) -> tuple[list, list]:
    """一行的显示顺序(Unicode 双向算法的简化版: 中性字随两侧强方向, 右到左语境里的
    数字作内嵌的左到右; 按 L2 从最高层起逐层翻转). 返回 (单位, 层级)."""
    base = 1 if rtl else 0
    para = "R" if rtl else "L"
    kinds = []
    last = para
    for it in line:
        d = _strong(it)
        if d:
            last = d
            kinds.append(d)
        elif it.k == _K_TEXT and unicodedata.bidirectional(it.s[0]) in ("EN", "AN"):
            kinds.append("AN" if last == "R" else "L")
        else:
            kinds.append(None)
    n = len(line)

    def side(k):
        return "R" if k in ("R", "AN") else k

    i = 0
    while i < n:
        if kinds[i] is not None:
            i += 1
            continue
        j = i
        while j < n and kinds[j] is None:
            j += 1
        before = side(kinds[i - 1]) if i else para
        after = side(kinds[j]) if j < n else para
        fill = before if before == after else para
        for k in range(i, j):
            kinds[k] = fill
        i = j
    levels = [2 if k == "AN" else 1 if k == "R" else base + base % 2 for k in kinds]
    order = list(range(n))
    for lev in range(max(levels, default=0), 0, -1):
        i = 0
        while i < n:
            if levels[order[i]] < lev:
                i += 1
                continue
            j = i
            while j < n and levels[order[j]] >= lev:
                j += 1
            order[i:j] = order[i:j][::-1]
            i = j
    return [line[k] for k in order], [levels[k] for k in order]


# ---------------- 块: 解析 ----------------

_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d{1,9}\.)\s+(.*)$")
_FENCE_RE = re.compile(r"^(\s*)```")


def _indent(text: str) -> int:
    return len(text.expandtabs(4)) - len(text.expandtabs(4).lstrip())


def _cells(row: str) -> list[str]:
    return [c.strip().replace("\\|", "|")
            for c in re.split(r"(?<!\\)\|", row.strip().strip("|"))]


def _code_items(line: str, region: str) -> list:
    st = _Style(size=_CODE_PX, region=region, pre=True)
    items = [_item(u, emo, flags, st) for u, emo, flags in _units(line.expandtabs(4))]
    mono = _fonts().mono
    if mono is not None:
        # 等宽字体外的字补足终端宽度(全角两格, 其余一格), 代码里的框线表格才对得齐
        cell = mono.at(_CODE_PX).getlength("M")
        for it in items:
            if it.k == _K_TEXT and it.face is not mono:
                wide = unicodedata.east_asian_width(it.s[0]) in ("W", "F")
                pad = (2 if wide else 1) * cell - it.adv
                if pad > 0:
                    it.pl, it.pr = pad / 2, pad - pad / 2
    return items


_MAX_DEPTH = 8                      # 引用/列表嵌套层数上限, 再深的按纯文本


def _parse(lines: list[str], color: tuple, region: str, depth: int = 0, level: int = 0) -> list:
    """块结构: 标题/段落/代码块/引用/列表/表格/分割线. 列表条目的正文递归解析, 支持嵌套、
    条目内代码块与空行后的段落、懒续行、编号起点. depth 是嵌套层数, level 是列表层数."""
    out: list = []
    i, n = 0, len(lines)
    base = _Style(size=_FS, region=region, color=color)
    if depth > _MAX_DEPTH:
        para = [line.strip() for line in lines if line.strip()]
        return [("p", _text(para, base))] if para else []
    while i < n:
        line = lines[i]
        fence = _FENCE_RE.match(line)
        if fence:
            strip = len(fence.group(1))
            code: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                raw = lines[i]
                lead = len(raw) - len(raw.lstrip(" "))
                code.append(raw[min(strip, lead):])
                i += 1
            i += 1
            out.append(("code", [_code_items(c, region) for c in code]))
            continue
        if not line.strip():
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            rank = len(heading.group(1))
            body = re.sub(r"\s+#+\s*$", "", heading.group(2).strip())
            px = round(_FS * _HEAD_EM[rank])
            st = replace(base, size=px, color=_MUTED if rank == 6 else color, bold=True)
            out.append(("h", rank, _text([body], st, 1.25)))
            i += 1
            continue
        if _HR.match(line):
            out.append(("hr",))
            i += 1
            continue
        if _QUOTE.match(line):
            quoted: list[str] = []
            while i < n and _QUOTE.match(lines[i]):
                quoted.append(re.sub(r"^\s{0,3}>\s?", "", lines[i]))
                i += 1
            out.append(("quote", _parse(quoted, _MUTED, region, depth + 1, level)))
            continue
        if _TABLE_ROW.match(line) and i + 1 < n and _TABLE_SPLIT.match(lines[i + 1] or ""):
            head = _cells(line)
            cols = len(head)
            aligns = []
            for c in (_cells(lines[i + 1]) + [""] * cols)[:cols]:
                aligns.append("center" if c.startswith(":") and c.endswith(":")
                              else "right" if c.endswith(":") else "left")
            i += 2
            rows: list[list[_Text]] = []
            while i < n and _TABLE_ROW.match(lines[i]):
                cells = (_cells(lines[i]) + [""] * cols)[:cols]
                rows.append([_text([c], base) for c in cells])
                i += 1
            head_st = replace(base, bold=True)
            out.append(("table", aligns, [_text([c], head_st) for c in head], rows))
            continue
        if _LIST_ITEM.match(line):
            block, i = _parse_list(lines, i, color, region, depth, level)
            out.append(block)
            continue
        # 段落: 单换行也断行(插件按纯文本习惯)
        para: list[str] = []
        while i < n and lines[i].strip() and not _BLOCK_START.match(lines[i]) \
                and not _HR.match(lines[i]):
            para.append(lines[i].strip())
            i += 1
        if not para:
            para.append(lines[i].strip())
            i += 1
        out.append(("p", _text(para, base)))
    return out


def _dedent(line: str, cols: int) -> str:
    line = line.expandtabs(4)
    return line[min(cols, len(line) - len(line.lstrip(" "))):]


def _parse_list(lines: list[str], i: int, color: tuple, region: str, depth: int,
                level: int) -> tuple:
    """缩进到条目正文位置(至少比记号多 2 列)的行都归这一条, 含代码块和空行后的段落;
    紧跟在文字后、没缩进也不是别的块开头的行是懒续行, 也归这一条."""
    first = _LIST_ITEM.match(lines[i])
    base = _indent(first.group(1))
    ordered = first.group(2).endswith(".")
    start = int(first.group(2)[:-1]) if ordered else 1
    inner = base + 2
    items: list[tuple[list[str], int]] = []     # (正文行, 正文起始列)
    n = len(lines)
    fenced = False

    def same_kind(m: re.Match) -> bool:
        return m.group(2).endswith(".") == ordered

    while i < n:
        line = lines[i]
        if fenced:                          # 条目里的代码块: 到闭合前都归它
            items[-1][0].append(_dedent(line, items[-1][1]))
            fenced = not line.strip().startswith("```")
            i += 1
            continue
        ind = _indent(line)
        m = _LIST_ITEM.match(line)
        if m and ind < inner and not _HR.match(line):
            if ind < base or not same_kind(m):
                break                       # 回到外层, 或换了列表类型
            col = len(line.expandtabs(4)) - len(m.group(3).expandtabs(4))
            items.append(([m.group(3)], col))
            i += 1
            continue
        if not line.strip():
            j = i
            while j < n and not lines[j].strip():
                j += 1
            if j < n:
                nxt = _LIST_ITEM.match(lines[j])
                if _indent(lines[j]) >= inner:
                    items[-1][0].extend([""] * (j - i))
                    i = j
                    continue
                if nxt and _indent(lines[j]) >= base and same_kind(nxt) \
                        and not _HR.match(lines[j]):
                    i = j                   # 空行隔开的下一条, 还是这个列表
                    continue
            break
        body = items[-1][0]
        if ind >= inner:
            body.append(_dedent(line, items[-1][1]))
            fenced = line.strip().startswith("```")
            i += 1
            continue
        if body[-1].strip() and not _BLOCK_START.match(line) and not _HR.match(line):
            body.append(line.strip())       # 懒续行
            i += 1
            continue
        break
    kids = [_parse(body, color, region, depth + 1, level + 1) for body, _ in items]
    return ("list", ordered, start, level, kids, color, region), i


# ---------------- 块: 排版成显示列表 ----------------

class _Ctx:
    """排版结果: ops 绘制指令(第二项都是所在行顶, 截断时按它过滤), cuts 可截断的行底."""

    def __init__(self):
        self.ops: list[tuple] = []
        self.cuts: list[float] = []
        self.right = 0.0
        self.mark: float | None = None     # 列表条目里第一行的基线, 记号对齐用


def _emit_line(ctx: _Ctx, line: list, x: float, top: float, lh: int, size: int,
               region: str, mono: bool = False, rtl: bool = False) -> float:
    asc, desc = _metrics(size, mono, region)
    base = round(top + (lh - asc - desc) / 2 + asc)
    if ctx.mark is None:
        ctx.mark = base
    ops = ctx.ops
    line = line[:_trim(line)]
    if rtl or any(it.flags & _U_RTL for it in line):
        line, levels = _visual(line, rtl)
    else:
        levels = None
    xs = []
    cx = x
    for it in line:
        xs.append(cx)
        cx += _width(it)
    end = len(line)
    # 行内代码底色(折行时每行各画一段)
    i = 0
    while i < end:
        it = line[i]
        if not it.cid:
            i += 1
            continue
        j = i
        while j + 1 < end and line[j + 1].cid == it.cid:
            j += 1
        casc, cdesc = _metrics(it.px, True, region)
        pv = round(it.px * 0.2)
        x0 = xs[i] - (round(it.px * 0.4) if not line[i].pl else 0)
        x1 = xs[j] + _width(line[j]) + (round(it.px * 0.4) if not line[j].pr else 0)
        ops.append(("rect", top, x0, base - casc - pv, x1, base + cdesc + pv,
                    _CODE_BG, 6 * _S))
        i = j + 1
    # 文字按 (字体, 颜色, 描边, 斜体, 上标, 方向) 合并成串, 一串画一次(泰文等一行一次);
    # 右到左的词已按显示顺序排好, 各自单独画
    run: list | None = None
    run_key = None
    last: _It | None = None

    def flush():
        if run:
            ops.append(("text", top, run[0], base + run_key[4], "".join(run[1]),
                        run_key[0], run_key[1], run_key[2], run_key[3], run_key[6]))

    for n, it in enumerate(line):
        gx = xs[n] + it.pl
        if it.k == _K_EMOJI:
            flush()
            run = None
            emoji = _fonts().emoji
            ops.append(("emoji", top, gx, emoji.top(base + it.dy, it.px), it.s, it.px))
            continue
        color = _LINK if it.st.link else it.st.color
        text = it.s
        if levels and levels[n] % 2:
            text = _MIRROR.get(text, text)
        direction = _direction(it.flags)
        if it.flags & _U_RTL:
            flush()
            run = None
            if direction is None:
                text = text[::-1]           # 没有 raqm: 至少把字序倒过来
            ops.append(("text", top, gx, base + it.dy, text, it.font, color, it.sw,
                        it.st.italic, direction))
            continue
        key = (it.font, color, it.sw, it.st.italic, it.dy, it.cid, direction)
        # 带内边距的单位接不进串(串内按自然字宽排)
        if run is not None and key == run_key and not it.pl and not last.pr:
            run[1].append(text)
        else:
            flush()
            run, run_key = [gx, [text]], key
        last = it
    flush()
    # 删除线
    i = 0
    while i < end:
        if not line[i].st.strike:
            i += 1
            continue
        j = i
        while j + 1 < end and line[j + 1].st.strike:
            j += 1
        it = line[i]
        y = base + it.dy - round(it.px * 0.32)
        thick = max(2, round(it.px / 16))
        ops.append(("rect", top, xs[i] + it.pl, y, xs[j] + _width(line[j]) - line[j].pr,
                    y + thick, _LINK if it.st.link else it.st.color, 0))
        i = j + 1
    return base


def _lay_text(ctx: _Ctx, text: _Text, x0: float, x1: float, y: float,
              align: str = "left") -> tuple[float, float]:
    lh = round(text.size * text.lh)
    first = None
    if text.rtl and align == "left":
        align = "right"
    for line in _flow(text.words, x1 - x0):
        width = _line_width(line)
        x = x0
        if align == "center":
            x = x0 + (x1 - x0 - width) / 2
        elif align == "right":
            x = x1 - width
        base = _emit_line(ctx, line, x, y, lh, text.size, text.region, rtl=text.rtl)
        if first is None:
            first = base
        ctx.right = max(ctx.right, x + width)
        y += lh
        ctx.cuts.append(y)
    return y, first


def _lay_heading(ctx, block, x0, x1, y):
    _, level, text = block
    y, _ = _lay_text(ctx, text, x0, x1, y)
    if level <= 2:
        y += round(text.size * 0.3)
        ctx.ops.append(("rect", y, x0, y, x1, y + _S, _RULE, 0))
        y += _S
        ctx.right = max(ctx.right, x1)
    return y


def _lay_para(ctx, block, x0, x1, y):
    return _lay_text(ctx, block[1], x0, x1, y)[0]


def _lay_code(ctx, block, x0, x1, y):
    lh = round(_CODE_PX * 1.45)
    pad = 16 * _S
    inner = x1 - x0 - 2 * pad
    rows: list[list] = []
    for items in block[1]:
        cur: list = []
        w = 0.0
        for it in _fit_units(items, inner):     # 代码逐字折行, 不挑断点
            if cur and w + _width(it) > inner:
                rows.append(cur)
                cur, w = [], 0.0
            cur.append(it)
            w += _width(it)
        rows.append(cur)
    if not rows:
        rows.append([])
    region = block[1][0][0].st.region if block[1] and block[1][0] else "SC"
    height = len(rows) * lh + 2 * pad
    ctx.ops.append(("rect", y, x0, y, x1, y + height, _PRE_BG, 6 * _S))
    ty = y + pad
    for row in rows:
        _emit_line(ctx, row, x0 + pad, ty, lh, _CODE_PX, region, mono=True)
        ty += lh
        ctx.cuts.append(ty)
    ctx.right = max(ctx.right, x1)
    return y + height


def _lay_quote(ctx, block, x0, x1, y):
    bar, pad = 4 * _S, 16 * _S
    if x1 - x0 - bar - 2 * pad < 8 * _FS:   # 太窄就不再缩进(嵌套很深时)
        return _lay_blocks(ctx, block[1], x0, x1, y)
    top = y
    y = _lay_blocks(ctx, block[1], x0 + bar + pad, x1 - pad, y)
    ctx.ops.append(("rect", top, x0, top, x0 + bar, y, _BORDER, 0))
    return y


def _roman(n: int) -> str:
    out = ""
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
                       (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"),
                       (4, "iv"), (1, "i")):
        while n >= value:
            out, n = out + sym, n - value
    return out


def _alpha(n: int) -> str:
    out = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        out = chr(97 + r) + out
    return out


def _lay_list(ctx, block, x0, x1, y):
    _, ordered, start, level, items, color, region = block
    # 太窄就不再缩进(嵌套很深时)
    indent = 2 * _FS if x1 - x0 - 2 * _FS >= 8 * _FS else 0
    size = _FS
    for n, blocks in enumerate(items):
        if n:
            y += round(_FS * 0.25)
        ix = x0 + indent
        outer, ctx.mark = ctx.mark, None
        top = y
        y = _lay_blocks(ctx, blocks, ix, x1, y)
        base = ctx.mark
        ctx.mark = outer if outer is not None else base
        if base is None:                    # 空条目占一行
            asc, desc = _metrics(size, False, region)
            base = round(top + (round(size * 1.5) - asc - desc) / 2 + asc)
            y = top + round(size * 1.5)
        if ordered:
            num = start + n
            label = (f"{num}." if level == 0 or num > 3999
                     else f"{_roman(num)}." if level == 1 else f"{_alpha(num)}.")
            font = _cjk(region)[0].at(size)
            lx = ix - round(size * 0.25) - font.getlength(label)
            ctx.ops.append(("text", base - size, lx, base, label, font, color, 0, False, None))
        else:
            r = size * 0.16
            cx, cy = ix - size * 0.75, base - size * 0.33
            ctx.ops.append(("dot", base - size, cx - r, cy - r, cx + r, cy + r, color,
                            level == 1, level >= 2))
    return y


def _table_widths(block, avail: float) -> list[float]:
    _, aligns, head, rows = block
    hpad, bw = 13 * _S, _S
    cols = len(head)
    grid = [head] + rows
    nat = [max(r[c].natural for r in grid) + 2 * hpad for c in range(cols)]
    low = [max(r[c].min_w for r in grid) + 2 * hpad for c in range(cols)]
    inner = avail - bw * (cols + 1)
    if sum(nat) <= inner:
        return nat
    if sum(low) >= inner:                   # 最长的词都放不下: 按比例压, 单元格里逐字断
        return [max(2 * hpad + _FS, inner * w / sum(low)) for w in low]
    grow = [a - b for a, b in zip(nat, low)]
    extra = inner - sum(low)
    return [b + extra * g / sum(grow) for b, g in zip(low, grow)]


def _table_need(block) -> float:
    widths = _table_widths(block, float("inf"))
    return sum(widths) + _S * (len(widths) + 1)


def _lay_table(ctx, block, x0, x1, y):
    _, aligns, head, rows = block
    hpad, vpad, bw = 13 * _S, 6 * _S, _S
    widths = _table_widths(block, x1 - x0)
    total = sum(widths) + bw * (len(widths) + 1)
    lh = round(_FS * 1.5)
    top = y
    ctx.ops.append(None)                    # 边框底色占位, 量完高度再填
    frame = len(ctx.ops) - 1
    y += bw
    for r, row in enumerate([head] + rows):
        flows = [_flow(cell.words, w - 2 * hpad) for cell, w in zip(row, widths)]
        height = max(len(f) for f in flows) * lh + 2 * vpad
        # 表头与偶数行灰底, 格线由底下的边框色透出来
        fill = _PRE_BG if r % 2 == 0 else _BG
        cx = x0 + bw
        for cell, cell_lines, w, align in zip(row, flows, widths, aligns):
            ctx.ops.append(("rect", y, cx, y, cx + w, y + height, fill, 0))
            if cell.rtl and align == "left":
                align = "right"
            ly = y + vpad
            for line in cell_lines:
                lw = _line_width(line)
                lx = cx + hpad
                if align == "center":
                    lx += (w - 2 * hpad - lw) / 2
                elif align == "right":
                    lx += w - 2 * hpad - lw
                _emit_line(ctx, line, lx, ly, lh, _FS, cell.region, rtl=cell.rtl)
                ly += lh
            cx += w + bw
        y += height + bw
        ctx.cuts.append(y)
    ctx.ops[frame] = ("rect", top, x0, top, x0 + total, y, _BORDER, 0)
    ctx.right = max(ctx.right, x0 + total)
    return y


def _lay_hr(ctx, block, x0, x1, y):
    ctx.ops.append(("rect", y, x0, y, x1, y + 4 * _S, _BORDER, 0))
    ctx.right = max(ctx.right, x1)
    return y + 4 * _S


_LAYERS = {"h": _lay_heading, "p": _lay_para, "code": _lay_code, "quote": _lay_quote,
           "list": _lay_list, "table": _lay_table, "hr": _lay_hr}
_MARGINS = {"h": (24 * _S, 16 * _S), "hr": (24 * _S, 24 * _S)}
_DEFAULT_MARGIN = (0, 16 * _S)


def _lay_blocks(ctx: _Ctx, blocks: list, x0: float, x1: float, y: float) -> float:
    """块间距按 CSS 外边距折叠取大; 首块不留上边距, 末块不留下边距."""
    prev = None
    for block in blocks:
        mt, mb = _MARGINS.get(block[0], _DEFAULT_MARGIN)
        nested = block[0] == "list" and block[3]
        if nested:
            mt = mb = 0                     # 嵌套列表紧贴条目文字
        if prev is not None:
            y += 0 if nested else max(prev, mt)
        y = _LAYERS[block[0]](ctx, block, x0, x1, y)
        prev = mb
    return y


def _layout(blocks: list, width: int) -> tuple[_Ctx, int]:
    ctx = _Ctx()
    y = _lay_blocks(ctx, blocks, _PAD, width - _PAD, _PAD)
    return ctx, math.ceil(y) + _PAD


def _choose_width(blocks: list) -> int:
    """最小的宽 W 使 高/W <= 目标比, 或已与最大宽时一样矮(再加宽也不变矮, 如短行列表);
    表格不折行要的宽度作下限. 条件对 W 单调, 二分."""
    lo, hi = _WIDTH * _S, _MAX_WIDTH * _S
    need = max((_table_need(b) for b in blocks if b[0] == "table"), default=0)
    lo = min(hi, max(lo, math.ceil(need) + 2 * _PAD))

    def height(w: int) -> int:
        return _layout(blocks, w)[1]

    floor = height(hi) * 1.02

    def ok(w: int) -> bool:
        h = height(w)
        return h <= w * _TARGET_RATIO or h <= floor

    if ok(lo):
        return lo
    while hi - lo > 10 * _S:
        mid = (lo + hi) // 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return hi


# ---------------- 绘制 ----------------

def _draw_italic(img: Image.Image, x: float, base: float, text: str,
                 font: ImageFont.FreeTypeFont, fill: tuple, sw: int,
                 direction: str | None) -> None:
    """仿斜体: 先画到蒙版上, 以基线为轴错切再贴回."""
    asc, desc = font.getmetrics()
    margin = 2 + sw
    kw = {"direction": direction} if direction else {}
    width = math.ceil(font.getlength(text, **kw)) + 2 * margin
    height = asc + desc + 2 * margin
    mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(mask).text((margin, margin + asc), text, font=font, fill=255,
                              anchor="ls", stroke_width=sw, stroke_fill=255, **kw)
    k = _ITALIC_SKEW
    left = math.ceil(k * (desc + margin))
    out_w = width + math.ceil(k * height)
    bl = margin + asc
    sheared = mask.transform((out_w, height), Image.Transform.AFFINE,
                             (1, k, -left - k * bl, 0, 1, 0), Image.Resampling.BICUBIC)
    px, py = round(x) - margin - left, round(base) - bl
    img.paste(fill, (px, py, px + out_w, py + height), sheared)


def _paint(ops: list, width: int, height: int, img: Image.Image | None = None) -> Image.Image:
    if img is None:
        img = Image.new("RGB", (width, height), _BG)
    draw = ImageDraw.Draw(img)
    emoji = _fonts().emoji
    for op in ops:
        if op is None or op[1] >= height:
            continue
        kind = op[0]
        if kind == "rect":
            _, _, x0, y0, x1, y1, fill, radius = op
            box = (round(x0), round(y0), round(x1) - 1, round(y1) - 1)
            if box[2] < box[0] or box[3] < box[1]:
                continue
            radius = min(radius, (box[2] - box[0]) // 2, (box[3] - box[1]) // 2)
            if radius > 0:
                draw.rounded_rectangle(box, radius, fill=fill)
            else:
                draw.rectangle(box, fill=fill)
        elif kind == "text":
            _, _, x, y, text, font, fill, sw, italic, direction = op
            if italic:
                _draw_italic(img, x, y, text, font, fill, sw, direction)
            else:
                draw.text((x, y), text, font=font, fill=fill, anchor="ls",
                          stroke_width=sw, stroke_fill=fill if sw else None,
                          direction=direction)
        elif kind == "emoji":
            _, _, x, y, cluster, px = op
            pic = emoji.image(cluster, px)
            img.paste(pic, (round(x), round(y)), pic)
        elif kind == "dot":
            _, _, x0, y0, x1, y1, fill, hollow, square = op
            if square:
                draw.rectangle((x0, y0, x1, y1), fill=fill)
            elif hollow:
                draw.ellipse((x0, y0, x1, y1), outline=fill, width=max(2, _S))
            else:
                draw.ellipse((x0, y0, x1, y1), fill=fill)
    return img


def _truncated(ctx: _Ctx, width: int, region: str) -> Image.Image:
    """超高: 截在某行底下, 末尾补一行省略提示. 直接画在成图上, 截线以下涂白再写提示."""
    note = _text([_TRUNCATED_NOTE], _Style(size=_FS, region=region, color=_MUTED))
    gap = 16 * _S
    note_h = round(_FS * 1.5) + gap
    limit = _MAX_HEIGHT * _S - _PAD - note_h
    cut = math.ceil(max((c for c in ctx.cuts if c <= limit), default=limit))
    height = cut + note_h + _PAD
    img = _paint([op for op in ctx.ops if op is None or op[1] < cut], width, height)
    ImageDraw.Draw(img).rectangle((0, cut, width, height), fill=_BG)
    tail = _Ctx()
    _lay_text(tail, note, _PAD, width - _PAD, cut + gap)
    return _paint(tail.ops, width, height, img)


def render_png(source: str) -> bytes | None:
    """同步渲染; 没有中文字体或没内容返回 None, 其余异常上抛."""
    global _fc_left
    if _find_fonts() is None:
        return None
    _fc_left = _FC_BUDGET
    cut_source = len(source) > _SOURCE_MAX
    source = source[:_SOURCE_MAX]
    region = _region(source)
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks = _parse(lines, _TEXT, region)
    if not blocks:
        return None
    width = _choose_width(blocks)
    ctx, height = _layout(blocks, width)
    # 内容没铺满就收窄(不窄于起始宽)
    width = max(_WIDTH * _S, min(width, math.ceil(ctx.right) + _PAD))
    if height > _MAX_HEIGHT * _S or cut_source:
        img = _truncated(ctx, width, region)
    else:
        img = _paint(ctx.ops, width, height)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _render_sync(source: str) -> bytes | None:
    # 上一张超时的线程可能还占着锁, 等不到就放弃, 别越堆越多
    if not _render_lock.acquire(timeout=_TIMEOUT):
        logger.warning("textimg: 等渲染锁超时, 长文本保持原样发送")
        return None
    try:
        return render_png(source)
    except Exception:                       # noqa: BLE001 排版/字体任何问题都退回发文字
        logger.exception("textimg: 渲染失败")
        return None
    finally:
        _render_lock.release()


def _cache_put(digest: str, data: bytes) -> None:
    if len(data) > _CACHE_ITEM_MAX:
        return
    _cache[digest] = data
    total = sum(len(v) for v in _cache.values())
    while total > _CACHE_BYTES or len(_cache) > _CACHE_MAX:
        total -= len(_cache.popitem(last=False)[1])


async def render_text_image(source: str) -> bytes | None:
    """文本(可含 markdown) -> PNG; 任何失败(含超时)返回 None, 调用方发原文."""
    try:
        digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
        if digest in _cache:
            _cache.move_to_end(digest)
            return _cache[digest]
        async with _sem:
            if digest in _cache:            # 排队期间已被渲染
                _cache.move_to_end(digest)
                return _cache[digest]
            # 超时只是不等了, 线程照样画完(放锁); 信号量随 async with 退出
            data = await asyncio.wait_for(asyncio.to_thread(_render_sync, source), _TIMEOUT)
        if data:
            _cache_put(digest, data)
        return data
    except TimeoutError:
        logger.warning("textimg: 渲染超时(%ss), 长文本保持原样发送", _TIMEOUT)
        return None
    except Exception:                       # noqa: BLE001
        logger.exception("textimg: 渲染失败")
        return None
