# 部署

## 安装

推荐用 [uv](https://docs.astral.sh/uv/)：它按 `uv.lock` 在项目目录里建 `.venv`，并自动准备 Python 3.12。

```bash
git clone https://github.com/Loping151/qbob && cd qbob
mkdir -p data && cp config.example.json data/config.json
./start.sh
```

- 首次启动会创建管理员 `admin`，随机密码写在 `data/initial_admin_password.txt`。首次登录后管理台会提示改密码，
  改完这个文件自动删除。之后任何账号都可以点右上角自己的用户名改密码。
- 长文本转图片需要 `PATH` 里有 `google-chrome` 或 `chromium`（名字要对上，Ubuntu 的 `chromium-browser` 不认），
  没有就照常发文字；发非 silk 格式的语音需要 `ffmpeg`。
- 所有运行数据（配置、数据库、媒体缓存、日志）都在 `data/`，备份和迁移只管这个目录。

不用 uv 也行（需要 Python 3.12+）：`python3 -m venv .venv && .venv/bin/pip install -e .`，再用 `.venv/bin/python -m qqbot_onebot --config data/config.json` 启动。

## 配置文件

`data/config.json`。**先停服务再改**：服务运行时会把内存里的配置整份写回这个文件（保存选项、建分组、
自动设默认 bot 等），手改的内容可能被覆盖。「设置 → 选项」里的几项在管理台改，保存即生效。常用的：

| 字段 | 默认 | 说明 |
|---|---|---|
| `port` / `host` | 17800 / 127.0.0.1 | 公网面：媒体、转发网页、webhook。要从公网访问，见下文 |
| `admin_port` / `admin_host` | 17810 / 127.0.0.1 | 管理台 |
| `admin_on_public` | true | 在公网面的 `/admin/` 下也挂一份管理台；不想暴露就关掉 |
| `http_api_port` / `http_api_host` | 17820 / 127.0.0.1 | OneBot HTTP API 与 QQ 协议透传 |
| `http_api_token` | 自动生成 | HTTP API 的访问令牌 |
| `public_base_url` | 空 | 公网面对外的 https 地址，转发网页默认用它拼链接；收发消息不需要 |
| `forward_base_url` | 空 | 转发网页单独用的地址（也可在插件里填） |
| `root_redirect_url` | 空 | 有人访问公网面的根路径时跳转到哪里 |
| `message_ttl_days` | 7 | 消息记录保留天数 |
| `media_ttl_hours` | 1 | 本地媒体保留小时数 |
| `log_file` / `log_max_mb` / `log_backups` | `data/logs/qqbot.log` / 10 / 4 | 日志文件与轮转上限；`log_file` 留空则只输出到终端 |
| `update_repo` / `update_mirror` / `update_check_hours` | 本仓库 / 空 / 6 | 更新检查，见[更新](#更新)；`update_check_hours` 为 0 关闭 |

全局选项（superusers、群启用方式等）也存在这个文件里，但建议在管理台「设置 → 选项」里改。
后端预设（一组 OneBot 反向 WebSocket 地址，新 bot 按默认预设接入）存在 `data/provision.json`，
在「设置 → 后端」里改；模板是 `provision.example.json`。

## 作为系统服务运行

先建好 `.venv`（`uv sync`，或上面的 pip 方式），再写入 `/etc/systemd/system/qqbot-onebot.service`
（模板：`systemd/qqbot-onebot.service.example`）：

```ini
[Unit]
Description=QQ official bot -> OneBot v11 adapter (qbob)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=YOUR_USER
WorkingDirectory=/path/to/qbob
ExecStart=/path/to/qbob/.venv/bin/python -m qqbot_onebot --config data/config.json
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now qqbot-onebot
journalctl -u qqbot-onebot -f
```

用普通用户运行，不要用 root。用 BotShepherd 模式时，面板密码可放进 `data/bs.env`（`BS_WEB_PASSWORD=...`，
600 权限），在 unit 里加 `EnvironmentFile=-/path/to/qbob/data/bs.env`。

## 公网访问

### 什么时候需要

收发消息本身**不需要**公网：事件走 QQ 的 WebSocket 网关（本服务主动连出去），图片、语音、视频、文件
也是本服务通过分片上传把字节推给 QQ，本地媒体不对外提供下载。需要让外面访问进来的只有下面这些，用不到就不必配：

| 路径 | 谁来访问 | 什么时候需要 |
|---|---|---|
| `/f/` | 群友的浏览器 | 开了「合并转发转网页」(卡片默认经腾讯文档的外链跳转页 docs.qq.com 再到这里) |
| `/f/c/` | QQ 服务器 | 发图文卡片时平台来拉封面(私聊, 或群聊近期没收到过群图时; 其余直接用 QQ 的 CDN) |
| `/qqbot/webhook/{appid}` | QQ 服务器 | 事件接收选了 Webhook 模式（默认的 WebSocket 模式不需要） |
| `/admin/` | 你自己 | 想从外网用管理台（可用 `admin_on_public` 关掉） |

这些都由**公网面**（默认 17800 端口）提供。

### 怎么配

1. 让一个 https 域名能访问到本机的 17800 端口（下面三种任选）。
2. 把这个地址填进 `data/config.json` 的 `public_base_url`，例如 `https://bot.example.com`，重启。
3. 转发网页想用另一个域名，就同样把它指向 17800，填进插件「合并转发转网页」的网页地址。
   这个域名只放行 `/f/`，访问不到管理台和 webhook。

反代时请保留原来的 `Host`，并带上 `X-Forwarded-Proto: https`：本服务靠它们生成正确的跳转地址、识别真实访客 IP。
反代/穿透要跑在**本机**：只有来自 127.0.0.1 的转发头会被信任。

**有公网 IP：nginx 反向代理**

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    # ssl_certificate / ssl_certificate_key ...
    client_max_body_size 50m;
    location / {
        proxy_pass http://127.0.0.1:17800;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
    }
}
```

**没有公网 IP：Cloudflare Tunnel**

```bash
cloudflared tunnel --url http://127.0.0.1:17800
```

临时隧道会给一个随机的 `trycloudflare.com` 地址；长期使用请在 Cloudflare 里建命名隧道并绑定自己的域名。

**没有公网 IP：frp**（需要一台有公网 IP 的服务器跑 frps）

```toml
[[proxies]]
name = "qbob"
type = "https"
customDomains = ["bot.example.com"]
[proxies.plugin]
type = "https2http"
localAddr = "127.0.0.1:17800"
crtPath = "/path/to/fullchain.pem"
keyPath = "/path/to/privkey.pem"
requestHeaders.set.x-forwarded-proto = "https"
```

不要加 `hostHeaderRewrite`：改写 Host 会让跳转地址变成 `127.0.0.1`。

## BotShepherd 模式

在「设置 → 后端」选「经 BotShepherd」，填 BotShepherd 根目录（连接目录与面板端口由它推出）。

- 新 bot 会在 BotShepherd 里得到一条独立连接（端口从端口区间里挑），预设里的地址作为它的下游。
  之后想换下游，在 bot 的「编辑」里选预设点「应用预设」。
- 面板密码不写进任何文件：来自环境变量 `BS_WEB_PASSWORD`，或在「设置 → 后端」保存（只存内存，重启失效）。
  没有密码时只写连接文件，需要重启 BotShepherd 才生效。
- 老版本留下的 `provision.json` 没有 `mode` 字段时按 BotShepherd 模式处理。

## 安全

- 管理台默认只监听本机。通过 `/admin/` 暴露到公网时，登录有失败锁定，走 https 时 cookie 自动带 `secure`；
  仍请使用强密码，必要时再加一层访问限制。
- 不要用 root 或其它高权限用户运行。
- HTTP API 与透传默认只监听本机；改成对外监听前先想清楚谁能访问。

## 排查问题

日志在 `data/logs/qqbot.log`（systemd 下也可用 `journalctl -u qqbot-onebot`）。反馈问题时附上对应时间段的日志：

- 发送失败：哪个 bot、哪个群/用户、retcode 与原因（如 QQ 拒收、被动回复窗口已过、配额用完）。
- 收到了但没转给后端：群未启用、群不合规、配额静默、用户在黑名单、后端没连上。同一会话同一原因 10 分钟只记一次。
- 后端连接的建立与断开、媒体直传、markdown 被拒后降级等。

收发的消息本身在管理台「记录」里查。

## 更新

服务每 `update_check_hours` 小时（默认 6）从 GitHub 读一次 `main` 分支的版本号，有新版本时管理台右上角出现
「更新」按钮（管理员可用）。它做的是：

- 在项目目录执行 `git pull --ff-only`，只在 `main` 分支上、且本地没有冲突改动时才会成功；
- `uv.lock` 有变化时顺带 `uv sync`（找不到 uv 会提示你手动同步，systemd 下 `PATH` 里常常没有 `~/.local/bin`）；
- 拉取后需要重启服务生效。

访问不了 GitHub 时，在 `config.json` 的 `update_mirror` 填 https 镜像前缀（如 `https://ghfast.top/`），读版本号和拉代码
都走它。这一项决定从哪拉代码，所以只能在配置文件里改。也可以手动更新：

```bash
git pull && uv sync && sudo systemctl restart qqbot-onebot
```
