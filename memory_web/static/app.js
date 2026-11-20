import {coreMemoryPage, memosPage} from "./memos.js?v=40";
import {importsPage} from "./imports.js?v=76";
import {readPersonaFile} from "./persona-file.js?v=1";

const basePath = new URL(import.meta.url).pathname.replace(
  /\/static\/app\.js$/,
  "",
);
const appPath = (path) => basePath + path;
const pagePath = () => location.pathname.slice(basePath.length) || "/";
const root = document.querySelector("#app");
// Phones: hide the fixed bottom nav while the keyboard is up. Otherwise it stays
// pinned to the page frame and floats over the content (iOS) or leaves a gap (Android).
const TYPING_FIELD = 'input:not([type="checkbox"]):not([type="radio"]):not([type="date"]):not([type="file"]):not([type="hidden"]):not([readonly]), textarea:not([readonly])';
const isTypingField = (el) => !!el?.matches?.(TYPING_FIELD);
document.addEventListener("focusin", (event) => {
  if (isTypingField(event.target)) document.documentElement.classList.add("typing");
});
document.addEventListener("focusout", () => {
  // Moving straight to the next field keeps the nav hidden.
  setTimeout(() => {
    if (!isTypingField(document.activeElement)) document.documentElement.classList.remove("typing");
  }, 0);
});
// Android's back key closes the keyboard but leaves the field focused; watch the
// visible area so the nav returns when the keyboard goes, and hides if it comes back.
let keyboardSeen = false;
window.visualViewport?.addEventListener("resize", () => {
  const open = window.innerHeight - window.visualViewport.height > 120;
  if (open) {
    keyboardSeen = true;
    if (isTypingField(document.activeElement)) document.documentElement.classList.add("typing");
  } else if (keyboardSeen) {
    keyboardSeen = false;
    document.documentElement.classList.remove("typing");
  }
});
const labels = {
  external: "现实经历",
  shared: "共同赛博经历",
  dream: "睡梦",
  imagined: "想象 / 故事",
  stable: "稳定",
  disputed: "存疑",
  tier_0_exempt: "豁免层",
  tier_1_rule: "规则",
  tier_2_reflection: "反思",
  tier_3_semantic: "长期认识",
  tier_4_episode: "具体经历",
  tier_5_emotional: "情绪",
  pending: "待审核",
  structure_pending: "整理中",
  structure_failed: "整理失败",
  approved: "已收录",
  rejected: "已拒绝",
  atomic: "记忆卡片",
  topic_summary: "主题总结",
};
let state = {
  space: "",
  spaces: [],
  contract: null,
  csrf: "",
  page: "overview",
  pending: 0,
  memoryTopic: null,
  dirty: false,
  busy: false,
  epoch: 0,
  filter: "pending",
  fixed: false,
};
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const $ = (selector, parent = document) => parent.querySelector(selector);
const $$ = (selector, parent = document) => [
  ...parent.querySelectorAll(selector),
];
const encodedSpace = () => encodeURIComponent(state.space);
const url = (suffix) => `/api/spaces/${encodedSpace()}${suffix}`;
const NAV_PAGES = ["overview", "review", "memories", "conversations", "settings"];
const OVERVIEW_PAGES = ["diaries", "summaries", "core-memory", "bookmarks"];
const PAGES = [...NAV_PAGES, ...OVERVIEW_PAGES, "imports"];
const pageCache = new Map();
let pageDataRevision = 0;
const configurationPages = ["settings", "core-memory", "overview", "memories", "review", "imports"];
const reviewPages = ["review", "overview", "memories"];
const affectedPages = {
  configuration: configurationPages,
  participants: configurationPages,
  persona: configurationPages,
  "auto-approval": configurationPages,
  "period-notes": [...configurationPages, "bookmarks"],
  "core-memory": configurationPages,
  drafts: reviewPages,
  "topic-summaries": reviewPages,
  cards: reviewPages,
  diaries: ["diaries", "overview"],
  "recent-notes": ["bookmarks", "overview"],
  bookmarks: ["bookmarks", "overview"],
  imports: ["imports", ...reviewPages],
};
function clearPageCache() {
  pageCache.clear();
  pageDataRevision++;
}
function invalidatePageData(path, updated) {
  if (!state.space || !path.startsWith(url("/"))) return;
  const resource = path.slice(url("/").length).split(/[/?]/, 1)[0];
  const pages = affectedPages[resource];
  if (!pages) return;
  pageDataRevision++;
  for (const [key, entry] of pageCache) {
    if (pages.includes(entry.page)) pageCache.delete(key);
  }
  // Configuration writes return the same full document as the page read.
  if (updated && pages === configurationPages) {
    const data = new Map([[url("/configuration"), JSON.stringify(updated)]]);
    for (const page of ["settings", "core-memory"]) pageCache.set(page, {page, variant: null, data});
  }
}
const LEGACY = {
  drafts: "review",
  cards: "memories",
  memos: "bookmarks",
  participants: "settings",
  topics: "settings",
};
const title = (page) =>
  ({
    overview: "概览",
    review: "待审核",
    memories: "记忆",
    conversations: "召回",
    imports: "导入",
    settings: "设置",
  })[page];
function pageFromPath() {
  const page = pagePath().slice(1).split("/")[0];
  if (page === "memories") {
    const tab = new URLSearchParams(location.search).get("tab");
    if (tab === "diaries" || tab === "conversations") return tab;
  }
  return LEGACY[page] || (PAGES.includes(page) ? page : "overview");
}
function pageURL(page) {
  return appPath("/" + page);
}
function message(text) {
  $("#notice").textContent = text;
  clearTimeout(message.timer);
  message.timer = setTimeout(() => ($("#notice").textContent = ""), 4500);
}
async function api(path, { method = "GET", body, file, notifyAuth = () => true } = {}) {
  if (method !== "GET") invalidatePageData(path);
  let response;
  try {
    response = await fetch(appPath(path), {
      method,
      credentials: "same-origin",
      headers:
        body === undefined && file === undefined
          ? {}
          : { "Content-Type": file ? "application/octet-stream" : "application/json", "X-CSRF-Token": state.csrf },
      body: file || (body === undefined ? undefined : JSON.stringify(body)),
    });
  } catch {
    const error = new Error("网络连接中断。编辑内容仍保留，请稍后重试。");
    error.network = true;
    throw error;
  }
  let data;
  try {
    data = await response.json();
  } catch {
    // The reply was cut off in transit, or a proxy answered with a non-JSON page.
    const error = new Error(
      response.ok
        ? "网络连接中断。编辑内容仍保留，请稍后重试。"
        : `服务器暂时没有正常响应（${response.status}），请稍后重试。`,
    );
    error.network = true;
    error.status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(data.detail || "请求未完成");
    error.status = response.status;
    if (response.status === 401 && path !== "/api/login" && notifyAuth()) {
      clearPageCache();
      message("登录已失效，编辑内容暂留在当前页。重新登录后再操作。");
      showRelogin();
    }
    throw error;
  }
  if (method !== "GET") invalidatePageData(path, data);
  return data;
}
function showRelogin() {
  if ($("#relogin")) return;
  const dialog = document.createElement("dialog");
  dialog.id = "relogin";
  dialog.innerHTML =
    '<form><h2>重新登录</h2><p class="muted">登录后继续当前编辑。</p><label>管理员密码<input name="password" type="password" autocomplete="current-password" required></label><p class="error" role="alert"></p><button class="primary">登录</button><button type="button" class="cancel">稍后</button></form>';
  root.append(dialog);
  dialog.showModal();
  $(".cancel", dialog).onclick = () => dialog.remove();
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    const button = $(".primary", dialog);
    button.disabled = true;
    try {
      const session = await api("/api/login", {
        method: "POST",
        body: { password: $("input", dialog).value, remember: false },
      });
      state.csrf = session.csrf;
      dialog.remove();
      message("已登录，可以继续操作");
    } catch (error) {
      $(".error", dialog).textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
}
const pendingSaves = new Set();
const saveStatusHTML = () => `<div class="autosave-status"><span class="meta" role="status"></span><button type="button" class="quiet retry-save" hidden>重试</button></div>`;

function autoSave(status, read, save, {fadeSaved = false} = {}) {
  let saved = JSON.stringify(read());
  let timer, running, failure;
  let paused = false;
  let composing = false, disposed = false;
  const label = $("[role=status]", status);
  const retry = $(".retry-save", status);
  const needsSave = () => !!failure || JSON.stringify(read()) !== saved;
  const show = (text, error = false, success = false) => {
    label.textContent = text;
    label.classList.toggle("error", error);
    if (fadeSaved) label.classList.toggle("fade-saved", success);
    retry.hidden = !error;
  };
  if (fadeSaved) label.addEventListener("animationend", () => {
    if (label.classList.contains("fade-saved")) show("");
  });
  const saver = {
    changed(immediate = false, inComposition = false) {
      if (disposed) return;
      composing = inComposition;
      clearTimeout(timer);
      if (!running && !needsSave()) {
        if (pendingSaves.delete(saver)) show("");
        return;
      }
      pendingSaves.add(saver);
      if (composing) return;
      paused = false;
      show("保存中…");
      if (immediate) saver.flush();
      else if (!running) timer = setTimeout(saver.flush, 1000);
    },
    flush(force = false) {
      clearTimeout(timer);
      if (disposed) return Promise.resolve(true);
      composing = false;
      if (running) return running;
      if (paused) return Promise.resolve(false);
      if (!force && !needsSave()) return Promise.resolve(true);
      pendingSaves.add(saver);
      show("保存中…");
      // Each response acknowledges only the values sent with that request.
      running = Promise.resolve().then(async () => {
        while (!disposed) {
          const payload = read();
          const sent = JSON.stringify(payload);
          try {
            await save(payload);
            saved = sent;
            failure = null;
          } catch (error) {
            failure = error;
            // A newer value typed during this request stays queued until the next edit or retry.
            paused = true;
            break;
          }
          if (composing || sent === JSON.stringify(read())) break;
        }
        return !failure;
      }).finally(() => {
        running = null;
        if (disposed || (composing && !failure)) return;
        if (failure) {
          const reason = failure.status === 409
            ? `${failure.message}。当前编辑已保留，请核对最新版本。`
            : failure.message;
          show(`保存失败（${reason}）`, true);
        } else if (needsSave()) {
          saver.changed();
        } else {
          pendingSaves.delete(saver);
          show("已保存", false, true);
        }
      });
      return running;
    },
    reset() {
      clearTimeout(timer);
      pendingSaves.delete(saver);
      saved = JSON.stringify(read());
      failure = null;
      paused = false;
      composing = false;
      show("");
    },
    dispose() {
      disposed = true;
      clearTimeout(timer);
      pendingSaves.delete(saver);
    },
  };
  retry.onclick = () => {
    paused = false;
    saver.flush();
  };
  return saver;
}

function wireAutoSave(form, saver) {
  form.addEventListener("input", (event) => {
    if (isTypingField(event.target)) saver.changed(false, event.isComposing);
  });
  form.addEventListener("compositionend", () => saver.changed());
  form.addEventListener("change", (event) => {
    if (!isTypingField(event.target)) saver.changed(true);
  });
  form.addEventListener("focusout", (event) => {
    if (isTypingField(event.target)) saver.flush();
  });
}

let leaving = false;
async function allowLeave() {
  if (leaving) return false;
  if (state.busy) {
    message("当前操作还未完成，请稍候");
    return false;
  }
  if (pendingSaves.size) {
    const epoch = state.epoch;
    leaving = true;
    try {
      while (pendingSaves.size) {
        const results = await Promise.all([...pendingSaves].map(saver => saver.flush()));
        if (results.includes(false)) break;
      }
      if (state.busy || epoch !== state.epoch) return false;
    } finally {
      leaving = false;
    }
  }
  return (
    (!state.dirty && !pendingSaves.size) || window.confirm("有尚未保存的修改。离开此页并丢弃修改？")
  );
}
window.addEventListener("beforeunload", (event) => {
  if (state.dirty || pendingSaves.size || state.busy) {
    pendingSaves.forEach(saver => saver.flush());
    event.preventDefault();
    event.returnValue = "";
  }
});
function options(values, value, blank = false) {
  return (
    (blank ? '<option value="">未设置</option>' : "") +
    values
      .map(
        (item) =>
          `<option value="${esc(item)}" ${item === value ? "selected" : ""}>${esc(labels[item] || item)}</option>`,
      )
      .join("")
  );
}
function field(label, name, value, type = "text", attributes = "") {
  return `<label>${esc(label)}<input name="${name}" type="${type}" value="${esc(value)}" ${attributes}></label>`;
}
// Native date inputs show the phone's own format (e.g. 2026/11/5) in grey; we hide that
// text and overlay a formal Chinese date in the normal ink color instead.
const chineseDate = (value) => {
  const [year, month, day] = value.split("-").map(Number);
  return `${year}年${month}月${day}日`;
};
function dateField(label, name, value, hint) {
  return `<label class="date-field"><span class="date-control ${value ? "has-value" : ""}"><input name="${name}" type="date" value="${esc(value)}" aria-label="${esc(label)}"><span class="date-display" aria-hidden="true" data-hint="${esc(hint)}">${esc(value ? chineseDate(value) : hint)}</span></span></label>`;
}
function fitTextarea(textarea) {
  textarea.style.height = "auto";
  const borders = textarea.offsetHeight - textarea.clientHeight;
  textarea.style.height = textarea.scrollHeight + borders + "px";
}
// Reflow editable text when the viewport changes (rotation or a narrower window).
window.addEventListener("resize", () => {
  cancelAnimationFrame(fitTextarea.frame);
  fitTextarea.frame = requestAnimationFrame(() => {
    $$(".readfield textarea, .topic-edit textarea, .memo-line-editor textarea, #core-memory").forEach(fitTextarea);
  });
});
function wireDates(parent) {
  $$(".date-control input", parent).forEach((input) => {
    const sync = () => {
      const display = input.nextElementSibling;
      input.parentElement.classList.toggle("has-value", !!input.value);
      display.textContent = input.value ? chineseDate(input.value) : display.dataset.hint;
    };
    input.addEventListener("input", sync);
    input.addEventListener("change", sync);
  });
}
function errorAt(element, error) {
  element.textContent =
    error.message +
    (error.status === 409 ? "。当前编辑已保留；请核对最新版本后再保存。" : "");
}
function track(form) {
  form.addEventListener("input", () => {
    state.dirty = true;
  });
  form.addEventListener("change", () => {
    state.dirty = true;
  });
}
const disabledButtons = new WeakMap();
function busy(form, value, trackPage = true) {
  if (trackPage) state.busy = value;
  form.setAttribute("aria-busy", String(value));
  // Inert prevents edits during a pending save without excluding controls from
  // FormData. A late response must never mark newer, unsaved typing as saved.
  form.inert = value;
  $$("button", form).forEach((button) => {
    if (value) {
      disabledButtons.set(button, button.disabled);
      button.disabled = true;
    } else {
      button.disabled = disabledButtons.get(button) ?? button.disabled;
      disabledButtons.delete(button);
    }
  });
}
// Small local SVGs share one stroke and need no image or font requests.
function icon(name) {
  const paths = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    review: '<rect x="5" y="3" width="14" height="18" rx="3"/><path d="m9 12 2 2 4-4M9 7h6M9 18h6"/>',
    memories: '<rect x="5" y="6" width="14" height="15" rx="3"/><path d="M8 3h8M9 11h6M9 15h4"/>',
    conversations: '<path d="M5 4h14a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-8l-5 4v-4H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    logout: '<path d="M10 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h5M13 8l4 4-4 4M8 12h12"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
    filter: '<path d="M4 7h16M7 12h10M10 17h4"/>',
  };
  return `<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.memories}</svg>`;
}
function navLabel(page) {
  return `${icon(page)}<span>${esc(title(page))}</span>${page === "review" ? badgeHTML() : ""}`;
}
function badgeHTML() {
  if (!state.pending) return "";
  return `<span class="badge" role="img" aria-label="${state.pending} 张待审核" title="${state.pending} 张待审核"></span>`;
}
function renderNavigation() {
  const nav = $(".topbar nav");
  if (!nav) return;
  const page = !state.space || state.page === "imports" ? "settings"
    : OVERVIEW_PAGES.includes(state.page) ? "overview" : state.page;
  nav.innerHTML = NAV_PAGES.map(item => `<a href="${appPath("/" + item)}" data-page="${item}" class="${page === item ? "active" : ""}"${page === item ? ' aria-current="page"' : ""}>${navLabel(item)}</a>`).join("");
  $$("[data-page]", nav).forEach(a => a.onclick = async event => {
    event.preventDefault();
    if (await allowLeave()) navigate(a.dataset.page);
  });
}
let badgeRevision = 0;
function updateBadge(count) {
  badgeRevision++;
  const pending = Number(count) || 0;
  if (pending === state.pending) return;
  state.pending = pending;
  const link = $('.topbar nav [data-page="review"]');
  if (!link) return;
  $(".badge", link)?.remove();
  link.insertAdjacentHTML("beforeend", badgeHTML());
}
async function refreshBadge(shellElement) {
  const revision = badgeRevision;
  const dataRevision = pageDataRevision;
  try {
    const data = await api(url("/drafts?status=pending&size=1"), {notifyAuth: () => false});
    if (shellElement.isConnected && revision === badgeRevision && dataRevision === pageDataRevision) {
      updateBadge(data.total);
    }
  } catch {
    // Pages remain readable when this supplementary refresh fails.
  }
}
const PAGE_INFO = {
  overview: ["概览", "OUR LITTLE MOMENTS"],
  review: ["记忆审核", "A LITTLE SPACE FOR MEMORIES"],
  memories: ["记忆", "COLLECTION"],
  imports: ["导入", "BRING YOUR MEMORIES"],
  conversations: ["聊天与召回", "CONVERSATIONS"],
  diaries: ["日记", "DIARY"],
  summaries: ["及时摘要", "SUMMARIES"],
  "core-memory": ["核心记忆", "CORE MEMORY"],
  bookmarks: ["记忆书签", "BOOKMARKS"],
  settings: ["设置", "MAKE IT YOURS"],
};
function brandHTML() {
  return '<span class="brand-mark"><svg viewBox="0 0 40 40" fill="none" aria-hidden="true"><path d="M20 2.4C30.6 2.4 36.6 11 36.6 23.6v8.2q0 5.8-6.6 5.8H16.2C6.6 37.6 2.4 32.6 2.4 23 2.4 12.2 8.4 2.4 20 2.4Z" fill="var(--card)" stroke="var(--muted)" stroke-width=".6"/><path d="M11.5 27.4q8.4-1 14.6-12.4" stroke="#b07a7f" stroke-width=".7" stroke-linecap="round"/><path d="M15 23.4q-2.6-6.4-5-7.8M17.2 21.2q-6.6-1.8-7.2-5.2" stroke="#b07a7f" stroke-width=".55" stroke-linecap="round"/><path d="M12.6 19.6q-2.6-.6-3.2-3.2q2.6.4 3.2 3.2Z" fill="#dcaeb0"/><path d="M13.4 18.3q-.4-2.4 1.2-4q.8 2.4-1.2 4Z" fill="#e6c4c5"/><path d="M14 26.6q-1.8 1.8-4.2 1.4q1.6-2 4.2-1.4Z" fill="#e6c4c5"/><circle cx="9.7" cy="15.4" r="1.15" fill="#d99ea3"/><path d="M24 24C17.5 20 15.8 15.6 18.2 13c2.1-2.2 4.8-1.2 5.8 1 1-2.2 3.7-3.2 5.8-1 2.4 2.6.7 7-5.8 11Z" fill="var(--pink)"/><path d="M24 24c4-2.6 7-5.6 6.6-8.8-1.8 3.2-4.2 5.4-6.6 6.6Z" fill="#a9636b"/><path d="M19 14.6q.6-1.6 2.2-1.6" stroke="#fff" stroke-opacity=".75" stroke-width=".6" stroke-linecap="round"/></svg></span><span class="brand-text"><span class="brand-name">Amber Heart</span><span class="brand-sub">琥珀之心 · 记忆管理</span></span>';
}
function shell() {
  const page = state.space ? state.page : "settings";
  const [heading, eyebrow] = PAGE_INFO[page];
  document.title = `${heading} · Amber Heart`;
  const back = OVERVIEW_PAGES.includes(page)
    ? `<a class="back-arrow" href="${pageURL("overview")}" data-go="overview" aria-label="返回概览">‹</a>`
    : "";
  root.innerHTML = `<div class="shell" data-current-page="${page}"><header class="topbar"><a class="brand" href="${appPath("/overview")}">${brandHTML()}</a><nav aria-label="功能页"></nav></header><header class="pagehead"><p class="eyebrow">${esc(eyebrow)}</p><div class="heading-row">${back}<h1 id="page-title">${esc(heading)}</h1></div></header><main id="main" aria-labelledby="page-title"></main><footer>Amber Heart · 琥珀之心${state.fixed ? " · 当前使用固定假响应，仅供合成材料验收" : ""}</footer></div>`;
  renderNavigation();
  wireGo($(".pagehead"));
  $(".brand").onclick = async (event) => {
    event.preventDefault();
    if (await allowLeave()) navigate("overview");
  };
}

