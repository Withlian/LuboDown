/* 定时任务页：定时检查 / 自动下载开关、间隔、起始时间、倒计时与运行日志。 */
(function (Lubo) {
  "use strict";

  const { $, esc, fmtTime, fmtCountdown, unixToInput, inputToUnix } = Lubo.util;

  class SchedulePage extends Lubo.Page {
    constructor(app, root) {
      super(app, root);
      this.sig = "";
      this.logSig = "";
      const save = (patch, msg) => this.store.save(patch).then((r) => r && r.ok && msg && this.toast(msg, "ok"));
      this.bind({
        "#sw-auto-check": ["change", (e) => save({ auto_check: e.target.checked }, e.target.checked ? "定时检查已启用" : "定时检查已关闭")],
        "#sw-auto-download": ["change", (e) => save({ auto_download: e.target.checked }, e.target.checked ? "自动下载已开启" : "自动下载已关闭")],
        "#sel-interval": ["change", (e) => save({ check_interval_min: Number(e.target.value) }, "间隔已更新")],
        "#in-since": ["change", (e) => {
          const unix = inputToUnix(e.target.value);
          save({ auto_download_since: unix }, unix ? "起始时间已设置，之后开播的场次才会自动下载" : "起始时间已清除");
        }],
        "#btn-clear-since": ["click", () => { $("#in-since").value = ""; save({ auto_download_since: 0 }, "起始时间已清除"); }],
        "#btn-check-2": ["click", (e) => app.pages.sessions.checkNow(e.currentTarget)],
      });
    }

    get title() { return "定时任务"; }

    render(state) {
      const st = state.settings || {};
      const sig = JSON.stringify([st.auto_check, st.auto_download, st.check_interval_min, st.auto_download_since]);
      if (sig !== this.sig) {
        this.sig = sig;
        $("#sw-auto-check").checked = !!st.auto_check;
        $("#auto-check-label").textContent = st.auto_check ? "已启用" : "已关闭";
        $("#sw-auto-download").checked = !!st.auto_download;
        $("#auto-download-label").textContent = st.auto_download ? "已开启" : "已关闭";
        $("#sel-interval").value = String(st.check_interval_min || 60);
        const since = $("#in-since");
        if (document.activeElement !== since) since.value = st.auto_download_since ? unixToInput(st.auto_download_since) : "";
      }
      this._renderLogs(state.logs || []);
    }

    tick() {
      const sch = this.store.state.scheduler || {};
      $("#next-run").textContent = sch.checking ? "检查中…"
        : sch.next_run_ts ? `${fmtTime(sch.next_run_ts)}（${fmtCountdown(sch.next_run_ts)}后）` : "—";
    }

    _renderLogs(logs) {
      // 用内容签名而不是长度判断：日志环饱和后长度不再变化
      const last = logs[logs.length - 1];
      const sig = last ? `${logs.length}|${logs[0].t}|${last.t}|${last.msg}` : "0";
      if (sig === this.logSig) return;
      const first = this.logSig === "";
      this.logSig = sig;
      const box = $("#log-box");
      const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 60;
      box.innerHTML = logs.map((l) =>
        `<div class="log-line ${esc(l.level)}"><span class="t">${esc(l.t)}</span><span class="msg">${esc(l.msg)}</span></div>`).join("");
      if (!first && box.lastElementChild) box.lastElementChild.classList.add("fresh");
      if (nearBottom) box.scrollTo({ top: box.scrollHeight, behavior: first ? "auto" : "smooth" });
    }
  }

  Lubo.SchedulePage = SchedulePage;
})(window.Lubo = window.Lubo || {});
