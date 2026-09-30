"""LuboDown —— 配置与会话数据库持久化。"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

# 打包成 exe 后，程序目录取 exe 所在位置（而非解包临时目录）
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = APP_DIR / "config"
DEFAULT_DOWNLOAD_DIR = APP_DIR / "downloads"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
SESSIONS_FILE = CONFIG_DIR / "sessions.json"
LOG_FILE = CONFIG_DIR / "lubo.log"

DEFAULT_SETTINGS = {
    "sessdata": "",
    "bili_jct": "",
    "anchors": [],                  # 关注的主播，默认为空，需在设置中添加
    "download_dir": "",             # 空 = 跟随程序所在目录下的 downloads
    "check_interval_min": 60,       # 定时检查间隔（分钟）
    "auto_check": True,             # 定时检查开关
    "auto_download": False,         # 检查到新场次是否自动下载
    "auto_download_since": 0,       # 仅自动下载该时间戳之后开播的场次（0=不限）
    "concurrency": 6,               # 分段下载并发数
    "keep_parts": False,            # 合成后是否保留分段文件
    "ffmpeg_path": "ffmpeg",        # ffmpeg 可执行文件
}

# 会话状态机: new -> downloading -> merging -> done / failed / paused
STATUS_TEXT = {
    "new": "未下载",
    "queued": "排队中",
    "downloading": "下载中",
    "merging": "合成中",
    "done": "已完成",
    "failed": "失败",
    "paused": "已暂停",
}


class Store:
    """线程安全的设置 + 会话库 + 内存日志环。"""

    def __init__(self) -> None:
        CONFIG_DIR.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.settings = self._load(SETTINGS_FILE, DEFAULT_SETTINGS)
        for k, v in DEFAULT_SETTINGS.items():   # 旧版本配置文件补齐新增字段
            self.settings.setdefault(k, v)
        # 便携化：下载目录为空 = 跟随程序所在目录；历史遗留的旧安装绝对路径一并归一
        cur = (self.settings.get("download_dir") or "").strip().strip('"')
        if not cur or self._is_default_dir(cur):
            if cur:  # 旧版本写入的绝对路径（等于默认位置）→ 归一为空串并落盘
                self.settings["download_dir"] = ""
                self._save(SETTINGS_FILE, self.settings)
            self.settings["download_dir"] = ""
        self.sessions: dict[str, dict] = self._load(SESSIONS_FILE, {})
        self._normalize_session_files()
        self._logs: list[dict] = []

    @staticmethod
    def _is_default_dir(path: str) -> bool:
        try:
            return Path(path).resolve() == DEFAULT_DOWNLOAD_DIR.resolve()
        except OSError:
            return False

    def download_root(self) -> Path:
        """实际使用的下载根目录（未自定义时跟随程序目录）。"""
        v = (self.settings.get("download_dir") or "").strip()
        return Path(v) if v else DEFAULT_DOWNLOAD_DIR

    def resolve_session_file(self, rec: dict) -> Path | None:
        """会话记录里的文件路径 -> 实际绝对路径（相对路径按下载根目录解析）。"""
        f = (rec.get("file") or "").strip()
        if not f:
            return None
        p = Path(f)
        return p if p.is_absolute() else self.download_root() / p

    def _normalize_session_files(self) -> None:
        """把历史遗留的绝对文件路径转为相对下载根目录的路径（便携化）。"""
        changed = False
        root = self.download_root()
        for rec in self.sessions.values():
            f = (rec.get("file") or "").strip()
            if not f or not Path(f).is_absolute():
                continue
            try:
                rec["file"] = str(Path(f).relative_to(root))
                changed = True
            except ValueError:
                pass    # 自定义目录等外部路径，保留原样
        if changed:
            self._save(SESSIONS_FILE, self.sessions)

    @staticmethod
    def _load(file: Path, default):
        if file.exists():
            try:
                return json.loads(file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return json.loads(json.dumps(default))  # deep copy

    @staticmethod
    def _save(file: Path, data) -> None:
        tmp = file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(file)

    # ---------- settings ----------
    def save_settings(self, patch: dict) -> None:
        with self._lock:
            for k, v in patch.items():
                if k in self.settings:
                    self.settings[k] = v
            # 下载目录规范化：等于默认位置时存空串（便携），并去掉复制路径带来的引号
            cur = (self.settings.get("download_dir") or "").strip().strip('"')
            if not cur or self._is_default_dir(cur):
                cur = ""
            self.settings["download_dir"] = cur
            self._save(SETTINGS_FILE, self.settings)

    def masked_settings(self) -> dict:
        """给前端的设置视图：凭证只回显尾号。"""
        with self._lock:
            s = json.loads(json.dumps(self.settings))
            s["download_dir"] = str(self.download_root())
            for key in ("sessdata", "bili_jct"):
                v = s.get(key) or ""
                s[key] = f"••••（尾号 {v[-4:]}）" if len(v) > 4 else ("••••" if v else "")
                s[f"has_{key}"] = bool(v)
            return s

    # ---------- sessions ----------
    def upsert_sessions(self, items: list[dict]) -> int:
        """按 live_key 合并场次信息，返回新增数量。"""
        added = 0
        with self._lock:
            for it in items:
                key = it["live_key"]
                if key not in self.sessions:
                    it.setdefault("status", "new")
                    self.sessions[key] = it
                    added += 1
                else:
                    self.sessions[key].update(
                        {k: v for k, v in it.items() if k != "status"})
            self._save(SESSIONS_FILE, self.sessions)
        return added

    def update_session(self, live_key: str, **fields) -> None:
        with self._lock:
            if live_key in self.sessions:
                self.sessions[live_key].update(fields)
                self._save(SESSIONS_FILE, self.sessions)

    def remove_session(self, live_key: str) -> None:
        with self._lock:
            self.sessions.pop(live_key, None)
            self._save(SESSIONS_FILE, self.sessions)

    def all_sessions(self) -> list[dict]:
        with self._lock:
            return sorted(self.sessions.values(),
                          key=lambda s: -int(s.get("start_time") or 0))

    # ---------- logs ----------
    def log(self, msg: str, level: str = "info") -> None:
        entry = {"t": time.strftime("%m-%d %H:%M:%S"), "level": level, "msg": msg}
        with self._lock:
            self._logs.append(entry)
            self._logs = self._logs[-300:]
        line = f"[{entry['t']}] [{level}] {msg}\n"
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
                lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
                LOG_FILE.write_text("\n".join(lines[-2000:]) + "\n", encoding="utf-8")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
        if sys.stdout is not None:  # pythonw 下无控制台，stdout 为 None
            try:
                print(line, end="")
            except OSError:
                pass

    def recent_logs(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._logs[-n:])
