"""LuboDown —— 回放下载器。

流程：GetUserSliceStream 拿 m3u8 -> 并发下载分段（可断点续传、
签名过期自动刷新播放列表）-> ffmpeg 经本地 HLS 播放列表复封装为单个 MP4。
所有 B 站请求都经过 URL 安全校验（https/域名白名单/公网 IP 固定；
DoH 解析的已知例外见 docs/replay-api-notes.md）。
"""
from __future__ import annotations

import http.client
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .bilibili import UA, ApiError, _PinnedConnection, check_url, request
from .store import ACTIVE_STATES, APP_DIR

TZ8 = timezone(timedelta(hours=8))
UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]+')

BUNDLED_FFMPEG = APP_DIR / "bin" / "ffmpeg.exe"


def resolve_ffmpeg(settings: dict) -> str | None:
    """解析 ffmpeg：用户自定义路径 > 项目内置 bin/ffmpeg.exe > PATH。"""
    configured = (settings.get("ffmpeg_path") or "").strip()
    if configured and configured != "ffmpeg":
        return shutil.which(configured) or (configured if Path(configured).exists() else None)
    if BUNDLED_FFMPEG.exists():
        return str(BUNDLED_FFMPEG)
    return shutil.which("ffmpeg")


def safe_name(text: str, fallback: str = "untitled") -> str:
    text = UNSAFE.sub("_", (text or "").strip()).strip(" ._")
    return text[:60] or fallback


def _merging_tmp(final: Path) -> Path:
    """合成期的临时输出（与成品同目录，成功后 os.replace 原子转正）。"""
    return final.parent / f".{final.stem}.merging.mp4"


def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(int(ts), TZ8).strftime("%Y-%m-%d %H:%M:%S")


class Job:
    def __init__(self, live_key: str, meta: dict):
        self.live_key = live_key
        self.uid = int(meta.get("uid") or 0)
        self.name = meta.get("name", "")
        self.title = meta.get("title", "")
        self.start_time = int(meta.get("start_time") or 0)
        self.end_time = int(meta.get("end_time") or 0)
        self.status = "queued"
        self.error = ""
        self.total_segs = 0
        self.done_segs = 0
        self.done_bytes = 0
        self.total_bytes = 0      # 估算值
        self.speed_bps = 0.0
        self.file = ""
        self.stop_event = threading.Event()
        self._seg_lock = threading.Lock()
        self._last_bytes = 0
        self._last_t = time.time()

    def bump(self, nbytes: int) -> None:
        with self._seg_lock:
            self.done_segs += 1
            self.done_bytes += nbytes
            done, total = self.done_segs, self.done_bytes
        self.touch_speed(total)
        if self.total_bytes == 0 and done >= 5:
            self.total_bytes = int(total / done * self.total_segs)

    def apply_baseline(self, segs: int, nbytes: int) -> None:
        """续传基线：磁盘已有分段计入进度，
        并同步速度基准——速度只统计本次新下载的字节，不把基线算进去。"""
        self.done_segs = segs
        self.done_bytes = nbytes
        self._last_bytes = nbytes
        self._last_t = time.time()

    def touch_speed(self, now_bytes: int) -> None:
        now = time.time()
        dt = now - self._last_t
        if dt >= 1.0:
            inst = max(0, now_bytes - self._last_bytes) / dt
            self.speed_bps = inst if self.speed_bps == 0 else self.speed_bps * 0.7 + inst * 0.3
            self._last_bytes, self._last_t = now_bytes, now

    def view(self) -> dict:
        pct = (self.done_segs / self.total_segs * 100) if self.total_segs else 0.0
        return {
            "live_key": self.live_key, "uid": self.uid, "name": self.name,
            "title": self.title, "start_time": self.start_time, "end_time": self.end_time,
            "start_text": fmt_ts(self.start_time) if self.start_time else "",
            "status": self.status, "error": self.error,
            "total_segs": self.total_segs, "done_segs": self.done_segs,
            "done_bytes": self.done_bytes, "total_bytes": self.total_bytes,
            "speed_bps": round(self.speed_bps), "percent": round(pct, 1),
            "file": self.file,
        }