function logoutHTML() {
  return `<div class="settings-session"><button type="button" id="logout" class="quiet">${icon("logout")}退出登录</button></div>`;
}
function wireLogout() {
  $("#logout").onclick = async () => {
    if (!await allowLeave()) return;
    try {
      await api("/api/logout", { method: "POST", body: {} });
      clearPageCache();
      state.dirty = false;
      pendingSaves.forEach(saver => saver.dispose());
      location.assign(appPath("/login"));
    } catch (error) {
      message(error.message);
    }
  };
}
function wireSpaceSelector() {
  const control = $("#space");
  if (!control) return;
  control.onchange = async (event) => {
    const space = event.target.value;
    if (!await allowLeave()) {
      event.target.value = state.space;
      return;
    }
    clearPageCache();
    updateBadge(0);
    state.space = space;
    sessionStorage.setItem("memory-space", state.space);
    state.filter = "pending";
    await navigate(state.page);
  };
}
async function navigate(page, push = true) {
  pendingSaves.forEach(saver => saver.dispose());
  state.page = page;
  state.dirty = false;
  state.epoch++;
  const epoch = state.epoch;
  if (push) history.pushState({}, "", pageURL(page));
  shell();
  let main = $("#main");
  if (state.space && page !== "overview") void refreshBadge(main.parentElement);
  const view = {filter: state.filter, topic: state.memoryTopic, section: state.settingsSection};
  state.memoryTopic = state.settingsSection = null;
  const variant = page === "review" ? view.filter : page === "memories" ? view.topic : null;
  const key = page === "review" ? `${page}:${variant}` : variant ? "memories:topic" : page;
  const entry = pageCache.get(key);
  const snapshot = entry?.variant === variant ? entry.data : null;
  if (!snapshot) main.innerHTML = '<p class="loading" role="status">正在读取…</p>';
  let interacted = false;
  const keepView = () => { interacted = true; };
  if (snapshot) {
    for (const event of ["pointerdown", "click", "keydown", "input", "change", "submit"]) {
      main.addEventListener(event, keepView, {capture: true, once: true});
    }
  }
  const revision = pageDataRevision;
  const current = () => epoch === state.epoch && revision === pageDataRevision;
  const results = new Map(), requests = new Map();
  // Keep serialized copies so a form can update its working data without changing the cache.
  const fresh = (path) => {
    if (!requests.has(path)) {
      requests.set(path, api(path, {notifyAuth: current}).then(data => {
        const value = JSON.stringify(data);
        results.set(path, value);
        return value;
      }));
    }
    return requests.get(path);
  };
  const from = (data) => async (path) => JSON.parse(data?.get(path) ?? await fresh(path));
  let renderEpoch = epoch;
  try {
    if (!state.space) {
      await settingsPage(main, true, epoch);
      return;
    }
    const refresh = snapshot && refreshPageData(page, view, snapshot, fresh, results, current).catch(() => false);
    await renderPage(main, page, epoch, view, from(snapshot), !snapshot, keepView);
    if (snapshot && !await refresh) return;
    if (!current()) return;
    if (results.size) pageCache.set(key, {page, variant, data: results});
    if (!snapshot || (snapshot.size === results.size && [...results].every(([path, value]) => snapshot.get(path) === value))) return;
    if (interacted || leaving || state.busy || state.dirty || pendingSaves.size || main.contains(document.activeElement)) return;
    const scroll = window.scrollY;
    const replacement = main.cloneNode(false);
    main.replaceWith(replacement);
    main = replacement;
    renderEpoch = ++state.epoch;
    await renderPage(main, page, renderEpoch, view, from(results), true);
    if (renderEpoch === state.epoch) window.scrollTo(0, scroll);
  } catch (error) {
    if (renderEpoch === state.epoch) {
      main.innerHTML = `<p class="error empty" role="alert">${esc(error.message)}</p><button id="retry">重新读取</button>`;
      $("#retry").onclick = () => navigate(page, false);
    }
  }
}
async function refreshPageData(page, view, snapshot, fresh, results, current) {
  if (page !== "review") {
    await Promise.all([...snapshot.keys()].map(fresh));
    return true;
  }
  const list = JSON.parse(await fresh(reviewPath(view.filter)));
  // The refreshed list determines which detail requests are still relevant.
  for (let start = 0; start < list.items.length; start += 4) {
    if (!current()) return false;
    await Promise.all(list.items.slice(start, start + 4).map(async item => {
      const path = reviewDetailPath(item);
      try { await fresh(path); }
      catch { if (snapshot.has(path)) results.set(path, snapshot.get(path)); }
    }));
  }
  return true;
}
function renderPage(main, page, epoch, view, read, fresh, keepView = () => {}) {
  const deps = {api, read, keepView, url, esc, state, epoch, message, busy, fitTextarea, autoSave, wireAutoSave, saveStatusHTML};
  switch (page) {
    case "overview": return overviewPage(main, epoch, read, fresh);
    case "review": return draftsPage(main, epoch, view.filter, read, fresh);
    case "memories": return memoryCardsPage(main, epoch, view, read, fresh);
    case "conversations": return conversationsPage(main, epoch, read);
    case "diaries": return diariesPage(main, epoch, read);
    case "summaries": return summariesPage(main, epoch, read);
    case "imports": return importsPage(main, deps);
    case "bookmarks": return memosPage(main, deps);
    case "core-memory": return coreMemoryPage(main, deps);
    case "settings": return settingsPage(main, false, epoch, view.section, read);
  }
}
window.addEventListener("popstate", async () => {
  const page = pageFromPath();
  if (await allowLeave()) {
    navigate(page, false);
  } else if (!leaving) {
    history.pushState({}, "", pageURL(state.page));
  }
});
function pager(data, change) {
  return `<div class="pagination"><button class="previous" ${data.page <= 1 ? "disabled" : ""}>上一页</button><span class="meta">${data.total} 条 · ${data.page} / ${Math.max(1, Math.ceil(data.total / data.size))}</span><button class="next" ${data.page * data.size >= data.total ? "disabled" : ""}>下一页</button></div>`;
}
function wirePager(parent, data, change) {
  $(".previous", parent).onclick = () => change(data.page - 1);
  $(".next", parent).onclick = () => change(data.page + 1);
}
function wireGo(parent) {
  $$("[data-go]", parent).forEach(
    (element) =>
      (element.onclick = async (event) => {
        event.preventDefault();
        if (!(await allowLeave())) return;
        // e.g. the empty memory list links straight to the import section of settings.
        if (element.dataset.section) state.settingsSection = element.dataset.section;
        navigate(element.dataset.go);
      }),
  );
}
async function overviewPage(main, epoch, read, fresh) {
  const data = await read(url("/overview"));
  if (epoch !== state.epoch) return;
  if (fresh) updateBadge(data.pending);
  const pair = `${esc(data.user)}和${esc(data.assistant)}`;
  const intro = data.days_together
    ? `${pair}，已经相伴`
    : `${pair}，记忆已经收藏了`;
  const days = data.days_together || data.days_collected;
  const sub = data.days_together
    ? ""
    : data.acquaintance_date
      ? `相识日是 ${esc(data.acquaintance_date)}，到那天开始计算相伴天数。`
      : `从 ${esc(data.created_date)} 开始收藏。<a href="${appPath("/settings")}" data-go="settings">填写相识日</a>，这里会改为显示你们相伴的天数。`;
  const used = data.topics.filter((topic) => topic.cards > 0);
  const largest = Math.max(1, ...used.map((topic) => topic.cards));
  const idle = data.topics.length - used.length;
  const status = data.overview_entries || {};
  const entryStatus = {
    diaries: status.diary_date && `最新一篇 ${status.diary_date}`,
    summaries: status.summary_date && `最新一份 ${status.summary_date}`,
    "core-memory": status.core_chars == null ? "" : `${status.core_chars} 字`,
    bookmarks: status.new_bookmarks == null ? "" : `今天新记 ${status.new_bookmarks} 条`,
  };
  const entries = OVERVIEW_PAGES.map(page => `<a class="card overview-entry" href="${pageURL(page)}" data-go="${page}"><strong>${PAGE_INFO[page][0]}</strong>${entryStatus[page] ? `<span class="meta">${esc(entryStatus[page])}</span>` : ""}</a>`).join("");
  main.innerHTML = `<section class="hero card"><div class="hero-days"><p class="hero-intro">${intro}</p><h2 class="headline"><strong>${days}</strong><span>天</span></h2></div>${sub ? `<p class="muted">${sub}</p>` : ""}<div class="stats"><span>记忆总数 <strong>${data.cards}</strong></span><span>本月新增 <strong>${data.cards_this_month}</strong></span><span>涉及主题 <strong>${used.length}</strong></span></div></section>${
    `<section class="panel today-panel" aria-label="待办"><div class="today card">${data.pending
      ? `<div class="callout"><span>有 <strong>${data.pending}</strong> 张新记忆等你审核</span><button type="button" class="primary" data-go="review">去审核 →</button></div>`
      : '<p class="callout calm">暂时没有要做的事。每天整理出的新记忆，会在这里提醒你审核。</p>'
    }<details class="today-status"><summary>后台整理状态</summary></details></div></section>`
  }<nav class="overview-entries" aria-label="日记、摘要与记忆">${entries}</nav><section class="panel" aria-labelledby="topic-panel"><div class="panelhead"><h2 id="topic-panel">主题分布</h2><span class="meta">点一下，看这个主题的记忆</span></div><div class="card bars-card">${
    used.length
      ? `<ul class="bars">${used.map((topic) => `<li><button type="button" class="barrow" data-topic="${topic.id}" data-ratio="${topic.cards / largest}"><span class="barname">${esc(topic.name)}</span><span class="bartrack" aria-hidden="true"><span class="barfill"></span></span><span class="barcount">${topic.cards}</span></button></li>`).join("")}</ul>`
      : '<p class="empty">还没有收录的记忆。审核通过的草稿会出现在这里。</p>'
  }${idle && used.length ? `<p class="meta idle">另有 ${idle} 个主题暂时没有记忆</p>` : ""}</div></section>`;
  const approval = data.auto_approval;
  if (data.pending && approval) {
    let schedule = "";
    if (!approval.enabled) schedule = "当前仅手动收录。";
    else if (approval.last_error_code) schedule = "自动入库暂未完成，会重试；草稿仍可手动审核。";
    else if (!approval.ready_count) schedule = approval.manual_count ? "" : "草稿还在整理，完整进入待审核后开始计时。";
    else if (approval.overdue) schedule = "有草稿已到期，等待每日整理收录。";
    else if (approval.next_run_at) schedule = `下一批预计 <time datetime="${esc(approval.next_run_at)}">${esc(approval.next_run_at.slice(5,16).replace("T"," "))}</time> 自动入库。`;
    if (approval.enabled && approval.manual_count) schedule += `${schedule ? " " : ""}有 ${approval.manual_count} 张生成后设置或原对话有改动，需要你手动确认。`;
    $(".callout", main).insertAdjacentHTML("afterend", `<p class="approval-schedule muted">${schedule}</p>`);
  }
  const health = data.archive_health;
  if (health?.paused) {
    const detail = health.reason === "insufficient_samples"
      ? `最近 ${health.window_days} 天有 ${health.injected_rounds} 轮带记忆的对话，至少需要 ${health.minimum_samples} 轮。`
      : `最近 ${health.window_days} 天回执率为 ${Math.floor(health.rate * 1000) / 10}%，低于 ${Number((health.minimum_rate * 100).toFixed(2))}%。`;
    $(".today-status", main).insertAdjacentHTML("beforeend", `<p class="archive-guard muted">自动归档已暂停：${detail}卡片会继续保留。</p>`);
  }
  const extraction = data.daily_extraction;
  if (extraction) {
    const status = {unconfigured: "未配置", waiting: "等待每日整理", running: "正在整理", failed: "整理暂未完成，会自动重试", skipped: "当天没有可整理的对话", done: "已整理"}[extraction.status] || "等待每日整理";
    const added = extraction.added_count ? `，最近一次新增 <strong>${extraction.added_count}</strong> 张卡片` : "";
    const pending = extraction.pending_count ? `，${extraction.pending_count} 张待审核` : "";
    const line = `<p class="daily-extraction muted" data-status="${esc(extraction.status)}">每日抽卡：${esc(status)}${added}${pending}。${extraction.added_count ? `<a href="${appPath("/memories")}" data-go="memories">查看记忆</a>` : ""}</p>`;
    // A failure is something to know today; normal states stay in the folded status.
    if (extraction.status === "failed") $(".today-status", main).insertAdjacentHTML("beforebegin", line.replace('class="daily-extraction muted"', 'class="daily-extraction today-alert"'));
    else $(".today-status", main).insertAdjacentHTML("beforeend", line);
  }
  const summaries = data.topic_summaries;
  if (summaries) {
    const status = {unconfigured:"未配置",waiting:"等待每月 3 日整理",running:"正在整理",failed:"整理暂未完成，会重试",done:"已整理"}[summaries.status] || "等待整理";
    const line = `<p class="monthly-summaries muted" data-status="${esc(summaries.status)}">主题总结：${esc(status)}。</p>`;
    if (summaries.status === "failed") $(".today-status", main).insertAdjacentHTML("beforebegin", line.replace('class="monthly-summaries muted"', 'class="monthly-summaries today-alert"'));
    else $(".today-status", main).insertAdjacentHTML("beforeend", line);
  }
  const recall = data.recall_counts;
  if (recall && (recall.failed || recall.timed_out || recall.degraded)) {
    $(".today-status", main).insertAdjacentHTML("beforebegin", `<p class="recall-overview today-alert">记忆召回记录：${recall.timed_out} 轮超时 · ${recall.failed} 轮失败 · ${recall.degraded} 轮降级。<a href="${pageURL("conversations")}" data-go="conversations">查看聊天与召回</a></p>`);
  }
  // CSP forbids inline style attributes; CSSOM assignments are allowed.
  $$(".barrow", main).forEach((row) => {
    $(".barfill", row).style.width =
      Math.max(4, Math.round(Number(row.dataset.ratio) * 100)) + "%";
    row.onclick = () => {
      state.memoryTopic = Number(row.dataset.topic);
      navigate("memories");
    };
  });
  wireGo(main);
  void onboardingCard(main, epoch);
}
// "开始使用" card: shown on a new space until a key exists and the first chat has come
// through the proxy, or until dismissed. Once done it is remembered, so no extra requests.
async function onboardingCard(main, epoch) {
  const storageKey = `amber-onboarding-done:${state.space}`;
  try {
    if (localStorage.getItem(storageKey)) return;
  } catch {
    // Storage can be unavailable; the card then simply checks again next time.
  }
  const remember = () => {
    try { localStorage.setItem(storageKey, "1"); } catch { /* ignore */ }
  };
  let key, conversations;
  try {
    const notifyAuth = () => epoch === state.epoch;
    [key, conversations] = await Promise.all([
      api(url("/access-key"), {notifyAuth}), api(url("/conversations?size=1"), {notifyAuth}),
    ]);
  } catch {
    return;
  }
  if (epoch !== state.epoch || !main.isConnected) return;
  const hasKey = !!key.key;
  const connected = conversations.total > 0;
  if (hasKey && connected) {
    remember();
    return;
  }
  const go = (section, text) => `<a href="#" data-go="settings" data-section="${section}">${text}</a>`;
  $(".overview-entries", main).insertAdjacentHTML("afterend", `<section class="onboarding card" aria-labelledby="onboarding-title"><div class="onboarding-head"><h2 id="onboarding-title">开始使用</h2><button type="button" class="quiet" id="dismiss-onboarding">我知道了</button></div><ol class="onboarding-steps"><li class="done">填写你和 TA 的名字</li><li class="${hasKey ? "done" : ""}">生成访问钥匙${hasKey ? "" : " · " + go("set-key", "去设置")}</li><li class="${connected ? "done" : ""}">在聊天应用里连上记忆库${connected ? "" : `<span class="meta">填好接口地址和钥匙后发一条消息，就算完成。</span>` + go("set-key", "查看接口地址")}</li></ol><p class="onboarding-optional meta">可选：${go("set-persona", "写人设")} · ${go("set-import", "导入以前的记忆")}</p></section>`);
  const card = $(".onboarding", main);
  wireGo(card);
  $("#dismiss-onboarding", card).onclick = () => {
    remember();
    card.remove();
  };
}
const reviewPath = (filter) => url(`/drafts?status=${filter}&all_items=true`);
const reviewDetailPath = (item) => url(item.item_kind === "topic_summary" ? `/topic-summaries/${item.id}` : `/drafts/${item.id}`);
async function draftsPage(main, epoch, filter, read, fresh) {
  const rejected = filter === "rejected";
  const data = await read(reviewPath(filter));
  if (epoch !== state.epoch) return;
  if (!rejected && fresh) updateBadge(data.total);
  const empty = rejected
    ? '<p class="empty">没有被拒绝的草稿。</p>'
    : '<p class="empty">草稿都审完了。<br>新的草稿会在这里等你。</p>';
  main.innerHTML = `${rejected ? '<div class="subhead"><h2>已拒绝的草稿</h2><button type="button" class="quiet" id="back-pending">← 回到待审核</button></div>' : ""}<div class="review-stack"><section class="reader" aria-label="草稿列表"></section>${rejected ? "" : '<button type="button" class="quiet rejected-link" id="show-rejected">查看已拒绝的草稿</button>'}</div>`;
  const switchTo = async (filter) => {
    if (!await allowLeave()) return;
    state.filter = filter;
    navigate("review", false);
  };
  $("#show-rejected")?.addEventListener("click", () => switchTo("rejected"));
  $("#back-pending")?.addEventListener("click", () => switchTo("pending"));
  const reader = $(".reader", main);
  if (!data.items.length) { reader.innerHTML = empty; return; }
  const loads = data.items.map(item => {
    const slot = document.createElement("div");
    slot.className = "review-card-slot";
    slot.innerHTML = '<p class="loading">正在读取草稿…</p>';
    reader.append(slot);
    const done = (handled = true) => {
      if (!slot.isConnected) return;
      slot.remove();
      if (!rejected && (handled || fresh)) updateBadge(Math.max(0, state.pending - 1));
      if (!reader.children.length) reader.innerHTML = empty;
    };
    return () => item.item_kind === "topic_summary"
      ? openSummary(slot, item.id, epoch, done, read) : openDraft(slot, item.id, epoch, done, null, read);
  });
  window.scrollTo(0, 0);
  for (let start = 0; start < loads.length && epoch === state.epoch; start += 4) {
    await Promise.all(loads.slice(start, start + 4).map(load => load()));
  }
}

