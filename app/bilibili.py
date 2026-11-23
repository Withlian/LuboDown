"""LuboDown —— B站直播回放接口客户端。

安全请求层：仅 https、域名白名单、解析 IP 必须全部为公网地址、
连接固定到已校验 IP（防 DNS rebinding）、重定向逐跳复检。
接口清单见 docs/replay-api-notes.md。
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import json
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

ALLOWED_SUFFIXES = (
    "bilibili.com",
    "bilivideo.com",
    "bilivideo.cn",
    "hdslb.com",
    "akamaized.net",
)

_pinned: dict[str, list[str]] = {}
_pin_lock = threading.Lock()


class RequestBlocked(Exception):
    """URL 未通过安全校验。"""


def _doh_resolve(host: str) -> list[str]:
    """fake-ip 代理环境（如 Clash TUN）下 getaddrinfo 会被劫持，
    改用公共 DNS 的 HTTP 接口（IP 直连，不走系统代理、不依赖本地 DNS）解析真实地址。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for doh in (f"http://223.5.5.5/resolve?name={host}&type=A",
                f"http://119.29.29.29/d?dn={host}"):
        try:
            with opener.open(doh, timeout=8) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
            if text.startswith("{"):   # 阿里 DoH：JSON，Answer 里含 CNAME 链，只取 A 记录
                answer = json.loads(text).get("Answer") or []
                cands = [a.get("data") for a in answer if isinstance(a, dict) and a.get("type") == 1]
            else:                      # 腾讯 HTTPDNS：纯文本 "ip1;ip2"
                cands = text.split(";")
            ips = []
            for c in cands:
                try:
                    ips.append(str(ipaddress.IPv4Address(str(c).strip())))
                except ValueError:
                    continue
            if ips:
                return ips
        except (OSError, ValueError, http.client.HTTPException):  # 含 JSONDecodeError
            continue
    return []


def _resolve_global(host: str) -> list[str]:
    with _pin_lock:
        if host in _pinned:
            return _pinned[host]
    infos = socket.getaddrinfo(host, None)
    ips = sorted({i[4][0] for i in infos})
    # 全部解析结果都落在内网/保留段 → 疑似 fake-ip 代理劫持，改用 DoH 取真实地址
    if ips and all(not ipaddress.ip_address(ip).is_global for ip in ips):
        ips = sorted(set(_doh_resolve(host))) or ips
    for ip in ips:
        if not ipaddress.ip_address(ip).is_global:
            raise RequestBlocked(f"域名 {host} 解析到非公网地址 {ip}")
    with _pin_lock:
        _pinned[host] = ips
    return ips


def check_url(url: str, suffixes: tuple[str, ...] = ALLOWED_SUFFIXES) -> tuple[str, list[str]]:
    """校验并固定 URL。返回 (hostname, 公网IP列表)。suffixes 供扫码登录放宽到 passport 域名。"""
    p = urllib.parse.urlparse(url)
    if p.scheme != "https":
        raise RequestBlocked(f"拒绝非 https 请求: {url[:80]}")
    host = (p.hostname or "").lower()
    if not any(host == s or host.endswith("." + s) for s in suffixes):
        raise RequestBlocked(f"拒绝非白名单域名: {host}")
    return host, _resolve_global(host)


def refresh_pin(host: str) -> None:
    """清除某域名的已校验 IP 缓存（长下载中 CDN 换 IP 时允许重新解析）。"""
    with _pin_lock:
        _pinned.pop(host, None)


class _PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, ips=None, **kw):
        super().__init__(host, **kw)
        self._ips = ips or []

    def _candidate_ips(self):
        """先试已固定的 IP；全部连不上才视为解析结果失效，
        清缓存重新解析一轮再试（站点换 IP、TUN 代理环境变化后的自愈路径，
        opener 与 KeepAlive 两条传输共用）。"""
        yield from self._ips
        refresh_pin(self.host)
        yield from _resolve_global(self.host)

    def connect(self):
        err: OSError | None = None
        for ip in self._candidate_ips():
            try:
                self.sock = socket.create_connection((ip, self.port), timeout=self.timeout)
                break
            except OSError as e:
                err = e
        if self.sock is None:
            raise err if err else OSError("no address")
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        host, ips = check_url(req.full_url)  # 重定向后的每一跳都会再进这里
        return self.do_open(functools.partial(_PinnedConnection, ips=ips), req,
                            context=self._context)


# 显式空 ProxyHandler 覆盖默认行为：B 站均为国内直连端点，
# 不读取系统代理——避免代理工具（Clash 等）换端口/退出后 urllib
# 仍把请求发给已失效的 127.0.0.1:7890 导致连接被拒。
_no_proxy = urllib.request.ProxyHandler({})
_opener = urllib.request.build_opener(
    urllib.request.HTTPRedirectHandler(),
    urllib.request.HTTPDefaultErrorHandler(),
    _no_proxy,
    _PinnedHandler(),
)


