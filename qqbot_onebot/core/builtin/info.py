"""获取信息: 回显 bot 与本群接入信息(仅虚拟号, 不暴露 openid), 供跨 bot 加白.

群内限群管/su, 私聊不限; 初次部署时群里也放开, 好让部署者查到自己的 id 设 su。
"""

from __future__ import annotations

import logging
import time

from .base import CommandContext, command

logger = logging.getLogger("qqbot.builtin")

INFO_THROTTLE_SECONDS = 60
_info_last: dict[str, float] = {}     # group_openid -> ts

RECV_LABELS = {
    "all": "全部消息",
    "only_mention": "仅@消息",
    "mention_and_context": "@及上下文",
}
ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "普通成员"}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(str(role or "").lower(), "普通成员")


async def _group_profile_lines(ctx: CommandContext) -> list[str]:
    """群资料(群名/人数/简介); 接口失败返回空."""
    try:
        info = await ctx.bot.api.group_info(ctx.peer_openid)
    except Exception as exc:
        logger.warning("[%s] group_info(%s…) failed: %s",
                       ctx.bot.appid, ctx.peer_openid[:12], exc)
        return []
    lines: list[str] = []
    if info.get("group_name"):
        lines.append(f"群名: {info['group_name']}")
    if info.get("group_member_num"):
        lines.append(f"群人数: {info['group_member_num']}")
    memo = " ".join(str(info.get("group_finger_memo") or "").split())
    if len(memo) > 200:
        memo = memo[:200] + "…"
    lines.append(f"群简介: {memo or '(未设置)'}")
    return lines


def setup_mode(bot) -> bool:
    """初次部署(仅一个 bot 且无 su); 一旦不满足就置 setup_done, 永不再开."""
    manager = getattr(bot, "manager", None)
    config = getattr(manager, "config", None)
    if manager is None or getattr(config, "setup_done", True):
        return False
    if len(manager.bots) == 1 and not bot.superusers:
        return True
    config.setup_done = True
    config.save()
    return False


@command("获取信息")
async def cmd_info(ctx: CommandContext) -> bool:
    bot = ctx.bot
    setup = setup_mode(bot)
    # 群消息里平台会下发 member_role; 普通成员拒绝, 提示按群节流
    if ctx.chat_type == "group" and not (ctx.is_group_admin or ctx.is_su or setup):
        now = time.time()
        if now - _info_last.get(ctx.peer_openid, 0) < INFO_THROTTLE_SECONDS:
            return True
        _info_last[ctx.peer_openid] = now
        await ctx.reply("仅群主/管理员可使用「获取信息」")
        return True

    # BotID/群ID 已在启用码里, 不单列
    lines = ["【QQBot-OneBot 信息】", f"AppID: {bot.appid}"]
    if bot.cfg.get("bot_qq"):
        lines.append(f"BotQQ: {bot.cfg['bot_qq']}")
    if ctx.chat_type == "group":
        group_virtual = await bot.virtual_for_peer("group", ctx.peer_openid)
        lines.extend(await _group_profile_lines(ctx))
    lines.append(f"用户ID: {ctx.user_virtual}")
    if ctx.chat_type == "private":
        profile = await bot.user_profile(ctx.peer_openid, force=True)
        if profile is not None and isinstance(profile.get("allow_proactive_msg"), bool):
            lines.append("主动消息: "
                         + ("已开通" if profile["allow_proactive_msg"] else "未开通"))
    if ctx.chat_type == "group":
        lines.append(f"角色: {role_label(ctx.member_role)}")
        # 强制刷新(受 30s/群 节流)
        state = await bot.group_state(ctx.peer_openid, refresh=True,
                                      reason="cmd-info")
        recv_label = RECV_LABELS.get(state.recv_msg_setting,
                                     state.recv_msg_setting or "未知")
        if state.api_ok:
            lines.append(f"主动消息: {'已开通' if state.allow_proactive else '未开通'}")
            lines.append(f"消息接收: {recv_label}")
        else:
            # 接口失败: 显示"未知"及当前推断, 而非"未开通"
            proactive = (
                ("已开通" if state.allow_proactive else "未开通")
                if state.proactive_known else "未知(暂按开通处理)"
            )
            lines.append(f"主动消息: {proactive}")
            lines.append(
                "消息接收: "
                + ("全部消息(据收到的非@消息推断)" if state.inferred_recv_all
                   else recv_label))
            lines.append("状态来源: 接口暂不可用，按推断判定")
        lines.append("Bot角色: "
                     + (role_label(state.bot_role) if state.bot_role else "未知"))
        whitelisted = bot.access.allowed(bot.appid, "white", "group",
                                         ctx.peer_openid)
        lines.append(f"白名单: {'已启用' if whitelisted else '未启用'}")
        lines.append(f"启用码: QQOB1|bot={bot.self_id}|group={group_virtual}")
    lines.append(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    if setup:
        lines.append("尚未设置超级用户: 把上面的用户ID填进 superusers")
    await ctx.reply("\n".join(lines))
    return True