async function openDraft(reader, id, epoch, done, data = null, read = api) {
  try {
    data ||= await read(url(`/drafts/${id}`));
    if (epoch !== state.epoch || !reader.isConnected) return;
    if (state.filter === "pending" && !REVIEWABLE.includes(data.draft.status)) {
      done(false);
      return;
    }
    renderDraft(reader, data, () => openDraft(reader, id, epoch, done), done,
      latest => openDraft(reader, id, epoch, done, latest));
  } catch (error) {
    if (epoch === state.epoch && reader.isConnected) {
      reader.innerHTML = `<p class="error">${esc(error.message)}</p><button class="reload-detail">重试</button>`;
      $(".reload-detail", reader).onclick = () => openDraft(reader, id, epoch, done);
    }
  }
}
function summarySourcesHTML(sources) {
  const group = (kind, title) => {
    const items = sources.filter(source => source.disposition === kind);
    return `<details class="summary-sources"><summary>${title} · ${items.length} 张</summary>${items.map(source => `<article><h3>${esc(source.title || "来源卡片已删除")}</h3>${source.content ? `<p class="card-content">${esc(source.content)}</p>` : ""}</article>`).join("")}</details>`;
  };
  return group("written", "写进总结的卡片") + group("skipped", "未写入，仍正常保留的卡片");
}
async function openSummary(reader, id, epoch, removeCard, read = api) {
  try {
    const data = await read(url(`/topic-summaries/${id}`));
    if (epoch !== state.epoch || !reader.isConnected) return;
    const batch = data.batch;
    if (state.filter === "pending" && batch.status !== "pending") {
      removeCard(false);
      return;
    }
    const pending = batch.status === "pending";
    reader.innerHTML = `<article class="draft-card summary-draft" data-summary-id="${id}"><header class="draft-head"><span class="meta">主题总结</span><span class="meta">${esc(data.topic.name)} · ${esc(batch.window_start)} 至 ${esc(batch.window_end)}</span></header><h2>${esc(batch.title || "已撤销的总结")}</h2><p class="card-content">${esc(batch.content)}</p><p class="muted">${pending ? "正文和吸收清单只读。确认后，只有写进总结的卡片会被吸收；未写入的继续正常保留。" : "这篇总结已处理。"}</p>${summarySourcesHTML(data.sources)}<p class="error" role="alert"></p><div class="actions">${pending ? '<button class="danger reject-summary" type="button">驳回</button><button class="primary approve-summary" type="button">确认生效</button>' : ""}</div></article>`;
    const storageKey = `memory-summary-review:${state.space}:${id}`;
    const send = async (action) => {
      if (state.busy) return;
      if (action === "reject" && !window.confirm("驳回这篇总结？来源卡片继续保留，下次按更长的窗口重新整理。")) return;
      const payload = {expected_version: batch.version};
      if (action === "approve") {
        let key = sessionStorage.getItem(storageKey);
        if (!key) {key = crypto.randomUUID(); sessionStorage.setItem(storageKey,key);}
        payload.expected_policy_revision = data.policy_revision;
        payload.request_key = key;
      }
      busy(reader,true);
      let done = false;
      try {
        await api(url(`/topic-summaries/${id}/${action}`),{method:"POST",body:payload});
        sessionStorage.removeItem(storageKey);
        done = true;
        message(action === "approve" ? "总结已生效" : "总结已驳回，来源卡片继续保留");
      } catch (error) {
        $(".error",reader).textContent = error.network ? "未能确认处理结果，请重试或刷新核对。" : error.message;
        if (error.status === 409) {
          const reload = document.createElement("button");
          reload.textContent = "刷新总结";
          reload.onclick = () => openSummary(reader,id,epoch,removeCard);
          $(".error",reader).append(reload);
        }
      } finally {busy(reader,false);}
      if (done) removeCard();
    };
    $(".approve-summary",reader)?.addEventListener("click",() => send("approve"));
    $(".reject-summary",reader)?.addEventListener("click",() => send("reject"));
  } catch (error) {
    if (epoch === state.epoch && reader.isConnected) {
      reader.innerHTML = `<p class="error">${esc(error.message)}</p><button class="reload-detail">重试</button>`;
      $(".reload-detail",reader).onclick = () => openSummary(reader,id,epoch,removeCard);
    }
  }
}
const REVIEWABLE = ["pending", "structure_pending", "structure_failed"];
function draftStatus(draft) {
  if (draft.status === "approved") return "已收录到记忆里";
  if (draft.duplicate_memory_id) return "和已有的记忆重复，没有新增";
  if (draft.status === "rejected") return "已拒绝";
  return "";
}
function topicPicker(picks, configuration, initial, writable = true) {
  let topicOrder = [...(initial || [])];
  const maximum = state.contract.max_topics;
  const renderTopics = () => {
    const slots = topicOrder.length ? topicOrder : [null];
    const chosen = topicOrder.filter(Boolean);
    picks.innerHTML =
      slots
        .map(
          (id, index) =>
            `<span class="topic-pick${id ? "" : " empty"}"><span class="topic-name">${esc(configuration.topics.find((topic) => topic.id === id)?.name || "选择主题")}</span>${index === 0 && slots.length > 1 ? '<small class="topic-primary">主</small>' : ""}<select class="topic-select" data-index="${index}" aria-label="${index === 0 ? "主要主题" : `第 ${index + 1} 个主题`}" ${writable ? "" : "disabled"}>${id ? "" : '<option value="">选择主题</option>'}${configuration.topics.map((topic) => `<option value="${topic.id}" ${topic.id === id ? "selected" : ""} ${topic.id !== id && chosen.includes(topic.id) ? "disabled" : ""}>${esc(topic.name)}</option>`).join("")}</select>${index > 0 && writable ? `<button type="button" class="quiet remove-topic" data-index="${index}" aria-label="去掉第 ${index + 1} 个主题">×</button>` : ""}</span>`,
        )
        .join("") +
      (writable && chosen.length && slots.length < maximum
        ? '<button type="button" class="quiet add-topic">+ 主题</button>'
        : "");
    $$(".topic-select", picks).forEach(
      (control) =>
        (control.onchange = () => {
          const index = Number(control.dataset.index);
          const value = control.value ? Number(control.value) : null;
          if (!topicOrder.length) topicOrder = [value];
          else topicOrder[index] = value;
          renderTopics();
          picks.dispatchEvent(new Event("change", {bubbles: true}));
        }),
    );
    $$(".remove-topic", picks).forEach(
      (button) =>
        (button.onclick = () => {
          topicOrder.splice(Number(button.dataset.index), 1);
          renderTopics();
          picks.dispatchEvent(new Event("change", {bubbles: true}));
        }),
    );
    const add = $(".add-topic", picks);
    if (add)
      add.onclick = () => {
        topicOrder.push(null);
        renderTopics();
        const last = $$(".topic-select", picks).at(-1);
        last.focus();
        try { last.showPicker?.(); } catch { /* the chip stays tappable */ }
      };
  };
  renderTopics();
  return () => topicOrder.filter(Boolean);
}

