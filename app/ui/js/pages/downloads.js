/* 下载页：实时速度主视觉卡（曲线 + 下载中/排队/已完成计数）与下载任务列表（进行中 / 已暂停与失败 / 已完成）。 */
(function (Lubo) {
  "use strict";

  const { $, esc, icon, fmtShort, fmtDur, fmtBytes, fmtSpeed, speedText } = Lubo.util;
  const { Motion, SessionCard, Sparkline, Status, Tabs } = Lubo;
  // 每个标签对应状态模型的一个 tab 维度，过滤天然对齐（见 core/status.js）
  const VIEWS = {
    active: { empty: ["download", "暂无进行中的下载，可在回放列表中开始下载"] },
    stopped: { empty: ["pause", "没有已暂停或失败的任务"] },
    done: { empty: ["check", "还没有下载完成的回放"] },
  };
  const ZERO_PROGRESS = { done_segs: 0, done_bytes: 0, total_bytes: 0, speed_bps: 0, percent: 0 };

  class DownloadsPage extends Lubo.Page {
    constructor(app, root) {
      super(app, root);
      this.list = $("#dlp-list");
      this.tabs = new Tabs($("#dlp-tabs"));
      this.chart = new Sparkline($("#dlp-chart"));
      this.rows = new Map();     // live_key -> 结构签名（不含进度数值）
      this.listSig = null;

      this.tabs.addEventListener("change", () => this.render(this.store.state));
      this.bind({ "#btn-open-dir": ["click", () => this.api.call("open_path", "", false)] });
      this.list.addEventListener("click", (e) => this._onAction(e));
    }

    get title() { return "下载"; }

    /** 从回放列表跳转过来时切到对应标签 */
    showTab(tab) { if (VIEWS[tab]) this.tabs.select(tab); }

    render(state) {
      const sessions = state.sessions || [];
      this._renderHero(sessions);
      const view = VIEWS[this.tabs.value] || VIEWS.active;
      const tab = this.tabs.value;
      const list = sessions.filter((s) => Status.of(s).tab === tab).sort((a, b) =>
        (Status.of(a).order ?? 9) - (Status.of(b).order ?? 9) || b.start_time - a.start_time);
      this._renderList(list, view.empty);
    }

    /* ---------- 速度主视觉卡 ---------- */
    _renderHero(sessions) {
      const running = sessions.filter((s) => Status.RUNNING.has(s.status));
      const queued = sessions.filter((s) => s.status === "queued").length;
      this.chart.draw(this.store.speeds);
      $("#dlp-speed").textContent = speedText(this.store.speed, running.length);
      const cur = running[0];
      $("#dlp-sub").textContent = cur
        ? `${running.length} 个任务进行中 · ${cur.title || "未命名直播"}`
        : queued ? `${queued} 个任务排队中` : "暂无进行中的任务";
      Motion.countTo($("#dlp-running"), running.length);
      Motion.countTo($("#dlp-queued"), queued);
      Motion.countTo($("#dlp-done"), sessions.filter((s) => s.status === "done").length);
    }

    /* ---------- 任务列表 ---------- */
    _renderList(list, [emptyIcon, emptyText]) {
      $("#dlp-count").textContent = list.length;
      // 成员或顺序变化才重建骨架；否则逐行比对结构，只有进度变化时原地更新
      const sig = this.tabs.value + ":" + list.map((s) => s.live_key).join(",");
      if (sig !== this.listSig) {
        const fresh = this.listSig === null || !this.listSig.startsWith(this.tabs.value + ":");
        this.listSig = sig;
        this.rows.clear();
        this.list.innerHTML = list.length
          ? list.map((s, i) => `<div class="dl-slot" data-key="${esc(s.live_key)}"${fresh ? ` style="--i:${Math.min(i, 10)}"` : ""}></div>`).join("")
          : `<div class="empty"><div class="empty-ico">${icon(emptyIcon)}</div><div>${emptyText}</div></div>`;
      }
      for (const s of list) this._renderRow(s);
    }

    _renderRow(s) {
      const slot = this.list.querySelector(`.dl-slot[data-key="${CSS.escape(s.live_key)}"]`);
      if (!slot) return;
      const structure = DownloadsPage.html({ ...s, ...ZERO_PROGRESS });
      if (this.rows.get(s.live_key) !== structure) {
        const isNew = !this.rows.has(s.live_key);
        slot.innerHTML = DownloadsPage.html(s);
        if (!isNew || !slot.style.getPropertyValue("--i")) slot.firstElementChild.style.animation = "none";
        this.rows.set(s.live_key, structure);
        // 进度条从 0 起步，下一帧设为实际值，借过渡平滑推进
        requestAnimationFrame(() => requestAnimationFrame(() => slot.firstElementChild && DownloadsPage.patch(slot.firstElementChild, s)));
      } else {
        DownloadsPage.patch(slot.firstElementChild, s);
      }
    }

    static html(s) {
      const { label, glyph } = Status.of(s);
      const key = esc(s.live_key);
      const cover = SessionCard.cover(s);
      const { pct, tone } = SessionCard.progress(s);
      const extra = s.error
        ? ` · <span class="dl-err${s.status === "failed" ? "" : " soft"}" title="${esc(s.error)}">${esc(s.error)}</span>`
        : s.status === "done" && s.file ? ` · <span title="${esc(s.file)}">${esc(s.file.split(/[\\/]/).pop())}</span>` : "";
      return `<article class="dl-row${s.status === "downloading" ? " is-active" : ""}">
        <div class="dl-badge">${icon(glyph)}${cover}</div>
        <div class="dl-info">
          <div class="dl-title"><h4 title="${esc(s.title)}">${esc(s.title || "未命名直播")}</h4><span class="chip ${esc(s.status)}">${esc(label)}</span></div>
          <p class="dl-sub"><b>${esc(s.name || s.uid)}</b> · ${fmtShort(s.start_time)}${extra}</p>
        </div>
        <div class="dl-progress">
          <div class="dl-head"><span class="dl-meta">${DownloadsPage.meta(s)}</span><span class="dl-pct">${pct}%</span></div>
          <div class="bar"><div class="bar-fill ${tone}"></div></div>
        </div>
        <div class="dl-actions">${DownloadsPage.actions(s, key)}</div>
      </article>`;
    }

    static meta(s) {
      if (s.status === "done") {
        const dur = s.duration || (s.end_time > s.start_time ? s.end_time - s.start_time : 0);
        return `${s.file_size ? fmtBytes(s.file_size) + " · " : ""}时长 ${fmtDur(dur)}`;
      }
      if (s.total_segs) {
        const segs = `${s.done_segs || 0}/${s.total_segs} 段`;
        const bytes = s.done_bytes ? ` · ${fmtBytes(s.done_bytes)}${s.total_bytes ? " / " + fmtBytes(s.total_bytes) : ""}` : "";
        const speed = s.speed_bps ? ` · ${fmtSpeed(s.speed_bps)}` : "";
        return segs + bytes + speed;
      }
      return s.status === "queued" ? "等待开始" : "尚未开始下载";
    }

    static patch(row, s) {
      const { pct } = SessionCard.progress(s);
      row.querySelector(".bar-fill").style.transform = `scaleX(${pct / 100})`;
      const pctEl = row.querySelector(".dl-pct");
      const meta = row.querySelector(".dl-meta");
      const metaText = DownloadsPage.meta(s);
      if (pctEl.textContent !== `${pct}%`) pctEl.textContent = `${pct}%`;
      if (meta.textContent !== metaText) meta.textContent = metaText;
    }

    static actions(s, key) {
      const btn = (act, glyph, tip) =>
        `<button class="icon-btn go" data-act="${act}" data-key="${key}" title="${tip}">${icon(glyph)}</button>`;
      if (s.status === "done") return btn("open", "folder-open", "在文件夹中显示");
      if (Status.ACTIVE.has(s.status)) return btn("pause", "pause", "暂停");
      if (s.status === "failed") return btn("start", "retry", "重试");
      return btn("start", "play", "继续");
    }

    async _onAction(e) {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const key = btn.dataset.key;
      const session = this.store.sessions.find((x) => x.live_key === key);
      const act = btn.dataset.act;
      if (act === "start") {
        await this.startDownload(btn, key);
      } else if (act === "pause") {
        await Motion.busy(btn, () => this.api.call("pause_download", key));
        this.toast("暂停请求已发送");
        this.store.refresh();
      } else if (act === "open" && session && session.file) {
        const r = await this.api.call("open_path", session.file, true);
        if (r && !r.ok) this.toast(r.message || "无法打开文件位置", "err");
      }
    }
  }

  Lubo.DownloadsPage = DownloadsPage;
})(window.Lubo = window.Lubo || {});
