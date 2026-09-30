"""LuboDown —— 定时检查调度器。

按设定间隔自动拉取各主播的回放列表，可选自动下载新场次。
"""
from __future__ import annotations

import threading
import time

from .downloader import fmt_ts


def _to_record(uid: int, name: str, it: dict) -> dict:
    live = it.get("live_info") or {}
    video = it.get("video_info") or {}
    alarm = it.get("alarm_info") or {}
    duration = video.get("duration") or 0
    # alarm/alert 描述的是主播侧"合成MP4"的状态，与切片流无关：
    # 只要在切片时长存在（录像可用）时一律不显示，避免误导。
    alert = "" if duration else (
        video.get("alert_message") or alarm.get("message") or "录像缺失")
    return {
        "live_key": str(it.get("live_key") or ""),
        "uid": uid,
        "name": name,
        "replay_id": it.get("replay_id"),
        "room_id": it.get("room_id"),
        "title": live.get("title", ""),
        "cover": live.get("cover", ""),
        "start_time": int(it.get("start_time") or 0),
        "end_time": int(it.get("end_time") or 0),
        "duration": duration,
        "replay_status": video.get("replay_status"),
        "alert_message": alert,
    }


def is_ready(rec: dict) -> bool:
    """切片时长有效即视为可下载（replay_status/alarm 只反映主播侧合成状态）。"""
    return (rec.get("duration") or 0) > 0 and rec.get("end_time", 0) > rec.get("start_time", 0) > 0


class Scheduler:
    def __init__(self, store, client, manager):
        self.store = store
        self.client = client
        self.manager = manager
        self.checking = False
        self.next_run = 0.0
        self._lock = threading.Lock()
        threading.Thread(target=self._tick_loop, daemon=True).start()

    # ------------- 对外 -------------
    def next_run_text(self) -> str:
        if self.checking:
            return "检查中…"
        if not self.store.settings.get("auto_check"):
            return "定时未启用"
        if self.next_run <= 0:
            return "待启动"
        return fmt_ts(int(self.next_run))

    def trigger_now(self, auto_download: bool | None = None) -> bool:
        """立即检查（不阻塞调用方）。"""
        with self._lock:
            if self.checking:
                return False
            self.checking = True
        threading.Thread(target=self._check, args=(auto_download,), daemon=True).start()
        return True

    # ------------- 内部 -------------
    def _tick_loop(self):
        time.sleep(3)                     # 等应用完成初始化
        self.next_run = time.time() + 10  # 启动后先查一次
        while True:
            now = time.time()
            if (self.store.settings.get("auto_check")
                    and not self.checking and now >= self.next_run):
                self.trigger_now()
            time.sleep(15)

    def _check(self, auto_download: bool | None = None):
        try:
            self.store.log("开始检查回放列表…")
            if not self.store.settings.get("sessdata"):
                self.store.log("未配置 SESSDATA，跳过检查", "error")
                return
            total_added = 0
            for anchor in self.store.settings.get("anchors", []):
                try:
                    uid = int(anchor["uid"])
                    name = anchor.get("name") or str(uid)
                    items = self.client.fetch_all_replays(uid)
                except Exception as e:
                    self.store.log(f"获取主播 {anchor} 的回放列表失败：{e}", "error")
                    continue
                records = [_to_record(uid, name, it) for it in items if it.get("live_key")]
                added = self.store.upsert_sessions(records)
                total_added += added
                self.store.log(f"{name}：共 {len(records)} 场，新增 {added} 场")
                should_auto = (self.store.settings.get("auto_download")
                               if auto_download is None else auto_download)
                if should_auto:
                    since = int(self.store.settings.get("auto_download_since") or 0)
                    skipped = 0
                    for rec in records:
                        cur = self.store.sessions.get(rec["live_key"], {})
                        if cur.get("status") != "new" or not is_ready(rec):
                            continue
                        if since and int(rec["start_time"]) < since:
                            skipped += 1          # 起始时间之前开播：不自动下载
                            continue
                        self.manager.start(rec["live_key"])
                    if skipped:
                        self.store.log(f"有 {skipped} 场早于设定的起始时间，未自动下载（可在列表手动下载）")
            if total_added:
                self.store.log(f"检查完成，新增 {total_added} 场")
        finally:
            interval = float(self.store.settings.get("check_interval_min", 60))
            self.next_run = time.time() + interval * 60
            self.checking = False
