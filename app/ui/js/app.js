/* 应用入口：组装服务与页面，负责页面切换与全局状态（顶栏状态灯、跳转链接）。 */
(function (Lubo) {
  "use strict";

  const { $, icon } = Lubo.util;

  class App {
    constructor() {
      this.toaster = new Lubo.Toaster($("#toasts"));
      this.api = new Lubo.ApiClient(this.toaster);
      this.store = new Lubo.StateStore(this.api);
      this.theme = new Lubo.ThemeController($("#btn-theme"));
      this.sidebar = new Lubo.Sidebar($("#sidebar"));
      this.shell = $("#shell");
      this.frame = new Lubo.WindowFrame(this.api, this.shell);
      this.title = $("#page-title");

      this.pages = {
        sessions: new Lubo.SessionsPage(this, $("#page-sessions")),
        schedule: new Lubo.SchedulePage(this, $("#page-schedule")),
        settings: new Lubo.SettingsPage(this, $("#page-settings")),
      };
      this.panel = new Lubo.SidePanel(this, $("#panel"), (uid) => this.pages.sessions.setAnchor(uid));
      this.current = "sessions";

      this.sidebar.addEventListener("navigate", (e) => this.navigate(e.detail));
      document.addEventListener("click", (e) => {
        const link = e.target.closest("[data-goto]");
        if (link) this.sidebar.select(link.dataset.goto);
      });
      this.store.addEventListener("change", (e) => this.render(e.detail));
      this.store.addEventListener("tick", () => Object.values(this.pages).forEach((p) => p.tick()));
    }

    async boot() {
      const waiting = setTimeout(() => {
        $("#session-grid").innerHTML = `<div class="empty"><div class="empty-ico">${icon("clock")}</div>
          <div>等待应用后端就绪…（若长时间无响应请重启程序）</div></div>`;
      }, 3000);
      await Lubo.ApiClient.ready();
      clearTimeout(waiting);
      this.frame.sync();
      this.store.start();
    }

    /** 旧页淡出后切入新页；连续快速点击以最后一次为准 */
    navigate(name) {
      const next = this.pages[name];
      if (!next) return;
      const leaving = this.pages[this.current].root;
      leaving.classList.add("leaving");
      this.shell.dataset.page = name;
      this.current = name;
      this.title.textContent = next.title;
      this.title.classList.remove("swap");
      void this.title.offsetWidth;
      this.title.classList.add("swap");
      clearTimeout(this._switchTimer);
      this._switchTimer = setTimeout(() => {
        Object.values(this.pages).forEach((p) => p.root.classList.remove("active", "leaving"));
        next.root.classList.add("active");
        $("#main").scrollTop = 0;
        next.onShow();
      }, 140);
    }

    render(state) {
      const cookie = $("#st-cookie"), ffmpeg = $("#st-ffmpeg");
      const hasCookie = !!(state.settings && state.settings.has_sessdata);
      cookie.className = "status-item" + (hasCookie ? " ok" : "");
      cookie.title = hasCookie ? "Cookie 已配置" : "Cookie 未配置，请在设置中填写";
      ffmpeg.className = "status-item" + (state.ffmpeg_ok ? " ok" : " warn");
      ffmpeg.title = state.ffmpeg_ok ? "ffmpeg 正常" : "未找到 ffmpeg，请在设置中指定";
      Object.values(this.pages).forEach((p) => p.render(state));
      this.panel.render(state);
    }
  }

  Lubo.App = App;
  new App().boot();
})(window.Lubo = window.Lubo || {});
