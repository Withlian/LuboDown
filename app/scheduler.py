"""LuboDown —— 定时检查调度器。

按设定间隔自动拉取各主播的回放列表，可选自动下载新场次。
"""
from __future__ import annotations

import threading
import time

from .downloader import fmt_ts


READY_GRACE_SEC = 30 * 60   # duration 缺失时，结束后多久视为切片已就绪


def _to_record(uid: int, name: str, it: dict) -> dict:
    live = it.get("live_info") or {}
    video = it.get("video_info") or {}
    # 不展示 alarm_info/alert_message："录像生成失败"等描述的是主播侧合成 MP4
    # 的状态，与可下载的切片流无关（竖屏场几乎全报但都能下）。切片是否真的
    # 缺失，以实际拉取播放列表的报错为准。
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
        "duration": video.get("duration") or 0,
        "replay_status": video.get("replay_status"),
    }


def is_ready(rec: dict) -> bool:
    """切片流是否可下载。

    duration 反映的是主播侧合成 MP4 的时长，竖屏场可能恒为 0 但切片完整
    可下：duration>0 立即可下；缺失时等结束后留出缓冲再放行，避免对
    尚未生成切片的场次空跑一次失败下载。"""
    if not (rec.get("end_time", 0) > rec.get("start_time", 0) > 0):
        return False
    return ((rec.get("duration") or 0) > 0
            or time.time() > int(rec["end_time"]) + READY_GRACE_SEC)


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

    def trigger_now(self) -> bool:
        """立即检查（不阻塞调用方）。"""
        with self._lock:
            if self.checking:
                return False
            self.checking = True
        threading.Thread(target=self._check, daemon=True).start()
        return True

    def run_check(self, auto_download: bool = False) -> tuple[int, int]:
        """拉取全部主播的回放列表并入库，返回 (总场数, 新增场数)。

        手动刷新与定时检查共用的唯一入口；auto_download=True 时对
        尚为 new 且切片已就绪的场次启动下载（受 auto_download_since 约束）。
        所有主播都拉取失败时抛出最后一个异常，由调用方决定如何呈现。"""
        total = added = fetched = 0
        last_err: Exception | None = None
        for anchor in self.store.settings.get("anchors", []):
            try:
                uid = int(anchor["uid"])
                name = anchor.get("name") or str(uid)
                items = self.client.fetch_all_replays(uid)
            except Exception as e:
                last_err = e
                self.store.log(f"获取主播 {anchor} 的回放列表失败：{e}", "error")
                continue
            fetched += 1
            records = [_to_record(uid, name, it) for it in items if it.get("live_key")]
            total += len(records)
            new_added = self.store.upsert_sessions(records)
            added += new_added
            self.store.log(f"{name}：共 {len(records)} 场，新增 {new_added} 场")
            if auto_download:
                self._auto_download(records)
        if fetched == 0 and last_err is not None:
            raise last_err
        return total, added

    def _auto_download(self, records: list[dict]) -> None:
        """对状态仍为 new 且切片已就绪的场次启动下载。"""
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

    def _check(self):
        try:
            self.store.log("开始检查回放列表…")
            if not self.store.settings.get("sessdata"):
                self.store.log("未配置 SESSDATA，跳过检查", "error")
                return
            try:
                _, added = self.run_check(
                    auto_download=bool(self.store.settings.get("auto_download")))
            except Exception as e:
                self.store.log(f"检查失败：{e}", "error")
                return
            if added:
                self.store.log(f"检查完成，新增 {added} 场")
        finally:
            try:
                interval = float(self.store.settings.get("check_interval_min", 60))
            except (TypeError, ValueError):
                interval = 60.0   # 配置被手工改坏时不让 checking 永久卡死
            self.next_run = time.time() + interval * 60
            self.checking = False
