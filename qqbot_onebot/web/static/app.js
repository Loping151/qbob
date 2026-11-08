/* qqbot-onebot 管理台前端 —— 纯 vanilla JS, 无外部依赖 */

// ==================== 基础路径 ====================
// 同一 SPA 挂在 "/" 和 "/admin/" 下: API 用 BASE 前缀, 静态资源用相对路径。
// 兜底: 以无尾斜杠的目录路径(如 /admin)加载时手动补回该段。
export const BASE = (() => {
  let dir = new URL(".", location.href).pathname; // 文档所在目录, 以 "/" 结尾
  const p = location.pathname;
  if (!p.endsWith("/") && !/\.[A-Za-z0-9]+$/.test(p)) dir = p + "/";
  return dir.replace(/\/$/, "");
})();

// ==================== 状态 ====================

const ROLE_RANK = { user: 0, advanced: 1, admin: 2 };
const ROLE_NAME = { user: "只读", advanced: "高级", admin: "管理员" };

const state = {
  me: null,               // {username, role}
  status: null,           // /api/status 缓存 (页脚用)
  botFilter: { q: "", tag: "", grp: "" },
};

const rank = () => ROLE_RANK[state.me?.role] ?? -1;
const canWrite = () => rank() >= ROLE_RANK.advanced;
const isAdmin = () => rank() >= ROLE_RANK.admin;

export const $ = (sel, root = document) => root.querySelector(sel);

// ==================== 工具函数 ====================

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

export function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

export function toast(msg, type = "ok", ms = 2600) {
  const root = $("#toast-root");
  const div = document.createElement("div");
  div.className = `toast ${type}`;
  div.textContent = msg;
  root.appendChild(div);
  setTimeout(() => div.remove(), ms);
}

async function copyText(text) {
  text = String(text ?? "");
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    // 非安全上下文降级
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* ignore */ }
    ta.remove();
  }
  toast(`已复制: ${text.length > 40 ? text.slice(0, 40) + "…" : text}`);
}

function tsNorm(ts) {
  ts = Number(ts) || 0;
  if (ts > 1e12) ts = Math.floor(ts / 1000); // 毫秒 → 秒
  return ts;
}

