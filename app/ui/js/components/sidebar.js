/* 侧边栏：页面导航（选中滑块跟随）与收起/展开（Ctrl+B，状态记忆）。导航时派发 "navigate"。 */
(function (Lubo) {
  "use strict";

  const KEY = "lubo-sidebar";   // 与 index.html 头部内联脚本一致

  class Sidebar extends EventTarget {
    constructor(root) {
      super();
      this.root = root;
      this.html = document.documentElement;
      this.items = Lubo.util.$$(".sb-nav .sb-item", root);
      this.pill = Lubo.util.$(".sb-pill", root);
      this.toggleBtn = Lubo.util.$("#btn-sidebar", root);

      this.items.forEach((btn) => btn.addEventListener("click", () => this.select(btn.dataset.page)));
      this.toggleBtn.addEventListener("click", () => this.toggle());
      document.addEventListener("keydown", (e) => {
        if (e.ctrlKey && !e.altKey && e.key.toLowerCase() === "b") {
          e.preventDefault();
          this.toggle();
        }
      });
      this._syncLabels();
      this._movePill(true);
    }

    get collapsed() { return this.html.dataset.sidebar === "collapsed"; }

    select(page) {
      const target = this.items.find((b) => b.dataset.page === page);
      if (!target || target.classList.contains("active")) return;
      this.items.forEach((b) => b.classList.toggle("active", b === target));
      this._movePill();
      this.dispatchEvent(new CustomEvent("navigate", { detail: page }));
    }

    toggle() {
      const collapsed = !this.collapsed;
      this.html.dataset.sidebar = collapsed ? "collapsed" : "expanded";
      Lubo.util.pref.set(KEY, collapsed ? "collapsed" : "expanded");
      this._syncLabels();
    }

    _syncLabels() {
      const label = this.collapsed ? "展开侧边栏" : "收起侧边栏";
      this.toggleBtn.dataset.tip = `${label}（Ctrl+B）`;
      Lubo.util.$(".sb-label", this.toggleBtn).textContent = label;
    }

    _movePill(instant = false) {
      const active = this.items.find((b) => b.classList.contains("active"));
      if (!active) return;
      if (instant) this.pill.style.transition = "none";
      this.pill.style.transform = `translateY(${active.offsetTop}px)`;
      if (instant) { void this.pill.offsetHeight; this.pill.style.transition = ""; }
    }
  }

  Lubo.Sidebar = Sidebar;
})(window.Lubo = window.Lubo || {});
