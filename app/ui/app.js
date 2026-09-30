/* LuboDown 前端逻辑：轮询状态、渲染列表、设置交互 */
"use strict";

const TZ8 = 8 * 3600 * 1000;
const $ = (id) => document.getElementById(id);
const api = () => window.pywebview.api;

const STATUS_TEXT = {
  new: "未下载", queued: "排队中", downloading: "下载中", merging: "合成中",
  done: "已完成", failed: "失败", paused: "已暂停",
};

let lastSessionsHash = "";
let lastLogCount = -1;
let settingsLoaded = false;
let pendingRemove = null;   // 两段式删除确认
let countdownBase = null;

/* ---------- 工具 ---------- */
function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 2600);
  setTimeout(() => el.remove(), 3000);
}

function call(name, ...args) {
  return api()[name](...args).catch((e) => {
    toast(`调用 ${name} 失败：${e}`, "err");
    return null;
  });
}

function pad(n) { return String(n).padStart(2, "0"); }

function fmtTime(unix) {
  if (!unix) return "—";
  const d = new Date(unix * 1000 + TZ8);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
       + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}
function fmtDate(unix) {
  return fmtTime(unix).split(" ")[0];
}
function fmtDur(sec) {
  if (!sec && sec !== 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
function fmtBytes(b) {
  if (!b) return "0";
  if (b > 1024 ** 3) return (b / 1024 ** 3).toFixed(2) + " GB";
  if (b > 1024 ** 2) return (b / 1024 ** 2).toFixed(1) + " MB";
  return (b / 1024).toFixed(0) + " KB";
}
function fmtSpeed(bps) {
  if (!bps) return "";
  return fmtBytes(bps) + "/s";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------- 页面切换 ---------- */
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".page").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    $("page-" + btn.dataset.page).classList.add("active");
    if (btn.dataset.page === "settings") loadSettingsPage();
  });
});

/* ---------- 场次列表 ---------- */
function sessActions(s) {
  const st = s.status;
  let html = "";
  if (st === "done") {
    html += `<button class="btn small" data-act="open" data-key="${s.live_key}">打开文件</button>`;
  } else if (st === "downloading" || st === "queued" || st === "merging") {
    html += `<button class="btn small" data-act="pause" data-key="${s.live_key}">暂停</button>`;
  } else if (st === "paused" || st === "failed") {
    html += `<button class="btn small primary" data-act="start" data-key="${s.live_key}">${st === "paused" ? "继续" : "重试"}</button>`;
  } else {
    html += `<button class="btn small primary" data-act="start" data-key="${s.live_key}">下载</button>`;
  }
  const label = pendingRemove === s.live_key ? "确认删除?" : "删除";
  html += `<button class="btn small danger" data-act="remove" data-key="${s.live_key}">${label}</button>`;
  return html;
}

function progressHtml(jv) {
  const pct = jv.percent || 0;
  const est = jv.total_bytes ? ` / 约 ${fmtBytes(jv.total_bytes)}` : "";
  return `<div class="progress-wrap">
    <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
    <div class="progress-text">
      <span>${jv.done_segs} / ${jv.total_segs} 段 · ${fmtBytes(jv.done_bytes)}${est}</span>
      <span>${pct}% · ${fmtSpeed(jv.speed_bps)}</span>
    </div>
  </div>`;
}

function sessCard(s) {
  const dur = fmtDur(s.duration || (s.end_time > s.start_time ? s.end_time - s.start_time : 0));
  const alert = s.alert_message ? `<div class="sess-alert" title="${esc(s.alert_message)}">⚠ ${esc(s.alert_message)}</div>` : "";
  const err = s.error ? `<div class="sess-alert" title="${esc(s.error)}">✖ ${esc(s.error)}</div>` : "";
  const liveJob = ["downloading", "merging", "queued"].includes(s.status) && s.total_segs;
  const cover = s.cover
    ? `<img class="cover" src="${esc(s.cover)}" referrerpolicy="no-referrer" onerror="this.removeAttribute('src')">`
    : `<div class="cover"></div>`;
  return `<div class="sess" data-key="${s.live_key}">
    ${cover}
    <div class="sess-info">
      <div class="sess-title">
        <span class="badge ${s.status}">${STATUS_TEXT[s.status] || s.status}</span>
        ${esc(s.title || "未命名直播")}
      </div>
      <div class="sess-meta">
        <span class="sess-anchor">${esc(s.name || s.uid)}</span>
        <span class="mono">${fmtTime(s.start_time)} ~ ${fmtTime(s.end_time)}</span>
        <span>时长 ${dur}</span>
      </div>
      ${alert}${err}
      ${liveJob ? progressHtml(s) : ""}
    </div>
    <div class="sess-actions">${sessActions(s)}</div>
  </div>`;
}

