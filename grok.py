import base64
import json
import os
import random
import string
import time
import re
import struct
import threading
import concurrent.futures
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup

from g import EmailService, TurnstileService, UserAgreementService, NsfwSettingsService


# 基础配置
site_url = "https://accounts.x.ai"
_BASE_DIR = Path(__file__).resolve().parent
_ACTION_CACHE_FILE = _BASE_DIR / "logs" / "action_id_cache.json"
# chrome120 在部分 Windows/curl_cffi 组合下连 accounts.x.ai 会 curl(28) 超时，改用更稳指纹
DEFAULT_IMPERSONATE = "chrome131"
# device flow / 导入专用指纹池（TLS 失败时轮换）
DEVICE_FLOW_IMPERSONATES = ("chrome131", "chrome136", "chrome124", "chrome")
# 与 grokcli-2api sso_to_auth_json 保持一致：只有 device flow 换到 token 才算可导入
OIDC_ISSUER = "https://auth.x.ai"
GROK_CLI_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_SCOPES = (
    "openid profile email offline_access grok-cli:access "
    "api:access conversations:read conversations:write"
)
CHROME_PROFILES = [
    {"impersonate": "chrome124", "version": "124.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome131", "version": "131.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome136", "version": "136.0.0.0", "brand": "chrome"},
    {"impersonate": "chrome120", "version": "120.0.0.0", "brand": "chrome"},
    {"impersonate": "edge101", "version": "101.0.1210.47", "brand": "edge"},
]
# device flow 全局串行：避免本机并发 curl_cffi 与 Docker 内 TLS 踩踏
_DEVICE_FLOW_LOCK = threading.Lock()
# 全局冷却：遇到 rate_limited 后，后续 device flow 至少隔这么久
_device_flow_cooldown_until = 0.0
_device_flow_cooldown_lock = threading.Lock()


def _enrich_worker_count() -> int:
    """SSO 后后台 enrich（device flow / 协议 / NSFW）线程数。"""
    raw = (os.environ.get("ENRICH_WORKERS") or "12").strip()
    try:
        n = int(raw)
    except ValueError:
        n = 12
    return max(1, min(n, 32))


def get_random_chrome_profile():
    profile = random.choice(CHROME_PROFILES)
    if profile.get("brand") == "edge":
        chrome_major = profile["version"].split(".")[0]
        chrome_version = f"{chrome_major}.0.0.0"
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_version} Safari/537.36 Edg/{profile['version']}"
        )
    else:
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{profile['version']} Safari/537.36"
        )
    return profile["impersonate"], ua


PROXIES = {
    # "http": "http://127.0.0.1:10808",
    # "https": "http://127.0.0.1:10808"
}


def generate_random_name() -> str:
    length = random.randint(4, 6)
    return random.choice(string.ascii_uppercase) + "".join(
        random.choice(string.ascii_lowercase) for _ in range(length - 1)
    )


def generate_random_string(length: int = 15) -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(length))


def _b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def decode_jwt_payload(token: str) -> dict:
    """解析 JWT payload（不验签），失败返回空 dict。"""
    try:
        parts = (token or "").strip().split(".")
        if len(parts) != 3:
            return {}
        return json.loads(_b64url_decode(parts[1]))
    except Exception:
        return {}


def is_auth_token_usable(token: Any, *, skew_seconds: float = 60.0) -> bool:
    """
    判断注册时缓存的 device flow token 是否还能直接写上游。
    有 access_token，且 JWT exp 未过期（留 skew 余量）才算可用。
    """
    if not isinstance(token, dict):
        return False
    access = (token.get("access_token") or token.get("key") or "").strip()
    if not access:
        return False
    payload = decode_jwt_payload(access)
    exp = payload.get("exp")
    if exp is not None:
        try:
            if float(exp) <= time.time() + float(skew_seconds):
                return False
        except (TypeError, ValueError):
            return False
    return True


def is_sso_jwt_shape(sso: str) -> bool:
    """粗检：xAI sso cookie 一般为 eyJ 开头的 JWT。"""
    s = (sso or "").strip()
    if not s or not s.startswith("eyJ"):
        return False
    parts = s.split(".")
    if len(parts) != 3:
        return False
    payload = decode_jwt_payload(s)
    return bool(payload)


def _device_flow_error_kind(err: str) -> str:
    """分类 device flow 错误，便于退避策略。"""
    e = (err or "").lower()
    if "rate_limited" in e or "rate limit" in e or "too many" in e:
        return "rate_limited"
    if "curl: (35)" in e or "tls" in e or "ssl" in e or "openssl" in e:
        return "tls"
    if (
        "timed out" in e
        or "timeout" in e
        or "curl: (28)" in e
        or "connection timed out" in e
    ):
        return "timeout"
    if "device/code" in e or "device code" in e:
        return "device_code"
    if "authorization_pending" in e or "未拿到 access_token" in e:
        return "token_poll"
    if "会话无效" in e or "非有效 jwt" in e:
        return "invalid"
    return "other"


def _device_flow_wait_seconds(err: str, attempt: int = 1) -> float:
    """按错误类型返回建议等待秒数。"""
    kind = _device_flow_error_kind(err)
    a = max(1, int(attempt))
    if kind == "rate_limited":
        return min(90.0, 18.0 * a + random.uniform(2, 6))
    if kind == "timeout":
        return min(45.0, 6.0 * a + random.uniform(1, 3))
    if kind == "tls":
        return min(30.0, 4.0 * a + random.uniform(0.5, 2))
    if kind == "device_code":
        return min(40.0, 8.0 * a)
    if kind == "token_poll":
        return min(20.0, 3.0 * a)
    return min(20.0, 3.0 * a)


def _mark_device_flow_cooldown(seconds: float) -> None:
    global _device_flow_cooldown_until
    until = time.time() + max(0.0, float(seconds))
    with _device_flow_cooldown_lock:
        if until > _device_flow_cooldown_until:
            _device_flow_cooldown_until = until


def _wait_device_flow_cooldown() -> None:
    """若处于 rate_limited 冷却期则阻塞等待。"""
    with _device_flow_cooldown_lock:
        until = float(_device_flow_cooldown_until or 0.0)
    remain = until - time.time()
    if remain > 0:
        time.sleep(min(remain, 120.0))


