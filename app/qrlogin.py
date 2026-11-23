"""LuboDown —— B 站扫码登录。

取代原来手工从浏览器 Cookie 里复制 SESSDATA / bili_jct 的方式。
流程与 JLiverTool（crates/jlivertool-core/src/bilibili/api.rs 的 qr_generate /
qr_poll / resolve_qr_login）一致：

1. GET passport.bilibili.com/x/passport-login/web/qrcode/generate -> {url, qrcode_key}
2. 每 2 秒 GET .../qrcode/poll?qrcode_key=...，data.code：
       0=成功  86101=未扫码  86090=已扫码待确认  86038=已过期
3. 成功后 data.url 是一串携带 Cookie 的跳转地址：优先从查询串里取
   SESSDATA / bili_jct / DedeUserID（B 站把凭证直接拼在 crossDomain 链接中），
   取不全时逐跳跟随 302 并收集 Set-Cookie。

请求仍走 bilibili.py 的安全层（仅 https、域名白名单、解析结果必须是公网 IP、
连接固定到已校验 IP、不读系统代理），只是白名单放宽到 passport 域名。
"""
from __future__ import annotations

import base64
import functools
import io
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .bilibili import UA, ApiError, _PinnedConnection, check_url

PASSPORT = "https://passport.bilibili.com/x/passport-login/web/qrcode"
# 扫码成功后的换票跳转可能落在 passport.biligame.com
LOGIN_SUFFIXES = ("bilibili.com", "biligame.com")
MAX_REDIRECTS = 5

POLL_INTERVAL = 2.0
MAX_POLLS = 90          # 约 3 分钟；B 站二维码本身 180 秒过期，先到者为准

# 登录凭证三件套：SESSDATA 是凭证主体，bili_jct 用于需要 csrf 的写接口，
# DedeUserID 供 nav 等账号接口识别身份
COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """302 不自动跟随：换票要逐跳读取 Set-Cookie，跳转由调用方控制。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _LoginHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        host, ips = check_url(req.full_url, LOGIN_SUFFIXES)
        return self.do_open(functools.partial(_PinnedConnection, ips=ips), req,
                            context=self._context)


_opener = urllib.request.build_opener(
    _NoRedirect(),
    urllib.request.HTTPDefaultErrorHandler(),
    urllib.request.ProxyHandler({}),   # 与 bilibili.py 一致：不读系统代理
    _LoginHandler(),
)


def _get(url: str, cookie: str = "", timeout: int = 15) -> tuple[int, dict, bytes]:
    """GET，返回 (状态码, 响应头, 响应体)；3xx 不跟随，按状态码原样返回。"""
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:   # 含 3xx（见 _NoRedirect）与 4xx/5xx
        return e.code, e.headers, e.read()


def _get_json(url: str, timeout: int = 15) -> dict:
    status, _, body = _get(url, timeout=timeout)
    if status != 200:
        raise ApiError(status, f"HTTP {status}")
    try:
        obj = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as e:
        raise ApiError(-1, "接口返回非 JSON") from e
    if not isinstance(obj, dict):
        raise ApiError(-1, "接口返回格式异常")
    return obj


def _cookies_from_query(url: str) -> dict[str, str]:
    """取跳转地址查询串里的凭证（parse_qs 已做过 unquote，SESSDATA 里的 %2C 会还原）。"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    out = {}
    for key in COOKIE_KEYS:
        value = (q.get(key) or [""])[0]
        if value:
            out[key] = value
    return out


def _cookies_from_headers(headers) -> dict[str, str]:
    out = {}
    for raw in (headers.get_all("Set-Cookie") or []) if headers else []:
        head = raw.split(";", 1)[0]
        name, sep, value = head.partition("=")
        name, value = name.strip(), value.strip()
        if sep and name in COOKIE_KEYS and value:
            out[name] = value
    return out


def resolve_cookies(login_url: str) -> dict[str, str]:
    """把扫码成功返回的跳转地址换成登录 Cookie。

    凭证一般已在查询串里；取不全时逐跳跟随 302 收集 Set-Cookie。
    SESSDATA 与 bili_jct 是必备项，DedeUserID 缺失仍可正常调用回放接口。
    """
    cookies = _cookies_from_query(login_url)
    url = login_url
    for _ in range(MAX_REDIRECTS + 1):
        if cookies.get("SESSDATA") and cookies.get("bili_jct"):
            return cookies
        cookie = "; ".join(f"{k}={v}" for k, v in cookies.items())
        status, headers, _ = _get(url, cookie=cookie)
        cookies.update(_cookies_from_headers(headers))
        location = headers.get("Location") if headers else None
        if not location:
            break
        url = urllib.parse.urljoin(url, location)
    if cookies.get("SESSDATA") and cookies.get("bili_jct"):
        return cookies
    return {}


