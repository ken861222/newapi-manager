"""NewAPI 管理平台 — Flask 后端."""

import os
from dotenv import load_dotenv
load_dotenv()
import time
import hashlib
import concurrent.futures
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from cryptography.fernet import Fernet

from models import db, Site, CheckinRecord, RequestLog
from newapi_client import NewAPIClient

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///data.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(32).hex())
app.config["SESSION_COOKIE_NAME"] = "newapi_manager_session"
db.init_app(app)

# ── Auth config from env ──────────────────────────────────────────────
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")


def _hash_pw(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


# ── Encryption ────────────────────────────────────────────────────────

KEY_FILE = os.path.join(os.path.dirname(__file__), ".secret_key")


def _load_key() -> bytes:
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE, "rb").read()
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    return key


_fernet = Fernet(_load_key())
enc = lambda t: _fernet.encrypt(t.encode())
dec = lambda t: _fernet.decrypt(t).decode()


# ── Auth middleware ────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


# ── Cache ─────────────────────────────────────────────────────────────

_dashboard_cache = {"data": None, "ts": 0}
_CACHE_TTL = 120


def _invalidate_cache():
    _dashboard_cache["data"] = None
    _dashboard_cache["ts"] = 0


# ── Request logger ────────────────────────────────────────────────────

def _make_log_handler(site_id):
    def handler(method, url, status, req_body, resp_body, error=None):
        with app.app_context():
            log = RequestLog(
                site_id=site_id, method=method, url=url, status_code=status,
                request_body=req_body, response_body=resp_body, error=error,
            )
            db.session.add(log)
            db.session.commit()
    return handler


# ── Client factory ────────────────────────────────────────────────────

def _client(site: Site) -> NewAPIClient:
    kwargs = {"base_url": site.url, "user_id": site.user_id or 0,
              "on_log": _make_log_handler(site.id)}
    if site.token_encrypted:
        kwargs["token"] = dec(site.token_encrypted)
    else:
        kwargs["username"] = site.username
        kwargs["password"] = dec(site.password_encrypted)
    return NewAPIClient(**kwargs)


# ── Core helpers ──────────────────────────────────────────────────────

def _today_cst():
    return (datetime.utcnow() + timedelta(hours=8)).date()


def _fetch_site_status(site):
    try:
        client = _client(site)
        info = client.get_user_info()
        last_rec = (
            CheckinRecord.query.filter_by(site_id=site.id)
            .order_by(CheckinRecord.created_at.desc()).first()
        )
        today_cst = _today_cst()
        checked_today = last_rec and (last_rec.created_at + timedelta(hours=8)).date() >= today_cst and last_rec.success
        return {
            "id": site.id, "name": site.name, "url": site.url,
            "user_id": site.user_id, "auth_mode": site.auth_mode,
            "auto_checkin": site.auto_checkin, "checkin_hour": site.checkin_hour,
            "online": True,
            "quota": info.quota, "used_quota": info.used_quota,
            "balance_usd": round(info.balance_usd, 4),
            "used_usd": round(info.used_usd, 4),
            "request_count": info.request_count,
            "group": info.group, "role": info.role,
            "display_name": info.display_name or info.username,
            "checked_today": checked_today,
            "last_checkin_msg": last_rec.message if last_rec else "",
            "last_checkin_at": last_rec.created_at.isoformat() if last_rec else None,
        }
    except Exception as exc:
        return {
            "id": site.id, "name": site.name, "url": site.url,
            "user_id": site.user_id, "auth_mode": site.auth_mode,
            "auto_checkin": site.auto_checkin, "checkin_hour": site.checkin_hour,
            "online": False, "error": str(exc),
            "quota": 0, "used_quota": 0, "balance_usd": 0, "used_usd": 0,
            "request_count": 0, "checked_today": False,
            "last_checkin_msg": "", "last_checkin_at": None,
        }


