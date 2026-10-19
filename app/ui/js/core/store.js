/* 状态仓库：轮询后端状态并以事件广播。"change" 携带最新状态，"tick" 每秒一次供倒计时使用。
   同时记录总下载速度的近期采样，供回放列表与下载页的速度曲线共用。 */
(function (Lubo) {
  "use strict";

  const SPEED_SAMPLES = 30;

  class StateStore extends EventTarget {
    constructor(api, interval = 1500) {
      super();
      this.api = api;
      this.interval = interval;
      this.state = { settings: {}, sessions: [], scheduler: {}, logs: [], ffmpeg_ok: false };
      this.speeds = new Array(SPEED_SAMPLES).fill(0);   // 每轮轮询一个采样，最新在末尾
    }

    get settings() { return this.state.settings || {}; }
    get sessions() { return this.state.sessions || []; }
    get speed() { return this.speeds[this.speeds.length - 1]; }

    async refresh() {
      const s = await this.api.call("get_state");
      if (!s) return;
      this.state = s;
      this.speeds.push((s.sessions || []).reduce((sum, x) => sum + (Lubo.Status.RUNNING.has(x.status) ? x.speed_bps || 0 : 0), 0));
      this.speeds.shift();
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
