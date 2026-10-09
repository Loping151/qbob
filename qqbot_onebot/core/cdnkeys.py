"""QQ CDN 直链的 rkey: 从入站媒体 URL 里捡最新的, 给转发网页换用.

- 直链 multimedia.nt.qq.com.cn/download?appid=…&fileid=…&rkey=…, 缺 rkey 回 400.
  rkey 按 appid(1407 群图 / 1403 语音 / 1406 私聊图)区分, 同类型全局通用, 不绑文件/群/bot;
  旧 fileid 换上新 rkey 照样可取.
- 群视频(1415)直链不带 fileid, rkey 绑文件: 换给别的 fileid 拿到的仍是 rkey 那个视频(实测).
  不带 fileid 的 rkey 不收也不换, 自己传的视频不拼直链.
- rkey 每条消息单独签发, 寿命 60 分钟; 页面有缓存且懒加载, 超过 RKEY_MAX_AGE 就不用.
- 分片上传 merge 回的 file_uuid 即 fileid, 自己传的图也能拼直链.

平台无接口取 rkey, 只能捡; 进程内所有 bot 共用. 只用标准库: 外部项目也会 import
这个模块, 改接口要当心.
"""

from __future__ import annotations

import json
import re
import time

CDN_DOWNLOAD = "https://multimedia.nt.qq.com.cn/download"
RKEY_MAX_AGE = 45 * 60

# (上传场景, file_type) -> 直链 appid. 群语音是 silk(浏览器放不了)、文件无直链;
# 私聊 rkey 太少见, 私聊不走直链.
APPID_BY_UPLOAD = {("group", 1): "1407"}

_APPID_RE = re.compile(r"[?&]appid=(\d+)")
_RKEY_RE = re.compile(r"([?&])rkey=[^&]*")
_RKEY_VALUE_RE = re.compile(r"[?&]rkey=([A-Za-z0-9_\-]+)")
_FILEID_RE = re.compile(r"[?&]fileid=")

# appid -> (rkey, 捡到的时间)
_keys: dict[str, tuple[str, float]] = {}


def is_cdn(url: str) -> bool:
    return url.startswith(CDN_DOWNLOAD + "?")


def appid_of(url: str) -> str:
    match = _APPID_RE.search(url) if is_cdn(url) else None
    return match.group(1) if match else ""


def note(url: str, now: float | None = None) -> bool:
    """记下入站 URL 里的 rkey, 返回是否变化(决定要不要落盘)."""
    appid = appid_of(url) if _FILEID_RE.search(url) else ""
    match = _RKEY_VALUE_RE.search(url) if appid else None
    if not match:
        return False
    rkey = match.group(1)
    old = _keys.get(appid)
    _keys[appid] = (rkey, time.time() if now is None else now)
    return old is None or old[0] != rkey


def fresh(now: float | None = None) -> dict[str, str]:
    """还敢用的 rkey: {appid: rkey}."""
    now = time.time() if now is None else now
    return {appid: rkey for appid, (rkey, seen) in _keys.items()
            if now - seen <= RKEY_MAX_AGE}


def with_rkey(url: str, rkey: str) -> str:
    """换上(或补上)rkey, 别的参数原样; 不带 fileid 的原样返回(rkey 即文件)."""
    if not _FILEID_RE.search(url):
        return url
    if _RKEY_RE.search(url):
        return _RKEY_RE.sub(lambda m: f"{m.group(1)}rkey={rkey}", url, count=1)
    return f"{url}&rkey={rkey}"


def cdn_url(appid: str, fileid: str) -> str:
    """自己传的文件 -> 不带 rkey 的直链(渲染时补). 图片带 spec=0, 页面换 spec 取中/小图."""
    url = f"{CDN_DOWNLOAD}?appid={appid}&fileid={fileid}"
    return url + "&spec=0" if appid in ("1407", "1406") else url


def raw_url_until(url: str) -> int:
    """merge 回的 raw_url 到期时间: 取 q-sign-time=起;止 的止(实际 1 小时, 非文档说的
    一天), 解不出按 1 小时; 留 60 秒余量."""
    match = re.search(r"q-sign-time=\d+(?:%3B|;)(\d+)", url)
    return int(match.group(1)) - 60 if match else int(time.time()) + 3540


def dump() -> str:
    return json.dumps(_keys)


def load(blob: str | None) -> None:
    """启动时从 kv 读回, 重启后到下一张入站图前仍有 rkey 可用."""
    try:
        data = json.loads(blob or "{}")
    except ValueError:
        return
    for appid, value in data.items():
        try:
            rkey, seen = str(value[0]), float(value[1])
        except (TypeError, ValueError, IndexError):
            continue
        if appid not in _keys or _keys[appid][1] < seen:
            _keys[appid] = (rkey, seen)


def reset() -> None:
    """测试用."""
    _keys.clear()