function structureFailure(code) {
  return ({
    "structure:timeout": "整理时模型超时了",
    "structure:TimeoutError": "整理时模型超时了",
    "structure:rate_limited": "整理时模型服务限流了",
    "structure:provider": "模型服务未能完成整理",
    "structure:ModelCallError": "模型服务未能完成整理",
    "structure:format": "模型返回的内容格式不符合要求",
    "structure:ContractError": "模型返回的内容格式不符合要求",
    "structure:credentials": "整理结果包含不能保存的凭证信息",
    "structure:CredentialContent": "整理结果包含不能保存的凭证信息",
    "structure:configuration_changed": "重新整理时设置有变化",
    "structure:material_expired": "重新整理时原文已过期",
  })[code] || "整理未完成，暂时无法确定原因";
}
function importOriginalHTML(original) {
  if (!original) return "";
  return `<details class="import-original"><summary>导入原文</summary>${original.deleted_at ? '<p class="muted">原文已过保留期限或已删除。</p>' : `<p class="card-content">${esc(original.text)}</p>`}</details>`;
}

function renderDraft(reader, data, reloadCard, removeCard, replaceCard) {
  const { draft, source, configuration } = data;
  // Reviewers only judge whether the card reads true. Model-filled structure
  // (importance, emotion, layers, entities, relations, evidence) is carried
  // through unchanged instead of being shown as decisions.
  const p = draft.payload || draft.narrative;
  let draftVersion = draft;
  const writable = REVIEWABLE.includes(draft.status) && !source.deleted_at;
  const complete = !!draft.payload;
  const policyChanged = data.configuration_changes.length > 0;
  const date = p.source_date || draft.business_date || "";
  const status = draftStatus(draft);
  const unfinished = {
    structure_pending:
      "这张草稿还在整理中，还没分好主题。可以等整理完再审，也可以现在选好主题直接收录。",
    structure_failed: structureFailure(draft.error_code) + "。可以重新整理，也可以选好主题后直接收录。",
  };
  reader.innerHTML = `<article class="draft-card" data-draft-id="${draft.id}"><header class="draft-head"><span class="meta">${esc(date)}${status ? " · " + esc(status) : ""}</span>${writable && !source.deleted_at ? '<span class="edit-hint">点击空白处可编辑</span>' : ""}</header>${policyChanged && writable ? `<aside class="configuration-warning" role="alert"><h3>生成这张草稿后，设置有改动</h3><ul>${data.configuration_changes.map((change) => `<li>${esc(change)}</li>`).join("")}</ul><p>看一眼正文中的名字和人物指代是否准确，再确认收录。</p></aside>` : ""}${data.manual_review && writable ? '<p class="hint">这张到了自动收录的时间，但生成后设置或原对话有改动，所以没有自动收录。看一眼没问题，点收录就行。</p>' : ""}${!complete && writable ? `<p class="hint">${unfinished[draft.status] || unfinished.structure_failed}</p>` : ""}${source.deleted_at ? '<p class="hint">原对话已删除，这张草稿不能再修改或收录。</p>' : ""}${source.material_kind === "memory" ? importOriginalHTML({text: source.segments?.[0]?.text, deleted_at: source.deleted_at}) : ""}${draft.error_code === "import_approval_failed" && writable ? '<p class="hint">导入时收录未完成，可以核对后手动收录。</p>' : ""}<form class="draft-form"><div class="readfield title"><label class="fieldcaption" for="draft-title-${draft.id}">标题</label><textarea id="draft-title-${draft.id}" name="title" rows="1" required>${esc(p.title)}</textarea></div><div class="readfield body"><label class="fieldcaption" for="draft-content-${draft.id}"><span>正文</span><span class="wordcount">${p.content.length} 字</span></label><textarea id="draft-content-${draft.id}" name="content" rows="1" required>${esc(p.content)}</textarea></div><div class="readfield summary"><label class="fieldcaption" for="draft-summary-${draft.id}">摘要</label><textarea id="draft-summary-${draft.id}" name="summary" rows="1" required>${esc(p.summary)}</textarea></div><div class="draft-props"><div class="prop"><div class="prop-head"><span class="prop-label" id="topic-label-${draft.id}">主题</span><label class="check milestone"><input type="checkbox" name="is_milestone" ${p.is_milestone ? "checked" : ""}>里程碑</label></div><div class="topic-picks" role="group" aria-labelledby="topic-label-${draft.id}"></div></div></div>${writable ? saveStatusHTML() : ""}<p class="error draft-error" role="alert"></p><button type="button" class="quiet reload-draft" hidden>读取最新版本</button><p class="draft-result" role="status"></p><div class="actions">${draft.status === "structure_failed" && writable ? '<button type="button" class="quiet restructure-draft">重新整理</button>' : ""}<button type="button" class="danger reject-draft">拒绝</button><span class="approval-area">${policyChanged && writable ? '<span class="approval-note">将按新设置收录</span>' : ""}<span class="approval-buttons"><button type="submit" class="primary approve-draft">确认收录</button></span></span></div></form></article>`;
  const form = $(".draft-form", reader);
  const reload = $(".reload-draft", form);
  const rejectable = REVIEWABLE.includes(draft.status);
  if (!writable) {
    $$("input,textarea,select,button", form).forEach(
      (element) => (element.disabled = true),
    );
    $(".approve-draft", form).hidden = true;
  }
  reload.disabled = false;
  $(".reject-draft", form).disabled = !rejectable;
  $(".reject-draft", form).hidden = !rejectable;
  $$(".readfield textarea", form).forEach((textarea) => {
    fitTextarea(textarea);
    for (const event of ["input", "focus", "blur"]) {
      textarea.addEventListener(event, () => fitTextarea(textarea));
    }
  });
  $('[name="content"]', form).oninput = (event) => {
    $(".wordcount", form).textContent = event.target.value.length + " 字";
  };
  // Topics: the first dropdown is the primary topic; up to max_topics in all.
  const picks = $(".topic-picks", form);
  const chosenTopics = topicPicker(picks, configuration, p.topic_ids, writable);
  const maximum = state.contract.max_topics;
  const collect = () => {
    const values = Object.fromEntries(new FormData(form));
    return {
      title: values.title,
      content: values.content,
      summary: values.summary,
      importance: p.importance,
      evidence: p.evidence,
      experience_mode: p.experience_mode || "external",
      cognitive_status: p.cognitive_status || "stable",
      decay_layer: p.decay_layer || "tier_4_episode",
      // A business day can differ from the calendar day and is not the date
      // of an event recalled in the conversation. Unknown event dates stay null.
      source_date: p.source_date ?? null,
      is_milestone: !!values.is_milestone,
      valence: p.valence ?? null,
      arousal: p.arousal ?? null,
      topic_ids: chosenTopics(),
      entities: p.entities || [],
      relations: p.relations || [],
    };
  };
  const reviewStorage = `memory-review:${state.space}:${draft.id}`;
  let uncertain = sessionStorage.getItem(reviewStorage);
  const result = $(".draft-result", form);
  const error = $(".draft-error", form);
  const showError = (exc) => {
    errorAt(error, exc);
    if (exc.status === 409) reload.hidden = false;
  };
  const saveStatus = $(".autosave-status", form);
  const saver = writable ? autoSave(saveStatus, collect, async (payload) => {
    if (!payload.topic_ids.length) throw new Error("请先选一个主题");
    try {
      const actual = await api(url(`/drafts/${draft.id}`), {
        method: "PUT",
        body: {
          payload,
          expected_version: draftVersion.version,
          expected_policy_revision: configuration.revision,
        },
      });
      if (form.isConnected) draftVersion = actual;
      reload.hidden = true;
    } catch (exc) {
      if (exc.status === 409) reload.hidden = false;
      throw exc;
    }
  }) : null;
  if (saver) wireAutoSave(form, saver);
  reload.onclick = async () => {
    if (state.busy) return;
    if (saver && !await saver.flush() && !window.confirm("读取最新版本会丢弃这张草稿尚未保存的修改。继续？")) return;
    saver?.dispose();
    await reloadCard();
  };
  $(".reject-draft", form).onclick = async () => {
    if (state.busy || !window.confirm("拒绝这张草稿？"))
      return;
    busy(form, true);
    try {
      await saver?.flush();
      await api(url(`/drafts/${draft.id}/reject`), {
        method: "POST",
        body: { expected_version: draftVersion.version },
      });
      sessionStorage.removeItem(reviewStorage);
      message("草稿已拒绝");
      saver?.dispose();
      removeCard();
    } catch (exc) {
      showError(exc);
    } finally {
      state.busy = false;
      if (form.isConnected) busy(form, false);
    }
  };
  const finish = async (actual) => {
    if (!["approved", "rejected"].includes(actual.status)) return false;
    if (actual.review_key !== uncertain) {
      sessionStorage.removeItem(reviewStorage);
      uncertain = null;
      message("这张草稿已经在别处处理过了");
      saver?.dispose();
      removeCard();
      return true;
    }
    sessionStorage.removeItem(reviewStorage);
    uncertain = null;
    message(
      actual.status === "approved" ? "已收录" : "和已有的记忆重复，没有新增",
    );
    saver?.dispose();
    removeCard();
    return true;
  };
  async function checkResult() {
    const current = await api(url(`/drafts/${draft.id}`));
    return finish(current.draft);
  }
  if (uncertain && writable) {
    result.innerHTML =
      '上次收录结果待核对。<button type="button" class="check-result">核对收录结果</button>';
    $(".check-result", form).onclick = async () => {
      try {
        if (!(await checkResult()))
          message("尚未查到已提交结果，可以使用原请求再次确认收录");
      } catch (exc) {
        showError(exc);
      }
    };
  }
  const restructure = $(".restructure-draft", form);
  if (restructure) restructure.onclick = async () => {
    if (state.busy || form.inert) return;
    const saved = await saver.flush();
    if (!saved && !window.confirm("重新整理会丢弃这张草稿尚未保存的修改。继续？")) return;
    if (!form.isConnected || form.inert) return;
    if (draftVersion.status !== "structure_failed") {
      saver.dispose();
      await reloadCard();
      return;
    }
    busy(form, true, false);
    error.textContent = "";
    result.textContent = "正在重新整理…";
    try {
      const queued = await api(url(`/drafts/${draft.id}/restructure`), {
        method: "POST", body: {expected_version: draftVersion.version},
      });
      const deadline = Date.now() + queued.wait_seconds * 1000;
      while (form.isConnected && Date.now() < deadline) {
        const latest = await api(url(`/drafts/${draft.id}`));
        if (latest.draft.version !== queued.version) {
          saver.dispose();
          await replaceCard(latest);
          return;
        }
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      if (form.isConnected) {
        result.textContent = "整理结果尚未确认，可以读取最新版本。";
        reload.hidden = false;
      }
    } catch (exc) {
      showError(exc);
      result.textContent = "";
      if (exc.network) reload.hidden = false;
    } finally {
      if (form.isConnected) busy(form, false, false);
    }
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (state.busy || !writable) return;
    error.textContent = "";
    const chosen = chosenTopics();
    if (chosen.length < 1 || chosen.length > maximum) {
      error.textContent = "请先选一个主题";
      $(".topic-select", picks)?.focus();
      return;
    }
    busy(form, true);
    result.textContent = "正在收录…";
    try {
      // Read form values even while buttons are disabled; controls remain readable.
      if (uncertain && (await checkResult())) return;
      // Finishing an incomplete draft or accepting new settings also requires a save.
      const mustSave = !draftVersion.payload || draftVersion.policy_revision !== configuration.revision;
      if (!await saver.flush(mustSave)) {
        result.textContent = "草稿保存成功后才能收录";
        return;
      }
      uncertain = uncertain || crypto.randomUUID();
      sessionStorage.setItem(reviewStorage, uncertain);
      const actual = await api(url(`/drafts/${draft.id}/approve`), {
        method: "POST",
        body: {
          expected_version: draftVersion.version,
          expected_policy_revision: configuration.revision,
          request_key: uncertain,
        },
      });
      await finish(actual);
    } catch (exc) {
      if (exc.network) {
        try {
          if (await checkResult()) return;
        } catch {}
        result.textContent =
          "收录结果尚未确认。再次点击确认会先核对原请求，不会根据列表变化猜测成功。";
      } else {
        result.textContent = "收录未完成，草稿和编辑内容保留";
        if (exc.status !== 503) {
          sessionStorage.removeItem(reviewStorage);
          uncertain = null;
        }
      }
      showError(exc);
    } finally {
      state.busy = false;
      if (form.isConnected) busy(form, false);
    }
  };
}
function aliasList(value) {
  // Aliases are typed on one line; accept 顿号, commas, semicolons or line breaks.
  return [...new Set(value.split(/[\n、，,；;]/).map((item) => item.trim()).filter(Boolean))];
}
function lines(value) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}
function participantsForm(config, initialize) {
  return `<form id="participants-form"><div class="people">${["user", "assistant"].map((role) => `<section class="person"><h3>${role === "user" ? "你" : "TA"}</h3>${field("名字", role + "_name", config?.participants[role].name || "", "text", `required maxlength="200" placeholder="${role === "user" ? "例：林小满" : "例：顾言"}"`)}<label>又名（如小名）<input name="${role}_aliases" type="text" maxlength="1000" placeholder="${role === "user" ? "例：满满、小满" : "例：阿言、言言"}" value="${esc(config?.participants[role].aliases.join("、") || "")}"></label></section>`).join("")}</div><p class="muted">又名可以空着，也可以填多个，用顿号隔开。聊天里出现这些名字时，记忆系统会知道说的是同一个人。想让 TA 怎么称呼你，请写在人设或核心记忆里。</p>${initialize ? `<label>记忆主题模板（创建后固定）<select name="topic_template"><option value="lover">恋人（默认）</option><option value="general">通用</option></select></label><p class="muted" id="template-preview"></p>` : ""}<p class="error" role="alert"></p><div class="actions"><button class="primary">${initialize ? "创建空间" : "保存参与者"}</button></div></form>`;
}
function wireParticipants(form, config, initialize, writeConfig = (write) => write()) {
  track(form);
  if (initialize) {
    const preview = () =>
      ($("#template-preview").textContent = "记忆会按这些主题归类：" + state.contract.topic_presets[
        form.elements.topic_template.value
      ]
        .map((topic) => topic.name + (topic.is_fallback ? "（兜底）" : ""))
        .join(" · "));
    form.elements.topic_template.onchange = preview;
    preview();
  }
  form.onsubmit = async (event) => {
    event.preventDefault();
    if (state.busy) return;
    const values = Object.fromEntries(new FormData(form));
    const errorBox = $(".error", form);
    errorBox.textContent = "";
    busy(form, true);
    try {
      // 首发一个安装只对应一位 AI，空间编号固定，不让用户填写。
      const space = initialize ? "default" : state.space;
      const latest = await writeConfig(() => api(
        `/api/spaces/${encodeURIComponent(space)}/participants`,
        {
          method: "PUT",
          body: {
            expected_revision: config?.revision || 0,
            user: { name: values.user_name, aliases: aliasList(values.user_aliases) },
            assistant: {
              name: values.assistant_name,
              aliases: aliasList(values.assistant_aliases),
            },
            ...(initialize ? { topic_template: values.topic_template } : {}),
          },
        },
      ));
      state.dirty = false;
      state.spaces = await api("/api/spaces");
      if (initialize) {
        await finishInitialize();
        return;
      }
      state.space = space;
      sessionStorage.setItem("memory-space", space);
      // The AI name labels the space selector, so refresh the header too.
      const select = $("#space");
      const option = select?.querySelector(`option[value="${CSS.escape(space)}"]`);
      if (option) option.textContent = latest.participants.assistant.name;
      message("参与者已保存");
    } catch (error) {
      // The space may have been created even though the reply never arrived;
      // a retry would then only hit "template already chosen". Check first.
      if (initialize) {
        try {
          state.spaces = await api("/api/spaces");
        } catch {
          // Keep the original error below.
        }
        if (state.spaces.some((s) => s.id === "default")) {
          await finishInitialize();
          return;
        }
      }
      if (error.message === "两个参与者的名字/别名不能重叠，纯称呼词不能作为参与者别名") {
        errorBox.textContent = "两个人的名字或又名有重复，或者填了宝宝、老婆这类两个人都可能用的爱称。爱称不用填在这里，想让 TA 这么叫你，写在人设或核心记忆里。";
      } else {
        errorAt(errorBox, error);
      }
    } finally {
      state.busy = false;
      if (form.isConnected) busy(form, false);
    }
  };
}
async function finishInitialize() {
  // 首发一个安装只对应一位 AI，空间编号固定为 default。
  clearPageCache();
  state.space = "default";
  sessionStorage.setItem("memory-space", state.space);
  // 重装后空间编号仍是 default，清掉旧空间留下的"开始使用"已完成标记。
  try { localStorage.removeItem(`amber-onboarding-done:${state.space}`); } catch { /* ignore */ }
  state.dirty = false;
  message("空间已创建，可以开始使用");
  await navigate("overview", false);
}
function receiptLabel(receipt, memoryId = null) {
  if (receipt.status === "reported_unused") return "模型报告未使用";
  if (receipt.status === "reported" && (memoryId === null || (receipt.reported_ids || []).includes(memoryId))) return "模型报告使用";
  if (receipt.status === "judged" && (memoryId === null || (receipt.reported_ids || []).includes(memoryId))) return "夜间补判使用";
  if (receipt.status === "judged" || receipt.status === "judged_unused") return "夜间补判未使用";
  return "未报告";
}
const bodyText = (parts) => (parts || []).map((part) => part.type === "text" ? part.text : "[曾发送图片，图片内容未保留]").join("");
const shortTime = (value) => value ? value.slice(5, 16).replace("T", " ") : "";
// The space list carries the assistant's configured name.
const assistantName = () => state.spaces.find((space) => space.id === state.space)?.name || "";
// One fold per day, newest open: today is what matters; older days stay a tap away.
async function summariesPage(main, epoch, read) {
  const data = await read(url("/summaries?size=100"));
  if (epoch !== state.epoch) return;
  if (!data.items.length) {
    main.innerHTML = '<section id="summary-library" aria-label="及时摘要记录"><p class="empty">还没有及时摘要。对话达到条件后会自动生成。</p></section>';
    return;
  }
  const days = new Map();
  for (const item of data.items) {
    if (!days.has(item.business_date)) days.set(item.business_date, []);
    days.get(item.business_date).push(item);
  }
  const time = (item) => `${(item.start_at || "").slice(11, 16)}–${(item.end_at || "").slice(11, 16)}`;
  main.innerHTML = `<section id="summary-library" aria-label="及时摘要记录">${[...days].map(([day, items], index) =>
    `<details class="summary-day" ${index === 0 ? "open" : ""}><summary><span>${diaryDate(day)}</span></summary>${items.map((item) =>
      `<article class="summary-entry card"><h2 class="summary-when">${esc(time(item))}</h2><p class="summary-text">${esc(item.content || "")}</p></article>`).join("")}</details>`).join("")}<p class="muted summary-retention">只保留最近 15 天，更早的日子可以看日记。</p></section>`;
}
async function conversationsPage(main, epoch, read) {
  const status = {failed: "上游失败 · 未获回应", incomplete: "未完成", generating: "正在回复", waiting_tools: "等待工具"};
  const recallStatus = {ok: "召回完成", empty: "未召回卡片", failed: "召回失败，本轮未注入记忆", timeout: "召回超时，本轮未注入记忆", degraded: "召回已降级", skipped_image: "纯图片消息，跳过召回", expired: "原文已删除", not_run: "未记录召回"};
  function turnHTML(turn) {
    const events = turn.messages.map((m, index) => {
      const ai = m.role === "assistant";
      const body = turn.content_deleted_at ? "原文已删除" : (m.display_text ?? bodyText(m.content_parts));
      const recall = ai && turn.recall_count > 0
        ? `<details class="conversation-recall" data-turn="${turn.id}" data-request="${m.request_id}"><summary>本轮召回 ${turn.recall_count} 张记忆</summary><div class="recall-detail"></div></details>`
        : "";
      const html = ai && !body.trim() ? "" : `<section class="conversation-message ${ai ? "assistant" : "user"}"><div class="conversation-heading"><span>${ai ? esc(assistantName()) : "你"}</span><time datetime="${esc(m.occurred_at)}">${esc(m.occurred_at?.slice(11, 16))}</time>${m.content_state === "incomplete" ? '<span class="conversation-state">未完成</span>' : ""}</div><p class="conversation-body">${esc(body)}</p>${recall}</section>`;
      return {index: m.event_index ?? index, html};
    });
    for (const tool of turn.tool_calls || []) {
      events.push({index: tool.event_index, html: `<p class="conversation-tool">${esc(tool.label)} · ${esc(tool.outcome)}</p>`});
    }
    events.sort((a, b) => a.index - b.index);
    return `<article class="conversation-turn" data-turn="${turn.id}">${turn.history_omitted?.turns ? `<p class="conversation-state history-omitted">本轮省略了较早的 ${turn.history_omitted.turns} 轮原话（${turn.history_omitted.messages} 条），已告诉 TA。</p>` : ""}${events.map(event => event.html).join("")}${status[turn.generation_state] ? `<p class="conversation-state">${status[turn.generation_state]}</p>` : ""}</article>`;
  }
  function daysHTML(items) {
    const days = new Map();
    for (const turn of items) {
      const day = turn.created_at.slice(0, 10);
      if (!days.has(day)) days.set(day, []);
      days.get(day).push(turn);
    }
    return [...days.keys()].sort().map(day => {
      const date = new Date(`${day}T00:00:00Z`);
      const weekday = "日一二三四五六"[date.getUTCDay()];
      const heading = `${date.getUTCMonth() + 1}月${date.getUTCDate()}日 周${weekday}`;
      return `<section class="conversation-day" aria-label="${heading}"><h2><time datetime="${esc(day)}">${heading}</time></h2>${days.get(day).map(turnHTML).join("")}</section>`;
    }).join("");
  }
  function wireRecall(root) {
    $$(".conversation-recall", root).forEach((detail) => detail.addEventListener("toggle", async () => {
      if (!detail.open || detail.dataset.loaded) return;
      const target = $(".recall-detail", detail);
      target.textContent = "正在读取本轮记忆…";
      try {
        const result = await api(url(`/conversations/${detail.dataset.turn}/recall`));
        if (!detail.isConnected) return;
        const snapshot = result.recall_snapshot || {};
        const receipt = result.requests.find((r) => String(r.id) === detail.dataset.request)?.usage_receipt || {};
        target.innerHTML = `<p class="meta">${esc(recallStatus[snapshot.status] || "未记录召回")}</p>${(snapshot.cards || []).map((card) => `<div class="recalled-card"><h3>${esc(card.title || "未命名卡片")}<span class="receipt-label">${receiptLabel(receipt, card.memory_id)}</span></h3><p>${esc(card.content)}</p></div>`).join("") || '<p class="muted">本轮没有注入卡片。</p>'}`;
        detail.dataset.loaded = "1";
      } catch (error) {
        target.innerHTML = `<p class="error">${esc(error.message)}；收起后重新展开可重试。</p>`;
      }
    }));
  }
  const data = await read(url("/conversations"));
  if (epoch !== state.epoch) return;
  if (!data.items.length) {
    main.innerHTML = '<div class="conversation-list"><p class="empty">还没有对话。连接聊天应用后，经过代理的对话会留在这里。</p></div>';
    return;
  }
  // Like a chat window: open at the newest turn, load older turns when the reader scrolls up.
  main.innerHTML = `<p class="conversation-more meta" role="status"></p><div class="conversation-list">${daysHTML(data.items)}</div>`;
  const list = $(".conversation-list", main), more = $(".conversation-more", main);
  wireRecall(list);
  window.scrollTo(0, document.documentElement.scrollHeight);
  let before = data.has_more ? data.next_before : null, loading = false;
  if (before === null) return more.remove();
  const observer = new IntersectionObserver(async (entries) => {
    if (epoch !== state.epoch || !main.isConnected) return observer.disconnect();
    if (!entries.some((entry) => entry.isIntersecting) || loading) return;
    loading = true;
    more.textContent = "正在读取更早的对话…";
    try {
      const older = await api(url(`/conversations?before=${before}`));
      if (epoch !== state.epoch || !main.isConnected) return observer.disconnect();
      const batch = document.createElement("template");
      batch.innerHTML = daysHTML(older.items);
      wireRecall(batch.content);
      // Keep the turn being read in place after older turns grow above it.
      const anchor = $(".conversation-turn", list), anchorTop = anchor.getBoundingClientRect().top;
      const first = $(".conversation-day", list), last = batch.content.lastElementChild;
      if (first && last && $("h2 time", first).getAttribute("datetime") === $("h2 time", last).getAttribute("datetime")) {
        $("h2", first).after(...[...last.children].slice(1));
        last.remove();
      }
      list.prepend(batch.content);
      before = older.has_more ? older.next_before : null;
      more.textContent = "";
      observer.unobserve(more);
      if (before === null) more.remove();
      window.scrollBy(0, anchor.getBoundingClientRect().top - anchorTop);
      if (before !== null) observer.observe(more);
    } catch (error) {
      if (!more.isConnected) return;
      observer.unobserve(more);
      more.innerHTML = `<span class="error">${esc(error.message)}</span> <button type="button" class="quiet">重试</button>`;
      $("button", more).onclick = () => observer.observe(more);
    } finally {
      loading = false;
    }
  }, {rootMargin: "600px 0px 0px 0px"});
  observer.observe(more);
}

