"""NewAPI HTTP client — Bearer token + New-Api-User header per official docs."""

import requests
from typing import Optional, List, Tuple, Callable
from dataclasses import dataclass, field

TIMEOUT = 15


@dataclass
class UserInfo:
    username: str = ""
    display_name: str = ""
    user_id: int = 0
    quota: int = 0
    used_quota: int = 0
    request_count: int = 0
    group: str = ""
    role: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def balance_usd(self) -> float:
        return self.quota / 500000

    @property
    def used_usd(self) -> float:
        return self.used_quota / 500000


@dataclass
class CheckinResult:
    success: bool
    message: str
    quota_gained: int = 0


@dataclass
class LogEntry:
    model_name: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    quota: int = 0
    created_at: int = 0
    channel: str = ""
    token_name: str = ""


class NewAPIClient:
    def __init__(self, base_url: str, user_id: int,
                 token: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 on_log: Optional[Callable] = None):
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._on_log = on_log
        self._session.headers.update({
            "Content-Type": "application/json",
            "New-Api-User": str(user_id),
        })
        if token:
            self._session.headers["Authorization"] = "Bearer " + token.strip()
        elif username and password:
            self._login(username, password)
        else:
            raise ValueError("需要提供 token 或 username+password")

    def _login(self, username: str, password: str):
        r = self._session.post(
            self.base_url + "/api/user/login",
            json={"username": username, "password": password},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            raise RuntimeError(data.get("message", "登录失败"))
        self._session.headers["Authorization"] = "Bearer " + data["data"]

    def _log(self, method: str, path: str, status: int, req_body: str, resp_body: str, error: str = None):
        if self._on_log:
            try:
                self._on_log(method, self.base_url + path, status, req_body, resp_body, error)
            except Exception:
                pass

    def _request(self, method: str, path: str, raw: bool = False, **kw) -> dict:
        full_url = self.base_url + path
        req_body = str(kw.get("json", kw.get("data", "")))[:1000]
        try:
            r = self._session.request(method, full_url, timeout=TIMEOUT, **kw)
            resp_body = r.text[:1000]
            if r.status_code == 401:
                self._log(method, path, r.status_code, req_body, resp_body, "令牌无效或已过期")
                raise RuntimeError("令牌无效或已过期 (401)")
            r.raise_for_status()
            d = r.json()
            self._log(method, path, r.status_code, req_body, resp_body)
            if not raw and not d.get("success") and d.get("message"):
                raise RuntimeError(d["message"])
            return d
        except requests.RequestException as e:
            self._log(method, path, 0, req_body, "", str(e))
            raise

    def get_user_info(self) -> UserInfo:
        data = self._request("GET", "/api/user/self")
        u = data.get("data", {})
        return UserInfo(
            username=u.get("username", ""), display_name=u.get("display_name", ""),
            user_id=u.get("id", 0), quota=u.get("quota", 0),
            used_quota=u.get("used_quota", 0), request_count=u.get("request_count", 0),
            group=u.get("group", ""), role=u.get("role", ""), raw=u,
        )

    def checkin(self) -> CheckinResult:
        for endpoint in ("/api/user/checkin", "/api/user/sign_in"):
            try:
                data = self._request("POST", endpoint, raw=True)
                msg = str(data.get("message", ""))
                ok = bool(data.get("success"))
                if not ok and ("已签到" in msg or "already" in msg.lower()):
                    ok = True
                return CheckinResult(success=ok, message=msg,
                                     quota_gained=data.get("data", 0) if data.get("success") else 0)
            except Exception:
                continue
        return CheckinResult(success=False, message="签到接口不存在或不支持")

    def get_logs(self, page: int = 1, per_page: int = 20, **kw) -> Tuple[List[LogEntry], int]:
        params = {"p": page, "per_page": per_page, "type": 0, **kw}
        data = self._request("GET", "/api/log/search", params=params)
        entries = [LogEntry(
            model_name=e.get("model_name", ""), prompt_tokens=e.get("prompt_tokens", 0),
            completion_tokens=e.get("completion_tokens", 0), quota=e.get("quota", 0),
            created_at=e.get("created_at", 0), channel=e.get("channel", ""),
            token_name=e.get("token_name", ""),
        ) for e in (data.get("data") or [])]
        return entries, data.get("total", 0)