export function relTime(ts) {
  ts = tsNorm(ts);
  if (!ts) return "-";
  const d = Math.floor(Date.now() / 1000) - ts;
  if (d < 0) return new Date(ts * 1000).toLocaleString();
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 7 * 86400) return `${Math.floor(d / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

function fullTime(ts) {
  ts = tsNorm(ts);
  return ts ? new Date(ts * 1000).toLocaleString() : "";
}

function timeCell(ts) {
  return `<span title="${esc(fullTime(ts))}">${esc(relTime(ts))}</span>`;
}

/** openid 缩略显示 */
function shortId(id) {
  id = String(id ?? "");
  if (id.length <= 14) return id;
  return id.slice(0, 8) + "…" + id.slice(-4);
}

/** 缩略 + 复制按钮 */
function copyCell(full, display = null) {
  full = String(full ?? "");
  if (!full) return '<span class="muted">-</span>';
  return `<span class="mono" title="${esc(full)}">${esc(display ?? full)}</span>` +
    `<button class="copy-btn" data-copy="${esc(full)}" title="复制">⧉</button>`;
}

// ==================== API 封装 ====================

/** "1, 2，3 4" -> [1, 2, 3, 4]: 逗号(中英文)/空白分隔的数字 ID 列表 */
function parseIdList(text) {
  return String(text || "").split(/[,，\s]+/).map(Number).filter((n) => Number.isFinite(n) && n > 0);
}

/** 标签 + 值的一行(详情、存储这类「键: 值」列表) */
function kvRow(k, html) {
  return `<div class="prov-kv"><span class="prov-k">${esc(k)}</span><span>${html}</span></div>`;
}

// 手机网络可能很差: 每个请求带 15s 上限, 读请求失败自动重试一次。
const API_TIMEOUT_MS = 15000;
const API_UPLOAD_TIMEOUT_MS = 120000;   // 传大文件慢, 单独放宽
const API_RETRY_MS = 600;                        // 重试退避
const API_RETRY_STATUS = new Set([502, 503, 504]); // 网关抖动, 值得重试

class ApiError extends Error {
  /**
   * @param aborted 主动取消 (切页/换筛选/关弹窗) —— 静默, 绝不弹 toast
   * @param timeout 超时 —— 提示语和普通网络错误分开
   */
  constructor(status, message, { aborted = false, timeout = false } = {}) {
    super(message);
    this.status = status;
    this.aborted = aborted;
    this.timeout = timeout;
  }
}

const abortedError = () => new ApiError(-1, "请求已取消", { aborted: true });
const timeoutError = () => new ApiError(408, "请求超时，请检查网络", { timeout: true });

function sleep(ms, signal) {
  return new Promise((resolve) => {
    const t = setTimeout(resolve, ms);
    signal?.addEventListener("abort", () => { clearTimeout(t); resolve(); }, { once: true });
  });
}

/** AbortSignal.timeout 的降级实现 (老 WebView 没有) */
function timeoutSignal(ms) {
  if (typeof AbortSignal !== "undefined" && AbortSignal.timeout) return AbortSignal.timeout(ms);
  const ac = new AbortController();
  setTimeout(() => ac.abort(new DOMException("timeout", "TimeoutError")), ms);
  return ac.signal;
}

/** 合并「调用方取消」与「超时」两个信号; AbortSignal.any 不可用时手动接线 */
function anySignal(signals) {
  const list = signals.filter(Boolean);
  if (list.length <= 1) return list[0];
  if (typeof AbortSignal !== "undefined" && AbortSignal.any) return AbortSignal.any(list);
  const ac = new AbortController();
  function onAbort() { ac.abort(this.reason); }
  for (const s of list) {
    if (s.aborted) { ac.abort(s.reason); return ac.signal; }
    s.addEventListener("abort", onAbort, { once: true });
  }
  return ac.signal;
}

/** 单次请求, 不含重试 */
async function apiOnce(path, { method, body, silent401, signal }) {
  const opts = { method, credentials: "same-origin", headers: {} };
  const isForm = typeof FormData !== "undefined" && body instanceof FormData;
  if (body !== undefined) {
    if (isForm) {
      opts.body = body;      // 让浏览器自己带 multipart boundary, 别设 Content-Type
    } else {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  // 传大文件比普通请求慢得多, 给它更宽的超时
  const tsig = timeoutSignal(isForm ? API_UPLOAD_TIMEOUT_MS : API_TIMEOUT_MS);
  opts.signal = anySignal([signal, tsig]);

  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    if (signal?.aborted) throw abortedError();     // 先判调用方: 切页不该报超时
    if (tsig.aborted || e?.name === "TimeoutError") throw timeoutError();
    throw new ApiError(0, "网络错误, 无法连接服务器");
  }
  let data = null;
  try { data = await res.json(); } catch { /* 无 body 或读取被中断 */ }
  if (signal?.aborted) throw abortedError();       // 读 body 途中被取消
  if (res.status === 401) {
    if (!silent401) showLogin();
    throw new ApiError(401, data?.detail || "未登录或会话已过期");
  }
  if (res.status === 403) {
    alert(data?.detail || "权限不足");
    throw new ApiError(403, data?.detail || "权限不足");
  }
  if (!res.ok) {
    throw new ApiError(res.status, data?.detail || `请求失败 (HTTP ${res.status})`);
  }
  return data;
}

/**
 * @param opts.signal   调用方取消信号 (切页 / 换筛选 / 关弹窗时中断在途请求)
 * GET 遇网络错误或 502/503/504 重试一次; 写请求不重试(后端可能已落库), 超时也不重试。
 */
export async function api(path, opts = {}) {
  const o = {
    method: String(opts.method || "GET").toUpperCase(),
    body: opts.body,
    silent401: !!opts.silent401,
    signal: opts.signal,
  };
  try {
    return await apiOnce(path, o);
  } catch (e) {
    const retryable = o.method === "GET" && !e.aborted && !e.timeout &&
      (e.status === 0 || API_RETRY_STATUS.has(e.status));
    if (!retryable) throw e;
    await sleep(API_RETRY_MS, o.signal);
    if (o.signal?.aborted) throw abortedError();
    return await apiOnce(path, o);
  }
}

/** 包一层: 出错弹 toast (401/403 已各自处理, 主动取消静默) */
async function apiTry(path, opts) {
  try {
    return await api(path, opts);
  } catch (e) {
    if (!e.aborted && e.status !== 401 && e.status !== 403) toast(e.message, "err");
    throw e;
  }
}

// ==================== 登录 / 会话 ====================

function showLogin() {
  state.me = null;
  leaveView();            // 会话没了: 停掉列表观察器与在途请求
  closeModal();
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
  const form = $("#login-form");
  form.password.value = "";
  form.username.focus();
}

function showApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  $("#whoami").innerHTML =
    `<span class="mono">${esc(state.me.username)}</span> ` +
    `<span class="badge accent">${esc(ROLE_NAME[state.me.role] || state.me.role)}</span>`;
  // 按角色隐藏导航
  document.querySelectorAll("#nav a[data-min-role]").forEach((a) => {
    const need = ROLE_RANK[a.dataset.minRole] ?? 99;
    a.classList.toggle("hidden", rank() < need);
  });
  $("#whoami").title = "修改密码";
  $("#whoami").onclick = () => passwordModal(false);
  loadFooter();
  navigate();
  if (state.me.initial_password) passwordModal(true);
}

/** 改自己的密码. initial: 还在用首启生成的初始密码时登录后自动弹出 */
function passwordModal(initial) {
  const modal = openModal(initial ? "请设置新密码" : "修改密码", `
    <form id="pw-form">
      ${initial ? '<p class="prov-msg warn">当前还在用首次启动生成的初始密码（<code>data/initial_admin_password.txt</code>）。改掉后该文件会被删除。</p>' : ""}
      <label>当前密码<input name="old" type="password" required autocomplete="current-password"></label>
      <label>新密码 <span class="muted">(至少 8 位)</span>
        <input name="new" type="password" required minlength="8" autocomplete="new-password"></label>
      <label>再输一次<input name="again" type="password" required minlength="8" autocomplete="new-password"></label>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>${initial ? "稍后" : "取消"}</button>
        <button type="submit" class="btn primary">保存</button>
      </div>
    </form>`);
  $("[data-close2]", modal).addEventListener("click", closeModal);
  $("#pw-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    if (f.new.value !== f.again.value) { toast("两次输入的新密码不一样", "err"); return; }
    try {
      await apiTry(`${BASE}/api/me/password`, { method: "POST", body: { old: f.old.value, new: f.new.value } });
      state.me.initial_password = false;
      toast("密码已修改，其它登录处已下线");
      closeModal();
    } catch { /* toast 已提示 */ }
  });
}

async function loadFooter() {
  try {
    const st = await api(`${BASE}/api/status`);
    state.status = st;
    renderFooter();
    renderUpdateButton(st.update);
  } catch { /* 忽略 */ }
}

/** 右上角「更新」: GitHub 上版本号更新时才出现; 只有管理员能点 */
function renderUpdateButton(up) {
  const btn = $("#update-btn");
  if (!btn) return;
  const show = !!up?.has_update;
  btn.classList.toggle("hidden", !show);
  if (!show) return;
  btn.textContent = `更新 v${up.latest}`;
  btn.title = isAdmin() ? `当前 v${up.current}，点击拉取新版本` : "有新版本，请管理员更新";
  btn.disabled = !isAdmin();
  btn.onclick = () => runUpdate(up);
}

async function runUpdate(up) {
  if (!confirm(`从 GitHub 拉取 v${up.latest}（当前 v${up.current}）？拉取后需重启服务生效。`)) return;
  const btn = $("#update-btn");
  btn.disabled = true;
  btn.textContent = "更新中…";
  try {
    const r = await apiTry(`${BASE}/api/update/pull`, { method: "POST" });
    toast(r.message, "ok", 6000);
    btn.textContent = "已拉取，待重启";
  } catch {
    btn.disabled = false;
    btn.textContent = `更新 v${up.latest}`;
  }
}

function renderFooter() {
  const st = state.status;
  if (!st) return;
  const base = st.public_base_url || "";
  $("#footer").innerHTML =
    `<span>qqbot-onebot <b>v${esc(st.version || "?")}</b></span>` +
    `<span>公网地址: <span class="mono">${esc(base || "(未配置)")}</span></span>` +
    `<span>QQ 开放平台回调地址填: <span class="mono">${esc(base)}/qqbot/webhook/{appid}</span></span>`;
}

// ==================== 路由 ====================

const ROUTES = {
  bots: renderBots,
  chat: renderChat,
  access: renderAccess,
  idmap: renderIdmap,
  messages: renderMessages,
  stats: renderStats,
  provision: renderProvision,
  plugins: renderPlugins,
  options: renderOptions,
  storage: renderStorage,
  users: renderUsers,
  help: renderHelp,
};
const ROUTE_MIN_ROLE = { chat: "advanced", messages: "advanced",
                         provision: "advanced", storage: "advanced", users: "admin" };

// 顶栏的一个标签可以装几个子页: 流水类(消息/ID)一组, 配置类一组
const TAB_GROUPS = {
  records: { tabs: [["messages", "消息"], ["idmap", "ID"]] },
  settings: { tabs: [["options", "选项"], ["plugins", "插件"], ["provision", "后端"],
                     ["storage", "存储"], ["users", "账号"]] },
};
const GROUP_OF = {};
for (const [group, { tabs }] of Object.entries(TAB_GROUPS)) {
  for (const [route] of tabs) GROUP_OF[route] = group;
}
const allowed = (route) => !ROUTE_MIN_ROLE[route] || rank() >= ROLE_RANK[ROUTE_MIN_ROLE[route]];

function currentRoute() {
  const m = location.hash.match(/^#\/(\w+)/);
  const name = m ? m[1] : "";
  if (TAB_GROUPS[name]) {
    // 点的是组: 进这组里第一个有权限看的子页
    return TAB_GROUPS[name].tabs.map(([r]) => r).find(allowed) || "bots";
  }
  return ROUTES[name] ? name : "bots";
}

function subtabsHTML(route) {
  const group = GROUP_OF[route];
  if (!group) return "";
  return `<nav class="subtabs">${TAB_GROUPS[group].tabs.filter(([r]) => allowed(r))
    .map(([r, label]) => `<a href="#/${r}"${r === route ? ' class="active"' : ""}>${label}</a>`)
    .join("")}</nav>`;
}

// ---------- 视图生命周期 ----------
// 每个视图一个 AbortController + 清理函数; 切页时统一收尾,
// 免得在途请求回来写 DOM、定时器留在后台空转。

let viewAbort = null;
let viewCleanups = [];

/** 视图内注册清理 (关观察器 / 停定时器); navigate() 换页时自动执行 */
export function onViewLeave(fn) { viewCleanups.push(fn); }

/** 当前视图的取消信号; 传给该视图发起的所有请求 */
export function viewSignal() { return viewAbort ? viewAbort.signal : undefined; }

function leaveView() {
  if (viewAbort) { viewAbort.abort(); viewAbort = null; }
  const fns = viewCleanups;
  viewCleanups = [];
  for (const fn of fns) { try { fn(); } catch { /* ignore */ } }
}

async function navigate() {
  if (!state.me) return;
  leaveView();                       // 先收上一个视图, 再渲染新的
  viewAbort = new AbortController();
  const sig = viewAbort.signal;
  let route = currentRoute();
  if (!allowed(route)) route = "bots";
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.route === (GROUP_OF[route] || route)));
  let main = $("#main");
  const tabs = subtabsHTML(route);
  if (tabs) {
    main.innerHTML = tabs + '<div id="subview"></div>';
    main = $("#subview");
  }
  main.innerHTML = '<div class="loading">加载中…</div>';
  try {
    await ROUTES[route](main);
  } catch (e) {
    if (e.aborted || e.status === 401 || sig.aborted) return;
    main.innerHTML = `<div class="empty"><span class="big">⚠</span>加载失败: ${esc(e.message)}</div>`;
  }
}

// ==================== 弹窗 ====================

/** 弹窗关闭时要跑的清理 (定时器等); 同一时刻只有一个弹窗, 存一个就够 */
let modalCleanup = null;

/** 由弹窗内部注册: 关闭 / 被下一个弹窗顶掉时都会执行 */
function onModalClose(fn) { modalCleanup = fn; }

function runModalCleanup() {
  const fn = modalCleanup;
  modalCleanup = null;
  if (fn) { try { fn(); } catch { /* ignore */ } }
}

function openModal(title, bodyHTML, { wide = false } = {}) {
  runModalCleanup(); // 直接开新弹窗时也要清掉上一个的定时器
  const root = $("#modal-root");
  root.innerHTML =
    `<div class="modal-backdrop"><div class="modal${wide ? " wide" : ""}">` +
    `<div class="modal-head"><h3>${esc(title)}</h3>` +
    `<button type="button" class="icon-btn" data-close title="关闭">✕</button></div>` +
    `<div class="modal-body">${bodyHTML}</div></div></div>`;
  const backdrop = root.firstElementChild;
  backdrop.addEventListener("mousedown", (e) => {
    if (e.target === backdrop) closeModal();
  });
  backdrop.querySelector("[data-close]").addEventListener("click", closeModal);
  return backdrop.querySelector(".modal");
}

function closeModal() {
  $("#modal-root").innerHTML = "";
  runModalCleanup();
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ==================== 翻页列表 ====================

export const LIST_LOADING_HTML = '<div class="inf-msg"><span class="spinner"></span>加载中…</div>';

/**
 * 翻页列表: 页码 + 跳页 + 总数; 越界回末页, 令牌防串页, 翻页后滚回顶部。
 *
 * @param {HTMLElement} o.mount   容器, 内容会被完全接管
 * @param {string} o.head         <thead> 里那一整行 <th>…</th>
 * @param {(item:any)=>string} o.rowHTML  单条 <tr>
 * @param {(offset:number, limit:number, signal:AbortSignal)=>Promise<{items:any[], total:number}>} o.fetchPage
 * @param {string|(()=>string)} o.empty   零结果时的空状态 HTML
 * @returns {{reset:Function, refresh:Function, destroy:Function}}
 */
function pagedList({ mount, head, rowHTML, fetchPage, empty, pageSize = 50 }) {
  mount.innerHTML = `
    <div class="table-wrap pg-wrap hidden"><table>
      <thead><tr>${head}</tr></thead>
      <tbody></tbody>
    </table></div>
    <div class="pg-empty"></div>
    <div class="pg-status" aria-live="polite"></div>
    <div class="pager hidden">
      <button type="button" class="btn small" data-pg="first" title="首页">⇤</button>
      <button type="button" class="btn small" data-pg="prev">‹ 上一页</button>
      <span class="pg-info">第
        <input class="pg-jump" type="number" min="1" inputmode="numeric" aria-label="跳到第几页">
        / <span class="pg-pages">1</span> 页</span>
      <button type="button" class="btn small" data-pg="next">下一页 ›</button>
      <button type="button" class="btn small" data-pg="last" title="末页">⇥</button>
      <span class="pg-total muted"></span>
    </div>`;

  const wrap = mount.querySelector(".pg-wrap");
  const tbody = mount.querySelector("tbody");
  const emptyBox = mount.querySelector(".pg-empty");
  const statusBox = mount.querySelector(".pg-status");
  const pager = mount.querySelector(".pager");
  const jump = pager.querySelector(".pg-jump");
  const pagesEl = pager.querySelector(".pg-pages");
  const totalEl = pager.querySelector(".pg-total");

  let ctrl = null;   // 在途请求的 AbortController
  let token = 0;     // 单调递增; 只有最新令牌的响应准许渲染 (防串页)
  let page = 0, pages = 1, total = 0, capped = false;
  let dead = false;

  const emptyHTML = () => (typeof empty === "function" ? empty() : empty || "");
  const setStatus = (html) => { statusBox.innerHTML = html; };

  function renderPager() {
    pager.classList.toggle("hidden", !total);
    jump.value = String(page + 1);
    jump.max = String(pages);
    pagesEl.textContent = String(pages);
    totalEl.textContent = `共 ${total}${capped ? "+" : ""} 条`;   // 搜索时后端只数到上限
    for (const b of pager.querySelectorAll("[data-pg]")) {
      const k = b.dataset.pg;
      b.disabled = (k === "first" || k === "prev") ? page <= 0 : page >= pages - 1;
    }
  }

  async function load(toPage, { scroll = false } = {}) {
    if (dead) return;
    page = Math.max(0, toPage | 0);
    if (ctrl) ctrl.abort();
    const ctl = new AbortController();
    ctrl = ctl;
    const my = ++token;
    setStatus(LIST_LOADING_HTML);
    let res;
    try {
      res = await fetchPage(page * pageSize, pageSize, ctl.signal);
    } catch (e) {
      if (dead || my !== token || e?.aborted) return;
      if (e?.status === 401) { setStatus(""); return; }
      setStatus(`<div class="inf-msg err">${esc(e?.message || "加载失败")}</div>` +
        '<button type="button" class="btn block pg-retry">重试</button>');
      return;
    } finally {
      if (ctrl === ctl) ctrl = null;
    }
    if (dead || my !== token) return;         // 换筛选/切页了, 这页结果作废
    total = Math.max(0, Number(res?.total) || 0);
    capped = !!res?.total_capped;
    pages = Math.max(1, Math.ceil(total / pageSize));
    if (page > 0 && page >= pages) { load(pages - 1); return; } // 越界回末页
    const items = res?.items || [];
    tbody.innerHTML = items.map(rowHTML).join("");
    wrap.classList.toggle("hidden", !items.length);
    emptyBox.innerHTML = items.length ? "" : emptyHTML();
    setStatus("");
    renderPager();
    if (scroll) mount.scrollIntoView({ block: "start", behavior: "instant" });
  }

  statusBox.addEventListener("click", (e) => {
    if (e.target.closest(".pg-retry")) load(page);
  });
  pager.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-pg]");
    if (!btn || btn.disabled) return;
    const to = { first: 0, prev: page - 1, next: page + 1, last: pages - 1 }[btn.dataset.pg];
    load(to, { scroll: true });
  });
  const doJump = () => {
    const n = Math.min(pages, Math.max(1, Number(jump.value) || 1));
    if (n - 1 !== page) load(n - 1, { scroll: true }); else jump.value = String(page + 1);
  };
  jump.addEventListener("change", doJump);
  jump.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); doJump(); } });

  /** 筛选/搜索变了: 回第一页 */
  function reset() { if (!dead) load(0); }
  /** 原地刷新当前页(删除条目后不跳回第一页) */
  function refresh() { if (!dead) load(page); }
  /** 离开视图: 断请求, 不留任何后台活动 */
  function destroy() {
    dead = true;
    token++;
    if (ctrl) { ctrl.abort(); ctrl = null; }
  }

  load(0);
  return { reset, refresh, destroy };
}

// ==================== Bot 管理页 ====================

let botsCtx = null; // { bots, live: Map<appid, statusBot> }

async function renderBots(main) {
  const sig = viewSignal();
  const [status, bots, groupInfo] = await Promise.all([
    api(`${BASE}/api/status`, { signal: sig }),
    api(`${BASE}/api/bots`, { signal: sig }),
    api(`${BASE}/api/groups`, { signal: sig }).catch(() => ({ default: "", groups: [] })),
  ]);
  if (sig?.aborted) return;
  state.status = status;
  renderFooter();
  // 分组顺序全页统一: 默认分组在前, 其余按建立顺序, 未分组垫底; 组内按添加时间
  const order = [groupInfo.default, ...groupInfo.groups.map((g) => g.name)];
  const grpRank = (g) => { const i = order.indexOf(g); return i < 0 ? (g ? order.length : order.length + 1) : i; };
  bots.sort((a, b) => grpRank(a.grp || "") - grpRank(b.grp || "") ||
    String(a.grp || "").localeCompare(String(b.grp || ""), "zh") ||
    (a.created_at || 0) - (b.created_at || 0));
  botsCtx = { bots, live: new Map((status.bots || []).map((b) => [b.appid, b])) };

  const grps = [...new Set(bots.map((b) => b.grp || ""))];
  const f = state.botFilter;
  // 筛选中的分组被改名/删除了: 退回全部, 别停在「没有匹配的 bot」
  if (f.grp && !grps.includes(f.grp === UNGROUPED ? "" : f.grp)) f.grp = "";
  main.innerHTML = `
    <div class="page-head">
      <h2>Bot 管理</h2>
      <div class="toolbar">
        <input id="bot-q" class="search" placeholder="搜索 名称 / appid / tag" value="${esc(f.q)}">
        <select id="bot-grp">
          <option value="">全部分组</option>
          ${grps.map((g) => { const v = g || UNGROUPED;
            return `<option value="${esc(v)}"${v === f.grp ? " selected" : ""}>${esc(g || "未分组")}</option>`; }).join("")}
        </select>
        ${canWrite() ? '<button id="bot-groups" class="btn">分组</button>' : ""}
        ${canWrite() ? '<button id="bot-qr" class="btn">扫码添加</button>' : ""}
        ${canWrite() ? '<button id="bot-add" class="btn primary">＋ 添加 Bot</button>' : ""}
      </div>
    </div>
    <div id="bot-tagbar" class="tagbar"></div>
    <div id="bot-list"></div>`;

  $("#bot-q").addEventListener("input", debounce((e) => {
    state.botFilter.q = e.target.value.trim();
    botPage = 0;   // 换筛选回第一页
    renderBotList();
  }, 200));
  $("#bot-grp").addEventListener("change", (e) => {
    state.botFilter.grp = e.target.value;
    botPage = 0;
    renderBotList();
  });
  $("#bot-add")?.addEventListener("click", () => botFormModal(null));
  $("#bot-qr")?.addEventListener("click", qrConnectModal);
  $("#bot-groups")?.addEventListener("click", groupsModal);

  const list = $("#bot-list");
  list.addEventListener("click", onBotListClick);
  list.addEventListener("toggle", onGroupsToggle, true); // details 展开
  renderTagbar();
  renderBotList();
}

function renderTagbar() {
  const tags = [...new Set(botsCtx.bots.flatMap((b) => b.tags || []))].sort();
  const bar = $("#bot-tagbar");
  if (!tags.length && !state.botFilter.tag) { bar.innerHTML = ""; return; }
  const cur = state.botFilter.tag;
  bar.innerHTML =
    tags.map((t) =>
      `<span class="badge tag${t === cur ? " accent" : ""}" data-tag="${esc(t)}">${esc(t)}</span>`).join("") +
    (cur ? ` <button class="btn small ghost" data-tag-clear>清除 tag 筛选</button>` : "");
  bar.querySelectorAll("[data-tag]").forEach((el) =>
    el.addEventListener("click", () => {
      state.botFilter.tag = state.botFilter.tag === el.dataset.tag ? "" : el.dataset.tag;
      botPage = 0;   // 换筛选回第一页
      renderTagbar(); renderBotList();
    }));
  bar.querySelector("[data-tag-clear]")?.addEventListener("click", () => {
    state.botFilter.tag = "";
    botPage = 0;   // 换筛选回第一页
    renderTagbar(); renderBotList();
  });
}

// 分组筛选里「未分组」的取值: 空串已经表示「全部分组」了
const UNGROUPED = "\u0000none";

function filteredBots() {
  const { q, tag, grp } = state.botFilter;
  const ql = q.toLowerCase();
  return botsCtx.bots.filter((b) => {
    if (tag && !(b.tags || []).includes(tag)) return false;
    if (grp && (b.grp || "") !== (grp === UNGROUPED ? "" : grp)) return false;
    if (ql) {
      const hay = [b.name, b.appid, ...(b.tags || []), b.grp]
        .filter(Boolean).join(" ").toLowerCase();
      if (!hay.includes(ql)) return false;
    }
    return true;
  });
}

const BOT_PAGE = 24;          // 每页卡片数: 再多手机端渲染就吃力了
let botPage = 0;

function renderBotList() {
  const list = $("#bot-list");
  const all = filteredBots();
  const pages = Math.max(1, Math.ceil(all.length / BOT_PAGE));
  if (botPage >= pages) botPage = pages - 1;
  const bots = all.slice(botPage * BOT_PAGE, (botPage + 1) * BOT_PAGE);
  if (!all.length) {
    list.innerHTML = botsCtx.bots.length
      ? '<div class="empty"><span class="big">⌕</span>没有匹配的 bot, 试试调整筛选条件</div>'
      : `<div class="empty"><span class="big">🤖</span>还没有配置任何 bot${canWrite() ? ', 点击右上角 "添加 Bot" 开始' : ""}</div>`;
    return;
  }
  // 按分组切段(all 已排好序); 标题数字是整组跨页的数量, 接上一页的组标「续」
  const total = new Map();
  for (const b of all) total.set(b.grp || "", (total.get(b.grp || "") || 0) + 1);
  const byGrp = new Map();
  for (const b of bots) {
    const g = b.grp || "";
    if (!byGrp.has(g)) byGrp.set(g, []);
    byGrp.get(g).push(b);
  }
  const keys = [...byGrp.keys()];
  const before = all.slice(0, botPage * BOT_PAGE);
  list.innerHTML = keys.map((g) => {
    const cont = before.some((b) => (b.grp || "") === g);
    return `<section class="grp-section">
      ${total.size > 1 || g ? `<h3 class="grp-title">📁 ${esc(g || "未分组")}${cont ? '<span class="muted">（续）</span>' : ""} <span class="muted">(${total.get(g)})</span></h3>` : ""}
      <div class="bot-grid">${byGrp.get(g).map(botCardHTML).join("")}</div>
    </section>`;
  }).join("") + botPagerHTML(all.length, pages);
  list.querySelectorAll("[data-botpage]").forEach((btn) =>
    btn.addEventListener("click", () => {
      botPage = Number(btn.dataset.botpage);
      renderBotList();
      $("#bot-list").scrollIntoView({ block: "start", behavior: "instant" });
    }));
}

function botPagerHTML(total, pages) {
  if (pages <= 1) return "";
  return `<div class="pager">
    <button type="button" class="btn small" data-botpage="${botPage - 1}"
            ${botPage <= 0 ? "disabled" : ""}>‹ 上一页</button>
    <span class="pg-info">第 ${botPage + 1} / ${pages} 页</span>
    <button type="button" class="btn small" data-botpage="${botPage + 1}"
            ${botPage >= pages - 1 ? "disabled" : ""}>下一页 ›</button>
    <span class="pg-total muted">共 ${total} 个 bot</span>
  </div>`;
}

function botCardHTML(b) {
  const live = botsCtx.live.get(b.appid);
  const links = live?.links || [];
  const groups = live?.groups || [];
  const eps = b.onebot_endpoints || [];

  const epHTML = eps.length
    ? eps.map((ep) => {
        const lk = links.find((l) => l.url === ep.url);
        const on = !!lk?.connected;
        const stat = on
          ? `已连接${lk.connected_at ? " · " + relTime(lk.connected_at) : ""}`
          : "未连接";
        return `<div class="bot-ep">
          <span class="dot ${on ? "on" : "off"}" title="${esc(stat)}"></span>
          <span class="ep-url" title="${esc(ep.url)}">${esc(ep.url)}</span>
          <span class="muted" style="flex:none">${esc(stat)}</span>
        </div>`;
      }).join("")
    : links.length
      // 只读账号拿不到端点地址(后端遮掉了), 只报连上几个
      ? `<div class="muted" style="font-size:12.5px">OneBot 端点 ${links.filter((l) => l.connected).length}/${links.length} 已连接</div>`
      : '<div class="muted" style="font-size:12.5px">未配置 OneBot 端点</div>';

  const tagHTML = (b.tags || []).map((t) =>
    `<span class="badge tag" data-tag="${esc(t)}">${esc(t)}</span>`).join("");

  // 群状态按需加载: 100 bot × 1000 群若全内联, 首页要渲染十万行
  const groupCount = live?.group_count ?? groups.length;
  const groupsHTML = groupCount
    ? `<details class="groups" data-appid="${esc(b.appid)}">
        <summary>群状态 <span class="muted">(${groupCount})</span></summary>
        <div class="groups-body">${LIST_LOADING_HTML}</div>
      </details>`
    : "";

  return `<article class="bot-card" data-appid="${esc(b.appid)}">
    <div class="bot-head">
      ${b.avatar
        ? `<img class="bot-avatar zoomable" src="${esc(b.avatar)}"
             data-zoom="${esc(b.avatar)}" alt="${esc(b.name || b.appid)}"
             title="点击放大" loading="lazy"
             onerror="this.replaceWith(Object.assign(document.createElement('span'),
                      {className:'bot-avatar ph', textContent:'🤖'}))">`
        : '<span class="bot-avatar ph">🤖</span>'}
      <span class="dot ${b.running ? "on" : ""}" title="${b.running ? "运行中" : "未运行"}"></span>
      <span class="bot-name">${esc(b.name || b.appid)}</span>
      <span class="spacer"></span>
      <span class="badge">${b.event_mode === "websocket" ? "WebSocket" : "Webhook"}</span>
      ${b.enabled ? "" : '<span class="badge err">已禁用</span>'}
    </div>
    <div class="bot-meta">
      <span>appid: ${copyCell(b.appid)}</span>
      <span>虚拟号: ${b.self_id ? copyCell(b.self_id) : '<span class="muted">-</span>'}</span>
      ${b.bot_qq ? `<span>QQ: <span class="mono">${esc(b.bot_qq)}</span></span>` : ""}
    </div>
    ${tagHTML || b.grp ? `<div class="bot-badges">${b.grp ? `<span class="badge">📁 ${esc(b.grp)}</span>` : ""}${tagHTML}</div>` : ""}
    <div class="bot-eps">${epHTML}</div>
    ${b.notes ? `<div class="muted" style="font-size:12.5px">📝 ${esc(b.notes)}</div>` : ""}
    ${canWrite() ? `<div class="bot-actions">
      <a class="btn small" href="#/chat/${esc(b.appid)}">聊天</a>
      <button class="btn small" data-act="reload" data-appid="${esc(b.appid)}">重载</button>
      <button class="btn small" data-act="sync" data-appid="${esc(b.appid)}" title="跟一次平台上的名字/头像/QQ号">刷新资料</button>
      <button class="btn small" data-act="edit" data-appid="${esc(b.appid)}">编辑</button>
      ${(b.onebot_endpoints || []).length ? "" :
        `<button class="btn small" data-act="provision" data-appid="${esc(b.appid)}">配置后端</button>`}
      ${isAdmin() ? `<button class="btn small danger" data-act="delete" data-appid="${esc(b.appid)}">删除</button>` : ""}
    </div>` : ""}
    ${groupsHTML}
  </article>`;
}

/** 去掉控制符与 bidi 方向符: QQ 花名里的一个 RLO 就能把整行后面的列翻成从右往左 */
function cleanName(name) {
  return String(name || "").replace(/[\u0000-\u001f\u202a-\u202e\u2066-\u2069]/g, "");
}

function groupRowHTML(appid, g) {
  const compliant = g.compliant
    ? '<span class="ok-text" title="全量消息 + 允许主动">✓</span>'
    : '<span class="err-text" title="需: 接收全量消息 且 允许主动消息">✗</span>';
  const recv = { all: "全量" }[g.recv_msg_setting] || g.recv_msg_setting || "-";
  // 虚拟号由后端直接带来; 别逐行打 /api/idmap(100 个群就是 100 次全表扫描)
  const vid = g.virtual_id || 0;
  const name = cleanName(g.name);
  return `<tr data-openid="${esc(g.openid)}">
    <td><span class="grp-name" title="${esc(name)}">${name ? esc(name) : '<span class="muted">-</span>'}</span></td>
    <td>${copyCell(g.openid, shortId(g.openid))}</td>
    <td class="vcell mono">${vid ? copyCell(vid) : '<span class="muted">未知</span>'}</td>
    <td>${compliant}</td>
    <td>${esc(g.bot_role || "-")}</td>
    <td>${g.allow_proactive ? '<span class="ok-text">允许</span>' : '<span class="err-text">禁止</span>'}</td>
    <td>${esc(recv)}</td>
    <td class="checked-cell">${timeCell(g.checked_at)}</td>
    <td>${g.enabled
      ? '<span class="badge ok">已启用</span>'
      : '<span class="badge">未启用</span>'}</td>
    ${canWrite() ? `<td class="nowrap"><button class="btn small${g.enabled ? " danger" : " primary"}"
        data-toggle-access data-appid="${esc(appid)}" data-openid="${esc(g.openid)}"
        data-enable="${g.enabled ? "0" : "1"}"
        title="${g.enabled ? "移出白名单, bot 将不再响应该群" : "加入白名单, bot 开始响应该群"}"
      >${g.enabled ? "禁用" : "启用"}</button>
      <button class="btn small" data-refresh data-appid="${esc(appid)}" data-vid="${vid}"
        data-openid="${esc(g.openid)}" title="重新拉取群状态">刷新</button></td>` : ""}
  </tr>`;
}

async function onBotListClick(e) {
  const tagEl = e.target.closest("[data-tag]");
  if (tagEl) {
    state.botFilter.tag = tagEl.dataset.tag;
    botPage = 0;   // 换筛选回第一页
    renderTagbar(); renderBotList();
    return;
  }
  const refreshBtn = e.target.closest("[data-refresh]");
  if (refreshBtn) { await onGroupRefresh(refreshBtn); return; }

  const toggleBtn = e.target.closest("[data-toggle-access]");
  if (toggleBtn) { await onGroupToggleAccess(toggleBtn); return; }

  const actBtn = e.target.closest("[data-act]");
  if (!actBtn) return;
  const appid = actBtn.dataset.appid;
  const bot = botsCtx.bots.find((b) => b.appid === appid);
  if (actBtn.dataset.act === "edit") { botFormModal(bot); return; }
  if (actBtn.dataset.act === "sync") {
    actBtn.disabled = true;
    try {
      const r = await api(`${BASE}/api/bots/${encodeURIComponent(appid)}/sync_identity`,
                          { method: "POST" });
      toast(`已同步：${r.name || appid}`);
      if (bot) Object.assign(bot, { name: r.name, avatar: r.avatar, bot_qq: r.bot_qq });
      renderBotList();
    } catch (err) {
      if (err.status !== 401) toast(err.message, "err", 6000);
    } finally {
      actBtn.disabled = false;
    }
    return;
  }
  if (actBtn.dataset.act === "provision") { provisionRunModal(bot || { appid }); return; }
  if (actBtn.dataset.act === "reload") {
    actBtn.disabled = true;
    try {
      await apiTry(`${BASE}/api/bots/${encodeURIComponent(appid)}/reload`, { method: "POST" });
      toast(`bot ${appid} 已重载`);
      navigate();
    } catch { actBtn.disabled = false; }
    return;
  }
  if (actBtn.dataset.act === "delete") {
    deleteBotModal(bot || { appid });
  }
}

/** 删除 bot: 二次确认 + 是否连带删掉 BotShepherd 里的连接 */
function deleteBotModal(bot) {
  const appid = bot.appid;
  const endpoint = (bot.onebot_endpoints || [])[0]?.url || "";
  const modal = openModal(`删除 Bot · ${bot.name || appid}`, `
    <form id="del-form">
      <div class="prov-msg err">此操作不可恢复：将删除该 bot 的配置与运行实例。</div>
      <div class="prov-bot">
        <div><span class="prov-k">名称</span><b>${esc(bot.name || "(未命名)")}</b></div>
        <div><span class="prov-k">AppID</span><span class="mono wrap">${esc(appid)}</span></div>
        ${endpoint ? `<div><span class="prov-k">端点</span><span class="mono wrap">${esc(endpoint)}</span></div>` : ""}
      </div>
      <div id="del-bs"></div>
      <label>输入 AppID 确认
        <input name="confirm" placeholder="${esc(appid)}" autocomplete="off">
      </label>
      <div id="del-result"></div>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>取消</button>
        <button type="submit" class="btn danger">确认删除</button>
      </div>
    </form>`);
  $("[data-close2]", modal).addEventListener("click", closeModal);
  const form = $("#del-form", modal);
  const result = $("#del-result", modal);
  // 只有 BotShepherd 模式下才有连接可清; 直连模式不给这个选项
  if (endpoint) {
    api(`${BASE}/api/provision/config`).then((cfg) => {
      const box = $("#del-bs", modal);
      if (!box || cfg?.mode !== "botshepherd") return;
      box.innerHTML = `<label class="check-row">
        <input type="checkbox" name="drop_backend" checked>
        <span>同时删除 BotShepherd 里的连接<br>
          <small class="muted">会先停掉连接释放端口，再删除其配置；不勾选则连接保留，端口继续占用</small></span>
      </label>`;
    }).catch(() => {});
  }
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    if (String(fd.get("confirm") || "").trim() !== appid) {
      toast("请输入完整 AppID 以确认", "err");
      return;
    }
    const drop = fd.get("drop_backend") ? 1 : 0;
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "删除中…";
    try {
      const r = await apiTry(
        `${BASE}/api/bots/${encodeURIComponent(appid)}?drop_backend=${drop}`,
        { method: "DELETE" });
      closeModal();
      const backend = r?.backend || {};
      if (backend.attempted && !backend.ok) {
        // bot 删了但后端没清干净: 必须说清楚, 否则端口会被悄悄占着
        toast(`bot 已删除，但后端未清理: ${backend.message || "未知原因"}`, "err", 8000);
      } else if (backend.ok) {
        toast(`bot ${appid} 已删除，${backend.message}`);
      } else {
        toast(`bot ${appid} 已删除`);
      }
      navigate();
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "确认删除";
      result.innerHTML = `<div class="prov-msg err">${esc(err.message)}</div>`;
    }
  });
}

/** 群状态那一行的启用/禁用: 就地改这一行, 不重拉整张表 */
async function onGroupToggleAccess(btn) {
  const enable = btn.dataset.enable === "1";
  const openid = btn.dataset.openid;
  if (!enable && !confirm("禁用后 bot 将不再响应该群, 确定?")) return;
  btn.disabled = true;
  try {
    await api(`${BASE}/api/access/toggle`, { method: "POST", body: {
      bot_appid: btn.dataset.appid, chat_type: "group", openid, enable } });
    toast(enable ? "已启用" : "已禁用");
    const row = btn.closest("tr");
    const cell = row?.children[8];
    if (cell) {
      cell.innerHTML = enable
        ? '<span class="badge ok">已启用</span>'
        : '<span class="badge">未启用</span>';
    }
    btn.dataset.enable = enable ? "0" : "1";
    btn.textContent = enable ? "禁用" : "启用";
    btn.classList.toggle("danger", enable);
    btn.classList.toggle("primary", !enable);
  } catch (err) {
    if (err.status !== 401) toast(err.message, "err", 6000);
  } finally {
    btn.disabled = false;
  }
}

/** 群状态展开时才拉数据(按需), 再解析虚拟群号 */
async function onGroupsToggle(e) {
  const det = e.target;
  if (!det.matches?.("details.groups") || !det.open || det.dataset.resolved) return;
  det.dataset.resolved = "1";
  const appid = det.dataset.appid;
  const body = det.querySelector(".groups-body");
  if (body) {
    try {
      const res = await api(
        `${BASE}/api/bots/${encodeURIComponent(appid)}/groups?limit=100`,
        { signal: viewSignal() });
      const items = res?.items || [];
      body.innerHTML = items.length
        ? `<div class="table-wrap" style="box-shadow:none"><table>
             <thead><tr><th>群名</th><th>openid</th><th>虚拟群号</th><th>合规</th><th>角色</th><th>主动</th><th>接收</th><th>检查于</th><th>启用</th>${canWrite() ? "<th></th>" : ""}</tr></thead>
             <tbody>${items.map((g) => groupRowHTML(appid, g)).join("")}</tbody>
           </table></div>
           ${res.total > items.length ? `<div class="inf-msg cap">已显示前 ${items.length} / ${res.total} 个群</div>` : ""}`
        : '<div class="inf-msg end">暂无群状态</div>';
    } catch (err) {
      if (err?.aborted) return;
      body.innerHTML = `<div class="inf-msg err">加载失败: ${esc(err.message)}</div>`;
      det.dataset.resolved = "";     // 允许收起重开再试
      return;
    }
  }
}

async function onGroupRefresh(btn) {
  const { appid, vid, openid } = btn.dataset;
  if (!vid && !openid) return;
  btn.disabled = true;
  btn.textContent = "…";
  try {
    // 优先按 openid: 退群残留的行没有虚拟号, 只有这条路能刷到(也才能清掉)
    const res = await apiTry(
      `${BASE}/api/bots/${encodeURIComponent(appid)}/refresh_group_state`,
      { method: "POST", body: openid ? { openid } : { group_id: Number(vid) } });
    const s = res.state || {};
    const tr = btn.closest("tr");
    if (res.gone) {
      tr.remove();
      toast("bot 已不在该群, 已清理该行");
      return;
    }
    // 整行按同一个模板重画: 名字/ID/启用状态沿用这一行现有的, 状态换新
    const toggle = tr.querySelector("[data-toggle-access]");
    tr.outerHTML = groupRowHTML(appid, {
      ...s, openid, virtual_id: Number(vid) || 0,
      name: tr.querySelector(".grp-name")?.title || "",
      enabled: toggle ? toggle.dataset.enable === "0" : !!s.enabled,
    });
    toast("群状态已刷新");
    return;
  } catch { /* toast 已提示 */ }
  btn.disabled = false;
  btn.textContent = "刷新";
}

// ---------- Bot 添加/编辑表单 ----------

/**
 * @param bot     非空 = 编辑模式
 * @param prefill 新建模式下的预填值 (扫码接入拿到的 appid/secret/superusers)
 */
function botFormModal(bot, prefill = null) {
  const isEdit = !!bot;
  const b = bot || prefill || {};
  const eps = (b.onebot_endpoints || []).length ? b.onebot_endpoints : [{ url: "", access_token: "" }];

  const modal = openModal(isEdit ? `编辑 Bot · ${b.name || b.appid}` : "添加 Bot", `
    <form id="bot-form">
      ${prefill && !isEdit ? '<p class="form-note">✓ 凭据已由扫码授权获取, 确认无误后点「创建」</p>' : ""}
      <div class="form-row">
        <label>AppID *
          <input name="appid" required value="${esc(b.appid || "")}" ${isEdit ? "readonly" : ""} placeholder="QQ 开放平台 AppID">
        </label>
        <label>AppSecret ${isEdit ? "" : "*"}
          <input name="secret" ${isEdit ? "" : "required"} value="${isEdit ? "" : esc(b.secret || "")}" placeholder="${isEdit ? "留空则不修改" : "QQ 开放平台 AppSecret"}" autocomplete="off">
        </label>
      </div>
      <div class="form-row">
        <label>名称
          <input name="name" value="${esc(b.name || "")}" placeholder="展示用名称">
        </label>
        <label>Bot QQ号
          <input name="bot_qq" value="${esc(b.bot_qq || "")}" readonly
                 placeholder="${isEdit ? "连上平台后自动获取" : "创建后自动获取"}"
                 title="与 AppID 绑定, 从平台自动获取, 不能修改">
        </label>
      </div>
      <div class="form-row">
        <label>事件接收方式
          <select name="event_mode">
            <option value="websocket"${(b.event_mode || "websocket") === "websocket" ? " selected" : ""}>WebSocket (默认，免配回调)</option>
            <option value="webhook"${b.event_mode === "webhook" ? " selected" : ""}>Webhook (需在开放平台填回调地址)</option>
          </select>
        </label>
        <label>分组
          <select name="grp" data-grp-select data-current="${esc(b.grp || "")}">
            <option value="${esc(b.grp || "")}">${esc(b.grp || (isEdit ? "未分组" : "默认分组"))}</option>
          </select>
        </label>
      </div>
      <label>OneBot v11 反向 WS 端点</label>
      <div id="ep-rows">${eps.map(epRowHTML).join("")}</div>
      <div class="ep-tools">
        <button type="button" class="btn small" id="ep-add">＋ 添加端点</button>
        <span id="ep-preset" class="ep-preset"></span>
      </div>
      <p class="hint" id="ep-hint">多个 bot 可连接不同的 ws 端点, 也可以共用同一个</p>
      <label>Superusers <span class="muted">(逗号分隔 15 位虚拟号)</span>
        <input name="superusers" value="${esc((b.superusers || []).join(", "))}" placeholder="如 100000000000001, 100000000000002">
      </label>
      <label>Tags <span class="muted">(逗号分隔)</span>
        <input name="tags" value="${esc((b.tags || []).join(", "))}" placeholder="如 生产, 测试">
      </label>
      <div class="form-row">
        <label>群启用方式
          <select name="group_list_mode">
            <option value="white"${(b.group_list_mode || "white") === "white" ? " selected" : ""}>需要启用（su 发「启用」后才响应）</option>
            <option value="black"${b.group_list_mode === "black" ? " selected" : ""}>总是启用（「禁用」的群除外）</option>
          </select>
          <span class="hint">如通过其他方式控制 bot 启用状态，选择总是启用即可</span>
        </label>
        <label>私聊名单模式
          <select disabled><option>黑名单 (固定)</option></select>
          <span class="hint">私聊放开，仅黑名单用户被静默</span>
        </label>
      </div>
      <label>备注
        <textarea name="notes" rows="2">${esc(b.notes || "")}</textarea>
      </label>
      <label>透传回调地址 <span class="muted">(可选, 每行一个)</span>
        <textarea name="passthrough_webhooks" rows="2" class="mono" placeholder="http://127.0.0.1:8080/qq/webhook">${
          esc((b.passthrough_webhooks || []).map((w) => (typeof w === "string" ? w : w.url)).join("\n"))}</textarea>
        <span class="hint">给直接说 QQ 官方协议的框架用：平台原始事件签名后原样 POST 过去（与 OneBot 后端同一道启用闸）。走网关的框架不用填，见接口文档</span>
      </label>
      <label class="check-row"><input type="checkbox" name="enabled" ${b.enabled ?? true ? "checked" : ""}> 启用该 bot</label>
      <label class="check-row"><input type="checkbox" name="markdown_enabled" ${b.markdown_enabled ?? true ? "checked" : ""}> 启用 Markdown 消息</label>
      <label class="check-row"><input type="checkbox" name="report_self_message" ${b.report_self_message ?? true ? "checked" : ""}> 上报自身消息（bot 自己发的话也能触发指令；对每条消息都作答的插件慎用）</label>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>取消</button>
        <button type="submit" class="btn primary">${isEdit ? "保存" : "创建"}</button>
      </div>
    </form>`, { wide: true });

  const form = $("#bot-form", modal);
  $("[data-close2]", modal).addEventListener("click", closeModal);
  fillGroupSelect(form.grp, !isEdit);
  if (!isEdit && !b.group_list_mode) {
    // 新 bot 的启用方式跟随全局选项(失败就保持表单里的默认)
    api(`${BASE}/api/options`).then((r) => {
      const mode = r?.options?.default_group_list_mode;
      if (mode && document.body.contains(form)) form.group_list_mode.value = mode;
    }).catch(() => {});
  }
  $("#ep-add", modal).addEventListener("click", () => {
    $("#ep-rows", modal).insertAdjacentHTML("beforeend", epRowHTML({ url: "", access_token: "" }));
  });
  loadPresetPicker(modal, isEdit ? b.appid : "", eps.some((ep) => ep.url));
  $("#ep-rows", modal).addEventListener("click", (e) => {
    const del = e.target.closest("[data-ep-del]");
    if (del) del.closest(".ep-row").remove();
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const splitList = (s) => String(s || "").split(/[,，\s]+/).map((x) => x.trim()).filter(Boolean);
    const endpoints = [...form.querySelectorAll(".ep-row")].map((row) => ({
      url: row.querySelector(".ep-url-in").value.trim(),
      access_token: row.querySelector(".ep-token-in").value.trim(),
    })).filter((ep) => ep.url);
    const body = {
      name: String(fd.get("name") || "").trim(),
      enabled: form.enabled.checked,
      event_mode: fd.get("event_mode"),
      onebot_endpoints: endpoints,
      superusers: parseIdList(fd.get("superusers")),
      tags: splitList(fd.get("tags")),
      grp: String(fd.get("grp") || "").trim(),
      notes: String(fd.get("notes") || "").trim(),
      group_list_mode: fd.get("group_list_mode"),
      private_list_mode: "black",  // 私聊恒黑名单
      markdown_enabled: form.markdown_enabled.checked,
      report_self_message: form.report_self_message.checked,
      passthrough_webhooks: String(fd.get("passthrough_webhooks") || "").split(/\s+/)
        .map((x) => x.trim()).filter(Boolean),
    };
    const secret = String(fd.get("secret") || "").trim();
    let created = null;
    try {
      if (isEdit) {
        // secret 留空或掩码则不发送
        if (secret && !secret.includes("•")) body.secret = secret;
        await apiTry(`${BASE}/api/bots/${encodeURIComponent(b.appid)}`, { method: "PUT", body });
        toast("已保存并重载");
      } else {
        body.appid = String(fd.get("appid") || "").trim();
        body.secret = secret;
        await apiTry(`${BASE}/api/bots`, { method: "POST", body });
        created = { appid: body.appid, name: body.name, grp: body.grp };
        toast("bot 已创建");
      }
      closeModal();
      navigate();
      // 新建完顺手问一句要不要一键配置后端 (最常见的下一步)
      if (created) createdNextStepModal(created);
    } catch { /* toast 已提示 */ }
  });
}

/** 创建成功后的下一步引导: 立即配置后端 / 稍后 */
function createdNextStepModal(bot) {
  const modal = openModal("Bot 已创建", `
    <p class="prov-msg ok">已添加 <b>${esc(bot.name || bot.appid)}</b> <span class="mono wrap">${esc(bot.appid)}</span></p>
    <p class="hint" style="margin:8px 0 0">还需要一条 OneBot 反向 WS 后端连接才能真正跑起来, 现在配置?</p>
    <div class="modal-actions">
      <button type="button" class="btn" data-close2>稍后</button>
      <button type="button" class="btn primary" data-go>立即配置后端</button>
    </div>`);
  $("[data-close2]", modal).addEventListener("click", closeModal);
  $("[data-go]", modal).addEventListener("click", () => provisionRunModal(bot));
}

/** 编辑 Bot 里的「应用预设」: 直连模式把预设的地址填进端点行(保存才生效);
 *  BotShepherd 模式改这个 bot 那条 BS 连接的下游(立即生效), 没接过的交给卡片上的「配置后端」 */
async function loadPresetPicker(modal, appid, hasEndpoints) {
  let cfg;
  try { cfg = await api(`${BASE}/api/provision/config`); } catch { return; }
  const box = $("#ep-preset", modal);
  const hint = $("#ep-hint", modal);
  const profiles = cfg?.profiles || [];
  if (!box || !profiles.length) return;
  const viaBs = cfg.mode === "botshepherd";
  if (viaBs && !(appid && hasEndpoints)) {
    hint.textContent = "BotShepherd 模式：用卡片上的「配置后端」按预设建连接";
    return;
  }
  box.innerHTML = `<select id="ep-preset-sel">${profiles.map((p, i) =>
    `<option value="${i}"${p.default ? " selected" : ""}>${esc(p.name)}${p.default ? "（默认）" : ""}</option>`).join("")}</select>
    <button type="button" class="btn small" id="ep-preset-apply">应用预设</button>`;
  if (viaBs) {
    hint.textContent = "BotShepherd 模式：端点是 BotShepherd 的连接，「应用预设」改这条连接的下游，立即生效";
  }
  $("#ep-preset-apply", modal).addEventListener("click", async (e) => {
    const p = profiles[Number($("#ep-preset-sel", modal).value)];
    if (!p) return;
    if (!viaBs) {
      $("#ep-rows", modal).innerHTML = (p.targets || []).map((url) =>
        epRowHTML({ url, access_token: p.access_token || "" })).join("");
      toast(`已填入预设「${p.name}」，保存后生效`);
      return;
    }
    if (!confirm(`把这个 bot 的 BotShepherd 连接下游换成预设「${p.name}」？\n${(p.targets || []).join("\n")}`)) return;
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await apiTry(`${BASE}/api/provision/retarget`, {
        method: "POST", body: { appid, profile: p.name } });
      toast(res.message, res.applied ? "ok" : "warn", 5000);
    } catch { /* apiTry 已提示 */ } finally {
      btn.disabled = false;
    }
  });
}

function epRowHTML(ep) {
  return `<div class="ep-row">
    <input class="ep-url-in" value="${esc(ep.url || "")}" placeholder="ws://127.0.0.1:8080/onebot/v11/ws">
    <input class="ep-token-in" value="${esc(ep.access_token || "")}" placeholder="access_token (可选)">
    <button type="button" class="icon-btn" data-ep-del title="删除此行">✕</button>
  </div>`;
}

/** 滚到某个 bot 卡片并高亮 (列表可能还在渲染, 轮询等一会儿) */
async function focusBot(appid) {
  closeModal();
  state.botFilter = { q: appid, tag: "", grp: "" };
  botPage = 0;
  if (currentRoute() !== "bots") {
    location.hash = "#/bots";           // 触发 hashchange → navigate()
  } else {
    const input = $("#bot-q");
    if (input) input.value = appid;
    renderTagbar();
    renderBotList();
  }
  for (let i = 0; i < 25; i++) {
    const card = [...document.querySelectorAll(".bot-card")]
      .find((c) => c.dataset.appid === appid);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("flash");
      setTimeout(() => card.classList.remove("flash"), 2000);
      return;
    }
    await new Promise((r) => setTimeout(r, 100));
  }
}

// ---------- 扫码添加 bot ----------

const QR_POLL_MS = 2000;
const QR_TTL_MS = 5 * 60 * 1000;

let qrTimer = null;
/** 进行中的绑定任务, 跨弹窗存活: 弹窗关掉后转后台轮询, 扫上了照样弹出配置窗口 */
let qrPending = null; // { task, started }

/** 停轮询; 幂等 */
function qrStopPoll() {
  if (qrTimer) { clearInterval(qrTimer); qrTimer = null; }
}

/** 任务终结(完成/过期/失效/被新任务顶掉): 停轮询并丢弃任务 */
function qrFinish() {
  qrStopPoll();
  qrPending = null;
}

function qrConnectModal() {
  const modal = openModal("扫码添加 Bot", `
    <div id="qr-box" class="qr-box"><div class="loading">正在生成二维码…</div></div>
    <div class="modal-actions">
      <button type="button" class="btn" data-close2>关闭</button>
    </div>`);
  // 有意不在 onModalClose 停轮询: 关弹窗只收起界面, 任务转后台等到过期。
  // 切页时只收这个扫码弹窗, 已换成别的弹窗时不能误关
  onViewLeave(() => { if ($("#qr-box")) closeModal(); });
  $("[data-close2]", modal).addEventListener("click", closeModal);
  if (qrPending && Date.now() - qrPending.started < QR_TTL_MS) {
    // 后台还挂着没过期的任务 -> 接管显示同一张二维码, 不作废重来
    qrShowPanel(qrPending);
    if (!qrTimer) qrPollLoop(qrPending); // 保险: 定时器意外没了就补上
  } else {
    qrStart();
  }
}

async function qrStart() {
  qrFinish();
  const box = $("#qr-box");
  if (!box) return;
  box.innerHTML = '<div class="loading">正在生成二维码…</div>';

  let task;
  try {
    task = await api(`${BASE}/api/qrconnect/start`, { method: "POST" });
  } catch (e) {
    if (e.status === 401) return;
    qrRetryPanel(`创建绑定任务失败: ${e.message}`);
    return;
  }
  qrPending = { task, started: Date.now() };
  qrShowPanel(qrPending); // 期间弹窗被关掉也没关系: 面板画不上, 轮询照跑
  qrPollLoop(qrPending);
}

function qrShowPanel(pending) {
  const box = $("#qr-box");
  if (!box) return;
  const url = pending.task.connect_url || "";
  const left = Math.max(0, Math.ceil((QR_TTL_MS - (Date.now() - pending.started)) / 1000));
  box.innerHTML = `
    <div class="qr-img">${pending.task.qr_svg || ""}</div>
    <p class="qr-cap">用手机 QQ 扫码，在弹出的页面确认授权</p>
    <a class="btn primary block qr-open" href="${esc(url)}" target="_blank" rel="noopener noreferrer">在手机 QQ 中打开</a>
    <p class="hint qr-hint">正用这台手机看本页面? 二维码自己扫不了 —— 直接点上面的按钮。</p>
    <div class="qr-url"><span class="mono wrap">${esc(url)}</span>
      <button type="button" class="copy-btn" data-copy="${esc(url)}" title="复制链接">⧉</button></div>
    <p id="qr-status" class="qr-status">等待扫码授权… 剩余 ${left} 秒（关掉本窗口也会继续等）</p>
    <button type="button" class="btn block" data-qr-new>重新生成二维码</button>`;
  box.querySelector("[data-qr-new]").addEventListener("click", () => qrStart());
}

function qrPollLoop(pending) {
  qrStopPoll();
  let fails = 0;
  qrTimer = setInterval(async () => {
    if (qrPending !== pending) { qrStopPoll(); return; } // 被新任务顶掉
    const box = $("#qr-box"); // 弹窗开着才更新界面; 关了照样轮询
    const left = QR_TTL_MS - (Date.now() - pending.started);
    if (left <= 0) {
      qrFinish();
      if (box) qrRetryPanel("二维码已过期（超过 5 分钟未完成授权）");
      return;
    }
    const statusEl = box && box.querySelector("#qr-status");
    if (statusEl) {
      statusEl.textContent =
        `等待扫码授权… 剩余 ${Math.ceil(left / 1000)} 秒（关掉本窗口也会继续等）`;
    }
    let res;
    try {
      res = await api(`${BASE}/api/qrconnect/poll/${encodeURIComponent(pending.task.task_id)}`);
    } catch (e) {
      if (e.status === 401 || e.status === 403) { qrFinish(); return; }
      if (e.status === 404) {
        qrFinish();
        if (box) qrRetryPanel("任务已失效, 请重新生成");
        return;
      }
      // 网络抖动: 弹窗开着就提示重试; 后台则默默重试到过期, 不丢任务
      if (++fails >= 3 && box) { qrFinish(); qrRetryPanel(`轮询失败: ${e.message}`); }
      return;
    }
    fails = 0;
    const st = res?.status;
    if (st === "pending") return;
    qrFinish();
    if (st === "expired") { if (box) qrRetryPanel("二维码已过期"); return; }
    if (st === "exists") {
      if (box) qrExistsPanel(res);
      else toast(`扫码完成：bot ${res.appid} 已存在，未做改动`, "warn", 6000);
      return;
    }
    if (st === "ready") {
      // 弹窗可能早被关了 -> 主动弹出配置窗口(扫码者是号主, 不预填 su)
      closeModal();
      toast("已获取凭据，请确认后保存");
      botFormModal(null, {
        appid: res.appid,
        secret: res.secret,
        superusers: [],
      });
      return;
    }
    if (box) qrRetryPanel(`未知状态: ${st}`);
  }, QR_POLL_MS);
}

/** 失败/过期面板: 一个大的「重新生成」按钮 */
function qrRetryPanel(msg) {
  const box = $("#qr-box");
  if (!box || !document.body.contains(box)) return;
  box.innerHTML = `
    <div class="prov-msg err">${esc(msg)}</div>
    <button type="button" class="btn primary block" data-qr-retry>重新生成</button>`;
  box.querySelector("[data-qr-retry]").addEventListener("click", () => qrStart());
}

/** bot 已存在: 只展示, 不改任何东西 */
function qrExistsPanel(res) {
  const box = $("#qr-box");
  if (!box || !document.body.contains(box)) return;
  box.innerHTML = `
    <div class="prov-msg warn">${esc(res.message || "该 bot 已存在，未做任何改动")}</div>
    <div class="qr-exists">
      <div><span class="prov-k">名称</span><b>${esc(res.name || "(未命名)")}</b></div>
      <div><span class="prov-k">AppID</span><span class="mono wrap">${esc(res.appid || "")}</span></div>
    </div>
    <button type="button" class="btn primary block" data-qr-goto>查看这个 bot</button>
    <p class="hint">没有改动任何配置。要换 secret 请到该 bot 的「编辑」里手动填。</p>`;
  box.querySelector("[data-qr-goto]").addEventListener("click", () => focusBot(res.appid));
}

// ---------- 一键配置后端 ----------

function provisionRunModal(bot) {
  const modal = openModal(`配置后端 · ${bot.name || bot.appid}`, `
    <form id="prov-form">
      <div class="prov-bot">
        <div><span class="prov-k">名称</span><b>${esc(bot.name || "(未命名)")}</b></div>
        <div><span class="prov-k">AppID</span><span class="mono wrap">${esc(bot.appid)}</span></div>
      </div>
      <label>号主 <span id="prov-owner-req">*</span>
        <input name="description" placeholder="如：10001" autocomplete="off">
      </label>
      <p class="hint" id="prov-owner-hint">只填号主本人(名字/QQ号), 会自动写成「号主 xxxx」</p>
      <label>分组
        <select name="grp" data-current="${esc(bot.grp || "")}">
          <option value="${esc(bot.grp || "")}">${esc(bot.grp || "未分组")}</option>
        </select>
      </label>
      <label>预设
        <select name="profile" id="prov-profile"><option value="">加载中…</option></select>
      </label>
      <div id="prov-cred" class="hint">检查 BotShepherd 密码状态…</div>
      <div id="prov-result"></div>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>取消</button>
        <button type="submit" class="btn primary">开始配置</button>
      </div>
    </form>`);

  const form = $("#prov-form", modal);
  const result = $("#prov-result", modal);
  $("[data-close2]", modal).addEventListener("click", closeModal);

  // 预设下拉异步填充, 期间表单可照填; 关弹窗即中断
  let provMode = "onebot";
  const cfgCtl = new AbortController();
  onModalClose(() => cfgCtl.abort());
  fillGroupSelect(form.grp, false);
  (async () => {
    const sel = $("#prov-profile", modal);
    try {
      const cfg = await api(`${BASE}/api/provision/config`, { signal: cfgCtl.signal });
      if (!document.body.contains(sel)) return;
      provMode = cfg?.mode || "onebot";
      const profiles = cfg?.profiles || [];
      sel.innerHTML = profiles.length
        ? profiles.map((p) =>
            `<option value="${esc(p.name)}"${p.default ? " selected" : ""}>` +
            `${esc(p.name)}${p.default ? " · 默认" : ""} (${(p.targets || []).length} 个目标)</option>`).join("")
        : '<option value="">未配置预设, 请先去「设置 → 后端」新增</option>';
      const cred = $("#prov-cred", modal);
      if (provMode === "onebot") {
        // 直连: 号主只进 bot 备注, 可不填; 没有 BS 密码这回事
        $("#prov-owner-req", modal).textContent = "(可选)";
        $("#prov-owner-hint", modal).textContent = "直连模式：号主写进 bot 备注";
        if (cred) cred.innerHTML = "直连模式：预设里的端点直接写成该 bot 的 OneBot 端点";
      } else if (cred) {
        $("#prov-owner-hint", modal).textContent =
          "只填号主本人(名字/QQ号), 会自动写成「号主 xxxx」进 BotShepherd 连接备注";
        cred.innerHTML = cfg?._bs_runtime?.password_ready
          ? '✅ BotShepherd 密码已就绪，配置后<b>立即生效</b>'
          : '⚠️ 未设置 BotShepherd 密码：只会写入配置文件，需自行重启 BS 才生效。' +
            (isAdmin()
              ? '密码是全局的，去 <a href="#/provision" data-close-modal>设置 → 后端</a> 设置一次即可。'
              : '密码是全局的，请管理员在「设置 → 后端」设置一次。');
        cred.querySelector("[data-close-modal]")?.addEventListener("click", closeModal);
      }
    } catch (e) {
      if (!e.aborted && document.body.contains(sel)) {
        sel.innerHTML = `<option value="">预设加载失败: ${esc(e.message)}</option>`;
      }
    }
  })();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const description = String(fd.get("description") || "").trim();
    if (!description && provMode !== "onebot") { toast("请填写号主", "err"); return; }
    const body = {
      appid: bot.appid,
      description,
      grp: String(fd.get("grp") || "").trim(),
      profile: String(fd.get("profile") || ""),
    };

    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    btn.textContent = "配置中…";
    result.innerHTML = "";
    try {
      const r = await api(`${BASE}/api/provision/run`, { method: "POST", body });
      if (r.mode === "onebot") {
        result.innerHTML = `
          <div class="prov-msg ok">✓ ${esc(r.message || "配置完成")}</div>
          <div class="prov-kv"><span class="prov-k">OneBot 端点</span><span class="prov-targets">${
            (r.targets || []).map((t) => `<span class="mono wrap">${esc(t)}</span>`).join("")}</span></div>`;
        toast("后端已配置");
        btn.remove();
        $("[data-close2]", modal).textContent = "完成";
        navigate();
        return;
      }
      result.innerHTML = `
        <div class="prov-msg ok">✓ ${esc(r.message || "配置完成")}</div>
        <div class="prov-kv"><span class="prov-k">端口</span><span class="mono">${esc(r.port)}</span></div>
        <div class="prov-kv"><span class="prov-k">连接 ID</span><span class="mono wrap">${esc(r.connection_id)}</span></div>
        <div class="prov-kv"><span class="prov-k">本机端点</span><span class="mono wrap">${esc(r.endpoint)}</span></div>
        <div class="prov-kv"><span class="prov-k">后端目标</span><span class="prov-targets">${
          (r.targets || []).map((t) => `<span class="mono wrap">${esc(t)}</span>`).join("") ||
          '<span class="muted">(无)</span>'}</span></div>
        <div class="prov-kv"><span class="prov-k">生效</span><span>${
          r.applied ? '<span class="ok-text">已在 BotShepherd 立即生效</span>'
                    : '<span class="err-text">需重启 BS 或在其面板重启该连接</span>'}</span></div>`;
      toast("后端已配置");
      btn.remove();
      $("[data-close2]", modal).textContent = "完成";
      result.scrollIntoView({ block: "nearest" });
      navigate(); // 刷新身后的 bot 列表 (端点已改)
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "开始配置";
      if (err.status === 401 || err.status === 403) return;
      if (err.status === 409) {
        result.innerHTML =
          `<div class="prov-msg err">${esc(err.message)}</div>` +
          '<p class="hint">已配置过的 bot 只能在「编辑」里手动改端点，' +
          '以免把正在跑的连接换掉。</p>';
      } else {
        result.innerHTML = `<div class="prov-msg err">${esc(err.message)}</div>`;
      }
      result.scrollIntoView({ block: "nearest" });
    }
  });
}

// ---------- 分组 ----------

/** 分组下拉. forNew: 新 bot 默认选默认分组; 否则保持当前值(可能是「未分组」) */
async function fillGroupSelect(select, forNew) {
  if (!select) return;
  let data;
  try { data = await api(`${BASE}/api/groups`); } catch { return; }
  if (!document.body.contains(select)) return;
  const current = select.dataset.current || "";
  const chosen = forNew && !current ? data.default : current;
  const names = data.groups.map((g) => g.name);
  select.innerHTML = (current && !names.includes(current) || (!current && !forNew)
    ? `<option value="${esc(current)}">${esc(current || "未分组")}</option>` : "") +
    names.map((n) => `<option value="${esc(n)}"${n === chosen ? " selected" : ""}>` +
      `${esc(n)}${n === data.default ? "（默认）" : ""}</option>`).join("");
}

/** Bot 管理 →「分组」: 建分组、改名、删、设默认. 新 bot 都进默认分组 */
function groupsModal() {
  const modal = openModal("分组", '<div id="grp-box">加载中…</div>');
  const box = $("#grp-box", modal);
  const render = (data) => {
    const admin = isAdmin();
    box.innerHTML = `
      <p class="hint">新 bot（包括「创建bot」接入的）都进默认分组。删除分组时里面的 bot 挪进默认分组。</p>
      <div class="table-wrap"><table><thead><tr><th>分组</th><th>bot 数</th><th></th></tr></thead><tbody>
        ${data.groups.map((g) => `<tr data-g="${esc(g.name)}">
          <td>${esc(g.name)} ${g.name === data.default ? '<span class="badge accent">默认</span>' : ""}</td>
          <td class="mono">${g.count}</td>
          <td class="nowrap">${admin ? `
            <button type="button" class="btn small" data-act="rename">改名</button>
            ${g.name === data.default ? "" : `<button type="button" class="btn small" data-act="default">设为默认</button>
            <button type="button" class="btn small danger" data-act="delete">删除</button>`}` : ""}</td>
        </tr>`).join("")}
        ${data.ungrouped ? `<tr><td class="muted">未分组</td><td class="mono">${data.ungrouped}</td><td></td></tr>` : ""}
      </tbody></table></div>
      <form id="grp-new" class="prov-cred-row">
        <label>新建分组<input name="name" maxlength="32" autocomplete="off" placeholder="分组名"></label>
        <button type="submit" class="btn">新建</button>
      </form>
      ${admin ? "" : '<p class="hint">改名、删除、设默认需要管理员账号</p>'}`;
    $("#grp-new", box).addEventListener("submit", async (e) => {
      e.preventDefault();
      const name = e.target.name.value.trim();
      if (!name) return;
      try { render(await apiTry(`${BASE}/api/groups`, { method: "POST", body: { name } })); toast("已新建"); navigate(); }
      catch { /* toast 已提示 */ }
    });
    box.querySelectorAll("[data-act]").forEach((btn) => btn.addEventListener("click", async () => {
      const name = btn.closest("tr").dataset.g;
      const act = btn.dataset.act;
      let req;
      if (act === "rename") {
        const next = prompt(`「${name}」改名为`, name);
        if (!next || next.trim() === name) return;
        req = apiTry(`${BASE}/api/groups`, { method: "PUT", body: { old: name, new: next.trim() } });
      } else if (act === "default") {
        req = apiTry(`${BASE}/api/groups`, { method: "PUT", body: { name, default: true } });
      } else {
        if (!confirm(`删除分组「${name}」？里面的 bot 会挪进默认分组`)) return;
        req = apiTry(`${BASE}/api/groups/${encodeURIComponent(name)}`, { method: "DELETE" });
      }
      try { render(await req); toast("已保存"); navigate(); } catch { /* toast 已提示 */ }
    }));
  };
  api(`${BASE}/api/groups`).then(render).catch((e) => { box.textContent = `加载失败: ${e.message}`; });
}

const ACCESS_PAGE = 100;

async function renderAccess(main, keepBot = "") {
  const sig = viewSignal();
  const bots = await api(`${BASE}/api/bots`, { signal: sig });
  if (sig?.aborted) return;
  const selBot = keepBot;
  main.innerHTML = `
    <div class="page-head">
      <h2>黑白名单</h2>
      <div class="toolbar">
        <input id="acc-q" class="search" placeholder="搜索 openid / 虚拟号 / 备注 / 添加者"
               autocomplete="off" autocapitalize="off" spellcheck="false">
        <select id="acc-bot">
          <option value="">全部 bot</option>
          ${bots.map((b) => `<option value="${esc(b.appid)}"${b.appid === selBot ? " selected" : ""}>${esc(b.name || b.appid)} (${esc(b.appid)})</option>`).join("")}
        </select>
        ${canWrite() ? '<button id="acc-add" class="btn primary">＋ 添加条目</button>' : ""}
      </div>
    </div>
    <div id="acc-list"></div>`;

  const box = $("#acc-list");
  const list = pagedList({
    mount: box,
    pageSize: ACCESS_PAGE,
    head: `<th>Bot</th><th>类型</th><th>名单</th><th>虚拟号</th><th>openid</th>` +
          `<th>备注</th><th>添加者</th><th>时间</th>${canWrite() ? "<th></th>" : ""}`,
    rowHTML: accessRowHTML,
    empty: () => {
      const q = $("#acc-q")?.value.trim();
      return `<div class="empty"><span class="big">📋</span>` +
        `${q ? "没有匹配的名单条目" : "暂无名单条目"}<br>` +
        `白名单模式下只有名单内的群/用户可用; 黑名单模式下名单内的会被拒绝</div>`;
    },
    fetchPage: async (offset, limit, signal) => {
      const p = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      const appid = $("#acc-bot")?.value || "";
      if (appid) p.set("bot_appid", appid);
      const q = $("#acc-q")?.value.trim() || "";
      if (q) p.set("q", q);
      return api(`${BASE}/api/access?${p}`, { signal });
    },
  });
  onViewLeave(list.destroy);

  // 删除按钮走事件委托, 不逐行绑; 删完原地刷新当前页, 不跳回第一页
  box.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-del]");
    if (!btn) return;
    if (!confirm("确定删除该名单条目?")) return;
    btn.disabled = true;
    try {
      await apiTry(`${BASE}/api/access/${encodeURIComponent(btn.dataset.del)}`, { method: "DELETE" });
      toast("已删除");
      list.refresh();
    } catch { btn.disabled = false; }
  });

  $("#acc-q").addEventListener("input", debounce(list.reset, 350));
  $("#acc-bot").addEventListener("change", list.reset);
  $("#acc-add")?.addEventListener("click", () =>
    accessFormModal(bots, $("#acc-bot").value, list.reset));
}

function accessRowHTML(r) {
  return `<tr>
    <td class="mono">${esc(r.bot_appid)}</td>
    <td><span class="badge">${r.chat_type === "group" ? "群" : "私聊"}</span></td>
    <td><span class="badge ${r.list_type === "white" ? "ok" : "err"}">${r.list_type === "white" ? "白" : "黑"}</span></td>
    <td>${r.virtual_id ? copyCell(r.virtual_id) : '<span class="muted">-</span>'}</td>
    <td>${copyCell(r.openid, shortId(r.openid))}</td>
    <td>${esc(r.note || "")}</td>
    <td class="muted">${esc(r.added_by || "")}</td>
    <td>${timeCell(r.added_at)}</td>
    ${canWrite() ? `<td><button class="btn small danger" data-del="${esc(r.id)}">删除</button></td>` : ""}
  </tr>`;
}

function accessFormModal(bots, defaultBot, onDone) {
  const modal = openModal("添加名单条目", `
    <form id="acc-form">
      <label>Bot *
        <select name="bot_appid" required>
          ${bots.map((b) => `<option value="${esc(b.appid)}"${b.appid === defaultBot ? " selected" : ""}>${esc(b.name || b.appid)} (${esc(b.appid)})</option>`).join("")}
        </select>
      </label>
      <div class="form-row">
        <label>会话类型
          <select name="chat_type">
            <option value="group">群聊</option>
            <option value="private">私聊</option>
          </select>
        </label>
        <label>名单类型
          <select name="list_type">
            <option value="white">白名单</option>
            <option value="black">黑名单</option>
          </select>
        </label>
      </div>
      <label>15 位虚拟号
        <input name="virtual_id" placeholder="如 100000000000001" inputmode="numeric">
      </label>
      <label>openid
        <input name="openid" placeholder="或填 openid">
      </label>
      <p class="hint">虚拟号与 openid 二选一填写即可</p>
      <label>备注
        <input name="note" placeholder="可选">
      </label>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>取消</button>
        <button type="submit" class="btn primary">添加</button>
      </div>
    </form>`);

  $("[data-close2]", modal).addEventListener("click", closeModal);
  $("#acc-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const virtual = String(fd.get("virtual_id") || "").trim();
    const openid = String(fd.get("openid") || "").trim();
    if (!virtual && !openid) { toast("请填写虚拟号或 openid", "err"); return; }
    const body = {
      bot_appid: fd.get("bot_appid"),
      chat_type: fd.get("chat_type"),
      list_type: fd.get("list_type"),
      note: String(fd.get("note") || "").trim(),
    };
    if (virtual) body.virtual_id = Number(virtual);
    if (openid) body.openid = openid;
    try {
      await apiTry(`${BASE}/api/access`, { method: "POST", body });
      toast("已添加");
      closeModal();
      onDone();
    } catch { /* toast 已提示 */ }
  });
}

// ==================== 记录 → ID ====================

const IDMAP_KIND = { user: ["用户", "accent"], group: ["群", "ok"], bot: ["Bot", ""] };
const IDMAP_PAGE = 50;

async function renderIdmap(main) {
  main.innerHTML = `
    <div class="page-head">
      <h2>ID</h2>
      <div class="toolbar">
        <input id="idmap-q" class="search" placeholder="搜索 ID / openid / 昵称 / appid"
               autocomplete="off" autocapitalize="off" spellcheck="false">
        <select id="idmap-kind">
          <option value="">全部类型</option>
          <option value="user">用户</option>
          <option value="group">群</option>
          <option value="bot">Bot</option>
        </select>
      </div>
    </div>
    <p class="page-hint">每个 bot 看到的用户/群 openid 不同，这里是它们对应的 15 位 ID。用户在启用的群或私聊里发过消息才会出现；也可以向 bot 发「获取信息」直接看到自己的 ID。</p>
    <div id="idmap-list"></div>`;

  const list = pagedList({
    mount: $("#idmap-list"),
    pageSize: IDMAP_PAGE,
    head: "<th>ID</th><th>类型</th><th>昵称</th><th>所属 Bot</th><th>openid</th><th>最后活跃</th>",
    rowHTML: idmapRowHTML,
    empty: () => {
      const q = $("#idmap-q")?.value.trim();
      return `<div class="empty"><span class="big">⌕</span>` +
        `${q ? "没有匹配的映射记录" : "暂无映射记录"}<br>` +
        `提示: 用户先私聊/群内@bot 发一条消息, 映射才会出现</div>`;
    },
    fetchPage: async (offset, limit, signal) => {
      const p = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      const q = $("#idmap-q")?.value.trim() || "";
      if (q) p.set("q", q);
      const kind = $("#idmap-kind")?.value || "";
      if (kind) p.set("kind", kind);
      return api(`${BASE}/api/idmap?${p}`, { signal });
    },
  });
  onViewLeave(list.destroy);

  $("#idmap-q").addEventListener("input", debounce(list.reset, 350));
  $("#idmap-kind").addEventListener("change", list.reset);
}

function idmapRowHTML(r) {
  const [kn, kc] = IDMAP_KIND[r.kind] || [r.kind, ""];
  return `<tr>
    <td>${copyCell(r.virtual_id)}</td>
    <td><span class="badge ${kc}">${esc(kn)}</span></td>
    <td>${esc(r.nickname || "") || '<span class="muted">-</span>'}</td>
    <td class="mono">${esc(r.bot_appid)}</td>
    <td>${copyCell(r.openid, shortId(r.openid))}</td>
    <td>${timeCell(r.last_seen)}</td>
  </tr>`;
}

// ==================== 消息页 ====================

const MSG_PAGE = 50;

/** OneBot 段类型 → 中文标签; 未知类型原样显示 */
const SEG_LABEL = {
  text: "文本", image: "图片", record: "语音", audio: "语音", video: "视频",
  file: "文件", at: "@", reply: "回复", face: "表情", markdown: "MD",
  keyboard: "按钮", ark: "卡片", embed: "卡片", node: "转发", forward: "转发",
  json: "JSON", share: "分享", location: "位置",
};
const segLabel = (t) => SEG_LABEL[t] || String(t || "?");

/** 非文本段渲染成小徽章: [图片] [图片]×3 */
function kindBadges(kinds) {
  const counts = new Map();
  for (const k of kinds || []) {
    if (k === "text") continue;
    counts.set(k, (counts.get(k) || 0) + 1);
  }
  if (!counts.size) return "";
  return [...counts].map(([k, n]) =>
    `<span class="badge mini">[${esc(segLabel(k))}]${n > 1 ? `×${n}` : ""}</span>`).join("");
}

/** 名字 + 可复制的 id; 没有名字(或名字就是号本身)只显示号 */
function whoCell(id, name) {
  if (!id) return '<span class="muted">-</span>';
  const label = name && String(name) !== String(id) ? `<div class="who-name">${esc(name)}</div>` : "";
  return label + copyCell(id);
}

function msgRowHTML(r) {
  const sum = String(r.summary || "");
  const badges = kindBadges(r.kinds);
  return `<tr class="msg-row" data-mid="${esc(r.mid)}">
    <td class="nowrap">${timeCell(r.ts)}</td>
    <td>${copyCell(r.mid)}</td>
    <td><span class="badge ${r.direction === "in" ? "accent" : "ok"}">${r.direction === "in" ? "收" : "发"}</span></td>
    <td><span class="badge">${r.chat_type === "group" ? "群" : "私聊"}</span> ${whoCell(r.peer_virtual, r.peer_name)}</td>
    <td>${whoCell(r.user_virtual, r.user_name)}</td>
    <td><span class="msg-sum">${badges}${sum ? esc(sum) : '<span class="muted">(无文本)</span>'}</span></td>
    <td class="nowrap"><button type="button" class="btn small" data-detail="${esc(r.mid)}">详情</button></td>
  </tr>`;
}

async function renderMessages(main) {
  const sig = viewSignal();
  const bots = await api(`${BASE}/api/bots`, { signal: sig });
  if (sig?.aborted) return;
  main.innerHTML = `
    <div class="page-head">
      <h2>消息</h2>
      <div class="toolbar">
        <input id="msg-q" class="search" placeholder="搜索 内容 / 群或用户 ID / 消息 ID"
               autocomplete="off" autocapitalize="off" spellcheck="false">
        <select id="msg-bot">
          <option value="">全部 bot</option>
          ${bots.map((b) => `<option value="${esc(b.appid)}">${esc(b.name || b.appid)} (${esc(b.appid)})</option>`).join("")}
        </select>
        <select id="msg-type">
          <option value="">全部类型</option>
          <option value="group">群聊</option>
          <option value="private">私聊</option>
        </select>
        <button id="msg-refresh" class="btn">刷新</button>
      </div>
    </div>
    <div id="msg-list"></div>`;

  const box = $("#msg-list");
  const list = pagedList({
    mount: box,
    pageSize: MSG_PAGE,
    head: "<th>时间</th><th>消息 ID</th><th>收/发</th><th>会话</th>" +
          "<th>发送者</th><th>内容摘要</th><th></th>",
    rowHTML: msgRowHTML,
    empty: () => {
      const q = $("#msg-q")?.value.trim();
      return `<div class="empty"><span class="big">💬</span>` +
        `${q ? "没有匹配的消息" : "暂无消息记录"}<br>换个筛选或关键词试试</div>`;
    },
    fetchPage: async (offset, limit, signal) => {
      const p = new URLSearchParams({ limit: String(limit), offset: String(offset) });
      const appid = $("#msg-bot")?.value || "";
      if (appid) p.set("appid", appid);
      const ct = $("#msg-type")?.value || "";
      if (ct) p.set("chat_type", ct);
      const q = $("#msg-q")?.value.trim() || "";
      if (q) p.set("q", q);
      return api(`${BASE}/api/messages?${p}`, { signal });
    },
  });
  onViewLeave(list.destroy);

  // 整行可点 (「详情」按钮只是可见的抓手); 复制按钮不算
  box.addEventListener("click", (e) => {
    if (e.target.closest("[data-copy]")) return;
    const tr = e.target.closest("tr.msg-row");
    if (tr?.dataset.mid) messageDetailModal(tr.dataset.mid);
  });

  $("#msg-q").addEventListener("input", debounce(list.reset, 350));
  $("#msg-bot").addEventListener("change", list.reset);
  $("#msg-type").addEventListener("change", list.reset);
  $("#msg-refresh").addEventListener("click", list.refresh);
}

// ---------- 消息详情 ----------

function jsonPretty(v) {
  if (v === undefined) return "undefined";
  try {
    const s = JSON.stringify(v, null, 2);
    return s === undefined ? String(v) : s;
  } catch { return String(v); }
}

/** 列表里没有 content, 点开才按 mid 单取完整记录 */
function messageDetailModal(mid) {
  const modal = openModal(`消息详情 #${mid}`, `<div id="msg-detail">${LIST_LOADING_HTML}</div>`,
                          { wide: true });
  const ctl = new AbortController();
  onModalClose(() => ctl.abort());     // ✕ / 遮罩 / Esc 都会中断在途请求
  loadMessageDetail(modal, mid, ctl);
}

