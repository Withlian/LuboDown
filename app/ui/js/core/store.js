/* 状态仓库：轮询后端状态并以事件广播。"change" 携带最新状态，"tick" 每秒一次供倒计时使用。 */
(function (Lubo) {
  "use strict";

  const ACTIVE = new Set(["queued", "downloading", "merging"]);
  const PENDING = new Set(["new", "paused", "failed"]);

  class StateStore extends EventTarget {
    static ACTIVE = ACTIVE;
    static PENDING = PENDING;

    constructor(api, interval = 1500) {
      super();
      this.api = api;
      this.interval = interval;
      this.state = { settings: {}, sessions: [], scheduler: {}, logs: [], ffmpeg_ok: false };
    }

    get settings() { return this.state.settings || {}; }
    get sessions() { return this.state.sessions || []; }

    async refresh() {
      const s = await this.api.call("get_state");
      if (!s) return;
      this.state = s;
      this.dispatchEvent(new CustomEvent("change", { detail: s }));
    }

    start() {
      const loop = () => this.refresh().finally(() => setTimeout(loop, this.interval));
      loop();
      setInterval(() => this.dispatchEvent(new Event("tick")), 1000);
    }

    /** 保存设置后立即刷新，界面不必等下一轮轮询 */
    async save(patch) {
      const r = await this.api.call("save_settings", patch);
      if (r && r.ok) this.refresh();
      return r;
    }
  }

  Lubo.StateStore = StateStore;
})(window.Lubo = window.Lubo || {});
