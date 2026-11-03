"""插件: 改写收发内容的钩子.

- 内置插件在 plugin/built-in/, 自己的放 plugin/ 下(`xxx.py` 或 `xxx/__init__.py`,
  `_`/`.` 开头跳过); 模块级 `plugin = Plugin(...)`, `@plugin.hook("点位")` 挂函数,
  点位见 HOOK_POINTS 与 docs/plugins.md.
- 开关与配置存 data/plugins.json; 管理台「重新加载」热重载, 不监听文件.
- 钩子异常或返回值形状不对只记日志、视同未返回; 仅带 retcode 的 SendError 原样上抛。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from ..config import PROJECT_ROOT

if TYPE_CHECKING:
    from ..core.bot import BotInstance

logger = logging.getLogger("qqbot.plugins")

PLUGIN_ROOT = Path(__file__).resolve().parent
BUILTIN_DIR = PLUGIN_ROOT / "built-in"

# 点位 -> (跑法, 说明). pipe: 非 None 返回值替换后继续传; first: 首个非 None 即采用
HOOK_POINTS = {
    "message_out": ("pipe", "发送前的整条 OneBot 消息(段列表) -> 新段列表"),
    "text_out": ("pipe", "单个文本段 -> 它的 markdown 版本(纯文本版不变)"),
    "forward_out": ("first", "合并转发节点(群聊和私聊) -> (纯文本, markdown) 或 {card, plain, md}(card 为 None 发文字) 代替逐条摊平"),
    "payload_out": ("first", "单条平台消息体 -> 拆/换成若干条消息体"),
    "event_in": ("pipe", "下发给 OneBot 后端前的消息事件 -> 新事件, False 丢弃"),
}


@dataclass
class ConfigField:
    """插件配置项. type: str/int/bool/text; secret 值对只读账号隐藏."""

    key: str
    label: str
    type: str = "str"
    default: Any = ""
    help: str = ""
    placeholder: str = ""
    secret: bool = False

    def coerce(self, value: Any) -> Any:
        if self.type == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on")
            return bool(value)
        if self.type == "int":
            try:
                return int(value)
            except (TypeError, ValueError):
                return self.default
        return "" if value is None else str(value)

    def describe(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type,
                "default": "" if self.secret else self.default, "help": self.help,
                "placeholder": self.placeholder, "secret": self.secret}


@dataclass
class HookContext:
    """钩子上下文; plan 仅 forward_out 有."""

    bot: "BotInstance"
    chat_type: str = ""
    peer_openid: str = ""
    plan: Any = None


Hook = Callable[..., Awaitable[Any]]


class Plugin:
    def __init__(self, name: str, title: str = "", description: str = "",
                 config: list[ConfigField] | tuple = (),
                 default_enabled: bool = True, priority: int = 100, doc: str = ""):
        self.name = name
        self.doc = doc                  # 说明链接
        self.title = title or name
        self.description = description
        self.fields = list(config)
        self.default_enabled = default_enabled
        self.priority = priority
        self.hooks: dict[str, list[Hook]] = {}
        # 可选 (manager) -> str, 管理台卡片上的状态行
        self.status: Callable[[Any], str] | None = None
        # 由 PluginManager 填
        self.enabled = default_enabled
        self.config: dict[str, Any] = {f.key: f.default for f in self.fields}
        self.builtin = False
        self.source = ""

    def hook(self, point: str) -> Callable[[Hook], Hook]:
        if point not in HOOK_POINTS:
            raise ValueError(f"未知的钩子点位: {point}(可用: {', '.join(HOOK_POINTS)})")

        def decorate(func: Hook) -> Hook:
            self.hooks.setdefault(point, []).append(func)
            return func
        return decorate

    def describe(self, manager: Any = None, reveal: bool = True) -> dict:
        """reveal=False 时隐藏 secret 值."""
        status, level = "", "info"
        if self.status is not None:
            try:
                result = self.status(manager) or ""
                status, level = result if isinstance(result, tuple) else (result, "info")
            except Exception as exc:        # noqa: BLE001
                status, level = f"状态读取失败: {exc!r}", "error"
        hidden = {f.key for f in self.fields if f.secret and not reveal}
        return {
            "name": self.name, "title": self.title, "doc": self.doc,
            "status": str(status), "status_level": level,
            "description": self.description, "builtin": self.builtin,
            "source": self.source, "enabled": self.enabled,
            "hooks": sorted(self.hooks), "fields": [f.describe() for f in self.fields],
            "config": {k: ("" if k in hidden else v) for k, v in self.config.items()},
        }


class PluginManager:
    """进程级插件注册表; 未设 state_path 时状态只在内存."""

    def __init__(self, dirs: list[tuple[Path, bool]] | None = None):
        self.dirs = dirs if dirs is not None else [(BUILTIN_DIR, True), (PLUGIN_ROOT, False)]
        self.plugins: dict[str, Plugin] = {}
        self.errors: list[dict] = []
        self.state_path: Path | None = None
        self._state: dict = {}
        self._loaded = False
        self._generation = 0
        self._lock = threading.Lock()

    # ---------------- 加载 ----------------

    def set_state_path(self, path: str | Path) -> None:
        self.state_path = Path(path)
        self._state = self._read_state()
        for plugin in self.plugins.values():
            self._apply_state(plugin)

    def _read_state(self) -> dict:
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("plugins.json 读取失败, 按默认值: %s", exc)
            return {}
        return data.get("plugins", {}) if isinstance(data, dict) else {}

    def _save_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"plugins": self._state}, ensure_ascii=False,
                                  indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _apply_state(self, plugin: Plugin) -> None:
        saved = self._state.get(plugin.name) or {}
        plugin.enabled = bool(saved.get("enabled", plugin.default_enabled))
        values = saved.get("config") or {}
        plugin.config = {
            f.key: f.coerce(values[f.key]) if f.key in values else f.default
            for f in plugin.fields
        }

    def ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def load(self) -> None:
        """(重新)扫描插件目录, 丢弃旧模块后重新导入."""
        with self._lock:
            self._generation += 1
            for mod_name in [m for m in sys.modules if m.startswith("qqbot_plugin_")]:
                sys.modules.pop(mod_name, None)
            self.plugins = {}
            self.errors = []
            for directory, builtin in self.dirs:
                for entry in self._candidates(directory, builtin):
                    self._load_one(entry, builtin)
            self._loaded = True
        logger.info("插件已加载: %s%s", ", ".join(self.plugins) or "(无)",
                    f"; {len(self.errors)} 个失败" if self.errors else "")

    @staticmethod
    def _candidates(directory: Path, builtin: bool) -> list[Path]:
        if not directory.is_dir():
            return []
        out = []
        for entry in sorted(directory.iterdir()):
            if entry.name.startswith(("_", ".")):
                continue
            if not builtin and entry == BUILTIN_DIR:
                continue
            if entry.is_file() and entry.suffix == ".py":
                out.append(entry)
            elif entry.is_dir() and (entry / "__init__.py").is_file():
                out.append(entry)
        return out

    def _load_one(self, entry: Path, builtin: bool) -> None:
        stem = entry.stem if entry.is_file() else entry.name
        safe = "".join(c if c.isalnum() else "_" for c in stem)
        mod_name = f"qqbot_plugin_{'b' if builtin else 'u'}{self._generation}_{safe}"
        try:
            if entry.is_dir():
                spec = importlib.util.spec_from_file_location(
                    mod_name, entry / "__init__.py",
                    submodule_search_locations=[str(entry)])
            else:
                spec = importlib.util.spec_from_file_location(mod_name, entry)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[mod_name] = module
            spec.loader.exec_module(module)
            plugin = getattr(module, "plugin", None)
            if not isinstance(plugin, Plugin):
                raise TypeError("模块里没有 `plugin = Plugin(...)`")
            if plugin.name in self.plugins:
                raise ValueError(f"插件名重复: {plugin.name}")
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            logger.exception("插件加载失败: %s", entry)
            self.errors.append({"source": self._rel(entry), "error": repr(exc)})
            return
        plugin.builtin = builtin
        plugin.source = self._rel(entry)
        self._apply_state(plugin)
        self.plugins[plugin.name] = plugin

    @staticmethod
    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(PROJECT_ROOT))
        except ValueError:
            return str(path)

    # ---------------- 管理 ----------------

    def describe(self, manager: Any = None, reveal: bool = True) -> dict:
        self.ensure_loaded()
        return {"plugins": [p.describe(manager, reveal) for p in self._ordered()],
                "errors": list(self.errors)}

    def update(self, name: str, enabled: bool | None = None,
               config: dict | None = None) -> Plugin:
        self.ensure_loaded()
        plugin = self.plugins.get(name)
        if plugin is None:
            raise KeyError(name)
        saved = self._state.setdefault(name, {})
        if enabled is not None:
            plugin.enabled = bool(enabled)
            saved["enabled"] = plugin.enabled
        if config is not None:
            known = {f.key: f for f in plugin.fields}
            values = saved.setdefault("config", {})
            for key, value in config.items():
                if key in known:
                    values[key] = known[key].coerce(value)
                    plugin.config[key] = values[key]
        self._save_state()
        return plugin

    def _ordered(self) -> list[Plugin]:
        return sorted(self.plugins.values(),
                      key=lambda p: (p.priority, not p.builtin, p.name))

    def _hooks(self, point: str) -> list[tuple[Plugin, Hook]]:
        self.ensure_loaded()
        return [(p, fn) for p in self._ordered() if p.enabled
                for fn in p.hooks.get(point, ())]

    # ---------------- 调用 ----------------

    @staticmethod
    def _fatal(exc: Exception) -> bool:
        return hasattr(exc, "retcode")      # SendError

    async def _call(self, point: str, plugin: Plugin, fn: Hook, ctx: HookContext,
                    *args: Any) -> Any:
        """跑一个钩子; 出错或返回值不对视为 None, 带 retcode 的错误上抛."""
        try:
            result = await fn(ctx, *args)
        except Exception as exc:
            if self._fatal(exc):
                raise
            logger.exception("插件 %s 的 %s 钩子出错, 跳过", plugin.name, point)
            return None
        if result is None or _RESULT_OK[point](result):
            return result
        logger.warning("插件 %s 的 %s 钩子返回了 %s, 不认, 跳过",
                       plugin.name, point, type(result).__name__)
        return None

    async def pipe(self, point: str, ctx: HookContext, value: Any) -> Any:
        for plugin, fn in self._hooks(point):
            result = await self._call(point, plugin, fn, ctx, value)
            if result is not None:
                value = result
                if value is False:          # 仅 event_in 允许
                    break
        return value

    async def first(self, point: str, ctx: HookContext, *args: Any) -> Any:
        for plugin, fn in self._hooks(point):
            result = await self._call(point, plugin, fn, ctx, *args)
            if result is not None:
                return result
        return None


# 各点位接受的返回值形状
_RESULT_OK = {
    "message_out": lambda r: isinstance(r, list) and all(isinstance(x, dict) for x in r),
    "text_out": lambda r: isinstance(r, str),
    "forward_out": lambda r: isinstance(r, str) or (
        isinstance(r, tuple) and len(r) == 2 and all(isinstance(x, str) for x in r)) or (
        isinstance(r, dict) and isinstance(r.get("card"), (dict, type(None)))
        and isinstance(r.get("plain"), str) and isinstance(r.get("md"), (str, type(None)))),
    "payload_out": lambda r: isinstance(r, list) and all(isinstance(x, dict) for x in r),
    "event_in": lambda r: r is False or isinstance(r, dict),
}


registry = PluginManager()
