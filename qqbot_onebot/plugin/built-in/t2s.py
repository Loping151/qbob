"""繁体转简体: 下发给 OneBot 后端的消息, 文本段按 OpenCC 的词语 + 单字表转成简体.

只改发给后端的那一份(库里的记录、透传的原始事件不动), @、图片等其它段不碰。
人名、日文昵称里的繁体字也会被转(乾纱凪 -> 干纱凪), 按名字查东西的指令可能对不上, 所以默认关。
"""

from qqbot_onebot.core.t2s import to_simplified
from qqbot_onebot.onebot.segments import segments_to_cq
from qqbot_onebot.plugin import Plugin

plugin = Plugin(
    name="t2s",
    title="繁体转简体",
    description="收到的消息转成简体再交给后端, 繁体用户也能触发指令; 人名里的繁体字也会被转",
    default_enabled=False,
)


@plugin.hook("event_in")
async def convert(ctx, event):
    segments = event.get("message")
    if not isinstance(segments, list):
        return None
    out, changed = [], False
    for seg in segments:
        data = seg.get("data") if isinstance(seg, dict) else None
        if isinstance(data, dict) and seg.get("type") == "text":
            text = str(data.get("text") or "")
            simplified = to_simplified(text)
            if simplified != text:
                seg = {**seg, "data": {**data, "text": simplified}}
                changed = True
        out.append(seg)
    if not changed:
        return None
    return {**event, "message": out, "raw_message": segments_to_cq(out)}
