"""长文本转图片: 超阈值的文本按 markdown 渲染成图, 链接另发一条(图里点不了).

兑换码类、带指令按钮、内置指令回复不转; 渲染失败原样发文本. Pillow 直接排版, 要有中文字体(见 docs/deploy.md)。
"""

from qqbot_onebot.core.textimg import TEXT_IMAGE_MIN_CHARS
from qqbot_onebot.plugin import ConfigField, Plugin

plugin = Plugin(
    name="text_image",
    title="长文本转图片",
    description="超过阈值的长文本渲染成图片发送，链接另发一条可点的",
    config=[
        ConfigField("min_chars", "字数阈值", type="int", default=TEXT_IMAGE_MIN_CHARS,
                    help="正文(去掉 @ 后)超过这么多字才转图"),
    ],
)


@plugin.hook("payload_out")
async def to_image(ctx, payload):
    out = await ctx.bot.sender._maybe_imagify(
        payload, min_chars=int(plugin.config.get("min_chars") or TEXT_IMAGE_MIN_CHARS))
    return None if out == [payload] else out