let renderedCards = {};   // live_key -> 已渲染卡片 HTML

function renderSessions(sessions) {
  const box = $("session-list");
  if (!sessions.length) {
    if (box.dataset.mode !== "empty") {
      box.innerHTML = `<div class="empty">暂无回放数据，点击「立即检查新回放」拉取</div>`;
      box.dataset.mode = "empty";
      renderedCards = {};
      renderSessions._sig = "";
    }
    return;
  }
  // 骨架（日期分组 + 卡片占位）仅在结构变化时重建
  const sig = sessions.map((s) => s.live_key).join(",")
    + "|" + sessions.map((s) => fmtDate(s.start_time)).join(",");
  if (renderSessions._sig !== sig) {
    let html = "", lastDate = "";
    for (const s of sessions) {
      const d = fmtDate(s.start_time);
      if (d !== lastDate) {
        html += `<div class="date-head">${d}</div>`;
        lastDate = d;
      }
      html += `<div data-slot="${s.live_key}"></div>`;
    }
    box.innerHTML = html;
    box.dataset.mode = "list";
    renderSessions._sig = sig;
    renderedCards = {};
  }
  // 每张卡片只在其内容变化时整体替换（进度更新不打扰其他卡片）
  for (const s of sessions) {
    const h = sessCard(s);
    if (renderedCards[s.live_key] !== h) {
      const slot = box.querySelector(`[data-slot="${s.live_key}"]`);
      if (slot) slot.innerHTML = h;
      renderedCards[s.live_key] = h;
    }
  }
}

$("session-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const key = btn.dataset.key;
  const act = btn.dataset.act;
  if (act === "start") {
    const r = await call("start_download", key);
    if (r && r.ok) toast("已加入下载队列", "ok");
  } else if (act === "pause") {
    await call("pause_download", key);
    toast("暂停请求已发送");
  } else if (act === "open") {
    const s = stateCache.sessions.find((x) => x.live_key === key);
    if (s && s.file) await call("open_path", s.file, true);
  } else if (act === "remove") {
    const s = stateCache.sessions.find((x) => x.live_key === key);
    if (pendingRemove !== key) {
      pendingRemove = key;
      setTimeout(() => { pendingRemove = null; renderSessions(stateCache.sessions); }, 3000);
      renderSessions(stateCache.sessions);
      return;
    }
    pendingRemove = null;
    const delFiles = s && s.status === "done";
    const r = await call("delete_session", key, delFiles);
    if (r && r.ok) { toast("已删除", "ok"); refresh(); }
    else if (r) toast(r.message || "删除失败", "err");
  }
});

/* ---------- 设置页 ---------- */
function setVal(id, value) {
  const el = $(id);
  if (document.activeElement === el) return;  // 正在编辑时不用状态覆盖输入
  el.value = value;
}

function loadSettingsPage() {
  const s = stateCache.settings || {};
  setVal("in-download-dir", s.download_dir || "");
  setVal("in-ffmpeg", s.ffmpeg_path || "");
  $("sel-concurrency").value = String(s.concurrency || 6);
  $("sw-keep-parts").checked = !!s.keep_parts;
  $("in-sessdata").placeholder = s.has_sessdata ? "已保存" : "尚未填写";
  $("in-bili-jct").placeholder = s.has_bili_jct ? "已保存" : "尚未填写";
  renderAnchors(s.anchors || []);
}

function renderAnchors(anchors) {
  $("anchor-list").innerHTML = anchors.map((a) =>
    `<span class="anchor-chip">${esc(a.name)}<span style="color:var(--muted);font-size:11px">${a.uid}</span>
     <button data-uid="${a.uid}" title="移除">✕</button></span>`).join("")
    || `<span class="hint">尚未添加主播</span>`;
}
$("anchor-list").addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-uid]");
  if (!btn) return;
  const r = await call("remove_anchor", Number(btn.dataset.uid));
  if (r && r.ok) { toast("已移除", "ok"); await refresh(); loadSettingsPage(); }
});

