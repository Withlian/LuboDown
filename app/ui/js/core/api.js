/* 后端桥接：封装 pywebview JS API，统一处理"未就绪"与调用失败。 */
(function (Lubo) {
  "use strict";

  class ApiClient {
    constructor(toaster) {
      this.toaster = toaster;
    }

    /** 等待 pywebview 注入 window.pywebview.api */
    static ready() {
      return new Promise((resolve) => {
        if (window.pywebview && window.pywebview.api) resolve();
        else window.addEventListener("pywebviewready", () => resolve(), { once: true });
      });
    }

    /** 调用后端方法；失败时提示并返回 null，调用方只需判断返回值 */
    async call(name, ...args) {
      const api = window.pywebview && window.pywebview.api;
      if (!api || typeof api[name] !== "function") return null;
      try {
        return await api[name](...args);
      } catch (e) {
        this.toaster.show(`调用 ${name} 失败：${e}`, "err");
        return null;
      }
    }
  }

  Lubo.ApiClient = ApiClient;
})(window.Lubo = window.Lubo || {});