def _checkin_site(site: Site) -> dict:
    try:
        result = _client(site).checkin()
        rec = CheckinRecord(site_id=site.id, success=result.success, message=result.message)
        db.session.add(rec)
        db.session.commit()
        return {"site_id": site.id, "name": site.name,
                "success": result.success, "message": result.message,
                "quota_gained": result.quota_gained}
    except Exception as exc:
        rec = CheckinRecord(site_id=site.id, success=False, message=str(exc))
        db.session.add(rec)
        db.session.commit()
        return {"site_id": site.id, "name": site.name,
                "success": False, "message": str(exc), "quota_gained": 0}


# ── Scheduler ─────────────────────────────────────────────────────────

scheduler = BackgroundScheduler()


def _scheduled_checkin():
    with app.app_context():
        cst_hour = (datetime.utcnow().hour + 8) % 24
        for site in Site.query.filter_by(auto_checkin=True).all():
            if site.checkin_hour == cst_hour or site.checkin_hour == -1:
                _checkin_site(site)


scheduler.add_job(_scheduled_checkin, "cron", minute=0)
scheduler.start()


# ── Pages ─────────────────────────────────────────────────────────────

@app.route("/login")
def login_page():
    if session.get("logged_in"):
        return redirect("/")
    return render_template("login.html")


@app.route("/")
@login_required
def index():
    return render_template("index.html")


# ── Auth API ──────────────────────────────────────────────────────────

@app.route("/api/login", methods=["POST"])
def api_login():
    d = request.json or {}
    if d.get("username") == ADMIN_USER and d.get("password") == ADMIN_PASS:
        session["logged_in"] = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "用户名或密码错误"}), 401


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"success": True})


@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    global ADMIN_PASS
    d = request.json or {}
    old_pw = d.get("old_password", "")
    new_pw = d.get("new_password", "")
    if old_pw != ADMIN_PASS:
        return jsonify({"error": "原密码错误"}), 400
    if len(new_pw) < 4:
        return jsonify({"error": "密码至少4位"}), 400
    ADMIN_PASS = new_pw
    # Write to .env file
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    _update_env(env_path, "ADMIN_PASS", new_pw)
    return jsonify({"success": True})


def _update_env(path, key, value):
    lines = []
    found = False
    if os.path.exists(path):
        with open(path, "r") as f:
            for line in f:
                if line.startswith(key + "="):
                    lines.append(f"{key}={value}\n")
                    found = True
                else:
                    lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)


# ── Site API ──────────────────────────────────────────────────────────

@app.route("/api/sites", methods=["GET"])
@login_required
def list_sites():
    return jsonify([s.to_dict() for s in Site.query.all()])


@app.route("/api/sites", methods=["POST"])
@login_required
def add_site():
    d = request.json or {}
    url = (d.get("url") or "").strip().rstrip("/")
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    token = (d.get("token") or "").strip()
    user_id = d.get("user_id") or 0
    name = (d.get("name") or "").strip()
    if not name:
        name = "站点-" + url.replace("https://", "").replace("http://", "").split("/")[0]
    site = Site(name=name, url=url, user_id=user_id,
                auto_checkin=d.get("auto_checkin", True),
                checkin_hour=d.get("checkin_hour", 8))
    if token:
        site.token_encrypted = enc(token)
    db.session.add(site)
    db.session.commit()
    _invalidate_cache()
    return jsonify(site.to_dict())


@app.route("/api/sites/<int:sid>", methods=["PUT"])
@login_required
def update_site(sid):
    site = db.session.get(Site, sid)
    if not site:
        return jsonify({"error": "站点不存在"}), 404
    d = request.json or {}
    if "name" in d: site.name = d["name"]
    if "url" in d: site.url = d["url"].rstrip("/")
    if "user_id" in d: site.user_id = d["user_id"]
    if d.get("token"): site.token_encrypted = enc(d["token"])
    if "auto_checkin" in d: site.auto_checkin = d["auto_checkin"]
    if "checkin_hour" in d: site.checkin_hour = d["checkin_hour"]
    db.session.commit()
    _invalidate_cache()
    return jsonify(site.to_dict())