async function settingsPage(main, initialize, epoch, section, read) {
  if (initialize) {
    main.innerHTML = `<section class="settings-section"><h2>初始化记忆空间</h2><p class="muted">这一套记忆库，留给你和 TA 共同经历的记忆。</p>${participantsForm(null, true)}</section>${logoutHTML()}`;
    wireParticipants($("#participants-form"), null, true);
    wireLogout();
    return;
  }
  const config = await read(url("/configuration"));
  if (epoch !== state.epoch) return;
  const spaceSection = state.spaces.length > 1
    ? `<section class="settings-section space-settings"><h2>记忆空间</h2><p class="muted">每个空间有各自的参与者、主题和记忆。</p><label>当前空间<select id="space" aria-label="记忆空间">${state.spaces.map((space) => `<option value="${esc(space.id)}" ${space.id === state.space ? "selected" : ""}>${esc(space.name)}</option>`).join("")}</select></label></section>`
    : "";
  main.innerHTML = `${spaceSection}<div class="settings-index" role="group" aria-label="设置分区"><button type="button" data-jump="set-together">相识日</button><button type="button" data-jump="set-people">参与者</button><button type="button" data-jump="set-key">访问钥匙</button><button type="button" data-jump="set-mcp">MCP 连接</button><button type="button" data-jump="set-persona">人设</button><button type="button" data-jump="set-import">导入</button><button type="button" data-jump="set-review">自动入库</button><button type="button" data-jump="set-bookmarks">记忆书签</button></div><section class="settings-section" id="set-together" aria-labelledby="together-title"><h2 id="together-title">相识日</h2><p class="muted">填写后，概览会显示“已经相伴 N 天”，相识那天算第 1 天。可以不填。</p><form id="together-form" class="together"><div class="together-row">${dateField("相识日", "acquaintance_date", config.acquaintance_date || "", "选择日期")}<button type="button" class="quiet" id="clear-date" ${config.acquaintance_date ? "" : "hidden"}>清除</button></div>${saveStatusHTML()}</form></section><section class="settings-section" id="set-people" aria-labelledby="people-title"><h2 id="people-title">参与者</h2><p class="muted">整理和搜索记忆时，靠这些名字认出谁是谁。搜索会用新名字识别人，已有正文不会自动改写。</p>${participantsForm(config, false)}</section><section class="settings-section" id="set-key" aria-labelledby="key-title"><h2 id="key-title">访问钥匙</h2><p class="muted">在聊天应用里新增一个 OpenAI 兼容的服务：接口地址填下面这个地址，API Key 填这把钥匙。钥匙只能访问当前空间；重新生成后，所有设备上的旧钥匙立即失效。</p><div class="mcp-field"><label for="proxy-address">接口地址<input id="proxy-address" type="text" readonly autocomplete="off"></label><button type="button" id="copy-proxy">复制</button></div><div id="key-content"><p class="loading">正在读取…</p></div></section><section class="settings-section" id="set-mcp" aria-labelledby="mcp-title"><h2 id="mcp-title">MCP 连接</h2><p class="muted">让 TA 主动查记忆、记书签。这里使用的就是“访问钥匙”一栏里的同一把钥匙。</p><div id="mcp-content"></div></section><section class="settings-section" id="set-persona" aria-labelledby="persona-title"><h2 id="persona-title">人设（系统提示词）</h2><p class="muted">写在这里的内容会替换聊天应用里的系统提示词（包括世界书）；不写就用聊天应用自己的。修改从下一轮对话生效。</p><form id="persona-form"><div class="persona-import"><button type="button" class="quiet" id="import-persona" aria-describedby="persona-formats">从文件导入</button><span class="muted" id="persona-formats">支持 .txt、.md、.docx</span><input type="file" id="persona-file" hidden></div><label for="persona-text"><textarea id="persona-text" aria-label="人设（系统提示词）" name="persona" rows="7" maxlength="100000" placeholder="输入人设…">${esc(config.persona || "")}</textarea></label><p class="error" id="persona-import-error" role="alert"></p>${saveStatusHTML()}</form></section><section class="settings-section" id="set-import" aria-labelledby="import-title"><h2 id="import-title">导入</h2><p class="muted">把以前的聊天记录或你自己整理的记忆搬过来，整理成卡片。不导入也可以，从零开始。</p><button type="button" class="primary" data-go="imports">去导入</button></section><section class="settings-section" id="set-review" aria-labelledby="auto-approval-title"><h2 id="auto-approval-title">自动入库</h2><form id="auto-approval-form"><label class="check" for="auto-approval"><input id="auto-approval" type="checkbox" name="auto_approve_enabled" ${config.auto_approve_enabled ? "checked" : ""}>未审核的卡片 ${config.auto_approve_delay_hours} 小时后自动入库</label><p class="muted">达到等待时间后，在之后的每日整理中收录；修改并保存草稿会重新计时。关闭后仅手动收录。</p>${saveStatusHTML()}</form></section><section class="settings-section" id="set-bookmarks" aria-labelledby="bookmark-settings-title"><h2 id="bookmark-settings-title">记忆书签</h2><form id="period-notes-form"><label class="check" for="period-notes"><input id="period-notes" type="checkbox" name="period_notes_enabled" ${config.period_notes_enabled ? "checked" : ""}>记录月经周期</label><p class="muted">关闭后，记忆书签不再显示这个分区，TA 也不会再往里记；已记下的内容会保留。</p>${saveStatusHTML()}</form></section>${logoutHTML()}`;
  wireSpaceSelector();
  wireLogout();
  // The four writes use the same space revision, even across different forms.
  let configWrite = Promise.resolve();
  const writeConfig = (write) => {
    const request = configWrite.then(async () => {
      const latest = await write();
      Object.assign(config, latest);
      return latest;
    });
    configWrite = request.catch(() => {});
    return request;
  };
  // Section index: pinned at the top while scrolling; the section in view is underlined.
  const sectionIndex = $(".settings-index", main);
  const jumps = $$("[data-jump]", main);
  const jumpTargets = jumps.map((button) => $("#" + button.dataset.jump));
  jumps.forEach(
    (button) =>
      (button.onclick = () =>
        $("#" + button.dataset.jump).scrollIntoView({ behavior: "smooth" })),
  );
  let currentJump = -1;
  let jumpFrame = 0;
  const markCurrentSection = () => {
    if (!sectionIndex.isConnected) {
      window.removeEventListener("scroll", onSettingsScroll);
      return;
    }
    // A section counts as current once its heading is in the upper quarter of the screen.
    const line = sectionIndex.getBoundingClientRect().bottom + window.innerHeight * 0.25;
    let active = 0;
    jumpTargets.forEach((section, i) => {
      if (section.getBoundingClientRect().top <= line) active = i;
    });
    // The last sections are short and may never reach the top; the page end selects the last.
    if (window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 2) active = jumps.length - 1;
    if (active === currentJump) return;
    currentJump = active;
    jumps.forEach((button, i) => button.setAttribute("aria-current", String(i === active)));
    const button = jumps[active];
    sectionIndex.scrollTo({ left: button.offsetLeft - (sectionIndex.clientWidth - button.offsetWidth) / 2, behavior: "smooth" });
  };
  const onSettingsScroll = () => {
    if (!jumpFrame) jumpFrame = requestAnimationFrame(() => { jumpFrame = 0; markCurrentSection(); });
  };
  window.addEventListener("scroll", onSettingsScroll, { passive: true });
  markCurrentSection();
  wireGo($("#set-import", main));
  // The chat proxy lives next to /mcp on the same origin.
  const proxyAddress = $("#proxy-address", main);
  proxyAddress.value = (state.mcpUrl || "").replace(/\/mcp\/?$/, "/v1");
  $("#copy-proxy", main).onclick = async () => {
    try {
      await navigator.clipboard.writeText(proxyAddress.value);
      message("接口地址已复制");
    } catch {
      proxyAddress.focus();
      proxyAddress.select();
    }
  };
  if (section) $("#" + section, main)?.scrollIntoView();
  const autosaveSetting = (form, path, key, read) => {
    const save = autoSave($(".autosave-status", form), read, (value) =>
      writeConfig(() => api(url(path), {
        method: "PATCH",
        body: { expected_revision: config.revision, [key]: value },
      })),
    );
    wireAutoSave(form, save);
    form.onsubmit = (event) => event.preventDefault();
    return save;
  };
  const together = $("#together-form");
  const date = together.elements.acquaintance_date;
  const clearDate = $("#clear-date");
  wireDates(together);
  const saveDate = autosaveSetting(together, "/configuration", "acquaintance_date", () => date.value || null);
  const updateClear = () => { clearDate.hidden = !date.value; };
  date.addEventListener("input", updateClear);
  date.addEventListener("change", updateClear);
  clearDate.onclick = () => {
    date.value = "";
    date.dispatchEvent(new Event("input"));
    saveDate.changed(true);
  };
  const persona = $("#persona-form");
  autosaveSetting(persona, "/persona", "persona", () => persona.elements.persona.value);
  wirePersonaImport(persona);
  const autoApproval = $("#auto-approval-form");
  autosaveSetting(autoApproval, "/auto-approval", "auto_approve_enabled", () => autoApproval.elements.auto_approve_enabled.checked);
  const periodNotes = $("#period-notes-form");
  autosaveSetting(periodNotes, "/period-notes", "period_notes_enabled", () => periodNotes.elements.period_notes_enabled.checked);
  wireParticipants($("#participants-form"), config, false, writeConfig);
  void accessKeySection(epoch);
}