async function loadMessageDetail(modal, mid, ctl) {
  const box = $("#msg-detail", modal);
  if (!box) return;
  box.innerHTML = LIST_LOADING_HTML;
  let rec;
  try {
    rec = await api(`${BASE}/api/messages/${encodeURIComponent(mid)}`, { signal: ctl.signal });
  } catch (e) {
    if (e?.aborted || e?.status === 401 || !document.body.contains(box)) return;
    box.innerHTML = `<div class="prov-msg err">${esc(e?.message || "加载失败")}</div>` +
      '<button type="button" class="btn block" data-retry>重试</button>';
    box.querySelector("[data-retry]")
      .addEventListener("click", () => loadMessageDetail(modal, mid, ctl));
    return;
  }
  if (!document.body.contains(box)) return;   // 期间弹窗被关了
  box.innerHTML = messageDetailHTML(rec);
  box.querySelector("[data-close3]")?.addEventListener("click", closeModal);
}

/** 媒体预览: CDN 失效或本地已过期时给占位, 文件给下载链接 */
const GONE = (label) =>
  `<span class="seg-gone">[${label}已失效]</span>`;
function previewHTML(p) {
  const label = { image: "图片", video: "视频", audio: "语音", file: "文件" }[p.type] || "媒体";
  if (!p.url || p.state !== "ok") return `<div class="dm-item">${GONE(label)}</div>`;
  const url = esc(p.local ? `${BASE}/api/media/${encodeURIComponent(p.local)}` : p.url);
  const onerr = `onerror="this.replaceWith(Object.assign(document.createElement('span'),` +
    `{className:'seg-gone',textContent:'[${label}已失效]'}))"`;
  if (p.type === "image") {
    return `<div class="dm-item"><img class="dm-img" src="${url}" data-zoom="${url}" alt="图片"
      loading="lazy" referrerpolicy="no-referrer" ${onerr}></div>`;
  }
  if (p.type === "video") {
    return `<div class="dm-item dm-wide"><video class="dm-video" src="${url}" controls preload="metadata"
      referrerpolicy="no-referrer" ${onerr}></video></div>`;
  }
  if (p.type === "audio") {
    return `<div class="dm-item dm-wide"><audio controls preload="none"
      src="${esc(BASE)}/api/chat/audio?url=${encodeURIComponent(p.url)}"></audio></div>`;
  }
  return `<div class="dm-item dm-wide"><a class="btn small" href="${url}" target="_blank"
    rel="noopener noreferrer" download>下载 ${esc(p.name || "文件")}</a></div>`;
}

