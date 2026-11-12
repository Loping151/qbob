# 头像适配

本适配器的用户 ID 是 15 位数字(`[10^14, 10^15)`), 不是 QQ 号。按 QQ 号拼的头像地址
(`q.qlogo.cn/g?b=qq&nk=<ID>`)对它**不会报错**, 而是返回 200 + 默认企鹅图, 所以不能靠"请求失败再回退"
来发现问题, 要**按位数分流**:

- 15 位: 调扩展动作 `get_additional_user_detail`, 用返回的 `avatar.data`;
- 其它: 照旧拼 qlogo。

`get_additional_user_detail` 的参数与返回见[接口文档](api.md#get_additional_user_detail)。要点:

- 返回的地址形如 `https://q.qlogo.cn/qqapp/{appid}/{openid}/{0|100|640}`, 对同一个人固定不变, 可以长期缓存;
- **用哪条 bot 连接查都行**: ID 映射全实例共用, 返回的 `appid` 是该 ID 所属的 bot(0.1.8 及以前只有那个 bot 认得, 别的返回 1404);
- `size`: 0 原图、≤100 小图、其它 640。

下面各框架的补丁都基于写作时(2026-09-29)各仓库的最新提交, 链接固定到该提交; 仓库后来变了, 按链接对照
改动位置即可。

## 通用写法

```python
def is_qqbot_id(uid) -> bool:
    s = str(uid).strip()
    return len(s) == 15 and s.isdigit()


async def avatar_url(bot, user_id, size: int = 640) -> str:
    fallback = f"https://q.qlogo.cn/g?b=qq&nk={user_id}&s={size}"
    if not is_qqbot_id(user_id):
        return fallback
    try:
        data = await bot.call_api("get_additional_user_detail", user_id=int(user_id), size=size)
        avatar = (data or {}).get("avatar") or {}
        if avatar.get("type") == "url" and avatar.get("data"):
            return avatar["data"]
    except Exception:
        pass
    return fallback
```

## NoneBot2

适配器 [nonebot/adapter-onebot](https://github.com/nonebot/adapter-onebot/tree/58bb4874768bad06ba5a60baa4357e6d8a21ce49)
(`58bb487`)本身不拼头像, 插件自己拼。插件里直接用上面的 `avatar_url(bot, user_id)`, `bot` 用事件对应的那个。
`call_api` 在 `status == "failed"` 时抛 `ActionFailed`
([utils.py#L44-L58](https://github.com/nonebot/adapter-onebot/blob/58bb4874768bad06ba5a60baa4357e6d8a21ce49/nonebot/adapters/onebot/v11/utils.py#L44-L58))。

常用的用户信息插件需要各改一处:

### nonebot-plugin-userinfo

[noneplugin/nonebot-plugin-userinfo@5f5a53d](https://github.com/noneplugin/nonebot-plugin-userinfo/tree/5f5a53d955f5d347ac2fb2e01237b653c9c292f4)

- `check_qq_number` 只认 5~11 位([utils.py#L23-L24](https://github.com/noneplugin/nonebot-plugin-userinfo/blob/5f5a53d955f5d347ac2fb2e01237b653c9c292f4/nonebot_plugin_userinfo/utils.py#L23-L24)),
  15 位 ID 连用户信息都拿不到;
- 头像是 `QQAvatar`(qlogo)([adapters/onebot_v11.py#L36-L83](https://github.com/noneplugin/nonebot-plugin-userinfo/blob/5f5a53d955f5d347ac2fb2e01237b653c9c292f4/nonebot_plugin_userinfo/adapters/onebot_v11.py#L36-L83))。

改 `adapters/onebot_v11.py`:

```python
from ..image_source import ImageUrl, QQAvatar

def _is_qqbot_id(uid: str) -> bool:
    return len(uid) == 15 and uid.isdigit()

async def _avatar(bot, user_id: str):
    if _is_qqbot_id(user_id):
        try:
            d = await bot.call_api("get_additional_user_detail", user_id=int(user_id), size=640)
            av = (d or {}).get("avatar") or {}
            if av.get("type") == "url" and av.get("data"):
                return ImageUrl(url=av["data"])
        except ActionFailed:
            pass
    return QQAvatar(qq=int(user_id))

# _get_info 里:
-            if not check_qq_number(user_id):
+            if not check_qq_number(user_id) and not _is_qqbot_id(user_id):
                 return None
 ...
-                    user_avatar=QQAvatar(qq=qq),
+                    user_avatar=await _avatar(self.bot, str(qq)),
 ...
-                user_avatar=QQAvatar(qq=int(user_id)),
+                user_avatar=await _avatar(self.bot, user_id),
```

### nonebot-plugin-uninfo

[RF-Tar-Railt/nonebot-plugin-uninfo@843659d](https://github.com/RF-Tar-Railt/nonebot-plugin-uninfo/tree/843659dd5281dc6015d04e5f46222971255497b5)

头像在 onebot11 适配的 `extract_user` / `extract_member` 里拼
([adapters/onebot11/main.py#L51-L105](https://github.com/RF-Tar-Railt/nonebot-plugin-uninfo/blob/843659dd5281dc6015d04e5f46222971255497b5/src/nonebot_plugin_uninfo/adapters/onebot11/main.py#L51-L105)),
这几个方法是同步的; 统一经过的异步入口是 `fetch`
([fetch.py#L90-L142](https://github.com/RF-Tar-Railt/nonebot-plugin-uninfo/blob/843659dd5281dc6015d04e5f46222971255497b5/src/nonebot_plugin_uninfo/fetch.py#L90-L142))。
在 onebot11 的 `InfoFetcher` 里覆盖:

```python
async def _qqbot_avatar(bot, uid) -> str | None:
    s = str(uid)
    if len(s) != 15 or not s.isdigit():
        return None
    try:
        d = await bot.call_api("get_additional_user_detail", user_id=int(s), size=640)
        av = (d or {}).get("avatar") or {}
        return av["data"] if av.get("type") == "url" and av.get("data") else None
    except ActionFailed:
        return None

class InfoFetcher(BaseInfoFetcher):
    async def fetch(self, bot, event):
        sess = await super().fetch(bot, event)
        if url := await _qqbot_avatar(bot, sess.user.id):
            sess.user.avatar = url
        return sess
    # query_user / query_member 同理: 取到结果后把 user.avatar 换掉
```

## AstrBot

[AstrBotDevs/AstrBot@b53999e](https://github.com/AstrBotDevs/AstrBot/tree/b53999e959cfc3b71d7b74713ddee837be843fcb):
核心不拼用户头像(`MessageMember` 没有头像字段), 插件自己拼。aiocqhttp 平台下 `event.bot` 是 `CQHttp`,
调用时带上 `self_id` 让它走事件来的那条连接
([aiocqhttp_message_event.py#L262-L271](https://github.com/AstrBotDevs/AstrBot/blob/b53999e959cfc3b71d7b74713ddee837be843fcb/astrbot/core/platform/sources/aiocqhttp/aiocqhttp_message_event.py#L262-L271)):

```python
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

async def avatar_url(event, user_id=None, size: int = 640) -> str:
    uid = str(user_id or event.get_sender_id())
    url = f"https://q.qlogo.cn/g?b=qq&nk={uid}&s={size}"
    if len(uid) == 15 and uid.isdigit() and isinstance(event, AiocqhttpMessageEvent):
        try:
            d = await event.bot.call_action("get_additional_user_detail", user_id=int(uid),
                                            size=size, self_id=event.get_self_id())
            av = (d or {}).get("avatar") or {}
            if av.get("type") == "url" and av.get("data"):
                url = av["data"]
        except Exception:
            pass
    return url
```

## Yunzai

以 [TRSS-Yunzai](https://github.com/TimeRainStarSky/Yunzai/tree/79a79c3defd9111429cd1da2acd120f22e68aa29)
(`79a79c3`)为准(Miao-Yunzai 基于 icqq, 没有 OneBot v11 适配器, 核心里无处可改)。

OneBot v11 适配器里好友与群成员的 `getAvatarUrl()` 拼 qlogo
([OneBotv11.js#L814-L882](https://github.com/TimeRainStarSky/Yunzai/blob/79a79c3defd9111429cd1da2acd120f22e68aa29/plugins/adapter/OneBotv11.js#L814-L882)),
bot 自己的头像在 [L1000-L1002](https://github.com/TimeRainStarSky/Yunzai/blob/79a79c3defd9111429cd1da2acd120f22e68aa29/plugins/adapter/OneBotv11.js#L1000-L1002)。
改法: 15 位 ID 时 `getAvatarUrl()` 返回 Promise(频道分支本来就这么做, 调用方需 `await`), 其它照旧同步返回:

```js
// 适配器里新增
qqbotAvatar(data, user_id, fallback) {
  if (!/^\d{15}$/.test(String(user_id))) return fallback
  const cache = (this.qqbotAvatarCache ??= new Map())
  const key = `${data.self_id}:${user_id}`
  if (!cache.has(key))
    cache.set(key, data.bot.sendApi("get_additional_user_detail", { user_id, size: 640 })
      .then(r => (r?.avatar?.type === "url" && r.avatar.data) || fallback)
      .catch(() => { cache.delete(key); return fallback }))
  return cache.get(key)
}

// pickFriend / pickMember 里
const qqbotAvatar = this.qqbotAvatar.bind(this, i, user_id)
...
getAvatarUrl() {
  return this.avatar || qqbotAvatar(`https://q.qlogo.cn/g?b=qq&s=0&nk=${user_id}`)
},
```

## gsuid_core

[Genshin-bots/gsuid_core@7047c25](https://github.com/Genshin-bots/gsuid_core/tree/7047c257e5f466ae575b9a8cf13eb759989d30fe)
本身调不了 OneBot 动作, 头像靠框架侧的客户端适配器填进消息的 `sender.avatar`
([image_tools.py#L271-L299](https://github.com/Genshin-bots/gsuid_core/blob/7047c257e5f466ae575b9a8cf13eb759989d30fe/gsuid_core/utils/image/image_tools.py#L271-L299));
`sender` 为空时才按 ID 拼 qlogo
([image_tools.py#L489-L503](https://github.com/Genshin-bots/gsuid_core/blob/7047c257e5f466ae575b9a8cf13eb759989d30fe/gsuid_core/utils/image/image_tools.py#L489-L503))。
所以要改的是各框架里的客户端:

### NoneBot2 客户端(nonebot-plugin-genshinuid)

[KimigaiiWuyi/GenshinUID@e57b76a](https://github.com/KimigaiiWuyi/GenshinUID/tree/e57b76a089365357b3581bf605127842d1bd31dc)(`v4-nonebot2` 分支):
`_sender_ob11` 拼 qlogo([receive.py#L25-L38](https://github.com/KimigaiiWuyi/GenshinUID/blob/e57b76a089365357b3581bf605127842d1bd31dc/GenshinUID/receive.py#L25-L38)),
在异步的 `build_message_receive` 里换掉([receive.py#L220-L254](https://github.com/KimigaiiWuyi/GenshinUID/blob/e57b76a089365357b3581bf605127842d1bd31dc/GenshinUID/receive.py#L220-L254)):

```python
    sender = dict(extract_sender(bot, event, user_id, bot_id))
    if adapter_name(bot) == "OneBot V11" and len(user_id) == 15 and user_id.isdigit():
        try:
            d = await bot.call_api("get_additional_user_detail", user_id=int(user_id), size=640)
            av = (d or {}).get("avatar") or {}
            if av.get("type") == "url" and av.get("data"):
                sender["avatar"] = av["data"]
        except Exception:
            pass
    return MessageReceive(..., sender=sender, ...)
```

### AstrBot 客户端(astrbot_plugin_gscore_adapter)

[KimigaiiWuyi/astrbot_plugin_gscore_adapter@00ec0fd](https://github.com/KimigaiiWuyi/astrbot_plugin_gscore_adapter/tree/00ec0fd262d83bed95d61e3c48b2f7bbba78e978):
aiocqhttp 分支拼 qlogo 放进 `sender`([main.py#L503-L526](https://github.com/KimigaiiWuyi/astrbot_plugin_gscore_adapter/blob/00ec0fd262d83bed95d61e3c48b2f7bbba78e978/main.py#L503-L526)):

```python
        elif pn == "aiocqhttp":
            avatar = f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
            if len(user_id) == 15 and user_id.isdigit():
                try:
                    d = await event.bot.call_action("get_additional_user_detail",
                                                    user_id=int(user_id), size=640, self_id=self_id)
                    av = (d or {}).get("avatar") or {}
                    if av.get("type") == "url" and av.get("data"):
                        avatar = av["data"]
                except Exception as e:
                    logger.warning(f"get_additional_user_detail failed: {e}")
```

### Yunzai 客户端(yunzai-gscore-adapter)

[xiowo/yunzai-gscore-adapter@1b4add7](https://github.com/xiowo/yunzai-gscore-adapter/tree/1b4add78dad4533e0465aef859e6e2a4112f0aeb):
`makeSender` 是同步的, 用 `e.member?.getAvatarUrl?.()`
([lib/message.js#L16-L30](https://github.com/xiowo/yunzai-gscore-adapter/blob/1b4add78dad4533e0465aef859e6e2a4112f0aeb/lib/message.js#L16-L30)),
在异步的 `makeReceivePacket` 里换成下面的版本([L465-L481](https://github.com/xiowo/yunzai-gscore-adapter/blob/1b4add78dad4533e0465aef859e6e2a4112f0aeb/lib/message.js#L465-L481));
不论 Yunzai 本体是否已按上面改过都适用:

```js
export async function makeSenderAsync(e) {
  const sender = makeSender(e)
  sender.avatar = await sender.avatar   // getAvatarUrl 可能返回 Promise
  if (/^\d{15}$/.test(sender.user_id) && !String(sender.avatar).includes("/qqapp/")
      && typeof e.bot?.sendApi === "function") {
    try {
      const r = await e.bot.sendApi("get_additional_user_detail", { user_id: e.user_id, size: 640 })
      if (r?.avatar?.type === "url" && r.avatar.data) sender.avatar = r.avatar.data
    } catch {}
  }
  return sender
}
// makeReceivePacket 里:  sender: await makeSenderAsync(e),
```
