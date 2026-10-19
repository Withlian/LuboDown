/* 页面基类：持有应用级服务，约定 render(state) / tick() / onShow() 三个钩子。 */
(function (Lubo) {
  "use strict";

  class Page {
    constructor(app, root) {
      this.app = app;
      this.root = root;
      this.store = app.store;
      this.api = app.api;
    }

    /** 页面标题（顶栏显示） */
    get title() { return ""; }

    /** 每次状态轮询后调用；子类自行做差异判断，避免无谓的 DOM 重建 */
    render(state) {}

    /** 每秒调用一次，用于倒计时等 */
    tick() {}

    /** 切换到本页时调用 */
    onShow() {}

    $(sel) { return Lubo.util.$(sel, this.root); }

    toast(message, type) { this.app.toaster.show(message, type); }

    /** 开始下载的共用流程（回放列表与下载页）：提示结果并刷新状态 */
    async startDownload(btn, key) {
      const r = await Lubo.Motion.busy(btn, () => this.api.call("start_download", key));
      if (!r) return;   // 按钮已在加载中
      if (r.ok) this.toast("已加入下载队列", "ok");
      else this.toast("该场次已不存在，列表即将刷新", "err");
      this.store.refresh();
    }

    /** 为一组控件绑定事件的小助手：{ "#id": ["click", handler] } */
    bind(map) {
      for (const [sel, [type, handler]] of Object.entries(map)) {
        Lubo.util.$(sel).addEventListener(type, handler);
      }
    }
  }

  Lubo.Page = Page;
})(window.Lubo = window.Lubo || {});