/** 全部字段一律 esc(); 长内容后端已截断 */
function messageDetailHTML(r) {
  const segs = Array.isArray(r.content) ? r.content : [];
  const kv = kvRow;
  const segHTML = segs.length
    ? segs.map((seg, i) => {
        const type = String(seg?.type ?? "?");
        const body = type === "text"
          ? String(seg?.data?.text ?? "")
          : jsonPretty(seg?.data);
        return `<div class="seg">
          <div class="seg-head">
            <span class="seg-idx">#${i + 1}</span>
            <span class="badge ${type === "text" ? "" : "accent"}">${esc(segLabel(type))}</span>
            <span class="mono muted">${esc(type)}</span>
          </div>
          <pre class="seg-body">${esc(body) || '<span class="muted">(空)</span>'}</pre>
        </div>`;
      }).join("")
    : '<div class="muted">(无消息段)</div>';

  return `
    <div class="detail-meta">
      ${kv("时间", `<span>${esc(fullTime(r.ts))} <span class="muted">· ${esc(relTime(r.ts))}</span></span>`)}
      ${kv("方向", `<span><span class="badge ${r.direction === "in" ? "accent" : "ok"}">${r.direction === "in" ? "收" : "发"}</span> ` +
        `<span class="badge">${r.chat_type === "group" ? "群聊" : "私聊"}</span></span>`)}
      ${kv("Bot", `<span class="mono wrap">${esc(r.bot_appid || "-")}</span>`)}
      ${kv("消息 ID", copyCell(r.mid))}
      ${kv("会话 ID", r.peer_virtual ? copyCell(r.peer_virtual) : '<span class="muted">-</span>')}
      ${kv("发送者 ID", r.user_virtual ? copyCell(r.user_virtual) : '<span class="muted">-</span>')}
      ${kv("会话 openid", r.peer_openid ? copyCell(r.peer_openid, shortId(r.peer_openid)) : '<span class="muted">-</span>')}
      ${kv("平台消息 id", r.qq_msg_id ? copyCell(r.qq_msg_id, shortId(r.qq_msg_id)) : '<span class="muted">-</span>')}
    </div>
    ${(r.previews || []).length ? `<h4 class="detail-h">媒体 <span class="muted">(${r.previews.length})</span></h4>
      <div class="detail-media">${r.previews.map(previewHTML).join("")}</div>` : ""}
    <h4 class="detail-h">消息段 <span class="muted">(${segs.length})</span></h4>
    <div class="seg-list">${segHTML}</div>
    ${r.sender && Object.keys(r.sender).length
      ? `<h4 class="detail-h">sender</h4><pre class="seg-body">${esc(jsonPretty(r.sender))}</pre>` : ""}
    <div class="modal-actions">
      <button type="button" class="btn" data-close3>关闭</button>
    </div>`;
}

