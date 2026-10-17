/* 右侧面板：主播列表（点击筛选回放、可只看有任务的主播）与最近日志。 */
(function (Lubo) {
  "use strict";

  const { $, esc, icon } = Lubo.util;
  const AVATAR_COLORS = ["#FB7299", "#F59E6B", "#3FA796", "#5B8DEF", "#E8963A", "#E0628F"];

  class SidePanel {
    constructor(app, root, onSelect) {
      this.app = app;
      this.root = root;
      this.onSelect = onSelect;
      this.selected = null;
      this.sig = "";
      this.logSig = "";
      this.tabs = new Lubo.Tabs($("#panel-tabs", root));
      this.tabs.addEventListener("change", () => this.render(app.store.state));
      $("#panel-anchors", root).addEventListener("click", (e) => {
        const item = e.target.closest(".anchor-item");
        if (!item) return;
        const uid = item.dataset.uid || null;
        this.selected = this.selected === uid ? null : uid;   // 再次点击取消筛选
        this.onSelect(this.selected);
        this.render(app.store.state);
      });
    }

    render(state) {
      this._renderAnchors(state);
      this._renderLogs(state.logs || []);
    }

    _renderAnchors(state) {
      const anchors = (state.settings && state.settings.anchors) || [];
      const stats = new Map();
      for (const s of state.sessions || []) {
        const st = stats.get(String(s.uid)) || { total: 0, busy: 0 };
        st.total++;
        if (Lubo.StateStore.ACTIVE.has(s.status)) st.busy++;
        stats.set(String(s.uid), st);
      }
      // 正在筛选的主播被移除后取消筛选，否则列表会一直停在已不存在的主播上
      if (this.selected !== null && !anchors.some((a) => String(a.uid) === this.selected)) {
        this.selected = null;
        this.onSelect(null);
      }
      const onlyBusy = this.tabs.value === "busy";
      const rows = anchors
        .map((a) => ({ uid: String(a.uid), name: a.name || String(a.uid), ...(stats.get(String(a.uid)) || { total: 0, busy: 0 }) }))
        .filter((a) => !onlyBusy || a.busy);
      const sig = JSON.stringify([rows, this.selected, onlyBusy]);
      if (sig === this.sig) return;
      this.sig = sig;
      // 只有列表成员变化（首次、切换标签、增删主播）才重播入场动画，计数更新不重播
      const members = onlyBusy + ":" + rows.map((a) => a.uid).join(",");
      const animate = members !== this.members;
      this.members = members;

      const item = (a, i) => `<li><button class="anchor-item${this.selected === a.uid ? " active" : ""}" data-uid="${esc(a.uid)}"${animate ? ` style="--i:${i}"` : ` style="animation:none"`}>
          <span class="avatar" style="background:${AVATAR_COLORS[Number(a.uid) % AVATAR_COLORS.length] || AVATAR_COLORS[0]}">${esc(a.name.slice(0, 1))}</span>
          <span class="anchor-text"><b>${esc(a.name)}</b><small>${a.total} 场${a.busy ? ` · ${a.busy} 个任务进行中` : ""}</small></span>
          ${icon("check")}
        </button></li>`;
      const all = `<li><button class="anchor-item${this.selected === null ? " active" : ""}" data-uid=""${animate ? "" : ` style="animation:none"`}>
          <span class="avatar all">${icon("users")}</span>
          <span class="anchor-text"><b>全部主播</b><small>${(state.sessions || []).length} 场回放</small></span>
          ${icon("check")}
        </button></li>`;
      $("#panel-anchors", this.root).innerHTML = rows.length || !onlyBusy
        ? (onlyBusy ? "" : all) + rows.map(item).join("")
        : `<li class="panel-empty">暂无进行中的任务</li>`;
    }

    _renderLogs(logs) {
      const recent = logs.slice(-4).reverse();
      const sig = recent.map((l) => l.t + l.msg).join("|");
      if (sig === this.logSig) return;
      this.logSig = sig;
      $("#panel-logs", this.root).innerHTML = recent.map((l) =>
        `<li class="${esc(l.level)}"><span class="t">${esc(l.t.slice(-8, -3))}</span><span class="msg" title="${esc(l.msg)}">${esc(l.msg)}</span></li>`,
      ).join("") || `<li class="panel-empty">暂无日志</li>`;
    }
  }

  Lubo.SidePanel = SidePanel;
})(window.Lubo = window.Lubo || {});
