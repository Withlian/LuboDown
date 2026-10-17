/* 窗口外框（无边框圆角窗口）：自绘最小化/最大化/关闭，标题区拖动与双击最大化，边缘缩放。原生行为由后端 WindowFrame 实现。 */
(function (Lubo) {
  "use strict";

  const { $ } = Lubo.util;
  const EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];
  const INTERACTIVE = "button, input, select, textarea, a, label, .tabs, .status-pill";
  const DBL_CLICK_MS = 320;

  class WindowFrame {
    constructor(api, shell) {
      this.api = api;
      this.html = document.documentElement;
      this.maxBtn = $("#win-max");
      this.lastDown = 0;

      shell.insertAdjacentHTML("beforeend",
        EDGES.map((e) => `<div class="rz rz-${e}" data-edge="${e}"></div>`).join(""));
      shell.addEventListener("mousedown", (e) => this._onMouseDown(e));
      $("#win-min").addEventListener("click", () => api.call("window_minimize"));
      this.maxBtn.addEventListener("click", () => this.toggleMaximize());
      $("#win-close").addEventListener("click", () => api.call("window_close"));
    }

    /** 后端就绪后读取窗口状态 */
    async sync() { this._apply(await this.api.call("window_state")); }

    async toggleMaximize() { this._apply(await this.api.call("window_toggle_maximize")); }

    _onMouseDown(e) {
      if (e.button !== 0) return;
      const edge = e.target.dataset.edge;
      if (edge) {
        e.preventDefault();
        this.api.call("window_resize", edge);
        return;
      }
      if (!e.target.closest("[data-drag]") || e.target.closest(INTERACTIVE)) return;
      e.preventDefault();
      // 系统移动循环会吞掉 dblclick，这里按两次按下的间隔自行判断双击
      if (e.timeStamp - this.lastDown < DBL_CLICK_MS) {
        this.lastDown = 0;
        this.toggleMaximize();
        return;
      }
      this.lastDown = e.timeStamp;
      this.api.call("window_drag").then((s) => this._apply(s));
    }

    _apply(state) {
      if (!state) return;
      this.html.dataset.window = state.maximized ? "max" : "normal";
      this.maxBtn.title = state.maximized ? "还原" : "最大化";
    }
  }

  Lubo.WindowFrame = WindowFrame;
})(window.Lubo = window.Lubo || {});