// ==================== 后端预设配置页 ====================

/** 高级用户只读; PUT 是 admin-only, 所以非管理员整页禁用输入 */
async function renderProvision(main) {
  const sig = viewSignal();
  const cfg = await api(`${BASE}/api/provision/config`, { signal: sig });
  if (sig?.aborted) return;
  const ro = !isAdmin();
  const d = ro ? " disabled" : "";
  const bs = cfg.bs || {};
  const rt = cfg._bs_runtime || {};   // 现读自 BotShepherd, 只读展示
  const profiles = cfg.profiles || [];

  const mode = cfg.mode || "onebot";
  main.innerHTML = `
    <div class="prov">
      <div class="page-head">
        <h2>后端配置</h2>
        <div class="toolbar">${ro ? "" : '<button id="prov-save" class="btn primary">保存</button>'}</div>
      </div>
      <p class="page-hint">${ro
        ? "只读视图 —— 修改后端预设需要<b>管理员</b>账号。"
        : "「创建bot」只在设有默认预设时自动接后端；没有默认预设就保持未配置。卡片上的「配置后端」仍可手动选择任意预设。"}</p>

      <section class="help-card prov-card">
        <h3>接入方式</h3>
        <label class="check-row"><input type="radio" name="prov-mode" value="onebot"${mode === "onebot" ? " checked" : ""}${d}>
          直连 OneBot —— 预设里的端点直接写成 bot 的反向 WS 端点</label>
        <label class="check-row"><input type="radio" name="prov-mode" value="botshepherd"${mode === "botshepherd" ? " checked" : ""}${d}>
          经 <a href="https://github.com/Loping151/BotShepherd" target="_blank" rel="noopener">BotShepherd</a>
          —— 为每个 bot 建一条独立连接, 预设里的端点作为该连接的下游</label>
      </section>

      <section class="help-card prov-card" id="bs-card"${mode === "botshepherd" ? "" : ' hidden'}>
        <h3>BotShepherd 接入</h3>
        <label>BotShepherd 根目录
          <input id="bs-dir" class="mono" value="${esc(bs.dir || "")}" placeholder="/path/to/BotShepherd"${d}>
        </label>
        <p class="hint">连接目录与面板端口都由它推出。</p>
        <label>Web 面板地址（留空自动）
          <input id="bs-web_base" class="mono" value="${esc(bs.web_base || "")}" placeholder="留空 = 用 global_config 里的 web_port"${d}>
        </label>
        <div class="help-note">
          <b>凭据不落盘</b>：面板用户名与端口每次现读 BotShepherd 的
          <code>global_config.json</code>；密码来自环境变量 <code>BS_WEB_PASSWORD</code>，
          或在下面「保存密码」（只存内存，重启失效），不会写进任何文件。
          <div class="prov-runtime">
            <div>当前用户名：<span class="mono">${esc(rt.username || "读取失败")}</span></div>
            <div>面板地址：<span class="mono">${esc(rt.web_base || "-")}</span></div>
            <div>连接目录：<span class="mono">${esc(rt.connections_dir || "-")}</span></div>
            <div>密码：${{
              session: '<span class="badge ok">已在本页设置（重启失效）</span>',
              env: '<span class="badge ok">来自环境变量</span>',
            }[rt.password_source] || '<span class="badge">未设置（只写连接文件，需重启 BS 才生效）</span>'}</div>
            ${rt.error ? `<div class="err">读取 BotShepherd 配置失败：${esc(rt.error)}</div>` : ""}
          </div>
        </div>
        <div class="prov-cred-row">
          <label>BotShepherd 密码（全局，仅存内存）
            <input id="bs-pw" type="password" autocomplete="new-password"
                   placeholder="${rt.password_ready ? "已就绪，留空即不改" : "设置后配置才能立即生效"}"${d}>
          </label>
          ${ro ? "" : '<button type="button" id="bs-pw-save" class="btn">保存密码</button>'}
        </div>
        <p class="hint">不写进任何文件，服务重启即失效；也可在 systemd 里注入
          <code>BS_WEB_PASSWORD</code> 长期有效。保存时会真去登录 BotShepherd 校验。</p>
        <div class="form-row">
          <label>监听绑定地址
            <input id="bs-client_bind" class="mono" value="${esc(bs.client_bind || "")}" placeholder="0.0.0.0"${d}>
          </label>
          <label>端口区间 起
            <input id="bs-port_start" type="number" inputmode="numeric" min="1" max="65535" value="${esc(bs.port_start ?? "")}"${d}>
          </label>
          <label>端口区间 止
            <input id="bs-port_end" type="number" inputmode="numeric" min="1" max="65535" value="${esc(bs.port_end ?? "")}"${d}>
          </label>
        </div>
      </section>

      <div class="prov-sec-head">
        <h3>后端预设</h3>
        ${ro ? "" : '<div class="toolbar"><button id="prof-clear-default" class="btn small">不设默认</button>' +
          '<button id="prof-add" class="btn small">＋ 新增预设</button></div>'}
      </div>
      <p class="hint prof-intro">每个预设是一组 OneBot 反向 WebSocket 地址（如 nonebot2 的 <code>ws://127.0.0.1:8080/onebot/v11/ws</code>）。
        新 bot 按默认预设接入；卡片上的「配置后端」可以选任意预设。</p>
      <div id="prof-list">${profiles.map((p) => profCardHTML(p, ro)).join("") ||
        '<div class="empty">尚未配置后端预设</div>'}</div>
      ${ro ? "" : '<button id="prov-save2" class="btn primary block prov-save2">保存</button>'}
    </div>`;

  if (ro) return;

  main.querySelectorAll('input[name="prov-mode"]').forEach((r) =>
    r.addEventListener("change", () => {
      $("#bs-card").hidden = r.value !== "botshepherd";
    }));

  const pwSave = $("#bs-pw-save");
  if (pwSave) {
    pwSave.addEventListener("click", async () => {
      const password = $("#bs-pw").value;
      if (!password) { toast("请输入密码", "err"); return; }
      pwSave.disabled = true;
      try {
        const r = await apiTry(`${BASE}/api/provision/bs_credential`,
                               { method: "POST", body: { password } });
        toast(`BotShepherd 密码已生效 (${r.username || ""})`);
        $("#bs-pw").value = "";
        navigate();
      } catch { /* toast 已提示 */ } finally { pwSave.disabled = false; }
    });
  }

  const list = $("#prof-list");
  $("#prof-add").addEventListener("click", () => {
    list.querySelector(".empty")?.remove();
    list.insertAdjacentHTML("beforeend",
      profCardHTML({ name: "", targets: [""] }, false));
    list.lastElementChild.scrollIntoView({ behavior: "smooth", block: "center" });
  });
  $("#prof-clear-default").addEventListener("click", () => {
    list.querySelectorAll(".prof-def").forEach((input) => { input.checked = false; });
    toast("已取消默认预设，保存后新 bot 不会自动配置后端");
  });
  list.addEventListener("click", (e) => {
    const card = e.target.closest(".prof-card");
    if (!card) return;
    if (e.target.closest("[data-prof-del]")) {
      const name = card.querySelector(".prof-name").value.trim();
      if (!confirm(`确定删除预设 "${name || "(未命名)"}"?`)) return;
      card.remove();
      if (!list.querySelector(".prof-card")) {
        list.innerHTML = '<div class="empty">尚未配置后端预设</div>';
      }
      return;
    }
    if (e.target.closest("[data-tgt-add]")) {
      card.querySelector(".prof-targets")
        .insertAdjacentHTML("beforeend", profTargetRowHTML("", false));
      return;
    }
    if (e.target.closest("[data-tgt-del]")) {
      const rows = card.querySelectorAll(".ep-row");
      if (rows.length <= 1) { card.querySelector(".tgt-in").value = ""; return; }
      e.target.closest(".ep-row").remove();
    }
  });

  const save = () => saveProvisionConfig(cfg);
  $("#prov-save").addEventListener("click", save);
  $("#prov-save2").addEventListener("click", save);
}

