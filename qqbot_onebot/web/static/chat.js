// 聊天室页: 从 app.js 动态 import 进来(体量大, 不进首屏包)
import { BASE, api, esc, toast, debounce, $, LIST_LOADING_HTML,
         viewSignal, onViewLeave, relTime, openLightbox, closeLightbox,
         openModal, onModalClose }
  from './app.js';

// ==================== 聊天室 ====================

const CHAT_PAGE = 40;
const CHAT_POLL_MS = 4000;
const CHAT_DOM_MAX = 300;    // 气泡 DOM 上限, 超了从顶部裁
const RECALL_WINDOW_SEC = 120;  // 非群管只能撤 2 分钟内的(群管不受此限)
const CHAT_INPUT_MAX = 140;     // 输入框最高多少像素, 与 style.css 的 max-height 一致

const CHAT_SAVE_KEY = "qqob_chat_last";

function saveChatPick() {
  try {
    localStorage.setItem(CHAT_SAVE_KEY, JSON.stringify({
      appid: chatState.appid, chatType: chatState.chatType,
      peerId: chatState.peerId, peerName: chatState.peerName }));
  } catch { /* 隐私模式下不可用, 忽略 */ }
}

function loadChatPick() {
  try { return JSON.parse(localStorage.getItem(CHAT_SAVE_KEY) || "null"); }
  catch { return null; }
}

const chatState = {
  appid: "", chatType: "", peerId: 0, peerName: "",
  oldest: 0, newest: 0, hasMore: false, loading: false, quotes: {},
  forwards: {},          // 聊天记录卡片: id -> {count, lines, nodes?, gone?}(见 chatview.py)
  botRole: "",   // bot 在当前群的角色: admin/owner 才给撤回别人和禁言
  mdEnabled: true,       // 当前 bot 是否开了 markdown(决定气泡渲不渲染)
  pending: [],           // 待发送的附件 {url, name, kind}
  replyTo: null,         // 正在引用的消息 {mid, nickname, summary}
};

export async function renderChat(main) {
  const sig = viewSignal();
  const [status, bots] = await Promise.all([
    api(`${BASE}/api/status`, { signal: sig }),
    api(`${BASE}/api/bots`, { signal: sig }),
  ]);
  if (sig?.aborted) return;
  const live = new Map((status.bots || []).map((b) => [b.appid, b]));
  const runnable = bots.filter((b) => b.enabled);
  // 支持从 bot 卡片带 appid 跳进来: #/chat/<appid>
  const saved = loadChatPick();
  const wanted = (location.hash.split("/")[2] || "").trim();
  if (wanted && runnable.some((b) => b.appid === wanted)) chatState.appid = wanted;
  else if (!chatState.appid && saved
           && runnable.some((b) => b.appid === saved.appid)) {
    chatState.appid = saved.appid;      // 刷新后回到上次看的 bot
  }
  if (!runnable.some((b) => b.appid === chatState.appid)) {
    chatState.appid = runnable[0]?.appid || "";
  }
  // 关了 markdown 的 bot 发出去的就是纯文本, 聊天室也别渲染(见 renderMarkdown)
  const mdOf = new Map(runnable.map((b) => [b.appid, b.markdown_enabled !== false]));
  const syncMd = () => { chatState.mdEnabled = mdOf.get(chatState.appid) !== false; };
  syncMd();

  main.innerHTML = `
    <div class="page-head">
      <h2>聊天</h2>
      <div class="toolbar">
        <select id="chat-bot">
          ${runnable.map((b) => `<option value="${esc(b.appid)}"${b.appid === chatState.appid ? " selected" : ""}>${esc(b.name || b.appid)}${live.get(b.appid)?.qq_connected ? "" : "（离线）"}</option>`).join("")}
        </select>
      </div>
    </div>
    ${runnable.length ? "" : '<div class="empty">没有已启用的 bot</div>'}
    <div class="chat-wrap${runnable.length ? "" : " hidden"}">
      <details class="chat-side" id="chat-side" open>
        <summary class="chat-side-head">
          <span id="chat-side-label">会话列表</span>
        </summary>
        <input id="chat-search" class="search" placeholder="搜索群名 / 群号"
               autocomplete="off" autocapitalize="off" spellcheck="false">
        <div id="chat-peers" class="chat-peers"></div>
      </details>
      <section class="chat-main" id="chat-main">
        <div class="chat-empty" id="chat-placeholder">
          选择一个会话开始
        </div>
      </section>
    </div>`;

  if (!runnable.length) return;
  bindDropZone($("#chat-main"));      // 随 #chat-main 一起建, 一个视图只绑一次
  document.addEventListener("contextmenu", onChatContextMenu);
  $("#chat-bot").addEventListener("change", (e) => {
    chatState.appid = e.target.value;
    chatState.peerId = 0;
    syncMd();
    saveChatPick();
    loadPeers();
    $("#chat-main").innerHTML =
      '<div class="chat-empty">选择一个会话开始</div>';
  });
  $("#chat-search").addEventListener("input", debounce(loadPeers, 300));
  $("#chat-peers").addEventListener("click", (e) => {
    const row = e.target.closest("[data-peer]");
    if (!row) return;
    openConversation(row.dataset.type, Number(row.dataset.peer), row.dataset.name);
  });

  // 清理必须在任何 await 之前注册: 否则 await 期间切页, 之后建的 interval 没人收
  const timer = { id: null };
  const dismissCtx = () => closeCtxMenu();
  const onEsc = (e) => { if (e.key === "Escape") closeCtxMenu(); };
  document.addEventListener("click", dismissCtx);
  document.addEventListener("scroll", dismissCtx, true);
  document.addEventListener("keydown", onEsc);
  // 回到前台立即补拉一次: 后台时轮询暂停, 手机还会冻结定时器
  const onVisible = () => {
    if (!document.hidden && chatState.peerId) pollNewMessages();
  };
  document.addEventListener("visibilitychange", onVisible);
  document.addEventListener("paste", onDocumentPaste);
  document.addEventListener("dragover", swallowStrayDrop);
  document.addEventListener("drop", swallowStrayDrop);
  onViewLeave(() => {
    if (timer.id) clearInterval(timer.id);
    closeLightbox();
    closeCtxMenu();
    document.removeEventListener("contextmenu", onChatContextMenu);
    document.removeEventListener("click", dismissCtx);
    document.removeEventListener("scroll", dismissCtx, true);
    document.removeEventListener("keydown", onEsc);
    document.removeEventListener("visibilitychange", onVisible);
    document.removeEventListener("paste", onDocumentPaste);
    document.removeEventListener("dragover", swallowStrayDrop);
    document.removeEventListener("drop", swallowStrayDrop);
    document.body.classList.remove("chat-fs");   // 别把全屏态带到别的页
  });
  await loadPeers();
  if (viewSignal()?.aborted) return;      // 期间已经切页, 不要再起表
  // 刷新后自动回到上次那个会话, 不用重新找一遍
  if (!chatState.peerId && saved?.peerId && saved.appid === chatState.appid) {
    const row = document.querySelector(`[data-peer="${saved.peerId}"]`);
    if (row) openConversation(row.dataset.type, Number(row.dataset.peer),
                              row.dataset.name);
  }
  // 轮询新消息: 页面隐藏/失焦时跳过, 不在后台空转
  timer.id = setInterval(() => {
    if (document.hidden || !chatState.peerId) return;
    pollNewMessages();
  }, CHAT_POLL_MS);
}