$("btn-add-anchor").addEventListener("click", async () => {
  const v = $("in-uid").value.trim();
  if (!v) return;
  const r = await call("add_anchor", v);
  if (r && r.ok) {
    toast(`已添加：${r.name}`, "ok");
    $("in-uid").value = "";
    await refresh();
    loadSettingsPage();   // 立即刷新主播列表，不用切页面
  } else if (r) toast(r.message || "添加失败", "err");
});
$("in-uid").addEventListener("keydown", (e) => { if (e.key === "Enter") $("btn-add-anchor").click(); });

$("btn-save-cred").addEventListener("click", async () => {
  const patch = {};
  if ($("in-sessdata").value.trim()) patch.sessdata = $("in-sessdata").value.trim();
  if ($("in-bili-jct").value.trim()) patch.bili_jct = $("in-bili-jct").value.trim();
  if (!Object.keys(patch).length) { toast("没有需要保存的新凭证", ""); return; }
  const r = await call("save_settings", patch);
  if (r && r.ok) {
    $("in-sessdata").value = $("in-bili-jct").value = "";
    toast("凭证已保存", "ok");
    refresh();
  }
});

$("btn-verify").addEventListener("click", async () => {
  const el = $("verify-result");
  el.textContent = "检测中…";
  const r = await call("verify_cookie");
  if (r) {
    el.textContent = r.message;
    el.style.color = r.ok ? "var(--green)" : "var(--red)";
  } else el.textContent = "检测失败";
});

$("btn-browse").addEventListener("click", async () => {
  const r = await call("choose_folder");
  if (r && r.ok) {
    $("in-download-dir").value = r.path;
    const s = await call("save_settings", { download_dir: r.path });
    if (s && s.ok) toast("下载目录已保存", "ok");
  }
});
$("in-download-dir").addEventListener("change", async (e) => {
  const v = e.target.value.trim();
  if (!v) return;
  const s = await call("save_settings", { download_dir: v });
  if (s && s.ok) toast("下载目录已保存", "ok");
});
$("in-ffmpeg").addEventListener("change", async (e) => {
  const r = await call("save_settings", { ffmpeg_path: e.target.value.trim() || "ffmpeg" });
  if (r && r.ok) toast("ffmpeg 路径已保存", "ok");
});
$("sel-concurrency").addEventListener("change", async (e) => {
  await call("save_settings", { concurrency: Number(e.target.value) });
  toast("并发数已保存", "ok");
});
$("sw-keep-parts").addEventListener("change", async (e) => {
  await call("save_settings", { keep_parts: e.target.checked });
  toast(e.target.checked ? "将保留分段文件" : "分段文件将在合成后清除", "ok");
});

document.querySelectorAll(".pw-eye").forEach((b) => {
  b.addEventListener("click", () => {
    const inp = $(b.dataset.for);
    inp.type = inp.type === "password" ? "text" : "password";
  });
});