function profCardHTML(p, ro) {
  const d = ro ? " disabled" : "";
  const targets = (p.targets || []).length ? p.targets : [""];
  return `<section class="prof-card">
    <div class="prof-head">
      <input class="prof-name" value="${esc(p.name || "")}" placeholder="预设名称, 如: 默认"${d}>
      <label class="check-row prof-def-row">
        <input type="radio" name="prof-default" class="prof-def"${p.default ? " checked" : ""}${d}> 设为默认
      </label>
      ${ro ? "" : '<button type="button" class="btn small danger" data-prof-del>删除该预设</button>'}
    </div>
    <div class="prof-targets">${targets.map((t) => profTargetRowHTML(t, ro)).join("")}</div>
    ${ro ? "" : '<button type="button" class="btn small" data-tgt-add>＋ 添加目标端点</button>'}
    <label>access_token <span class="muted">(直连模式用, 可选; 与后端 ONEBOT_ACCESS_TOKEN 一致)</span>
      <input class="prof-token mono" value="${esc(p.access_token || "")}" autocomplete="off"${d}>
    </label>
  </section>`;
}

function profTargetRowHTML(url, ro) {
  return `<div class="ep-row">
    <input class="tgt-in mono" value="${esc(url || "")}" placeholder="ws://localhost:8088/onebot/v11/ws"${ro ? " disabled" : ""}>
    ${ro ? "" : '<button type="button" class="icon-btn" data-tgt-del title="删除此行">✕</button>'}
  </div>`;
}

/** 读回整页表单并 PUT (整个对象一起发, 后端按掩码保留旧密码) */
async function saveProvisionConfig(cfg) {
  const val = (id) => String($(`#${id}`).value || "").trim();
  const mode = document.querySelector('input[name="prov-mode"]:checked')?.value || "onebot";
  const portStart = parseInt(val("bs-port_start"), 10);
  const portEnd = parseInt(val("bs-port_end"), 10);
  if (!Number.isFinite(portStart) || !Number.isFinite(portEnd) ||
      portStart < 1 || portEnd > 65535 || portStart > portEnd) {
    toast("端口区间不合法 (1-65535 且 起 ≤ 止)", "err");
    return;
  }
  const profiles = [...document.querySelectorAll(".prof-card")].map((card) => {
    const item = {
      name: card.querySelector(".prof-name").value.trim(),
      targets: [...card.querySelectorAll(".tgt-in")]
        .map((i) => i.value.trim()).filter(Boolean),
    };
    if (card.querySelector(".prof-def").checked) item.default = true;
    const token = card.querySelector(".prof-token")?.value.trim();
    if (token) item.access_token = token;
    return item;
  });
  const bad = profiles.find((p) => !p.name || !p.targets.length);
  if (bad) {
    toast(bad.name ? `预设 "${bad.name}" 没有目标端点` : "预设名称不能为空", "err");
    return;
  }
  const bsBody = {
    ...(cfg.bs || {}),
    dir: val("bs-dir"),
    web_base: val("bs-web_base"),
    client_bind: val("bs-client_bind"),
    port_start: portStart,
    port_end: portEnd,
  };
  // 凭据不落盘: 后端也会剥掉这两个字段, 这里不发送
  delete bsBody.username;
  delete bsBody.password;
  const body = { ...cfg, mode, bs: bsBody, profiles };
  delete body._bs_runtime;   // 只读快照, 不回写
  try {
    await apiTry(`${BASE}/api/provision/config`, { method: "PUT", body });
    toast("后端配置已保存");
    navigate();
  } catch { /* toast 已提示 */ }
}

// ==================== 插件页 ====================