async function loadPeers() {
  const box = $("#chat-peers");
  if (!box) return;
  const q = $("#chat-search")?.value.trim() || "";
  box.innerHTML = LIST_LOADING_HTML;
  try {
    const params = new URLSearchParams({ appid: chatState.appid, limit: "80" });
    if (q) params.set("q", q);
    const res = await api(`${BASE}/api/chat/peers?${params}`, { signal: viewSignal() });
    const items = res?.items || [];
    if (!items.length) {
      box.innerHTML = `<div class="chat-empty small">${q ? "没有匹配的会话" : "还没有消息记录"}</div>`;
      return;
    }
    box.innerHTML = items.map((p) => `
      <div class="peer-row${p.peer_id === chatState.peerId ? " active" : ""}"
           data-peer="${p.peer_id}" data-type="${esc(p.chat_type)}" data-name="${esc(p.name)}">
        <span class="peer-ava">${avatarChar(p.name)}</span>
        <span class="peer-body">
          <span class="peer-name">${esc(p.name)}</span>
          <span class="peer-meta">${p.chat_type === "group" ? "群" : "私聊"} · ${p.count} 条 · ${relTime(p.last_ts)}</span>
        </span>
      </div>`).join("");
  } catch (e) {
    if (e?.aborted) return;
    box.innerHTML = `<div class="chat-empty small">加载失败: ${esc(e.message)}</div>`;
  }
}

async function openConversation(chatType, peerId, name) {
  Object.assign(chatState, {
    chatType, peerId, peerName: name,
    oldest: 0, newest: 0, hasMore: false, pending: [], quotes: {},
    forwards: {}, replyTo: null,
  });
  saveChatPick();
  document.querySelectorAll(".peer-row").forEach((el) =>
    el.classList.toggle("active", Number(el.dataset.peer) === peerId));
  const label = document.querySelector("#chat-side-label");
  if (label) label.textContent = name || "会话列表";
  // 窄屏: 选完就收起, 把高度让给消息区(桌面端保持展开)
  const side = document.querySelector("#chat-side");
  if (side && window.matchMedia("(max-width: 860px)").matches) side.open = false;
  $("#chat-main").innerHTML = `
    <header class="chat-head">
      <button type="button" class="btn small chat-back" id="chat-back"
              title="返回会话列表">‹</button>
      <b id="chat-title">${esc(name)}</b>
      <span class="muted mono">${peerId}</span>
      <span class="badge">${chatType === "group" ? "群聊" : "私聊"}</span>
      <button type="button" class="btn small ghost chat-refresh" id="chat-refresh"
              title="立即拉取最新消息">刷新</button>
    </header>
    <div class="chat-log" id="chat-log">${LIST_LOADING_HTML}</div>
    <div class="chat-reply hidden" id="chat-reply"></div>
    <div class="chat-attach hidden" id="chat-attach"></div>
    <form class="chat-composer" id="chat-form">
      <label class="btn ghost chat-file" title="发送图片 / 视频 / 文件（也可直接拖入或粘贴）">
        ＋<input type="file" id="chat-file" hidden multiple>
      </label>
      <textarea id="chat-input" rows="1" placeholder="说点什么…"></textarea>
      <button type="submit" class="btn primary">发送</button>
    </form>`;

  $("#chat-log").addEventListener("scroll", onChatScroll);
  $("#chat-log").addEventListener("click", onQuoteJump);
  $("#chat-log").addEventListener("click", onBubbleAction);
  $("#chat-back")?.addEventListener("click", exitFullscreen);
  $("#chat-refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      // 整页重拉: 休眠久了可能漏了不止一页; 服务端顺带刷新群名/角色
      await loadMessages({ initial: true });
      loadPeers();                    // 左侧排序/计数也捋新, 不等它
    } finally {
      btn.disabled = false;
    }
  });
  enterFullscreen();
  $("#chat-file").addEventListener("change", onPickFiles);
  const input = $("#chat-input");
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  input.addEventListener("input", () => autoGrowInput(input));
  $("#chat-form").addEventListener("submit", onChatSend);
  await loadMessages({ initial: true });
}

async function loadMessages({ initial = false, before = 0 } = {}) {
  if (chatState.loading) return;
  chatState.loading = true;
  const log = $("#chat-log");
  try {
    const params = new URLSearchParams({
      appid: chatState.appid, chat_type: chatState.chatType,
      peer_id: String(chatState.peerId), limit: String(CHAT_PAGE),
    });
    if (before) params.set("before", String(before));
    const res = await api(`${BASE}/api/chat/messages?${params}`, { signal: viewSignal() });
    if (!log || !document.body.contains(log)) return;
    Object.assign(chatState.quotes, res?.quotes || {});
    mergeForwards(res?.forwards);
    const items = res?.items || [];
    chatState.hasMore = !!res?.has_more;
    if (res?.bot_role !== undefined) chatState.botRole = res.bot_role || "";
    // 群名可能是刚补拉到的(或群主改过): 顺手更新标题与左侧那一行
    if (res?.peer_name && res.peer_name !== chatState.peerName) {
      chatState.peerName = res.peer_name;
      saveChatPick();
      const title = document.querySelector("#chat-title");
      if (title) title.textContent = res.peer_name;
      const label = document.querySelector("#chat-side-label");
      if (label) label.textContent = res.peer_name;
      const row = document.querySelector(`[data-peer="${chatState.peerId}"] .peer-name`);
      if (row) row.textContent = res.peer_name;
    }
    if (initial) {
      log.innerHTML = items.length
        ? items.map(bubbleHTML).join("")
        : '<div class="chat-empty small">还没有消息</div>';
      if (items.length) {
        chatState.oldest = items[0].mid;
        chatState.newest = items[items.length - 1].mid;
      }
      if (chatState.hasMore) prependMoreBar(log);
      // 贴底: 同步先来一次, 布局/图片解码完再补几次(不依赖任何回调必达)
      log.scrollTop = log.scrollHeight;
      settleToBottom(log);
    } else if (!items.length) {
      // 没有更早的了: 把按钮换成到底提示, 别让人一直点
      const bar = log.querySelector(".chat-more");
      if (bar) bar.outerHTML = '<div class="inf-msg end">没有更早的消息了</div>';
    } else {
      // 向上翻页: 锚在"当前第一条"上, 插入后把它挪回原来的视觉位置
      const anchor = log.querySelector(".bub-row");
      const anchorTop = anchor ? anchor.getBoundingClientRect().top : 0;
      log.querySelector(".chat-more")?.remove();
      log.insertAdjacentHTML("afterbegin", items.map(bubbleHTML).join(""));
      chatState.oldest = items[0].mid;
      if (chatState.hasMore) prependMoreBar(log);
      if (anchor) {
        log.scrollTop += anchor.getBoundingClientRect().top - anchorTop;
      }
    }
  } catch (e) {
    if (!e?.aborted && log) {
      log.insertAdjacentHTML("beforeend",
        `<div class="chat-empty small">加载失败: ${esc(e.message)}</div>`);
    }
  } finally {
    chatState.loading = false;
  }
}

