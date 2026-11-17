"""重复消息闸: 同一个 bot 在同一个会话里短时间反复发同一条消息, 超过上限就不再发(防后端死循环刷屏).

- 按内容逐条计数, 不看是否连续: 1、2、3、1、2、3 轮着发的, 三条各自累计。
- 文字、@、表情、卡片等各种段都算; 图片/语音/视频/文件按文件内容认(base64、本地路径), 换个路径或传法照样认得, 外链按地址认。
  文件内容只取总长度 + 开头 256KB, 不把整个文件读进内存。
- 被拦下的也记一次: 循环不停就一直拦, 停够一个窗口自然恢复。
- 上限与窗口是选项 repeat_limit / repeat_window(开关 repeat_guard), 见 config.py。
"""

from __future__ import annotations

import hashlib
import time
from collections import deque

KEEP = 512          # 每个会话最多记这么多条近期外发
PEERS_MAX = 4096    # 会话数到这个量时清掉窗口内没发过的
MEDIA_TYPES = ("image", "record", "video", "file")
MEDIA_HEAD = 1 << 18    # 本地媒体的内容 = 总长度 + 开头这么多字节


def fingerprint(segments: list[dict], media=None) -> bytes:
    """消息内容指纹(哈希). reply 段不算(循环里每轮引用的消息都不同).

    media: MediaStore, 给了就把本地媒体换成文件内容(MediaStore.peek)再算.
    """
    digest = hashlib.blake2b(digest_size=8)
    for seg in segments:
        kind = seg.get("type")
        if kind == "reply":
            continue
        digest.update(f"\x01{kind}".encode())
        data = seg.get("data") or {}
        for key in sorted(data):
            value = data[key]
            digest.update(f"\x00{key}\x00".encode())
            local = (media.peek(value, MEDIA_HEAD) if media is not None and key == "file"
                     and kind in MEDIA_TYPES and isinstance(value, str) else None)
            if local is None:
                digest.update(str(value).encode("utf-8", "surrogatepass"))
            else:
                digest.update(local[0].to_bytes(8, "big") + local[1])
    return digest.digest()


class RepeatGuard:
    def __init__(self) -> None:
        self._sent: dict[tuple[str, str], deque[tuple[float, bytes]]] = {}

    def count(self, chat_type: str, peer_openid: str, mark: bytes,
              window: float, now: float | None = None) -> int:
        """记一次外发(mark = fingerprint), 返回这条消息在窗口内(含这次)发了几次."""
        now = time.monotonic() if now is None else now
        key = (chat_type, peer_openid)
        recent = self._sent.get(key)
        if recent is None:
            if len(self._sent) >= PEERS_MAX:
                self._sent = {k: v for k, v in self._sent.items()
                              if v and now - v[-1][0] <= window}
            recent = self._sent[key] = deque(maxlen=KEEP)
        while recent and now - recent[0][0] > window:
            recent.popleft()
        recent.append((now, mark))
        return sum(1 for _, seen in recent if seen == mark)
