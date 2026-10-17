/* 轻提示：右上角弹入，停留后淡出移除。 */
(function (Lubo) {
  "use strict";

  const ICON = { ok: "check", err: "alert" };

  class Toaster {
    constructor(root, duration = 2800) {
      this.root = root;
      this.duration = duration;
    }

    show(message, type = "") {
      const el = document.createElement("div");
      el.className = `toast ${type}`;
      el.innerHTML = Lubo.util.icon(ICON[type] || "info");
      const text = document.createElement("span");
      text.textContent = message;
      el.appendChild(text);
      this.root.appendChild(el);
      setTimeout(() => this._dismiss(el), this.duration);
    }

    _dismiss(el) {
      el.classList.add("leaving");
      el.addEventListener("animationend", () => el.remove(), { once: true });
      setTimeout(() => el.remove(), 600);   // 动画被禁用时的兜底
    }
  }

  Lubo.Toaster = Toaster;
})(window.Lubo = window.Lubo || {});