/** 输入框跟着内容长高. border-box 下 scrollHeight 不含边框, 要补上
 *  (offsetHeight - clientHeight), 否则多行时被裁并冒出滚动条. */
function autoGrowInput(input) {
  input.style.height = "auto";
  const border = input.offsetHeight - input.clientHeight;
  input.style.height =
    Math.min(input.scrollHeight + border, CHAT_INPUT_MAX) + "px";
  // 变高会挤掉消息区高度: 原本贴底就跟着贴回去
  const log = $("#chat-log");
  if (log && log.scrollHeight - log.scrollTop - log.clientHeight < 120) {
    log.scrollTop = log.scrollHeight;
  }
}

/** 算不算"贴着底". 阈值须大于一张图片(解码后撑高 240px),
 *  否则刚插入的图会把自己挤出贴底状态, 之后不再跟滚. */
function nearBottom(log) {
  const away = log.scrollHeight - log.scrollTop - log.clientHeight;
  return away < Math.max(280, log.clientHeight * 0.8);
}

/** 贴底: 布局/媒体加载会改变高度, 分几次补位. 用 setTimeout 不用 rAF(手机上会被节流) */
function settleToBottom(log) {
  const stick = () => {
    if (!document.body.contains(log)) return;
    // 只在用户还贴着底部时才跟, 别把正在翻旧消息的人拽走
    if (nearBottom(log)) log.scrollTop = log.scrollHeight;
  };
  for (const delay of [0, 60, 250, 800]) setTimeout(stick, delay);
  for (const img of log.querySelectorAll("img")) {
    if (img.complete) continue;
    img.addEventListener("load", stick, { once: true });
    img.addEventListener("error", stick, { once: true });
  }
  for (const v of log.querySelectorAll("video")) {
    if (v.readyState >= 1) continue;
    v.addEventListener("loadedmetadata", stick, { once: true });
  }
}

/** DOM 窗口上限: 活跃群开一整天能堆几千条, 手机上会被系统杀掉标签页 */
function trimLog(log) {
  const rows = log.querySelectorAll(".bub-row");
  const excess = rows.length - CHAT_DOM_MAX;
  for (let i = 0; i < excess; i++) rows[i].remove();
  if (excess > 0) {
    // 顶部被裁掉了, 重新标记还能往上翻
    chatState.hasMore = true;
    const first = log.querySelector(".bub-row");
    if (first) chatState.oldest = Number(first.dataset.mid) || chatState.oldest;
    if (!log.querySelector(".chat-more")) prependMoreBar(log);
  }
}

// 发言人头衔: 和 QQ 一样跟在昵称后面, 只有群主/管理员才显示
const ROLE_TAG = { owner: "群主", admin: "管理员" };

function prependMoreBar(log) {
  if (log.querySelector(".chat-more")) return;
  log.insertAdjacentHTML("afterbegin",
    '<div class="chat-more"><button type="button" class="btn small">加载更早的消息</button></div>');
  log.querySelector(".chat-more button").addEventListener("click", (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "加载中…";
    loadMessages({ before: chatState.oldest });
  });
}

/** 手机上单个会话全屏: 列表让位, 消息区占满一屏 */
function enterFullscreen() {
  if (!window.matchMedia("(max-width: 860px)").matches) return;
  document.body.classList.add("chat-fs");
  const side = document.querySelector("#chat-side");
  if (side) side.open = false;
}

function exitFullscreen() {
  document.body.classList.remove("chat-fs");
  const side = document.querySelector("#chat-side");
  if (side) side.open = true;
  // 回到列表页: 手机上聊天区只以全屏形态存在, 退出就把它清掉
  if (window.matchMedia("(max-width: 860px)").matches) {
    chatState.peerId = 0;
    const main = document.querySelector("#chat-main");
    if (main) main.innerHTML = "";
  }
  side?.scrollIntoView({ block: "start", behavior: "instant" });
}

// ---------- 右键菜单 ----------
// 聊天页禁掉默认右键, 气泡与头像上弹自己的菜单. 例外: 输入框保留原生菜单;
// 触屏不接管(长按留给文字选择).
let ctxMenuEl = null;

function closeCtxMenu() {
  ctxMenuEl?.remove();
  ctxMenuEl = null;
}