class ApiError(Exception):
    def __init__(self, code, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def request(url: str, cookie: str = "", timeout: int = 30, retries: int = 2,
            raw: bool = False):
    """安全 GET。raw=False 时解析 JSON 并检查 B 站 code；raw=True 返回二进制。"""
    headers = {"User-Agent": UA, "Referer": "https://live.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with _opener.open(req, timeout=timeout) as resp:
                content = resp.read()
            if raw:
                return content
            obj = json.loads(content.decode("utf-8", "replace"))
            if isinstance(obj, dict) and "code" in obj:
                code = obj.get("code")
                if code != 0:
                    raise ApiError(code, str(obj.get("message") or obj.get("msg") or "未知错误"))
            return obj
        except urllib.error.HTTPError as e:  # 403/404 等不重试
            raise ApiError(e.code, f"HTTP {e.code}: {url[:80]}") from e
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                http.client.HTTPException) as e:   # IncompleteRead 等读响应中断不是 OSError
            last_err = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    msg = f"网络错误: {last_err}"
    winerror = getattr(last_err, "winerror", None)
    if winerror == 10061:
        msg += "（连接被拒绝：若开启了代理工具，请确认其监听端口与系统代理一致，或重启代理后重试）"
    elif winerror in (10054, 10060):
        msg += "（连接被重置或超时：请检查网络与代理节点）"
    raise ApiError(-1, msg)


def cookie_str(sessdata: str, bili_jct: str, dede_user_id: str = "") -> str:
    """拼 Cookie 头。DedeUserID 并非所有接口都需要，但 nav 等账号接口缺它会判未登录。"""
    pairs = [f"SESSDATA={sessdata}"] if sessdata else []
    if bili_jct:
        pairs.append(f"bili_jct={bili_jct}")
    if dede_user_id:
        pairs.append(f"DedeUserID={dede_user_id}")
    return "; ".join(pairs)


API_BASE = "https://api.live.bilibili.com"


class BiliClient:
    def __init__(self, store):
        self.store = store

    def _cookie(self) -> str:
        s = self.store.settings
        return cookie_str(s.get("sessdata", ""), s.get("bili_jct", ""),
                          s.get("dede_user_id", ""))

    def get_master_info(self, uid: int) -> dict:
        """公开接口：uid -> {name, room_id}，用于添加主播时自动取名。

        注意：该接口对不存在的 uid 也返回 code=0，只是 uname 为空、room_id 为 0，
        所以「是否有效主播」必须用 name 是否为空判断，不能只看 code。
        """
        r = request(f"{API_BASE}/live_user/v1/Master/info?uid={uid}")
        data = r.get("data") or {}
        info = data.get("info") or {}
        return {"name": info.get("uname") or "",
                "room_id": data.get("room_id") or None}

    def get_room_info(self, room_id: int) -> dict | None:
        """房间号 -> {uid, room_id, title}。房间不存在或未开播返回 None。"""
        try:
            r = request(f"{API_BASE}/room/v1/Room/get_info?room_id={room_id}")
        except ApiError:
            return None
        d = r.get("data") or {}
        if not d.get("uid"):
            return None
        return {"uid": int(d["uid"]),
                "room_id": int(d.get("room_id") or 0),
                "title": d.get("title") or ""}

    def resolve_anchor(self, value: int) -> dict:
        """把用户输入识别成主播：先当 UID，失败再当直播间房号。

        用户常直接粘 live.bilibili.com/81004 里的房间号，而回放接口要的是 UID，
        传错会得到 [-301 没有剪辑权限]，看起来像「没授权」其实主播都找错了。
        返回 {uid, name, room_id, via_room, valid}；valid=False 表示既不是主播也不是房间。
        """
        try:
            info = self.get_master_info(value)
        except ApiError:
            info = {"name": "", "room_id": None}
        if info["name"]:
            return {"uid": value, "name": info["name"], "room_id": info["room_id"],
                    "via_room": False, "valid": True}
        room = self.get_room_info(value)
        if room:
            r_uid = room["uid"]
            try:
                name = self.get_master_info(r_uid)["name"]
            except ApiError:
                name = ""
            return {"uid": r_uid, "name": name or str(r_uid), "room_id": room["room_id"],
                    "via_room": True, "valid": True}
        return {"uid": value, "name": "", "room_id": None, "via_room": False, "valid": False}

    def get_replay_list(self, uid: int, page: int = 1, page_size: int = 20) -> dict:
        """他人回放列表（需该主播授权剪辑权限）。time_range=3 即近14天。

        注意：B 站 2026-10 起 page_size 超过 20 会返回空列表（total 仍正确），
        因此这里上限取 20，翻页由 fetch_all_replays 完成。"""
        url = (f"{API_BASE}/xlive/web-room/v1/videoService/GetOtherSliceList"
               f"?live_uid={uid}&time_range=3&page={page}&page_size={page_size}")
        r = request(url, cookie=self._cookie())
        return r.get("data") or {}

    def fetch_all_replays(self, uid: int) -> list[dict]:
        """翻页拉全（每页 20 条，防御性翻页）。"""
        out: list[dict] = []
        for page in range(1, 6):
            data = self.get_replay_list(uid, page=page)
            items = data.get("replay_info") or []
            out.extend(items)
            total = (data.get("pagination") or {}).get("total") or 0
            if len(out) >= total or not items:
                break
        return out

    def get_slice_entries(self, live_key: str, start: int, end: int, uid: int) -> list[dict]:
        """他人场次切片流（需授权）。返回该场次**全部**回放子段（按时间排序）。

        手机竖屏直播会产生多个碎片子段（推流重连所致），必须全部合并下载。
        """
        url = (f"{API_BASE}/xlive/web-room/v1/videoService/GetUserSliceStream"
               f"?live_key={live_key}&start_time={start}&end_time={end}&live_uid={uid}")
        r = request(url, cookie=self._cookie())
        lst = (r.get("data") or {}).get("list") or []
        entries = [{"start_time": int(e.get("start_time") or 0),
                    "end_time": int(e.get("end_time") or 0),
                    "stream": e.get("stream") or "", "type": e.get("type")}
                   for e in lst if e.get("stream")]
        if not entries:
            raise ApiError(-2, "回放没有可用的视频流（可能尚未生成或已失效）")
        entries.sort(key=lambda e: e["start_time"])
        return entries

    def get_account(self) -> dict | None:
        """当前登录账号（nav 接口）：{mid, name, face}。未登录或凭证失效返回 None。"""
        try:
            r = request("https://api.bilibili.com/x/web-interface/nav", cookie=self._cookie())
        except ApiError:
            return None
        data = r.get("data") or {}
        if not data.get("isLogin"):
            return None
        return {"mid": int(data.get("mid") or 0),
                "name": data.get("uname") or "",
                "face": data.get("face") or ""}

    def verify_login(self) -> dict:
        """用列表接口探测登录态与各主播的剪辑权限（逐个尝试，任一通过即可）。

        失败时区分三种情况，避免把「UID 填错」误报成「未授权」：
          1. -101       登录态失效；
          2. 301 + 该账号根本不是主播（常见于把直播间房号当 UID）→ 返回可自动修正的 fix；
          3. 301 + 主播存在        → 真正的未授权。
        """
        anchors = self.store.settings.get("anchors", [])
        parsed = []
        for a in anchors:
            try:
                parsed.append((int(a["uid"]), a.get("name") or str(a["uid"])))
            except (KeyError, TypeError, ValueError):
                continue    # 跳过配置中的异常条目
        if not parsed:
            return {"ok": False,
                    "message": "未配置主播" if not anchors else "主播配置异常，请在设置中重新添加"}
        denied: list[str] = []
        wrong: list[str] = []
        fixes: list[dict] = []
        last_err = ""
        for uid, name in parsed:
            try:
                self.get_replay_list(uid, page=1, page_size=1)
                return {"ok": True, "message": f"登录有效，{name} 已授权剪辑权限", "fix": fixes}
            except ApiError as e:
                if e.code == -101:   # 登录态全局无效，无需再试其余主播
                    return {"ok": False, "message": "登录态已失效，请在设置中重新扫码登录"}
                if e.code == 301:
                    hit = self.resolve_anchor(uid)
                    if hit["valid"] and hit["via_room"]:
                        # 输入的是房间号：不是权限问题，记下正确 UID 供上层自动修正
                        fixes.append({"from": uid, "to": hit["uid"], "name": hit["name"]})
                        wrong.append(f"{uid} 是直播间房号（主播 UID 应为 {hit['uid']}）")
                    elif hit["valid"]:
                        denied.append(f"{name}（UID {uid}）")
                    else:
                        wrong.append(f"{uid}（查不到这个主播，请确认填的是 UID）")
                else:
                    last_err = f"[{e.code}] {e.message}"
        parts = []
        if denied:
            parts.append(f"{'、'.join(denied)} 未授权你的剪辑权限")
        if wrong:
            parts.append(f"{'、'.join(wrong)}")
        if not parts:
            return {"ok": False, "message": last_err or "检测失败"}
        return {"ok": False, "message": "已登录，但 " + "；".join(parts), "fix": fixes}

