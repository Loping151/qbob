"""本地媒体库: 内容寻址落盘, 按 TTL 清理, 发送时分片直传.

用 media://{token} 在进程内引用, 不对外提供下载: 发出去只走分片直传.
后端给的本地文件超过 copy_max_mb 时不复制, 链接原文件(见 MediaStore.link_file).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import secrets
import stat
import time
from pathlib import Path

logger = logging.getLogger("qqbot.media")

_SUFFIX_BY_MAGIC = [
    (b"\x89PNG", ".png"),
    (b"\xff\xd8", ".jpg"),
    (b"GIF8", ".gif"),
    (b"RIFF", ".webp"),
    (b"\x1a\x45\xdf\xa3", ".webm"),
    (b"\x00\x00\x00", ".mp4"),  # ftyp box (粗略)
    (b"ID3", ".mp3"),
    (b"OggS", ".ogg"),
    (b"\x02#!SILK_V3", ".silk"),
    (b"#!SILK_V3", ".silk"),
    (b"%PDF", ".pdf"),
    (b"PK\x03\x04", ".zip"),
]


# 平台把 URL 末段原样当文件名(不做百分号解码): 中文保持原样, 只转义破坏 URL 结构的字符
_URL_ESCAPE = {"%": "%25", "#": "%23", "?": "%3F", " ": "%20",
               "/": "%2F", "\\": "%5C", '"': "%22"}


def url_safe_name(name: str) -> str:
    name = Path(name).name
    return "".join(_URL_ESCAPE.get(ch, ch) for ch in name if ch >= " ")


def sniff_suffix(data: bytes, fallback: str = ".bin") -> str:
    for magic, suffix in _SUFFIX_BY_MAGIC:
        if data[: len(magic)] == magic:
            return suffix
    # 无魔数: 能按 UTF-8 解就当纯文本
    try:
        data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return fallback
    return ".txt"


# 本地媒体的引用前缀, 仅本进程有效
LOCAL_SCHEME = "media://"


def _local_path(file_field: str) -> Path | None:
    """file:// 或本地绝对路径 -> Path; 其它形式返回 None(不查文件是否存在)."""
    if file_field.startswith("file://"):
        return Path(file_field[7:])
    if file_field.startswith("/"):
        return Path(file_field)
    return None


def _refresh(path: Path) -> None:
    """续期(改修改时间); 符号链接只改链接自己, 不碰后端的原文件."""
    os.utime(path, follow_symlinks=not path.is_symlink())


