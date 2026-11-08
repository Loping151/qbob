"""进程级配置. bot 本身存数据库(管理台管理); 写回走原子替换, 崩溃不会截断."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from .fileutil import atomic_write_private_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "data" / "config.json"

# 新域名 api.bot.qq.com 优先, 旧域名兜底; 启动时逐个探测, 命中即固定.
DEFAULT_TOKEN_URLS = [
    "https://api.bot.qq.com/app/getAppAccessToken",
    "https://bots.qq.com/app/getAppAccessToken",
]
DEFAULT_API_BASES = [
    "https://api.bot.qq.com",
    "https://api.sgroup.qq.com",
]


@dataclass
class ServerConfig:
    # 公网面: 转发网页 /f、webhook、/media、/admin. 外网访问需前置反代(只信任 127.0.0.1 的 X-Forwarded-*)
    host: str = "127.0.0.1"
    port: int = 17800
    # 管理面: UI + REST API, 默认仅本机. admin_on_public 另挂到公网面 /admin(登录+失败锁定保护)
    admin_host: str = "127.0.0.1"
    admin_port: int = 17810
    admin_on_public: bool = True
    # HTTP API 面: napcat 风格调 OneBot 动作, 仅本机, token 鉴权
    http_api_host: str = "127.0.0.1"
    http_api_port: int = 17820
    http_api_token: str = ""  # 为空则首启自动生成
    # 公网面对外的 https 根地址, 如 "https://bot.example.com"; 收发消息与媒体不依赖它.
    # 转发网页的默认地址, 也是判断转发专用域名的依据.
    public_base_url: str = ""
    # 转发网页(/f/{token})对外根地址, 为空复用 public_base_url
    forward_base_url: str = ""
    # 公网面根路径 "/" 302 到这里; 为空则 404
    root_redirect_url: str = ""
    token_urls: list[str] = field(default_factory=lambda: list(DEFAULT_TOKEN_URLS))
    api_bases: list[str] = field(default_factory=lambda: list(DEFAULT_API_BASES))
    db_path: str = str(PROJECT_ROOT / "data" / "qqbot_onebot.db")
    media_dir: str = str(PROJECT_ROOT / "data" / "media")
    media_ttl_hours: int = 1
    message_ttl_days: int = 7
    session_secret: str = ""
    log_level: str = "INFO"
    # 日志文件(终端照常输出), 按大小轮转, 最多占 log_max_mb × (log_backups + 1); 为空不写文件
    log_file: str = str(PROJECT_ROOT / "data" / "logs" / "qqbot.log")
    log_max_mb: int = 10
    log_backups: int = 4

    # ---- 行为选项(管理台「选项」页可改, 即时生效并写回) ----
    # 全局 superuser(15 位虚拟号), 与各 bot 自己的 su 取并集. 同一人在不同 bot 下 id 不同
    superusers: list[int] = field(default_factory=list)
    # 新建 bot 的群模式: white = su 发「启用」才响应; black = 默认响应, 「禁用」的群静默
    default_group_list_mode: str = "white"
    # 群必须开「接收全部消息」才响应; 关掉后仅 @ 的群也工作. 「主动消息」始终必需
    require_recv_all: bool = True
    # 「创建bot」扫码后追问号主; 关掉则记为「未知」
    ask_owner: bool = True
    # 合并转发摊平成多条时附「可回复本消息一次性撤回」提示(撤回功能随提示开关)
    recall_hint: bool = True
    # 平台报瞬时故障(「请稍后重试」类)时框架内重试 2 次(间隔 1s/3s), 再失败才回 1502 给后端
    retry_transient: bool = True
    # HTTP API 未指定 bot 时用的 appid; 为空则取第一个连上的 bot
    default_bot: str = ""
    # 管理台 bot 分组与新 bot 的默认分组(不存在时自动补上)
    bot_groups: list[str] = field(default_factory=lambda: ["默认"])
    default_bot_group: str = "默认"
    # 出现第二个 bot 或任一 superuser 后置 true, 之后「获取信息」不再对所有人开放; 不会自动复位
    setup_done: bool = False
    # 更新检查: 比对 GitHub 版本, 有新版时管理台出「更新」(= git pull). update_check_hours=0 关闭.
    # update_mirror: GitHub 镜像前缀(如 https://ghfast.top/), 须 https, 只能在本文件改
    update_repo: str = "Loping151/qbob"
    update_mirror: str = ""
    update_check_hours: int = 6

    # 仅首启种子: bots 表为空时导入一次, 之后以数据库为准, 改此项无效
    seed_bots: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CONFIG_PATH) -> "ServerConfig":
        path = Path(path)
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for key, value in data.items():
                if hasattr(cfg, key) and not key.startswith("_"):
                    setattr(cfg, key, value)
        cfg._path = path
        changed = False
        if not cfg.session_secret:
            cfg.session_secret = secrets.token_hex(32)
            changed = True
        if not cfg.http_api_token:
            cfg.http_api_token = secrets.token_urlsafe(24)
            changed = True
        if not path.exists() or changed:
            cfg.save(path)
        return cfg

    def save(self, path: str | Path | None = None) -> None:
        path = path or getattr(self, "_path", None)
        if path is None:
            return          # 未从文件加载(测试直接构造), 不写默认路径
        path = Path(path)
        data = {k: v for k, v in self.__dict__.items() if not k.startswith("_")}
        atomic_write_private_text(
            path, json.dumps(data, ensure_ascii=False, indent=2))


# 管理台「选项」页可改的字段 -> 类型
OPTION_FIELDS: dict[str, type] = {
    "superusers": list, "default_group_list_mode": str, "require_recv_all": bool,
    "ask_owner": bool, "recall_hint": bool, "retry_transient": bool,
    "default_bot": str, "default_bot_group": str,
}
# update_mirror 故意不在内: 它决定 git pull 的源, 只许改 config.json


def option(holder, name: str, default):
    """读全局选项; holder 可为 ServerConfig/BotManager/BotInstance, 拿不到配置用默认值."""
    if isinstance(holder, ServerConfig):
        config = holder
    else:
        config = getattr(holder, "config", None)
        if not isinstance(config, ServerConfig) and not hasattr(config, name):
            config = getattr(getattr(holder, "manager", None), "config", None)
    return getattr(config, name, default) if config is not None else default
