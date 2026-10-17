"""LuboDown —— 配置与会话数据库持久化。"""
from __future__ import annotations

import json
import re
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
DELETED_FILE = CONFIG_DIR / "deleted.json"
LOG_FILE = CONFIG_DIR / "lubo.log"

# 用户删除的场次在此期限内不再被检查重新入库（B 站回放只保留约 14 天，留足余量）
DELETED_KEEP_SEC = 30 * 86400

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
    "name_with_anchor": False,      # 文件名附加主播名与开播时间
    "ffmpeg_path": "ffmpeg",        # ffmpeg 可执行文件
}

# 会话状态机: new -> queued -> downloading -> merging -> done / failed / paused
# "进行中"状态：进程重启后残留的这些状态恢复为 paused（分段保留、可续传）。
# 状态判断统一引用此集合，新增状态时只改这里。
ACTIVE_STATES = frozenset({"queued", "downloading", "merging"})

# live_key 会进入文件系统路径（分段目录 .parts_{key}、成品文件名等），
# 在入库这道唯一的门上拒收一切非纯 ID 形态，下载/删除/清理等下游路径天然继承该保证。
VALID_KEY_RE = re.compile(r"[0-9A-Za-z]{6,64}")


class Store:
    """线程安全的设置 + 会话库 + 内存日志环。"""

    def __init__(self) -> None:
        CONFIG_DIR.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self.settings = self._load(SETTINGS_FILE, DEFAULT_SETTINGS)
        for k, v in DEFAULT_SETTINGS.items():   # 旧版本配置文件补齐新增字段
            self.settings.setdefault(k, v)
        if self._normalize_download_dir():      # 便携化归一，仅在有变化时落盘
            self._save(SETTINGS_FILE, self.settings)
        self.sessions: dict[str, dict] = self._load(SESSIONS_FILE, {})
        self._normalize_session_files()
        self.deleted: dict[str, int] = self._load(DELETED_FILE, {})   # live_key -> 删除时间戳
        if self._prune_deleted():
            self._save(DELETED_FILE, self.deleted)
        self._logs: list[dict] = []

    @staticmethod
    def _is_default_dir(path: str) -> bool:
        try:
            return Path(path).resolve() == DEFAULT_DOWNLOAD_DIR.resolve()
        except OSError:
            return False

    def _normalize_download_dir(self) -> bool:
        """下载目录归一：等于默认位置或为空都存空串（跟随程序目录，便携），
        并去掉复制路径时夹带的引号。返回归一是否改变了存储值。"""
        cur = (self.settings.get("download_dir") or "").strip().strip('"')
        if not cur or self._is_default_dir(cur):
            cur = ""
        changed = cur != self.settings.get("download_dir")
        self.settings["download_dir"] = cur
        return changed

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
        for i in range(3):   # 目标文件可能被杀毒/网盘同步短暂占用
            try:
                tmp.replace(file)
                return
            except PermissionError:
                if i == 2:
                    raise
                time.sleep(0.3)

    def _prune_deleted(self) -> bool:
        """清掉过期的删除记录，返回是否有变化。"""
        cutoff = time.time() - DELETED_KEEP_SEC
        expired = [k for k, t in self.deleted.items()
                   if not isinstance(t, (int, float)) or t < cutoff]
        for k in expired:
            del self.deleted[k]
        return bool(expired)

    # ---------- settings ----------
    def save_settings(self, patch: dict) -> None:
        with self._lock:
            for k, v in patch.items():
                if k in self.settings:
                    self.settings[k] = v
            self._normalize_download_dir()
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
        """按 live_key 合并场次信息，返回新增数量（live_key 形态校验见 VALID_KEY_RE）。"""
        added = 0
        with self._lock:
            for it in items:
                key = str(it.get("live_key") or "")
                if not VALID_KEY_RE.fullmatch(key):
                    if key:
                        self.log(f"忽略异常 live_key 的场次：{key[:24]}")
                    continue
                if key in self.deleted:   # 用户删过的场次不再重新入库（否则会被自动下载再下一遍）
                    continue
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
            self.deleted[live_key] = int(time.time())
            self._prune_deleted()
            self._save(DELETED_FILE, self.deleted)

    def all_sessions(self) -> list[dict]:
        with self._lock:
            return sorted(self.sessions.values(),
                          key=lambda s: -int(s.get("start_time") or 0))

    def snapshot_sessions(self) -> dict[str, dict]:
        """锁内浅拷贝：后台线程遍历返回值，不会被 upsert 的并发增删打断迭代。"""
        with self._lock:
            return dict(self.sessions)

    # ---------- logs ----------
    def log(self, msg: str, level: str = "info") -> None:
        entry = {"t": time.strftime("%m-%d %H:%M:%S"), "level": level, "msg": msg}
        line = f"[{entry['t']}] [{level}] {msg}\n"
        with self._lock:
            self._logs.append(entry)
            self._logs = self._logs[-300:]
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
            except (OSError, UnicodeEncodeError):  # pywebview 会把 None 的 stdout 换成系统 ANSI 编码的 devnull，非中文系统写中文会抛错
                pass

    def recent_logs(self, n: int = 200) -> list[dict]:
        with self._lock:
            return list(self._logs[-n:])
