"""用户头像: 优先平台下发的 author.avatar, 空或失效才退回自拼的 q.qlogo.cn/qqapp.

author.avatar 是 2026-11-13 起群消息带的(文档未写): `thirdqq.qlogo.cn/bqqthird?key=…&kti=…&s=640`,
key 每条消息都换, 与自拼链接是同一张图. 实测(2026-11-15):
- `s=` 换尺寸可用: 0=原图(1080), 100, 640; 去掉 s 回 400.
- key 失效/被改回 **400**(X-ErrNo -104) 而不是 404, 故凡非 200 图片都算失效.
- 41 小时前的链接仍 200, 实际寿命未知; HEAD 可用.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from ..idmap import MapEntry

logger = logging.getLogger("qqbot.avatars")

_SIZE_RE = re.compile(r"([?&])s=\d*")
OK_TTL = 600          # 验过能用的, 这么久内不再验
BAD_TTL = 3600        # 验过失效的(同一链接); 新消息会带来新链接, 不受此限
PROBE_TIMEOUT = 4.0
_CACHE_MAX = 4096


def size_spec(size: int) -> str:
    """OneBotAdditional 的 size -> 链接尺寸: 0=原图, ≤100 取 100, 其余 640."""
    return "0" if size == 0 else ("100" if size <= 100 else "640")


def fallback_url(appid: str, openid: str, spec: str = "100") -> str:
    return f"https://q.qlogo.cn/qqapp/{appid}/{openid}/{spec}"


def sized(url: str, spec: str) -> str:
    """替换(或补上) s= 参数; key 原样保留, 不重新编码."""
    if _SIZE_RE.search(url):
        return _SIZE_RE.sub(lambda m: f"{m.group(1)}s={spec}", url, count=1)
    return f"{url}{'&' if '?' in url else '?'}s={spec}"


def candidates(entry: "MapEntry", spec: str = "100") -> tuple[str, str]:
    """(首选, 备用): 有下发的头像链接时首选它、备用自拼; 没有时首选自拼、备用为空."""
    own = fallback_url(entry.bot_appid, entry.openid, spec)
    if entry.avatar:
        return sized(entry.avatar, spec), own
    return own, ""


class AvatarProbe:
    """验下发的头像链接还能不能用(HEAD), 结果按链接缓存."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[bool, float]] = {}

    async def alive(self, http, url: str) -> bool:
        now = time.monotonic()
        hit = self._cache.get(url)
        if hit is not None and now - hit[1] < (OK_TTL if hit[0] else BAD_TTL):
            return hit[0]
        ok = await self._probe(http, url)
        if len(self._cache) >= _CACHE_MAX:
            self._cache.clear()
        self._cache[url] = (ok, now)
        return ok

    @staticmethod
    async def _probe(http, url: str) -> bool:
        if http is None:
            return False
        try:
            async with http.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(
                    total=PROBE_TIMEOUT)) as resp:
                ctype = resp.headers.get("Content-Type", "")
                ok = resp.status == 200 and ctype.startswith("image/")
                if not ok:
                    logger.info("下发头像链接失效(%s %s), 改用自拼: %s",
                                resp.status, resp.headers.get("X-ErrNo", ""), url[:80])
                return ok
        except Exception as exc:  # 超时/连不上/aiohttp.ClientError
            logger.info("验头像链接失败(%r), 改用自拼: %s", exc, url[:80])
            return False


# 进程内共用: 多个 bot 查同一人的头像只验一次
PROBE = AvatarProbe()


async def resolve(entry: "MapEntry", spec: str, http, probe: AvatarProbe) -> str:
    """给插件的头像: 下发链接验过能用就用它, 否则自拼."""
    first, backup = candidates(entry, spec)
    if not backup:
        return first
    return first if await probe.alive(http, first) else backup