function wirePersonaImport(form) {
  const button = $("#import-persona", form);
  const picker = $("#persona-file", form);
  const error = $("#persona-import-error", form);
  const textarea = form.elements.persona;
  button.onclick = () => picker.click();
  picker.onchange = async () => {
    const file = picker.files[0];
    if (!file) return;
    picker.value = "";
    error.textContent = "";
    button.disabled = true;
    try {
      const text = await readPersonaFile(file, textarea.maxLength);
      if (!form.isConnected) return;
      if (textarea.value && !window.confirm("会替换当前人设，继续吗？")) return;
      textarea.value = text;
      textarea.dispatchEvent(new Event("input", {bubbles: true}));
    } catch (exc) {
      error.textContent = exc.message;
    } finally {
      button.disabled = false;
    }
  };
}

async function accessKeySection(epoch) {
  const target = $("#key-content");
  if (!target) return;
  async function load() {
    renderMcpConnection("");
    try {
      const data = await api(url("/access-key"), {notifyAuth: () => epoch === state.epoch});
      if (epoch !== state.epoch || !target.isConnected) return;
      renderMcpConnection(data.key || "");
      if (!data.configured) {
        target.innerHTML = '<p class="muted">管理员需要先在服务器配置访问钥匙主密钥。</p>';
        return;
      }
      target.innerHTML = `${data.key ? '<label>当前钥匙<input id="space-key" type="password" readonly autocomplete="off" aria-label="当前访问钥匙"></label><div class="key-actions"><button type="button" id="show-key">显示全文</button><button type="button" id="copy-key">复制</button></div>' : '<p class="muted">还没有有效的访问钥匙。</p>'}<div class="key-actions"><button type="button" class="primary" id="generate-key">${data.key ? '重新生成' : '生成钥匙'}</button>${data.key ? '<button type="button" class="quiet" id="revoke-key">作废钥匙</button>' : ''}</div><p class="error" role="alert"></p>`;
      if (data.key) {
        const input = $("#space-key", target);
        input.value = data.key;
        $("#show-key", target).onclick = (event) => {
          const hidden = input.type === "password";
          input.type = hidden ? "text" : "password";
          event.target.textContent = hidden ? "收起全文" : "显示全文";
        };
        $("#copy-key", target).onclick = async () => {
          try {
            await navigator.clipboard.writeText(input.value);
            message("钥匙已复制");
          } catch {
            input.type = "text";
            input.select();
            $(".error", target).textContent = "无法自动复制，请选中钥匙手动复制。";
          }
        };
      }
      async function change(method) {
        if (state.busy) return;
        if (data.key && !confirm(method === "DELETE" ? "作废后，所有设备都无法继续使用这把钥匙。确定作废？" : "重新生成后，所有设备都需要填写新钥匙。确定继续？")) return;
        busy(target, true);
        try {
          await api(url("/access-key"), { method, body: {} });
          await load();
          message(method === "DELETE" ? "钥匙已作废" : "新钥匙已生成");
        } catch (error) {
          errorAt($(".error", target), error);
        } finally {
          state.busy = false;
          if (target.isConnected) busy(target, false);
        }
      }
      $("#generate-key", target).onclick = () => change("POST");
      const revoke = $("#revoke-key", target);
      if (revoke) revoke.onclick = () => change("DELETE");
    } catch (error) {
      if (epoch !== state.epoch || !target.isConnected) return;
      target.innerHTML = '<p class="error" role="alert"></p><button type="button">重新读取钥匙</button>';
      errorAt($(".error", target), error);
      $("button", target).onclick = load;
    }
  }
  await load();
}