function onChatContextMenu(e) {
  if (window.matchMedia("(hover: none)").matches) return;
  if (e.target.closest("textarea, input")) return;
  e.preventDefault();                     // 聊天页内一律压掉默认菜单
  closeCtxMenu();
  const row = e.target.closest(".bub-row");
  if (!row) return;
  const items = [];
  // 图片上右键(含头像): 顶一项放大, 顶替被禁掉的原生"在新标签打开"
  const zoomable = e.target.closest("img[data-zoom], img.bub-ava");
  if (zoomable) {
    const src = zoomable.dataset.zoom || zoomable.src;
    items.push({ label: "查看大图", run: () => openLightbox(src, zoomable.alt || "") });
  }
  // 现成的操作按钮(引用/撤回/禁言)原样搬进菜单: 显隐/权限逻辑零重复
  for (const btn of row.querySelectorAll(".bub-act:not(:disabled)")) {
    items.push({ label: btn.textContent, run: () => btn.click() });
  }
  const text = (row.querySelector(".bub")?.textContent || "").trim();
  if (text) {
    items.push({ label: "复制文本", run: async () => {
      try {
        await navigator.clipboard.writeText(text);
        toast("已复制");
      } catch {
        toast("复制失败：浏览器未授权剪贴板", "err");
      }
    } });
  }
  if (!items.length) return;
  ctxMenuEl = document.createElement("div");
  ctxMenuEl.className = "ctx-menu";
  for (const item of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = item.label;
    b.addEventListener("click", () => { closeCtxMenu(); item.run(); });
    ctxMenuEl.appendChild(b);
  }
  document.body.appendChild(ctxMenuEl);
  const rect = ctxMenuEl.getBoundingClientRect();   // 先量再摆, 不让菜单出屏
  ctxMenuEl.style.left = `${Math.min(e.clientX, innerWidth - rect.width - 8)}px`;
  ctxMenuEl.style.top = `${Math.min(e.clientY, innerHeight - rect.height - 8)}px`;
}

/** 引用某条消息: 摘一句摘要挂到输入框上方, 发送时作为 reply 段带出去.
 *  摘要用后端给的(data-sum): 卡片/表情/聊天记录的 textContent 读起来不是一句话 */
function pickReply(btn) {
  const row = btn.closest(".bub-row");
  chatState.replyTo = {
    mid: Number(btn.dataset.replyTo),
    nickname: row?.querySelector(".bub-name")?.textContent
      || (row?.classList.contains("mine") ? "我" : ""),
    summary: row?.dataset.sum || "",
  };
  renderReplyTo();
  $("#chat-input")?.focus();
}

function renderReplyTo() {
  const box = $("#chat-reply");
  if (!box) return;
  const target = chatState.replyTo;
  box.classList.toggle("hidden", !target);
  if (!target) { box.innerHTML = ""; return; }
  box.innerHTML = `
    <span class="chat-reply-body">回复 <b>${esc(target.nickname || "消息")}</b>
      ${esc(target.summary)}</span>
    <button type="button" class="icon-btn" data-drop-reply title="取消引用">✕</button>`;
  box.querySelector("[data-drop-reply]").addEventListener("click", () => {
    chatState.replyTo = null;
    renderReplyTo();
  });
}

/** 引用 / 撤回 / 禁言 */
async function onBubbleAction(e) {
  const fwd = e.target.closest("[data-fwd]");
  if (fwd) { openForward(fwd.dataset.fwd); return; }
  const quote = e.target.closest("[data-reply-to]");
  if (quote) { pickReply(quote); return; }
  const recall = e.target.closest("[data-recall]");
  const mute = e.target.closest("[data-mute]");
  if (recall) {
    if (!confirm("撤回这条消息？")) return;
    recall.disabled = true;
    try {
      await api(`${BASE}/api/chat/recall`, { method: "POST", body: {
        appid: chatState.appid, mid: Number(recall.dataset.recall) } });
      toast("已撤回");
      const row = recall.closest(".bub-row");
      row?.querySelector(".bub")?.classList.add("recalled");
      row?.querySelector(".bub-time")?.insertAdjacentHTML(
        "beforebegin", '<span class="badge mini recalled-tag">已撤回</span>');
      row?.querySelector(".bub-acts")?.remove();
    } catch (err) {
      recall.disabled = false;
      if (err.status !== 401) toast(err.message, "err", 6000);
    }
    return;
  }
  if (mute) {
    const name = mute.dataset.name || mute.dataset.mute;
    const input = prompt(`禁言 ${name} 多少分钟？(0 = 解除禁言)`, "10");
    if (input === null) return;
    const minutes = Number(input);
    if (!Number.isFinite(minutes) || minutes < 0) { toast("请输入分钟数", "err"); return; }
    mute.disabled = true;
    try {
      await api(`${BASE}/api/chat/mute`, { method: "POST", body: {
        appid: chatState.appid, peer_id: chatState.peerId,
        user_id: Number(mute.dataset.mute), duration: Math.round(minutes * 60) } });
      toast(minutes ? `已禁言 ${minutes} 分钟` : "已解除禁言");
    } catch (err) {
      if (err.status !== 401) toast(err.message, "err", 6000);
    } finally {
      mute.disabled = false;
    }
  }
}

/** 点引用条 -> 跳到原消息并高亮; 不在已加载范围内就先往上翻 */
async function onQuoteJump(e) {
  const chip = e.target.closest("[data-reply]");
  if (!chip) return;
  const mid = Number(chip.dataset.reply);
  if (!mid) return;
  const log = $("#chat-log");
  let target = log.querySelector(`.bub-row[data-mid="${mid}"]`);
  // 往上多翻几页找它(每页 40 条, 找 5 页够用了, 再多说明离太远)
  for (let i = 0; !target && i < 5 && chatState.hasMore && mid < chatState.oldest; i++) {
    await loadMessages({ before: chatState.oldest });
    target = log.querySelector(`.bub-row[data-mid="${mid}"]`);
  }
  if (!target) { toast("原消息不在最近的记录里", "warn"); return; }
  target.scrollIntoView({ block: "center", behavior: "smooth" });
  target.classList.add("flash");
  setTimeout(() => target.classList.remove("flash"), 1600);
}

function onChatScroll(e) {
  if (e.target.scrollTop < 40 && chatState.hasMore && !chatState.loading) {
    loadMessages({ before: chatState.oldest });
  }
}

async function pollNewMessages() {
  const log = $("#chat-log");
  if (!log || !document.body.contains(log) || chatState.loading) return;
  try {
    const params = new URLSearchParams({
      appid: chatState.appid, chat_type: chatState.chatType,
      peer_id: String(chatState.peerId), limit: String(CHAT_PAGE),
    });
    const res = await api(`${BASE}/api/chat/messages?${params}`, { signal: viewSignal() });
    Object.assign(chatState.quotes, res?.quotes || {});
    mergeForwards(res?.forwards);
    // 已在 DOM 里的不重复渲染(并发轮询/整页重拉与增量交错都可能重复)
    const items = (res?.items || []).filter(
      (m) => m.mid > chatState.newest
        && !log.querySelector(`.bub-row[data-mid="${m.mid}"]`));
    if (!items.length) return;
    if (items.length >= CHAT_PAGE) {
      // 一整页全是新的: 中间可能还漏了更多, 直接整页重拉
      await loadMessages({ initial: true });
      return;
    }
    // 贴底时才自动滚动, 用户在翻旧消息就别打扰
    const atBottom = nearBottom(log);
    log.querySelector(".chat-empty")?.remove();
    log.insertAdjacentHTML("beforeend", items.map(bubbleHTML).join(""));
    chatState.newest = items[items.length - 1].mid;
    trimLog(log);
    if (atBottom) {
      log.scrollTop = log.scrollHeight;
      settleToBottom(log);          // 刚插入的图片/视频加载完还会长高, 跟到底
    }
  } catch { /* 轮询失败静默, 下一轮再来 */ }
}

