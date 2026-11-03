"""FastAPI 应用装配.

- 公网面(host:port): /f、QQ webhook, 及挂在 /admin 的管理台(admin_on_public).
- 管理面(admin_host:admin_port, 默认 127.0.0.1): REST API + 前端 UI.
HTTP API 面(OneBot 动作 + /qqapi 透传)见 web/http_api.py.
"""

from __future__ import annotations

import gzip
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from ..config import ServerConfig
from ..core import cards, cdnkeys, forwardpage
from ..core.manager import BotManager
from ..qq.webhook import build_router as build_webhook_router
from .auth import AuthManager
from .routes import build_router as build_api_router

logger = logging.getLogger("qqbot.app")

STATIC_DIR = Path(__file__).parent / "static"

# 请求体上限: uvicorn 默认不限 body, 未认证入口(webhook 验签前全量读、/f 诊断、
# 登录、取 token)会被大 body 吃光内存. 按 Content-Length 提前拒绝, 未认证面
# chunked 直接 411; 其余路由认证后才读 body, 兜底值盖住 chat/upload 的 200MB.
_UNAUTH_BODY_LIMITS = (
    ("/qqbot/webhook", 1024 * 1024),   # 真实事件仅几 KB
    ("/f/", 8 * 1024),                 # 诊断回传只截 4KB
    ("/api/login", 16 * 1024),
    ("/qqapi/app/getAppAccessToken", 16 * 1024),
)
_BODY_LIMIT_FALLBACK = 224 * 1024 * 1024


def _body_limit(path: str) -> tuple[int, bool]:
    """(body 上限, 是否未认证面); 先剥掉 /admin 前缀, 两个入口同口径."""
    if path.startswith("/admin"):
        path = path[len("/admin"):] or "/"
    for prefix, limit in _UNAUTH_BODY_LIMITS:
        if path.startswith(prefix):
            return limit, True
    return _BODY_LIMIT_FALLBACK, False


