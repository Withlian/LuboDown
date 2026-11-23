/* 设置页：B 站账号（扫码登录）、关注的主播、下载目录与并发、ffmpeg 路径。 */
(function (Lubo) {
  "use strict";

  const { $, $$, esc, icon } = Lubo.util;
  const { Motion } = Lubo;

  /** 后端状态 -> 界面文案 */
  const QR_TEXT = {
    idle: "尚未开始",
    loading: "正在生成二维码…",
    waiting: "等待扫码…",
    scanned: "已扫码，请在手机上确认登录",
    success: "登录成功",
    expired: "二维码已过期，请刷新",
    error: "生成失败，请刷新重试",
  };

  class SettingsPage extends Lubo.Page {
    constructor(app, root) {
      super(app, root);
      this.sig = "";
      this.loggedIn = false;
      this.qrState = "idle";
      this.qrTimer = 0;
      const save = (patch, msg) => this.store.save(patch).then((r) => r && r.ok && msg && this.toast(msg, "ok"));
      this.bind({
        "#btn-save-cred": ["click", (e) => this.saveCredentials(e.currentTarget)],
        "#btn-verify": ["click", (e) => this.verify(e.currentTarget)],
        "#btn-qr-reload": ["click", () => this.startQr()],
        "#btn-qr-refresh": ["click", () => this.startQr()],
        "#btn-logout": ["click", (e) => this.logout(e.currentTarget)],
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
        "#sw-revive-deleted": ["change", (e) => save({ revive_deleted: e.target.checked }, e.target.checked ? "删除的场次将在刷新后回到列表" : "删除的场次刷新后不再出现，可在「最近删除」中恢复")],
        "#sel-win-preset": ["change", (e) => {
          if (e.target.value === "custom") { $("#in-win-w").focus(); return; }
          [$("#in-win-w").value, $("#in-win-h").value] = e.target.value.split("x");
        }],
        "#in-win-w": ["input", () => this._syncPreset()],
        "#in-win-h": ["input", () => this._syncPreset()],
        "#btn-win-apply": ["click", (e) => this.applyWindowSize(e.currentTarget)],
      });
      for (const id of ["#in-win-w", "#in-win-h"]) {
        $(id).addEventListener("keydown", (e) => { if (e.key === "Enter") $("#btn-win-apply").click(); });
      }
      $("#anchor-chips").addEventListener("click", (e) => {
        const btn = e.target.closest("button[data-uid]");
        if (btn) this.removeAnchor(btn);
      });
      $$(".pw-eye", root).forEach((b) => b.addEventListener("click", () => {
        const input = $("#" + b.dataset.for);
        input.type = input.type === "password" ? "text" : "password";
      }));
      $("#acc-face").addEventListener("error", (e) => { e.target.hidden = true; });
    }

    get title() { return "设置"; }

    /** 进入设置页：未登录则确保有一张有效的二维码（离开再回来不重新生成） */
    async onShow() {
      if (this.loggedIn) return;
      const s = await this.api.call("qr_status");
      if (!s) return;
      if (s.state === "waiting" || s.state === "scanned") {
        this.applyQr(s);
        this.pollQr();
      } else {
        this.startQr();
      }
    }

    render(state) {
      const s = state.settings || {};
      const sig = JSON.stringify([s.has_sessdata, s.has_bili_jct, s.user_name, s.user_face, s.dede_user_id,
        s.anchors, s.download_dir, s.ffmpeg_path, s.concurrency, s.keep_parts, s.name_with_anchor,
        s.revive_deleted, s.window_width, s.window_height]);
      if (sig === this.sig) return;
      this.sig = sig;
      const setVal = (id, v) => { const el = $(id); if (document.activeElement !== el) el.value = v; };
      setVal("#in-download-dir", s.download_dir || "");
      setVal("#in-ffmpeg", s.ffmpeg_path || "");
      setVal("#in-win-w", s.window_width || 1240);
      setVal("#in-win-h", s.window_height || 820);
      this._syncPreset();
      $("#sel-concurrency").value = String(s.concurrency || 6);
      $("#sw-keep-parts").checked = !!s.keep_parts;
      $("#sw-name-anchor").checked = !!s.name_with_anchor;
      $("#sw-revive-deleted").checked = !!s.revive_deleted;
      $("#in-sessdata").placeholder = s.has_sessdata ? "已保存" : "尚未填写";
      $("#in-bili-jct").placeholder = s.has_bili_jct ? "已保存" : "尚未填写";
      $("#anchor-chips").innerHTML = (s.anchors || []).map((a) =>
        `<span class="anchor-chip">${esc(a.name)}<small>${esc(a.uid)}</small>
          <button class="icon-btn del" data-uid="${esc(a.uid)}" title="移除">${icon("x")}</button></span>`).join("")
        || `<span class="hint">尚未添加主播</span>`;
      this.renderAccount(s);
    }

    /** 已登录 -> 账号卡；未登录 -> 二维码面板 */
    renderAccount(s) {
      const logged = !!s.has_sessdata;
      const wasLogged = this.loggedIn;
      this.loggedIn = logged;
      $("#qr-panel").hidden = logged;
      $("#account-panel").hidden = !logged;
      if (!logged) {
        // 刚退出登录（或凭证被清空）：作废旧码，等 onShow/登出流程重新生成
        if (wasLogged) this.qrState = "idle";
        return;
      }
      this.stopPolling();
      $("#acc-name").textContent = s.user_name || "已登录";
      $("#acc-uid").textContent = s.dede_user_id ? `UID ${s.dede_user_id}` : "凭证已保存在本机";
      const face = $("#acc-face");
      if (s.user_face) {
        face.src = s.user_face;
        face.hidden = false;
      } else {
        face.hidden = true;
        face.removeAttribute("src");
      }
    }

    // ---------- 扫码登录 ----------
    async startQr() {
      if (this.loggedIn) return;
      this.stopPolling();
      this.applyQr({ state: "loading", message: QR_TEXT.loading });
      const s = await this.api.call("qr_start");
      if (!s) {
        this.applyQr({ state: "error", message: "无法连接后端，请重启程序" });
        return;
      }
      this.applyQr(s);
      if (s.state === "waiting" || s.state === "scanned") this.pollQr();
    }

    applyQr(s) {
      this.qrState = s.state || "idle";
      const dead = s.state === "expired" || s.state === "error";
      const img = $("#qr-img"), ph = $("#qr-placeholder"), mask = $("#qr-mask");
      if (s.image) {
        if (img.getAttribute("src") !== s.image) img.src = s.image;
        img.hidden = false;
        ph.hidden = true;
      } else if (s.state === "loading") {
        img.hidden = true;
        ph.hidden = false;
        ph.textContent = "正在生成…";
      }
      mask.hidden = !dead;
      if (dead) {
        $("#qr-mask-text").textContent = s.state === "expired" ? "二维码已过期" : "生成失败";
        img.classList.add("dim");
      } else {
        img.classList.remove("dim");
      }
      const st = $("#qr-status");
      st.className = "qr-status"
        + (s.state === "success" ? " ok" : "")
        + (dead ? " err" : "");
      $("#qr-status-text").textContent = s.message || QR_TEXT[this.qrState] || "";
    }

    /** 1.2s 取一次状态；离开设置页时跳过请求，回来后由 onShow 恢复 */
    pollQr() {
      this.stopPolling();
      this.qrTimer = setInterval(async () => {
        if (this.qrState === "success" || this.qrState === "expired" || this.qrState === "error") {
          this.stopPolling();
          return;
        }
        if (!this.root.classList.contains("active")) return;
        const s = await this.api.call("qr_status");
        if (!s) return;
        this.applyQr(s);
        if (s.state === "success") {
          this.stopPolling();
          this.qrState = "idle";
          this.toast(s.message || "登录成功", "ok");
          this.store.refresh();
        }
      }, 1200);
    }

    stopPolling() {
      if (this.qrTimer) {
        clearInterval(this.qrTimer);
        this.qrTimer = 0;
      }
    }

    async logout(btn) {
      const r = await Motion.busy(btn, () => this.api.call("logout"));
      if (!r || !r.ok) return;
      this.toast("已退出登录", "ok");
      this.qrState = "idle";
      await this.store.refresh();
      this.startQr();
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
        const acc = await this.api.call("account_info");   // 补齐昵称头像，失败不影响凭证本身
        this.store.refresh();
        this.toast(acc && acc.ok ? `凭证已保存：${acc.name}` : "凭证已保存", "ok");
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
        this.toast(r.message || `已添加：${r.name}`, "ok");
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

    /** 输入的宽高恰好是某个预设时选中它，否则显示"自定义" */
    _syncPreset() {
      const sel = $("#sel-win-preset");
      const v = `${$("#in-win-w").value}x${$("#in-win-h").value}`;
      sel.value = Array.from(sel.options).some((o) => o.value === v) ? v : "custom";
    }

    async applyWindowSize(btn) {
      const w = parseInt($("#in-win-w").value, 10), h = parseInt($("#in-win-h").value, 10);
      if (!(w >= 980 && w <= 3840 && h >= 620 && h <= 2160)) {
        this.toast("宽度需在 980–3840、高度需在 620–2160 之间", "err");
        return;
      }
      const r = await Motion.busy(btn, () => this.api.call("set_window_size", w, h));
      if (r && r.ok) {
        this.toast(r.fitted ? `受屏幕大小限制，窗口已设为 ${r.width} × ${r.height}` : `窗口大小已设为 ${r.width} × ${r.height}`, "ok");
        this.store.refresh();
      } else if (r) {
        this.toast(r.message || "设置失败", "err");
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
