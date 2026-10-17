/* 平滑曲线图：Catmull-Rom 样条转贝塞尔。点数不变时借 CSS d 属性过渡，数据变化会平滑形变。 */
(function (Lubo) {
  "use strict";

  class Sparkline {
    constructor(svg, padding = 8) {
      this.line = svg.querySelector(".spark-line");
      this.area = svg.querySelector(".spark-area");
      const box = svg.viewBox.baseVal;
      this.w = box.width;
      this.h = box.height;
      this.pad = padding;
      this.drawn = false;
    }

    draw(values) {
      // 首次先画一条基线，下一帧再画真实数据，让曲线从底部升起
      if (!this.drawn) {
        this.drawn = true;
        this._set(values.map(() => 0));
        requestAnimationFrame(() => requestAnimationFrame(() => this._set(values)));
        return;
      }
      this._set(values);
    }

    _set(values) {
      const max = Math.max(1, ...values);
      const step = this.w / Math.max(1, values.length - 1);
      const pts = values.map((v, i) => [i * step, this.h - this.pad - (v / max) * (this.h - this.pad * 2)]);
      const line = Sparkline.smooth(pts);
      this.line.style.d = `path("${line}")`;
      this.area.style.d = `path("${line} L ${this.w} ${this.h} L 0 ${this.h} Z")`;
    }

    static smooth(pts) {
      let d = `M ${pts[0][0]} ${pts[0][1]}`;
      for (let i = 0; i < pts.length - 1; i++) {
        const p0 = pts[i - 1] || pts[i], p1 = pts[i], p2 = pts[i + 1], p3 = pts[i + 2] || p2;
        const c1 = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
        const c2 = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
        d += ` C ${c1[0].toFixed(1)} ${c1[1].toFixed(1)}, ${c2[0].toFixed(1)} ${c2[1].toFixed(1)}, ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
      }
      return d;
    }
  }

  Lubo.Sparkline = Sparkline;
})(window.Lubo = window.Lubo || {});
