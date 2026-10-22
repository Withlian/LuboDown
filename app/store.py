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

# 全部设置的单一权威定义：键 -> (默认值, 下限, 上限)。
# 类型即默认值的类型（str/bool/int/list）；下限/上限仅对 int 有意义，None=不限。
# DEFAULT_SETTINGS、api.save_settings 的类型清洗与 main.py 的窗口 min_size 均由此派生，
# 新增设置在这里加一行（前端另加对应控件）即可。
SETTING_SPECS = {
    "sessdata": ("", None, None),           # 凭证，只存本机
    "bili_jct": ("", None, None),
    "anchors": ([], None, None),            # 关注的主播，只经 add/remove_anchor 增删
    "download_dir": ("", None, None),       # 空 = 跟随程序所在目录下的 downloads，归一见 _normalize_download_dir
    "check_interval_min": (60, 5, 10_080),  # 定时检查间隔（分钟）
    "auto_check": (True, None, None),       # 定时检查开关
    "auto_download": (False, None, None),   # 检查到新场次是否自动下载
    "auto_download_since": (0, 0, None),    # 仅自动下载该时间戳之后开播的场次（0=不限）
    "revive_deleted": (True, None, None),   # 刷新/检查时让删除过的场次重新入库（回到未下载、不自动下载）
    "concurrency": (6, 1, 16),              # 分段下载并发数
    "keep_parts": (False, None, None),      # 合成后是否保留分段文件
    "name_with_anchor": (False, None, None),  # 文件名附加主播名与开播时间
    "ffmpeg_path": ("ffmpeg", None, None),  # ffmpeg 可执行文件
    "window_width": (1240, 980, 3840),      # 窗口大小（逻辑像素，固定不可拖动调整，在设置中修改）
    "window_height": (820, 620, 2160),
}

DEFAULT_SETTINGS = {k: spec[0] for k, spec in SETTING_SPECS.items()}


def clamp_setting(key: str, value) -> int:
    """数值设置收敛到 SETTING_SPECS 区间；类型非法时抛 TypeError/ValueError。"""
    _, lo, hi = SETTING_SPECS[key]
    v = int(value)
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _deleted_at(entry) -> float | None:
    """删除记录的时间戳。新格式为 {"t": 时间戳, "rec": 场次快照}，旧版本只存时间戳。"""
    if isinstance(entry, dict):
        entry = entry.get("t")
    if isinstance(entry, bool) or not isinstance(entry, (int, float)):
        return None
    return entry