/** 插件列表: 开关 + 每插件配置; 改完插件代码点「重新加载」即生效(不监听文件) */
async function renderPlugins(main) {
  const sig = viewSignal();
  const data = await api(`${BASE}/api/plugins`, { signal: sig });
  if (sig?.aborted) return;
  const ro = !isAdmin();
  const d = ro ? " disabled" : "";
  const fieldHTML = (p, f) => {
    const v = p.config?.[f.key] ?? f.default;
    const help = f.help ? `<p class="hint">${esc(f.help)}</p>` : "";
    if (f.type === "bool") {
      return `<label class="check-row"><input type="checkbox" data-key="${esc(f.key)}"${v ? " checked" : ""}${d}> ${esc(f.label)}</label>${help}`;
    }
    const input = f.type === "text"
      ? `<textarea data-key="${esc(f.key)}" rows="3"${d}>${esc(v)}</textarea>`
      : `<input data-key="${esc(f.key)}" ${f.type === "int" ? 'type="number" inputmode="numeric"' : ""} value="${esc(v)}" placeholder="${esc(f.placeholder || "")}"${d}>`;
    return `<label>${esc(f.label)}${input}</label>${help}`;
  };
  main.innerHTML = `
    <div class="page-head">
      <h2>插件</h2>
      <div class="toolbar">${ro ? "" : '<button id="plugin-reload" class="btn">重新加载</button>'}</div>
    </div>
    <p class="page-hint">改写收发内容的钩子。内置插件在 <code>qqbot_onebot/plugin/built-in/</code>，自己的放
      <code>qqbot_onebot/plugin/</code> 下（单文件或文件夹），改完点「重新加载」。写法见
      <a href="https://github.com/Loping151/qbob/blob/main/docs/plugins.md" target="_blank" rel="noopener">插件开发文档</a>。${ro ? " 修改需要<b>管理员</b>账号。" : ""}</p>
    ${(data.errors || []).map((e) =>
      `<p class="prov-msg err">加载失败 <span class="mono">${esc(e.source)}</span>：${esc(e.error)}</p>`).join("")}
    <div class="help">${(data.plugins || []).map((p) => `
      <section class="help-card plugin-card" data-name="${esc(p.name)}">
        <div class="plugin-head">
          <h3>${esc(p.title)}</h3>
          <span class="badge${p.builtin ? " accent" : ""}">${p.builtin ? "内置" : "自定义"}</span>
          <label class="switch" title="启用"><input type="checkbox" data-enabled${p.enabled ? " checked" : ""}${d}><span></span></label>
        </div>
        <p class="muted plugin-desc">${esc(p.description || "")}${p.doc
          ? ` <a href="${esc(p.doc)}" target="_blank" rel="noopener">配置说明</a>` : ""}</p>
        ${p.status ? `<p class="plugin-status${p.status_level === "error" ? " err" : ""}">${esc(p.status)}</p>` : ""}
        ${(p.fields || []).map((f) => fieldHTML(p, f)).join("")}
        <div class="plugin-foot">
          <span class="muted mono">${esc(p.name)} · ${(p.hooks || []).map(esc).join(", ")} · ${esc(p.source)}</span>
          ${ro || !(p.fields || []).length ? "" : '<button type="button" class="btn small primary" data-save>保存</button>'}
        </div>
      </section>`).join("") || '<div class="empty">没有插件</div>'}
    </div>`;
  if (ro) return;

  const put = async (name, body) => {
    await apiTry(`${BASE}/api/plugins/${encodeURIComponent(name)}`, { method: "PUT", body });
  };
  main.querySelectorAll(".plugin-card").forEach((card) => {
    const name = card.dataset.name;
    card.querySelector("[data-enabled]").addEventListener("change", async (e) => {
      try {
        await put(name, { enabled: e.target.checked });
        toast(e.target.checked ? "已启用" : "已停用");
        navigate();
      } catch { e.target.checked = !e.target.checked; }
    });
    const save = card.querySelector("[data-save]");
    if (save) save.addEventListener("click", async () => {
      const config = {};
      card.querySelectorAll("[data-key]").forEach((el) => {
        config[el.dataset.key] = el.type === "checkbox" ? el.checked : el.value;
      });
      try { await put(name, { config }); toast("配置已保存"); navigate(); } catch { /* toast 已提示 */ }
    });
  });
  $("#plugin-reload").addEventListener("click", async () => {
    try {
      const r = await apiTry(`${BASE}/api/plugins/reload`, { method: "POST" });
      toast(`已重新加载 ${(r.plugins || []).length} 个插件` +
            ((r.errors || []).length ? `，${r.errors.length} 个失败` : ""),
            (r.errors || []).length ? "err" : undefined);
      navigate();
    } catch { /* toast 已提示 */ }
  });
}

// ==================== 存储页 ====================

function fmtBytes(n) {
  n = Number(n || 0);
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1073741824) return `${(n / 1048576).toFixed(1)} MB`;
  return `${(n / 1073741824).toFixed(2)} GB`;
}

const TABLE_LABEL = {
  messages: "消息", forwards: "收到的聊天记录", forward_pages: "转发网页", id_map: "ID 映射",
  group_members: "群成员缓存", media_cache: "媒体上传缓存", passive_events: "被动回复凭据",
  peer_states: "群状态", access_list: "黑白名单",
};

