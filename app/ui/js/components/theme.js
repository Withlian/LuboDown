/* 主题：亮/暗切换。支持 View Transitions 时以按钮为圆心圆形扩散，否则颜色平滑过渡。 */
(function (Lubo) {
  "use strict";

  const KEY = "lubo-theme";

  class ThemeController {
    constructor(button) {
      this.root = document.documentElement;
      button.addEventListener("click", (e) => this.toggle(e.currentTarget));
      if (window.matchMedia) {   // 未手动选择过时跟随系统
        matchMedia("(prefers-color-scheme: dark)").addEventListener("change", (e) => {
          if (!Lubo.util.pref.get(KEY)) this._apply(e.matches ? "dark" : "light", true);
        });
      }
    }

    get current() { return this.root.dataset.theme; }

    toggle(origin) {
      const next = this.current === "dark" ? "light" : "dark";
      Lubo.util.pref.set(KEY, next);
      if (!document.startViewTransition || Lubo.Motion.reduced) {
        this._apply(next, true);
        return;
      }
      const r = origin.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
      const vt = document.startViewTransition(() => this._apply(next, false));
      vt.ready.then(() => this.root.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 560, easing: Lubo.Motion.EASE, pseudoElement: "::view-transition-new(root)" },
      )).catch(() => {});
    }

    _apply(theme, fade) {
      if (fade) {
        this.root.classList.add("theme-anim");
        setTimeout(() => this.root.classList.remove("theme-anim"), 400);
      }
      this.root.dataset.theme = theme;
    }
  }

  Lubo.ThemeController = ThemeController;
})(window.Lubo = window.Lubo || {});