function renderMcpConnection(key) {
  const target = $("#mcp-content");
  if (!target) return;
  const fields = [
    ["mcp-address", "服务器地址", state.mcpUrl, false],
    ["mcp-transport", "传输方式", "Streamable HTTP", false],
    ["mcp-header-name", "请求头名称", "Authorization", false],
    ["mcp-header-value", "请求头值", key ? "Bearer " + key : "", true],
  ];
  target.innerHTML = fields.map(([id, label, value, secret]) => `<div class="mcp-field"><label for="${id}">${label}<input id="${id}" type="${secret ? 'password' : 'text'}" readonly autocomplete="off"></label><button type="button" data-copy-mcp="${id}" ${value ? '' : 'disabled'} aria-label="复制${label}">复制</button></div>`).join("") + `<p class="muted">${key ? '复制请求头值时会包含 Bearer 和空格。钥匙重新生成后，请在客户端同步替换。' : '请先在“访问钥匙”中生成钥匙，再复制请求头值。'}</p><div class="mcp-instructions"><h3>在客户端中填写</h3><p>在客户端的设置里找到 MCP（也可能叫“MCP 服务器”“工具”），新增一个，名称随意，比如“amber-heart”。传输方式选 <strong>Streamable HTTP</strong>（有的客户端叫 HTTP），服务器地址填上面的地址。</p><p>在自定义请求头（Headers）里新增一条：名称填 Authorization，值粘贴上面完整的请求头值，然后保存并启用。</p><p>客户端需要同时支持 Streamable HTTP 和自定义请求头；只支持 OAuth 登录的客户端暂时接不上。</p></div><p class="error" role="alert"></p>`;
  fields.forEach(([id, , value]) => $("#" + id, target).value = value || "");
  $$("[data-copy-mcp]", target).forEach(button => button.onclick = async () => {
    const input = $("#" + button.dataset.copyMcp, target);
    try {
      await navigator.clipboard.writeText(input.value);
      message("已复制");
    } catch {
      if (input.type === "password") input.type = "text";
      input.focus(); input.select();
      $(".error", target).textContent = "无法自动复制，已选中内容，请手动复制。";
    }
  });
}

