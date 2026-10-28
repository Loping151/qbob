"""QQ 官方机器人 api-v2 REST 客户端(单 bot 一实例).

- access_token 提前 60s 刷新, 加锁单飞.
- token 端点与 API base 有新旧两套域名, 首次成功后固定.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

logger = logging.getLogger("qqbot.api")


class RateLimiter:
    """滑动窗口限流, 超额排队不抛错(上传类接口 QPS: 预上传/分片完成 10, 富媒体 50)."""

    def __init__(self, qps: int, name: str = ""):
        self.qps = max(1, qps)
        self.name = name
        self._hits: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        # 持锁等待即 FIFO
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= 1.0:
                    self._hits.popleft()
                if len(self._hits) < self.qps:
                    self._hits.append(now)
                    return
                wait = 1.0 - (now - self._hits[0]) + 0.005
                logger.debug("rate limit %s: 排队 %.3fs", self.name, wait)
                await asyncio.sleep(wait)

# 凭据的确定性判决, 不会自愈
# 100016 invalid appid or secret(注销的 bot 也报); 100007 appid invalid(bot 过期/被删, 2026-10 实测)
INVALID_CREDENTIAL_CODES = {100016, 100007}
# "bot 已死": 11265 已封禁. 11254 只是单个接口被封, 不算
FATAL_BOT_CODES = {11265}
# 禁言到期补偿秒数(接口往返约 2 秒)
MUTE_LATENCY_PAD = 2


class QQApiError(Exception):
    def __init__(self, status: int, code: int, message: str, trace_id: str = ""):
        self.status = status
        self.code = code
        self.message = message
        self.trace_id = trace_id
        super().__init__(f"HTTP {status} code={code} {message} trace={trace_id}")


class QQApiClient:
    def __init__(
        self,
        appid: str,
        secret: str,
        token_urls: list[str],
        api_bases: list[str],
        session: aiohttp.ClientSession,
    ):
        self.appid = appid
        self.secret = secret
        self._token_urls = list(token_urls)
        self._api_bases = list(api_bases)
        self._session = session
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()
        self._api_base: str | None = None
        # 连续凭据错误计数(拿到 token 即清零); BotInstance 注入回调做自动禁用
        self.credential_failures = 0
        self.on_credential_error = None  # (count: int, exc: QQApiError) -> None
        # 封禁类错误单独计数: 封禁的 bot 仍能拿 token, 混用会被清零
        self.fatal_code_failures = 0
        self.on_fatal_code = None  # (count: int, exc: QQApiError) -> None
        # 上传类 QPS 闸(文档值留一成余量)
        self.rl_upload = RateLimiter(45, "files")
        self.rl_prepare = RateLimiter(9, "upload_prepare")
        self.rl_part = RateLimiter(9, "upload_part_finish")

    # ---------------- token ----------------

    async def get_token(self) -> str:
        if self._token and time.time() < self._token_expire_at - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_expire_at - 60:
                return self._token
            last_exc: Exception | None = None
            for url in self._token_urls:
                try:
                    async with self._session.post(
                        url,
                        json={"appId": self.appid, "clientSecret": self.secret},
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        data = await resp.json(content_type=None)
                    if resp.status == 200 and data.get("access_token"):
                        self._token = data["access_token"]
                        self._token_expire_at = time.time() + int(
                            data.get("expires_in", 7200)
                        )
                        # 固定住可用端点
                        self._token_urls = [url]
                        self.credential_failures = 0
                        return self._token
                    last_exc = QQApiError(
                        resp.status, int(data.get("code", -1)),
                        str(data.get("message", data)),
                    )
                except QQApiError as exc:
                    last_exc = exc
                except Exception as exc:  # 网络/DNS
                    last_exc = exc
                logger.warning("token endpoint %s failed for %s: %s", url, self.appid, last_exc)
            # 所有端点都判凭据无效才计一次
            if isinstance(last_exc, QQApiError) \
                    and last_exc.code in INVALID_CREDENTIAL_CODES:
                self.credential_failures += 1
                if self.on_credential_error is not None:
                    try:
                        self.on_credential_error(self.credential_failures, last_exc)
                    except Exception:
                        logger.exception("on_credential_error callback failed")
            raise last_exc or RuntimeError("no token url configured")

    # ---------------- request core ----------------

    async def request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        retry_auth: bool = True,
    ) -> Any:
        token = await self.get_token()
        bases = [self._api_base] if self._api_base else list(self._api_bases)
        last_exc: Exception | None = None
        for base in bases:
            url = base + path
            try:
                async with self._session.request(
                    method,
                    url,
                    json=json_body,
                    headers={"Authorization": f"QQBot {token}"},
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (200, 201, 202, 204):
                        self._api_base = base
                        if resp.content_type and "json" in resp.content_type:
                            body = await resp.json(content_type=None)
                        else:
                            text = await resp.text()
                            body = {} if not text else {"raw": text}
                        # 201/202 带 err_code 的异步审核态也算成功(消息进审核)
                        return body
                    body = await resp.json(content_type=None)
                    code = int(body.get("code", body.get("err_code", -1)) or -1)
                    message = str(body.get("message", body))
                    trace = resp.headers.get("X-Tps-trace-ID", "")
                    if resp.status == 401 and retry_auth:
                        # token 失效, 强刷一次
                        self._token = ""
                        return await self.request(method, path, json_body, retry_auth=False)
                    if resp.status == 404 and self._api_base is None:
                        # 可能是错误的 base, 试下一个
                        last_exc = QQApiError(resp.status, code, message, trace)
                        continue
                    exc = QQApiError(resp.status, code, message, trace)
                    if code in FATAL_BOT_CODES:
                        self.fatal_code_failures += 1
                        if self.on_fatal_code is not None:
                            try:
                                self.on_fatal_code(self.fatal_code_failures, exc)
                            except Exception:
                                logger.exception("on_fatal_code callback failed")
                    else:
                        self.fatal_code_failures = 0
                    raise exc
            except QQApiError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning("api base %s failed: %s", base, exc)
        assert last_exc is not None
        raise last_exc

    async def raw(self, method: str, path_qs: str, body: bytes,
                  content_type: str = "") -> tuple[int, str, bytes]:
        """透传: 原样转发, 返回 (状态码, Content-Type, 响应体); 401 时刷 token 重试一次."""
        headers = {}
        if content_type:
            headers["Content-Type"] = content_type
        for attempt in (0, 1):
            token = await self.get_token()
            headers["Authorization"] = f"QQBot {token}"
            bases = [self._api_base] if self._api_base else list(self._api_bases)
            last_exc: Exception | None = None
            for base in bases:
                try:
                    async with self._session.request(
                        method, base + path_qs, data=body or None, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=60),
                    ) as resp:
                        data = await resp.read()
                        if resp.status == 404 and self._api_base is None:
                            last_exc = QQApiError(404, -1, "not found")
                            continue
                        if resp.status == 401 and attempt == 0:
                            self._token = ""
                            break
                        if resp.status < 400:
                            self._api_base = base
                        return resp.status, resp.content_type or "", data
                except Exception as exc:
                    last_exc = exc
                    logger.warning("api base %s failed: %s", base, exc)
            else:
                if last_exc is not None and not isinstance(last_exc, QQApiError):
                    raise last_exc
                return 404, "application/json", b'{"code":404,"message":"not found"}'
        return 401, "application/json", b'{"code":401,"message":"unauthorized"}'

    # ---------------- messages ----------------

    async def send_group_message(self, group_openid: str, body: dict) -> dict:
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/messages", body
        )

    async def send_c2c_message(self, user_openid: str, body: dict) -> dict:
        return await self.request(
            "POST", f"/v2/users/{user_openid}/messages", body
        )

    @staticmethod
    def _media_base(chat_type: str, peer_openid: str) -> str:
        return (f"/v2/groups/{peer_openid}" if chat_type == "group"
                else f"/v2/users/{peer_openid}")

    async def upload_group_media(
        self, group_openid: str, file_type: int, url: str, file_name: str = ""
    ) -> dict:
        """file_type: 1 图片 2 视频 3 语音 4 文件; 仅公网 URL. file_name 缺省取 URL 末段."""
        body: dict = {"file_type": file_type, "url": url, "srv_send_msg": False}
        if file_name:
            body["file_name"] = file_name
        await self.rl_upload.acquire()
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/files", body
        )

    async def upload_c2c_media(
        self, user_openid: str, file_type: int, url: str, file_name: str = ""
    ) -> dict:
        body: dict = {"file_type": file_type, "url": url, "srv_send_msg": False}
        if file_name:
            body["file_name"] = file_name
        await self.rl_upload.acquire()
        return await self.request(
            "POST", f"/v2/users/{user_openid}/files", body
        )

    # ---------------- 分片直传(不需要公网图床) ----------------

    async def upload_prepare(
        self, chat_type: str, peer_openid: str, file_type: int, file_size: int,
        file_name: str, md5: str, sha1: str, md5_10m: str,
    ) -> dict:
        """预上传: 拿 upload_id 与各分片的预签名 URL(直传腾讯 COS)."""
        await self.rl_prepare.acquire()
        return await self.request(
            "POST", f"{self._media_base(chat_type, peer_openid)}/upload_prepare",
            {"file_type": file_type, "file_size": str(file_size),
             "file_name": file_name, "md5": md5, "sha1": sha1,
             "md5_10m": md5_10m},
        )

    async def upload_part_finish(
        self, chat_type: str, peer_openid: str, upload_id: str,
        part_index: int, block_size: int, md5: str,
    ) -> dict:
        await self.rl_part.acquire()
        return await self.request(
            "POST",
            f"{self._media_base(chat_type, peer_openid)}/upload_part_finish",
            {"upload_id": upload_id, "part_index": part_index,
             "block_size": str(block_size), "md5": md5},
        )

    async def upload_merge(
        self, chat_type: str, peer_openid: str, file_type: int,
        upload_id: str, file_name: str,
    ) -> dict:
        """分片合并 -> file_info."""
        await self.rl_upload.acquire()
        return await self.request(
            "POST", f"{self._media_base(chat_type, peer_openid)}/files",
            {"file_type": file_type, "srv_send_msg": False,
             "file_name": file_name, "upload_id": upload_id},
        )

    async def recall_group_message(self, group_openid: str, message_id: str) -> None:
        await self.request(
            "DELETE", f"/v2/groups/{group_openid}/messages/{message_id}"
        )

    async def recall_c2c_message(self, user_openid: str, message_id: str) -> None:
        await self.request(
            "DELETE", f"/v2/users/{user_openid}/messages/{message_id}"
        )

    # ---------------- group management ----------------

    async def group_info(self, group_openid: str) -> dict:
        """GET /v2/groups/{group_openid}/info ->
        group_openid, group_name, group_finger_memo, group_class_text,
        group_tags[], group_member_num."""
        data = await self.request("GET", f"/v2/groups/{group_openid}/info")
        if isinstance(data.get("data"), dict):
            data = data["data"]
        return data

    async def get_restrict_chat_setting(self, group_openid: str) -> dict:
        return await self.request(
            "GET", f"/v2/groups/{group_openid}/restrict_chat_setting"
        )

    async def set_member_mute(
        self, group_openid: str, member_openid: str, mute_expire_at: int
    ) -> dict:
        """禁言/解禁普通成员(bot 须为群管); mute_expire_at 为 unix 秒, 0 = 解除.

        平台要 RFC3339 字符串(传时间戳报 10007); 到期时刻补 MUTE_LATENCY_PAD 秒.
        """
        if mute_expire_at > 0:
            mute_expire_at += MUTE_LATENCY_PAD
            expire = datetime.fromtimestamp(
                mute_expire_at, tz=timezone(timedelta(hours=8))).isoformat()
            member = {"op": "add", "member_openid": member_openid,
                      "mute_expire_at": expire}
        else:
            member = {"op": "del", "member_openid": member_openid,
                      "mute_expire_at": ""}
        return await self.request(
            "POST", f"/v2/groups/{group_openid}/restrict_chat_setting",
            {"members": [member]},
        )

    # 注: 群聊 api-v2 没有全员禁言. POST 只认 members[], global_rule 只读(塞了被静默忽略).

    async def approve_join_request(
        self, group_openid: str, member_openid: str, approve: bool,
        reject_reason: str = "", join_request_id: str = "",
    ) -> dict:
        """审批入群申请; 回传事件里的 join_request_id(同一人可能有多条挂起申请)."""
        body: dict = {"op": "approve" if approve else "decline"}
        if join_request_id:
            body["join_request_id"] = join_request_id
        if not approve and reject_reason:
            body["reject_reason"] = reject_reason
        return await self.request(
            "POST",
            f"/v2/groups/{group_openid}/approval_join_request/{member_openid}",
            body,
        )

    async def join_request_list(self, group_openid: str) -> dict:
        return await self.request(
            "GET", f"/v2/groups/{group_openid}/join_request_list"
        )

    async def ack_interaction(self, interaction_id: str, code: int = 0) -> dict:
        """回执按钮/快捷菜单点击, 须 3 秒内.

        code: 0 成功 / 1 操作失败 / 2 操作频繁 / 3 重复操作 / 4 无权限 / 5 仅管理员可操作
        """
        return await self.request(
            "PUT", f"/interactions/{interaction_id}", {"code": code}
        )

    # ---------------- misc ----------------

    async def me(self) -> dict:
        return await self.request("GET", "/users/@me")

    async def group_bot_state(self, group_openid: str) -> dict:
        """机器人群内状态 GET /v2/groups/{group_openid}/bot_state (30 QPM).

        响应: member_openid, joined_at(RFC3339), allow_proactive_msg(bool),
        recv_msg_setting('all'|'only_mention'|'mention_and_context'),
        member_role('member'|'admin'|'owner').
        """
        data = await self.request("GET", f"/v2/groups/{group_openid}/bot_state")
        if isinstance(data.get("data"), dict):
            data = data["data"]
        return {
            **data,
            "allow_proactive_msg": bool(data.get("allow_proactive_msg", False)),
            "recv_msg_setting": str(data.get("recv_msg_setting", "only_mention")),
            "member_role": str(data.get("member_role", "member")),
        }

    async def gateway_url(self) -> str:
        data = await self.request("GET", "/gateway")
        return data["url"]