/** 头像占位: 名字首字, 不用 emoji(Array.from 按码点切, 代理对昵称不出乱码); 已转义 */
function avatarChar(name) {
  const ch = Array.from(String(name || "").trim())[0] || "";
  return ch ? esc(ch) : "?";
}

/** 一条消息 -> 气泡. 自己发的靠右, 别人的靠左带头像+昵称 */
function bubbleHTML(m) {
  const mine = m.direction === "out";
  const name = m.nickname || String(m.user_id || "");
  const role = mine ? chatState.botRole : m.role;
  const tag = ROLE_TAG[role];
  const roleTag = tag ? `<span class="badge mini role ${role}">${tag}</span>` : "";
  // 图挂了由 app.js 的 onMediaError 处理: 先换 data-fb 的备用链接, 再挂换成首字占位
  const fb = m.avatar_fb ? ` data-fb="${esc(m.avatar_fb)}"` : "";
  const avatar = m.avatar
    ? `<img class="bub-ava" src="${esc(m.avatar)}"${fb} data-gone="${avatarChar(name)}"
         data-gone-class="bub-ava ph" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="bub-ava ph">${avatarChar(name)}</span>`;
  return `<div class="bub-row ${mine ? "mine" : ""}" data-mid="${m.mid}"
    data-sum="${esc(m.summary || "")}">
    ${avatar}
    <div class="bub-col">
      <div class="bub-meta">${mine ? roleTag : `<span class="bub-name">${esc(name)}</span>${roleTag}`}
        ${m.recalled ? '<span class="badge mini recalled-tag">已撤回</span>' : ""}
        <span class="bub-time" title="${esc(fmtTime(m.ts))}">${fmtClock(m.ts)}</span></div>
      <div class="bub${m.recalled ? " recalled" : ""}">${
        segmentsHTML(m.content, mine && chatState.mdEnabled)}</div>
      ${bubbleActionsHTML(m, mine)}
    </div>
  </div>`;
}

/** 气泡下的操作, 只显示做得到的. 撤回(实测, 与文档不符): bot 是群管则自己的不限时,
 *  别人的也能撤但群主/管理员的报 40062003; 普通成员只能撤自己 2 分钟内的.
 *  禁言要 bot 是群管且对方不是管理层. */
function bubbleActionsHTML(m, mine) {
  if (m.recalled) return "";
  const admin = chatState.botRole === "admin" || chatState.botRole === "owner";
  const fresh = Date.now() / 1000 - (m.ts || 0) < RECALL_WINDOW_SEC;
  const targetIsStaff = m.role === "owner" || m.role === "admin";
  const canRecall = m.recallable !== false
    && (mine ? admin || fresh
             : admin && !targetIsStaff && chatState.chatType === "group");
  const acts = [`<button type="button" class="bub-act" data-reply-to="${m.mid}">引用</button>`];
  if (canRecall) {
    acts.push(`<button type="button" class="bub-act" data-recall="${m.mid}">撤回</button>`);
  }
  if (!mine && admin && !targetIsStaff && chatState.chatType === "group" && m.user_id) {
    acts.push(`<button type="button" class="bub-act" data-mute="${m.user_id}"
                 data-name="${esc(m.nickname || "")}">禁言</button>`);
  }
  return acts.length ? `<div class="bub-acts">${acts.join("")}</div>` : "";
}