def _request_device_code(timeout: int = 20) -> dict | None:
    data = urllib.parse.urlencode(
        {"client_id": GROK_CLI_CLIENT_ID, "scope": OIDC_SCOPES}
    ).encode()
    req = urllib.request.Request(
        f"{OIDC_ISSUER}/oauth2/device/code",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 429 / 限流时带上可读信息，便于上层退避
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")[:200]
        except Exception:
            pass
        if e.code == 429 or "rate" in body.lower():
            return {"_error": f"device/code rate_limited HTTP {e.code}: {body}"}
        return {"_error": f"device/code HTTP {e.code}: {body}"}
    except Exception as e:
        return {"_error": f"device/code 异常: {e}"}


def _poll_device_token(
    device_code: str,
    interval: int,
    expires_in: int,
    timeout: int = 60,
) -> dict | None:
    deadline = time.time() + min(int(expires_in or 1800), timeout)
    wait = max(2, int(interval or 5))
    net_fail = 0
    while time.time() < deadline:
        time.sleep(wait)
        data = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": GROK_CLI_CLIENT_ID,
                "device_code": device_code,
            }
        ).encode()
        req = urllib.request.Request(
            f"{OIDC_ISSUER}/oauth2/token",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except Exception:
                net_fail += 1
                if net_fail >= 4:
                    return None
                wait = min(wait + 2, 12)
                continue
            error = err.get("error", "")
            if error == "authorization_pending":
                net_fail = 0
                continue
            if error == "slow_down":
                wait += 3
                continue
            return None
        except Exception:
            # 瞬时网络：多试几次，别一次超时就放弃已 approve 的 flow
            net_fail += 1
            if net_fail >= 5:
                return None
            wait = min(wait + 1, 10)
            continue
    return None


def _rfc3339_ns(ts: float) -> str:
    """与 grokcli-2api 一致的 expires_at 格式。"""
    from datetime import datetime, timezone

    dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(dt.microsecond * 1000):09d}Z"


def token_to_auth_entry(token: dict, email: str = "") -> dict[str, Any]:
    """
    将 device flow 得到的 token 转成上游 /accounts/import 可接受的 entry。
    与 grokcli-2api sso_to_auth_json.token_to_auth_entry 字段对齐。
    """
    access = (token or {}).get("access_token") or (token or {}).get("key") or ""
    refresh = (token or {}).get("refresh_token") or ""
    payload = decode_jwt_payload(access)

    user_id = payload.get("sub") or payload.get("principal_id") or ""
    principal_id = payload.get("principal_id") or user_id
    principal_type = payload.get("principal_type") or "User"

    expires_in = int((token or {}).get("expires_in") or 21600)
    if "exp" in payload:
        expires_at = _rfc3339_ns(float(payload["exp"]))
    else:
        expires_at = _rfc3339_ns(time.time() + expires_in)

    iat = payload.get("iat")
    create_time = _rfc3339_ns(float(iat) if iat else time.time())

    return {
        "key": access,
        "auth_mode": "oidc",
        "create_time": create_time,
        "user_id": user_id,
        "email": email or payload.get("email") or "",
        "principal_type": principal_type,
        "principal_id": principal_id,
        "refresh_token": refresh,
        "expires_at": expires_at,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": GROK_CLI_CLIENT_ID,
    }