# 会话状态机: new -> queued -> downloading -> merging -> done / failed / paused
# （另有两个只存在于界面视图的状态：missing=已完成但成品已不在磁盘上，由 api.get_state 判定、不落库；
#  deleted=「最近删除」里可恢复的场次，来自 deleted.json）
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
        # live_key -> {"t": 删除时间戳, "rec": 删除时的场次快照（供恢复）}；旧版本的值是裸时间戳
        self.deleted: dict[str, dict | int] = self._load(DELETED_FILE, {})
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
        expired = [k for k, v in self.deleted.items()
                   if (t := _deleted_at(v)) is None or t < cutoff]
        for k in expired:
            del self.deleted[k]
        return bool(expired)

    # ---------- settings ----------
    def save_settings(self, patch: dict) -> None:
        with self._lock:
            old_root = self.download_root()
            old_dir = self.settings.get("download_dir")
            for k, v in patch.items():
                if k in self.settings:
                    self.settings[k] = v
            self._normalize_download_dir()
            try:
                self._save(SETTINGS_FILE, self.settings)
                if self.download_root() != old_root:
                    self._rebase_session_files(old_root)
            except OSError:
                # 回滚下载目录（内存与磁盘）：留着新目录的话，重试时旧==新会让
                # rebase 被跳过，会话文件路径停在半迁移状态
                self.settings["download_dir"] = old_dir
                try:
                    self._save(SETTINGS_FILE, self.settings)
                except OSError:
                    pass
                raise

    def _rebase_session_files(self, old_root: Path) -> None:
        """下载目录改动后修正已完成场次的文件路径：库中相对路径按当前根目录解析，
        不修正的话旧目录里的成品会全部"找不到"（大小为 0、无法定位或删除）。
        文件已随目录搬到新根目录下的保持相对；仍留在旧目录的改存绝对路径；
        原本记为绝对路径、恰好落在新根目录内的转回相对。"""
        new_root = self.download_root()
        changed = False
        for rec in self.sessions.values():
            f = (rec.get("file") or "").strip()
            if not f:
                continue
            p = Path(f)
            if not p.is_absolute():
                if (new_root / p).exists() or not (old_root / p).exists():
                    continue
                p = old_root / p
            try:
                value = str(p.relative_to(new_root))
            except ValueError:
                value = str(p)
            if value != f:
                rec["file"] = value
                changed = True
        if changed:
            self._save(SESSIONS_FILE, self.sessions)

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
    def upsert_sessions(self, items: list[dict]) -> tuple[int, int]:
        """按 live_key 合并场次信息，返回 (新增数量, 恢复的已删除场次数量)
        （live_key 形态校验见 VALID_KEY_RE）。"""
        added = revived = 0
        tombs_changed = False
        with self._lock:
            revive = bool(self.settings.get("revive_deleted"))
            for it in items:
                key = str(it.get("live_key") or "")
                if not VALID_KEY_RE.fullmatch(key):
                    if key:
                        self.log(f"忽略异常 live_key 的场次：{key[:24]}")
                    continue
                if key in self.deleted and revive and key not in self.sessions:
                    # 删过的场次随刷新回到列表、状态为未下载；打上 no_auto 标记，
                    # 定时检查的自动下载不会把它当新场次再下一遍，需要时手动下载
                    del self.deleted[key]
                    tombs_changed = True
                    self.sessions[key] = {**it, "status": "new", "no_auto": True}
                    revived += 1
                    continue
                if key in self.deleted:   # 关闭恢复时，删过的场次不再重新入库（否则会被自动下载再下一遍）
                    tomb = self.deleted[key]
                    if not (isinstance(tomb, dict) and isinstance(tomb.get("rec"), dict)):
                        # 旧版本的删除记录只有时间戳：补上场次快照，「最近删除」才能列出并恢复
                        self.deleted[key] = {"t": _deleted_at(tomb) or int(time.time()), "rec": it}
                        tombs_changed = True
                    continue
                if key not in self.sessions:
                    it.setdefault("status", "new")
                    self.sessions[key] = it
                    added += 1
                else:
                    self.sessions[key].update(
                        {k: v for k, v in it.items() if k != "status"})
            self._save(SESSIONS_FILE, self.sessions)
            if tombs_changed:
                self._save(DELETED_FILE, self.deleted)
        return added, revived

    def update_session(self, live_key: str, **fields) -> None:
        with self._lock:
            if live_key in self.sessions:
                self.sessions[live_key].update(fields)
                self._save(SESSIONS_FILE, self.sessions)

    def remove_session(self, live_key: str) -> None:
        with self._lock:
            rec = self.sessions.pop(live_key, None)
            old = self.deleted.get(live_key)
            if rec is None and isinstance(old, dict):   # 重复删除：保留已有快照，不让"可恢复"丢失
                rec = old.get("rec")
            self._save(SESSIONS_FILE, self.sessions)
            entry: dict = {"t": int(time.time())}
            if rec:
                entry["rec"] = rec
            self.deleted[live_key] = entry
            self._prune_deleted()
            self._save(DELETED_FILE, self.deleted)

    def deleted_sessions(self) -> list[dict]:
        """「最近删除」：带场次快照、可恢复的删除记录（新的在前），附 deleted_at。"""
        with self._lock:
            out = [dict(v["rec"], live_key=k, deleted_at=int(_deleted_at(v) or 0))
                   for k, v in self.deleted.items()
                   if isinstance(v, dict) and isinstance(v.get("rec"), dict)
                   and k not in self.sessions]
        return sorted(out, key=lambda s: -int(s.get("start_time") or 0))

    def restore_session(self, live_key: str) -> dict | None:
        """撤销删除：按未下载的场次重新入库，返回恢复后的记录；没有可恢复的快照时返回 None。
        删除时成品与分段已清理，状态一律回到 new、下载从头开始
        （磁盘上若仍留有成品，下载流程会识别并直接标记完成）；与刷新时的自动恢复一样不参与自动下载。"""
        with self._lock:
            tomb = self.deleted.get(live_key)
            rec = tomb.get("rec") if isinstance(tomb, dict) else None
            if not isinstance(rec, dict):
                return None
            if live_key not in self.sessions:
                self.sessions[live_key] = {
                    **{k: v for k, v in rec.items() if k not in ("status", "error", "file")},
                    "live_key": live_key, "status": "new", "no_auto": True}
                self._save(SESSIONS_FILE, self.sessions)
            del self.deleted[live_key]
            self._save(DELETED_FILE, self.deleted)
            return self.sessions[live_key]

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