def qr_png_data_url(text: str, box: int = 8) -> str:
    """把二维码文本渲染成 data URL，前端直接塞给 <img src>。"""
    import qrcode   # 延迟导入：缺依赖时只影响扫码入口，不影响其余功能启动

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=box, border=2)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class QrLoginManager:
    """扫码登录状态机：idle / loading / waiting / scanned / success / expired / error。

    轮询跑在后台线程，前端按固定间隔取 status()。刷新二维码不中断旧线程，
    而是把代际号 +1，旧线程下一轮自行退出——省掉 join 与线程收尾的同步成本。
    """

    def __init__(self, store, client):
        self._store = store
        self._client = client
        self._lock = threading.RLock()
        self._gen = 0
        self._state = "idle"
        self._message = ""
        self._image = ""
        self._key = ""

    # ---------- 对外 ----------
    def start(self) -> dict:
        """生成新二维码并开始轮询；重复调用即刷新（旧二维码立即作废）。"""
        with self._lock:
            self._gen += 1
            gen = self._gen
            self._state, self._message, self._image = "loading", "正在生成二维码…", ""
        try:
            data = _get_json(f"{PASSPORT}/generate")
            if data.get("code") != 0:
                raise ApiError(data.get("code"), data.get("message") or "生成二维码失败")
            info = data.get("data") or {}
            url, key = info.get("url") or "", info.get("qrcode_key") or ""
            if not url or not key:
                raise ApiError(-1, "二维码数据不完整")
            image = qr_png_data_url(url)
        except Exception as e:   # 网络异常、白名单拦截、缺 qrcode 依赖
            self._set(gen, "error", f"生成二维码失败：{e}")
            self._store.log(f"生成登录二维码失败：{e}", "error")
            return self.status()
        if self._set(gen, "waiting", "请使用哔哩哔哩 App 扫码", image=image):
            self._key = key
            threading.Thread(target=self._poll, args=(gen, key), daemon=True).start()
            self._store.log("已生成本次登录二维码，等待扫码")
        return self.status()

    def cancel(self) -> None:
        """作废当前二维码并回到未开始状态（退出登录时调用）。"""
        with self._lock:
            self._gen += 1
            self._state, self._message, self._image, self._key = "idle", "", "", ""

    def status(self) -> dict:
        with self._lock:
            s = self._store.settings
            return {
                "state": self._state,
                "message": self._message,
                "image": self._image,
                "logged_in": bool(s.get("sessdata")),
                "account": {
                    "name": s.get("user_name") or "",
                    "face": s.get("user_face") or "",
                    "mid": s.get("dede_user_id") or "",
                },
            }

    # ---------- 内部 ----------
    def _set(self, gen: int, state: str, message: str, image: str | None = None) -> bool:
        """仅在代际号未变时更新状态，返回是否写入成功。"""
        with self._lock:
            if gen != self._gen:
                return False
            self._state, self._message = state, message
            if image is not None:
                self._image = image
            return True

    def _expire(self, gen: int, message: str) -> None:
        if self._set(gen, "expired", message):
            self._store.log(f"扫码登录：{message}", "info")

    def _poll(self, gen: int, key: str) -> None:
        fails = 0
        for _ in range(MAX_POLLS):
            time.sleep(POLL_INTERVAL)
            with self._lock:
                if gen != self._gen:
                    return   # 已刷新或已取消
            try:
                data = _get_json(f"{PASSPORT}/poll?qrcode_key={urllib.parse.quote(key)}")
                fails = 0
            except Exception as e:
                fails += 1
                if fails == 3:   # 偶发抖动不打扰用户，连续失败才记日志
                    self._store.log(f"扫码状态查询失败（将重试）：{e}", "error")
                continue

            outer = data.get("code")
            if outer not in (0, None):
                # 顶层 code 也可能直接是过期码
                if outer in (86038, -400):
                    self._expire(gen, "二维码已过期，请点击刷新")
                else:
                    self._expire(gen, f"扫码登录失败：{data.get('message') or outer}")
                return

            inner = data.get("data") or {}
            code = inner.get("code")
            if code == 0:
                self._finish(gen, inner.get("url") or "")
                return
            if code == 86090:
                self._set(gen, "scanned", "已扫码，请在手机上确认登录")
            elif code == 86038:
                self._expire(gen, "二维码已过期，请点击刷新")
                return
            # 86101 未扫码：继续等待
        self._expire(gen, "二维码已过期，请点击刷新")

    def _finish(self, gen: int, login_url: str) -> None:
        try:
            cookies = resolve_cookies(login_url) if login_url else {}
        except Exception as e:
            cookies = {}
            self._store.log(f"换取登录凭证失败：{e}", "error")
        if not cookies:
            self._set(gen, "error", "扫码成功，但未能换取登录凭证，请刷新二维码重试")
            return
        name = self._save(cookies)
        if self._set(gen, "success", f"登录成功{('：' + name) if name else ''}"):
            self._store.log(f"扫码登录成功：{name or cookies.get('DedeUserID') or ''}".rstrip("："))

    def _save(self, cookies: dict[str, str]) -> str:
        """凭证落库，并用 nav 接口补齐昵称与头像（失败不影响登录本身）。"""
        self._store.save_settings({
            "sessdata": cookies.get("SESSDATA", ""),
            "bili_jct": cookies.get("bili_jct", ""),
            "dede_user_id": cookies.get("DedeUserID", ""),
            "user_name": "",
            "user_face": "",
        })
        try:
            account = self._client.get_account()
        except Exception:
            account = None
        if not account:
            return ""
        self._store.save_settings({
            "user_name": account.get("name") or "",
            "user_face": account.get("face") or "",
            "dede_user_id": str(account.get("mid") or cookies.get("DedeUserID") or ""),
        })
        return account.get("name") or ""