/* ---------- 定时任务页 ---------- */
function unixToInput(ts) {
  const d = new Date(ts * 1000 + TZ8);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function syncScheduleControls(s) {
  const st = stateCache.settings || {};
  $("sw-auto-check").checked = !!st.auto_check;
  $("auto-check-label").textContent = st.auto_check ? "已启用" : "已关闭";
  $("sw-auto-download").checked = !!st.auto_download;
  $("auto-download-label").textContent = st.auto_download ? "已开启" : "已关闭";
  $("sel-interval").value = String(st.check_interval_min || 60);
  const since = Number(st.auto_download_since || 0);
  setVal("in-since", since ? unixToInput(since) : "");
  countdownBase = s.scheduler && s.scheduler.next_run_ts ? s.scheduler.next_run_ts : null;
  updateCountdown();
}

function updateCountdown() {
  const el = $("next-run");
  if (!el) return;
  if (stateCache.scheduler && stateCache.scheduler.checking) { el.textContent = "检查中…"; return; }
  if (!countdownBase) { el.textContent = "—"; return; }
  const remain = Math.max(0, Math.round(countdownBase - Date.now() / 1000));
  const hh = Math.floor(remain / 3600), mm = Math.floor((remain % 3600) / 60), ss = remain % 60;
  el.textContent = `${fmtTime(countdownBase)}（${hh ? hh + "时" : ""}${mm}分${ss}秒后）`;
}
setInterval(updateCountdown, 1000);

$("sw-auto-check").addEventListener("change", async (e) => {
  await call("save_settings", { auto_check: e.target.checked });
  toast(e.target.checked ? "定时检查已启用" : "定时检查已关闭", "ok");
  refresh();
});
$("sw-auto-download").addEventListener("change", async (e) => {
  await call("save_settings", { auto_download: e.target.checked });
  toast(e.target.checked ? "自动下载已开启" : "自动下载已关闭", "ok");
  refresh();
});
$("in-since").addEventListener("change", async (e) => {
  const v = e.target.value;
  const unix = v ? Math.floor(Date.parse(v + ":00+08:00") / 1000) : 0;
  const r = await call("save_settings", { auto_download_since: unix });
  if (r && r.ok) toast(unix ? "起始时间已设置，之后开播的场次才会自动下载" : "起始时间已清除", "ok");
  refresh();
});
$("btn-clear-since").addEventListener("click", async () => {
  $("in-since").value = "";
  await call("save_settings", { auto_download_since: 0 });
  toast("起始时间已清除", "ok");
  refresh();
});
$("sel-interval").addEventListener("change", async (e) => {
  await call("save_settings", { check_interval_min: Number(e.target.value) });
  toast("间隔已更新", "ok");
  refresh();
});

/* ---------- 日志 ---------- */
function renderLogs(logs) {
  if (logs.length === lastLogCount) return;
  lastLogCount = logs.length;
  const box = $("log-box");
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  box.innerHTML = logs.map((l) =>
    `<div class="log-line ${l.level}"><span class="t">${esc(l.t)}</span><span class="msg">${esc(l.msg)}</span></div>`
  ).join("");
  if (nearBottom) box.scrollTop = box.scrollHeight;
}

/* ---------- 侧栏状态点 ---------- */
function renderFoot(s) {
  const hasCookie = s.settings && s.settings.has_sessdata;
  $("dot-cookie").className = "dot" + (hasCookie ? " ok" : "");
  $("foot-cookie").textContent = hasCookie ? "Cookie 已配置" : "Cookie 未配置";
  $("dot-ffmpeg").className = "dot" + (s.ffmpeg_ok ? " ok" : " warn");
  $("foot-ffmpeg").textContent = s.ffmpeg_ok ? "ffmpeg 正常" : "ffmpeg 未找到（去设置）";
}

/* ---------- 顶部按钮 ---------- */
$("btn-check").addEventListener("click", async () => {
  const r = await call("check_now");
  if (r && r.ok) toast("开始检查…", "ok");
  else if (r) toast(r.message, "err");
});
$("btn-check-2").addEventListener("click", () => $("btn-check").click());
$("btn-refresh").addEventListener("click", async () => {
  const r = await call("refresh_list");
  if (r && r.ok) toast(`刷新完成：共 ${r.total} 场，新增 ${r.added} 场`, "ok");
  else if (r) toast(r.message || "刷新失败", "err");
});

/* ---------- 状态轮询 ---------- */
let stateCache = { settings: {}, sessions: [], scheduler: {} };

async function refresh() {
  const s = await call("get_state");
  if (!s) return;
  stateCache = s;

  const hash = JSON.stringify([s.sessions, s.settings.has_sessdata, s.settings.has_bili_jct,
    s.ffmpeg_ok, s.scheduler.checking, s.scheduler.next_run_ts,
    s.settings.download_dir, (s.settings.anchors || [])]);
  if (hash !== lastSessionsHash) {
    lastSessionsHash = hash;
    renderSessions(s.sessions);
    syncScheduleControls(s);
    renderFoot(s);
    if (settingsLoaded) loadSettingsPage();
  }
  renderLogs(s.logs || []);
}

function pollLoop() {
  refresh().finally(() => setTimeout(pollLoop, 1500));
}

function init() {
  settingsLoaded = true;
  refresh();
  pollLoop();
}

if (window.pywebview) {
  init();
} else {
  window.addEventListener("pywebviewready", init);
  setTimeout(() => { if (!window.pywebview) $("session-list").innerHTML =
    `<div class="empty">等待应用后端就绪…（若长时间无响应请重启程序）</div>`; }, 3000);
}