function fmtClock(ts) {
  const d = new Date((ts || 0) * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function fmtTime(ts) {
  return new Date((ts || 0) * 1000).toLocaleString();
}

/** OneBot 段数组 -> 气泡内容 HTML. 段已由后端整理过(chatview.py): face 带 name/emoji,
 *  卡片类带 card, 连续的 node 已并成一个 forward 段.
 *  md: 这条文字会不会被当 markdown 发出去(出站 + bot 开了 markdown)
 */
function segmentsHTML(segments, md = false) {
  const parts = [];
  const segs = (segments || []).filter((seg) => seg && typeof seg === "object");
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    const d = seg.data || {};
    switch (seg.type) {
      case "text": {
        const text = String(d.text || "");
        parts.push(md && looksLikeMarkdown(text)
          ? `<div class="seg-md">${renderMarkdown(text)}</div>`
          : `<span class="seg-text">${linkify(text)}</span>`);
        if (d.cut) parts.push(`<span class="seg-gone">…（原文 ${Number(d.cut)} 字，入库时截断）</span>`);
        break;
      }
      case "image":
      case "mface": {
        const url = mediaSrc(d.url || d.file);
        parts.push(url
          ? `<img class="seg-img" src="${esc(url)}" data-zoom="${esc(url)}" alt="图片"
               loading="lazy" referrerpolicy="no-referrer" decoding="async"
               data-gone="[图片已失效]">`
          : '<span class="seg-gone">[图片]</span>');
        break;
      }
      case "video": {
        const url = mediaSrc(d.url || d.file);
        parts.push(url
          ? `<video class="seg-video" src="${esc(url)}" controls preload="metadata" referrerpolicy="no-referrer"></video>`
          : '<span class="seg-gone">[视频]</span>');
        break;
      }
      case "record":
      case "audio": {
        const url = String(d.url || d.file || "");
        // QQ 语音是 silk/amr, 浏览器都放不了(显示 0 秒) -> 走服务端转码
        parts.push(mediaSrc(url)
          ? `<audio class="seg-audio" controls preload="none"
               src="${esc(BASE)}/api/chat/audio?url=${encodeURIComponent(url)}"></audio>`
          : '<span class="seg-gone">[语音]</span>');
        break;
      }
      case "file": {
        const url = mediaSrc(d.url || d.file);
        const name = String(d.name || d.file_name || "文件");
        parts.push(url
          ? `<a class="seg-file" href="${esc(url)}" target="_blank" rel="noopener noreferrer"><span class="seg-tag">文件</span>${esc(name)}</a>`
          : `<span class="seg-gone">[文件] ${esc(name)}</span>`);
        break;
      }
      case "at":
        parts.push(`<span class="seg-at">@${esc(d.qq === "all" ? "全体成员" : d.name || d.qq || "")}</span>`);
        break;
      case "reply": {
        const q = chatState.quotes[String(d.id || "")];
        parts.push(q
          ? `<span class="seg-quote" data-reply="${esc(d.id)}" title="点击跳转到原消息">
               <b>${esc(q.nickname || "原消息")}</b>${esc(q.summary || "")}</span>`
          : `<span class="seg-quote gone" data-reply="${esc(d.id || "")}">引用的消息不在记录里</span>`);
        break;
      }
      case "face": {
        // 连续同一个表情并成一个 + ×N(刷屏的一排「流泪」一眼看完)
        let n = 1;
        while (i + n < segs.length && segs[i + n].type === "face"
               && String(segs[i + n].data?.id) === String(d.id)
               && segs[i + n].data?.name === d.name) n++;
        i += n - 1;
        parts.push(faceHTML(d, n));
        break;
      }
      case "forward":
      case "node":
        parts.push(forwardCardHTML(String(d.id || "")));
        break;
      case "json":
      case "music":
      case "share":
        parts.push(d.card ? cardHTML(d.card)
          : `<span class="seg-gone">[${seg.type === "music" ? "音乐" : "卡片"}]</span>`);
        break;
      case "xml":
        parts.push('<span class="seg-gone">[卡片]</span>');
        break;
      case "markdown":
        parts.push(`<div class="seg-md">${renderMarkdown(String(d.content || ""))}</div>`);
        break;
      case "keyboard":
        parts.push('<span class="seg-gone">[按钮]</span>');
        break;
      default:
        parts.push(`<span class="seg-gone">[${esc(seg.type)}]</span>`);
    }
  }
  return parts.join("") || '<span class="seg-gone">(空消息)</span>';
}

/** QQ 表情: 有 emoji 替身用替身, 没有显示 [名字]; n>1 时带 ×n. 悬停看名字 */
function faceHTML(d, n = 1) {
  const name = String(d.name || "表情");
  const times = n > 1 ? `<span class="seg-face-n">×${n}</span>` : "";
  return d.emoji
    ? `<span class="seg-face emo" title="${esc(name)}">${esc(d.emoji)}${times}</span>`
    : `<span class="seg-face" title="${esc(name)}">[${esc(name)}]${times}</span>`;
}

/** ARK / 音乐 / 分享卡片: 有链接整张可点; 图片挂了直接隐藏(data-gone 空串) */
function cardHTML(c) {
  const img = (url, cls) => (url
    ? `<img class="${cls}" src="${esc(url)}" alt="" loading="lazy" referrerpolicy="no-referrer"
         data-gone="" data-gone-class="sc-gone">` : "");
  const inner = `${img(c.image, "sc-img")}<span class="sc-body">`
    + `<span class="sc-title">${esc(c.title || c.label || "卡片")}</span>`
    + (c.desc ? `<span class="sc-desc">${esc(c.desc)}</span>` : "")
    + `<span class="sc-foot">${img(c.icon, "sc-icon")}`
    + `<span>${esc(c.source || c.label || "卡片")}</span></span></span>`;
  return c.url
    ? `<a class="seg-card" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer"
         title="${esc(c.url)}">${inner}</a>`
    : `<span class="seg-card">${inner}</span>`;
}

/** 聊天记录卡片: 前几条预览 + 条数, 点开看全部(openForward) */
function forwardCardHTML(id) {
  const f = chatState.forwards[id];
  if (!f || f.gone) {
    return '<span class="seg-fwd gone"><span class="fwd-title">聊天记录</span>'
      + '<span class="fwd-foot">内容已不在库里</span></span>';
  }
  const lines = (f.lines || []).map((l) =>
    `<span class="fwd-line"><b>${esc(l.name)}</b>：${esc(l.text)}</span>`).join("");
  return `<button type="button" class="seg-fwd" data-fwd="${esc(id)}">`
    + `<span class="fwd-title">聊天记录</span>${lines}`
    + `<span class="fwd-foot">查看 ${Number(f.count) || 0} 条转发消息</span></button>`;
}

/** 轮询会反复带回同一批预览; 已经点开取过的节点别被冲掉 */
function mergeForwards(map) {
  for (const [id, f] of Object.entries(map || {})) {
    const had = chatState.forwards[id];
    chatState.forwards[id] = { ...f, nodes: f.nodes || had?.nodes };
  }
}

/** 打开聊天记录. 嵌套的在同一个弹窗里往下钻, 顶上给「返回上一层」 */
let fwdTrail = [];
async function openForward(id, { nested = false } = {}) {
  const f = chatState.forwards[id];
  if (!f || f.gone) return;
  const trail = nested ? [...fwdTrail, id] : [id];
  // openModal 会先跑上一个弹窗的清理(清空 fwdTrail), 所以先算好再赋值
  const modal = openModal("聊天记录", `<div class="fwd-view">${LIST_LOADING_HTML}</div>`,
                          { wide: true });
  fwdTrail = trail;
  onModalClose(() => { fwdTrail = []; });
  const box = modal.querySelector(".fwd-view");
  box.addEventListener("click", onForwardViewClick);
  if (!f.nodes) {
    try {
      const res = await api(`${BASE}/api/chat/forward?${new URLSearchParams({
        appid: chatState.appid, id })}`);
      mergeForwards(res?.forwards);
      f.nodes = res?.nodes || [];
    } catch (e) {
      if (document.body.contains(box)) {
        box.innerHTML = `<div class="chat-empty small">加载失败: ${esc(e.message)}</div>`;
      }
      return;
    }
  }
  if (!document.body.contains(box)) return;     // 期间弹窗被关了
  box.innerHTML = forwardViewHTML(f.nodes);
}

function forwardViewHTML(nodes) {
  const back = fwdTrail.length > 1
    ? '<button type="button" class="btn small ghost fwd-back" data-fwd-back>‹ 返回上一层</button>' : "";
  const rows = (nodes || []).map((n) => `<div class="fwd-node">
      <span class="bub-ava ph">${avatarChar(n.name)}</span>
      <div class="bub-col"><div class="bub-meta"><span class="bub-name">${esc(n.name)}</span></div>
        <div class="bub">${segmentsHTML(n.content)}</div></div>
    </div>`).join("");
  return back + (rows || '<div class="chat-empty small">这份聊天记录是空的</div>');
}

function onForwardViewClick(e) {
  if (e.target.closest("[data-fwd-back]")) {
    const parent = fwdTrail[fwdTrail.length - 2];
    fwdTrail = fwdTrail.slice(0, -2);
    openForward(parent, { nested: fwdTrail.length > 0 });
    return;
  }
  const card = e.target.closest("[data-fwd]");
  if (card) openForward(card.dataset.fwd, { nested: true });
}

// ==================== markdown ====================
// 只渲染会被当 markdown 发出去的(出站 + bot 开了 markdown + 含语法), 与群里所见一致;
// 入站是纯文本, 不渲染. 行内元素先摘成占位符, 骨架整体转义后才做强调, 原始 HTML 进不来.

// 与 sender.py 的 _MD_PATTERNS 同一套探测口径, 改一处两边都要改
const MD_PATTERNS = [
  /^#{1,6}\s+\S/m,               // 标题
  /\*\*[^\s*][^*]*\*\*/,         // 粗体
  /~~[^\s~][^~]*~~/,             // 删除线
  /!\[[^\]]*\]\([^)]+\)/,        // 图片
  /(^|[^!])\[[^\]]+\]\([^)]+\)/, // 链接
  /^\s*[-*+]\s+\S/m,             // 无序列表
  /^\s*\d+\.\s+\S/m,             // 有序列表
  /^\s*>\s+\S/m,                 // 引用
  /^\s*(?:\*\s*){3,}$/m,         // 分割线
  /`[^`\n]+`/,                   // 行内代码
  /^```/m,                       // 代码块
  /^\s*\|.+\|\s*$/m,             // 表格
];

