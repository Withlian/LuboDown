"""LuboDown —— pywebview JS API 桥接层。前端通过 window.pywebview.api.* 调用。"""
from __future__ import annotations

import datetime as dt
import os
import shutil
import subprocess
from pathlib import Path

from .downloader import resolve_ffmpeg, safe_name


class Api:
    def __init__(self, store, client, manager, scheduler):
        self._store = store
        self._client = client
        self._manager = manager
        self._scheduler = scheduler
        self._window = None  # main.py 启动后注入

    # ============ 状态 ============
    def get_state(self) -> dict:
        jobs = self._manager.all_job_views()
        sessions = []
        for rec in self._store.all_sessions():
            view = {k: rec.get(k) for k in (
                "live_key", "uid", "name", "title", "cover", "start_time", "end_time",
                "duration", "replay_status", "alert_message", "status", "error")}
            fp = self._store.resolve_session_file(rec)
            view["file"] = str(fp) if fp else ""
            jv = jobs.get(rec["live_key"])
            if jv:                       # 下载中的任务用实时进度覆盖
                view.update({k: jv[k] for k in (
                    "status", "error", "total_segs", "done_segs", "done_bytes",
                    "total_bytes", "speed_bps", "percent")})
                view["file"] = jv["file"]
            elif rec.get("status") in ("queued", "downloading", "merging"):
                # 没有对应任务却标着进行中：进程中断等留下的残留，纠正为已暂停
                view["status"] = "paused"
                self._store.update_session(rec["live_key"], status="paused",
                                           error="程序中断，可继续")
            sessions.append(view)
        return {
            "settings": self._store.masked_settings(),
            "sessions": sessions,
            "scheduler": {
                "checking": self._scheduler.checking,
                "next_run_text": self._scheduler.next_run_text(),
                "next_run_ts": int(self._scheduler.next_run),
            },
            "ffmpeg_ok": resolve_ffmpeg(self._store.settings) is not None,
            "logs": self._store.recent_logs(150),
        }

    # ============ 设置 ============
    def save_settings(self, patch: dict) -> dict:
        """空字符串表示保留原值；凭证不回传前端。"""
        clean: dict = {}
        for key in ("sessdata", "bili_jct"):
            v = (patch.get(key) or "").strip()
            if v:
                clean[key] = v
        for key in ("check_interval_min", "concurrency"):
            try:
                if patch.get(key) is not None:
                    clean[key] = max(5, int(patch[key])) if key == "check_interval_min" \
                        else min(16, max(1, int(patch[key])))
            except (TypeError, ValueError):
                pass
        for key in ("auto_check", "auto_download", "keep_parts"):
            if key in patch:
                clean[key] = bool(patch[key])
        v = patch.get("auto_download_since")
        if v is not None:
            if isinstance(v, str):
                v = v.strip()
                if not v:
                    clean["auto_download_since"] = 0
                else:
                    try:
                        t = dt.datetime.fromisoformat(v)
                        if t.tzinfo is None:  # 前端 datetime-local 视为北京时间
                            t = t.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))
                        clean["auto_download_since"] = int(t.timestamp())
                    except ValueError:
                        return {"ok": False, "message": "起始时间格式不正确"}
            else:
                try:
                    clean["auto_download_since"] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
        if patch.get("download_dir") is not None:
            clean["download_dir"] = str(patch["download_dir"])   # 空串=恢复默认，save_settings 内部规范化
        if patch.get("ffmpeg_path"):
            clean["ffmpeg_path"] = str(patch["ffmpeg_path"]).strip()
        self._store.save_settings(clean)
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
        result = self._window.create_file_dialog(
            self._webview_mod.FOLDER_DIALOG)  # noqa: attribute set in attach
        if result:
            return {"ok": True, "path": result if isinstance(result, str) else result[0]}
        return {"ok": False, "message": "未选择"}

    def attach(self, window, webview_module) -> None:
        self._window = window
        self._webview_mod = webview_module

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
        if any(int(a["uid"]) == uid for a in anchors):
            return {"ok": False, "message": "该主播已存在"}
        anchors.append({"uid": uid, "name": info.get("name") or str(uid)})
        self._store.save_settings({"anchors": anchors})
        self._store.log(f"已添加主播：{info.get('name')}（{uid}）")
        return {"ok": True, "name": info.get("name")}

    def remove_anchor(self, uid: int) -> dict:
        anchors = [a for a in self._store.settings.get("anchors", [])
                   if int(a["uid"]) != int(uid)]
        self._store.save_settings({"anchors": anchors})
        return {"ok": True}

    # ============ 检查与下载 ============
    def check_now(self) -> dict:
        ok = self._scheduler.trigger_now()
        return {"ok": ok, "message": "" if ok else "已有检查在进行中"}

    def refresh_list(self) -> dict:
        """仅刷新列表，不触发自动下载。"""
        try:
            if not self._store.settings.get("sessdata"):
                return {"ok": False, "message": "请先在设置中填写 SESSDATA"}
            total = added = 0
            for anchor in self._store.settings.get("anchors", []):
                uid, name = int(anchor["uid"]), anchor.get("name", str(anchor["uid"]))
                items = self._client.fetch_all_replays(uid)
                from .scheduler import _to_record
                records = [_to_record(uid, name, it) for it in items if it.get("live_key")]
                total += len(records)
                added += self._store.upsert_sessions(records)
            self._store.log(f"手动刷新完成：{total} 场，新增 {added} 场")
            return {"ok": True, "total": total, "added": added}
        except Exception as e:
            self._store.log(f"刷新失败：{e}", "error")
            return {"ok": False, "message": str(e)}

    def start_download(self, live_key: str) -> dict:
        status = self._manager.start(live_key)
        return {"ok": status != "missing", "status": status}

    def pause_download(self, live_key: str) -> dict:
        self._manager.cancel(live_key)
        return {"ok": True}

    def delete_session(self, live_key: str, delete_files: bool = False) -> dict:
        job = self._manager.job_view(live_key)
        if job and job["status"] in ("downloading", "merging", "queued"):
            return {"ok": False, "message": "请先暂停下载再删除"}
        rec = self._store.sessions.get(live_key)
        if delete_files and rec:
            f = self._store.resolve_session_file(rec)
            try:
                root = self._store.download_root().resolve()
                if f and f.exists() and root in f.resolve().parents:
                    f.unlink()
            except OSError as e:
                return {"ok": False, "message": f"删除文件失败：{e}"}
        self._store.remove_session(live_key)
        if rec:
            # 连分段目录一起清理
            root = self._store.download_root()
            name = safe_name(rec.get("name") or str(rec.get("uid") or ""))
            shutil.rmtree(root / name / f".parts_{live_key}", ignore_errors=True)
        return {"ok": True}

    # ============ 文件 ============
    def open_path(self, target: str = "", select: bool = False) -> dict:
        """打开下载目录或在资源管理器中定位文件。仅允许下载目录内的路径。"""
        try:
            root = self._store.download_root().resolve()
            if target:
                rp = Path(target).resolve()
                if root not in rp.parents:
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