class _KeepAlive:
    """单个主机的 HTTPS 持久连接（分段下载用，避免每段重新握手）。"""

    def __init__(self, url: str):
        p = urllib.parse.urlparse(url)
        self.host, ips = check_url(url)
        self._ips = ips
        self.port = p.port or 443   # check_url 已保证 https
        self._conn: _PinnedConnection | None = None

    def _ensure(self):
        if self._conn is None:
            conn = _PinnedConnection(self.host, ips=self._ips, timeout=60)
            conn.connect()
            self._conn = conn
        return self._conn

    def get(self, path: str, headers: dict | None = None):
        """返回 (HTTP 状态码, 响应头, 响应体)。4xx/5xx 不复用连接。"""
        for attempt in (1, 2):
            try:
                conn = self._ensure()
                conn.request("GET", path, headers={"User-Agent": UA,
                                                   "Accept": "*/*", **(headers or {})})
                resp = conn.getresponse()
                body = resp.read()
                if resp.status >= 400:     # CDN 错误不复用连接
                    self._conn = None
                return resp.status, resp.headers, body
            except (OSError, http.client.HTTPException):
                # 连接作废；固定 IP 失效的自愈在 _PinnedConnection.connect 里完成
                self._conn = None
                if attempt == 2:
                    raise

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None


