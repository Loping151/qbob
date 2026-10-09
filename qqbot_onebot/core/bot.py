"""单个 QQ 官方 bot 实例: 事件管线 + 每群状态 + 发送.

入站管线顺序:
1. 记账(id 映射/成员缓存/消息入库), 无论是否合规都做;
2. 合规总闸(按群): 主动消息已开通 且 接收全部消息, 否则全静默(含内置指令).
   主动消息看 bot_state 或 GROUP_MSG_RECEIVE/REJECT 事件; 接收全部看
   recv_msg_setting=all 或"收到过非 @ 群消息"(改设置平台不推事件). 谁更晚听谁的;
3. 内置指令 —— 不受黑白名单限制, 消费后不转发;
4. 黑白名单(群默认白名单, 私聊默认黑名单);
5. 构造 OneBot 事件广播给所有 onebot 端点.

私聊无 bot_state 接口, 不设合规闸; 主动私聊许可由 C2C_MSG_RECEIVE/REJECT 维护.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qs, urlparse

import aiohttp

from ..db import Database
from ..idmap import IdMap
from ..qq.api import QQApiClient, QQApiError
from ..onebot import events as ob_events
from ..plugin import HookContext, registry as plugins
from .access import AccessControl
from .builtin import BuiltinCommands, match_builtin
from .media import MediaStore
from .messages import MessageStore
from .passthrough import Passthrough
from .quota import QuotaTracker
from .sender import Sender
from .unparsed import UnparsedLog, files as unparsed_files
from . import cdnkeys
from .uploader import MediaUploader

# rkey 落盘节流(所有 bot 共用一份 rkey, 见 cdnkeys)
_rkey_saved = [0.0]
_rkey_tasks: set = set()

if TYPE_CHECKING:
    from ..onebot.ws_client import OneBotLink

logger = logging.getLogger("qqbot.bot")

STATE_REFRESH_MIN_INTERVAL = 30  # 秒/群, bot_state 回查节流(接口限 30 QPM)
# 未知态(接口从未成功且无开关事件)的失败退避, 到期由下一条群消息驱动重查;
# 否则一次瞬时失败就让合规群永久静默.
STATE_FAILURE_RETRY_INTERVAL = 60
# 每 bot 的 bot_state 全局预算(平台限 30 QPM, 留余量), 挡跨群聚合风暴;
# 超额按一次失败处理(推迟), 不打接口.
BOT_STATE_QPM_BUDGET = 24
# bot 被提成管理员平台不推事件; 需要角色时缓存超过这个时长就刷一次.
ROLE_FRESH_SECONDS = 300
# 连续多少轮 invalid appid or secret 后自动禁用(这种错误不会自愈)
CREDENTIAL_DISABLE_AFTER = 3
# 定期跟平台身份(名字/头像/QQ号), 号主改昵称不必等重启
IDENTITY_REFRESH_INTERVAL = 3600
# 未落库消息 id 的去重记忆(有界). 平台重推只在投递后几秒内; 落库的由 store.seen 兜.
HANDLED_MSGS_MAX = 1024
# 收到但没下发的消息, 同一会话同一原因多久记一次日志
DROP_LOG_INTERVAL = 600
# 开机补查未知状态群: 延后避开开机流量, 单轮上限防千群 bot 打爆接口
STATE_SWEEP_DELAY = 30
STATE_SWEEP_MAX = 50

# "bot 已不在该群"的错误码: 11293 非群成员(退群后 bot_state 报它), 11255 无效
# group_openid, 及发送侧同义两个. 收到即作废该群状态 —— GROUP_DEL_ROBOT 丢了时
# 这是唯一的自愈路径.
GROUP_GONE_CODES = {11293, 11255, 40054003, 40034101}

# 正在处理的平台原始事件(透传用), 每个事件在自己的 task 里. sent: 已透传过
_RAW_EVENT: ContextVar[dict | None] = ContextVar("qqbot_raw_event", default=None)
# 这些事件在转成 OneBot 事件下发时顺带透传(同一道闸); 其余只看名单
_GATED_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
    "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "FRIEND_ADD", "GROUP_MEMBER_ADD",
    "GROUP_MEMBER_REMOVE", "GROUP_JOIN_REQUEST", "INTERACTION_CREATE",
}


# QQ 群消息里的 at 标记: <@openid> / <@!openid>; @全体成员另有几种写法
_AT_TAG_RE = re.compile(
    r"<@!?([^>\s]+)>"                      # <@openid> / <@!openid>
    r"|<qqbot-at-user\s+id=\"([^\"]+)\"\s*/?>"   # markdown 形式回显
    r"|(<qqbot-at-everyone\s*/?>)"          # @全体(频道形式)
    r"|(@everyone)\b"                       # @全体(旧写法)
)
# openid 是定长十六进制串; 不像 openid 的 at 目标不建映射, 免得造出假用户
_OPENID_RE = re.compile(r"^[0-9A-Fa-f]{16,64}$")
# QQ 表情: <faceType=1,faceId="5",ext="<base64 的 {"text":"..."}>">
# faceType=6 是图片占位(faceId=附件序号), 文件已在 attachments 里, 丢掉;
# faceId 为空的(如 faceType=4 的 [猪])没有 OneBot 表情 id, 转成 ext 里的文字
_FACE_TAG_RE = re.compile(
    r"<faceType=(\d+),\s*faceId=\"(\d*)\"(?:,\s*ext=\"([^\"]*)\")?\s*>")
_AT_ALL_TOKENS = {"everyone", "all", "全体成员"}
# 富媒体在 content 里的内联占位, 文件已在 attachments 里, 标记纯噪音:
# <attachmentType="image/gif",attachmentIndex=0,description="<base64 的 {"text":..}>">
_ATTACHMENT_TAG_RE = re.compile(
    r"<attachmentType=\"[^\"]*\"\s*,\s*attachmentIndex=\d+"
    r"(?:\s*,\s*description=\"[^\"]*\")?\s*>")

# 引用"本身就是引用"的消息时, 平台不给 msg_elements, 而是把链路渲染成纯文本
# 塞进 content, idx 为一次性 TMP_<uuid>:
#     === 消息 1 ===
#     [消息内容]   宝宝早安            <- 被引用那条自己说的话(只取这个)
#     [消息类型] 引用消息
#     [关联消息]
#     --- 第1条 ---
#         [消息内容] 已经醒啦…         <- 更早的链路, 丢弃
# 分隔用 [ \t]* 而非 \s*: 纯图片时 [消息内容] 后为空, \s* 会吃掉换行捞到下一行
_QUOTE_BLOB_HEAD_RE = re.compile(r"===[ \t]*消息[ \t]*\d+[ \t]*===")
_QUOTE_BLOB_RE = re.compile(
    r"===[ \t]*消息[ \t]*\d+[ \t]*===[ \t]*\r?\n[ \t]*\[消息内容\][ \t]*(.*?)"
    r"(?=\r?\n[ \t]*(?:\[消息类型\]|\[关联消息\]|---[ \t]*第|===[ \t]*消息)|\Z)",
    re.S,
)
# 一次性 idx 不能当别名回写, 否则冲掉那条消息真正的 REFIDX_
_STABLE_IDX_PREFIX = "REFIDX_"


def unwrap_quote_blob(text: str) -> str:
    """平台渲染的"引用的引用" -> 被引用那条自己的内容; 认不出就原样返回.

    只处理单段; 多段的是合并转发, 整段保留.
    """
    if len(_QUOTE_BLOB_HEAD_RE.findall(text)) != 1:
        return text
    match = _QUOTE_BLOB_RE.search(text)
    return match.group(1).strip() if match else text


# 平台把合并转发渲染成纯文本时的形状(见 _rendered_forward_nodes)
_FWD_BLOCK_RE = re.compile(r"===\s*消息\s*\d+\s*===")
_FWD_SENDER_RE = re.compile(r"\[发送者\]\s*(.+)")
# 正文到下一个方括号标记为止(可能有多行)
_FWD_BODY_RE = re.compile(r"\[消息内容\]\s*(.*?)(?=\n\s*\[|\Z)", re.S)
# 文件名可能带空格, 非贪婪匹配到下一个字段, 不能用 \S+; 尺寸、大小、URL 都可能缺
_FWD_ATTACH_RE = re.compile(
    r"\[附件\d+\][ \t]*类型:(\S+)[ \t]+文件名:(.+?)(?:[ \t]+尺寸:(\S+))?"
    r"(?:[ \t]+大小:(\S+))?(?:[ \t]+URL:(\S+))?[ \t]*$", re.M)

_LINK_KEYS = ("jumpurl", "jump_url", "qqdocurl", "url", "link", "share_url",
              "detail_url", "web_url", "weburl")


# 不带链接但能拼出来的 ARK: 一起听歌只给 QQ 音乐的 songmid
_ARK_LINK_TEMPLATES = {
    "music_together": ("songId", "https://y.qq.com/n/ryqq/songDetail/{}"),
}


def _ark_link(ark: dict) -> str:
    link = _find_link(ark)
    if link:
        return link
    key, template = _ARK_LINK_TEMPLATES.get(str(ark.get("ark_type")), ("", ""))
    value = str((ark.get("fields") or {}).get(key) or "") if key else ""
    return template.format(value) if value.isalnum() else ""


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def _find_link(node, depth: int = 0) -> str:
    """在 ARK 结构里找跳转链接(键名不固定, 递归找一层层嵌套的 fields)."""
    if depth > 4:
        return ""
    if isinstance(node, dict):
        for key, value in node.items():
            if (isinstance(value, str) and key.lower() in _LINK_KEYS
                    and value.startswith(("http://", "https://"))):
                return value
        for value in node.values():
            found = _find_link(value, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_link(item, depth + 1)
            if found:
                return found
    return ""


def _parse_ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except ValueError:
            pass
    return int(time.time())


@dataclass
class PeerState:
    allow_proactive: bool = False
    recv_msg_setting: str = ""
    bot_role: str = ""
    bot_openid: str = ""      # bot 自己在该群的 member_openid(认 @自己 要用)
    joined_at: str = ""
    checked_at: int = 0
    api_ok: bool = False           # 上次 bot_state 查询是否成功
    proactive_known: bool = False  # allow_proactive 是否有可信来源
    inferred_recv_all: bool = False  # 收到过非@群消息 => 该群必为"接收全部消息"
    # 全局选项(manager 写入), 关掉后只开了 @ 消息的群也算合规; 全进程同一口径
    require_recv_all: ClassVar[bool] = True

    @property
    def compliant(self) -> bool:
        """主动消息已开通 且 接收全部消息, 两者都要有可信来源, 缺一即静默.

        接口查成功过(api_ok)以接口为准; 否则主动消息须由开关事件确认过
        (proactive_known), recv=all 可由收到过非 @ 消息推断.
        """
        if not (self.api_ok or self.proactive_known):
            return False
        if not self.allow_proactive:
            return False
        if not PeerState.require_recv_all:
            return True
        return self.recv_msg_setting == "all" or self.inferred_recv_all


class BotInstance:
    def __init__(
        self,
        row: dict,
        *,
        db: Database,
        idmap: IdMap,
        access: AccessControl,
        store: MessageStore,
        media: MediaStore,
        http: aiohttp.ClientSession,
        token_urls: list[str],
        api_bases: list[str],
        unparsed: UnparsedLog | None = None,
    ):
        self.cfg = row
        self.appid: str = row["appid"]
        self.secret: str = row["secret"]
        self.db = db
        self.idmap = idmap
        self.access = access
        self.store = store
        self.media = media
        self.http = http
        self.unparsed = unparsed or UnparsedLog(
            unparsed_files(Path(getattr(db, "path", "data/x")).parent)[0]
        )
        self.api = QQApiClient(self.appid, self.secret, token_urls, api_bases, http)
        self.api.on_credential_error = self._on_credential_error
        self.api.on_fatal_code = self._on_fatal_code
        self.on_fatal = None       # (reason:str)->None, manager 注入: 自动禁用入口
        self._fatal_fired = False
        self.quota = QuotaTracker()
        self.sender = Sender(self)
        self.uploader = MediaUploader(self)
        self.builtin = BuiltinCommands(self)
        self.passthrough = Passthrough(self)

        self.self_id: int = 0  # 15 位虚拟号, start() 时确定
        self.links: list["OneBotLink"] = []
        self._tasks: list[asyncio.Task] = []
        self._peer_states: dict[tuple[str, str], PeerState] = {}
        self._state_locks: dict[str, asyncio.Lock] = {}
        self._state_query_times: deque[float] = deque()  # bot_state QPM 预算窗口
        self._inflight_msgs: set[str] = set()   # 入库前的并发去重
        # 处理过但没落库的消息 id(有界 FIFO). 未启用群不落库, store.seen 去不了重,
        # 而 @ 消息的 GROUP_MESSAGE_CREATE 副本会被误判成"收到非 @ 消息".
        self._handled_msgs: deque[str] = deque(maxlen=HANDLED_MSGS_MAX)
        self._handled_set: set[str] = set()
        self._drop_logged: dict[tuple[str, str], float] = {}
        self.me_info: dict = {}
        self.started = False
        # QQ 侧连通性: ws 模式看网关会话, webhook 模式看最近一次收到平台事件
        self.gateway_connected = False
        self.last_event_at: float = 0.0
        self.last_error: str = ""

    # ---------------- 配置视图 ----------------

    @property
    def name(self) -> str:
        return self.cfg.get("name") or self.me_info.get("username") or self.appid

    @property
    def superusers(self) -> list[int]:
        """本 bot 的 su 与全局 su(config.json 的 superusers)取并集."""
        own = [int(x) for x in self.cfg.get("superusers", [])]
        manager = getattr(self, "manager", None)
        extra = getattr(getattr(manager, "config", None), "superusers", None) or []
        return own + [int(x) for x in extra if int(x) not in own]

    @property
    def markdown_enabled(self) -> bool:
        return bool(self.cfg.get("markdown_enabled", True))

    @property
    def report_self_message(self) -> bool:
        """把 bot 自己发的消息也下发一份(由 sender 合成, 平台不回显自身消息)."""
        return bool(self.cfg.get("report_self_message", True))

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        if not self.secret:
            raise RuntimeError(f"bot {self.appid}: secret 为空, 拒绝启动")
        self.self_id = await self.idmap.to_virtual(
            self.appid, "bot", self.appid, nickname=self.cfg.get("name", "")
        )
        await self._load_peer_states()
        await self.sync_identity()

        from ..onebot.ws_client import OneBotLink  # noqa: PLC0415 循环依赖

        for endpoint in self.cfg.get("onebot_endpoints", []):
            if not endpoint.get("url"):
                continue
            link = OneBotLink(self, endpoint["url"], endpoint.get("access_token", ""))
            self.links.append(link)
            self._tasks.append(asyncio.create_task(link.run()))

        if self.cfg.get("event_mode") == "websocket":
            from ..qq.gateway import GatewayClient  # noqa: PLC0415

            self._tasks.append(asyncio.create_task(GatewayClient(self).run()))
        self._tasks.append(asyncio.create_task(self._identity_loop()))
        self._tasks.append(asyncio.create_task(self._sweep_unknown_states()))
        self.started = True
        logger.info("[%s] bot started, self_id=%s", self.appid, self.self_id)

    async def sync_identity(self) -> None:
        """跟一次平台上的身份: 名字 / 头像 / 真实 QQ 号, 变了就写; 失败只记日志."""
        try:
            self.me_info = await self.api.me()
        except Exception as exc:
            logger.warning("[%s] /users/@me failed: %s", self.appid, exc)
            return
        username = str(self.me_info.get("username") or "")
        if username and username != self.cfg.get("name"):
            logger.info("[%s] bot 名称: %r -> %r", self.appid,
                        self.cfg.get("name"), username)
            await self.db.execute(
                "UPDATE bots SET name=? WHERE appid=?", (username, self.appid))
            self.cfg["name"] = username
            # 名字也是 id_map 里 bot 那条的昵称, 一并更新
            await self.idmap.to_virtual(self.appid, "bot", self.appid,
                                        nickname=username)
        avatar = str(self.me_info.get("avatar", ""))
        if avatar.startswith("http://"):
            # 管理台走 https, http 图片会被浏览器按混合内容拦掉
            avatar = "https://" + avatar[len("http://"):]
        if avatar and avatar != self.cfg.get("avatar"):
            await self.db.execute(
                "UPDATE bots SET avatar=? WHERE appid=?", (avatar, self.appid))
            self.cfg["avatar"] = avatar
        # 真实 QQ 号只在 share_url 的 robot_uin 里(顶层 id 是内部标识)
        share_url = str(self.me_info.get("share_url", ""))
        uin = parse_qs(urlparse(share_url).query).get("robot_uin", [""])[0]
        if uin.isdigit() and int(uin) != int(self.cfg.get("bot_qq") or 0):
            await self.db.execute(
                "UPDATE bots SET bot_qq=? WHERE appid=?", (int(uin), self.appid))
            self.cfg["bot_qq"] = int(uin)
            logger.info("[%s] bot QQ 号: %s", self.appid, uin)

    async def _identity_loop(self) -> None:
        """低频跟随平台身份: 号主改了昵称/头像, 不必等下次重启."""
        while True:
            await asyncio.sleep(IDENTITY_REFRESH_INTERVAL)
            await self.sync_identity()

    # ---------------- 发送配额记账 ----------------

    def note_sent(self, chat_type: str, peer_openid: str,
                  proactive: bool = True) -> None:
        """记一条发送. 只有主动消息占每群 1000/天的配额, 被动回复不占."""
        if proactive:
            self.quota.note_sent(chat_type, peer_openid)
        self.quota.clear(chat_type, peer_openid)   # 发得出去 = 配额确实回来了

    def note_send_error(self, kind: str, code: int, peer_openid: str) -> None:
        self.quota.note_error(kind)
        if code == 40034100:
            self.quota.note_quota_error("group", peer_openid)

    def quota_blocked(self, chat_type: str, peer_openid: str) -> bool:
        """该会话是否处在配额静默窗口内(发不出去, 也就不必打扰后端)."""
        return self.quota.is_blocked(chat_type, peer_openid)

    def _fire_fatal(self, reason: str) -> None:
        if not self._fatal_fired and self.on_fatal is not None:
            self._fatal_fired = True
            self.on_fatal(reason)

    def _on_credential_error(self, count: int) -> None:
        """token 端点整轮报 invalid appid or secret; 连续达阈值即自动禁用."""
        self.last_error = f"invalid appid or secret（凭据错误 ×{count}）"
        if count >= CREDENTIAL_DISABLE_AFTER:
            self._fire_fatal(f"连续 {count} 轮 invalid appid or secret, 凭据无效")

    def _on_fatal_code(self, count: int, exc) -> None:
        """API 报"bot 已死"类错误码(封禁 11265 等): 与凭据无效同一待遇."""
        self.last_error = f"平台判决: {exc.message}（×{count}）"
        if count >= CREDENTIAL_DISABLE_AFTER:
            self._fire_fatal(f"平台报 {exc.code} {exc.message}, bot 已不可用")

    async def stop(self) -> None:
        self.started = False
        self.passthrough.close()
        for task in self._tasks:
            task.cancel()
        for link in self.links:
            await link.close()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.links.clear()

    def status_snapshot(self, with_groups: bool = False) -> dict:
        """运行态快照. with_groups=False 时只回群数量(群列表构造开销大)."""
        group_count = sum(1 for (chat_type, _) in self._peer_states
                          if chat_type == "group")
        groups = [
            {"openid": openid, **state.__dict__, "compliant": state.compliant,
             "state_source": "接口" if state.api_ok else "推断"}
            for (chat_type, openid), state in self._peer_states.items()
            if chat_type == "group"
        ] if with_groups else []
        mode = self.cfg.get("event_mode", "websocket")
        return {
            "appid": self.appid,
            "self_id": self.self_id,
            "name": self.name,
            "avatar": self.cfg.get("avatar", ""),
            "enabled": bool(self.cfg.get("enabled")),
            "event_mode": mode,
            "qq_connected": (
                self.gateway_connected if mode == "websocket"
                else time.time() - self.last_event_at < 900
            ),
            "last_event_at": int(self.last_event_at),
            "last_error": self.last_error,
            "links": [link.snapshot() for link in self.links],
            "passthrough": self.passthrough.snapshot(),
            "groups": groups,
            "group_count": group_count,
            # 媒体上行计量: saved 是缓存复用省下的
            "media": {
                "uploaded_mb": round(self.uploader.bytes_uploaded / 1048576, 2),
                "saved_mb": round(self.uploader.bytes_saved / 1048576, 2),
                "uploads": self.uploader.uploads_done,
                "cache_hits": self.uploader.cache_hits,
            },
            # 发送配额: 每群一天 1000 条
            "quota": self.quota.snapshot(),
        }

    # ---------------- peer_state ----------------

    async def _load_peer_states(self) -> None:
        rows = await self.db.fetchall(
            "SELECT * FROM peer_states WHERE bot_appid=?", (self.appid,)
        )
        for row in rows:
            state = PeerState(
                bool(row["allow_proactive"]), row["recv_msg_setting"],
                row["bot_role"], row["bot_openid"], row["joined_at"],
                row["checked_at"],
                bool(row["api_ok"]), bool(row["proactive_known"]),
                bool(row["inferred_recv_all"]),
            )
            # 迁移补的列默认 0/空: 清零 checked_at 让开机补查一次把新列填上
            if state.checked_at > 0 and (not state.api_ok or
                                         (row["chat_type"] == "group"
                                          and not state.bot_openid)):
                state.checked_at = 0
            self._peer_states[(row["chat_type"], row["peer_openid"])] = state

    async def _sweep_unknown_states(self) -> None:
        """开机补查状态未知的群, 顺带清掉退群残留的僵尸行(被踢后不再有事件触发).

        逐个查, 间隔 1s, 不和开机流量抢 QPM.
        """
        await asyncio.sleep(STATE_SWEEP_DELAY)
        pending = [openid for (chat_type, openid), st in self._peer_states.items()
                   if chat_type == "group" and not st.api_ok][:STATE_SWEEP_MAX]
        for openid in pending:
            try:
                await self.group_state(openid, reason="boot-sweep",
                                       skip_budget=True)
            except Exception as exc:            # noqa: BLE001 扫尾不该拖垮 bot
                logger.debug("[%s] boot-sweep(%s…) failed: %s",
                             self.appid, openid[:12], exc)
            await asyncio.sleep(1)

    async def _state_query_failed(self, key, group_openid: str, state, now: int,
                                  reason: str, exc: Exception):
        """bot_state 查失败: 保留既有值并推迟下次查询. 不下调 api_ok, 防偶发 500 静默好群."""
        logger.warning(
            "[%s] bot_state(%s…) failed (%s): %s%s",
            self.appid, group_openid[:12], reason, exc,
            " — 沿用上次成功的结果" if (state and state.api_ok)
            else " — 无历史结果, 退回推断口径",
        )
        self.last_error = f"bot_state: {exc}"
        if state is None:
            state = PeerState(checked_at=now)
            self._peer_states[key] = state
        else:
            state.checked_at = now
        await self._persist_peer_state("group", group_openid, state)
        return state

    async def _forget_group(self, group_openid: str) -> None:
        """作废该群的状态缓存(内存+库)与状态锁; 旧缓存对重新入群无效."""
        self._peer_states.pop(("group", group_openid), None)
        await self.db.execute(
            "DELETE FROM peer_states WHERE bot_appid=? AND chat_type='group'"
            " AND peer_openid=?",
            (self.appid, group_openid),
        )
        # 锁也回收, 否则反复进出群会泄漏
        self._state_locks.pop(group_openid, None)

    def _state_lock(self, openid: str) -> asyncio.Lock:
        if openid not in self._state_locks:
            self._state_locks[openid] = asyncio.Lock()
        return self._state_locks[openid]

    async def _persist_peer_state(
        self, chat_type: str, openid: str, state: PeerState
    ) -> None:
        await self.db.execute(
            "INSERT INTO peer_states (bot_appid, chat_type, peer_openid,"
            " allow_proactive, recv_msg_setting, bot_role, bot_openid, joined_at,"
            " checked_at, api_ok, proactive_known, inferred_recv_all)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(bot_appid, chat_type, peer_openid) DO UPDATE SET"
            " allow_proactive=excluded.allow_proactive,"
            " recv_msg_setting=excluded.recv_msg_setting,"
            " bot_role=excluded.bot_role, bot_openid=excluded.bot_openid,"
            " joined_at=excluded.joined_at,"
            " checked_at=excluded.checked_at, api_ok=excluded.api_ok,"
            " proactive_known=excluded.proactive_known,"
            " inferred_recv_all=excluded.inferred_recv_all",
            (self.appid, chat_type, openid, int(state.allow_proactive),
             state.recv_msg_setting, state.bot_role, state.bot_openid,
             state.joined_at, state.checked_at, int(state.api_ok),
             int(state.proactive_known), int(state.inferred_recv_all)),
        )

    @staticmethod
    def _state_retry_due(state: PeerState, now: int) -> bool:
        """未知态(api_ok 与 proactive_known 都没有)且退避到期 -> 该重查了."""
        if state.api_ok or state.proactive_known:
            return False
        return now - state.checked_at >= STATE_FAILURE_RETRY_INTERVAL

    def _state_budget_ok(self) -> bool:
        """滚动 60s 窗口内的 bot_state 全局预算(每 bot). 占坑即计数."""
        now = time.time()
        while self._state_query_times and now - self._state_query_times[0] > 60:
            self._state_query_times.popleft()
        if len(self._state_query_times) >= BOT_STATE_QPM_BUDGET:
            return False
        self._state_query_times.append(now)
        return True

    async def group_state(self, group_openid: str, refresh: bool = False,
                          reason: str = "", skip_budget: bool = False) -> PeerState:
        """群状态缓存; 未查询过 / refresh=True / 未知态退避到期时调 bot_state.

        节流: 同群 30s/60s 退避, 锁内双检, 全局 QPM 预算. skip_budget 给
        入群事件这类错过即丢的查询.
        """
        key = ("group", group_openid)
        state = self._peer_states.get(key)
        now = int(time.time())
        if (state is not None and state.checked_at > 0 and not refresh
                and not self._state_retry_due(state, now)):
            return state
        async with self._state_lock(group_openid):
            state = self._peer_states.get(key)
            now = int(time.time())
            if state is not None and state.checked_at > 0:
                due = (
                    now - state.checked_at >= STATE_REFRESH_MIN_INTERVAL
                    if refresh else self._state_retry_due(state, now)
                )
                if not due:
                    return state
            if not self._state_budget_ok() and not skip_budget:
                # 预算耗尽: 推迟, 不打接口也不落库(重启会对 api_ok=0 的行补查)
                logger.info("[%s] bot_state(%s…) %s: 全局预算耗尽(%s/min), 推迟",
                            self.appid, group_openid[:12], reason,
                            BOT_STATE_QPM_BUDGET)
                if state is None:
                    state = PeerState(checked_at=now)
                    self._peer_states[key] = state
                else:
                    state.checked_at = now
                return state
            try:
                data = await self.api.group_bot_state(group_openid)
            except Exception as exc:
                if isinstance(exc, QQApiError) and exc.code in GROUP_GONE_CODES:
                    # bot 已不在群(DEL_ROBOT 事件丢了): 与退群同一处理
                    logger.info("[%s] bot_state(%s…) %s: %s(%s), 作废该群状态",
                                self.appid, group_openid[:12], reason,
                                exc.message, exc.code)
                    await self._forget_group(group_openid)
                    return PeerState()
                return await self._state_query_failed(
                    key, group_openid, state, now, reason, exc)
            state = PeerState(
                allow_proactive=data["allow_proactive_msg"],
                recv_msg_setting=data["recv_msg_setting"],
                bot_role=data["member_role"],
                bot_openid=str(data.get("member_openid", "")),
                joined_at=str(data.get("joined_at", "")),
                checked_at=now,
                api_ok=True,
                proactive_known=True,
                inferred_recv_all=False,  # 接口权威, 清掉旧推断
            )
            self._peer_states[key] = state
            await self._persist_peer_state("group", group_openid, state)
            logger.info("[%s] bot_state(%s…) %s: proactive=%s recv=%s role=%s",
                        self.appid, group_openid[:12], reason,
                        state.allow_proactive, state.recv_msg_setting, state.bot_role)
            return state

    async def group_role(self, group_openid: str, force: bool = False) -> str:
        """bot 在该群的角色, 缓存太旧(或 force)就刷一次(提权平台不推事件).

        force 给"进入会话"这类用户动作, 仍受 30s/群 节流.
        """
        state = self._peer_states.get(("group", group_openid))
        stale = (force or state is None
                 or int(time.time()) - state.checked_at > ROLE_FRESH_SECONDS)
        if stale:
            state = await self.group_state(group_openid, refresh=True,
                                           reason="role-check")
        return state.bot_role if state else ""

    async def refresh_peer_state(self, chat_type: str, openid: str, reason: str) -> None:
        """发送失败(限频/权限)后的后台复查入口."""
        if chat_type == "group":
            await self.group_state(openid, refresh=True, reason=reason)

    async def _set_proactive_flag(
        self, chat_type: str, openid: str, allowed: bool
    ) -> None:
        # 与 group_state 同一把锁: 事件排到在途查询落地后再应用, 免被旧快照冲掉
        async with self._state_lock(openid):
            key = (chat_type, openid)
            state = self._peer_states.get(key)
            if state is None:
                state = PeerState()
                self._peer_states[key] = state
            state.allow_proactive = allowed
            state.proactive_known = True  # 平台推的开关事件是权威信号
            # 不要动 checked_at: 它专指"接口查过了", 填了会让 group_state 永不查询
            await self._persist_peer_state(chat_type, openid, state)

    async def _note_recv_all(self, group_openid: str) -> None:
        """收到非 @ 群消息 => 该群必然设了"接收全部消息"."""
        key = ("group", group_openid)
        state = self._peer_states.get(key)
        if state is not None and state.inferred_recv_all:
            return  # 免锁快路径
        async with self._state_lock(group_openid):
            state = self._peer_states.get(key)
            if state is None:
                state = PeerState()
                self._peer_states[key] = state
            if not state.inferred_recv_all:
                state.inferred_recv_all = True
                await self._persist_peer_state("group", group_openid, state)

    def peer_allows_proactive(self, chat_type: str, openid: str) -> bool:
        state = self._peer_states.get((chat_type, openid))
        if state is None:
            # 私聊默认乐观尝试(错误码兜底); 群默认悲观
            return chat_type == "private"
        # 私聊无 bot_state 接口, 可信来源只看 proactive_known(C2C 开关事件)
        if chat_type == "private" and not state.proactive_known:
            return True
        return state.allow_proactive

    # ---------------- 工具 ----------------

    def peer_enabled(self, chat_type: str, openid: str) -> bool:
        """该群/私聊对象过不过名单. 群按本 bot 的 group_list_mode, 私聊恒为黑名单."""
        mode = self.cfg.get("group_list_mode", "white") if chat_type == "group" else "black"
        return self.access.allowed(self.appid, mode, chat_type, openid)

    async def virtual_for_peer(self, chat_type: str, openid: str) -> int:
        kind = "group" if chat_type == "group" else "user"
        return await self.idmap.to_virtual(self.appid, kind, openid)

    async def display_name(self, virtual_qq: str) -> str:
        try:
            entry = self.idmap.lookup_virtual(int(virtual_qq))
            if entry and entry.nickname:
                return entry.nickname
        except (TypeError, ValueError):
            pass
        return str(virtual_qq)

    def broadcast(self, event: dict) -> None:
        if not any(link.connected for link in self.links):
            self.note_drop("后端", "没有已连接的 OneBot 后端, 事件丢弃"
                           if self.links else "没配置 OneBot 后端, 事件丢弃")
            return
        payload = json.dumps(event, ensure_ascii=False)
        for link in self.links:
            link.send_text(payload)

    def note_drop(self, peer: str, reason: str) -> None:
        """收到了但没下发给后端: 按会话+原因限频记一条, 便于排查「bot 不理人」."""
        key = (peer, reason)
        now = time.monotonic()
        if now - self._drop_logged.get(key, -DROP_LOG_INTERVAL) < DROP_LOG_INTERVAL:
            return
        if len(self._drop_logged) > 4096:
            self._drop_logged.clear()
        self._drop_logged[key] = now
        logger.info("[%s] %s 未下发: %s(%d 分钟内不再重复记录)",
                    self.appid, peer, reason, DROP_LOG_INTERVAL // 60)

    async def emit(self, event: dict, raw: bool = True) -> None:
        """下发 OneBot 事件(消息事件先过 event_in 钩子); raw=True 时顺带透传原始事件.

        插件丢掉的只是 OneBot 那一份, 透传侧照发.
        """
        if raw:
            self._publish_raw()
        if event.get("post_type") == "message":
            chat_type = "group" if event.get("message_type") == "group" else "private"
            event = await plugins.pipe("event_in", HookContext(self, chat_type), event)
            if not event:
                return
        self.broadcast(event)

    def _publish_raw(self) -> None:
        current = _RAW_EVENT.get()
        if current is None or current["sent"]:
            return
        current["sent"] = True
        self.passthrough.publish(current["t"], current["d"], current["id"])

    def _publish_ungated(self, current: dict) -> None:
        """没有 OneBot 对应物的事件: 过名单就透传."""
        data = current["d"] if isinstance(current["d"], dict) else {}
        group = str(data.get("group_openid") or "")
        if group:
            if not self.peer_enabled("group", group):
                return
        else:
            author = data.get("author") if isinstance(data.get("author"), dict) else {}
            user = str(data.get("openid") or data.get("user_openid")
                       or author.get("user_openid") or "")
            if user and not self.peer_enabled("private", user):
                return
        current["sent"] = True
        self.passthrough.publish(current["t"], current["d"], current["id"])

    # ---------------- QQ 事件入口 ----------------

    async def dispatch_qq_event(self, event_type: str, data: dict,
                                event_id: str = "") -> None:
        self.last_event_at = time.time()
        try:
            await self._dispatch(event_type, data, event_id)
        except Exception:
            logger.exception("[%s] event %s dispatch failed", self.appid, event_type)

    async def _dispatch(self, event_type: str, data: dict,
                        event_id: str = "") -> None:
        # 通知类事件的 event_id 可当被动回复凭据, 不吃主动消息配额
        if event_id:
            await self._record_event_credential(event_type, data, event_id)
        current = {"t": event_type, "d": data, "id": event_id, "sent": False}
        token = _RAW_EVENT.set(current)
        try:
            await self._dispatch_inner(event_type, data)
        finally:
            _RAW_EVENT.reset(token)
            if (not current["sent"] and event_type not in _GATED_EVENTS
                    and self.passthrough.active):
                self._publish_ungated(current)

    async def _dispatch_inner(self, event_type: str, data: dict) -> None:
        if event_type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            await self._on_group_message(data, at_me=event_type == "GROUP_AT_MESSAGE_CREATE")
            return
        handler = {
            "C2C_MESSAGE_CREATE": self._on_c2c_message,
            "GROUP_ADD_ROBOT": self._on_group_add_robot,
            "GROUP_DEL_ROBOT": self._on_group_del_robot,
            "FRIEND_ADD": self._on_friend_add,
            "FRIEND_DEL": self._on_friend_del,
            "GROUP_MSG_RECEIVE": self._on_group_msg_receive,
            "GROUP_MSG_REJECT": self._on_group_msg_reject,
            "C2C_MSG_RECEIVE": self._on_c2c_msg_receive,
            "C2C_MSG_REJECT": self._on_c2c_msg_reject,
            "GROUP_MEMBER_ADD": self._on_group_member_add,
            "GROUP_MEMBER_REMOVE": self._on_group_member_remove,
            "GROUP_JOIN_REQUEST": self._on_group_join_request,
            "INTERACTION_CREATE": self._on_interaction,
            "SUBSCRIBE_MESSAGE_STATUS": self._on_subscribe_status,
        }.get(event_type)
        if handler is None:
            # 平台事件类型比文档全, 没处理的落盘
            self.unparsed.record(f"event:{event_type}", "未处理的事件类型",
                                 data, self.appid)
            return
        await handler(data)

    _EVENT_CREDENTIAL_TYPES = {
        "GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE", "GROUP_JOIN_REQUEST",
        "GROUP_ADD_ROBOT", "GROUP_MSG_RECEIVE", "GROUP_MSG_REJECT",
        "FRIEND_ADD", "C2C_MSG_RECEIVE", "C2C_MSG_REJECT", "INTERACTION_CREATE",
    }

    async def _record_event_credential(self, event_type: str, data: dict,
                                       event_id: str) -> None:
        if event_type not in self._EVENT_CREDENTIAL_TYPES:
            return
        group_openid = data.get("group_openid", "")
        if group_openid:
            await self.store.record_event_credential(
                self.appid, "group", group_openid, event_id, event_type)
            return
        openid = data.get("openid") or data.get("user_openid", "")
        if openid:
            await self.store.record_event_credential(
                self.appid, "private", openid, event_id, event_type)

    async def _content_segments(
        self, text: str, bot_openid: str,
    ) -> tuple[list[dict], bool]:
        """把 content 里的 <@openid> 转成 at 段, 顺带判断有没有 @ 到 bot 自己.

        GROUP_MESSAGE_CREATE 的 content 原样保留 at 标记; @ 消息已被剥掉 @bot 前缀.
        """
        text = _ATTACHMENT_TAG_RE.sub("", text)
        segments: list[dict] = []
        at_me = False
        pos = 0
        for match in _AT_TAG_RE.finditer(text):
            if match.start() > pos:
                chunk = text[pos:match.start()]
                if chunk:
                    segments.append({"type": "text", "data": {"text": chunk}})
            raw = match.group(0)
            target = match.group(1) or match.group(2) or ""
            if match.group(3) or match.group(4) or target.lower() in _AT_ALL_TOKENS:
                segments.append({"type": "at", "data": {"qq": "all"}})
            elif target and (target == bot_openid or target == self.appid):
                segments.append({"type": "at", "data": {"qq": str(self.self_id)}})
                at_me = True
            elif _OPENID_RE.match(target):
                virtual = await self.idmap.to_virtual(self.appid, "user", target)
                segments.append({"type": "at", "data": {"qq": str(virtual)}})
            else:
                # 不认识的 at 目标: 原样留成文本, 别建假映射; 落盘
                segments.append({"type": "text", "data": {"text": raw}})
                self.unparsed.record("at_tag", f"未识别的 at 目标: {raw[:64]}",
                                     {"content": text[:500]}, self.appid)
            pos = match.end()
        tail = text[pos:]
        if tail:
            segments.append({"type": "text", "data": {"text": tail}})
        return self._split_faces(segments), at_me

    @staticmethod
    def _split_faces(segments: list[dict]) -> list[dict]:
        """把文本里的 <faceType=..,faceId=".."> 切成 OneBot face 段; ext 文字存为 summary."""
        out: list[dict] = []
        for seg in segments:
            if seg.get("type") != "text":
                out.append(seg)
                continue
            text = seg["data"].get("text", "")
            pos = 0
            for match in _FACE_TAG_RE.finditer(text):
                if match.start() > pos:
                    out.append({"type": "text",
                                "data": {"text": text[pos:match.start()]}})
                if match.group(1) == "6":
                    pos = match.end()
                    continue
                data = {"id": match.group(2)}   # OneBot face 段只认 id
                raw_ext = match.group(3) or ""
                if raw_ext:
                    try:
                        import base64 as _b64  # noqa: PLC0415
                        payload = json.loads(
                            _b64.b64decode(raw_ext + "==").decode("utf-8"))
                        if payload.get("text"):
                            data["summary"] = str(payload["text"])
                    except Exception:
                        pass
                if data["id"]:
                    out.append({"type": "face", "data": data})
                elif data.get("summary"):
                    out.append({"type": "text", "data": {"text": data["summary"]}})
                pos = match.end()
            tail = text[pos:]
            if tail or pos == 0:
                out.append({"type": "text", "data": {"text": tail or text}})
        merged: list[dict] = []
        for s in out:
            if s["type"] == "text":
                if not s["data"].get("text"):
                    continue
                if merged and merged[-1]["type"] == "text":
                    merged[-1] = {"type": "text", "data": {
                        "text": merged[-1]["data"]["text"] + s["data"]["text"]}}
                    continue
            merged.append(s)
        return merged

    def _note_rkey(self, url: str) -> None:
        """记下入站直链里的 rkey 给转发网页用(见 cdnkeys); 落盘限一分钟一次."""
        if not cdnkeys.note(url):
            return
        now = time.time()
        if now - _rkey_saved[0] < 60:
            return
        _rkey_saved[0] = now
        task = asyncio.create_task(self.db.kv_set("cdn_rkeys", cdnkeys.dump()))
        _rkey_tasks.add(task)
        task.add_done_callback(_rkey_tasks.discard)

    async def _attachments_to_segments(self, data: dict) -> list[dict]:
        segments: list[dict] = []
        for att in data.get("attachments") or []:
            url = att.get("url", "")
            if url.startswith("//"):
                url = "https:" + url
            elif url and not urlparse(url).hostname:
                url = "https://" + url  # 平台可能省略 scheme
            self._note_rkey(url)
            content_type = att.get("content_type", "")
            if content_type.startswith("image"):
                segments.append({"type": "image", "data": {
                    "file": att.get("filename") or url, "url": url,
                    "file_id": url, "subType": "0",
                }})
            elif content_type.startswith(("voice", "audio")):
                segments.append({"type": "record", "data": {
                    "file": att.get("filename") or url, "url": url,
                    "file_id": url,
                }})
            elif content_type.startswith("video"):
                segments.append({"type": "video", "data": {
                    "file": att.get("filename") or url, "url": url,
                    "file_id": url,
                }})
            else:
                segments.append({"type": "file", "data": {
                    "file": att.get("filename") or url, "url": url,
                    "name": att.get("filename", ""),
                    "file_id": url, "busid": 0,
                    "size": att.get("size", 0), "file_size": att.get("size", 0),
                }})
        return segments

    # 已知会消费的 payload 字段; 出现其它字段就落盘, 便于事后补解析
    _KNOWN_MSG_FIELDS = {
        "id", "author", "content", "timestamp", "attachments", "group_openid",
        "group_id", "message_type", "message_scene", "msg_elements", "mentions",
        "message_reference", "ark_data", "op_member_openid",
    }

    @staticmethod
    def _msg_idx_of(data: dict) -> str:
        """message_scene.ext 里的 msg_idx=REFIDX_xxx; 引用只认它, 用消息 id 平台静默忽略."""
        scene = data.get("message_scene") or {}
        for item in scene.get("ext") or []:
            if isinstance(item, str) and item.startswith("msg_idx="):
                return item[len("msg_idx="):]
        return ""

    @staticmethod
    def _ext_value(data: dict, key: str) -> str:
        """message_scene.ext 是 ["k=v", ...] 形式."""
        scene = data.get("message_scene") or {}
        prefix = key + "="
        for item in scene.get("ext") or []:
            if isinstance(item, str) and item.startswith(prefix):
                return item[len(prefix):]
        return ""

    async def _element_segments(self, element: dict) -> list[dict]:
        """msg_elements 里的一条 -> OneBot 段(实测形状: content + attachments)."""
        segments: list[dict] = []
        text = (element.get("content") or element.get("text")
                or element.get("desc") or "")
        if isinstance(text, dict):
            text = text.get("content") or text.get("text") or ""
        if text:
            segments.append({"type": "text", "data": {"text": str(text)}})
        # 媒体在 attachments 数组里
        segments.extend(await self._attachments_to_segments(element))
        # 兼容可能的扁平形状
        image = element.get("image")
        url = image.get("url") if isinstance(image, dict) else None
        url = url or element.get("pic_url") or (
            element.get("url") if isinstance(element.get("url"), str) else None)
        if url:
            if url.startswith("//"):
                url = "https:" + url
            segments.append({"type": "image", "data": {"file": url, "url": url}})
        return segments

    async def _rendered_forward_nodes(self, text: str) -> list[dict]:
        """把平台渲染成纯文本的合并转发(message_type=102 的常见形态)还原成节点:

            [群聊的聊天记录]
            === 消息 1 ===
            [发送者] 某人
            [消息内容] 说了什么
            [附件1] 类型:视频 文件名:x.mp4 尺寸:360x640 大小:137.6MB URL:https://…

        不还原的话媒体只剩裸 URL 混在文本里, 下游插件会误当链接处理.
        """
        blocks = _FWD_BLOCK_RE.split(text)[1:]      # 首段是 [群聊的聊天记录] 抬头
        nodes: list[dict] = []
        for block in blocks:
            segments: list[dict] = []
            sender = _FWD_SENDER_RE.search(block)
            body = _FWD_BODY_RE.search(block)
            if body and body.group(1).strip():
                segments.append({"type": "text",
                                 "data": {"text": body.group(1).strip()}})
            for match in _FWD_ATTACH_RE.finditer(block):
                kind, filename, _size, url = (match.group(1), match.group(2),
                                              match.group(4), match.group(5))
                if not url:
                    # 平台没给地址(常见于文件), 只能报个名字
                    segments.append({"type": "text", "data": {"text": f"[{kind}] {filename}"}})
                    continue
                seg_type = {"视频": "video", "语音": "record",
                            "文件": "file"}.get(kind, "image")
                segments.append({"type": seg_type, "data": {
                    "file": filename, "url": url, "file_id": url,
                }})
            if not segments:
                continue
            nodes.append({
                "sender": {"user_id": 0,
                           "nickname": sender.group(1).strip() if sender else "未知"},
                "time": 0,
                "message": segments,
            })
        return nodes

    async def _elements_to_nodes(self, elements) -> list[dict]:
        """msg_elements -> OneBot 转发节点."""
        nodes: list[dict] = []
        if not isinstance(elements, list):
            return nodes
        for element in elements:
            if not isinstance(element, dict):
                continue
            segments = await self._element_segments(element)
            if not segments:
                continue
            # msg_elements 不带发送者. 填"未知"而非空串: 插件的 .get 默认值对空串不生效
            nickname = (element.get("nickname") or element.get("sender_name")
                        or element.get("username") or "未知")
            nodes.append({
                "sender": {"user_id": 0, "nickname": str(nickname)},
                "time": int(element.get("timestamp") or 0),
                "message": segments,
            })
        return nodes

    async def _reply_segment(self, data: dict, chat_type: str, peer_openid: str,
                             peer_virtual: int) -> dict | None:
        """message_type=103 引用回复: 按 ext 的 ref_msg_idx 查被引用消息的 mid.

        查不到先按内容特征归属, 再不行用 msg_elements 补登记一条.
        """
        ref_idx = self._ext_value(data, "ref_msg_idx")
        if not ref_idx:
            return None
        elements = data.get("msg_elements")
        element = elements[0] if isinstance(elements, list) and elements else None
        found = await self._mid_by_idx(ref_idx)
        if found is not None:
            if isinstance(element, dict):
                await self._refresh_quoted_media(found, element)
            return {"type": "reply", "data": {"id": str(found)}}

        if not isinstance(element, dict):
            return None

        # 被引用的本身也是引用时是渲染文本, 先还原(见 unwrap_quote_blob)
        blob = str(element.get("content") or "")
        plain = unwrap_quote_blob(blob)
        if plain != blob:
            element = {**element, "content": plain}

        # 平台投递时可能换 idx(引用的引用是一次性 TMP_), 按内容特征在近期消息里找;
        # 否则落成 user_id=0 的影子记录, to_me/撤回都失效.
        matched, unique = await self._match_reply_fallback(chat_type, peer_openid, element)
        if matched is not None:
            # 只有稳定的 REFIDX_ 且候选唯一才记别名; 不覆盖原 idx, 免得误配后永久指错
            if unique and ref_idx.startswith(_STABLE_IDX_PREFIX):
                await self.db.execute(
                    "INSERT OR REPLACE INTO msg_aliases (bot_appid, msg_idx, mid, ts)"
                    " VALUES (?,?,?,?)", (self.appid, ref_idx, matched, int(time.time())))
            logger.info("[%s] reply idx 未命中, 按内容特征归属到 mid=%s",
                        self.appid, matched)
            await self._refresh_quoted_media(matched, element)
            return {"type": "reply", "data": {"id": str(matched)}}

        segments = await self._element_segments(element)
        if not segments:
            return None
        # 补登记: qq_msg_id 留空(不可撤回), 发送者未知 -> user_id 0
        mid = await self.store.record_incoming(
            self.appid, chat_type, peer_openid, peer_virtual, 0, "",
            segments, {"user_id": 0, "nickname": "", "card": "", "role": "member"},
            _parse_ts(element.get("timestamp") or data.get("timestamp")),
            msg_idx=ref_idx,
        )
        return {"type": "reply", "data": {"id": str(mid)}}

    async def _quotes_own_message(self, data: dict) -> bool:
        """这条是不是在引用 bot 自己发过的消息(只在未启用的群里问).

        idx 比对之外必须带内容兜底: 平台常换 idx(尤其图文消息).
        """
        ref_idx = self._ext_value(data, "ref_msg_idx")
        if not ref_idx:
            return False
        if await self._mid_by_idx(ref_idx, out_only=True) is not None:
            return True
        elements = data.get("msg_elements")
        element = elements[0] if isinstance(elements, list) and elements else None
        if not isinstance(element, dict):
            return False
        blob = str(element.get("content") or "")
        plain = unwrap_quote_blob(blob)
        if plain != blob:
            element = {**element, "content": plain}
        matched, _ = await self._match_reply_fallback(
            "group", data.get("group_openid", ""), element, out_only=True)
        return matched is not None

    async def _mid_by_idx(self, idx: str, out_only: bool = False) -> int | None:
        """按引用 idx 找消息: 先认消息自己的 idx, 再认按内容找回时记下的别名."""
        only = " AND direction='out'" if out_only else ""
        row = await self.db.fetchone(
            f"SELECT mid FROM messages WHERE bot_appid=? AND msg_idx=?{only}",
            (self.appid, idx))
        if row is None:
            row = await self.db.fetchone(
                "SELECT a.mid FROM msg_aliases a JOIN messages m ON m.mid=a.mid"
                f" WHERE a.bot_appid=? AND a.msg_idx=?{only.replace('direction', 'm.direction')}",
                (self.appid, idx))
        return row["mid"] if row else None

    async def _refresh_quoted_media(self, mid: int, element: dict) -> None:
        """用引用 payload 里新签的 URL 替换原消息的过期链接.

        同一张图文件名不变而 rkey 每次重签, 按文件名对上就回填. 自己发的消息没有平台
        文件名, 按类型逐个对位; 视频 rkey 绑文件(见 cdnkeys), 这是取回原视频的唯一地址.
        """
        quoted = [s for s in await self._attachments_to_segments(element)
                  if s["data"]["url"]]
        if not quoted:
            return
        row = await self.db.fetchone(
            "SELECT content, direction FROM messages WHERE mid=?", (mid,))
        if row is None:
            return
        try:
            segments = json.loads(row["content"])
        except ValueError:
            return
        media = [s for s in segments if isinstance(s, dict)
                 and s.get("type") in ("image", "video", "record", "file")]
        pairs: list[tuple[dict, str | None]] = []
        if row["direction"] == "out":
            for kind in {s["type"] for s in quoted}:
                mine = [s for s in media if s["type"] == kind]
                theirs = [s["data"]["url"] for s in quoted if s["type"] == kind]
                if len(mine) == len(theirs):        # 数不上就不猜
                    pairs.extend(zip(mine, theirs))
        else:
            by_name = {s["data"]["file"]: s["data"]["url"] for s in quoted}
            pairs = [(s, by_name.get(str((s.get("data") or {}).get("file", ""))))
                     for s in media]
        changed = False
        for seg, fresh in pairs:
            seg_data = seg.get("data") or {}
            if not fresh or seg_data.get("url") == fresh:
                continue
            if seg_data.get("file") == seg_data.get("url"):  # 自己发的 file 也是地址
                seg_data["file"] = fresh
            seg_data["url"] = fresh
            if seg_data.get("file_id"):
                seg_data["file_id"] = fresh
            seg["data"] = seg_data
            changed = True
        if changed:
            await self.db.execute(
                "UPDATE messages SET content=? WHERE mid=?",
                (json.dumps(segments, ensure_ascii=False), mid))
            logger.info("[%s] 引用带来新签的媒体 URL, 已回填 mid=%s",
                        self.appid, mid)

    async def _match_reply_fallback(
        self, chat_type: str, peer_openid: str, element: dict,
        out_only: bool = False,
    ) -> tuple[int | None, bool]:
        """引用 idx 对不上时, 按内容特征在近 30 分钟的同会话消息里归属.

        媒体按字节大小与外发的 media_sizes 求交, 文本要精确相等(也比插件改写后
        实际发出的文字). 外发优先, 再退到入站; 取最近一条.
        返回 (mid, 是否唯一候选), 都不中为 (None, False).
        """
        text = str(element.get("content") or "").strip()
        sizes = set()
        for att in element.get("attachments") or []:
            if isinstance(att, dict) and att.get("size"):
                try:
                    sizes.add(int(att["size"]))
                except (TypeError, ValueError):
                    pass
        if not text and not sizes:
            return None, False
        rows = await self.db.fetchall(
            "SELECT mid, direction, content, media_sizes, sent_text FROM messages"
            " WHERE bot_appid=? AND chat_type=? AND peer_openid=? AND ts>=?"
            # 影子记录不能当匹配目标
            " AND (direction='out' OR user_virtual!=0)"
            " ORDER BY mid DESC LIMIT 60",
            (self.appid, chat_type, peer_openid, int(time.time()) - 1800),
        )
        # 引用 markdown 消息时平台可能给原文: 链接写回纯文本发送时的「文字 网址」
        texts = {text, _MD_LINK_RE.sub(r"\1 \2", text)} - {""}
        hits: dict[str, list[int]] = {"out": [], "in": []}
        for row in rows:
            if out_only and row["direction"] != "out":
                continue
            hit = False
            if sizes:
                # 外链媒体没记字节大小, 字节不中还要试文本, 不能一票否决
                try:
                    row_sizes = set(json.loads(row["media_sizes"] or "[]"))
                except ValueError:
                    row_sizes = set()
                hit = bool(sizes & row_sizes)
            if not hit and texts:
                try:
                    segs = json.loads(row["content"])
                except ValueError:
                    segs = []
                row_text = "".join(
                    s.get("data", {}).get("text", "")
                    for s in segs if isinstance(s, dict) and s.get("type") == "text"
                ).strip()
                # 插件改写过的(转发转网页等), 记录的是原消息, 要比实际发出的文字
                hit = bool(texts & ({row_text, str(row["sent_text"] or "").strip()} - {""}))
            if hit:
                hits["out" if row["direction"] == "out" else "in"].append(row["mid"])
        picked = hits["out"] or hits["in"]
        return (picked[0], len(picked) == 1) if picked else (None, False)

    async def _extra_segments(self, data: dict, qq_msg_id: str) -> list[dict]:
        """处理 content/attachments 之外的形态: 合并转发/引用/ARK 等; 落盘供补解析."""
        segments: list[dict] = []
        msg_type = data.get("message_type", 0)
        elements = data.get("msg_elements")

        if msg_type == 103:
            # 引用回复由 _reply_segment 处理, 不要把 msg_elements 当合并转发
            return segments

        if elements or msg_type in (101, 102):
            nodes = await self._elements_to_nodes(elements)
            if not nodes and msg_type in (101, 102):
                # 常态是只给渲染文本, 没有结构化 elements
                nodes = await self._rendered_forward_nodes(
                    str(data.get("content") or ""))
            if nodes:
                fid = qq_msg_id or f"fwd-{int(time.time() * 1000)}"
                await self.db.execute(
                    "INSERT OR REPLACE INTO forwards (id, bot_appid, nodes, raw, ts)"
                    " VALUES (?,?,?,?,?)",
                    (fid, self.appid, json.dumps(nodes, ensure_ascii=False),
                     json.dumps(data, ensure_ascii=False)[:20000], int(time.time())),
                )
                segments.append({"type": "forward", "data": {"id": fid}})
            else:
                self.unparsed.record(f"message_type={msg_type}",
                                     "合并转发/聊天记录: 未识别结构, 需补解析",
                                     data, self.appid)
        elif msg_type not in (0, 3, 103, None):
            self.unparsed.record(f"message_type={msg_type}",
                                 "未支持的消息形态", data, self.appid)

        ark = data.get("ark_data")
        if isinstance(ark, dict):
            # ARK 卡片 -> OneBot json 段(content 里已有可读摘要)
            segments.append({"type": "json", "data": {
                "data": json.dumps(ark, ensure_ascii=False),
            }})
            # 带跳转链接就补一段文本给解析类插件; 小程序卡片平台不给链接, 不必记
            link = _ark_link(ark)
            if link:
                segments.append({"type": "text", "data": {"text": "\n" + link}})
            elif ark.get("ark_type") != "miniapp":
                self.unparsed.record(
                    "ark_no_link",
                    f"ARK({ark.get('ark_type', '?')}) 无跳转链接, 字段: "
                    f"{sorted((ark.get('fields') or {}).keys())}",
                    ark, self.appid,
                )

        unknown = set(data) - self._KNOWN_MSG_FIELDS
        if unknown:
            self.unparsed.record("unknown_fields",
                                 f"payload 出现未知字段: {sorted(unknown)}",
                                 data, self.appid)
        return segments

    async def _group_gate(self, group_openid: str, text: str) -> bool:
        """群合规总闸."""
        state = await self.group_state(group_openid, reason="first-seen")
        if state.compliant:
            return True
        if match_builtin(text) is not None:
            state = await self.group_state(
                group_openid, refresh=True, reason="builtin-cmd"
            )
        return state.compliant

    @staticmethod
    def _norm_role(role: object) -> str:
        """归一到 OneBot 的三个合法角色; 非法值会让严格校验的下游丢弃整条消息."""
        text = str(role or "").lower()
        return text if text in ("owner", "admin", "member") else "member"

    async def _member_role(self, group_openid: str, user_openid: str,
                           payload_role: str) -> str:
        """发送者角色: payload 通常不带, 回落到 group_members 缓存."""
        if payload_role:
            return self._norm_role(payload_role)
        row = await self.db.fetchone(
            "SELECT role FROM group_members WHERE bot_appid=? AND group_openid=?"
            " AND user_openid=?",
            (self.appid, group_openid, user_openid),
        )
        return self._norm_role(row["role"] if row else "")

    async def _on_group_message(self, data: dict, at_me: bool = True) -> None:
        author = data.get("author") or {}
        group_openid = data.get("group_openid", "")
        user_openid = author.get("member_openid") or author.get("id", "")
        if not group_openid or not user_openid:
            return
        qq_msg_id = str(data.get("id", ""))
        # 去重要在 recv_all 推断之前, 防 @ 消息的非 @ 副本造成误判
        if qq_msg_id:
            if qq_msg_id in self._inflight_msgs:
                return                  # 并发重推: 前一份还没入库
            # 必须在任何 await 之前占位
            self._inflight_msgs.add(qq_msg_id)
        try:
            if qq_msg_id and (qq_msg_id in self._handled_set
                              or await self.store.seen(self.appid, qq_msg_id)):
                return                  # 处理过的重推(未启用的群不落库, 只在内存里)
            recorded = await self._on_group_message_inner(data, at_me, qq_msg_id)
            # 落了库的由 store.seen 兜住, 内存只记没落库的
            if qq_msg_id and not recorded:
                self._remember_handled(qq_msg_id)
        finally:
            self._inflight_msgs.discard(qq_msg_id)

    def _remember_handled(self, qq_msg_id: str) -> None:
        if qq_msg_id in self._handled_set:
            return
        if len(self._handled_msgs) == self._handled_msgs.maxlen:
            self._handled_set.discard(self._handled_msgs[0])   # 满了会挤掉队首
        self._handled_msgs.append(qq_msg_id)
        self._handled_set.add(qq_msg_id)

    async def _on_group_message_inner(self, data: dict, at_me: bool,
                                      qq_msg_id: str) -> bool:
        """返回这条有没有落库(没落库的由调用方在内存里去重)."""
        author = data.get("author") or {}
        group_openid = data.get("group_openid", "")
        user_openid = author.get("member_openid") or author.get("id", "")
        if not at_me:
            # 能收到非 @ 消息本身就证明该群开了"接收全部消息"
            await self._note_recv_all(group_openid)
        nickname = author.get("username", "") or ""
        payload_role = author.get("member_role", "") or ""
        ts = _parse_ts(data.get("timestamp"))

        text = (data.get("content") or "").strip()
        # 认 "@bot 自己" 要用群状态里的 member_openid(首见该群才查接口)
        state = await self.group_state(group_openid, reason="first-seen")
        content_segs, at_in_text = await self._content_segments(
            text, state.bot_openid)
        command_text = "".join(
            seg["data"].get("text", "") for seg in content_segs
            if seg["type"] == "text").strip()
        # @ 了别人时不接内置指令; 指令匹配用去掉 at 段后的纯文本
        at_others = any(
            seg["type"] == "at" and seg["data"].get("qq") not in
            (str(self.self_id), "all")
            for seg in content_segs
        )
        builtin = None if at_others else match_builtin(command_text)

        # 未启用的群不建映射不落库, 只放行内置指令(启用流程要用)和引用 bot 自己
        # 消息的(追问/撤回); 群友互引不算.
        if not self.peer_enabled("group", group_openid) and builtin is None and not await self._quotes_own_message(data):
            if at_me:
                self.note_drop(f"群 {group_openid[:12]}…", "群未启用, 被 @ 也不响应")
            return False

        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        user_virtual = await self.idmap.to_virtual(
            self.appid, "user", user_openid, nickname=nickname,
            union_openid=author.get("union_openid") or None,
        )
        # role 仅在 payload 显式给出时覆盖缓存(GROUP_ADD_ROBOT 写入的 admin 不被冲掉)
        await self.db.execute(
            "INSERT INTO group_members (bot_appid, group_openid, user_openid,"
            " nickname, role, last_seen) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(bot_appid, group_openid, user_openid) DO UPDATE SET"
            " nickname=CASE WHEN excluded.nickname!='' THEN excluded.nickname ELSE nickname END,"
            " role=CASE WHEN ?!='' THEN ? ELSE role END,"
            " last_seen=excluded.last_seen",
            # 原始 payload_role 只用来判给没给, 写入的值统一归一化
            (self.appid, group_openid, user_openid, nickname,
             self._norm_role(payload_role), ts,
             payload_role, self._norm_role(payload_role)),
        )
        role = await self._member_role(group_openid, user_openid, payload_role)

        segments: list[dict] = []
        reply = await self._reply_segment(data, "group", group_openid, group_virtual)
        if reply is not None:
            segments.append(reply)   # 必须最前: nonebot 只看 message[0] 认引用
        if at_me and not at_in_text:
            # @ 事件的 content 已被平台剥掉 @bot, 这里补回来(nonebot 据此置 to_me)
            segments.append({"type": "at", "data": {"qq": str(self.self_id)}})
            if content_segs and content_segs[0]["type"] == "text":
                content_segs[0]["data"]["text"] = (
                    " " + content_segs[0]["data"]["text"].lstrip())
        segments.extend(content_segs)
        segments.extend(await self._attachments_to_segments(data))
        extra = await self._extra_segments(data, qq_msg_id)
        if any(seg["type"] == "forward" for seg in extra):
            # 已还原成 forward 节点, 原渲染文本不再下发(否则插件照样抠裸 URL)
            segments = [seg for seg in segments if not (
                seg["type"] == "text"
                and _FWD_BLOCK_RE.search(seg["data"].get("text", "")))]
        segments.extend(extra)

        at_me = at_me or at_in_text
        sender = ob_events.make_sender(user_virtual, nickname, role)
        mid = await self.store.record_incoming(
            self.appid, "group", group_openid, group_virtual, user_virtual,
            qq_msg_id, segments, sender, ts, msg_idx=self._msg_idx_of(data),
        )

        # 已落库, 往后一律 return True
        if not await self._group_gate(group_openid, command_text):
            self.note_drop(f"群 {group_virtual}", "群不合规(需开启主动消息/全量接收), 静默")
            return True

        quoted_mid = int(reply["data"]["id"]) if reply else 0
        if builtin is not None:
            consumed = await self.builtin.handle(
                builtin[0], builtin[1], "group", group_openid, user_openid,
                user_virtual, role, mid, quoted_mid=quoted_mid,
            )
            if consumed:
                return True
        elif quoted_mid and not at_others:
            # 引用了某条消息: 可能是在回答内置指令的追问
            if await self.builtin.handle_reply(
                command_text, "group", group_openid, user_openid,
                user_virtual, role, mid, quoted_mid,
            ):
                return True

        if not self.peer_enabled("group", group_openid):
            self.note_drop(f"群 {group_virtual}", "群未启用")
            return True
        if self.quota_blocked("group", group_openid):
            # 日配额耗尽: 回了也发不出去, 不下发(见 core/quota.py)
            self.note_drop(f"群 {group_virtual}", "主动消息配额用完, 静默中")
            return True

        await self.emit(ob_events.group_message_event(
            self.self_id, group_virtual, user_virtual, mid, segments, sender, ts,
        ))
        return True

    async def _on_c2c_message(self, data: dict) -> None:
        author = data.get("author") or {}
        user_openid = author.get("user_openid") or author.get("id", "")
        if not user_openid:
            return
        qq_msg_id = str(data.get("id", ""))
        if qq_msg_id:
            if qq_msg_id in self._inflight_msgs:
                return
            self._inflight_msgs.add(qq_msg_id)
        try:
            if qq_msg_id and await self.store.seen(self.appid, qq_msg_id):
                return
            await self._on_c2c_message_inner(data, qq_msg_id)
        finally:
            self._inflight_msgs.discard(qq_msg_id)

    async def _on_c2c_message_inner(self, data: dict, qq_msg_id: str) -> None:
        author = data.get("author") or {}
        user_openid = author.get("user_openid") or author.get("id", "")
        ts = _parse_ts(data.get("timestamp"))
        user_virtual = await self.idmap.to_virtual(
            self.appid, "user", user_openid,
            union_openid=author.get("union_openid") or None,
        )

        text = (data.get("content") or "").strip()
        segments: list[dict] = []
        reply = await self._reply_segment(data, "private", user_openid, user_virtual)
        if reply is not None:
            segments.append(reply)
        if text:
            segments.append({"type": "text", "data": {"text": text}})
        segments.extend(await self._attachments_to_segments(data))
        extra = await self._extra_segments(data, qq_msg_id)
        if any(seg["type"] == "forward" for seg in extra):
            # 已还原成 forward 节点, 原渲染文本不再下发(否则插件照样抠裸 URL)
            segments = [seg for seg in segments if not (
                seg["type"] == "text"
                and _FWD_BLOCK_RE.search(seg["data"].get("text", "")))]
        segments.extend(extra)

        entry = self.idmap.lookup_virtual(user_virtual)
        sender = ob_events.make_sender(
            user_virtual, entry.nickname if entry else "", "member"
        )
        mid = await self.store.record_incoming(
            self.appid, "private", user_openid, user_virtual, user_virtual,
            qq_msg_id, segments, sender, ts, msg_idx=self._msg_idx_of(data),
        )

        quoted_mid = int(reply["data"]["id"]) if reply else 0
        builtin = match_builtin(text)
        if builtin is not None:
            consumed = await self.builtin.handle(
                builtin[0], builtin[1], "private", user_openid, user_openid,
                user_virtual, "member", mid, quoted_mid=quoted_mid,
            )
            if consumed:
                return
        elif quoted_mid:
            if await self.builtin.handle_reply(
                text, "private", user_openid, user_openid,
                user_virtual, "member", mid, quoted_mid,
            ):
                return

        if not self.peer_enabled("private", user_openid):
            self.note_drop(f"私聊 {user_virtual}", "用户在黑名单")
            return

        await self.emit(ob_events.private_message_event(
            self.self_id, user_virtual, mid, segments, sender, ts,
        ))

    async def _on_group_add_robot(self, data: dict) -> None:
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        op_openid = data.get("op_member_openid", "")
        if not group_openid:
            return
        if op_openid:
            # 能加 bot 的必是群管/群主 -> 记为 admin; 要在启用前写(接着就发获取信息)
            await self.db.execute(
                "INSERT INTO group_members (bot_appid, group_openid, user_openid,"
                " nickname, role, last_seen) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(bot_appid, group_openid, user_openid) DO UPDATE SET"
                " role='admin', last_seen=excluded.last_seen",
                (self.appid, group_openid, op_openid, "", "admin", ts),
            )
        # skip_budget: 入群通知错过即丢
        state = await self.group_state(
            group_openid, refresh=True, reason="add-robot", skip_budget=True)
        if not state.compliant:
            return
        if not self.peer_enabled("group", group_openid):
            return
        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        op_virtual = (
            await self.idmap.to_virtual(self.appid, "user", op_openid)
            if op_openid else 0
        )
        await self.emit(ob_events.notice_event(
            self.self_id, "group_increase", ts, sub_type="invite",
            group_id=group_virtual, operator_id=op_virtual or self.self_id,
            user_id=self.self_id,
        ))

    async def _on_group_del_robot(self, data: dict) -> None:
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        op_openid = data.get("op_member_openid", "")
        if not group_openid:
            return
        state = self._peer_states.get(("group", group_openid))
        compliant = state.compliant if state else False
        # 退群即作废状态缓存, 否则重入群丢了 ADD_ROBOT 时会命中旧缓存直接放行
        async with self._state_lock(group_openid):
            await self._forget_group(group_openid)
        if not compliant:
            return
        if not self.peer_enabled("group", group_openid):
            return
        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        op_virtual = (
            await self.idmap.to_virtual(self.appid, "user", op_openid)
            if op_openid else 0
        )
        await self.emit(ob_events.notice_event(
            self.self_id, "group_decrease", ts, sub_type="kick_me",
            group_id=group_virtual, operator_id=op_virtual or self.self_id,
            user_id=self.self_id,
        ))

    async def _on_friend_add(self, data: dict) -> None:
        ts = _parse_ts(data.get("timestamp"))
        openid = data.get("openid", "")
        if not openid:
            return
        user_virtual = await self.idmap.to_virtual(self.appid, "user", openid)
        if not self.peer_enabled("private", openid):
            return
        await self.emit(ob_events.notice_event(
            self.self_id, "friend_add", ts, user_id=user_virtual,
        ))

    async def _on_friend_del(self, data: dict) -> None:
        # OneBot v11 无好友删除标准事件; 仅记账. TODO(qq-official): 可考虑私有扩展
        openid = data.get("openid", "")
        if not openid:
            return
        await self.idmap.to_virtual(self.appid, "user", openid)
        # 回收该对端的状态与锁, 否则私聊侧只增不减
        self._peer_states.pop(("private", openid), None)
        self._state_locks.pop(openid, None)
        await self.db.execute(
            "DELETE FROM peer_states WHERE bot_appid=? AND chat_type='private'"
            " AND peer_openid=?", (self.appid, openid))

    @staticmethod
    def _member_openid_of(data: dict) -> str:
        for key in ("member_openid", "openid", "requester_openid", "user_openid"):
            if data.get(key):
                return str(data[key])
        author = data.get("author") or {}
        return str(author.get("member_openid") or author.get("id") or "")

    async def _on_group_member_add(self, data: dict) -> None:
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        member_openid = self._member_openid_of(data)
        if not group_openid or not member_openid:
            return
        # 闸放在建映射之前, 免得给未启用群的人白占号段
        state = self._peer_states.get(("group", group_openid))
        if not (state and state.compliant):
            return
        if not self.peer_enabled("group", group_openid):
            return
        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        user_virtual = await self.idmap.to_virtual(self.appid, "user", member_openid)
        op_openid = data.get("op_member_openid", "")
        op_virtual = (
            await self.idmap.to_virtual(self.appid, "user", op_openid)
            if op_openid else 0
        )
        await self.emit(ob_events.notice_event(
            self.self_id, "group_increase", ts, sub_type="approve",
            group_id=group_virtual, operator_id=op_virtual or user_virtual,
            user_id=user_virtual,
        ))

    async def _on_group_member_remove(self, data: dict) -> None:
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        member_openid = self._member_openid_of(data)
        if not group_openid or not member_openid:
            return
        op_openid = data.get("op_member_openid", "")
        # 退群清理与是否启用无关, 照常做
        await self.db.execute(
            "DELETE FROM group_members WHERE bot_appid=? AND group_openid=?"
            " AND user_openid=?",
            (self.appid, group_openid, member_openid),
        )
        state = self._peer_states.get(("group", group_openid))
        if not (state and state.compliant):
            return
        if not self.peer_enabled("group", group_openid):
            return
        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        user_virtual = await self.idmap.to_virtual(self.appid, "user", member_openid)
        op_virtual = (
            await self.idmap.to_virtual(self.appid, "user", op_openid)
            if op_openid else 0
        )
        await self.emit(ob_events.notice_event(
            self.self_id, "group_decrease", ts,
            sub_type="kick" if op_openid and op_openid != member_openid else "leave",
            group_id=group_virtual, operator_id=op_virtual or user_virtual,
            user_id=user_virtual,
        ))

    async def _on_group_join_request(self, data: dict) -> None:
        """入群申请 -> OneBot request.group.add; flag 编码审批所需的 openid 对."""
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        member_openid = self._member_openid_of(data)
        if not group_openid or not member_openid:
            return
        state = self._peer_states.get(("group", group_openid))
        if not (state and state.compliant):
            return
        if not self.peer_enabled("group", group_openid):
            return
        group_virtual = await self.idmap.to_virtual(self.appid, "group", group_openid)
        user_virtual = await self.idmap.to_virtual(self.appid, "user", member_openid)
        await self.emit({
            "time": ts,
            "self_id": self.self_id,
            "post_type": "request",
            "request_type": "group",
            "sub_type": "add",
            "group_id": group_virtual,
            "user_id": user_virtual,
            "comment": str(data.get("comment") or data.get("apply_message") or ""),
            # 第三段是平台申请 ID, 审批接口要回传
            "flag": f"{group_openid}|{member_openid}"
                    f"|{data.get('join_request_id', '')}",
        })

    async def _on_interaction(self, data: dict) -> None:
        """按钮/快捷菜单点击: 3 秒内回执, 再以私有 notice qqbot_interaction 下发."""
        interaction_id = str(data.get("id", ""))
        if interaction_id:
            try:
                await self.api.ack_interaction(interaction_id)
            except Exception as exc:
                logger.warning("[%s] 交互回执失败 %s: %s",
                               self.appid, interaction_id[:16], exc)
        ts = _parse_ts(data.get("timestamp"))
        group_openid = data.get("group_openid", "")
        user_openid = (data.get("group_member_openid")
                       or data.get("user_openid", ""))
        fields: dict = {"interaction_id": interaction_id,
                        "data": data.get("data") or {},
                        "chat_type": data.get("chat_type"),
                        "scene": data.get("scene", "")}
        if group_openid:
            fields["group_id"] = await self.idmap.to_virtual(
                self.appid, "group", group_openid)
        if user_openid:
            fields["user_id"] = await self.idmap.to_virtual(
                self.appid, "user", user_openid)
        # 回执照发, 但下发与消息路径同一名单口径
        if group_openid:
            if not self.peer_enabled("group", group_openid):
                return
        elif user_openid and not self.peer_enabled("private", user_openid):
            return
        await self.emit(ob_events.notice_event(
            self.self_id, "qqbot_interaction", ts, **fields))

    async def _on_subscribe_status(self, data: dict) -> None:
        """订阅消息状态变更: OneBot 无对应语义, 记账即可."""
        self.unparsed.record("SUBSCRIBE_MESSAGE_STATUS", "订阅状态变更(仅记录)",
                             data, self.appid)

    # 群主开关"机器人主动发言" / 用户开关私聊主动消息 -> 免查询更新缓存

    async def _on_group_msg_receive(self, data: dict) -> None:
        openid = data.get("group_openid", "")
        if openid:
            await self._set_proactive_flag("group", openid, True)

    async def _on_group_msg_reject(self, data: dict) -> None:
        openid = data.get("group_openid", "")
        if openid:
            await self._set_proactive_flag("group", openid, False)

    async def _on_c2c_msg_receive(self, data: dict) -> None:
        openid = data.get("openid", "")
        if openid:
            await self._set_proactive_flag("private", openid, True)

    async def _on_c2c_msg_reject(self, data: dict) -> None:
        openid = data.get("openid", "")
        if openid:
            await self._set_proactive_flag("private", openid, False)
