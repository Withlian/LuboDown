/* 设置页：B 站凭证、关注的主播、下载目录与并发、ffmpeg 路径。 */
(function (Lubo) {
  "use strict";

  const { $, $$, esc, icon } = Lubo.util;
  const { Motion } = Lubo;

  class SettingsPage extends Lubo.Page {
    constructor(app, root) {
      super(app, root);
      this.sig = "";
      const save = (patch, msg) => this.store.save(patch).then((r) => r && r.ok && msg && this.toast(msg, "ok"));
      this.bind({
        "#btn-save-cred": ["click", (e) => this.saveCredentials(e.currentTarget)],
        "#btn-verify": ["click", (e) => this.verify(e.currentTarget)],
        "#btn-add-anchor": ["click", (e) => this.addAnchor(e.currentTarget)],
        "#in-uid": ["keydown", (e) => { if (e.key === "Enter") $("#btn-add-anchor").click(); }],
        "#btn-browse": ["click", (e) => this.browse(e.currentTarget)],
        "#in-download-dir": ["change", (e) => {
          const v = e.target.value.trim();
          save({ download_dir: v }, v ? "下载目录已保存" : "已恢复为默认下载目录");
        }],
        "#in-ffmpeg": ["change", (e) => save({ ffmpeg_path: e.target.value.trim() || "ffmpeg" }, "ffmpeg 路径已保存")],
        "#sel-concurrency": ["change", (e) => save({ concurrency: Number(e.target.value) }, "并发数已保存")],
        "#sw-keep-parts": ["change", (e) => save({ keep_parts: e.target.checked }, e.target.checked ? "将保留分段文件" : "分段文件将在合成后清除")],
        "#sw-name-anchor": ["change", (e) => save({ name_with_anchor: e.target.checked }, e.target.checked ? "新下载的文件名将包含主播名与开播时间" : "文件名保持纯日期序号格式")],
      });
      $("#anchor-chips").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-uid]");
        if (btn) this.removeAnchor(btn);
      });
      $$(".pw-eye", root).forEach((b) => b.addEventListener("click", () => {
        const input = $("#" + b.dataset.for);
        input.type = input.type === "password" ? "text" : "password";
      }));
    }

    get title() { return "设置"; }

    render(state) {
      const s = state.settings || {};
      const sig = JSON.stringify([s.has_sessdata, s.has_bili_jct, s.anchors, s.download_dir,
        s.ffmpeg_path, s.concurrency, s.keep_parts, s.name_with_anchor]);
      if (sig === this.sig) return;
      this.sig = sig;
      const setVal = (id, v) => { const el = $(id); if (document.activeElement !== el) el.value = v; };
      setVal("#in-download-dir", s.download_dir || "");
      setVal("#in-ffmpeg", s.ffmpeg_path || "");
      $("#sel-concurrency").value = String(s.concurrency || 6);
      $("#sw-keep-parts").checked = !!s.keep_parts;
      $("#sw-name-anchor").checked = !!s.name_with_anchor;
      $("#in-sessdata").placeholder = s.has_sessdata ? "已保存" : "尚未填写";
      $("#in-bili-jct").placeholder = s.has_bili_jct ? "已保存" : "尚未填写";
      $("#anchor-chips").innerHTML = (s.anchors || []).map((a) =>
        `<span class="anchor-chip">${esc(a.name)}<small>${esc(a.uid)}</small>
          <button class="icon-btn del" data-uid="${esc(a.uid)}" title="移除">${icon("x")}</button></span>`).join("")
        || `<span class="hint">尚未添加主播</span>`;
    }

    async saveCredentials(btn) {
      const patch = {};
      for (const [id, key] of [["#in-sessdata", "sessdata"], ["#in-bili-jct", "bili_jct"]]) {
        const v = $(id).value.trim();
        if (v) patch[key] = v;
      }
      if (!Object.keys(patch).length) { this.toast("没有需要保存的新凭证"); return; }
      const r = await Motion.busy(btn, () => this.store.save(patch));
      if (r && r.ok) {
        $("#in-sessdata").value = $("#in-bili-jct").value = "";
        this.toast("凭证已保存", "ok");
      }
    }

    async verify(btn) {
      const el = $("#verify-result");
      el.textContent = "检测中…";
      el.style.color = "";
      const r = await Motion.busy(btn, () => this.api.call("verify_cookie"));
      el.textContent = r ? r.message : "检测失败";
      el.style.color = r ? (r.ok ? "var(--ok)" : "var(--err)") : "";
      Motion.fadeIn(el, 0);
    }

    async addAnchor(btn) {
      const uid = $("#in-uid").value.trim();
      if (!uid) return;
      const r = await Motion.busy(btn, () => this.api.call("add_anchor", uid));
      if (r && r.ok) {
        this.toast(`已添加：${r.name}`, "ok");
        $("#in-uid").value = "";
        this.store.refresh();
      } else if (r) {
        this.toast(r.message || "添加失败", "err");
      }
    }

    async removeAnchor(btn) {
      const chip = btn.closest(".anchor-chip");
      const r = await this.api.call("remove_anchor", Number(btn.dataset.uid));
      if (r && r.ok) {
        await Motion.collapse(chip);
        this.toast("已移除", "ok");
        this.store.refresh();
      }
    }

    async browse(btn) {
      const r = await Motion.busy(btn, () => this.api.call("choose_folder"));
      if (r && r.ok) {
        $("#in-download-dir").value = r.path;
        const s = await this.store.save({ download_dir: r.path });
        if (s && s.ok) this.toast("下载目录已保存", "ok");
      }
    }
  }

  Lubo.SettingsPage = SettingsPage;
})(window.Lubo = window.Lubo || {});
