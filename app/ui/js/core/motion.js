/* 动效：统一的缓动与可复用动画原语。只动 transform / opacity，尊重系统"减少动画"设置。 */
(function (Lubo) {
  "use strict";

  class Motion {
    static EASE = "cubic-bezier(.22, 1, .36, 1)";

    static get reduced() {
      return !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
    }

    /** FLIP 第一步：记录元素当前位置，key 用于重建 DOM 后对应回同一元素 */
    static capture(elements, keyOf) {
      const rects = new Map();
      for (const el of elements) rects.set(keyOf(el), el.getBoundingClientRect());
      return rects;
    }

    /** FLIP 第二步：从旧位置平滑滑到新位置（排序、筛选、删除补位） */
    static play(elements, keyOf, rects) {
      if (Motion.reduced) return;
      for (const el of elements) {
        const old = rects.get(keyOf(el));
        if (!old) continue;
        const now = el.getBoundingClientRect();
        const dx = old.left - now.left, dy = old.top - now.top;
        if (Math.abs(dx) > 1 || Math.abs(dy) > 1) {
          el.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: "none" }],
                     { duration: 480, easing: Motion.EASE });
        }
      }
    }

    /** 元素收拢消失（删除时使用），返回动画完成的 Promise */
    static collapse(el) {
      if (Motion.reduced) return Promise.resolve();
      return el.animate([{ opacity: 1, transform: "none" }, { opacity: 0, transform: "scale(.92)" }],
                        { duration: 260, easing: Motion.EASE, fill: "forwards" }).finished.catch(() => {});
    }

    /** 内容替换后轻柔淡入 */
    static fadeIn(el, from = .35) {
      if (Motion.reduced || !el) return;
      el.animate([{ opacity: from, transform: "scale(.985)" }, { opacity: 1, transform: "none" }],
                 { duration: 360, easing: Motion.EASE });
    }

    /** 数字从旧值滚动计数到新值 */
    static countTo(el, to) {
      const from = Number(el.dataset.v || 0);
      if (from === to) return;
      el.dataset.v = to;
      cancelAnimationFrame(el._raf);
      if (Motion.reduced) { el.textContent = String(to); return; }
      const t0 = performance.now(), dur = 600;
      const step = (t) => {
        const p = Math.min(1, (t - t0) / dur);
        el.textContent = String(Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3))));
        if (p < 1) el._raf = requestAnimationFrame(step);
      };
      el._raf = requestAnimationFrame(step);
    }

    /** 耗时操作期间按钮显示加载态并防止重复点击 */
    static async busy(btn, task) {
      if (btn.classList.contains("loading")) return null;
      btn.classList.add("loading");
      try {
        return await task();
      } finally {
        btn.classList.remove("loading");
      }
    }
  }

  Lubo.Motion = Motion;
})(window.Lubo = window.Lubo || {});
