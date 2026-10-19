/* 场次状态模型：全部状态的单一权威定义，与后端 store.py 的状态机对应。
   文案、图标、所属下载页标签与进行中排序都在表里，集合由表派生；
   新增状态只改这里，散落各页的判断一律引用本模块。 */
(function (Lubo) {
  "use strict";

  const TABLE = {
    new:         { label: "未下载", glyph: "video" },
    queued:      { label: "排队中", glyph: "clock",    tab: "active",  order: 2 },
    downloading: { label: "下载中", glyph: "download", tab: "active",  order: 0 },
    merging:     { label: "合成中", glyph: "zap",      tab: "active",  order: 1 },
    done:        { label: "已完成", glyph: "check",    tab: "done" },
    failed:      { label: "失败",   glyph: "alert",    tab: "stopped" },
    paused:      { label: "已暂停", glyph: "pause",    tab: "stopped" },
  };

  const Status = {
    TABLE,
    ACTIVE: new Set(["queued", "downloading", "merging"]),   // 有任务进行中（对应后端 ACTIVE_STATES）
    RUNNING: new Set(["downloading", "merging"]),            // 正在产出速度的阶段
    PENDING: new Set(["new", "paused", "failed"]),           // 「未下载」标签：含暂停与失败

    /** 状态定义；未知状态兜底为原样文案 + video 图标 */
    of(s) {
      return TABLE[s.status] || { label: s.status, glyph: "video" };
    },

    /** 该状态对应的下载页标签；null 表示还没有下载任务（如未下载） */
    tab(s) {
      return this.of(s).tab || null;
    },
  };

  Lubo.Status = Status;
})(window.Lubo = window.Lubo || {});
