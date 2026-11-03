"""合并转发转网页: 群里只发一张「聊天记录」卡片或一条链接; 图片上传平台后由 QQ CDN 直出,
本服务只出 HTML(群聊近期没见过群图时还有卡片封面).

默认开「使用腾讯文档跳转」: 卡片经 docs.qq.com 的外链跳转页打开网页, 可在 QQ 内直接浏览(见 core/cards).
关掉后默认发链接, 「不跳转时也发卡片」才发卡片. 私聊、无可用网页地址、兑换码类内容(要能复制)时
退回逐条摊平。
"""

from qqbot_onebot.core.forwardpage import page_base
from qqbot_onebot.core.sender import forward_has_copy_codes
from qqbot_onebot.plugin import ConfigField, Plugin

plugin = Plugin(
    name="forward_page",
    title="合并转发转网页",
    description="群聊的合并转发做成一页网页，群里只发一张「聊天记录」卡片或一条链接。"
                "网页由本服务的公网面（config.json 的 port，默认 17800）提供，"
                "要让群友能打开，这个端口得从公网用 https 访问到：有公网 IP 就用 nginx/caddy "
                "反代，没有就用 frp、Cloudflare Tunnel 之类穿透。"
                "没有可用的网页地址时不生效，照常逐条发送",
    doc="https://github.com/Loping151/qbob/blob/main/docs/deploy.md#公网访问",
    config=[
        ConfigField("base_url", "网页地址", placeholder="https://forward.example.com",
                    help="公网访问本服务公网面的 https 地址，群里的链接形如 {网页地址}/f/xxxx。"
                         "留空则用 config.json 的 forward_base_url，再没有就用 public_base_url。"
                         "用单独的域名时，这个域名只能打开转发网页，访问不到管理台和媒体"),
        ConfigField("tdocs_jump", "使用腾讯文档跳转", type="bool", default=True,
                    help="发「聊天记录」卡片，点开经腾讯文档的外链跳转页（docs.qq.com）打开网页，"
                         "可在 QQ 内直接浏览"),
        ConfigField("own_url_card", "不跳转时也发卡片", type="bool", default=False,
                    help="关掉腾讯文档跳转时，卡片直接打开本服务的网页，QQ 可能提示用浏览器访问。"
                         "默认发链接"),
    ],
)


def _status(manager) -> str:
    """地址非本插件所填时才显示来源."""
    base, source = page_base(getattr(manager, "config", None))
    if source == "plugin":
        return ""
    if not base:
        return "没有可用的网页地址, 不生效", "error"
    return f"当前使用 config.json 的 {source}: {base}"


plugin.status = _status


@plugin.hook("forward_out")
async def to_page(ctx, nodes):
    if ctx.chat_type != "group":
        return None
    base, _ = page_base(getattr(getattr(ctx.bot, "manager", None), "config", None))
    if not base or forward_has_copy_codes(nodes):
        return None
    jump = bool(plugin.config.get("tdocs_jump", True))
    if jump or plugin.config.get("own_url_card"):
        return await ctx.bot.sender.build_forward_card(ctx.plan, nodes, base, jump=jump)
    return await ctx.bot.sender.build_forward_page(
        ctx.plan, nodes, ctx.peer_openid, base)