async function renderStorage(main) {
  const sig = viewSignal();
  const st = await api(`${BASE}/api/storage`, { signal: sig });
  if (sig?.aborted) return;
  const ro = !isAdmin();
  const db = st.database, media = st.media, logs = st.logs, un = st.unparsed;
  const btn = (target, label, cls = "") =>
    ro ? "" : `<button type="button" class="btn small ${cls}" data-clean="${target}">${label}</button>`;
  const row = kvRow;
  main.innerHTML = `
    <div class="page-head"><h2>存储</h2>
      <div class="toolbar"><button id="st-refresh" class="btn">刷新</button></div></div>
    <p class="page-hint">会增长的数据都有上限或过期时间；这里看占用、手动清理。${ro ? " 清理需要<b>管理员</b>账号。" : ""}</p>
    <div class="help">
      <section class="help-card">
        <h3>数据库 <span class="badge">${fmtBytes(db.file + db.wal)}</span></h3>
        ${row("文件", `${fmtBytes(db.file)} <span class="muted">+ WAL ${fmtBytes(db.wal)}</span>`)}
        ${row("实际数据", fmtBytes(db.used))}
        ${row("可回收", `${fmtBytes(db.reclaimable)} <span class="muted">删掉的数据留下的空页，会被复用；压缩后还给磁盘</span>`)}
        ${row("消息保留", `${db.message_ttl_days} 天，最多 50 万条，每小时清一次`)}
        <div class="table-wrap"><table><thead><tr><th>内容</th><th>行数</th></tr></thead><tbody>
          ${Object.entries(db.rows).map(([t, n]) =>
            `<tr><td>${esc(TABLE_LABEL[t] || t)}</td><td class="mono">${Number(n).toLocaleString()}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="st-actions">${btn("expired", "立即清理过期数据")}${btn("vacuum", "压缩数据库", "danger")}</div>
        ${ro ? "" : '<p class="hint">压缩会锁库几秒，期间所有 bot 的收发排队等待</p>'}
      </section>
      <section class="help-card">
        <h3>媒体缓存 <span class="badge">${fmtBytes(media.bytes)}</span></h3>
        ${row("文件", `${media.files} 个`)}
        ${row("保留", `${media.ttl_hours} 小时`)}
        <p class="hint">发送时托管的图片/语音/视频，过期自动删除（「立即清理过期数据」也会清）</p>
      </section>
      <section class="help-card">
        <h3>日志 <span class="badge">${fmtBytes(logs.bytes)}</span></h3>
        ${logs.path ? row("位置", `<span class="mono wrap">${esc(logs.path)}</span>`) +
          row("上限", `${fmtBytes(logs.limit)}，超出自动轮转删旧`) : row("状态", "未启用日志文件（config.json 的 log_file 为空）")}
        <div class="st-actions">${logs.path ? btn("logs", "清空日志") : ""}</div>
      </section>
      <section class="help-card">
        <h3>未解析记录 <span class="badge">${fmtBytes(un.bytes)}</span></h3>
        ${row("位置", `<span class="mono wrap">${esc(un.path)}</span>`)}
        ${row("上限", fmtBytes(un.limit))}
        <p class="hint">平台发来但没能完全解析的数据，留着排查用</p>
        <div class="st-actions">${btn("unparsed", "清空")}</div>
      </section>
    </div>`;
  $("#st-refresh").addEventListener("click", navigate);
  main.querySelectorAll("[data-clean]").forEach((b) => b.addEventListener("click", async () => {
    const target = b.dataset.clean;
    const ask = { vacuum: "压缩数据库期间（几秒）所有 bot 的收发会排队等待，确定？",
                  logs: "确定清空日志文件？", unparsed: "确定清空未解析记录？" }[target];
    if (ask && !confirm(ask)) return;
    b.disabled = true;
    try {
      const r = await apiTry(`${BASE}/api/storage/cleanup`, { method: "POST", body: { target } });
      toast(r.message);
      navigate();
    } catch { b.disabled = false; }
  }));
}

// ==================== 选项页 ====================

async function renderOptions(main) {
  const sig = viewSignal();
  const data = await api(`${BASE}/api/options`, { signal: sig });
  if (sig?.aborted) return;
  const o = data.options || {};
  const up = data.update || {};
  const ro = !isAdmin();
  const d = ro ? " disabled" : "";
  const check = (key, label, hint) => `
    <label class="check-row"><input type="checkbox" name="${key}"${o[key] ? " checked" : ""}${d}> ${label}</label>
    ${hint ? `<p class="hint">${hint}</p>` : ""}`;
  main.innerHTML = `
    <div class="page-head">
      <h2>选项</h2>
      <div class="toolbar">${ro ? "" : '<button id="opt-save" class="btn primary">保存</button>'}</div>
    </div>
    <p class="page-hint">全局生效，保存后立即应用并写回 <code>config.json</code>。${ro ? " 修改需要<b>管理员</b>账号。" : ""}</p>
    <form id="opt-form" class="help">
      <section class="help-card">
        <h3>群启用</h3>
        <label>新 bot 的群启用方式
          <select name="default_group_list_mode"${d}>
            <option value="white"${o.default_group_list_mode !== "black" ? " selected" : ""}>需要启用（su 发「启用」后才响应）</option>
            <option value="black"${o.default_group_list_mode === "black" ? " selected" : ""}>总是启用（「禁用」的群除外）</option>
          </select>
        </label>
        <p class="hint">如通过其他方式控制 bot 启用状态，选择总是启用即可。已有 bot 在各自的「编辑」里改</p>
        ${check("require_recv_all", "要求群开启「接收全部消息」",
          "关闭后只开了 @ 消息的群也会响应；「主动消息」始终必需。开着时群主关掉全量接收即自动停用")}
      </section>
      <section class="help-card">
        <h3>超级用户</h3>
        <label>全局 superusers <span class="muted">(逗号分隔 15 位 id，对所有 bot 生效)</span>
          <input name="superusers" class="mono" value="${esc((o.superusers || []).join(", "))}"${d}>
        </label>
        <p class="hint">同一个人在不同 bot 下 id 不同，要管几个 bot 就填几个；也可在各 bot 的「编辑」里单独配置</p>
      </section>
      <section class="help-card">
        <h3>创建 bot</h3>
        ${check("ask_owner", "扫码成功后询问号主",
          "关闭后号主记为「未知」，直接按默认预设完成创建")}
        <label>默认 bot
          <select name="default_bot"${d}>
            <option value="">（自动：第一个连上的 bot）</option>
            ${(data.bots || []).map((b) => `<option value="${esc(b.appid)}"${b.appid === o.default_bot ? " selected" : ""}>${esc(b.name || b.appid)} · ${esc(b.appid)}</option>`).join("")}
            ${o.default_bot && !(data.bots || []).some((b) => b.appid === o.default_bot)
              ? `<option value="${esc(o.default_bot)}" selected>${esc(o.default_bot)}（未运行）</option>` : ""}
          </select>
        </label>
        <p class="hint">HTTP API 不指定 bot 时用它</p>
        <label>新 bot 的默认分组
          <select name="default_bot_group"${d}>
            ${(data.groups || []).map((g) => `<option value="${esc(g)}"${g === o.default_bot_group ? " selected" : ""}>${esc(g)}</option>`).join("")}
          </select>
        </label>
        <p class="hint">新 bot（包括「创建bot」接入的）放进这个分组；建分组、改名在「Bot 管理 → 分组」</p>
      </section>
      <section class="help-card">
        <h3>更新</h3>
        <p>当前 <b>v${esc(up.current || "?")}</b>${up.latest ? `，GitHub 上 <b>v${esc(up.latest)}</b>` : ""}
          ${up.has_update ? '<span class="badge ok">有新版本</span>' : up.latest ? '<span class="badge">已是最新</span>' : ""}</p>
        ${up.error ? `<p class="hint err-text">${esc(up.error)}</p>` : ""}
        ${up.checked_at ? `<p class="hint">上次检查 ${esc(fullTime(up.checked_at))}，之后每几小时自动检查</p>` : ""}
        <p>GitHub 镜像源：<span class="mono">${esc(up.mirror || "（未设置，直连 GitHub）")}</span></p>
        <p class="hint">访问不了 GitHub 时在 <code>data/config.json</code> 的 <code>update_mirror</code> 填 https 镜像前缀（如 https://ghfast.top/），
          它决定更新从哪拉代码，所以只能在配置文件里改。更新 = 在项目目录 <code>git pull --ff-only</code>，拉取后重启服务生效</p>
        ${ro ? "" : `<div class="st-actions"><button type="button" class="btn small" id="upd-check">检查更新</button>
          ${up.has_update ? `<button type="button" class="btn small primary" id="upd-pull">更新到 v${esc(up.latest)}</button>` : ""}</div>`}
      </section>
      <section class="help-card">
        <h3>发送</h3>
        ${check("recall_hint", "转发摊平成多条时附一句「可回复本消息一次性撤回」",
          "只影响这句提示发不发；没发提示就没有一次性撤回的入口")}
        ${check("retry_transient", "平台临时故障时自动重试",
          "平台回「请稍后重试」类错误时框架内再发 2 次（间隔 1 秒、3 秒），仍失败才报错给后端")}
      </section>
    </form>`;
  if (ro) return;
  $("#opt-save").addEventListener("click", async () => {
    const form = $("#opt-form");
    const body = {
      default_group_list_mode: form.default_group_list_mode.value,
      require_recv_all: form.require_recv_all.checked,
      ask_owner: form.ask_owner.checked,
      recall_hint: form.recall_hint.checked,
      retry_transient: form.retry_transient.checked,
      default_bot: form.default_bot.value,
      default_bot_group: form.default_bot_group.value,
      superusers: parseIdList(form.superusers.value),
    };
    try {
      await apiTry(`${BASE}/api/options`, { method: "PUT", body });
      toast("选项已保存");
      navigate();
    } catch { /* toast 已提示 */ }
  });
  $("#upd-check")?.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      const r = await apiTry(`${BASE}/api/update/check`, { method: "POST" });
      toast(r.error || (r.has_update ? `发现新版本 v${r.latest}` : "已是最新"), r.error ? "err" : "ok");
      loadFooter();
      navigate();
    } catch { e.target.disabled = false; }
  });
  $("#upd-pull")?.addEventListener("click", () => runUpdate(up));
}

// ==================== 用户管理页 ====================

async function renderUsers(main) {
  main.innerHTML = `
    <div class="page-head">
      <h2>用户管理</h2>
      <div class="toolbar">
        <button id="user-add" class="btn primary">＋ 添加用户</button>
      </div>
    </div>
    <p class="page-hint">角色：<b>只读</b> 仅查看 · <b>高级</b> 日常运营（管 bot 和群、聊天、看消息）· <b>管理员</b> 另可改全局设置、删 bot、看 secret、管账号。
      <a href="https://github.com/Loping151/qbob/blob/main/docs/console.md#角色与权限" target="_blank" rel="noopener">完整权限表</a></p>
    <div id="user-table"></div>`;

  const load = async () => {
    const box = $("#user-table");
    box.innerHTML = '<div class="loading">加载中…</div>';
    let rows;
    try {
      rows = await api(`${BASE}/api/users`, { signal: viewSignal() });
    } catch (e) {
      if (e.aborted || e.status === 401) return;
      box.innerHTML = `<div class="empty">加载失败: ${esc(e.message)}</div>`;
      return;
    }
    if (!document.body.contains(box)) return;
    box.innerHTML = `<div class="table-wrap"><table>
      <thead><tr><th>用户名</th><th>角色</th><th>创建时间</th><th></th></tr></thead>
      <tbody>${rows.map((u) => {
        const self = u.username === state.me.username;
        return `<tr>
          <td class="mono">${esc(u.username)}${self ? ' <span class="badge accent">当前</span>' : ""}</td>
          <td><span class="badge${u.role === "admin" ? " err" : u.role === "advanced" ? " accent" : ""}">${esc(ROLE_NAME[u.role] || u.role)}</span></td>
          <td>${timeCell(u.created_at)}</td>
          <td style="white-space:nowrap">
            <button class="btn small" data-edit="${esc(u.username)}" data-role="${esc(u.role)}">改密码/角色</button>
            <button class="btn small danger" data-del="${esc(u.username)}" ${self ? "disabled title='不能删除自己'" : ""}>删除</button>
          </td>
        </tr>`;
      }).join("")}</tbody>
    </table></div>`;

    box.querySelectorAll("[data-edit]").forEach((btn) =>
      btn.addEventListener("click", () =>
        userFormModal({ username: btn.dataset.edit, role: btn.dataset.role }, load)));
    box.querySelectorAll("[data-del]:not([disabled])").forEach((btn) =>
      btn.addEventListener("click", async () => {
        const name = btn.dataset.del;
        if (!confirm(`确定删除用户 "${name}"?`)) return;
        try {
          await apiTry(`${BASE}/api/users/${encodeURIComponent(name)}`, { method: "DELETE" });
          toast(`用户 ${name} 已删除`);
          load();
        } catch { /* toast 已提示 */ }
      }));
  };

  $("#user-add").addEventListener("click", () => userFormModal(null, load));
  load();
}

function userFormModal(user, onDone) {
  const isEdit = !!user;
  const modal = openModal(isEdit ? `编辑用户 · ${user.username}` : "添加用户", `
    <form id="user-form">
      <label>用户名 *
        <input name="username" required value="${esc(user?.username || "")}" ${isEdit ? "readonly" : ""} autocomplete="off">
      </label>
      <label>密码 ${isEdit ? '<span class="muted">(留空则不修改)</span>' : "*"}
        <input name="password" type="password" ${isEdit ? "" : "required"} placeholder="${isEdit ? "留空不修改" : ""}" autocomplete="new-password">
      </label>
      <label>角色
        <select name="role">
          <option value="user"${(user?.role || "user") === "user" ? " selected" : ""}>只读 (user)</option>
          <option value="advanced"${user?.role === "advanced" ? " selected" : ""}>高级 (advanced)</option>
          <option value="admin"${user?.role === "admin" ? " selected" : ""}>管理员 (admin)</option>
        </select>
      </label>
      <div class="modal-actions">
        <button type="button" class="btn" data-close2>取消</button>
        <button type="submit" class="btn primary">${isEdit ? "保存" : "创建"}</button>
      </div>
    </form>`);

  $("[data-close2]", modal).addEventListener("click", closeModal);
  $("#user-form", modal).addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const password = String(fd.get("password") || "");
    const role = fd.get("role");
    try {
      if (isEdit) {
        const body = { role };
        if (password) body.password = password;
        await apiTry(`${BASE}/api/users/${encodeURIComponent(user.username)}`, { method: "PUT", body });
        toast("已保存");
      } else {
        await apiTry(`${BASE}/api/users`, {
          method: "POST",
          body: { username: String(fd.get("username") || "").trim(), password, role },
        });
        toast("用户已创建");
      }
      closeModal();
      onDone();
    } catch { /* toast 已提示 */ }
  });
}

// ==================== 帮助页 ====================

/** 纯静态内容, 所有角色可见; webhook 地址异步补全, 失败则降级为占位模板 */
async function renderHelp(main) {
  main.innerHTML = `
    <div class="page-head"><h2>帮助</h2></div>
    <div class="help">

      <section class="help-card">
        <h3>一、内置指令 <span class="badge accent">适配器直接处理</span></h3>
        <p class="muted">这些指令由适配器自己回应, 不经后端插件, 也不受群启用状态限制 (但仍受下方「合规门槛」约束)。</p>

        <div class="help-item">
          <div class="help-item-head"><code>获取信息</code></div>
          <div class="help-kv"><span class="help-k">谁能用</span><span>群里的<b>群主 / 管理员</b>(把 bot 拉进群的人会被自动记为管理员)与 su; 其他成员会收到一句提示, 每个群 <b>60 秒</b>限一次。私聊里任何人都能用。<b>初次部署</b>(只有一个 bot 且还没设任何 su)时群里人人可用, 方便查到自己的 id。</span></div>
          <div class="help-kv"><span class="help-k">作用</span><span>回显 <code>AppID</code>、<code>用户ID</code>(15 位 ID, <b>不含 openid</b>)、群资料(群名 / 群人数 / 群简介, 取不到时省略)、发送者角色、bot 在该群的权限状态(主动消息是否开通、消息接收设置、bot 角色)、白名单状态, 以及一行 <code>启用码</code>(带着 bot 和群的 ID)。</span></div>
        </div>

        <div class="help-item">
          <div class="help-item-head"><code>启用 &lt;粘贴获取信息的输出&gt;</code></div>
          <div class="help-kv"><span class="help-k">谁能用</span><span>该 bot 的<b>超级用户</b>(su: 该 bot 自己配置的, 加上「设置 → 选项」里的全局 superusers)。</span></div>
          <div class="help-kv"><span class="help-k">作用</span><span>从粘贴或引用的内容里提取启用码里的 bot 与群 ID, 给<b>启用码所指向的那个 bot</b> 的白名单加上该群 —— 也就是可以<b>跨 bot 加白</b>: 每个 bot 看到的 openid 都不一样, 所以必须用虚拟号来指认。</span></div>
        </div>

        <div class="help-item">
          <div class="help-item-head"><code>禁用 &lt;同上&gt;</code></div>
          <div class="help-kv"><span class="help-k">谁能用</span><span>同 <code>启用</code>, 仅该 bot 的 su。</span></div>
          <div class="help-kv"><span class="help-k">作用</span><span>把该群从对应 bot 的白名单里移出。</span></div>
        </div>

        <div class="help-item">
          <div class="help-item-head"><code>创建bot</code> <span class="badge accent">全程在聊天里完成</span></div>
          <div class="help-kv"><span class="help-k">谁能用</span><span>该 bot 的<b>超级用户</b>(su)。</span></div>
          <div class="help-kv"><span class="help-k">作用</span><span>不用打开管理台就能接入一个新 bot：
            <ol class="help-steps">
              <li>发 <code>创建bot</code> → 回一条<b>授权链接</b>，把它发给 bot 的创建者(5 分钟内有效)。</li>
              <li>对方用手机 QQ 打开并授权 → 适配器自动拿到 AppID/Secret，建好 bot(放进默认分组)，然后带着<b>头像、名称、QQ 号</b>回来问一句 <b>号主？</b></li>
              <li><b>引用</b>那条「号主？」消息，回一个 <b>QQ 号</b>(如 <code>10001</code>) → 记为 <code>号主 10001</code>，并按<b>默认预设</b>接上后端(直连 OneBot，或在 BotShepherd 建好连接，见 <a href="#/provision">设置 → 后端</a>)。<a href="#/options">选项</a>里关掉「询问号主」则跳过这一步，号主记为「未知」。</li>
            </ol>
            <b>取消与回退</b>：回复的<b>不是 QQ 号</b>(例如「取消」)即视为放弃；1 小时没人回也会自动放弃；同一会话再发一次 <code>创建bot</code>，上一个未完成的也会被撤掉。
            这三种情况都会把那个半成品 bot <b>连同数据一起删除</b>，回到点链接之前的状态 ——
            它已经连上平台却没有后端，留着只会白占资源。<br>
            注意：<b>一个会话同一时间只保留一次</b>创建流程；授权者是号主、不是后端主人，所以<b>不会</b>自动设成 su，需要的话到管理台自行配置。</span></div>
        </div>

        <p class="help-note">指令前面可以带 <code>/</code>(<code>/获取信息</code> 一样有效); 在群里需要 <b>@机器人</b> 才能触发, 除非该群开了「接收全部消息」。</p>
      </section>

      <section class="help-card">
        <h3>二、典型接入流程</h3>
        <ol class="help-steps">
          <li>群主 / 用户把自己的 <b>AppID + AppSecret</b> 提交给管理员; 或者管理员点 <a href="#/bots">「Bot 管理」</a>页的 <b>「扫码添加」</b>, 让机器人主人用手机 QQ 扫码授权, 凭据自动带回表单。</li>
          <li>管理员在 <a href="#/bots">「Bot 管理」</a>页添加该 bot (默认 <b>WebSocket</b> 模式, 无需配置回调地址)。</li>
          <li>在该 bot 卡片上点 <b>「配置后端」</b>, 填号主 + 选预设, 自动接上后端 (直连或经 BotShepherd, 在 <a href="#/provision">「设置 → 后端」</a>设置)。</li>
          <li>群主把 bot 拉进群, 并在群里开启<b>「机器人主动发言」</b>和<b>「接收全部消息」</b> —— 默认<b>两个都开</b>才会响应, 否则完全静默(「接收全部消息」这条可在「设置 → 选项」里关掉)。</li>
          <li>群主在群里 <b>@机器人</b> 发送 <code>获取信息</code>, 把输出<b>整段</b>发给管理员。</li>
          <li>管理员对着<b>任意一个自己是 su 的 bot</b> 发送 <code>启用 &lt;粘贴内容&gt;</code>, 完成加白。</li>
          <li>该群即可正常使用。</li>
        </ol>
      </section>

      <section class="help-card">
        <h3>三、合规门槛</h3>
        <p class="help-note warn">必须<b>同时</b>满足 <code>主动消息已开通</code> 且 <code>消息接收设置 = 全部消息</code>, 适配器才会响应该群的<b>任何</b>消息(<b>包括内置指令</b>)。不满足时<b>完全静默</b> —— 不报错, 也没有任何回复。「全部消息」这一条可在 <a href="#/options">选项</a> 里关掉。</p>
        <p>这两项由<b>群主在 QQ 客户端的群设置</b>里控制, 适配器只读取、不修改。</p>
        <p class="muted">腾讯的查询接口有频率限制, 所以状态是缓存的: 首次见到该群时查一次; 收到内置指令时会复查(每群 30 秒限一次); 群主拨动开关时平台会推事件, 即时更新。也可以在 <a href="#/bots">「Bot 管理」</a>页展开 bot 卡片的「群状态」手动刷新。</p>
      </section>

      <section class="help-card">
        <h3>四、名单规则</h3>
        <ul class="help-ul">
          <li><b>群聊</b>: 默认「需要启用」(白名单) —— 没被 <code>启用</code> 的群完全静默。新 bot 的默认方式在「设置 → 选项」里改, 已有 bot 在<a href="#/bots">编辑</a>里改成「总是启用」(黑名单)。</li>
          <li><b>私聊</b>: 固定<b>黑名单</b>模式 —— 默认放开, 只有黑名单里的用户被静默, 不可更改。</li>
          <li>名单按 <code>(bot, openid)</code> 存储: 同一个群 / 用户在不同 bot 下是<b>彼此独立</b>的条目。</li>
        </ul>
      </section>

      <section class="help-card">
        <h3>五、su(超级用户)怎么配</h3>
        <p>su 是 <b>15 位虚拟号</b> —— 同一个人在不同 bot 下的虚拟号<b>不一样</b>。可以填在各 bot 的「编辑」里, 也可以填进 <a href="#/options">选项</a> 的全局 superusers(对所有 bot 生效)。</p>
        <p>获取方法: 向这个 bot 发 <code>获取信息</code>, 输出里的 <code>用户ID</code> 就是; 或到 <a href="#/idmap">「记录 → ID」</a>搜索昵称 / openid。</p>
      </section>

      <section class="help-card">
        <h3>六、事件模式</h3>
        <div class="help-item">
          <div class="help-item-head"><b>WebSocket</b><span class="badge ok">默认</span></div>
          <p class="help-p">适配器主动连接腾讯网关, 开放平台后台<b>无需填回调地址</b>, 配好就能连上。</p>
        </div>
        <div class="help-item">
          <div class="help-item-head"><b>Webhook</b></div>
          <p class="help-p">需要在开放平台后台把回调地址填成:</p>
          <p class="help-p" id="help-webhook"><span class="muted">加载中…</span></p>
          <p class="help-p muted">把 <code>{appid}</code> 换成对应 bot 的 AppID。</p>
        </div>
        <p class="help-note">收发消息(包括图片、语音)在两种模式下都不需要公网地址; Webhook 模式需要 QQ 能从公网访问到回调地址。</p>
      </section>

    </div>`;

  fillHelpWebhook();
}

/** 优先用已缓存的 /api/status; 没有就拉一次, 401/失败时降级显示模板 */
async function fillHelpWebhook() {
  let st = state.status;
  if (!st) {
    try {
      st = await api(`${BASE}/api/status`, { silent401: true });
      state.status = st;
      renderFooter();
    } catch { st = null; }
  }
  const box = $("#help-webhook");
  if (!box) return; // 用户已切走
  const base = st?.public_base_url || "";
  if (!base) {
    box.innerHTML = `<code>{public_base_url}/qqbot/webhook/{appid}</code> ` +
      `<span class="muted">(公网地址尚未配置, 把 <code>{public_base_url}</code> 换成实际公网地址)</span>`;
    return;
  }
  const url = `${base}/qqbot/webhook/{appid}`;
  box.innerHTML = `<code>${esc(url)}</code>` +
    `<button class="copy-btn" data-copy="${esc(url)}" title="复制">⧉</button>`;
}

// ==================== 主题 ====================
// 三态循环: 跟随系统 -> 亮 -> 暗. 存 localStorage, 刷新后保持.

const THEME_KEY = "qqob_theme";
const THEME_ORDER = ["", "light", "dark"];
const THEME_ICON = { "": "🌗", light: "☀️", dark: "🌙" };
const THEME_NAME = { "": "跟随系统", light: "亮色", dark: "暗色" };

function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
  const btn = $("#theme-btn");
  if (btn) {
    btn.textContent = THEME_ICON[theme] || "🌗";
    btn.title = `主题: ${THEME_NAME[theme] || "跟随系统"}（点击切换）`;
  }
}

function initTheme() {
  let current = "";
  try { current = localStorage.getItem(THEME_KEY) || ""; } catch { /* 隐私模式 */ }
  applyTheme(current);
  $("#theme-btn")?.addEventListener("click", () => {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(current) + 1) % THEME_ORDER.length];
    current = next;
    try { localStorage.setItem(THEME_KEY, next); } catch { /* ignore */ }
    applyTheme(next);
  });
}

// ==================== 全局事件 & 初始化 ====================

// 移动端表格堆叠视图: 依表头为每个 td 生成 data-label (供 ≤700px 的 td::before 显示);
// 打过标的行记 data-lab 跳过, 免得每次变动重扫全表。
function applyTableLabels(root = document) {
  root.querySelectorAll("table").forEach((tbl) => {
    const trs = tbl.querySelectorAll("tbody tr:not([data-lab])");
    if (!trs.length) return;
    const heads = [...tbl.querySelectorAll("thead th")].map((th) => th.textContent.trim());
    trs.forEach((tr) => {
      tr.dataset.lab = "1";
      [...tr.children].forEach((td, i) => {
        if (heads[i]) td.dataset.label = heads[i];
      });
    });
  });
}

// 观察 childList 统一打标(不观察属性, 不会自触发); 一帧内合并多次变动。
let labelPending = false;
new MutationObserver(() => {
  if (labelPending) return;
  labelPending = true;
  requestAnimationFrame(() => { labelPending = false; applyTableLabels(); });
}).observe(document.body, { childList: true, subtree: true });

// 复制按钮全局委托
document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-copy]");
  if (btn) copyText(btn.dataset.copy);
});

// 登录表单
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target;
  const errBox = $("#login-error");
  errBox.classList.add("hidden");
  const submitBtn = form.querySelector("button[type=submit]");
  submitBtn.disabled = true;
  try {
    await api(`${BASE}/api/login`, {
      method: "POST",
      silent401: true,
      body: { username: form.username.value.trim(), password: form.password.value },
    });
    // 登录只回用户名/角色; 「还在用初始密码」这类状态在 /api/me 里, 拿全了再进
    state.me = await api(`${BASE}/api/me`, { silent401: true });
    showApp();
  } catch (err) {
    errBox.textContent = err.message || "登录失败";
    errBox.classList.remove("hidden");
  } finally {
    submitBtn.disabled = false;
  }
});

// 登出
$("#logout-btn").addEventListener("click", async () => {
  try { await api(`${BASE}/api/logout`, { method: "POST" }); } catch { /* ignore */ }
  showLogin();
});

window.addEventListener("hashchange", navigate);

// 启动: 尝试恢复会话
(async function init() {
  initTheme();          // 先上主题, 免得登录页先闪一下系统色
  try {
    state.me = await api(`${BASE}/api/me`, { silent401: true });
    showApp();
  } catch {
    showLogin();
  }
})();

// ==================== 灯箱(图片放大) ====================
// 全站共用: bot 头像、聊天图片点击放大。

let lightboxEl = null;

export function openLightbox(src, caption = "") {
  closeLightbox();
  lightboxEl = document.createElement("div");
  lightboxEl.className = "lightbox";
  lightboxEl.innerHTML = `
    <button type="button" class="lb-close" title="关闭 (Esc)">✕</button>
    <img src="${esc(src)}" alt="${esc(caption)}">
    ${caption ? `<div class="lb-cap">${esc(caption)}</div>` : ""}`;
  // 点背景关闭, 点图片本身不关(免得想拖动/长按保存时误关)
  lightboxEl.addEventListener("click", (e) => {
    if (e.target === lightboxEl || e.target.closest(".lb-close")) closeLightbox();
  });
  document.body.appendChild(lightboxEl);
  document.body.classList.add("lb-open-lock");
}

export function closeLightbox() {
  if (!lightboxEl) return;
  lightboxEl.remove();
  lightboxEl = null;
  document.body.classList.remove("lb-open-lock");
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && lightboxEl) { e.stopPropagation(); closeLightbox(); }
}, true);

// 全局委托: 任何带 data-zoom 的图片点击即放大
document.addEventListener("click", (e) => {
  const img = e.target.closest("img[data-zoom]");
  if (img && img.src) openLightbox(img.dataset.zoom || img.src, img.alt || "");
});

// ==================== 统计 ====================
// 图表代码只在看的时候才拉, 同 chat.js
let statsModule = null;

async function renderStats(main) {
  main.innerHTML = '<div class="loading">加载统计模块…</div>';
  if (!statsModule) statsModule = await import('./stats.js');
  return statsModule.renderStats(main);
}

// ==================== 聊天室 ====================
// 首屏用不到, 点进来才拉
let chatModule = null;

async function renderChat(main) {
  main.innerHTML = '<div class="loading">加载聊天模块…</div>';
  if (!chatModule) chatModule = await import('./chat.js');
  return chatModule.renderChat(main);
}