def sso_device_flow_to_token(
    sso: str,
    *,
    impersonate: str | None = None,
    timeout: int = 28,
) -> dict[str, Any]:
    """
    与 grokcli-2api sso_to_auth_json.sso_to_token 同路径：
    SSO cookie → 登录态探测 → OIDC device verify/approve → access_token。
    只有拿到 access_token 才视为「可导入」。

    加固：
    - 全局冷却（rate_limited 后拉长间隔）
    - TLS/超时失败时轮换 impersonate（chrome120 在部分环境会卡死）
    - device/code 失败返回可读错误
    """
    s = (sso or "").strip()
    if not s:
        return {"ok": False, "error": "空 sso", "token": None, "payload": {}}
    if not is_sso_jwt_shape(s):
        return {"ok": False, "error": "非有效 JWT 形态", "token": None, "payload": {}}

    payload = decode_jwt_payload(s)
    proxy_kw = {"proxies": PROXIES} if PROXIES else {}

    # 指纹列表：调用方指定的优先，再轮换稳妥指纹
    fps: list[str] = []
    if impersonate:
        fps.append(impersonate)
    for x in DEVICE_FLOW_IMPERSONATES:
        if x not in fps:
            fps.append(x)
    # 最多试 3 个指纹，避免一次导入拖太久
    fps = fps[:3]

    with _DEVICE_FLOW_LOCK:
        _wait_device_flow_cooldown()
        last_err = "device flow 失败"

        for fp_idx, fp in enumerate(fps):
            try:
                sess = requests.Session()
                sess.cookies.set("sso", s, domain=".x.ai")
                # 同时设 accounts 域，避免部分路径丢 cookie
                try:
                    sess.cookies.set("sso", s, domain="accounts.x.ai")
                except Exception:
                    pass

                r = sess.get(
                    "https://accounts.x.ai/",
                    impersonate=fp,
                    timeout=timeout,
                    allow_redirects=True,
                    **proxy_kw,
                )
                final_url = (r.url or "").lower()
                if "error=rate_limited" in final_url or "rate_limited" in final_url:
                    last_err = f"探测 rate_limited: {r.url}"
                    _mark_device_flow_cooldown(25 + 10 * fp_idx)
                    continue
                if "sign-in" in final_url or "sign-up" in final_url:
                    return {
                        "ok": False,
                        "error": f"会话无效（跳转 {r.url}）",
                        "token": None,
                        "payload": payload,
                    }
                if r.status_code >= 400:
                    last_err = f"探测 HTTP {r.status_code}"
                    continue
            except Exception as e:
                last_err = f"探测异常: {e}"
                kind = _device_flow_error_kind(last_err)
                if kind in ("timeout", "tls"):
                    # 换指纹再试
                    time.sleep(1.0 + 0.5 * fp_idx)
                    continue
                return {
                    "ok": False,
                    "error": last_err,
                    "token": None,
                    "payload": payload,
                }

            dc = _request_device_code(timeout=max(15, int(timeout)))
            if not dc or dc.get("_error"):
                last_err = (dc or {}).get("_error") or "device/code 申请失败"
                if _device_flow_error_kind(last_err) == "rate_limited":
                    _mark_device_flow_cooldown(30)
                # device/code 与指纹无关，不必换指纹空转
                return {
                    "ok": False,
                    "error": last_err,
                    "token": None,
                    "payload": payload,
                }
            if not dc.get("device_code") or not dc.get("user_code"):
                last_err = "device/code 申请失败（无 device_code）"
                return {
                    "ok": False,
                    "error": last_err,
                    "token": None,
                    "payload": payload,
                }

            try:
                verify_uri = dc.get("verification_uri_complete") or ""
                if verify_uri:
                    sess.get(
                        verify_uri,
                        impersonate=fp,
                        timeout=timeout,
                        allow_redirects=True,
                        **proxy_kw,
                    )
                r = sess.post(
                    f"{OIDC_ISSUER}/oauth2/device/verify",
                    data={"user_code": dc["user_code"]},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate=fp,
                    timeout=timeout,
                    allow_redirects=True,
                    **proxy_kw,
                )
                ru = r.url or ""
                if "rate_limited" in ru.lower() or "error=rate_limited" in ru.lower():
                    last_err = f"device verify 失败: {ru}"
                    _mark_device_flow_cooldown(30 + 10 * fp_idx)
                    time.sleep(2)
                    continue
                if "consent" not in ru:
                    last_err = f"device verify 失败: {ru}"
                    # 非限流的 verify 失败（如 code 过期）换指纹意义不大，直接返回
                    if "error=" in ru.lower() and "rate_limited" not in ru.lower():
                        return {
                            "ok": False,
                            "error": last_err,
                            "token": None,
                            "payload": payload,
                        }
                    continue
            except Exception as e:
                last_err = f"device verify 异常: {e}"
                kind = _device_flow_error_kind(last_err)
                if kind in ("timeout", "tls"):
                    time.sleep(1.0 + 0.5 * fp_idx)
                    continue
                return {
                    "ok": False,
                    "error": last_err,
                    "token": None,
                    "payload": payload,
                }

            try:
                r = sess.post(
                    f"{OIDC_ISSUER}/oauth2/device/approve",
                    data={
                        "user_code": dc["user_code"],
                        "action": "allow",
                        "principal_type": "User",
                        "principal_id": "",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    impersonate=fp,
                    timeout=timeout,
                    allow_redirects=True,
                    **proxy_kw,
                )
                ru = r.url or ""
                if "rate_limited" in ru.lower():
                    last_err = f"device approve 失败: {ru}"
                    _mark_device_flow_cooldown(30)
                    continue
                if "done" not in ru:
                    last_err = f"device approve 失败: {ru}"
                    return {
                        "ok": False,
                        "error": last_err,
                        "token": None,
                        "payload": payload,
                    }
            except Exception as e:
                last_err = f"device approve 异常: {e}"
                kind = _device_flow_error_kind(last_err)
                if kind in ("timeout", "tls"):
                    time.sleep(1.0 + 0.5 * fp_idx)
                    continue
                return {
                    "ok": False,
                    "error": last_err,
                    "token": None,
                    "payload": payload,
                }

            token = _poll_device_token(
                dc["device_code"],
                int(dc.get("interval") or 5),
                int(dc.get("expires_in") or 1800),
                timeout=60,
            )
            if not token or not (token.get("access_token") or token.get("key")):
                last_err = "device flow 未拿到 access_token"
                # approve 已成功但 token 轮询失败：再等一轮短重试（同一 device_code 可能已失效，重新整条）
                time.sleep(2)
                continue

            # 成功后轻微冷却，降低连打触发 rate_limited
            _mark_device_flow_cooldown(2.0)
            return {
                "ok": True,
                "error": None,
                "token": token,
                "payload": payload,
                "has_refresh": bool(token.get("refresh_token")),
                "impersonate": fp,
            }

        if _device_flow_error_kind(last_err) == "rate_limited":
            _mark_device_flow_cooldown(20)
        return {
            "ok": False,
            "error": last_err,
            "token": None,
            "payload": payload,
        }


def validate_sso_cookie(
    sso: str,
    *,
    impersonate: str = DEFAULT_IMPERSONATE,
    user_agent: Optional[str] = None,
    timeout: int = 15,
    require_device_flow: bool = True,
    retries: int = 2,
) -> dict[str, Any]:
    """
    校验换到的 sso 是否真正可导入上游。

    默认 require_device_flow=True：走完整 OIDC device flow（与 import-sso 同路径）。
    仅「能登录 accounts」不够——你之前 0/10 导入失败，就是 Docker 侧 TLS/并发问题 +
    注册机只做了浅校验。

    返回: {ok, error, payload, token?}
    """
    del user_agent  # 保留签名兼容
    s = (sso or "").strip()
    if not s:
        return {"ok": False, "error": "空 sso", "payload": {}}
    if not is_sso_jwt_shape(s):
        return {"ok": False, "error": "非有效 JWT 形态", "payload": {}}

    if not require_device_flow:
        # 浅校验（不推荐用于记成功）
        payload = decode_jwt_payload(s)
        try:
            sess = requests.Session()
            sess.cookies.set("sso", s, domain=".x.ai")
            r = sess.get(
                "https://accounts.x.ai/",
                impersonate=impersonate or DEFAULT_IMPERSONATE,
                timeout=timeout,
                allow_redirects=True,
                proxies=PROXIES or None,
            )
            final_url = (r.url or "").lower()
            if "sign-in" in final_url or "sign-up" in final_url:
                return {"ok": False, "error": f"会话无效（跳转 {r.url}）", "payload": payload}
            if r.status_code >= 400:
                return {"ok": False, "error": f"探测 HTTP {r.status_code}", "payload": payload}
            return {"ok": True, "error": None, "payload": payload}
        except Exception as e:
            return {"ok": False, "error": f"探测异常: {e}", "payload": payload}

    last_err = "device flow 失败"
    attempts = max(1, int(retries or 1))
    for i in range(1, attempts + 1):
        result = sso_device_flow_to_token(
            s,
            impersonate=None,  # 内部轮换稳妥指纹
            timeout=max(int(timeout or 20), 28),
        )
        if result.get("ok"):
            return result
        last_err = result.get("error") or last_err
        # 会话明确无效则不必重试
        if "会话无效" in str(last_err) or "非有效 JWT" in str(last_err):
            break
        if i < attempts:
            wait = _device_flow_wait_seconds(str(last_err), i)
            if _device_flow_error_kind(str(last_err)) == "rate_limited":
                _mark_device_flow_cooldown(wait)
            time.sleep(wait)
    return {
        "ok": False,
        "error": last_err,
        "payload": decode_jwt_payload(s),
        "token": None,
    }


def encode_grpc_message(field_id, string_value):
    key = (field_id << 3) | 2
    value_bytes = string_value.encode("utf-8")
    length = len(value_bytes)
    payload = struct.pack("B", key) + struct.pack("B", length) + value_bytes
    return b"\x00" + struct.pack(">I", len(payload)) + payload


def encode_grpc_message_verify(email, code):
    p1 = struct.pack("B", (1 << 3) | 2) + struct.pack("B", len(email)) + email.encode("utf-8")
    p2 = struct.pack("B", (2 << 3) | 2) + struct.pack("B", len(code)) + code.encode("utf-8")
    payload = p1 + p2
    return b"\x00" + struct.pack(">I", len(payload)) + payload


class LogBuffer:
    """线程安全日志缓冲，供控制台与 Web UI 共用。"""

    def __init__(self, maxlen: int = 2000):
        self._lock = threading.Lock()
        self._seq = 0
        self._entries = deque(maxlen=maxlen)

    def emit(self, message: str, level: str = "info"):
        ts = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._seq += 1
            entry = {
                "id": self._seq,
                "time": ts,
                "level": level,
                "message": message,
            }
            self._entries.append(entry)
        print(f"[{ts}] {message}")
        return entry

    def latest_id(self) -> int:
        with self._lock:
            return int(self._seq)

    def since(self, after_id: int = 0, limit: int = 200):
        with self._lock:
            # 服务重启后 seq 从 0 起，浏览器仍可能带着旧 after_id → 永远空结果（日志“卡住”）
            if after_id > self._seq:
                items = list(self._entries)[-limit:]
            else:
                items = [e for e in self._entries if e["id"] > after_id]
                items = items[-limit:]
            return items

    def clear(self):
        with self._lock:
            self._entries.clear()


class RegisterEngine:
    """可被 CLI 与 Web UI 共用的注册引擎。"""

    def __init__(self, log_fn: Optional[Callable[[str, str], None]] = None):
        self.site_url = site_url
        self.config = {
            "site_key": "0x4AAAAAAAhr9JGVDZbrZOo0",
            "action_id": None,
            "state_tree": (
                "%5B%22%22%2C%7B%22children%22%3A%5B%22(app)%22%2C%7B%22children%22%3A%5B%22"
                "(auth)%22%2C%7B%22children%22%3A%5B%22sign-up%22%2C%7B%22children%22%3A%5B%22"
                "__PAGE__%22%2C%7B%7D%2C%22%2Fsign-up%22%2C%22refresh%22%5D%7D%5D%7D%2Cnull%2C"
                "null%5D%7D%2Cnull%2Cnull%5D%7D%2Cnull%2Cnull%2Ctrue%5D"
            ),
        }
        self.post_lock = threading.Lock()
        self.file_lock = threading.Lock()
        self.stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
        self._worker_thread: Optional[threading.Thread] = None
        # 注册 POST 起步节流（不包整次 HTTP，允许多线程并行飞）
        self._post_min_interval = 0.25
        self._last_post_start = 0.0
        # SSO 后的 device flow / 协议 / NSFW：后台跑，不堵下一号
        self._enrich_workers = _enrich_worker_count()
        self._enrich_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._enrich_workers, thread_name_prefix="RegEnrich"
        )
        self._enrich_pending = 0
        self._nsfw_ok_count = 0
        self._nsfw_fail_count = 0
        self._enrich_lock = threading.Lock()

        self.success_count = 0
        self.fail_count = 0
        self.target_count = 0
        self.workers = 0
        self.start_time: Optional[float] = None
        self.output_file: Optional[str] = None
        self.status = "idle"  # idle | initializing | running | stopping | done | error
        self.error_message = ""
        # 足够覆盖大批量注册；过小会导致「成功 100、可导入只剩 50」
        self.recent_success: deque = deque(maxlen=5000)
        self._log_fn = log_fn or (lambda msg, level="info": print(msg))
        # Action ID 缓存：内存 + 磁盘，避免每次/重启都扫 JS（通常 5~20 秒）
        self._action_cache: dict = {
            "action_id": None,
            "site_key": None,
            "state_tree": None,
            "ts": 0.0,
        }
        self._action_cache_ttl = 6 * 3600  # 6 小时
        self._load_action_cache_disk()

    def log(self, message: str, level: str = "info"):
        self._log_fn(message, level)

    def get_status(self) -> dict:
        elapsed = 0.0
        avg = 0.0
        if self.start_time:
            elapsed = max(0.0, time.time() - self.start_time)
            if self.success_count > 0:
                avg = elapsed / self.success_count
        progress = 0.0
        if self.target_count > 0:
            progress = min(100.0, self.success_count / self.target_count * 100)
        return {
            "status": self.status,
            "success_count": self.success_count,
            "fail_count": self.fail_count,
            "target_count": self.target_count,
            "workers": self.workers,
            "elapsed": round(elapsed, 1),
            "avg_seconds": round(avg, 1),
            "progress": round(progress, 1),
            "output_file": self.output_file or "",
            "action_id": self.config.get("action_id") or "",
            "site_key": self.config.get("site_key") or "",
            "error_message": self.error_message,
            "enrich_pending": int(getattr(self, "_enrich_pending", 0) or 0),
            "enrich_workers": int(getattr(self, "_enrich_workers", 0) or 0),
            # NSFW 后台进度：完成=成功+失败，剩余=还在 enrich 队列/执行中
            "nsfw_ok_count": int(getattr(self, "_nsfw_ok_count", 0) or 0),
            "nsfw_fail_count": int(getattr(self, "_nsfw_fail_count", 0) or 0),
            "nsfw_done_count": int(
                (getattr(self, "_nsfw_ok_count", 0) or 0)
                + (getattr(self, "_nsfw_fail_count", 0) or 0)
            ),
            # 不把完整 sso / token 暴露给前端轮询；导入走服务端 id 匹配
            "recent_success": [
                {
                    "id": it.get("id"),
                    "email": it.get("email"),
                    "sso_preview": it.get("sso_preview"),
                    "sso": bool(it.get("sso")),  # 仅布尔，表示可导入
                    "has_token": is_auth_token_usable(it.get("auth_token")),
                    "nsfw": it.get("nsfw"),
                    "time": it.get("time"),
                    "imported": bool(it.get("imported")),
                }
                for it in self.recent_success
            ],
            "running": self.status in ("initializing", "running", "stopping"),
        }

    def is_running(self) -> bool:
        return self.status in ("initializing", "running", "stopping")

    def _load_action_cache_disk(self) -> None:
        """进程启动时从磁盘恢复 Action ID，避免重启后又扫 10+ 秒。"""
        try:
            if not _ACTION_CACHE_FILE.is_file():
                return
            data = json.loads(_ACTION_CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not data.get("action_id"):
                return
            ts = float(data.get("ts") or 0)
            if time.time() - ts > self._action_cache_ttl:
                return
            self._action_cache = {
                "action_id": data.get("action_id"),
                "site_key": data.get("site_key"),
                "state_tree": data.get("state_tree"),
                "ts": ts,
            }
        except Exception:
            pass

    def _apply_action_cache(self) -> bool:
        cache = self._action_cache
        if not cache.get("action_id"):
            # 内存空时再试一次磁盘（热更新/其他进程写过）
            self._load_action_cache_disk()
            cache = self._action_cache
        if not cache.get("action_id"):
            return False
        if time.time() - float(cache.get("ts") or 0) > self._action_cache_ttl:
            return False
        self.config["action_id"] = cache["action_id"]
        if cache.get("site_key"):
            self.config["site_key"] = cache["site_key"]
        if cache.get("state_tree"):
            self.config["state_tree"] = cache["state_tree"]
        return True

    def _save_action_cache(self) -> None:
        self._action_cache = {
            "action_id": self.config.get("action_id"),
            "site_key": self.config.get("site_key"),
            "state_tree": self.config.get("state_tree"),
            "ts": time.time(),
        }
        try:
            _ACTION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ACTION_CACHE_FILE.write_text(
                json.dumps(self._action_cache, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    def initialize(self, force_rescan: bool = False) -> bool:
        self.status = "initializing"
        # 优先用缓存，启动几乎立刻进入注册
        if not force_rescan and self._apply_action_cache():
            aid = self.config["action_id"] or ""
            self.log(
                f"使用缓存 Action ID: {aid[:18]}…（跳过页面扫描，秒开）",
                "success",
            )
            return True

        self.log(
            "正在初始化：扫描 accounts.x.ai 页面提取 Action ID（仅首次/缓存过期，约 5~20 秒）…",
            "info",
        )
        t0 = time.time()
        start_url = f"{self.site_url}/sign-up"
        with requests.Session(impersonate=DEFAULT_IMPERSONATE) as s:
            try:
                html = s.get(start_url, timeout=20).text
                key_match = re.search(r'sitekey":"(0x4[a-zA-Z0-9_-]+)"', html)
                if key_match:
                    self.config["site_key"] = key_match.group(1)
                tree_match = re.search(r'next-router-state-tree":"([^"]+)"', html)
                if tree_match:
                    self.config["state_tree"] = tree_match.group(1)
                soup = BeautifulSoup(html, "html.parser")
                js_urls = [
                    urljoin(start_url, script["src"])
                    for script in soup.find_all("script", src=True)
                    if "_next/static" in script["src"]
                ]
                # 优先扫体积较小的 chunk，命中后立刻停
                found = False
                for js_url in js_urls:
                    try:
                        js_content = s.get(js_url, timeout=15).text
                    except Exception:
                        continue
                    match = re.search(r"7f[a-fA-F0-9]{40}", js_content)
                    if match:
                        self.config["action_id"] = match.group(0)
                        cost = time.time() - t0
                        self.log(
                            f"Action ID: {self.config['action_id']}（扫描耗时 {cost:.1f}s，已写入缓存）",
                            "success",
                        )
                        found = True
                        break
                if not found:
                    self.config["action_id"] = None
            except Exception as e:
                self.error_message = f"初始化扫描失败: {e}"
                self.log(self.error_message, "error")
                self.status = "error"
                return False

        if not self.config["action_id"]:
            self.error_message = "未找到 Action ID"
            self.log(self.error_message, "error")
            self.status = "error"
            return False
        self._save_action_cache()
        return True

    def send_email_code_grpc(self, session, email):
        url = f"{self.site_url}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
        data = encode_grpc_message(1, email)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": self.site_url,
            "referer": f"{self.site_url}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as e:
            self.log(f"{email} 发送验证码异常: {e}", "error")
            return False

    def verify_email_code_grpc(self, session, email, code):
        url = f"{self.site_url}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
        data = encode_grpc_message_verify(email, code)
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": self.site_url,
            "referer": f"{self.site_url}/sign-up?redirect=grok-com",
        }
        try:
            res = session.post(url, data=data, headers=headers, timeout=15)
            return res.status_code == 200
        except Exception as e:
            self.log(f"{email} 验证验证码异常: {e}", "error")
            return False

    def _sleep(self, seconds: float) -> bool:
        """可中断 sleep。返回 True 表示已被 stop。"""
        if seconds <= 0:
            return self.stop_event.is_set()
        return self.stop_event.wait(timeout=seconds)

    def _fail_account(
        self,
        email_service: Optional[EmailService],
        email: Optional[str],
        reason: str,
        level: str = "error",
    ) -> None:
        """
        单个账号失败：记失败、删邮箱、本账号结束。
        不置 stop_event，其它线程与后续账号继续跑到达目标。
        （同一邮箱不再换号死磕；新一轮会开新邮箱，属于新账号。）
        """
        self.fail_count += 1
        self.error_message = reason
        self.log(f"{reason} — 本账号结束，任务继续", level)
        if email and email_service is not None:
            try:
                email_service.delete_email(email)
            except Exception:
                pass

    def _emergency_save_sso(self, email: str, sso: str, reason: str = "") -> bool:
        """
        紧急落盘：任何已拿到的 SSO 都必须写文件，避免账号创建成功却因后续步骤丢号。
        不计入 success_count 上限拦截——宁可多写一行，也不能吞掉已建账号。
        返回 True 表示至少写入成功一处。
        """
        s = (sso or "").strip()
        if not s:
            return False
        ok = False
        try:
            emergency = _BASE_DIR / "keys" / "emergency_sso.txt"
            emergency.parent.mkdir(parents=True, exist_ok=True)
            with self.file_lock:
                with open(emergency, "a", encoding="utf-8") as f:
                    f.write(f"{email}----{s}\n" if email else f"{s}\n")
                    ok = True
                if self.output_file:
                    with open(self.output_file, "a", encoding="utf-8") as f:
                        f.write(s + "\n")
                        ok = True
            self.log(
                f"{email} SSO 已紧急落盘（{reason or '防丢号'}）→ keys/emergency_sso.txt + 任务文件",
                "success",
            )
        except Exception as e:
            self.log(f"{email} 紧急落盘失败: {e} | SSO 前缀 {s[:20]}…", "error")
        return ok

    def _record_success(
        self,
        email: str,
        sso: str,
        unhinged_ok: bool,
        email_service: Optional[EmailService] = None,
        note: str = "",
        *,
        already_written: bool = False,
        auth_token: Optional[dict] = None,
    ) -> bool:
        """
        写入 SSO 并计入成功。返回 True 表示已计入。
        already_written=True：SSO 已在紧急落盘写过，只更新计数/UI，避免重复行。
        auth_token：注册时 device flow 换到的 token，导入上游可直写，免二次换票。
        """
        with self.file_lock:
            if self.success_count >= self.target_count:
                if not self.stop_event.is_set():
                    self.stop_event.set()
                # 已达目标但仍拿到 SSO：紧急保存，绝不丢号
                if not already_written:
                    try:
                        emergency = _BASE_DIR / "keys" / "emergency_sso.txt"
                        with open(emergency, "a", encoding="utf-8") as f:
                            f.write(f"{email}----{sso}\n")
                        self.log(f"{email} 已达目标，SSO 写入 emergency_sso.txt", "warn")
                    except Exception:
                        pass
                if email_service is not None:
                    try:
                        email_service.delete_email(email)
                    except Exception:
                        pass
                return ""
            if not already_written:
                try:
                    with open(self.output_file, "a", encoding="utf-8") as f:
                        f.write(sso + "\n")
                except Exception as write_err:
                    self.log(f"{email} 写入文件失败: {write_err}，尝试紧急落盘", "error")
                    try:
                        emergency = _BASE_DIR / "keys" / "emergency_sso.txt"
                        with open(emergency, "a", encoding="utf-8") as f:
                            f.write(f"{email}----{sso}\n")
                    except Exception as e2:
                        self.log(f"{email} 紧急落盘也失败: {e2}", "error")
                        self.fail_count += 1
                        return ""

            self.success_count += 1
            avg = (time.time() - self.start_time) / max(1, self.success_count)
            nsfw_tag = "✓" if unhinged_ok else "…"
            # 浅拷贝 token，避免后续引用被外部改写
            cached_token = None
            if isinstance(auth_token, dict) and is_auth_token_usable(auth_token):
                cached_token = dict(auth_token)
            success_id = f"{int(time.time() * 1000)}_{self.success_count}"
            self.recent_success.appendleft(
                {
                    "id": success_id,
                    "email": email,
                    "sso": sso,
                    "sso_preview": (sso[:18] + "...") if len(sso) > 18 else sso,
                    "auth_token": cached_token,
                    "nsfw": unhinged_ok,
                    "time": datetime.now().strftime("%H:%M:%S"),
                    "imported": False,
                }
            )
            extra = f" | {note}" if note else ""
            self.log(
                f"注册成功: {self.success_count}/{self.target_count} | {email} | "
                f"SSO: {sso[:15]}... | 平均: {avg:.1f}s | NSFW: {nsfw_tag}{extra}",
                "success",
            )
            if email_service is not None:
                try:
                    email_service.delete_email(email)
                except Exception:
                    pass
            if self.success_count >= self.target_count and not self.stop_event.is_set():
                self.stop_event.set()
                self.log(
                    f"已达到目标数量: {self.success_count}/{self.target_count}，停止新注册",
                    "success",
                )
            return success_id

    def _update_success_meta(
        self,
        success_id: str,
        *,
        auth_token: Optional[dict] = None,
        unhinged_ok: Optional[bool] = None,
        note: str = "",
    ) -> None:
        """后台 enrich 完成后回填 token / NSFW 状态（按 id 匹配）。"""
        if not success_id:
            return
        with self.file_lock:
            for item in self.recent_success:
                if item.get("id") != success_id:
                    continue
                if isinstance(auth_token, dict) and is_auth_token_usable(auth_token):
                    item["auth_token"] = dict(auth_token)
                if unhinged_ok is not None:
                    item["nsfw"] = bool(unhinged_ok)
                    # 仅首次结算，避免重复回填双计
                    if not item.get("_nsfw_counted"):
                        item["_nsfw_counted"] = True
                        if unhinged_ok:
                            self._nsfw_ok_count += 1
                        else:
                            self._nsfw_fail_count += 1
                if note:
                    item["note"] = note
                break

    def _throttle_signup_post(self) -> None:
        """注册 POST 起步节流：保证间隔，但不把整个 HTTP 锁死。"""
        with self.post_lock:
            now = time.time()
            wait = self._post_min_interval - (now - self._last_post_start)
            if wait > 0:
                time.sleep(wait)
            self._last_post_start = time.time()

    def _enrich_after_sso(
        self,
        *,
        email: str,
        sso: str,
        sso_rw: str,
        success_id: str,
        impersonate: str,
        user_agent: str,
    ) -> None:
        """
        SSO 已落盘并计成功后的锦上添花：
        device flow 缓存 token + 协议/NSFW。
        失败不影响成功计数；导入时仍可再走 device flow。
        """
        try:
            user_agreement_service = UserAgreementService()
            nsfw_service = NsfwSettingsService()
            note_parts: list[str] = []
            auth_token = None

            # 短超时 + 少重试：抢不到就留给导入阶段，别堵 enrich 池
            check = validate_sso_cookie(
                sso,
                impersonate=impersonate,
                user_agent=user_agent,
                require_device_flow=True,
                retries=2,
                timeout=20,
            )
            if check.get("ok") and isinstance(check.get("token"), dict):
                auth_token = check.get("token")
                note_parts.append("device_flow=ok+token_cached")
                self.log(
                    f"{email} [后台] device flow 通过，token 已缓存",
                    "success",
                )
            elif check.get("ok"):
                note_parts.append("device_flow=ok")
                self.log(f"{email} [后台] device flow 通过（无 token 体）", "info")
            else:
                err = check.get("error") or "device flow 失败"
                note_parts.append(f"device_flow_pending: {str(err)[:60]}")
                self.log(
                    f"{email} [后台] device flow 未通过（{err}），导入时再换",
                    "warn",
                )

            unhinged_ok = False
            try:
                tos_result = user_agreement_service.accept_tos_version(
                    sso=sso,
                    sso_rw=sso_rw or "",
                    impersonate=impersonate,
                    user_agent=user_agent,
                )
                tos_hex = tos_result.get("hex_reply") or ""
                if not tos_result.get("ok") or not tos_hex:
                    note_parts.append(
                        f"协议失败: {tos_result.get('error') or tos_result}"
                    )
                else:
                    nsfw_result = nsfw_service.enable_nsfw(
                        sso=sso,
                        sso_rw=sso_rw or "",
                        impersonate=impersonate,
                        user_agent=user_agent,
                    )
                    nsfw_hex = nsfw_result.get("hex_reply") or ""
                    if not nsfw_result.get("ok") or not nsfw_hex:
                        note_parts.append(
                            f"NSFW失败: {nsfw_result.get('error') or nsfw_result}"
                        )
                    else:
                        unhinged_result = nsfw_service.enable_unhinged(sso)
                        unhinged_ok = unhinged_result.get("ok", False)
                        if not unhinged_ok:
                            note_parts.append("unhinged失败")
                        else:
                            note_parts.append("nsfw=ok")
            except Exception as post_err:
                note_parts.append(f"协议/NSFW异常: {post_err}")
                self.log(f"{email} [后台] 协议/NSFW 异常: {post_err}", "warn")

            note = "; ".join(str(x)[:80] for x in note_parts)
            self._update_success_meta(
                success_id,
                auth_token=auth_token if isinstance(auth_token, dict) else None,
                unhinged_ok=unhinged_ok,
                note=note,
            )
            if unhinged_ok:
                self.log(f"{email} [后台] NSFW/unhinged 已开", "success")
        except Exception as e:
            self.log(f"{email} [后台] enrich 异常: {e}", "warn")
        finally:
            with self._enrich_lock:
                self._enrich_pending = max(0, self._enrich_pending - 1)

    def _schedule_enrich(
        self,
        *,
        email: str,
        sso: str,
        sso_rw: str,
        success_id: str,
        impersonate: str,
        user_agent: str,
    ) -> None:
        with self._enrich_lock:
            self._enrich_pending += 1
        try:
            self._enrich_pool.submit(
                self._enrich_after_sso,
                email=email,
                sso=sso,
                sso_rw=sso_rw or "",
                success_id=success_id,
                impersonate=impersonate,
                user_agent=user_agent,
            )
        except Exception as e:
            with self._enrich_lock:
                self._enrich_pending = max(0, self._enrich_pending - 1)
            self.log(f"{email} 调度后台 enrich 失败: {e}", "warn")

    def register_single_thread(self):
        # 多线程时轻微错开，单线程几乎立刻开跑
        if self.workers > 1:
            if self._sleep(random.uniform(0, min(1.5, 0.15 * self.workers))):
                return

        try:
            email_service = EmailService()
            turnstile_service = TurnstileService()
            # 协议/NSFW 已挪到后台 enrich 池，主线程不再初始化
        except Exception as e:
            self.log(f"服务初始化失败: {e}", "error")
            self.stop_event.set()
            return

        final_action_id = self.config["action_id"]
        if not final_action_id:
            self.log("线程退出：缺少 Action ID", "error")
            self.stop_event.set()
            return

        current_email = None

        # 失败只丢弃当前账号；未达目标则继续开新邮箱，直到目标或用户停止
        while not self.stop_event.is_set() and self.success_count < self.target_count:
            try:
                impersonate_fingerprint, account_user_agent = get_random_chrome_profile()
                with requests.Session(impersonate=impersonate_fingerprint, proxies=PROXIES) as session:
                    try:
                        session.get(self.site_url, timeout=8)
                    except Exception:
                        pass

                    if self.stop_event.is_set():
                        return

                    password = generate_random_string()

                    # 硬规则：Solver 不在线绝不创建邮箱，避免解不出验证码白耗配额
                    if not (turnstile_service.yescaptcha_key or "").strip():
                        try:
                            turnstile_service._ensure_local_solver(
                                stop_event=self.stop_event
                            )
                        except RuntimeError as te:
                            if str(te) == "stopped" or self.stop_event.is_set():
                                return
                            self.log(f"Solver 未就绪: {te}，等待后重试（未创建邮箱）", "warn")
                            if self._sleep(3):
                                return
                            continue
                        except Exception as se:
                            if self.stop_event.is_set():
                                return
                            self.log(
                                f"Solver 未就绪: {se}，等待后重试（未创建邮箱，不浪费配额）",
                                "warn",
                            )
                            if self._sleep(5):
                                return
                            continue

                    try:
                        jwt, email = email_service.create_email()
                        current_email = email
                    except Exception as e:
                        self._fail_account(
                            email_service, None, f"邮箱服务异常: {e}"
                        )
                        # 邮箱服务抖动：稍等再试，不整任务退出
                        if self._sleep(2):
                            return
                        continue

                    if not email:
                        self._fail_account(email_service, None, "创建邮箱失败")
                        if self._sleep(1):
                            return
                        continue

                    if self.stop_event.is_set():
                        email_service.delete_email(email)
                        current_email = None
                        return

                    self.log(f"开始注册: {email}", "info")

                    if not self.send_email_code_grpc(session, email):
                        self._fail_account(
                            email_service, email, f"{email} 发送邮箱验证码失败"
                        )
                        current_email = None
                        continue

                    self.log(f"{email} 已请求邮箱验证码，等待收件…", "info")
                    verify_code = email_service.fetch_verification_code(
                        email, stop_event=self.stop_event
                    )
                    if self.stop_event.is_set():
                        try:
                            email_service.delete_email(email)
                        except Exception:
                            pass
                        current_email = None
                        return
                    if not verify_code:
                        self._fail_account(
                            email_service, email, f"{email} 未收到邮箱验证码"
                        )
                        current_email = None
                        continue

                    self.log(f"{email} 邮箱验证码: {verify_code}，提交校验…", "info")
                    if not self.verify_email_code_grpc(session, email, verify_code):
                        self._fail_account(
                            email_service,
                            email,
                            f"{email} 邮箱验证码校验失败（码={verify_code}）",
                        )
                        current_email = None
                        continue

                    self.log(f"{email} 邮箱已通过（码={verify_code}），开始解 Turnstile…", "info")
                    signup_ok = False
                    last_turnstile_err = ""
                    # 邮箱验证码在「成功创建账号」后即失效；Turnstile 仅允许在未拿到 SSO 前重试
                    for attempt in range(1, 4):
                        if self.stop_event.is_set():
                            email_service.delete_email(email)
                            current_email = None
                            return

                        try:
                            self.log(
                                f"{email} Turnstile 第 {attempt}/3 次（Solver: {turnstile_service.solver_url}）",
                                "info",
                            )
                            task_id = turnstile_service.create_task(
                                self.site_url,
                                self.config["site_key"],
                                stop_event=self.stop_event,
                            )
                            self.log(f"{email} Turnstile 任务已创建: {str(task_id)[:18]}…", "info")
                            token = turnstile_service.get_response(
                                task_id, stop_event=self.stop_event
                            )
                        except RuntimeError as te:
                            if str(te) == "stopped" or self.stop_event.is_set():
                                try:
                                    email_service.delete_email(email)
                                except Exception:
                                    pass
                                current_email = None
                                return
                            last_turnstile_err = str(te)
                            self.log(f"{email} Turnstile 创建/查询失败: {te}", "error")
                            if self._sleep(1):
                                return
                            continue
                        except Exception as te:
                            if self.stop_event.is_set():
                                try:
                                    email_service.delete_email(email)
                                except Exception:
                                    pass
                                current_email = None
                                return
                            last_turnstile_err = str(te)
                            self.log(f"{email} Turnstile 创建/查询失败: {te}", "error")
                            if self._sleep(1):
                                return
                            continue

                        if self.stop_event.is_set() or (
                            not token and turnstile_service.last_error == "已停止"
                        ):
                            try:
                                email_service.delete_email(email)
                            except Exception:
                                pass
                            current_email = None
                            return

                        if not token or token == "CAPTCHA_FAIL":
                            err = turnstile_service.last_error or "无 token / CAPTCHA_FAIL"
                            last_turnstile_err = err
                            self.log(f"{email} Turnstile 失败: {err}", "warn")
                            if self._sleep(1):
                                return
                            continue

                        self.log(f"{email} Turnstile 成功，提交注册…", "info")
                        headers = {
                            "user-agent": account_user_agent,
                            "accept": "text/x-component",
                            "content-type": "text/plain;charset=UTF-8",
                            "origin": self.site_url,
                            "referer": f"{self.site_url}/sign-up",
                            "cookie": f"__cf_bm={session.cookies.get('__cf_bm', '')}",
                            "next-router-state-tree": self.config["state_tree"],
                            "next-action": final_action_id,
                        }
                        payload = [
                            {
                                "emailValidationCode": verify_code,
                                "createUserAndSessionRequest": {
                                    "email": email,
                                    "givenName": generate_random_name(),
                                    "familyName": generate_random_name(),
                                    "clearTextPassword": password,
                                    "tosAcceptedVersion": "$undefined",
                                },
                                "turnstileToken": token,
                                "promptOnDuplicateEmail": True,
                            }
                        ]

                        try:
                            # 仅起步节流，HTTP 本身并行，避免 8 线程全卡在一把锁上
                            self._throttle_signup_post()
                            res = session.post(
                                f"{self.site_url}/sign-up",
                                json=payload,
                                headers=headers,
                                timeout=15,
                            )
                        except Exception as pe:
                            if self.stop_event.is_set():
                                return
                            last_turnstile_err = f"提交注册异常: {pe}"
                            self.log(f"{email} 提交注册异常: {pe}", "error")
                            if self._sleep(1):
                                return
                            continue

                        if res.status_code != 200:
                            last_turnstile_err = f"注册 HTTP {res.status_code}: {res.text[:120]}"
                            self.log(f"{email} {last_turnstile_err}", "warn")
                            if self._sleep(1):
                                return
                            continue

                        body_text = res.text or ""
                        # 邮箱验证码已用尽 / 失效：再解 Turnstile 也没用，立刻结束本账号
                        if (
                            "invalid-validation" in body_text
                            or "Email validation code is invalid" in body_text
                            or "email:invalid" in body_text
                        ):
                            last_turnstile_err = "邮箱验证码已失效（不可重复注册）"
                            self.log(
                                f"{email} 邮箱验证码已失效，停止对本邮箱重试（避免空转）",
                                "error",
                            )
                            break

                        match = re.search(
                            r'(https://[^" \s]+set-cookie\?q=[^:" \s]+)1:', body_text
                        )
                        if not match:
                            last_turnstile_err = (
                                f"注册响应无 set-cookie: {body_text[:160]}"
                            )
                            self.log(f"{email} {last_turnstile_err}", "warn")
                            if self._sleep(1):
                                return
                            continue

                        verify_url = match.group(1)
                        try:
                            session.get(verify_url, allow_redirects=True, timeout=12)
                        except Exception:
                            if self.stop_event.is_set():
                                # 可能已有 cookie，尽量抢救
                                sso_try = session.cookies.get("sso")
                                if sso_try:
                                    self._emergency_save_sso(
                                        email, sso_try, "停止时抢救"
                                    )
                                return
                        sso = session.cookies.get("sso")
                        sso_rw = session.cookies.get("sso-rw")
                        if not sso:
                            last_turnstile_err = "未拿到 sso cookie"
                            self.log(f"{email} 未拿到 sso cookie，重试 Turnstile", "warn")
                            if self._sleep(1):
                                return
                            continue

                        # ========== 邮箱保护硬规则 ==========
                        # 1) 拿到 SSO = xAI 账号已创建，验证码已消耗
                        # 2) 立刻落盘，后续任何失败都不得丢号、不得重注册
                        # 3) device flow / 协议 / NSFW 全部是「锦上添花」
                        sso_saved = self._emergency_save_sso(
                            email, sso, "注册完成立即落盘"
                        )
                        if not sso_saved:
                            self.log(
                                f"{email} 落盘失败，将在记成功时再写一次",
                                "error",
                            )

                        # 关键提速：SSO 到手立刻计成功并开下一号；
                        # device flow / 协议 / NSFW 全部丢后台，不堵注册主路径。
                        self.log(
                            f"{email} 已拿到 SSO（{'已落盘' if sso_saved else '落盘失败'}），"
                            f"立即记成功，device flow/协议后台处理…",
                            "info",
                        )
                        sid = self._record_success(
                            email=email,
                            sso=sso,
                            unhinged_ok=False,
                            email_service=email_service,
                            note="enrich=pending",
                            already_written=sso_saved,
                            auth_token=None,
                        )
                        if sid:
                            self._schedule_enrich(
                                email=email,
                                sso=sso,
                                sso_rw=sso_rw or "",
                                success_id=sid,
                                impersonate=impersonate_fingerprint,
                                user_agent=account_user_agent,
                            )
                        # SSO 已在盘上：无论是否计入目标，都算本邮箱完成
                        current_email = None
                        signup_ok = True
                        break

                    if not signup_ok:
                        if self.stop_event.is_set():
                            if current_email:
                                try:
                                    email_service.delete_email(current_email)
                                except Exception:
                                    pass
                                current_email = None
                            return
                        detail = last_turnstile_err or "Turnstile/注册失败"
                        self._fail_account(
                            email_service,
                            email,
                            f"{email} 重试 3 次后仍失败（{detail}）",
                        )
                        current_email = None
                        # 本账号失败，继续下一封新邮箱，不结束整任务
                        continue

                    # 成功且未达目标：继续下一封邮箱
                    continue

            except Exception as e:
                if self.stop_event.is_set():
                    if current_email:
                        try:
                            email_service.delete_email(current_email)
                        except Exception:
                            pass
                    return
                self._fail_account(
                    email_service,
                    current_email,
                    f"异常: {str(e)[:120]}",
                )
                current_email = None
                if self._sleep(1):
                    return
                continue

    def _run_workers(self, workers: int):
        try:
            if not self.initialize():
                return
            if self.stop_event.is_set():
                self.status = "done"
                self.log("任务已取消（初始化后停止）", "warn")
                return
            self.status = "running"
            self.log(
                f"启动 {workers} 个注册线程，目标 {self.target_count} 个"
                f"（后台 enrich {self._enrich_workers} 线程）",
                "info",
            )
            self.log(f"输出: {self.output_file}", "info")
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                self._executor = executor
                futures = [executor.submit(self.register_single_thread) for _ in range(workers)]
                # 周期检查 stop，避免 wait 一直挂到所有线程自然结束才有反馈
                while True:
                    done, _not_done = concurrent.futures.wait(
                        futures, timeout=0.5, return_when=concurrent.futures.ALL_COMPLETED
                    )
                    if len(done) == len(futures):
                        break
                    if self.stop_event.is_set() and self.status == "stopping":
                        # 再等一小会儿让线程看到 stop_event 退出；最长约 6s
                        concurrent.futures.wait(futures, timeout=6)
                        break
            self._executor = None
            # 等后台 enrich 收尾一会儿（最多 ~20s），避免刚停任务 token 还没回填
            try:
                deadline = time.time() + 20.0
                while time.time() < deadline:
                    with self._enrich_lock:
                        pending = self._enrich_pending
                    if pending <= 0:
                        break
                    time.sleep(0.4)
                with self._enrich_lock:
                    left = self._enrich_pending
                if left > 0:
                    self.log(
                        f"仍有 {left} 个后台 enrich 未完成（device flow/协议），"
                        f"不影响已落盘 SSO；导入时可再换 token",
                        "warn",
                    )
            except Exception:
                pass
            if self.status == "error":
                pass
            elif self.stop_event.is_set() and self.success_count < self.target_count:
                self.status = "done"
                self.log(
                    f"任务已停止：成功 {self.success_count}/{self.target_count}，失败尝试 {self.fail_count}",
                    "warn",
                )
            else:
                self.status = "done"
                self.log(
                    f"任务结束：成功 {self.success_count}/{self.target_count}，失败尝试 {self.fail_count}",
                    "success" if self.success_count else "warn",
                )
        except Exception as e:
            self.error_message = str(e)
            self.status = "error"
            self.log(f"运行失败: {e}", "error")
        finally:
            self.stop_event.set()
            if self.status not in ("done", "error"):
                self.status = "done"
            # 任务结束即停看门狗，避免空闲时反复拉起已崩溃的 Solver
            try:
                import solver_manager

                solver_manager.stop_watchdog()
            except Exception:
                pass

    def stop(self) -> dict:
        if not self.is_running():
            return {"ok": False, "message": "当前没有运行中的任务", **self.get_status()}
        self.status = "stopping"
        self.stop_event.set()
        self.log("正在停止任务（等待当前网络请求结束，最多约数秒）…", "warn")
        return {"ok": True, "message": "已发送停止信号", **self.get_status()}

    def start(self, workers: int = 8, target: int = 100, blocking: bool = False) -> dict:
        with self._run_lock:
            # 线程还没跑到 initialize 时 status 也要立刻占位，防止重复启动
            if self.is_running() or (
                self._worker_thread is not None and self._worker_thread.is_alive()
            ):
                return {"ok": False, "message": "任务已在运行中", **self.get_status()}

            workers = max(1, min(int(workers), 64))
            target = max(1, int(target))

            self.stop_event.clear()
            self.success_count = 0
            self.fail_count = 0
            self.target_count = target
            self.workers = workers
            self.start_time = time.time()
            self.error_message = ""
            self.recent_success.clear()
            with self._enrich_lock:
                self._enrich_pending = 0
                self._nsfw_ok_count = 0
                self._nsfw_fail_count = 0
            # 先标 initializing，避免并发 /api/start 重复拉起
            self.status = "initializing"
            # 不清空缓存的 action_id；initialize 会优先用缓存
            if not self.config.get("action_id") and self._action_cache.get("action_id"):
                self._apply_action_cache()

            os.makedirs("keys", exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.output_file = f"keys/grok_{timestamp}_{target}.txt"

            if blocking:
                self._run_workers(workers)
                return {"ok": True, "message": "任务完成", **self.get_status()}

            self._worker_thread = threading.Thread(
                target=self._run_workers,
                args=(workers,),
                daemon=True,
                name="RegisterEngine",
            )
            self._worker_thread.start()
            return {"ok": True, "message": "任务已启动", **self.get_status()}


# 兼容旧 CLI 全局入口
_cli_logs = LogBuffer()
engine = RegisterEngine(log_fn=lambda msg, level="info": _cli_logs.emit(msg, level))


def main():
    print("=" * 60 + "\nGrok 注册机\n" + "=" * 60)
    try:
        t = int(input("\n并发数 (默认8): ").strip() or 8)
    except Exception:
        t = 8
    try:
        total = int(input("注册数量 (默认100): ").strip() or 100)
    except Exception:
        total = 100
    engine.start(workers=t, target=total, blocking=True)


if __name__ == "__main__":
    main()
