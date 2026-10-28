"""创建bot(仅 su): 扫码授权 -> 建 bot -> 引用「号主？」回填 -> 按默认预设配后端.

号主确认前 bot 只入库不上线(否则已能收消息、响应内置指令).
ask_owner 关闭时跳过回填, 号主记「未知」. 扫码者是号主而非后端主人, 不自动设 su。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time

from ...config import option
from ...db import insert_bot
from ..provision import NoDefaultProfile, apply_profile
from .base import CommandContext, command, reply_hook

logger = logging.getLogger("qqbot.builtin")

# 与前端二维码有效期一致
QR_TIMEOUT_SECONDS = 300
QR_POLL_INTERVAL = 3
OWNER_PROMPT_TTL = 3600

UNKNOWN_OWNER = "号主 未知"

_SPACES_RE = re.compile(r"\s+")

# 待回填号主, 按会话记账而非 mid: 平台引用回来的 idx 会变(落到影子记录).
# 每会话只留最新一次, 新的来就回退旧的半成品 bot.
_pending_owner: dict[tuple[str, str], dict] = {}
OWNER_PROMPT_MARK = "号主？"
# 每会话只对最新二维码报过期/超时; 旧码扫了照样算数, 只是不提示
_latest_qr: dict[tuple[str, str], str] = {}

# asyncio 只持弱引用, 留强引用防 GC
_bg_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


async def _identity(manager, appid: str) -> tuple[str, int, str]:
    """(名称, QQ号, 头像URL); 只拉平台身份不启动 bot, 取不到留空."""
    cfg = await manager.probe_identity(appid)
    return (str(cfg.get("name") or ""), int(cfg.get("bot_qq") or 0),
            str(cfg.get("avatar") or ""))


def owner_id_of(raw: str) -> str:
    """从回复里取号主 QQ 号; 非纯数字返回空串(视为取消)."""
    text = _SPACES_RE.sub("", raw or "").strip()
    if text.startswith("号主"):
        text = text[len("号主"):].strip()
    return text if text.isdigit() else ""


def owner_description(raw: str) -> str:
    """归一成「号主 <id>」(与前端一致)."""
    qq = owner_id_of(raw)
    return f"号主 {qq}" if qq else ""


@command("创建bot", "创建Bot", "创建BOT", exact=True)
async def cmd_create_bot(ctx: CommandContext) -> bool:
    if not ctx.is_su:
        # 非 su 当普通消息转发
        return False
    manager = getattr(ctx.bot, "manager", None)
    if manager is None or manager.http is None:
        await ctx.reply("创建失败：适配器未就绪")
        return True

    from ...qq.qrconnect import create_bind_task  # noqa: PLC0415 循环依赖

    try:
        task = await create_bind_task(manager.http)
    except Exception as exc:
        logger.warning("创建bot 生成授权链接失败: %s", exc)
        await ctx.reply("创建失败：无法生成授权链接，请稍后重试")
        return True
    manager.bind_tasks[task.task_id] = task
    _latest_qr[(ctx.chat_type, ctx.peer_openid)] = task.task_id
    # 发原始链接即可, 发送端会统一处理长链接
    await ctx.reply(
        "请 bot 的创建者点击链接绑定自己的 bot：\n"
        f"{task.connect_url}")
    _spawn(_wait_and_create(ctx, manager, task))
    return True


async def _wait_and_create(ctx: CommandContext, manager, task) -> None:
    """后台轮询扫码结果 -> 建 bot -> 追问号主."""
    from ...qq.qrconnect import (  # noqa: PLC0415
        STATUS_COMPLETED, STATUS_EXPIRED, decrypt_secret, poll_bind_result,
    )

    key = (ctx.chat_type, ctx.peer_openid)

    def is_latest() -> bool:
        return _latest_qr.get(key) == task.task_id

    deadline = time.time() + QR_TIMEOUT_SECONDS
    data = None
    while time.time() < deadline:
        await asyncio.sleep(QR_POLL_INTERVAL)
        try:
            result = await poll_bind_result(manager.http, task.task_id)
        except Exception as exc:
            logger.warning("创建bot 轮询失败: %s", exc)
            continue
        status = int(result.get("status", 0))
        if status == STATUS_EXPIRED:
            manager.bind_tasks.pop(task.task_id, None)
            if is_latest():
                _latest_qr.pop(key, None)
                await ctx.say("授权链接已过期")
            else:
                logger.info("创建bot: 旧二维码 %s… 过期(已被新码顶替), 不吱声",
                            task.task_id[:8])
            return
        if status == STATUS_COMPLETED:
            data = result
            break
    manager.bind_tasks.pop(task.task_id, None)
    if data is None:
        if is_latest():
            _latest_qr.pop(key, None)
            await ctx.say("等待授权超时，请重新发送「创建bot」")
        else:
            logger.info("创建bot: 旧二维码 %s… 等待超时(已被新码顶替), 不吱声",
                        task.task_id[:8])
        return
    if is_latest():
        _latest_qr.pop(key, None)

    appid = str(data.get("bot_appid", ""))
    try:
        secret = decrypt_secret(task.key_b64, data["bot_encrypt_secret"])
    except Exception as exc:
        logger.warning("创建bot 解密 secret 失败: %s", exc)
        await ctx.say("授权结果异常，请重新发送「创建bot」")
        return
    if not appid or not secret:
        await ctx.say("授权结果不完整，请重新发送「创建bot」")
        return

    exists = await manager.db.fetchone(
        "SELECT appid FROM bots WHERE appid=?", (appid,))
    if exists:
        # 不覆盖已有 bot(secret 可能正在用)
        await ctx.say(f"该 bot({appid}) 已存在，未做任何改动")
        return

    # 先不启用, 号主确认后由 _finish 放上线
    await insert_bot(manager.db, getattr(manager, "config", None),
                     {"appid": appid, "secret": secret, "enabled": 0})
    if hasattr(manager, "mark_setup_done"):
        await manager.mark_setup_done()
    logger.info("创建bot: %s 已创建, 等待号主回填", appid)

    name, bot_qq, avatar = await _identity(manager, appid)
    # 回显身份, 让 su 确认扫的是哪个号
    shown = name or "(未取到名称)"
    if bot_qq:
        shown += f"  QQ {bot_qq}"
    if not option(manager, "ask_owner", True):
        await ctx.say(f"已接入: {shown}\nAppID: {appid}\n"
                      + await _finish(manager, appid, UNKNOWN_OWNER), image=avatar)
        return
    # 引用「创建bot」那条, su 不盯着群也能收到提醒
    mid = await ctx.say(f"已接入: {shown}\nAppID: {appid}\n号主？",
                        image=avatar, quote=True)
    if mid is None:
        await ctx.say("bot 已创建但未启用，我没法追问号主，请到管理台配置后端并启用")
        return
    key = (ctx.chat_type, ctx.peer_openid)
    stale = _pending_owner.get(key)
    if stale and stale.get("appid") != appid:
        logger.info("创建bot: 会话内已有未完成的 %s, 撤掉它", stale["appid"])
        await _rollback(ctx, stale["appid"])
        await ctx.say(f"上一次未完成的创建({stale['appid']})已取消")
    _pending_owner[key] = {
        "appid": appid, "asker": ctx.user_virtual, "created": time.time(),
        "mid": int(mid),
    }
    # 超时无回复也回退半成品 bot
    _spawn(_expire_pending(ctx, key, appid))


async def _expire_pending(ctx: CommandContext, key: tuple, appid: str) -> None:
    await asyncio.sleep(OWNER_PROMPT_TTL)
    pending = _pending_owner.get(key)
    if pending is None or pending.get("appid") != appid:
        return          # 已回答/取消/被顶掉
    _pending_owner.pop(key, None)
    await _rollback(ctx, appid)
    await ctx.say("超时未收到号主，已取消本次创建")


async def _rollback(ctx: CommandContext, appid: str) -> None:
    """撤销创建: 删掉刚建的 bot 及数据."""
    manager = getattr(ctx.bot, "manager", None)
    if manager is None:
        return
    try:
        await manager.remove_bot(appid)
        logger.info("创建bot 回退: 已删除未完成的 bot %s", appid)
    except Exception:
        logger.exception("创建bot 回退失败 appid=%s", appid)


@reply_hook
async def on_owner_reply(ctx: CommandContext) -> bool:
    """su 引用「号主？」并回一个 QQ 号 -> 写描述并自动配后端; 回别的即取消."""
    key = (ctx.chat_type, ctx.peer_openid)
    pending = _pending_owner.get(key)
    if pending is None:
        return False
    if not ctx.is_su or ctx.user_virtual != pending["asker"]:
        return False
    # mid 对不上时比对文本标记: 平台换 idx 时引用会落到影子记录
    if ctx.quoted_mid != pending.get("mid"):
        quoted = await ctx.quoted_text()
        if OWNER_PROMPT_MARK not in quoted:
            return False
    if time.time() - pending["created"] > OWNER_PROMPT_TTL:
        _pending_owner.pop(key, None)
        await _rollback(ctx, pending["appid"])
        await ctx.reply("已过期，请重新发送「创建bot」")
        return True

    description = owner_description(ctx.arg or ctx.command)
    _pending_owner.pop(key, None)
    if not description:
        await _rollback(ctx, pending["appid"])
        await ctx.reply("未收到 QQ 号，已取消本次创建")
        return True

    manager = getattr(ctx.bot, "manager", None)
    await ctx.reply(await _finish(manager, pending["appid"], description))
    return True


async def _finish(manager, appid: str, description: str) -> str:
    """启用 bot 并配后端, 返回回执; 失败原因只进日志, 群里不回显运维细节."""
    await manager.db.execute("UPDATE bots SET enabled=1 WHERE appid=?", (appid,))
    try:
        result = await provision_backend(manager, appid, description)
    except NoDefaultProfile:
        receipt = "✅ 完成（未设置默认后端预设）"
    except Exception as exc:
        logger.warning("创建bot 配后端失败 appid=%s: %s", appid, exc)
        receipt = "bot 已创建，但配置后端失败，可到管理台「配置后端」重试"
    else:
        # 配后端成功时已顺带重载上线
        return ("✅ 完成" if result["applied"]
                else "✅ 完成（已写入 BS 配置文件，需重启 BS 后生效）")
    await manager.reload_bot(appid)
    return receipt


async def provision_backend(manager, appid: str, description: str) -> dict:
    """用默认预设给指定 bot 配后端(直连 OneBot 或经 BotShepherd)."""
    return await apply_profile(manager, appid, description)
