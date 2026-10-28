# 接口文档

本服务对外有三类接口:

1. **OneBot v11**: 反向 WebSocket(主)与 HTTP API, 给 nonebot2 等 OneBot 框架;
2. **透传(QQ 官方协议)**: 给直接对接 QQ 官方 API 的框架;
3. **管理 REST API**: 管理台用, 也可以脚本调用。

动作、消息段、事件的逐项支持情况见[支持矩阵](#支持矩阵)。

## ID

| 对象 | OneBot 里的 id | 说明 |
|---|---|---|
| bot 自己 | 15 位数字(`self_id`) | 由 AppID 映射 |
| 用户 | 15 位数字(`user_id`) | 由该 bot 看到的 openid 映射 |
| 群 | 15 位数字(`group_id`) | 由该 bot 看到的 group_openid 映射 |
| 消息 | int32(`message_id`) | 本地自增, 对应平台消息 id |

- 取值范围 `[10^14, 10^15)`, 恰好 15 位, 与真实 QQ 号不重叠。
- 平台给每个 bot 的 openid 不同, 所以**同一个人/群在不同 bot 下 id 不同**; 不同 bot 之间、用户与群之间
  的 id 保证不冲突, 反查一个 id 就能知道它属于哪个 bot。
- 同一 bot 下同一用户/群的 id 永远不变(映射存在数据库里)。
- 真实 openid 可用 `get_additional_user_detail` / `get_additional_group_detail` 取。

## OneBot 反向 WebSocket

每个 bot 可配多个端点(管理台 bot 编辑里的「OneBot v11 反向 WS 端点」), 本服务作为客户端连过去,
断线 5 秒重连。

握手头:

| 头 | 值 |
|---|---|
| `X-Self-ID` | bot 的 15 位 id |
| `X-Client-Role` | `Universal` |
| `User-Agent` | `qqbot-onebot/<版本>` |
| `Authorization` | `Bearer <access_token>`(端点配了 access_token 时) |

- 连上后立即发一个 `meta_event.lifecycle.connect`, 之后每 30 秒一个心跳 `meta_event.heartbeat`。
- 动作帧 `{"action", "params", "echo"}`, 响应原样带回 `echo`; 动作并发处理。
- 动作名的 `_async` / `_rate_limited` 后缀会被忽略(按同步执行)。
- 失败响应 `{"status": "failed", "retcode": N, "message": "..."}`, 常见 retcode:

| retcode | 含义 |
|---|---|
| 1400 | 参数/消息不合法, 或不支持的动作 |
| 1403 | 目标群/用户未启用, 主动消息被拦 |
| 1404 | 目标不存在、平台无此能力(「无法实现」), 或平台返回的其它 4xx 错误 |
| 1500 | 平台或本服务内部错误, 可重试 |
| 1502 | 频控或平台暂时故障, 稍后重试 |
| 1503 | 该群未开主动消息/全量消息、被禁言、被动回复窗口已过等 |

### 事件

标准 OneBot v11 事件, 另有:

- 群消息事件总是带一个 `at` bot 的段在前(nonebot 据此判断 `to_me`); 引用消息时 `reply` 段在最前。
- bot 自己发的消息也会作为普通 `message` 事件下发一份(`user_id` = `self_id`), 每 bot 可关。
- 按钮点击: `notice_type = "qqbot_interaction"`, 字段 `interaction_id`、`data`(平台原样)、
  `chat_type`、`scene`、`group_id`、`user_id`。本服务已替你回执平台。

## OneBot HTTP API

监听 `http_api_host:http_api_port`(默认 `127.0.0.1:17820`)。

```
POST /{action}            body 为 params, 或 {"params": {...}}
POST /{bot}/{action}      bot = AppID 或 15 位 self_id
GET  同上                 params 走查询串
```

- 鉴权: `Authorization: Bearer <http_api_token>`, 或 `X-Token` 头, 或 `?access_token=`。
  `http_api_token` 在 `data/config.json`, 首次启动自动生成。
- 不指定 bot 时: 只有一个 bot 就用它, 否则用默认 bot(选项 `default_bot`), 都没有则 404。
- 返回体与 OneBot 响应一致。

```bash
curl -s -H "Authorization: Bearer $TOKEN" -d '{"group_id": 100000000000002, "message": "hi"}' \
  http://127.0.0.1:17820/send_group_msg
```

## 扩展动作

### `get_additional_user_detail`

参数 `user_id`、`size`(0 原图 / ≤100 小图 / 其它 640, 默认 640)。

```json
{
  "user_id": 100000000000003,
  "platform": "qqbot",
  "nickname": "",
  "avatar": {"type": "url", "data": "https://q.qlogo.cn/qqapp/<appid>/<openid>/640", "size": 640},
  "openid": "<真实 openid>",
  "union_openid": "",
  "appid": "<AppID>",
  "kind": "user"
}
```

`kind` 为 `user` 或 `bot`(查 bot 自己时)。`union_openid` 平台目前下发为空。

### `get_additional_group_detail`

参数 `group_id`, 返回 `group_id`、`platform`、`group_name`、`member_count`、`openid`、`appid`。

## 支持矩阵

平台能力有限, 能做的直接做, 做不到但能用消息顶上的**降级**实现, 做不到的标**无法实现**:
调用时静默(不发任何消息), 返回 `retcode=1404`。纯装饰性的动作(表情回应等)即使降级失败也返回成功,
免得插件因为一个回执中断。

### 动作

| 动作 | 支持 | 说明 |
|---|---|---|
| `send_msg` / `send_group_msg` / `send_private_msg` | ✅ | 见下方消息段; 被动回复/主动消息自动选择 |
| `send_group_forward_msg` / `send_private_forward_msg` / `send_forward_msg` | 降级 | 群聊转为一页网页链接(插件「合并转发转网页」); 私聊、插件关闭、没有可用网页地址、或不含媒体的兑换码类内容时逐条发送 |
| `delete_msg` | ✅ | 撤回; 一条消息被拆成多条时整批撤。bot 非群管时仅 2 分钟内 |
| `get_msg` / `get_forward_msg` | ✅ | 读本地记录(默认保留 7 天) |
| `get_group_msg_history` / `get_friend_msg_history` | ✅ | 同上 |
| `get_login_info` / `get_status` / `get_version_info` | ✅ | |
| `get_stranger_info` | ✅ | 只有昵称, 其余字段为空 |
| `get_friend_list` | 降级 | 消息保留期内私聊过的用户 |
| `get_group_info` | ✅ | 官方接口: 群名、人数 |
| `get_group_at_all_remain` | ✅ | 今天还能否 @全体: bot 是群管且当天没用过为 1, 否则 0 |
| `get_group_list` | 降级 | 见过且已启用的群 |
| `get_group_member_info` | 降级 | bot 自己的角色走官方接口; 其他成员来自发言缓存 |
| `get_group_member_list` | 降级 | 近 90 天发言过的成员, 最近发言的在前 |
| `set_group_ban` | ✅ | 禁言单个成员(bot 须为群管) |
| `set_group_add_request` | ✅ | 入群申请审批 |
| `set_friend_add_request` | 降级 | 平台无好友申请, 直接返回成功 |
| `upload_group_file` / `upload_private_file` | ✅ | 以文件消息发出 |
| `get_file` / `get_image` / `get_record` / `get_group_file_url` / `download_file` | ✅ | 本地文件不对外提供下载地址: `get_file` 给本地路径(可要 base64), `get_group_file_url` 返回 1404 |
| `can_send_image` / `can_send_record` | ✅ | 恒为可发(媒体走分片直传) |
| `.handle_quick_operation` | ✅ | 回复/撤回/禁言/审批 |
| `group_poke` / `friend_poke` / `send_poke` | 降级 | 发一条「@对方 戳了戳你」 |
| `set_msg_emoji_like` | 降级 | 引用那条消息回一个 emoji |
| `mark_msg_as_read` 等已读类 / `set_restart` / `clean_cache` | 降级 | 直接返回成功 |
| `get_group_honor_info` / `get_essence_msg_list` / `get_group_essence` / `_get_group_notice` / `get_group_system_msg` / 群文件列表类 / `get_online_clients` | 降级 | 返回空数据 |
| `get_additional_user_detail` / `get_additional_group_detail` | ✅ | 扩展: 头像地址与真实 openid, 见[扩展动作](#扩展动作) |
| `set_group_whole_ban` / `set_group_kick` / `set_group_admin` / `set_group_card` / `set_group_name` / `set_group_leave` / `set_group_special_title` / `set_group_portrait` / 匿名类 | 无法实现 | |
| 群公告写 / 精华消息写 / 群文件管理 | 无法实现 | |
| `send_like` / `delete_friend` / `ocr_image` / `get_mini_app_ark` / `get_cookies` / `get_csrf_token` / `get_credentials` / 资料与状态设置 / AI 声聊 | 无法实现 | |
| 其它未列出的动作 | 不支持 | 返回 `retcode=1400 unsupported action` |

### 消息段(发送)

| 段 | 支持 | 说明 |
|---|---|---|
| `text` | ✅ | 超长时按插件转图片; 长链接按插件收短 |
| `image` / `mface` | ✅ | 文字与图片尽量同一气泡 |
| `record` | ✅ | 非 silk 自动转码 |
| `video` / `file` | ✅ | |
| `reply` | ✅ | 真引用 |
| `at` | 降级 | 群聊用 markdown 真 @(需开 Markdown); 私聊或关闭时为「@昵称」文本。@ 与图片必然拆成两条 |
| `at`(全体) | 降级 | bot 是群管且开 Markdown 时真 @全体, 平台限每群每天 1 次(北京时间); 用过、非群管或被拒时为「@全体成员」文本 |
| `face` | 降级 | 对应 emoji, 没有则 `[表情名]` |
| `poke` | 降级 | 「@对方 戳了戳你」 |
| `music` / `share` / `json` / `xml` | 降级 | 标题 + 链接 |
| `node` / `forward` | 降级 | 同转发动作 |
| markdown 图片 `![](url)` | 降级 | 抽出来当原生图片发 |

### 事件(接收)

| 事件 | 支持 | 说明 |
|---|---|---|
| 群消息 / 私聊消息 | ✅ | 含 `at`、`reply`、`image`、`record`、`video`、`file`、`face`、`forward`(聊天记录)、`json`(卡片) |
| bot 自己发的消息 | ✅ | 平台不回显, 由本框架合成(每 bot 可关) |
| `group_increase` / `group_decrease` | ✅ | bot 入群/退群、成员入群/退群 |
| `friend_add` | ✅ | 用户添加 bot |
| `request.group.add` | ✅ | 入群申请 |
| 按钮点击 | ✅ | 私有 notice `qqbot_interaction` |
| 撤回、戳一戳、禁言、群名片变更等通知 | 无法实现 | 平台不推送 |

## 透传(QQ 官方协议)

给直接对接 QQ 官方 API 的框架用, 挂在 HTTP API 同一个端口的 `/qqapi/` 下。框架里 bot 的 AppID /
AppSecret 照填真实值, 只把两个地址指过来:

| 框架配置 | 值 |
|---|---|
| 获取 token 的地址 | `http://<host>:17820/qqapi/app/getAppAccessToken` |
| API 根地址 | `http://<host>:17820/qqapi/` |

nonebot-adapter-qq 对应 `QQ_AUTH_BASE` 与 `QQ_API_BASE`。

### 鉴权

`POST /qqapi/app/getAppAccessToken`, body `{"appId", "clientSecret"}`, 与平台同形。校验通过后返回
平台真实的 `access_token` 与剩余 `expires_in`。之后的请求带 `Authorization: QQBot <token>`,
本服务靠这个 token 认出是哪个 bot。

### API

`/qqapi/` 下除了下面两个网关路径, **任何路径**都原样转发给平台(GET/POST/PUT/PATCH/DELETE; 方法、查询串、body、
Content-Type 不变), 响应原样返回。平台新出的接口不需要本服务更新。

透传侧发的消息不经本服务记账(被动回复的 `msg_seq`、消息落库), 与 OneBot 后端回复同一条消息时
`msg_seq` 可能冲突, 需要框架自己处理。

### 事件: 网关

`GET /qqapi/gateway` / `GET /qqapi/gateway/bot` 返回本服务的 ws 地址(`/gateway/bot` 带
`shards: 1` 与 `session_start_limit`)。连上后是标准流程:

1. 服务端 `{"op": 10, "d": {"heartbeat_interval": 41250}}`
2. 客户端 Identify `{"op": 2, "d": {"token": "QQBot <token>", "intents": N, "shard": [0, 1]}}`
   → 服务端 `READY`(`session_id`、`user`); token 不对回 `{"op": 9, "d": false}`
3. 心跳 `{"op": 1}` → `{"op": 11}`; 超过 120 秒没有心跳断开
4. 断线后 Resume `{"op": 6, "d": {"token", "session_id", "seq"}}` 补发错过的事件(会话保留 10 分钟、
   最多 500 条)并回 `RESUMED`

事件帧与平台一致: `{"op": 0, "s": 序号, "t": 事件类型, "d": 平台原始数据, "id": 事件 id}`。
`intents` 按平台的位过滤群/单聊、群成员、互动三类事件, 传 0 则全要。

### 事件: 回调

bot 编辑里填「透传回调地址」后, 每个事件以平台 webhook 同款格式 POST 过去:

- body: `{"op": 0, "id", "t", "d"}`
- 头: `X-Bot-Appid`、`X-Signature-Timestamp`、`X-Signature-Ed25519`(Ed25519, 种子为 bot secret
  重复拼到 32 字节, 签名内容为 timestamp + body)、`User-Agent: QQBot-Callback`
- 不重试, 失败只记日志。

### 下发范围

与 OneBot 后端同一道闸:

- 消息: 只在它会下发给 OneBot 后端时透传(群已开主动消息/全量消息、已启用、未被内置指令消费);
- bot 入/退群、成员变动、入群申请、好友添加、按钮点击: 同上;
- 其它事件(开关通知、订阅状态、平台新增的未知事件): 该群/用户过名单(需要启用的群已启用、没被禁用)即透传。

## 管理 REST API

管理面(`admin_port`, 或公网面 `/admin/`)的 `/api/*`, cookie 会话鉴权(`POST /api/login`,
body `{"username", "password"}`)。各接口需要的角色见[管理台 → 角色与权限](console.md#角色与权限)。
常用接口:

| 接口 | 说明 |
|---|---|
| `GET /api/me`, `POST /api/me/password` | 当前账号; 改自己的密码 |
| `GET /api/status` | 运行状态(含更新检查结果) |
| `GET/POST /api/bots`, `PUT/DELETE /api/bots/{appid}` | bot 增删改查 |
| `POST /api/bots/{appid}/reload` | 重载单个 bot |
| `POST /api/qrconnect/start`, `GET /api/qrconnect/poll/{task_id}` | 扫码接入 |
| `GET/PUT /api/provision/config`, `POST /api/provision/run`, `POST /api/provision/retarget` | 后端预设、一键接入、BotShepherd 模式下按预设改下游 |
| `GET/POST/PUT /api/groups`, `DELETE /api/groups/{name}` | bot 分组 |
| `GET/POST /api/access`, `DELETE /api/access/{id}` | 黑白名单 |
| `GET /api/idmap` | id 映射查询 |
| `GET /api/messages`, `GET /api/messages/{mid}` | 消息记录与详情 |
| `GET /api/stats` | 统计 |
| `POST /api/chat/send` 等 `/api/chat/*` | 聊天 |
| `GET /api/storage`, `POST /api/storage/cleanup` | 存储占用与清理 |
| `POST /api/update/check`, `POST /api/update/pull` | 检查更新 / 拉取更新 |
| `GET/PUT /api/options` | 全局选项 |
| `GET /api/plugins`, `PUT /api/plugins/{name}`, `POST /api/plugins/reload` | 插件开关/配置/重新加载 |
| `GET/POST/PUT/DELETE /api/users…` | 管理台账号 |
