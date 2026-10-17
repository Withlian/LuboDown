/* 场次卡片：生成卡片 HTML、计算结构签名（不含进度数值），以及进度的原地更新。 */
(function (Lubo) {
  "use strict";

  const { esc, icon, fmtShort, fmtDur, fmtBytes, fmtSpeed, DAY } = Lubo.util;
  const ACTIVE = Lubo.StateStore.ACTIVE;
  const KEEP_DAYS = 14;   // B 站回放保留时长

  const STATUS = {
    new: ["未下载", "video"], queued: ["排队中", "clock"], downloading: ["下载中", "download"],
    merging: ["合成中", "zap"], done: ["已完成", "check"], failed: ["失败", "alert"], paused: ["已暂停", "pause"],
  };
  const ZERO_PROGRESS = { done_segs: 0, done_bytes: 0, total_bytes: 0, speed_bps: 0, percent: 0 };

  class SessionCard {
    static html(s, confirming = false) {
      const [label, glyph] = STATUS[s.status] || [s.status, "video"];
      const key = esc(s.live_key);
      const cover = s.cover
        ? `<img src="${esc(s.cover)}" referrerpolicy="no-referrer" alt="" onerror="this.remove()">`
        : "";
      const err = s.error
        ? `<div class="sc-err${s.status === "failed" ? "" : " soft"}" title="${esc(s.error)}">${esc(s.error)}</div>`
        : "";
      const { pct, tone } = SessionCard.progress(s);
      return `<article class="sc${s.status === "downloading" ? " is-active" : ""}">
        <div class="sc-badge">${icon(glyph)}${cover}</div>
        <span class="chip ${esc(s.status)}">${esc(label)}</span>
        <div class="sc-actions">${SessionCard.actions(s, key, confirming)}</div>
        <h4 class="sc-title" title="${esc(s.title)}">${esc(s.title || "未命名直播")}</h4>
        <p class="sc-sub"><b>${esc(s.name || s.uid)}</b> · ${fmtShort(s.start_time)}</p>
        <div class="sc-progress">
          <div class="sc-row"><span>进度</span><span class="sc-pct">${pct}%</span></div>
          <div class="bar"><div class="bar-fill ${tone}"></div></div>
        </div>
        <div class="sc-foot"><span class="sc-meta">${SessionCard.meta(s)}</span>${SessionCard.expiry(s)}</div>
        ${err}
      </article>`;
    }

    /** 结构签名：进度数值置零后的 HTML，仅进度变化时签名不变，走原地更新 */
    static structure(s, confirming) {
      return SessionCard.html({ ...s, ...ZERO_PROGRESS }, confirming);
    }

    static patch(card, s) {
      const { pct } = SessionCard.progress(s);
      card.querySelector(".bar-fill").style.transform = `scaleX(${pct / 100})`;
      const pctEl = card.querySelector(".sc-pct");
      const meta = card.querySelector(".sc-meta");
      const metaText = SessionCard.meta(s);
      if (pctEl.textContent !== `${pct}%`) pctEl.textContent = `${pct}%`;
      if (meta.textContent !== metaText) meta.textContent = metaText;
    }

    static progress(s) {
      if (s.status === "done") return { pct: 100, tone: "done" };
      const pct = +Math.min(100, s.percent || 0).toFixed(1);
      const tone = s.status === "merging" ? "merging live" : ACTIVE.has(s.status) ? "live" : "";
      return { pct, tone };
    }

    static meta(s) {
      if (ACTIVE.has(s.status) && s.total_segs) {
        const total = s.total_bytes ? ` / ${fmtBytes(s.total_bytes)}` : "";
        const speed = s.speed_bps ? ` · ${fmtSpeed(s.speed_bps)}` : "";
        return `${fmtBytes(s.done_bytes)}${total}${speed}`;
      }
      const dur = s.duration || (s.end_time > s.start_time ? s.end_time - s.start_time : 0);
      return `时长 ${fmtDur(dur)}`;
    }

    static expiry(s) {
      if (s.status === "done") return `<span class="pill-soft saved">已保存</span>`;
      const left = Math.ceil((s.start_time + KEEP_DAYS * DAY - Date.now() / 1000) / DAY);
      if (left <= 0) return `<span class="pill-soft urgent">即将过期</span>`;
      return `<span class="pill-soft${left <= 3 ? " urgent" : ""}">剩 ${left} 天</span>`;
    }

    static actions(s, key, confirming) {
      const btn = (act, glyph, tip, cls = "go") =>
        `<button class="icon-btn ${cls}" data-act="${act}" data-key="${key}" title="${tip}">${icon(glyph)}</button>`;
      let main;
      if (s.status === "done") main = btn("open", "folder-open", "打开文件");
      else if (ACTIVE.has(s.status)) main = btn("pause", "pause", "暂停");
      else if (s.status === "paused") main = btn("start", "play", "继续");
      else if (s.status === "failed") main = btn("start", "retry", "重试");
      else main = btn("start", "download", "下载");
      // 已完成的场次确认后会连视频文件一起删，提示必须点明
      const del = confirming
        ? `<button class="icon-btn del confirming" data-act="remove" data-key="${key}" title="${s.status === "done" ? "将同时删除视频文件" : "再次点击确认"}">${icon("trash")}确认</button>`
        : btn("remove", "trash", "删除", "del");
      return main + del;
    }
  }

  Lubo.SessionCard = SessionCard;
})(window.Lubo = window.Lubo || {});
