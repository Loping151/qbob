# 插件开发

插件用来改写**收发的内容**: 发出去之前换个样子(转图片、转网页、改链接), 或者下发给 OneBot 后端
之前改/丢事件。内置插件(长文本转图片、合并转发转网页、长链接收短、繁体转简体)就是这么实现的, 源码在
`qqbot_onebot/plugin/built-in/`, 可以照着写。

> **理念: 复杂插件不应在适配器侧实现。适配器做好适配器即可。**
>
> 这里的插件只做「把消息在 QQ 上呈现得更好」这一层的事: 格式转换、平台限制的变通。
> 指令、对话、数据处理、调外部服务这些业务逻辑一律写在后端框架(nonebot2 等)里 ——
> 那边有完整的生态、隔离和热重载, 而适配器出了问题会拖垮所有 bot 的收发。

插件不是 OneBot 插件: 它跑在本服务进程里, 看到的是 OneBot 消息段与平台消息体, 用不着也碰不到
具体业务。

## 放在哪

```
qqbot_onebot/plugin/
├── __init__.py          插件运行时(Plugin / ConfigField / 钩子调度)
├── built-in/            内置插件, 随仓库发布
├── my_plugin.py         单文件插件
└── my_folder/           文件夹插件(可以拆多个模块, 相对导入可用)
    ├── __init__.py
    └── helper.py
```

`qqbot_onebot/plugin/` 下除了 `__init__.py` 和 `built-in/` 都已 gitignore。名字以 `_` 或 `.` 开头的
文件/文件夹不加载。

## 最小例子

```python
# qqbot_onebot/plugin/signature.py
from qqbot_onebot.plugin import ConfigField, Plugin

plugin = Plugin(
    name="signature",                 # 唯一名字, 开关与配置按它存
    title="消息落款",                  # 管理台显示
    description="在每条群消息末尾加一行落款",
    config=[ConfigField("text", "落款", default="—— 来自机器人")],
    default_enabled=False,            # 装上先不开, 去管理台打开
)


@plugin.hook("message_out")
async def add_signature(ctx, message):
    if ctx.chat_type != "group":
        return None                   # None = 不改
    return message + [{"type": "text", "data": {"text": "\n" + plugin.config["text"]}}]
```

放进去后, 管理台「设置 → 插件」点「重新加载」, 它就出现在列表里; 打开开关即生效(改开关、配置、重新加载都需要管理员账号)。

模块里必须有一个模块级变量 `plugin`, 是 `Plugin` 实例。

## 钩子点位

每个钩子是 `async def f(ctx, …)`。返回 `None` 表示不改, 交给下一个插件。

| 点位 | 参数 | 返回 | 跑法 |
|---|---|---|---|
| `message_out` | `message`: 要发的整条 OneBot 消息(段列表, 已规范化) | 新的段列表 | 依次改写 |
| `text_out` | `text`: 单个文本段的内容 | 它的 markdown 版本(纯文本版不变) | 依次改写 |
| `forward_out` | `nodes`: 群聊合并转发的节点列表 | `(纯文本, markdown)` 或一个字符串, 用它代替整份转发 | 第一个接手的算 |
| `payload_out` | `payload`: 一条平台消息体(`msg_type`/`content`/`markdown`…) | 若干条消息体的列表 | 第一个接手的算 |
| `event_in` | `event`: 要下发给 OneBot 后端的消息事件 | 新事件; `False` = 不下发 | 依次改写 |

- 「依次改写」: 前一个插件的结果传给下一个; 「第一个接手的算」: 有插件返回非 `None` 后, 后面的不再问。
- 顺序: `Plugin(priority=…)` 小的先跑(默认 100), 同优先级内置插件在前, 再按名字。
- `payload_out` 只在允许改写时调用(内置指令的回复不走这里, 比如启用码要能复制)。
- `forward_out` 群聊、私聊都会调用(内置的转网页插件只接群聊); 没有插件接手就逐条摊平发送。
- `event_in` 只改 OneBot 那一份, 透传给 QQ 协议框架的原始事件不受影响。
- 内置插件互相不依赖, 关掉任何一个都有退路(原样发送)。

### ctx

`HookContext`:

| 字段 | 说明 |
|---|---|
| `ctx.bot` | 当前 bot 实例(`BotInstance`), 常用: `appid`、`self_id`、`name`、`cfg`、`sender`、`db`、`idmap`、`media` |
| `ctx.chat_type` | `group` / `private` |
| `ctx.peer_openid` | 群或用户的 openid(`text_out`、`event_in` 里为空) |
| `ctx.plan` | 只在 `forward_out` 里有: 当前的发送计划 |

`ctx.bot` 下的对象是框架内部实现, 没有兼容承诺; 用之前看一眼源码。

## 配置项

`ConfigField(key, label, type="str", default="", help="", placeholder="", secret=False)`, `type` 可选
`str`、`int`、`bool`、`text`(多行)。管理台按这些生成表单, 值存在 `data/plugins.json`, 运行时读
`plugin.config[key]`(已按类型转换, 没配过就是默认值)。`secret=True` 的项(密钥、token)只读账号看不到值。

`Plugin(...)` 还可以带:

- `doc="https://…"`: 管理台卡片上显示「配置说明」链接;
- `priority=100`: 钩子执行顺序, 小的先跑;
- `default_enabled=True`: 装上时默认开不开。

另外可以给 `plugin.status` 赋一个函数 `(manager) -> str | (str, "error")`, 管理台卡片上显示一行当前状态
(比如内置转网页插件用它说明实际生效的网页地址; 返回 `(文字, "error")` 显示为红色)。

"没配置就不生效"这类逻辑由插件自己判断, 比如内置的合并转发转网页: 没有可用的网页地址时返回
`None`, 与开关无关。

## 出错

- 钩子里抛异常: 记日志, 当这个钩子没返回(`None`), 消息照常发。插件只该锦上添花。
- 例外: 抛带 `retcode` 属性的异常(框架的 `SendError`)会原样传给发送方, 插件拿到对应的
  OneBot 失败响应。用于"宁可让插件重试也别发残缺内容"的情况(内置转发网页在图片上传失败时这么做)。
- 加载失败(语法错误、没有 `plugin` 变量、与已加载的插件重名 —— 内置插件先加载, 自己的插件不能与它们同名):
  该插件不加载, 「设置 → 插件」页顶部显示原因, 其它插件不受影响。
- 钩子返回值形状不对(比如 `text_out` 返回了非字符串): 记日志, 当它没返回。

## 重新加载

「设置 → 插件」的「重新加载」会丢掉所有插件模块重新导入(内置的也一样), 开关与配置保留。不监听文件变化,
改完手动点一下。已经在发送中的消息用的是旧代码。
