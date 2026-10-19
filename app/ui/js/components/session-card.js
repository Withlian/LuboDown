/* 场次卡片（回放列表）：只展示场次信息与状态，下载过程（进度、暂停、重试）统一在下载页管理。
   状态定义见 core/status.js；进度计算由下载页共用。 */
(function (Lubo) {
  "use strict";

  const { esc, icon, fmtShort, fmtDur, DAY } = Lubo.util;
  const { Status } = Lubo;
  const KEEP_DAYS = 14;   // B 站回放保留时长

  class SessionCard {
    /** 封面图（回放列表卡片与下载页行共用；加载失败自行移除） */
    static cover(s) {
      return s.cover ? `<img src="${esc(s.cover)}" referrerpolicy="no-referrer" alt="" onerror="this.remove()">` : "";
    }

    static html(s, confirming = false) {
      const { label, glyph } = Status.of(s);
      const key = esc(s.live_key);
      const dur = s.duration || (s.end_time > s.start_time ? s.end_time - s.start_time : 0);
      return `<article class="sc${s.status === "downloading" ? " is-active" : ""}">
        <div class="sc-badge">${icon(glyph)}${SessionCard.cover(s)}</div>
        <span class="chip ${esc(s.status)}">${esc(label)}</span>
        <div class="sc-actions">${SessionCard.actions(s, key, confirming)}</div>
        <h4 class="sc-title" title="${esc(s.title)}">${esc(s.title || "未命名直播")}</h4>
        <p class="sc-sub"><b>${esc(s.name || s.uid)}</b> · ${fmtShort(s.start_time)}</p>
        <div class="sc-foot"><span class="sc-meta">时长 ${fmtDur(dur)}</span>${SessionCard.expiry(s)}</div>
      </article>`;
    }

    /** 进度百分比与进度条色调（下载页使用） */
    static progress(s) {
      if (s.status === "done") return { pct: 100, tone: "done" };
      const pct = +Math.min(100, s.percent || 0).toFixed(1);
      const tone = s.status === "merging" ? "merging live" : Status.ACTIVE.has(s.status) ? "live" : "";
      return { pct, tone };
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
      // 未下载的场次在这里开始下载；已有下载任务的跳到下载页对应标签查看
      const main = Status.tab(s) ? btn("goto", "arrow-right", "在下载页查看") : btn("start", "download", "下载");
      // 已完成的场次确认后会连视频文件一起删，提示必须点明
      const del = confirming
        ? `<button class="icon-btn del confirming" data-act="remove" data-key="${key}" title="${s.status === "done" ? "将同时删除视频文件" : "再次点击确认"}">${icon("trash")}确认</button>`
        : btn("remove", "trash", "删除", "del");
      return main + del;
    }
  }

  Lubo.SessionCard = SessionCard;
})(window.Lubo = window.Lubo || {});