class NoCacheStatic(StaticFiles):
    """静态文件带 Cache-Control: no-cache(每次按 ETag 校验)."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def install_body_limit(app: FastAPI) -> None:
    """每个监听面都装入站大小限制, 不只依赖反代."""
    @app.middleware("http")
    async def limit_request_body(request: Request, call_next):
        limit, unauth = _body_limit(request.url.path)
        if unauth and request.headers.get("transfer-encoding"):
            return Response(status_code=411)
        length = request.headers.get("content-length", "")
        if length:
            try:
                parsed_length = int(length)
            except ValueError:
                return Response(status_code=400)
            if parsed_length < 0:
                return Response(status_code=400)
            if parsed_length > limit:
                return Response(status_code=413)
        return await call_next(request)


def is_cross_site_write(method: str, scheme: str, host: str,
                        origin: str = "", fetch_site: str = "") -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return False
    if fetch_site.lower() == "cross-site":
        return True
    if not origin:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme != scheme or parsed.netloc.lower() != host.lower()



def forward_only_hosts(config) -> set[str]:
    """转发网页专用域名, 只许访问 /f/.

    仅在插件开启、配了 public_base_url 且转发域名与之不同时生效, 否则它可能是唯一入口.
    """
    from ..plugin import registry  # noqa: PLC0415

    public = (urlsplit(str(getattr(config, "public_base_url", "") or "")).hostname or "").lower()
    plugin = registry.plugins.get("forward_page")
    if not public or plugin is None or not plugin.enabled:
        return set()
    base, _ = forwardpage.page_base(config)
    host = (urlsplit(base).hostname or "").lower()
    return {host} if host and host != public else set()


def build_public_app(manager: BotManager,
                     config: ServerConfig | None = None) -> FastAPI:
    app = FastAPI(title="qqbot-onebot public", docs_url=None, redoc_url=None,
                  openapi_url=None)

    install_body_limit(app)

    app.include_router(build_webhook_router(manager))
    # 转发网页与消息同期过期
    page_ttl_days = int(getattr(config, "message_ttl_days", 7))

    # 根路径 302 到主站(SNI 转发的穿透看不到 path, 只能在这层做; 302 不被浏览器记死)
    root_redirect = (config.root_redirect_url if config else "").strip()

    @app.middleware("http")
    async def forward_host_only(request: Request, call_next):
        # 每次现算, 改配置即时生效
        hosts = forward_only_hosts(config) if config else set()
        if hosts:
            # 只看 Host: X-Forwarded-Host 可被客户端伪造(部署要求反代保留原 Host)
            host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
            path = request.url.path
            if host in hosts and not (path.startswith("/f/")
                                      or (path == "/" and root_redirect)):
                return Response(status_code=404)
        return await call_next(request)

    if root_redirect:
        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse(root_redirect, status_code=302)

    # 合并转发网页: 媒体走 QQ CDN, 只有 HTML 耗流量, 故 gzip.
    FORWARD_HEADERS = {
        # 内容不可信且同源挂着 /admin: CSP 只按 sha256 放行页面自带脚本
        "Content-Security-Policy": forwardpage.CSP,
        # 链接 token 即访问凭据, 不经 referer 泄给 CDN
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
        # 页面内 rkey 至少能撑这么久
        "Cache-Control": "private, max-age=300",
    }

    def _html_response(html: str, request: Request, status: int = 200,
                       csp: str = "") -> Response:
        body = html.encode("utf-8")
        headers = dict(FORWARD_HEADERS)
        if csp:
            headers["Content-Security-Policy"] = csp
        if "gzip" in request.headers.get("accept-encoding", ""):
            body = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
            headers["Vary"] = "Accept-Encoding"
        return Response(body, status_code=status, headers=headers,
                        media_type="text/html; charset=utf-8")

    @app.get(cards.COVER_PATH + "{name}")
    async def card_cover(name: str):
        """图文卡片的封面: 平台发送时来拉一次转存. 只认 card-*.jpg, 媒体库别的文件不外露."""
        path = manager.media.resolve(name) if cards.COVER_NAME_RE.fullmatch(name) else None
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, media_type="image/jpeg",
                            headers={"X-Content-Type-Options": "nosniff",
                                     "Cache-Control": "public, max-age=3600"})

    @app.get("/f/{token}")
    async def forward_page(token: str, request: Request):
        # ?diag=1: 静态排版诊断页, 不碰记录故不校验 token
        if "diag" in request.query_params:
            return _html_response(forwardpage.DIAG_HTML, request,
                                  csp=forwardpage.CSP_DIAG)
        # 读取时就判过期, 不等定期清理
        cutoff = int(time.time()) - page_ttl_days * 86400
        row = await manager.db.fetchone(
            "SELECT nodes, ts FROM forward_pages WHERE token=? AND ts >= ?",
            (token, cutoff))
        if row is None:
            return _html_response(forwardpage.NOT_FOUND_HTML, request, 404)
        try:
            nodes = json.loads(row["nodes"])
        except ValueError:
            nodes = []
        # ?perf=1: 附帧间隔采样并自动回传
        perf = "perf" in request.query_params
        # 直链换最新 rkey
        page = forwardpage.render_page(
            nodes, created_ts=row["ts"], perf=perf, rkeys=cdnkeys.fresh())
        return _html_response(
            page,
            request, csp=forwardpage.CSP_PERF if perf else "")

    @app.post("/f/{token}")
    async def forward_diag_report(token: str, request: Request):
        """诊断页回传到日志; 只认 ?diag=report, 截 4KB 纯文本."""
        if request.query_params.get("diag") != "report":
            raise HTTPException(status_code=404)
        body = (await request.body())[:4096].decode("utf-8", "replace")
        client = request.client.host if request.client else "?"
        logger.info("forward diag from %s\n%s", client, body)
        return Response(status_code=204)

    return app


def build_admin_app(manager: BotManager, auth: AuthManager,
                    config: ServerConfig) -> FastAPI:
    app = FastAPI(title="qqbot-onebot admin", docs_url=None, redoc_url=None,
                  openapi_url=None)
    install_body_limit(app)

    @app.middleware("http")
    async def admin_security_headers(request: Request, call_next):
        if is_cross_site_write(
                request.method, request.url.scheme,
                request.headers.get("host", ""),
                request.headers.get("origin", ""),
                request.headers.get("sec-fetch-site", "")):
            return Response(status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https:; media-src 'self' blob: https:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        path = request.url.path
        if path.startswith("/api/") or path.startswith("/admin/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        if request.url.scheme == "https":
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000")
        return response
    # 前端资源/JSON 压缩率高(首屏 138KB -> 41KB); 公网面是媒体, 不加
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.state.manager = manager
    app.state.auth = auth
    app.include_router(build_api_router())
    # no-cache: 每次按 ETag 校验, 免得浏览器启发式缓存留住旧脚本
    app.mount("/", NoCacheStatic(directory=STATIC_DIR, html=True), name="static")
    return app