function looksLikeMarkdown(text) {
  return MD_PATTERNS.some((re) => re.test(text));
}

// 块级起始: 段落遇到它就断开
const MD_BLOCK_START = /^\s{0,3}(?:```|#{1,6}\s|[-*+]\s|\d+\.\s|>|\|)/;
const MD_HR = /^\s{0,3}([-*_])\s*(?:\1\s*){2,}$/;
const MD_TABLE_SPLIT = /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/;

function mdUrlOk(url) {
  return /^https?:\/\//i.test(url);      // 只放行 http(s), 挡掉 javascript:
}

function mdLink(url, inner) {
  return `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${inner}</a>`;
}

/** markdown 图片: 和原生图片段同一套外观, 一样能点开灯箱 */
function mdImage(url, alt) {
  // QQ 的 markdown 图片写作 ![说明 #宽px #高px](url), 尺寸标记不是说明文字
  const text = esc(alt.replace(/#\d+px/g, "").trim() || "图片");
  return `<img class="seg-img" src="${esc(url)}" data-zoom="${esc(url)}" alt="${text}"
     loading="lazy" referrerpolicy="no-referrer" decoding="async" data-gone="[图片已失效]">`;
}

/** 强调类: 在**已转义**的串上做, 所以只会产出我们自己的标签 */
function mdEmphasis(s) {
  return s
    .replace(/~~([^~]+)~~/g, "<del>$1</del>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>")
    // 下划线斜体要求两侧是词边界, 否则 snake_case 会被拆成斜体
    .replace(/(^|[\s(（])_([^_\n]+)_(?=[\s)）,.，。!?！？]|$)/g, "$1<em>$2</em>");
}

/** 行内渲染: 代码/图片/链接先摘走(它们内部不该再被当语法), 剩下的才转义+强调 */
function mdInline(raw) {
  const slots = [];
  const hold = (html) => `\u0000${slots.push(html) - 1}\u0000`;
  let s = String(raw).replace(/\u0000/g, "");
  s = s.replace(/`([^`\n]+)`/g,
    (_, code) => hold(`<code class="md-code">${esc(code)}</code>`));
  s = s.replace(/!\[([^\]]*)\]\(\s*([^\s)]+)[^)]*\)/g,
    (whole, alt, url) => (mdUrlOk(url) ? hold(mdImage(url, alt)) : whole));
  s = s.replace(/\[([^\]]+)\]\(\s*([^\s)]+)[^)]*\)/g,
    (whole, label, url) => (mdUrlOk(url)
      ? hold(mdLink(url, mdEmphasis(esc(label)))) : whole));
  s = s.replace(/https?:\/\/[^\s<>"'）】]+/g, (url) => hold(mdLink(url, esc(url))));
  // 占位符是 NUL+数字+NUL, 转义与强调都不会动它
  return mdEmphasis(esc(s)).replace(/\u0000(\d+)\u0000/g, (_, n) => slots[Number(n)]);
}

/** 极简 markdown -> HTML. 支持标题/列表/引用/代码块/表格/分割线/段落 */
function renderMarkdown(src) {
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {                       // 围栏代码块
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      i++;                                            // 吃掉收尾的 ```
      out.push(`<pre class="md-pre"><code>${esc(code.join("\n"))}</code></pre>`);
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = /^\s{0,3}(#{1,6})\s+(.*)$/.exec(line);
    if (heading) {
      // 气泡里 h1 大得离谱, 整体降两级(h3 起), 只当"这行是标题"用
      const level = Math.min(heading[1].length + 2, 6);
      out.push(`<h${level} class="md-h">${mdInline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (MD_HR.test(line)) { out.push('<hr class="md-hr">'); i++; continue; }
    if (/^\s{0,3}>/.test(line)) {                     // 引用(可嵌套)
      const quoted = [];
      while (i < lines.length && /^\s{0,3}>/.test(lines[i])) {
        quoted.push(lines[i++].replace(/^\s{0,3}>\s?/, ""));
      }
      out.push(`<blockquote class="md-quote">${renderMarkdown(quoted.join("\n"))}</blockquote>`);
      continue;
    }
    // 表格: 表头 + |---|---| 分隔行, 缺分隔行就当普通段落
    if (/^\s*\|.*\|\s*$/.test(line) && MD_TABLE_SPLIT.test(lines[i + 1] || "")) {
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|")
        .map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        body.push(cells(lines[i++]));
      }
      out.push(`<table class="md-table"><thead><tr>${
        head.map((c) => `<th>${mdInline(c)}</th>`).join("")}</tr></thead><tbody>${
        body.map((row) => `<tr>${row.map((c) => `<td>${mdInline(c)}</td>`).join("")}</tr>`)
          .join("")}</tbody></table>`);
      continue;
    }
    const ordered = /^\s*\d+\.\s+/.test(line);
    if (ordered || /^\s*[-*+]\s+/.test(line)) {
      const item = ordered ? /^\s*\d+\.\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      const items = [];
      while (i < lines.length) {
        const m = item.exec(lines[i]);
        if (!m) break;
        items.push(`<li>${mdInline(m[1])}</li>`);
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag} class="md-list">${items.join("")}</${tag}>`);
      continue;
    }
    // 段落: 到空行或下一个块级为止; 单换行即断行(sender 会补 markdown 硬换行)
    const para = [];
    while (i < lines.length && lines[i].trim() && !MD_BLOCK_START.test(lines[i])) {
      para.push(lines[i++]);
    }
    if (!para.length) { para.push(lines[i++]); }      // 兜底: 绝不空转
    out.push(`<p class="md-p">${para.map(mdInline).join("<br>")}</p>`);
  }
  return out.join("");
}

/** 文本里的 URL 变可点链接; 其余内容一律转义 */
function linkify(text) {
  const out = [];
  let last = 0;
  const re = /https?:\/\/[^\s<>"']+/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    out.push(esc(text.slice(last, m.index)));
    out.push(`<a href="${esc(m[0])}" target="_blank" rel="noopener noreferrer">${esc(m[0])}</a>`);
    last = m.index + m[0].length;
  }
  out.push(esc(text.slice(last)));
  return out.join("");
}

// ---------- 拖入 / 粘贴 ----------

/** 拖动里带的是不是文件(拖选中的文字也会触发 drag 事件, 别把它当附件) */
function dragHasFiles(e) {
  return [...(e.dataTransfer?.types || [])].includes("Files");
}

/** 把整个消息区做成拖放区. 只能绑一次: #chat-main 切会话时不重建, 重复绑会上传多次 */
function bindDropZone(zone) {
  if (!zone || zone.dataset.dropBound) return;
  zone.dataset.dropBound = "1";
  // dragenter/dragleave 在子元素边界反复触发, 用计数防提示层闪烁
  let depth = 0;
  const paint = (on) => zone.classList.toggle("drop-hot", on);
  zone.addEventListener("dragenter", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth += 1;
    paint(true);
  });
  zone.addEventListener("dragover", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();                       // 不拦就不会触发 drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  });
  zone.addEventListener("dragleave", (e) => {
    if (!dragHasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) paint(false);
  });
  zone.addEventListener("drop", (e) => {
    if (!dragHasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    paint(false);
    const files = [...(e.dataTransfer?.files || [])];
    if (files.length) uploadFiles(files);
  });
}

/** 粘贴上传. 挂在 document 上, 焦点不在输入框也能贴图; 只在剪贴板有文件时接管 */
function onDocumentPaste(e) {
  if (!chatState.peerId || !document.querySelector("#chat-form")) return;
  const files = [...(e.clipboardData?.files || [])];
  if (!files.length) return;
  e.preventDefault();
  uploadFiles(files);
}

/** 文件掉在聊天区之外时, 浏览器默认会直接打开它(整页被替换掉) */
function swallowStrayDrop(e) {
  if (dragHasFiles(e)) e.preventDefault();
}

// ---------- 发送 ----------

function onPickFiles(e) {
  const files = [...(e.target.files || [])];
  e.target.value = "";
  if (files.length) uploadFiles(files);
}

// 本地媒体 media://token 不对外, 经登录后的 /api/media 看
function mediaSrc(raw) {
  const url = String(raw || "");
  if (url.startsWith("http")) return url;
  const m = /^media:\/\/([^/]+)/.exec(url);
  return m ? `${BASE}/api/media/${encodeURIComponent(m[1])}` : "";
}

async function uploadFiles(files) {
  const box = $("#chat-attach");
  if (!box) return;
  for (const file of files) {
    const slot = { name: file.name, kind: kindOfFile(file), url: "", uploading: true };
    chatState.pending.push(slot);
    renderAttachments();
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await api(`${BASE}/api/chat/upload`, { method: "POST", body: form });
      slot.url = res.url;
      slot.uploading = false;
    } catch (err) {
      chatState.pending = chatState.pending.filter((x) => x !== slot);
      toast(`上传失败: ${err.message}`, "err");
    }
    renderAttachments();
  }
}

function kindOfFile(file) {
  const type = file.type || "";
  if (type.startsWith("image/")) return "image";
  if (type.startsWith("video/")) return "video";
  if (type.startsWith("audio/")) return "record";
  return "file";
}

function renderAttachments() {
  const box = $("#chat-attach");
  if (!box) return;
  box.classList.toggle("hidden", !chatState.pending.length);
  box.innerHTML = chatState.pending.map((a, i) => `
    <span class="attach-chip${a.uploading ? " up" : ""}">
      ${a.kind === "image" && a.url
        ? `<img src="${esc(mediaSrc(a.url))}" alt="">`
        : `<span class="attach-ico">${a.kind === "video" ? "视频" : a.kind === "record" ? "音频" : "文件"}</span>`}
      <span class="attach-name">${esc(a.name)}</span>
      ${a.uploading ? '<span class="spinner"></span>'
        : `<button type="button" class="icon-btn" data-drop="${i}" title="移除">✕</button>`}
    </span>`).join("");
  box.querySelectorAll("[data-drop]").forEach((btn) =>
    btn.addEventListener("click", () => {
      chatState.pending.splice(Number(btn.dataset.drop), 1);
      renderAttachments();
    }));
}

async function onChatSend(e) {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value.trim();
  const ready = chatState.pending.filter((a) => a.url && !a.uploading);
  if (chatState.pending.some((a) => a.uploading)) {
    toast("还有附件在上传中", "warn");
    return;
  }
  if (!text && !ready.length) return;

  const message = [];
  // reply 段必须排在最前: sender 只认 message[0] 之后的 prefer_reply_mid
  if (chatState.replyTo) {
    message.push({ type: "reply", data: { id: String(chatState.replyTo.mid) } });
  }
  if (text) message.push({ type: "text", data: { text } });
  for (const a of ready) {
    message.push({ type: a.kind, data: { file: a.url, url: a.url, name: a.name } });
  }
  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  try {
    await api(`${BASE}/api/chat/send`, {
      method: "POST",
      body: {
        appid: chatState.appid, chat_type: chatState.chatType,
        peer_id: chatState.peerId, message,
      },
    });
    input.value = "";
    autoGrowInput(input);            // 收回单行高度, 顺便把消息区贴回底部
    chatState.pending = [];
    renderAttachments();
    chatState.replyTo = null;
    renderReplyTo();
    await pollNewMessages();     // 立刻把自己发的这条拉进来
    const log = $("#chat-log");
    if (log) log.scrollTop = log.scrollHeight;
  } catch (err) {
    if (err.status !== 401) toast(`发送失败: ${err.message}`, "err", 6000);
  } finally {
    btn.disabled = false;
  }
}
