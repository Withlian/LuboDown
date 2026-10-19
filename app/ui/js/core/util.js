/* 核心工具：DOM 查询、转义、图标、格式化、本地偏好存储。全部为无状态纯函数。 */
(function (Lubo) {
  "use strict";

  const TZ8 = 8 * 3600 * 1000;   // B 站时间按北京时间展示
  const DAY = 86400;
  const pad = (n) => String(n).padStart(2, "0");
  const beijing = (unix) => new Date(unix * 1000 + TZ8);

  const util = {
    DAY,
    $: (sel, root = document) => root.querySelector(sel),
    $$: (sel, root = document) => Array.from(root.querySelectorAll(sel)),

    esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    },

    icon(name) {
      return `<svg class="ico"><use href="#i-${name}"/></svg>`;
    },

    fmtTime(unix) {
      if (!unix) return "—";
      const d = beijing(unix);
      return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} `
           + `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    },
    fmtShort(unix) {   // 10-17 23:14
      const d = beijing(unix);
      return `${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    },
    dayKey(unix) {     // 北京时间的自然日序号，用于按天聚合
      return Math.floor((unix * 1000 + TZ8) / (DAY * 1000));
    },
    fmtDayLabel(dayKey) {
      const d = new Date(dayKey * DAY * 1000);
      return `${d.getUTCMonth() + 1}/${d.getUTCDate()}`;
    },
    fmtDur(sec) {
      if (!sec) return "—";
      sec = Math.round(sec);
      const h = Math.floor(sec / 3600), m = Math.round((sec % 3600) / 60);
      return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
    },
    fmtBytes(b) {
      if (!b) return "0";
      if (b > 1024 ** 3) return (b / 1024 ** 3).toFixed(2) + " GB";
      if (b > 1024 ** 2) return (b / 1024 ** 2).toFixed(1) + " MB";
      return (b / 1024).toFixed(0) + " KB";
    },
    fmtSpeed(bps) {
      return bps ? util.fmtBytes(bps) + "/s" : "";
    },
    /** 速度文案：有速度显示速度，无速度但在跑（合成中）则否则空闲 */
    speedText(bps, running) {
      return bps ? util.fmtBytes(bps) + "/s" : running ? "合成中" : "空闲";
    },
    fmtCountdown(ts) {
      const remain = Math.max(0, Math.round(ts - Date.now() / 1000));
      const h = Math.floor(remain / 3600), m = Math.floor((remain % 3600) / 60), s = remain % 60;
      return `${h ? h + " 时 " : ""}${m} 分 ${pad(s)} 秒`;
    },
    unixToInput(ts) {
      const d = beijing(ts);
      return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    },
    inputToUnix(v) {
      return v ? Math.floor(Date.parse(v + ":00+08:00") / 1000) : 0;
    },

    /* 本地偏好：隐私模式或存储被禁用时静默降级为仅本次生效 */
    pref: {
      get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
      set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* 忽略 */ } },
    },
  };

  Lubo.util = util;
})(window.Lubo = window.Lubo || {});