function cardDateHTML(value) {
  if (!value) return '<span class="carddate">无内容日期</span>';
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (!parts) return `<span class="carddate">${esc(value)}</span>`;
  const [, year, month, day] = parts;
  const date = `${Number(month)}月${Number(day)}日`;
  const full = `${year}年${date}`;
  const shown = Number(year) === new Date().getFullYear() ? date : full;
  return `<time class="carddate" datetime="${esc(value)}" aria-label="${full}" title="${full}">${shown}</time>`;
}
// "11月14日 · 周六"; another year keeps its year.
function diaryDate(value) {
  const parts = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!parts) return esc(value || "");
  const [, year, month, day] = parts.map(Number);
  const weekday = "日一二三四五六"[new Date(Date.UTC(year, month - 1, day)).getUTCDay()];
  const shown = `${year === new Date().getFullYear() ? "" : year + "年"}${month}月${day}日 · 周${weekday}`;
  return `<time datetime="${esc(value)}">${shown}</time>`;
}
// The model opens a diary with a title line such as "顾言的日记"; the date already says that.
const DIARY_TITLE = /^(#\s.*|#?\s*\S{1,12}的日记[。.]?)$/;
function diaryBodyText(content) {
  const lines = String(content || "").split("\n");
  if (lines.length > 1 && DIARY_TITLE.test(lines[0].trim()) && lines.slice(1).join("").trim()) lines.shift();
  return lines.join("\n").trim();
}
function diaryExcerpt(content) {
  const text = diaryBodyText(content).replace(/\s+/g, " ").trim();
  return text.length > 120 ? text.slice(0, 120) + "…" : text;
}
async function diariesPage(main, epoch, read) {
  let serial = 0;
  let closeEditor;
  // Opened diaries are kept for this visit, so reopening one is instant.
  const details = new Map();
  const timeout = (promise) => Promise.race([promise, new Promise((_, reject) =>
    setTimeout(() => reject(new Error("这篇日记没能打开，请重试。")), 15000))]);
  async function load(page, fetchPage = api) {
    const request = ++serial;
    if (!$(".diary-list", main)) main.innerHTML = '<p class="loading">正在读取日记…</p>';
    const data = await fetchPage(url(`/diaries?page=${page}`));
    if (epoch !== state.epoch || request !== serial) return;
    main.innerHTML = `<div class="diary-list">${data.items.map((item) => `<article class="diary-row" data-diary="${item.id}"><button type="button" class="diary-summary" aria-expanded="false"><span class="diary-date">${diaryDate(item.business_date)}<span class="diary-edited">${item.user_edited ? " · 已修改" : ""}</span></span>${item.excerpt ? `<span class="diary-excerpt">${esc(item.excerpt)}</span>` : ""}</button><div class="diary-reader"></div></article>`).join("") || '<p class="empty">还没有日记。有对话的日子结束后，会在这里留下记录。</p>'}</div>${data.total > data.size ? pager(data) : ""}`;
    $$(".diary-row", main).forEach((row) => $(".diary-summary", row).onclick = () => toggle(row));
    if (data.total > data.size) wirePager(main, data, async (page) => {
      if (!await allowLeave()) return;
      closeEditor?.();
      load(page).catch((error) => {
        if (epoch !== state.epoch) return;
        main.innerHTML = `<p class="error">${esc(error.message)}</p><button type="button">重试</button>`;
        $("button", main).onclick = () => load(page);
      });
    });
  }
  function close(row) {
    row.classList.remove("open");
    row.ticket = null;
    $(".diary-summary", row).setAttribute("aria-expanded", "false");
    $(".diary-reader", row).innerHTML = "";
  }
  // Opens in place: only this diary is fetched, with visible progress and a retry on failure.
  async function toggle(row) {
    if (!await allowLeave()) return;
    closeEditor?.();
    const wasOpen = row.classList.contains("open");
    $$(".diary-row.open", main).forEach(close);
    if (wasOpen) return;
    const ticket = row.ticket = {};
    row.classList.add("open");
    $(".diary-summary", row).setAttribute("aria-expanded", "true");
    const target = $(".diary-reader", row);
    const id = Number(row.dataset.diary);
    if (!details.has(id)) target.innerHTML = '<p class="loading">正在打开…</p>';
    try {
      const diary = details.get(id) || await timeout(api(url(`/diaries/${id}`)));
      details.set(id, diary);
      if (row.ticket === ticket && row.isConnected) show(row, diary);
    } catch (error) {
      if (row.ticket !== ticket || !row.isConnected) return;
      target.innerHTML = `<p class="error">${esc(error.message)}</p><button type="button" class="quiet">重试</button>`;
      $("button", target).onclick = () => { close(row); toggle(row); };
    }
  }
  function show(row, saved) {
    const target = $(".diary-reader", row);
    target.innerHTML = `<p class="diary-body">${esc(diaryBodyText(saved.content))}</p><div class="diary-actions"><button type="button" class="quiet edit-diary">编辑</button></div>`;
    $(".edit-diary", target).onclick = () => edit(row, saved);
  }
  function edit(row, saved) {
    const target = $(".diary-reader", row);
    target.innerHTML = `<form class="diary-form"><div class="readfield"><label for="diary-content">日记正文</label><textarea id="diary-content" name="content" rows="6" maxlength="100000" required>${esc(saved.content)}</textarea></div>${saveStatusHTML()}<div class="diary-actions"><button type="button" class="quiet close-diary">完成编辑</button></div></form>`;
    const form = $("form", target);
    const input = $("textarea", form);
    const saver = autoSave($(".autosave-status", form), () => input.value, async (content) => {
      if (!content.trim()) throw new Error("日记正文不能为空");
      saved = await api(url(`/diaries/${saved.id}`), {
        method: "PATCH",
        body: {content, expected_revision: saved.revision},
      });
      details.set(saved.id, saved);
      $(".diary-edited", row).textContent = " · 已修改";
      const excerpt = $(".diary-excerpt", row);
      if (excerpt) excerpt.textContent = diaryExcerpt(saved.content);
    });
    input.addEventListener("input", () => fitTextarea(input));
    wireAutoSave(form, saver);
    form.onsubmit = (event) => event.preventDefault();
    closeEditor = () => {
      saver.dispose();
      closeEditor = null;
      show(row, saved);
    };
    $(".close-diary", form).onclick = async () => {
      if (await allowLeave()) closeEditor?.();
    };
    fitTextarea(input);
    input.focus();
  }
  await load(1, read);
}

async function memoryCardsPage(main, epoch, view, read, fresh) {
  const initialTopic = view.topic ? String(view.topic) : "";
  let openCard = null;
  const initialFilters = new URLSearchParams({kind: "all", archive: "active", page: "1"});
  if (initialTopic) initialFilters.set("topic", initialTopic);
  const [overview, months, initialCards] = await Promise.all([
    read(url("/overview")), read(url("/archive-months")), read(url("/cards?" + initialFilters)),
  ]);
  if (epoch !== state.epoch) return;
  if (fresh) updateBadge(overview.pending);
  const topics = [...overview.topics].sort((a, b) => a.id - b.id);
  main.innerHTML = `<form id="card-filters"><div class="topic-strip" role="group" aria-label="按主题查看"><button type="button" class="topic-pill" data-topic="">全部<span>${overview.cards}</span></button>${topics.filter((topic) => topic.cards > 0 || String(topic.id) === initialTopic).map((topic) => `<button type="button" class="topic-pill" data-topic="${topic.id}">${esc(topic.name)}<span>${topic.cards}</span></button>`).join("")}</div><input type="hidden" name="topic" value="${initialTopic}"><div class="card-search"><div class="search-input">${icon("search")}<input name="q" aria-label="关键词" type="search" placeholder="搜索记忆…" maxlength="500"></div><button type="button" id="toggle-filters" aria-label="筛选" title="筛选" aria-expanded="false" aria-controls="filter-panel">${icon("filter")}</button></div><div id="filter-panel" class="filter-panel" hidden><label>种类<select name="kind"><option value="all">全部种类</option><option value="atomic">记忆卡片</option><option value="topic_summary">主题总结</option></select></label><label>状态<select name="archive"><option value="active">当前记忆</option><option value="archived">已归档</option><option value="all">全部状态</option></select></label><label id="archive-month-filter" hidden>归档月份<select name="archive_month"><option value="">全部月份</option>${months.map(item => `<option value="${esc(item.month)}">${esc(item.month.replace("-", "年"))}月</option>`).join("")}</select></label>${dateField("从哪天", "since", "", "选择开始日期")}${dateField("到哪天", "until", "", "选择结束日期")}</div><div id="filter-chips" class="filter-chips" aria-label="已选筛选条件"></div></form><div id="cards-result"></div>`;
  const filterForm = $("#card-filters");
  let editing = null;
  wireDates(filterForm);
  const markTopic = () => {
    $$(".topic-pill", filterForm).forEach((pill) => {
      const active = pill.dataset.topic === filterForm.elements.topic.value;
      pill.classList.toggle("active", active);
      pill.setAttribute("aria-pressed", String(active));
    });
  };
  $$(".topic-pill", filterForm).forEach(
    (pill) =>
      (pill.onclick = () => {
        filterForm.elements.topic.value = pill.dataset.topic;
        markTopic();
        load();
      }),
  );
  markTopic();
  $(".topic-pill.active", filterForm)?.scrollIntoView({ block: "nearest", inline: "center" });
  $("#toggle-filters").onclick = () => {
    const panel = $("#filter-panel");
    panel.hidden = !panel.hidden;
    $("#toggle-filters").setAttribute("aria-expanded", String(!panel.hidden));
  };
  function renderChips() {
    const archived = filterForm.elements.archive.value === "archived";
    $("#archive-month-filter", filterForm).hidden = !archived;
    if (!archived) filterForm.elements.archive_month.value = "";
    const names = {
      kind: (value) => labels[value],
      archive: (value) => value === "archived" ? "已归档" : "全部状态",
      archive_month: (value) => "归档于 " + value,
      since: (value) => "从 " + value,
      until: (value) => "到 " + value,
    };
    $("#filter-chips").innerHTML = Object.entries(names)
      .filter(
        ([key]) =>
          filterForm.elements[key].value &&
          (key === "archive" ? filterForm.elements[key].value !== "active" : filterForm.elements[key].value !== "all"),
      )
      .map(
        ([key, format]) =>
          `<button type="button" class="filter-chip" data-filter-key="${key}" aria-label="取消${esc(format(filterForm.elements[key].value))}">${esc(format(filterForm.elements[key].value))}<span aria-hidden="true">×</span></button>`,
      )
      .join("");
    $$(".filter-chip", filterForm).forEach(
      (button) =>
        (button.onclick = () => {
          const input = filterForm.elements[button.dataset.filterKey];
          input.value = input.name === "kind" ? "all" : input.name === "archive" ? "active" : "";
          input.dispatchEvent(new Event("input", { bubbles: true }));
          renderChips();
          load();
        }),
    );
  }
  let querySerial = 0;
  async function load(page = 1, force = false, initialData = null) {
    if (!force && (state.busy || editing && !await allowLeave())) return;
    if (epoch !== state.epoch) return;
    editing?.saver.dispose();
    editing = null;
    const serial = ++querySerial;
    const target = $("#cards-result");
    target.innerHTML = '<p class="loading">正在查找…</p>';
    const filters = new URLSearchParams(new FormData($("#card-filters")));
    for (const [key, value] of [...filters]) if (!value) filters.delete(key);
    filters.set("page", page);
    try {
      const data = initialData ?? await api(url("/cards?" + filters));
      if (epoch !== state.epoch || serial !== querySerial) return;
      if (!data.items.length && page > 1) { await load(page - 1, true); return; }
      target.innerHTML =
        data.items
          .map(
            (card) =>
              `<details class="cardrow" data-card="${card.id}"><summary><div class="cardheading"><h2>${esc(card.title || "未命名卡片")}${card.item_kind === "topic_summary" ? '<span class="tag">主题总结</span>' : ""}</h2>${cardDateHTML(card.source_date)}</div><p class="muted">${esc(card.summary || "")}</p></summary><div class="cardbody"></div></details>`,
          )
          .join("") ||
        (overview.cards
          ? '<p class="empty">没有找到卡片，试试调整关键词或筛选条件。</p>'
          : '<p class="empty">还没有记忆。以前的聊天记录或整理好的记忆，可以<a href="#" data-go="settings" data-section="set-import">去设置里导入</a>。</p>');
      if (!overview.cards) wireGo(target);
      if (data.total > data.size) {
        target.insertAdjacentHTML("beforeend", pager(data));
        wirePager(target, data, load);
      }
      $$(".cardrow", target).forEach((row) =>
        row.addEventListener("toggle", async () => {
          if (!row.open && editing?.row === row) {
            const current = editing;
            if (!await allowLeave()) { row.open = true; return; }
            if (row.open || editing !== current) return;
            closeCardEditor();
          }
          if (!row.open || row.dataset.loaded) return;
          const body = $(".cardbody", row);
          body.textContent = "正在读取全文…";
          try {
            const card = await api(url("/cards/" + row.dataset.card));
            row.dataset.loaded = "1";
            if (!row.isConnected || epoch !== state.epoch) return;
            renderCard(row, card, page);
          } catch (error) {
            body.innerHTML = `<p class="error">${esc(error.message)}；收起后重新展开可重试。</p>`;
          }
        }),
      );
      if (openCard) {
        const wanted = $(`.cardrow[data-card="${openCard}"]`, target);
        openCard = null;
        if (wanted) {
          wanted.open = true;
          wanted.scrollIntoView({ block: "start" });
        }
      }
    } catch (error) {
      if (epoch === state.epoch && serial === querySerial) {
        target.innerHTML = `<p class="error">${esc(error.message)}</p><button id="retry-cards">重试</button>`;
        $("#retry-cards").onclick = () => load(page);
      }
    }
  }
  function closeCardEditor() {
    if (!editing) return;
    const {row, card, page, saver} = editing;
    editing = null;
    saver.dispose();
    renderCard(row, card, page);
  }
  function renderCard(row, card, page) {
    const body = $(".cardbody", row);
    const orderedTopics = [...card.topics].sort((a, b) => (b.id === card.primary_topic_id) - (a.id === card.primary_topic_id));
    const facts = [orderedTopics.map(t => t.name).join("、"), card.is_milestone ? "里程碑" : "", card.archive_reason ? "已归档" : ""].filter(Boolean);
    body.innerHTML = `<p class="card-content">${esc(card.content)}</p>${importOriginalHTML(card.import_original)}${facts.length ? `<p class="meta">${esc(facts.join(" · "))}</p>` : ""}<div class="card-actions">${card.archive_reason ? '<button type="button" class="restore-card quiet">恢复</button>' : ""}${card.item_kind === "atomic" ? '<button type="button" class="edit-card quiet">修改</button><button type="button" class="delete-card danger">删除</button>' : card.topic_summary ? '<button type="button" class="summary-detail quiet">查看吸收清单</button><button type="button" class="revoke-summary danger">整篇撤销</button>' : ""}</div><p class="card-error error" role="alert"></p>${card.item_kind !== "atomic" ? `<p class="muted">${card.topic_summary ? "主题总结只读，可整篇撤销。" : "这篇旧总结没有审核记录，只读。"}</p><div class="summary-detail-body"></div>` : ""}`;
    const restore = $(".restore-card", body);
    if (restore) restore.onclick = async () => {
      if (!await allowLeave()) return;
      closeCardEditor();
      busy(body, true);
      let restored = false;
      try {
        await api(url(`/cards/${card.id}/restore`), {method: "POST", body: {expected_archived_at: card.archived_at}});
        restored = true;
        message("卡片已恢复，从现在重新计时");
      } catch (error) {
        $(".card-error", body).textContent = error.network ? "未能确认恢复结果，请重试或刷新列表核对。" : error.message;
      } finally { busy(body, false); }
      if (restored) await load(page, true);
    };
    if (card.item_kind !== "atomic") {
      $(".summary-detail",body)?.addEventListener("click",async () => {
        try {
          const data = await api(url(`/topic-summaries/${card.topic_summary.id}`));
          $(".summary-detail-body",body).innerHTML = summarySourcesHTML(data.sources);
        } catch (error) {errorAt($(".card-error",body),error);}
      });
      $(".revoke-summary",body)?.addEventListener("click",async () => {
        if (!await allowLeave() || !window.confirm("整篇撤销这篇总结？总结会删除，仍存在的子卡恢复在场，下次按更长的窗口重新整理。")) return;
        closeCardEditor();
        busy(body,true);
        let done = false;
        try {
          await api(url(`/topic-summaries/${card.topic_summary.id}/revoke`),{method:"POST",body:{expected_version:card.topic_summary.version}});
          done = true;
          message("总结已撤销，仍存在的子卡已恢复");
        } catch (error) {$(".card-error",body).textContent = error.network ? "未能确认撤销结果，请重试或刷新核对。" : error.message;}
        finally {busy(body,false);}
        if (done) await load(page,true);
      });
      return;
    }
    $(".edit-card", body).onclick = async () => {
      if (!await allowLeave()) return;
      closeCardEditor();
      try {
        const configuration = await api(url("/configuration"));
        if (!row.isConnected || !row.open || epoch !== state.epoch) return;
        // A second click can have opened another editor while configuration loaded.
        if (editing) return;
        const form = document.createElement("form");
        form.className = "card-editor";
        form.innerHTML = `<div class="readfield title"><label class="fieldcaption" for="card-title-${card.id}">标题</label><textarea id="card-title-${card.id}" name="title" rows="1" maxlength="300" required>${esc(card.title || "")}</textarea></div><div class="draft-props"><div class="prop"><div class="prop-head"><span class="prop-label">主题</span><label class="check milestone"><input type="checkbox" name="is_milestone" ${card.is_milestone ? "checked" : ""}>里程碑</label></div><div class="topic-picks" role="group" aria-label="卡片主题"></div></div></div><p class="muted card-edit-note">正文只读。内容有误时，可以删除这张卡片。</p>${saveStatusHTML()}<div class="card-actions"><button type="button" class="close-card quiet">完成编辑</button></div>`;
        $(".card-actions", body).hidden = true;
        $(".card-error", body).textContent = "";
        body.append(form);
        const chosenTopics = topicPicker($(".topic-picks", form), configuration, orderedTopics.map(t => t.id));
        const titleInput = $("textarea", form);
        fitTextarea(titleInput);
        titleInput.oninput = () => fitTextarea(titleInput);
        const collect = () => ({
          title: titleInput.value,
          topic_ids: chosenTopics(),
          is_milestone: form.elements.is_milestone.checked,
        });
        let savedChanges = false;
        const saver = autoSave($(".autosave-status", form), collect, async (payload) => {
          if (!payload.title.trim()) throw new Error("卡片标题不能为空");
          await api(url(`/cards/${card.id}`), {method: "PATCH", body: payload});
          savedChanges = true;
          card.title = payload.title.trim();
          card.primary_topic_id = payload.topic_ids[0];
          card.topics = configuration.topics.filter(topic => payload.topic_ids.includes(topic.id));
          card.is_milestone = payload.is_milestone;
          $("summary h2", row).textContent = card.title;
        });
        editing = {row, card, page, saver};
        wireAutoSave(form, saver);
        form.onsubmit = (event) => event.preventDefault();
        $(".close-card", form).onclick = async () => {
          if (!await allowLeave() || editing?.saver !== saver) return;
          if (savedChanges) {
            openCard = card.id;
            await load(page, true);
          } else {
            closeCardEditor();
          }
        };
        titleInput.focus();
      } catch (error) { errorAt($(".card-error", body), error); }
    };
    $(".delete-card", body).onclick = async () => {
      if (!await allowLeave() || !window.confirm(`彻底删除《${card.title || "未命名卡片"}》？删除后无法恢复。重复抽卡或导入时，这张卡片仍可能再次出现。`)) return;
      closeCardEditor();
      busy(body, true);
      let deleted = false;
      try {
        await api(url(`/cards/${card.id}`), {method: "DELETE", body: {}});
        deleted = true;
        message("卡片已彻底删除");
      } catch (error) {
        $(".card-error", body).textContent = error.network ? "未能确认删除结果，请稍后刷新列表核对。" : `${error.message}。删除未完成，可稍后重试。`;
      } finally { busy(body, false); }
      if (deleted) await load(page, true);
    };
  }
  filterForm.onsubmit = (event) => {
    event.preventDefault();
    renderChips();
    load();
  };
  filterForm.addEventListener("change", () => {
    renderChips();
    load();
  });
  filterForm.elements.q.addEventListener("search", () => load());
  filterForm.elements.q.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      // Close the phone keyboard so the results and bottom nav come back.
      event.target.blur();
      renderChips();
      load();
    }
  });
  renderChips();
  await load(1, false, initialCards);
}
function loginPage() {
  document.title = "登录 · Amber Heart";
  root.innerHTML =
    '<main class="login"><div class="brand">' + brandHTML() + '</div><h1>回到你的记忆库</h1><p>使用管理员密码，管理这个实例的全部空间。</p><form><label>管理员密码<input type="password" name="password" autocomplete="current-password" required autofocus maxlength="256"></label><label class="check"><input type="checkbox" name="remember">记住这台设备 · 30 天</label><p class="error" role="alert"></p><button class="primary">登录</button><p class="muted">未勾选时，会话最长保留 12 小时。</p></form></main>';
  const form = $("form");
  form.onsubmit = async (event) => {
    event.preventDefault();
    busy(form, true);
    $(".error", form).textContent = "";
    try {
      await api("/api/login", {
        method: "POST",
        body: {
          password: form.elements.password.value,
          remember: form.elements.remember.checked,
        },
      });
      // Logged in: nothing to keep, so leaving must not trigger the browser's
      // "unsaved changes" prompt that busy() would otherwise cause.
      state.busy = false;
      location.replace(appPath("/overview"));
    } catch (error) {
      errorAt($(".error", form), error);
    } finally {
      busy(form, false);
    }
  };
}
async function start() {
  if (pagePath() === "/login") {
    loginPage();
    return;
  }
  try {
    const session = await api("/api/session");
    state.csrf = session.csrf;
    state.fixed = session.fixed_models;
    state.mcpUrl = session.mcp_url;
    [state.spaces, state.contract] = await Promise.all([
      api("/api/spaces"),
      api("/api/contract"),
    ]);
    const saved = sessionStorage.getItem("memory-space");
    state.space =
      state.spaces.find((s) => s.id === saved)?.id || state.spaces[0]?.id || "";
    const page = pageFromPath();
    if (pagePath() !== "/" + page) history.replaceState({}, "", pageURL(page));
    await navigate(page, false);
  } catch (error) {
    if (error.status === 401) {
      location.replace(appPath("/login"));
      return;
    }
    root.innerHTML = `<p class="empty error">${esc(error.message)}</p><button id="retry-start">重新连接</button>`;
    $("#retry-start").onclick = start;
  }
}
start();
