/* 回放列表页：主视觉卡（14 天曲线 + 统计）、快捷卡（检查 / 下载速度）、场次卡片网格（筛选、搜索、排序）。
   这里负责发现场次与开始下载；下载过程在下载页管理，有任务的卡片跳转过去。 */
(function (Lubo) {
  "use strict";

  const { $, $$, icon, fmtSpeed, fmtDayLabel, dayKey, fmtCountdown } = Lubo.util;
  const { Motion, SessionCard, StateStore, Sparkline, Tabs } = Lubo;
  const DAYS = 14;

  class SessionsPage extends Lubo.Page {
    constructor(app, root) {
      super(app, root);
      this.grid = $("#session-grid");
      this.statusTabs = new Tabs($("#status-tabs"));
      this.heroChart = new Sparkline($("#hero-chart"));
      this.speedChart = new Sparkline($("#speed-chart"), 4);
      this.filter = { status: "all", uid: null, text: "", desc: true };
      this.cards = new Map();          // live_key -> { html, status }
      this.gridSig = "";
      this.pendingRemove = null;
      this.chartSig = "";

      this.statusTabs.addEventListener("change", (e) => this._setFilter({ status: e.detail }));
      $("#search").addEventListener("input", (e) => this._setFilter({ text: e.target.value.trim().toLowerCase() }));
      this.bind({
        "#btn-check": ["click", (e) => this.checkNow(e.currentTarget)],
        "#btn-refresh": ["click", (e) => this.refreshList(e.currentTarget)],
        "#btn-sort": ["click", (e) => this._toggleSort(e.currentTarget)],
      });
      this.grid.addEventListener("click", (e) => this._onCardAction(e));
    }

    get title() { return "回放列表"; }

    /** 侧边面板选择主播时调用；null 表示全部 */
    setAnchor(uid) { this._setFilter({ uid }); }

    render(state) {
      const sessions = state.sessions || [];
      this._renderHero(sessions);
      this._renderQuick(sessions);
      this._renderGrid(this._view(sessions));
    }

    tick() {
      const sch = this.store.state.scheduler || {};
      $("#quick-next").textContent = sch.checking ? "正在检查…"
        : sch.next_run_ts ? `下次检查 ${fmtCountdown(sch.next_run_ts)}后` : "定时检查未启用";
    }

    async checkNow(btn) {
      const r = await Motion.busy(btn, () => this.api.call("check_now"));
      if (r && r.ok) this.toast("开始检查…", "ok");
      else if (r) this.toast(r.message, "err");
    }

    async refreshList(btn) {
      const r = await Motion.busy(btn, () => this.api.call("refresh_list"));
      if (r && r.ok) { this.toast(`刷新完成：共 ${r.total} 场，新增 ${r.added} 场`, "ok"); this.store.refresh(); }
      else if (r) this.toast(r.message || "刷新失败", "err");
    }

    /* ---------- 主视觉卡与快捷卡 ---------- */
    _renderHero(sessions) {
      const count = { new: 0, queued: 0, downloading: 0, merging: 0, done: 0, failed: 0, paused: 0 };
      for (const s of sessions) if (s.status in count) count[s.status]++;
      const active = count.queued + count.downloading + count.merging;
      const pending = count.new + count.paused + count.failed;
      Motion.countTo($("#stat-total"), sessions.length);
      Motion.countTo($("#stat-active"), active);
      Motion.countTo($("#stat-done"), count.done);
      Motion.countTo($("#stat-pending"), pending);

      // 近 14 天每天开播场次
      const today = dayKey(Date.now() / 1000);
      const days = Array.from({ length: DAYS }, (_, i) => today - DAYS + 1 + i);
      const perDay = days.map((d) => sessions.filter((s) => dayKey(s.start_time) === d).length);
      const sig = days[0] + ":" + perDay.join(",");
      if (sig === this.chartSig) return;
      this.chartSig = sig;
      this.heroChart.draw(perDay);
      $("#hero-axis").innerHTML = days.filter((_, i) => i % 2 === 1).map((d) => `<span>${fmtDayLabel(d)}</span>`).join("");
    }

    _renderQuick(sessions) {
      const running = sessions.filter((s) => StateStore.RUNNING.has(s.status));
      const speed = this.store.speed;
      this.speedChart.draw(this.store.speeds);
      const cur = running[0];
      $("#dl-speed").textContent = speed ? fmtSpeed(speed) : running.length ? "合成中" : "空闲";
      $("#dl-title").textContent = cur ? `${cur.title || "未命名直播"} · ${+(cur.percent || 0).toFixed(1)}%` : "暂无进行中的任务";
    }

    /* ---------- 卡片网格 ---------- */
    _setFilter(patch) {
      Object.assign(this.filter, patch);
      this.render(this.store.state);
    }

    _toggleSort(btn) {
      this.filter.desc = !this.filter.desc;
      $("span", btn).textContent = this.filter.desc ? "最新在前" : "最早在前";
      this.render(this.store.state);
    }

    _view(sessions) {
      const { status, uid, text, desc } = this.filter;
      const match = {
        all: () => true,
        pending: (s) => StateStore.PENDING.has(s.status),   // 未下载：含暂停与失败
        done: (s) => s.status === "done",
      }[status];
      return sessions
        .filter((s) => match(s)
          && (uid === null || String(s.uid) === String(uid))
          && (!text || `${s.title} ${s.name}`.toLowerCase().includes(text)))
        .sort((a, b) => (desc ? b.start_time - a.start_time : a.start_time - b.start_time));
    }

    _renderGrid(list) {
      $("#list-count").textContent = list.length;
      if (!list.length) {
        if (this.gridSig !== "empty") {
          const filtered = this.store.sessions.length > 0;
          this.grid.innerHTML = `<div class="empty"><div class="empty-ico">${icon(filtered ? "search" : "inbox")}</div>
            <div>${filtered ? "没有符合条件的场次" : "暂无回放数据，点击检查新回放拉取"}</div></div>`;
          this.gridSig = "empty";
          this.cards.clear();
        }
        return;
      }
      // 骨架仅在场次集合或顺序变化时重建；已有卡片经 FLIP 平滑移动，只有新卡片入场
      const sig = list.map((s) => s.live_key).join(",");
      let rects = null;
      if (sig !== this.gridSig) {
        const keyOf = (el) => el.dataset.key;
        rects = Motion.capture($$(".slot", this.grid), keyOf);
        let n = 0;
        this.grid.innerHTML = list.map((s) => {
          const enter = rects.has(s.live_key) ? ` style="animation:none"` : ` style="--i:${Math.min(n++, 10)}"`;
          return `<div class="slot" data-key="${Lubo.util.esc(s.live_key)}"${enter}></div>`;
        }).join("");
        this.gridSig = sig;
        for (const entry of this.cards.values()) entry.html = null;
      }
      for (const s of list) this._renderCard(s);
      if (rects) Motion.play($$(".slot", this.grid), (el) => el.dataset.key, rects);
    }

    _renderCard(s) {
      const slot = this.grid.querySelector(`.slot[data-key="${CSS.escape(s.live_key)}"]`);
      if (!slot) return;
      const html = SessionCard.html(s, this.pendingRemove === s.live_key);
      const prev = this.cards.get(s.live_key);
      if (prev && prev.html === html) return;
      slot.innerHTML = html;
      if (prev && prev.status && prev.status !== s.status) Motion.fadeIn(slot.firstElementChild);
      this.cards.set(s.live_key, { html, status: s.status });
    }

    async _onCardAction(e) {
      const btn = e.target.closest("button[data-act]");
      if (!btn) return;
      const key = btn.dataset.key;
      const session = this.store.sessions.find((x) => x.live_key === key);
      const act = btn.dataset.act;
      if (act === "start") {
        const r = await Motion.busy(btn, () => this.api.call("start_download", key));
        if (r && r.ok) { this.toast("已加入下载队列", "ok"); this.store.refresh(); }
        else if (r) { this.toast("该场次已不存在，列表即将刷新", "err"); this.store.refresh(); }
      } else if (act === "goto") {
        this.app.showDownloads(session ? SessionCard.downloadsTab(session) : null);
      } else if (act === "remove") {
        this._remove(btn, key, session);
      }
    }

    async _remove(btn, key, session) {
      // 两段式确认：首次点击进入确认态，3 秒内再次点击才删除
      if (this.pendingRemove !== key) {
        this.pendingRemove = key;
        clearTimeout(this._removeTimer);
        this._removeTimer = setTimeout(() => { this.pendingRemove = null; this.render(this.store.state); }, 3000);
        this.render(this.store.state);
        return;
      }
      this.pendingRemove = null;
      const r = await Motion.busy(btn, () => this.api.call("delete_session", key, !!(session && session.status === "done")));
      if (r && r.ok) {
        const slot = btn.closest(".slot");
        if (slot) await Motion.collapse(slot);
        this.toast("已删除", "ok");
        this.store.refresh();
      } else if (r) {
        this.toast(r.message || "删除失败", "err");
      }
    }
  }

  Lubo.SessionsPage = SessionsPage;
})(window.Lubo = window.Lubo || {});
