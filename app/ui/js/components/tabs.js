/* 分段标签：滑块跟随选中项平滑移动，切换时派发 "change"（detail 为 data-value）。 */
(function (Lubo) {
  "use strict";

  class Tabs extends EventTarget {
    constructor(root) {
      super();
      this.root = root;
      this.pill = Lubo.util.$(".tabs-pill", root);
      this.buttons = Lubo.util.$$(".tab", root);
      this.buttons.forEach((b) => b.addEventListener("click", () => this.select(b.dataset.value)));
      // 字体加载或容器尺寸变化后重新对齐滑块
      new ResizeObserver(() => this._movePill(true)).observe(root);
    }

    get value() {
      const active = this.buttons.find((b) => b.classList.contains("active"));
      return active ? active.dataset.value : null;
    }

    select(value) {
      if (value === this.value) return;
      this.buttons.forEach((b) => b.classList.toggle("active", b.dataset.value === value));
      this._movePill();
      this.dispatchEvent(new CustomEvent("change", { detail: value }));
    }

    _movePill(instant = false) {
      const active = this.buttons.find((b) => b.classList.contains("active"));
      if (!active || !active.offsetWidth) return;
      if (instant) this.pill.style.transition = "none";
      this.pill.style.width = `${active.offsetWidth}px`;
      this.pill.style.transform = `translateX(${active.offsetLeft}px)`;
      if (instant) { void this.pill.offsetHeight; this.pill.style.transition = ""; }
    }
  }

  Lubo.Tabs = Tabs;
})(window.Lubo = window.Lubo || {});