class DownloadManager:
    def __init__(self, store, client):
        self.store = store
        self.client = client
        self.jobs: dict[str, Job] = {}
        self._queue: queue.Queue[tuple[str, Job]] = queue.Queue()
        self._lock = threading.Lock()
        # 启动时清理"幽灵状态"：上次进程退出时仍标记为进行中的场次，恢复为已暂停
        for key, rec in store.sessions.items():
            if rec.get("status") in ACTIVE_STATES:
                store.update_session(key, status="paused", error="程序上次退出时中断，可继续")
        self._runner = threading.Thread(target=self._run_loop, daemon=True)
        self._runner.start()

    # ------------- 对外接口 -------------
    def _apply_resume_baseline(self, job: Job) -> None:
        """把磁盘上已有的有效分段计入进度基线，
        续传时进度条从真实位置开始而不是从零跳到完成。"""
        try:
            _, _, parts_dir = self._session_dir(job)
            existing = [f for f in parts_dir.glob("seg_*.mp4") if f.stat().st_size > 0]
            if existing:
                job.apply_baseline(len(existing),
                                   sum(f.stat().st_size for f in existing))
        except OSError:
            pass

    def start(self, live_key: str) -> str:
        with self._lock:
            # 顺带清理失效任务（终态且会话已不存在），
            # 防止残留任务状态覆盖之后重建的同名会话
            for k in [k for k, j in self.jobs.items()
                      if j.status not in ACTIVE_STATES and k not in self.store.sessions]:
                del self.jobs[k]
            job = self.jobs.get(live_key)
            if job and job.status in ACTIVE_STATES:
                return job.status
            meta = self.store.sessions.get(live_key)
            if not meta:
                return "missing"
            job = Job(live_key, meta)
            self.jobs[live_key] = job
        self._apply_resume_baseline(job)
        self._queue.put((live_key, job))
        self.store.log(f"加入下载队列：{job.name} {fmt_ts(job.start_time)} {job.title}")
        return "queued"

    def cancel(self, live_key: str) -> None:
        with self._lock:
            job = self.jobs.get(live_key)
        if job and job.status in ACTIVE_STATES:
            job.stop_event.set()
            # 立即反映到界面与数据库，不等工作线程响应
            self._mark_paused(job)
            self.store.log("收到暂停请求，正在停止…")

    def job_view(self, live_key: str) -> dict | None:
        job = self.jobs.get(live_key)
        return job.view() if job else None

    def all_job_views(self) -> dict[str, dict]:
        with self._lock:
            return {k: j.view() for k, j in self.jobs.items()}

    def discard(self, live_key: str) -> None:
        """会话被删除时同步移除任务记录（进行中的除外，删除流程会先拒绝）。"""
        with self._lock:
            job = self.jobs.get(live_key)
            if job and job.status not in ACTIVE_STATES:
                del self.jobs[live_key]

    def purge_parts(self, live_key: str) -> None:
        """删除会话后清理其分段目录（带重试，失败记录日志）。
        按目录名在下载根目录下定位，主播改名后依然找得到。"""
        for d in self.store.download_root().glob(f"*/.parts_{live_key}"):
            self._cleanup_parts(d)

    # ------------- 队列线程 -------------
    def _settle(self, job: Job, status: str, error: str = "",
                file: Path | None = None) -> None:
        """任务状态统一收口：内存 Job 与会话库同步更新，避免双写漂移。
        file 传成品绝对路径；库中存相对下载根目录的便携形式。"""
        job.status, job.error = status, error
        fields = {"status": status, "error": error}
        if file is not None:
            job.file = str(file)
            try:
                fields["file"] = str(file.relative_to(self.store.download_root()))
            except ValueError:   # 根目录被中途改走：存绝对路径，store 对外部路径原样保留
                fields["file"] = str(file)
        self.store.update_session(job.live_key, **fields)

    def _mark_paused(self, job: Job) -> None:
        self._settle(job, "paused", "已暂停（可继续）")

    def _run_loop(self):
        while True:
            live_key, job = self._queue.get()
            if self.jobs.get(live_key) is not job:
                continue    # 过期条目：暂停后重建的新任务已取代它，直接丢弃
            if job.stop_event.is_set():   # 排队中已被暂停：直接出队，不再启动
                self._mark_paused(job)
                self.store.log(f"已取消排队任务：{job.name} {fmt_ts(job.start_time)}")
                continue
            try:
                self._download(job)
            except Exception as e:  # 兜底，保证队列线程不死
                self._settle(job, "failed", str(e))
                self.store.log(f"下载异常终止：{e}", "error")

    # ------------- 下载主体 -------------
    def _date_stem(self, job: Job) -> str:
        """按日期命名，一律带当天序号：2026.9.25-01、2026.9.25-02（按开播时间排序）。
        开启 name_with_anchor 后追加主播名与开播时间：2026.9.25-01_主播名_19-59。"""
        d = datetime.fromtimestamp(job.start_time, TZ8).date()
        prefix = f"{d.year}.{d.month}.{d.day}"
        same_day = sorted(
            (s for s in self.store.snapshot_sessions().values()
             if s.get("uid") == job.uid and s.get("start_time")
             and datetime.fromtimestamp(int(s["start_time"]), TZ8).date() == d),
            key=lambda s: int(s["start_time"]))
        index = next((i for i, s in enumerate(same_day, 1)
                      if s.get("live_key") == job.live_key), 1)
        stem = f"{prefix}-{index:02d}"
        if self.store.settings.get("name_with_anchor"):
            t = datetime.fromtimestamp(job.start_time, TZ8)
            stem += f"_{safe_name(job.name or str(job.uid))}_{t.strftime('%H-%M')}"
        return stem

    def _session_dir(self, job: Job) -> tuple[Path, Path, Path]:
        root = self.store.download_root()
        stem = self._date_stem(job)
        anchor_dir = root / safe_name(job.name or str(job.uid))
        return anchor_dir, anchor_dir / f"{stem}.mp4", anchor_dir / f".parts_{job.live_key}"

    def _download(self, job: Job):
        s = self.store.settings
        # —— 前置检查 ——
        if not s.get("sessdata"):
            self._settle(job, "failed", "未配置 SESSDATA，请先在设置中填写")
            self.store.log(job.error, "error")
            return
        ffmpeg = resolve_ffmpeg(s)
        if ffmpeg is None:
            self._settle(job, "failed", "找不到 ffmpeg（已内置于项目 bin/ffmpeg.exe，或请在设置中指定路径）")
            self.store.log(job.error, "error")
            return
        anchor_dir, final, parts_dir = self._session_dir(job)
        root = self.store.download_root()
        try:
            _merging_tmp(final).unlink(missing_ok=True)   # 上次合并中断的残留
        except OSError:
            pass
        final_rel = str(final.relative_to(root))
        if final.exists() and final.stat().st_size > 0:
            # 正常情况是断点/重复下载的跳过；但同日更早的场次可能晚于本场入库、
            # 令序号重排：若该路径记录在另一会话名下，属于编号冲突，
            # 拒绝把别人的成品误标为本场完成
            owner = next((k for k, r in self.store.snapshot_sessions().items()
                          if k != job.live_key and r.get("file") == final_rel), None)
            if owner:
                msg = (f"{final.name} 已被场次 {owner} 记录，疑似同日序号重排冲突，"
                       "可开启「文件名附加主播名与开播时间」或删除冲突文件后重试")
                self._settle(job, "failed", msg)
                self.store.log(msg, "error")
                return
            if not s.get("keep_parts"):
                self._cleanup_parts(parts_dir)   # 之前中断留下的分段一并清掉
            self._settle(job, "done", file=final)
            self.store.log(f"文件已存在，跳过下载：{final}")
            return
        try:
            root.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(root).free
        except OSError:
            free = None   # 空间检查是尽力而为的提示，失败不阻断下载
        if free is not None and free < 3 * 1024**3:
            self.store.log(f"警告：磁盘剩余空间仅 {free/1024**3:.1f}GB", "error")

        if job.stop_event.is_set():   # 拿到播放列表前就被暂停
            self._mark_paused(job)
            return

        self._settle(job, "downloading")

        try:
            pairs = self._fetch_playlist(job)
        except ApiError as e:
            self._settle(job, "failed", f"获取播放列表失败：{e.message}")
            self.store.log(job.error, "error")
            return

        job.total_segs = len(pairs)
        job.total_bytes = 0
        if job.stop_event.is_set():   # 建分段目录前再查一次，保证不留垃圾目录
            self._mark_paused(job)
            return
        parts_dir.mkdir(parents=True, exist_ok=True)
        self.store.log(f"开始下载 {job.total_segs} 个分段（并发 {s.get('concurrency', 6)}）")

        for pass_no in range(3):  # 整体重试轮次（处理签名过期等）
            missing = self._missing_segments(pairs, parts_dir)
            if not missing:
                break
            if pass_no > 0:
                self.store.log(f"第 {pass_no + 1} 轮：还有 {len(missing)} 段未完成，刷新签名重试")
                try:
                    pairs = self._refresh_playlist(job, len(pairs))
                except ApiError as e:
                    self.store.log(f"刷新播放列表失败：{e.message}", "error")
                    break
            self._download_pass(job, pairs, parts_dir, missing)
            if job.stop_event.is_set():
                self._mark_paused(job)
                self.store.log(f"已暂停：{job.done_segs}/{job.total_segs} 段完成，已下载 {job.done_bytes/1024**3:.2f}GB")
                return

        remaining = self._missing_segments(pairs, parts_dir)
        if remaining:
            self._settle(job, "failed", f"{len(remaining)} 个分段多次重试后仍失败")
            self.store.log(job.error, "error")
            return

        # —— 合成 ——
        self._settle(job, "merging")
        job.speed_bps = 0
        self.store.log(f"分段全部完成（{job.done_bytes/1024**3:.2f}GB），开始 ffmpeg 合成…")
        durs = [d for _, d in pairs]
        ok, msg = self._merge(parts_dir, final, ffmpeg, durs, job.stop_event)
        if not ok:
            if job.stop_event.is_set():   # 合成阶段被暂停：分段保留，续跑时重新合成
                self._mark_paused(job)
                return
            self._settle(job, "failed", f"合成失败：{msg}")
            self.store.log(job.error, "error")
            return
        if not s.get("keep_parts"):
            self._cleanup_parts(parts_dir)
        self._settle(job, "done", file=final)
        self.store.log(f"完成：{final}")

    def _cleanup_parts(self, parts_dir: Path, tries: int = 3) -> None:
        """删除分段目录；Windows 下文件可能被短暂占用，失败时重试并最终告警。"""
        for i in range(tries):
            shutil.rmtree(parts_dir, ignore_errors=True)
            if not parts_dir.exists():
                return
            time.sleep(1.5)
        if parts_dir.exists():
            self.store.log(f"分段目录未能完全清除（可能被杀毒/索引占用）：{parts_dir}", "error")

    # ------------- m3u8 -------------
    def _fetch_playlist(self, job: Job) -> list[tuple[str, float]]:
        """拉取场次**全部子段**的 m3u8 并按时间顺序合并，
        返回 [(分段URL, EXTINF时长秒), ...]。

        手机竖屏直播的录像会被 B 站拆成多个碎片子段（推流重连所致），
        只取第一个子段会得到一个几秒钟的"完整视频"。"""
        entries = self.client.get_slice_entries(
            job.live_key, job.start_time, job.end_time, job.uid)
        pairs: list[tuple[str, float]] = []
        for idx, e in enumerate(entries):
            m3u8_url = e["stream"]
            text = request(m3u8_url, raw=True).decode("utf-8", "replace")
            urls: list[str] = []
            durs: list[float] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"#EXTINF:([\d.]+)", line)
                if m:
                    durs.append(float(m.group(1)))
                elif not line.startswith("#"):
                    urls.append(line)
            if not urls:
                self.store.log(f"子段 {idx}（{fmt_ts(e['start_time'])}）没有分段，跳过")
                continue
            if len(durs) < len(urls):            # 兜底：缺 EXTINF 的段按 0 处理
                durs += [0.0] * (len(urls) - len(durs))
            pairs.extend((u if u.startswith("http") else urllib.parse.urljoin(m3u8_url, u), d)
                         for u, d in zip(urls, durs))
        if not pairs:
            raise ApiError(-2, "播放列表为空")
        return pairs

    def _refresh_playlist(self, job: Job, expected_count: int) -> list[tuple[str, float]]:
        pairs = self._fetch_playlist(job)
        if len(pairs) != expected_count:
            raise ApiError(-2, f"刷新后分段数变化（{expected_count}->{len(pairs)}），请重新下载")
        return pairs

    @staticmethod
    def _seg_path(parts_dir: Path, idx: int) -> Path:
        return parts_dir / f"seg_{idx:06d}.mp4"

    def _missing_segments(self, pairs: list, parts_dir: Path) -> list[int]:
        return [i for i in range(len(pairs))
                if not self._seg_path(parts_dir, i).exists()
                or self._seg_path(parts_dir, i).stat().st_size == 0]

    # ------------- 分段下载 -------------
    def _download_pass(self, job: Job, pairs: list, parts_dir: Path,
                       indexes: list[int]) -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        pool: dict[str, list[_KeepAlive]] = {}   # host -> 空闲连接
        pool_lock = threading.Lock()

        def acquire_ka(url: str) -> _KeepAlive:
            # 只复用同主机的连接：不同 CDN 主机之间的签名路径不通用
            host = urllib.parse.urlparse(url).hostname or ""
            with pool_lock:
                bucket = pool.get(host)
                if bucket:
                    return bucket.pop()
            return _KeepAlive(url)

        def release_ka(ka: _KeepAlive) -> None:
            with pool_lock:
                pool.setdefault(ka.host, []).append(ka)

        def fetch_one(idx: int) -> int:
            if job.stop_event.is_set():
                return 0
            url = pairs[idx][0]
            dest = self._seg_path(parts_dir, idx)
            tmp = dest.with_suffix(".tmp")
            ka = acquire_ka(url)
            try:
                code, rh, body = 0, None, b""
                for _hop in range(3):   # 最多跟 2 跳重定向，每一跳都重新过安全校验
                    p = urllib.parse.urlparse(url)
                    code, rh, body = ka.get(p.path + ("?" + p.query if p.query else ""))
                    if code not in (301, 302, 303, 307, 308):
                        break
                    loc = rh.get("Location") or ""
                    if not loc:
                        raise ApiError(code, f"HTTP {code}")
                    url = urllib.parse.urljoin(url, loc)
                    if urllib.parse.urlparse(url).hostname != ka.host:
                        ka.close()
                        ka = acquire_ka(url)   # 跨主机：改用目标主机经校验的连接
                else:
                    raise ApiError(-2, "重定向次数过多")
                if code != 200 or not body:
                    raise ApiError(code, f"HTTP {code}")
                tmp.write_bytes(body)
                tmp.replace(dest)
                job.bump(len(body))
                return len(body)
            except Exception:
                ka.close()  # 出错的连接不再回池
                raise
            finally:
                if ka._conn is not None:
                    release_ka(ka)

        try:
            with ThreadPoolExecutor(max_workers=int(self.store.settings.get("concurrency", 6))) as ex:
                futures = {ex.submit(fetch_one, i): i for i in indexes}
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception as e:
                        idx = futures[fut]
                        self.store.log(f"分段 {idx} 失败：{e}", "error")
                    if job.stop_event.is_set():
                        for f in futures:
                            f.cancel()
                        break
        finally:
            with pool_lock:
                for bucket in pool.values():
                    for ka in bucket:
                        ka.close()
                pool.clear()

    # ------------- 合成 -------------
    def _merge(self, parts_dir: Path, final: Path, ffmpeg: str,
               durs: list[float], stop_event: threading.Event) -> tuple[bool, str]:
        """用本地 HLS 播放列表交给 ffmpeg HLS demuxer 合成（按 durs 顺序逐段列出、
        不扫描目录，杂散/多余分段不会混入），时间戳/时长与远端 m3u8 直接读取时一致
        （concat demuxer 会导致时间戳错乱）。
        先写临时文件、成功后转正，避免失败留下被误判为已完成的半截成品；
        暂停时终止 ffmpeg 并删除半成品，分段保留待续跑重新合成。"""
        tmp_out = _merging_tmp(final)
        try:
            final.parent.mkdir(parents=True, exist_ok=True)
            lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4"]
            for i, dur in enumerate(durs):
                lines.append(f"#EXTINF:{max(dur, 0.001):.3f},")
                lines.append(self._seg_path(parts_dir, i).name)
            lines.append("#EXT-X-ENDLIST")
            list_file = parts_dir / "local.m3u8"
            list_file.write_text("\n".join(lines) + "\n", encoding="ascii")
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                   "-allowed_extensions", "ALL", "-protocol_whitelist", "file",
                   "-i", list_file.name,
                   "-c", "copy", str(tmp_out.resolve())]
            proc = subprocess.Popen(cmd, cwd=parts_dir, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, text=True)
            stderr = ""
            deadline = time.monotonic() + 3600
            while True:
                try:
                    stderr = proc.communicate(timeout=0.5)[1] or ""
                    break
                except subprocess.TimeoutExpired:
                    if not (stop_event.is_set() or time.monotonic() > deadline):
                        continue
                    proc.kill()
                    proc.communicate()
                    tmp_out.unlink(missing_ok=True)
                    if stop_event.is_set():
                        return False, "已暂停"
                    return False, "合成超时（超过 1 小时）"
            if proc.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
                tmp_out.unlink(missing_ok=True)
                return False, stderr.strip()[-400:] or "未知错误"
            os.replace(tmp_out, final)
            return True, ""
        except (OSError, subprocess.SubprocessError) as e:
            tmp_out.unlink(missing_ok=True)
            return False, str(e)
