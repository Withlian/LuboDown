"""LuboDown —— pywebview JS API 桥接层。前端通过 window.pywebview.api.* 调用。"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .downloader import resolve_ffmpeg
from .store import ACTIVE_STATES, SETTING_SPECS, clamp_setting


class Api:
    def __init__(self, store, client, manager, scheduler):
        self._store = store
        self._client = client
        self._manager = manager
        self._scheduler = scheduler
        self._window = None  # main.py 启动后注入
        self._frame = None

    # ============ 状态 ============
    def get_state(self) -> dict:
        jobs = self._manager.all_job_views()
        sessions = []
        for rec in self._store.all_sessions():
            view = {k: rec.get(k) for k in (
                "live_key", "uid", "name", "title", "cover", "start_time", "end_time",
                "duration", "replay_status", "status", "error")}
            fp = self._store.resolve_session_file(rec)
            view["file"] = str(fp) if fp else ""
            jv = jobs.get(rec["live_key"])
            if jv and jv["status"] != "done":
                # 进度数值只存在于内存 Job，进行中/暂停/失败以实时视图覆盖（queued 仅内存有）；
                # done 之后以库记录为准——file 会随后续下载目录迁移被改写，
                # Job 里完成时的绝对路径不跟着更新，覆盖会让界面指向旧位置
                view.update({k: jv[k] for k in (
                    "status", "error", "total_segs", "done_segs", "done_bytes",
                    "total_bytes", "speed_bps", "percent")})
                view["file"] = jv["file"]
            view["file_size"] = 0        # 下载页展示成品大小
            if view["status"] == "done" and view["file"]:
                try:
                    view["file_size"] = os.path.getsize(view["file"])
                except FileNotFoundError:
                    # 成品已在程序外被删除/移走：呈现为「文件缺失」，可在回放列表重新下载。
                    # 只改视图不落库——移动硬盘暂未接入等情况，文件回来后自动恢复为已完成；
                    # 库里仍是 done，定时检查的自动下载也不会把它当新场次重下
                    view["status"] = "missing"
                    view["error"] = "成品文件已不存在，可重新下载"
                except OSError:
                    pass
            sessions.append(view)
        deleted = [{**{k: rec.get(k) for k in (
            "live_key", "uid", "name", "title", "cover", "start_time", "end_time",
            "duration", "deleted_at")}, "status": "deleted"}
            for rec in self._store.deleted_sessions()]
        return {
            "settings": self._store.masked_settings(),
            "sessions": sessions,
            "deleted": deleted,
            "scheduler": {
                "checking": self._scheduler.checking,
                # 定时检查关闭时不下发下次时间，界面据此显示"未启用"而不是继续倒计时
                "next_run_ts": int(self._scheduler.next_run) if self._store.settings.get("auto_check") else 0,
            },
            "ffmpeg_ok": resolve_ffmpeg(self._store.settings) is not None,
            "logs": self._store.recent_logs(150),
        }

    # ============ 设置 ============
    def save_settings(self, patch: dict) -> dict:
        """按 SETTING_SPECS 的类型清洗前端 patch 后落库。
        字符串留空=保留原值（凭证、ffmpeg_path）；download_dir 例外，空串表示恢复默认；
        anchors 只经 add/remove_anchor 维护，不接受前端整包改写。"""
        clean: dict = {}
        if patch.get("download_dir") is not None:
            clean["download_dir"] = str(patch["download_dir"])   # 空串=恢复默认，save_settings 内部规范化
        for key, (default, _, _) in SETTING_SPECS.items():
            if key not in patch or key == "download_dir" or isinstance(default, list):
                continue
            if isinstance(default, bool):
                clean[key] = bool(patch[key])
            elif isinstance(default, int):
                try:
                    clean[key] = clamp_setting(key, patch[key])
                except (TypeError, ValueError):
                    pass
            else:
                v = str(patch[key] or "").strip()
                if v:
                    clean[key] = v
        old = {k: self._store.settings.get(k) for k in ("auto_check", "check_interval_min")}
        self._store.save_settings(clean)
        if any(self._store.settings.get(k) != v for k, v in old.items()):
            self._scheduler.reschedule()   # 不重排的话倒计时沿用关闭前的旧时间点 / 旧间隔
        self._store.log("设置已保存"
                       + ("（含新凭证）" if ("sessdata" in clean or "bili_jct" in clean) else ""))
        return {"ok": True}

    def verify_cookie(self) -> dict:
        result = self._client.verify_login()
        self._store.log(f"凭证检测：{result['message']}",
                       "info" if result["ok"] else "error")
        return result

    def choose_folder(self) -> dict:
        if self._window is None:
            return {"ok": False, "message": "窗口未就绪"}
        # pywebview 5.1+ 为 FileDialog.FOLDER，旧常量 FOLDER_DIALOG 已弃用
        dialogs = getattr(self._webview_mod, "FileDialog", None)
        folder = dialogs.FOLDER if dialogs else self._webview_mod.FOLDER_DIALOG
        result = self._window.create_file_dialog(folder)
        if result:
            return {"ok": True, "path": result if isinstance(result, str) else result[0]}
        return {"ok": False, "message": "未选择"}

    def attach(self, window, webview_module, frame) -> None:
        self._window = window
        self._webview_mod = webview_module
        self._frame = frame

    # ============ 窗口（无边框，按钮由前端绘制） ============
    def window_state(self) -> dict:
        return self._frame.state()

    def window_minimize(self) -> None:
        self._frame.minimize()

    def window_toggle_maximize(self) -> dict:
        return self._frame.toggle_maximize()

    def window_close(self) -> None:
        self._frame.close()

    def window_drag(self) -> dict:
        return self._frame.drag()

    def window_resize(self, edge: str) -> None:
        self._frame.resize(str(edge))

    def set_window_size(self, width: int, height: int) -> dict:
        """设置中修改窗口大小：立即生效并保存，下次启动沿用。返回实际尺寸（受屏幕大小限制可能更小）。"""
        try:
            w, h = clamp_setting("window_width", width), clamp_setting("window_height", height)
        except (TypeError, ValueError):
            return {"ok": False, "message": "宽高必须是数字"}
        self._store.save_settings({"window_width": w, "window_height": h})
        aw, ah = self._frame.set_size(w, h)
        return {"ok": True, "width": aw, "height": ah, "fitted": (aw, ah) != (w, h)}

    # ============ 主播 ============
    def add_anchor(self, uid: int) -> dict:
        try:
            uid = int(uid)
            info = self._client.get_master_info(uid)
        except (TypeError, ValueError):
            return {"ok": False, "message": "UID 必须是数字"}
        except Exception as e:
            return {"ok": False, "message": f"查询主播失败：{e}"}
        anchors = self._store.settings.get("anchors", [])
        for a in anchors:
            try:
                if int(a["uid"]) == uid:
                    return {"ok": False, "message": "该主播已存在"}
            except (KeyError, TypeError, ValueError):
                continue    # 跳过配置中的异常条目
        anchors.append({"uid": uid, "name": info.get("name") or str(uid)})
        self._store.save_settings({"anchors": anchors})
        self._store.log(f"已添加主播：{info.get('name')}（{uid}）")
        return {"ok": True, "name": info.get("name")}

    def remove_anchor(self, uid: int) -> dict:
        anchors = []
        for a in self._store.settings.get("anchors", []):
            try:
                if int(a["uid"]) == int(uid):
                    continue
            except (KeyError, TypeError, ValueError):
                pass    # 异常条目与 add_anchor 一致：跳过匹配、原样保留
            anchors.append(a)
        self._store.save_settings({"anchors": anchors})
        return {"ok": True}

    # ============ 检查与下载 ============
    def check_now(self) -> dict:
        ok = self._scheduler.trigger_now()
        return {"ok": ok, "message": "" if ok else "已有检查在进行中"}

    def refresh_list(self) -> dict:
        """仅刷新列表，不触发自动下载。"""
        if not self._store.settings.get("sessdata"):
            return {"ok": False, "message": "请先在设置中填写 SESSDATA"}
        try:
            result = self._scheduler.run_check()
        except Exception as e:
            self._store.log(f"刷新失败：{e}", "error")
            return {"ok": False, "message": str(e)}
        if result is None:    # 与定时检查互斥，一轮结束后再试
            return {"ok": False, "message": "已有检查在进行中"}
        total, added = result
        self._store.log(f"手动刷新完成：{total} 场，新增 {added} 场")
        return {"ok": True, "total": total, "added": added}

    def start_download(self, live_key: str) -> dict:
        status = self._manager.start(live_key)
        return {"ok": status != "missing", "status": status}

    def pause_download(self, live_key: str) -> dict:
        self._manager.cancel(live_key)
        return {"ok": True}

    def delete_session(self, live_key: str, delete_files: bool = False) -> dict:
        job = self._manager.job_view(live_key)
        if job and job["status"] in ACTIVE_STATES:   # job_view 返回的是 dict 视图
            return {"ok": False, "message": "请先暂停下载再删除"}
        rec = self._store.sessions.get(live_key)
        if delete_files and rec:
            f = self._store.resolve_session_file(rec)
            try:
                root = self._store.download_root().resolve()
                # 下载目录改过后旧成品记为绝对路径、不在当前根目录下，按扩展名放行本场自己的成品
                if f and f.exists() and (root in f.resolve().parents or f.suffix.lower() == ".mp4"):
                    f.unlink()
            except OSError as e:
                return {"ok": False, "message": f"删除文件失败：{e}"}
        self._store.remove_session(live_key)
        # 同步移除任务记录并清理分段目录：任务残留会把之后重建的同名会话
        # 覆盖成旧状态，分段残留会让重建后的下载"未下先续"
        self._manager.discard(live_key)
        self._manager.purge_parts(live_key)
        return {"ok": True}

    def restore_session(self, live_key: str) -> dict:
        """从「最近删除」恢复场次：重新入库为未下载，可再次下载。"""
        rec = self._store.restore_session(str(live_key))
        if rec is None:
            return {"ok": False, "message": "该场次的删除记录已不存在"}
        self._store.log(f"已恢复场次：{rec.get('name') or rec.get('uid')} {rec.get('title') or ''}".rstrip())
        return {"ok": True}

    # ============ 文件 ============
    def _is_session_file(self, path: Path) -> bool:
        """是否为某场次记录的成品文件（下载目录改过后旧成品可能在当前根目录之外）。"""
        for rec in self._store.snapshot_sessions().values():
            f = self._store.resolve_session_file(rec)
            if f and f.resolve() == path:
                return True
        return False

    def open_path(self, target: str = "", select: bool = False) -> dict:
        """打开下载目录或在资源管理器中定位文件。仅允许下载目录内的路径与场次记录的成品文件。"""
        try:
            root = self._store.download_root().resolve()
            if target:
                rp = Path(target).resolve()
                if root not in rp.parents and not self._is_session_file(rp):
                    return {"ok": False, "message": "仅允许访问下载目录内的路径"}
                if not rp.exists():
                    rp = root
                    select = False
                if select and rp.is_file():
                    subprocess.Popen(["explorer", "/select,", str(rp)])
                else:
                    os.startfile(rp if rp.is_dir() else root)  # noqa: S606
            else:
                root.mkdir(parents=True, exist_ok=True)
                os.startfile(root)  # noqa: S606
            return {"ok": True}
        except OSError as e:
            return {"ok": False, "message": str(e)}