@app.route("/api/sites/<int:sid>", methods=["DELETE"])
@login_required
def delete_site(sid):
    site = db.session.get(Site, sid)
    if not site:
        return jsonify({"error": "站点不存在"}), 404
    db.session.delete(site)
    db.session.commit()
    _invalidate_cache()
    return jsonify({"success": True})


@app.route("/api/test-connection", methods=["POST"])
@login_required
def test_connection():
    d = request.json or {}
    url = (d.get("url") or "").strip().rstrip("/")
    token = (d.get("token") or "").strip()
    user_id = d.get("user_id") or 0
    if not url or not token or not user_id:
        return jsonify({"error": "缺少参数"}), 400
    try:
        client = NewAPIClient(base_url=url, user_id=int(user_id), token=token)
        info = client.get_user_info()
        return jsonify({"success": True, "display_name": info.display_name or info.username,
                        "balance_usd": round(info.balance_usd, 4),
                        "used_usd": round(info.used_usd, 4),
                        "request_count": info.request_count})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)})


@app.route("/api/dashboard", methods=["GET"])
@login_required
def dashboard():
    now = time.time()
    if _dashboard_cache["data"] and now - _dashboard_cache["ts"] < _CACHE_TTL:
        return jsonify(_dashboard_cache["data"])
    sites = Site.query.all()
    if not sites:
        empty = {"sites": [], "summary": {"total": 0, "online": 0, "total_balance": 0, "total_used": 0, "total_requests": 0}}
        return jsonify(empty)
    def _fetch_with_ctx(s):
        with app.app_context():
            return _fetch_site_status(s)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_fetch_with_ctx, sites))
    online = sum(1 for r in results if r.get("online"))
    result_data = {
        "sites": results,
        "summary": {
            "total": len(results), "online": online, "offline": len(results) - online,
            "total_balance": round(sum(r["balance_usd"] for r in results), 4),
            "total_used": round(sum(r["used_usd"] for r in results), 4),
            "total_requests": sum(r["request_count"] for r in results),
        },
    }
    _dashboard_cache["data"] = result_data
    _dashboard_cache["ts"] = now
    return jsonify(result_data)


@app.route("/api/sites/<int:sid>/checkin", methods=["POST"])
@login_required
def checkin_site_route(sid):
    site = db.session.get(Site, sid)
    if not site: return jsonify({"error": "站点不存在"}), 404
    result = _checkin_site(site)
    _invalidate_cache()
    return jsonify(result)


@app.route("/api/checkin-all", methods=["POST"])
@login_required
def checkin_all():
    sites = Site.query.all()
    if not sites: return jsonify({"results": []})
    def _checkin_with_ctx(s):
        with app.app_context():
            return _checkin_site(s)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(_checkin_with_ctx, sites))
    _invalidate_cache()
    return jsonify({"results": results})


@app.route("/api/checkin-history", methods=["GET"])
@login_required
def checkin_history():
    limit = request.args.get("limit", 50, type=int)
    records = CheckinRecord.query.order_by(CheckinRecord.created_at.desc()).limit(limit).all()
    result = []
    for r in records:
        d = r.to_dict()
        d["site_name"] = r.site.name if r.site else "?"
        result.append(d)
    return jsonify(result)


# ── Request Logs API ──────────────────────────────────────────────────

@app.route("/api/logs", methods=["GET"])
@login_required
def get_logs():
    site_id = request.args.get("site_id", type=int)
    limit = request.args.get("limit", 100, type=int)
    q = RequestLog.query.order_by(RequestLog.created_at.desc())
    if site_id:
        q = q.filter_by(site_id=site_id)
    logs = q.limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=False)