class MediaStore:
    def __init__(self, media_dir: str, ttl_hours: int = 1, copy_max_mb: float = 10):
        self.dir = Path(media_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl_seconds = ttl_hours * 3600
        # 本地文件超过这个字节数就链接不复制; 0 = 一律链接
        self.copy_max_bytes = max(0.0, float(copy_max_mb)) * 1024 * 1024

    def put_bytes(self, data: bytes, suffix: str = "") -> str:
        """存字节, 返回 token(文件名); 内容寻址, 不重复落盘."""
        if not suffix:
            suffix = sniff_suffix(data)
        digest = hashlib.sha256(data).hexdigest()[:24]
        token = f"{digest}{suffix}"
        path = self.dir / token
        # 同名的若是符号链接(链接进来的文件), 换成实体: 原文件可能已被删或改写
        if path.is_symlink() or not path.exists():
            tmp = self.dir / f".tmp-{secrets.token_hex(8)}"
            tmp.write_bytes(data)
            tmp.replace(path)
        else:
            _refresh(path)
        return token

    def link_file(self, source: Path, suffix: str = "") -> str:
        """不复制, 把后端的本地文件链进媒体库, 返回 token(与 put_bytes 同样按内容哈希命名).

        同一文件系统用硬链接: 不写数据, 后端删掉原文件也不影响; 硬链接和原文件是同一个 inode,
        续期会连带刷新原文件的修改时间. 跨文件系统(或无权硬链接)退回符号链接, 原文件被删后
        这条媒体就当已过期. 两种都怕后端原地改写原文件: 已入库的内容会跟着变.
        """
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            head = handle.read(4096)
            digest.update(head)
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        token = f"{digest.hexdigest()[:24]}{suffix or sniff_suffix(head)}"
        path = self.dir / token
        if path.is_file() and not path.is_symlink():
            _refresh(path)          # 已有实体(复制或硬链接过): 续期即可
            return token
        tmp = self.dir / f".tmp-{secrets.token_hex(8)}"
        try:
            os.link(source, tmp)
        except OSError:
            os.symlink(source.resolve(), tmp)
        else:
            _refresh(tmp)           # inode 的时间是原文件的, 不刷新会被 cleanup 当成过期
        tmp.replace(path)
        return token

    def put_named(self, token: str, data: bytes) -> str:
        """按指定名字存(如语音转码结果按源 URL 记); 已有就续期, 别刚交出去就被清理掉."""
        existing = self.resolve(token)
        if existing is not None:
            _refresh(existing)
        elif "/" not in token and not token.startswith("."):
            tmp = self.dir / f".tmp-{secrets.token_hex(8)}"
            tmp.write_bytes(data)
            tmp.replace(self.dir / token)
        return token

    def resolve(self, token: str) -> Path | None:
        if "/" in token or "\\" in token or token.startswith("."):
            return None
        path = self.dir / token
        return path if path.is_file() else None

    def ref(self, token: str, filename: str = "") -> str:
        """本地媒体引用 media://token[/文件名](仅本进程有效)."""
        return LOCAL_SCHEME + token + (f"/{url_safe_name(filename)}" if filename else "")

    def ingest_file_field(
        self, file_field: str, name_hint: str = ""
    ) -> tuple[str | None, str | None]:
        """OneBot file 字段 -> (引用 URL, token), 无法处理返回 (None, None).

        支持 http(s)(原样返回) / base64:// / file:// / 本地绝对路径; 扩展名优先取 name_hint.
        """
        if not file_field:
            return None, None
        if file_field.startswith(("http://", "https://")):
            return file_field, None
        source = _local_path(file_field)
        if source is not None:
            try:
                if source.stat().st_size > self.copy_max_bytes:
                    name = name_hint or source.name
                    token = self.link_file(source, Path(name).suffix)
                    return self.ref(token, name), token
            except OSError:
                pass                # 不存在/读不了: 交给下面按原逻辑判
        data, source_name = self.load_bytes(file_field)
        if data is None:
            return None, None
        name = name_hint or source_name
        token = self.put_bytes(data, Path(name).suffix)
        return self.ref(token, name), token

    def load_bytes(self, file_field: str) -> tuple[bytes | None, str]:
        """file 字段 -> (字节, 原文件名), 不落盘; 外站 URL 或认不出返回 (None, "")."""
        if file_field.startswith(LOCAL_SCHEME):
            token = self.token_of_url(file_field)
            path = self.resolve(token) if token else None
            return (path.read_bytes(), path.name) if path else (None, "")
        if file_field.startswith("base64://"):
            try:
                return base64.b64decode(file_field[9:], validate=False), ""
            except (binascii.Error, ValueError):
                logger.warning("invalid base64 media payload")
                return None, ""
        path = _local_path(file_field)
        if path is not None and path.is_file():
            return path.read_bytes(), path.name
        return None, ""

    def peek(self, file_field: str, limit: int) -> tuple[int, bytes] | None:
        """file 字段 -> (总字节数, 开头 limit 字节), 只读开头、不把整个文件读进内存; 外站 URL 或认不出返回 None."""
        if file_field.startswith("base64://"):
            chars = -(-limit // 3) * 4          # 只解开头这一段
            try:
                head = base64.b64decode(file_field[9:9 + chars], validate=False)
            except (binascii.Error, ValueError):
                return None
            return (len(file_field) - 9) * 3 // 4 - file_field[-2:].count("="), head[:limit]
        if file_field.startswith(LOCAL_SCHEME):
            path = self.resolve(self.parse_token(file_field) or "")
        else:
            path = _local_path(file_field)
        try:
            with path.open("rb") as handle:
                return path.stat().st_size, handle.read(limit)
        except (OSError, AttributeError):       # 外链/认不出(None)、不存在、不是文件、没权限
            return None

    def parse_token(self, url: str) -> str | None:
        """URL 指向本媒体库则返回 token; 只看形状, 不查文件是否存在."""
        if not url:
            return None
        if url.startswith(LOCAL_SCHEME):
            return url[len(LOCAL_SCHEME):].split("/", 1)[0] or None
        return None

    def token_of_url(self, url: str) -> str | None:
        """URL 指向本媒体库且文件还在 -> token, 否则 None."""
        token = self.parse_token(url)
        return token if token and self.resolve(token) is not None else None

    def is_local(self, url: str) -> bool:
        return self.parse_token(url) is not None

    def size_of_url(self, url: str) -> int:
        """本媒体库文件的字节大小, 外链/找不到返回 0."""
        token = self.token_of_url(url)
        path = self.resolve(token) if token else None
        try:
            return path.stat().st_size if path else 0
        except OSError:
            return 0

    def cleanup(self) -> int:
        cutoff = time.time() - self.ttl_seconds
        removed = 0
        for path in self.dir.iterdir():
            try:
                # lstat: 符号链接按链接自己的时间算(原文件的时间不归我们管), 悬空的也照样清
                info = path.lstat()
                if ((stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))
                        and info.st_mtime < cutoff):
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed
