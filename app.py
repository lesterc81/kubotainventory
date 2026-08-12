"""
IT Asset Accountability & Workstation Management System
---------------------------------------------------------------------------
Refactored for production best-practice:
  - Application factory (create_app) instead of a module-level app singleton
  - Config classes per environment (no hardcoded dev secret in prod)
  - Blueprints instead of one flat route namespace
  - Centralized ObjectId parsing (no repeated try/except InvalidId blocks)
  - Centralized date handling (fixes BSON InvalidDocument crash on
    datetime.date objects â€” Mongo only supports datetime.datetime)
  - Logging instead of silent `except: pass`
  - CLI commands for admin seeding / index creation instead of
    doing it inside `if __name__ == "__main__"` (so `flask run` and
    gunicorn workers get it too)
  - Complete transfer functionality with validation
---------------------------------------------------------------------------
"""

import io
import json
import logging
import os
import re
import smtplib
import socket
import shutil
import threading
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from functools import wraps
from urllib.parse import urlsplit, urlunsplit

import bcrypt
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import errors as pymongo_errors
from flask import (Blueprint, Flask, abort, current_app, flash, jsonify,
                    redirect, render_template, request, send_file, session,
                    url_for)
from flask_cors import CORS
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                          login_user, logout_user)
from flask_pymongo import PyMongo
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect
from itsdangerous import URLSafeTimedSerializer
from wtforms import (BooleanField, DateField, HiddenField, PasswordField,
                      SelectField, StringField, TextAreaField)
from wtforms.validators import DataRequired, Email, Length, Optional

load_dotenv()

# =============================================================================
# Config
# =============================================================================
class BaseConfig:
    SECRET_KEY = os.environ["SECRET_KEY"]  # fail fast if missing â€” no silent dev fallback
    MONGO_URI = os.environ["MONGO_URI"]
    WTF_CSRF_ENABLED = True
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16MB
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PER_PAGE_DEFAULT = 10
    TRUST_PROXY_HEADERS = os.environ.get("TRUST_PROXY_HEADERS", "0") == "1"
    # SMTP â€” used for accountability receive emails (empty server = email disabled)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "true").lower() in ("1", "true", "yes")
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_FROM = os.environ.get("MAIL_FROM", "")
    MAIL_FROM_NAME = os.environ.get("MAIL_FROM_NAME", "IT Asset System")
    RECEIVE_TOKEN_MAX_AGE = 7 * 24 * 3600  # receive link valid for 7 days
    # Fixed base URL used in email links (e.g. "http://172.31.201.79:5000").
    # When empty, links auto-detect the request host.
    APP_BASE_URL = os.environ.get("APP_BASE_URL", "")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SESSION_COOKIE_SECURE = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    # The packaged desktop app serves plain HTTP on a LAN, so Secure cookies
    # (which browsers/requests refuse to send over http://) would break login.
    # Enable ``SESSION_COOKIE_SECURE`` only when TLS is actually terminated.
    SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() in ("1", "true", "yes")


class TestingConfig(BaseConfig):
    TESTING = True
    WTF_CSRF_ENABLED = False
    SECRET_KEY = os.environ.get("SECRET_KEY", "test-secret-key")
    MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/itsystem_test")


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}

# =============================================================================
# Extensions (instantiated once, bound to the app in create_app)
# =============================================================================
mongo = PyMongo()
csrf = CSRFProtect()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"

logger = logging.getLogger("itsystem")


# =============================================================================
# Email helpers (SMTP â€” stdlib, no extra dependency)
# =============================================================================
def mail_configured():
    """True when SMTP credentials exist so emails can actually be sent."""
    return bool(current_app.config.get("MAIL_SERVER") and current_app.config.get("MAIL_USERNAME"))


def get_receive_serializer():
    """Signed, time-limited token serializer for the 'Receive Assets' links."""
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="accountability-receive")


def send_mail(to_addr, subject, html_body):
    """Send an HTML email via the configured SMTP server. Raises on failure."""
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['MAIL_FROM_NAME']} <{cfg['MAIL_FROM']}>"
    msg["To"] = to_addr
    msg.set_content("This email requires an HTML-capable client. "
                    "Please open it in a modern web/email application.")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=30) as server:
        if cfg["MAIL_USE_TLS"]:
            server.starttls()
        server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        server.send_message(msg)


def send_receive_email(acc, emp):
    """Email the employee the 'Receive Assets' link for an accountability record."""
    token = get_receive_serializer().dumps({"acc_id": str(acc["_id"])})
    base = current_app.config.get("APP_BASE_URL", "").rstrip("/")
    if base:
        link = base + url_for("accountabilities.receive",
                              acc_id=str(acc["_id"]), token=token)
    else:
        link = url_for("accountabilities.receive",
                       acc_id=str(acc["_id"]), token=token, _external=True)
    employee_name = emp.get("full_name", "Employee")
    acc_type = acc.get("accountability_type", "")
    asset_count = len(acc.get("asset_ids", [])) or 0
    html = f"""
<div style="font-family:'DM Sans',Arial,sans-serif;max-width:560px;margin:auto;background:#FAF8F3;border:2.5px solid #221E18;border-radius:16px 18px 14px 20px;box-shadow:4px 4px 0 #221E18;padding:28px">
  <div style="text-align:center;margin-bottom:20px">
    <div style="font-size:15px;font-weight:700;letter-spacing:.5px;color:#221E18">IT ASSET SYSTEM</div>
    <div style="font-size:12px;color:#8A7F72;margin-top:2px">Asset Accountability</div>
  </div>
  <h1 style="font-family:'Prata',serif;font-weight:400;font-size:22px;margin:0 0 6px;color:#221E18">Hello {employee_name},</h1>
  <p style="font-size:14px;color:#3a342b;line-height:1.6;margin:0 0 18px">
    An accountability record of type <strong>{acc_type}</strong> covering
    <strong>{asset_count} asset(s)</strong> has been assigned to you.
    Please review the details and confirm receipt of the items.
  </p>
  <div style="text-align:center;margin:24px 0">
    <a href="{link}" style="display:inline-block;background:#221E18;color:#FAF8F3;padding:14px 28px;border-radius:12px;text-decoration:none;font-weight:700;font-size:14px;border:2px solid #221E18;box-shadow:3px 3px 0 #C73E3E">
      Confirm I Received These Assets
    </a>
  </div>
  <p style="font-size:12px;color:#8A7F72;line-height:1.5;margin:0">
    This link expires in 7 days. If you did not expect this email or believe it was sent
    in error, please contact your IT department.
  </p>
</div>
"""
    send_mail(emp["email"], "IT Asset Accountability â€” Please Confirm Receipt", html)


# =============================================================================
# Domain helpers
# =============================================================================
def to_datetime(value):
    """Normalize a date/datetime/None into a datetime for BSON storage."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time())
    return value


def safe_object_id(raw_id):
    """Parse a string into an ObjectId, or return None instead of raising."""
    if not raw_id:
        return None
    try:
        return ObjectId(raw_id)
    except (InvalidId, TypeError):
        return None


def get_or_404(collection_name, raw_id):
    """Fetch a document by id, or abort(404) if the id is invalid/missing."""
    oid = safe_object_id(raw_id)
    if oid is None:
        abort(404)
    doc = mongo.db[collection_name].find_one({"_id": oid})
    if doc is None:
        abort(404)
    return doc


def serialize_doc(doc):
    """Convert a MongoDB document into a JSON/Jinja-safe dict."""
    if doc is None:
        return None
    result = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, list):
            result[k] = [
                serialize_doc(i) if isinstance(i, dict)
                else str(i) if isinstance(i, ObjectId)
                else i
                for i in v
            ]
        elif isinstance(v, dict):
            result[k] = serialize_doc(v)
        else:
            result[k] = v
    if "_id" in doc:
        result["id"] = str(doc["_id"])   # <-- add this
    return result


def _process_audit_value(val):
    """Best-effort enrichment of an audit payload."""
    if not isinstance(val, dict):
        return val
    processed = {}
    for k, v in val.items():
        if isinstance(v, ObjectId):
            processed[k] = str(v)
            name = _lookup_display_name(v)
            if name:
                processed[f"{k}_name"] = name
        elif isinstance(v, datetime):
            processed[k] = v.isoformat()
        elif isinstance(v, dict):
            processed[k] = _process_audit_value(v)
        else:
            processed[k] = v
    return processed


def _lookup_display_name(oid):
    try:
        emp = mongo.db.employees.find_one({"_id": oid}, {"full_name": 1})
        if emp:
            return emp.get("full_name")
        asset = mongo.db.assets.find_one({"_id": oid}, {"asset_tag": 1})
        if asset:
            return asset.get("asset_tag")
        ws = mongo.db.workstations.find_one({"_id": oid}, {"workstation_code": 1})
        if ws:
            return ws.get("workstation_code")
    except Exception:
        logger.exception("Audit display-name lookup failed for %s", oid)
    return None


def audit_log(module, action, old_value=None, new_value=None, record_id=None):
    mongo.db.audit_logs.insert_one({
        "timestamp": datetime.utcnow(),
        "username": current_user.username if current_user.is_authenticated else "system",
        "ip_address": get_client_ip(),
        "module": module,
        "action": action,
        "record_id": str(record_id) if record_id else None,
        "old_value": _process_audit_value(old_value),
        "new_value": _process_audit_value(new_value),
    })


def get_client_ip():
    if current_app.config.get("TRUST_PROXY_HEADERS"):
        return request.headers.get("X-Forwarded-For", request.remote_addr)
    return request.remote_addr


# =============================================================================
# Input hygiene, status normalization, transactions, rate limiting, TTL cache
# =============================================================================
ACC_STATUSES = frozenset({"Active", "Pending Return", "Returned", "Incomplete", "Archived"})

ASSET_STATUSES = frozenset({"Available", "Assigned", "Under Maintenance", "Retired", "Disposed", "Lost"})


def safe_regex(raw, max_len=80):
    """Return a regex-literal pattern for user input (ReDoS + operator-safe)."""
    if not raw:
        return ""
    return re.escape(raw.strip()[:max_len])


def qre(q):
    """Build a case-insensitive regex dict that escapes user input."""
    return {"$regex": safe_regex(q), "$options": "i"}


def normalize_acc_status(status):
    """Map legacy statuses onto the canonical accountability status set."""
    if not status:
        return status
    s = str(status)
    if s == "Completed":
        return "Returned"
    return s if s in ACC_STATUSES else s


def _run_in_transaction(fn):
    """Execute a mutating flow inside a MongoDB session transaction when available.

    Falls back to running without a transaction on standalone/unsupported servers
    rather than failing, keeping behaviour correct when transactions are not
    supported by the deployment.
    """
    try:
        with mongo.cx.start_session() as s:
            try:
                return s.with_transaction(lambda _s: fn(session=_s))
            except pymongo_errors.OperationFailure as e:
                if e.code == 20:  # not a replica set / transactions unsupported
                    return fn(session=None)
                raise
    except pymongo_errors.OperationFailure as e:
        if e.code == 20:
            return fn(session=None)
        raise
    except (pymongo_errors.ConfigurationError, AttributeError):
        return fn(session=None)


_login_attempts = {}
_login_lock = threading.Lock()


def is_login_limited(ip, max_attempts=5, window_seconds=300):
    """True when IP has reached max_attempts failed logins within the window."""
    with _login_lock:
        now = time.time()
        cutoff = now - window_seconds
        recent = [t for t in _login_attempts.get(ip, []) if t >= cutoff]
        _login_attempts[ip] = recent
        return len(recent) >= max_attempts


def record_login_failure(ip):
    with _login_lock:
        _login_attempts.setdefault(ip, []).append(time.time())


def clear_login_failures(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


_dash_cache = {}
_dash_cache_lock = threading.Lock()


def get_dashboard_cache(key, ttl_seconds=30):
    with _dash_cache_lock:
        entry = _dash_cache.get(key)
        if entry and time.time() - entry[0] < ttl_seconds:
            return entry[1]
    return None


def set_dashboard_cache(key, value):
    with _dash_cache_lock:
        _dash_cache[key] = (time.time(), value)


def clear_dashboard_cache(key=None):
    with _dash_cache_lock:
        if key is None:
            _dash_cache.clear()
        else:
            _dash_cache.pop(key, None)


_ref_data_cache = {}
_ref_data_cache_lock = threading.Lock()
REF_DATA_TTL_SECONDS = 15


def get_reference_data(key, builder):
    """TTL-cached access to small reference lists (dropdown data).

    Returns a fresh list copy each call; the underlying list is refreshed from
    the DB at most once every REF_DATA_TTL_SECONDS. Use clear_reference_data()
    after employee/workstation writes when freshness matters immediately.
    """
    with _ref_data_cache_lock:
        entry = _ref_data_cache.get(key)
        if entry and time.time() - entry[0] < REF_DATA_TTL_SECONDS:
            return list(entry[1])
    value = list(builder())
    with _ref_data_cache_lock:
        _ref_data_cache[key] = (time.time(), value)
    return list(value)


def clear_reference_data(key=None):
    with _ref_data_cache_lock:
        if key is None:
            _ref_data_cache.clear()
        else:
            _ref_data_cache.pop(key, None)


def get_active_employees():
    """Active employees, sorted by name, TTL-cached."""
    return get_reference_data(
        "active_employees",
        lambda: mongo.db.employees.find({"status": "Active"}).sort("full_name", 1))


def get_active_workstations():
    """Active workstations, sorted by code, TTL-cached."""
    return get_reference_data(
        "active_workstations",
        lambda: mongo.db.workstations.find({"status": "Active"}).sort("workstation_code", 1))


_name_cache = {}
_name_cache_lock = threading.Lock()


def _name_cache_key(collection, oid):
    return "{}:{}".format(collection, oid)


def _cached_name(collection, oid):
    key = _name_cache_key(collection, oid)
    with _name_cache_lock:
        cached = _name_cache.get(key)
        if cached is not None:
            return cached
    coll = getattr(mongo.db, collection, None)
    name = None
    if coll is not None:
        doc = coll.find_one({"_id": oid}, {"name": 1, "full_name": 1, "serial_number": 1})
        if doc:
            name = doc.get("name") or doc.get("full_name") or doc.get("serial_number") or str(oid)
    with _name_cache_lock:
        _name_cache[key] = name
    return name


def invalidate_cached_name(collection, oid):
    key = _name_cache_key(collection, oid)
    with _name_cache_lock:
        _name_cache.pop(key, None)


def _build_name_map(oids):
    """Preload display names for a set of ObjectIds to avoid N+1 audit lookups."""
    if not oids:
        return {}
    ids = list(oids)
    name_map = {}
    for collection, field in (("employees", "full_name"),
                              ("assets", "asset_tag"),
                              ("workstations", "workstation_code")):
        coll = getattr(mongo.db, collection, None)
        if coll is None:
            continue
        for doc in coll.find({"_id": {"$in": ids}}, {field: 1}):
            name_map[str(doc["_id"])] = doc.get(field)
    return name_map


def enrich_audit_log(log_dict, name_map=None):
    """Attach human-readable names to a serialized audit log entry for display."""
    record_id = log_dict.get("record_id")
    if record_id:
        oid = safe_object_id(record_id)
        if oid:
            name = (name_map or {}).get(str(oid)) or _lookup_display_name(oid)
            if name:
                log_dict["record_name"] = name
    for value_key in ("old_value", "new_value"):
        val = log_dict.get(value_key)
        if isinstance(val, dict):
            for ref_field in ("assigned_to", "workstation_id", "asset_id", "employee_id"):
                ref = val.get(ref_field)
                if ref and f"{ref_field}_name" not in val:
                    oid = safe_object_id(ref)
                    if oid:
                        name = (name_map or {}).get(str(oid)) or _lookup_display_name(oid)
                        if name:
                            val[f"{ref_field}_name"] = name
    return log_dict


def paginate(query_result_cursor_factory, query, collection, sort_field, sort_dir=-1,
             page=1, per_page=None, projection=None):
    """Shared pagination helper."""
    per_page = per_page or current_app.config["PER_PAGE_DEFAULT"]
    page = max(page, 1)
    skip = (page - 1) * per_page
    total = collection.count_documents(query)
    items = list(collection.find(query, projection or {}).sort(sort_field, sort_dir).skip(skip).limit(per_page))
    total_pages = max((total + per_page - 1) // per_page, 1)
    return items, total, total_pages, page, per_page


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def editor_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_edit():
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _deliver_download(filename, buf_bytes, mimetype):
    """Return an export in a way that works in both browser and desktop exe.

    In desktop mode the file is written to the app's Exports folder and a small
    "Saved" page is returned (the launcher's JS API then opens it with the OS),
    because WebView2 drops `Content-Disposition: attachment` downloads. Otherwise
    behave like a normal streaming download.
    """
    buf = io.BytesIO(buf_bytes)
    buf.seek(0)
    exports_dir = current_app.config.get("EXPORTS_DIR")
    if current_app.config.get("IS_DESKTOP") and exports_dir:
        try:
            os.makedirs(exports_dir, exist_ok=True)
            dest = os.path.join(exports_dir, filename)
            with open(dest, "wb") as fh:
                shutil.copyfileobj(buf, fh)
            return render_template("exports/saved.html", filename=filename,
                                   full_path=dest)
        except Exception:
            pass
    return send_file(buf, download_name=filename, as_attachment=True, mimetype=mimetype)


def generate_qr(data_str, fill_color="black", back_color="white"):
    """Generic QR generator â€” encodes any string as base64 PNG."""
    import qrcode
    qr = qrcode.QRCode(version=1, box_size=4, border=2,
                        error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(data_str)
    qr.make(fit=True)
    img = qr.make_image(fill_color=fill_color, back_color=back_color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    import base64
    return base64.b64encode(buf.read()).decode("utf-8")


def _lan_ip():
    """Best-effort LAN address, so QR links resolve from phones, not 127.0.0.1."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _reachable_scan_url(endpoint, record_id):
    """A scan URL a phone can actually open.

    Uses APP_BASE_URL when configured; otherwise builds the URL from the
    incoming request but replaces any loopback/(0.0.0.0) host with the LAN IP.
    """
    base = current_app.config.get("APP_BASE_URL", "").rstrip("/")
    if base:
        return base + url_for(endpoint, record_id=record_id)

    external = url_for(endpoint, record_id=record_id, _external=True)
    parts = urlsplit(external)
    host = parts.hostname or ""
    if host in ("127.0.0.1", "0.0.0.0", "localhost"):
        host = _lan_ip()
    port = parts.port
    netloc = f"{host}:{port}" if port and port not in (80, 443) else host
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def generate_asset_qr(asset_doc):
    """QR encodes ONLY the scan URL.

    A pure single URI is required for iOS (iPhone) Camera to treat the QR as a
    tappable link; mixed multi-line text defeats that. The scan page shows the
    full asset detail, so nothing is lost.
    """
    asset_id = str(asset_doc.get("_id", asset_doc.get("id", "")))
    scan_url = _reachable_scan_url("scan.scan_asset", asset_id)
    return generate_qr(scan_url, fill_color="#1565C0", back_color="white")


def generate_workstation_qr(ws_doc, emp_doc=None, assets=None):
    ws_id = str(ws_doc.get("_id", ws_doc.get("id", "")))
    scan_url = _reachable_scan_url("scan.scan_workstation", ws_id)
    return generate_qr(scan_url, fill_color="#00838F", back_color="white")

# =============================================================================
# QR Scan Routes
# =============================================================================
scan_bp = Blueprint("scan", __name__)

@scan_bp.route("/scan/asset/<record_id>")
def scan_asset(record_id):
    oid = safe_object_id(record_id)
    if not oid:
        abort(404)
    doc = mongo.db.assets.find_one({"_id": oid})
    if not doc:
        abort(404)

    emp = None
    if doc.get("assigned_to"):
        emp_oid = safe_object_id(doc["assigned_to"])
        if emp_oid:
            emp = mongo.db.employees.find_one({"_id": emp_oid})

    workstation_info = None
    if doc.get("workstation_id"):
        ws_oid = safe_object_id(doc["workstation_id"])
        if ws_oid:
            ws = mongo.db.workstations.find_one({"_id": ws_oid}, {"workstation_code": 1, "location": 1})
            if ws:
                workstation_info = f"{ws.get('workstation_code', '')} ({ws.get('location', '')})"

    return render_template("scan/result.html",
        type="Asset",
        tag=doc.get("asset_tag", ""),
        model=doc.get("model_name", ""),
        device_type=doc.get("device_type", ""),
        serial=doc.get("serial_number", ""),
        status=doc.get("status", ""),
        location=doc.get("location", ""),
        assigned_to=emp.get("full_name", "Unassigned") if emp else "Unassigned",
        employee_id=emp.get("employee_id", "") if emp else "",
        brand=doc.get("brand", ""),
        workstation_info=workstation_info,
        children=None,
    )


@scan_bp.route("/scan/workstation/<record_id>")
def scan_workstation(record_id):
    oid = safe_object_id(record_id)
    if not oid:
        abort(404)
    doc = mongo.db.workstations.find_one({"_id": oid})
    if not doc:
        abort(404)

    # Source of truth: assets actually linked to this workstation
    assets = list(mongo.db.assets.find(
        {"workstation_id": record_id},
        {"asset_tag": 1, "device_type": 1, "model_name": 1, "serial_number": 1},
    ))

    emp = None
    acc = mongo.db.accountabilities.find_one(
        {"workstation_id": {"$in": [oid, str(oid)]}, "status": "Active"},
    )
    if acc and acc.get("employee_id"):
        emp_oid = safe_object_id(acc["employee_id"])
        if emp_oid:
            emp = mongo.db.employees.find_one({"_id": emp_oid})

    return render_template("scan/result.html",
        type="Workstation",
        tag=doc.get("workstation_code", ""),
        model=doc.get("workstation_name", ""),
        device_type="Workstation",
        serial="",
        status=doc.get("status", ""),
        location=doc.get("location", ""),
        assigned_to=emp.get("full_name", "Unassigned") if emp else "Unassigned",
        employee_id=emp.get("employee_id", "") if emp else "",
        brand=doc.get("department", ""),
        workstation_info=None,
        children=assets,
    )


# =============================================================================
# Validation Helpers for Transfers
# =============================================================================
def validate_employee(employee_id):
    """Validate employee exists and is active."""
    oid = safe_object_id(employee_id)
    if not oid:
        return None
    emp = mongo.db.employees.find_one({"_id": oid})
    if not emp or emp.get("status") != "Active":
        return None
    return emp


def validate_workstation(workstation_id):
    """Validate workstation exists and is active."""
    oid = safe_object_id(workstation_id)
    if not oid:
        return None
    ws = mongo.db.workstations.find_one({"_id": oid})
    if not ws or ws.get("status") == "Archived":
        return None
    return ws


def get_workstation_employee(workstation_id):
    """Get the current employee assigned to a workstation."""
    acc = mongo.db.accountabilities.find_one({
        "workstation_id": workstation_id,
        "status": "Active"
    })
    if acc and acc.get("employee_id"):
        emp_oid = safe_object_id(acc["employee_id"])
        if emp_oid:
            return mongo.db.employees.find_one({"_id": emp_oid})
    return None


def create_accountability(employee_id, asset_ids, acc_type, notes=None, session=None):
    """Create a new accountability record.

    Runs on the given session when provided so multi-collection writes are atomic.
    """
    if not isinstance(asset_ids, list):
        asset_ids = [asset_ids]

    # Check if employee already has an active accountability
    existing = mongo.db.accountabilities.find_one({
        "employee_id": employee_id,
        "status": "Active"
    }, session=session)

    if existing:
        # Add to existing accountability
        mongo.db.accountabilities.update_one(
            {"_id": existing["_id"]},
            {
                "$addToSet": {"asset_ids": {"$each": asset_ids}},
                "$push": {"remarks_timeline": {
                    "text": f"Assets added from {acc_type}",
                    "by": current_user.username,
                    "date": datetime.utcnow().isoformat()
                }}
            },
            session=session
        )
        return existing["_id"]
    else:
        # Create new accountability
        doc = {
            "employee_id": employee_id,
            "asset_ids": asset_ids,
            "accountability_type": acc_type,
            "effective_date": datetime.utcnow(),
            "status": "Active",
            "notes": notes or "",
            "remarks_timeline": [{
                "text": f"Accountability created - {acc_type}",
                "by": current_user.username,
                "date": datetime.utcnow().isoformat()
            }],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow()
        }
        result = mongo.db.accountabilities.insert_one(doc, session=session)
        return result.inserted_id


def close_accountability_for_asset(asset_id, session=None):
    """Close any active accountability for this asset."""
    acc = mongo.db.accountabilities.find_one({
        "asset_ids": {"$in": [asset_id]},
        "status": "Active"
    }, session=session)
    if acc:
        mongo.db.accountabilities.update_one(
            {"_id": acc["_id"]},
            {
                "$set": {"updated_at": datetime.utcnow()},
                "$pull": {"asset_ids": asset_id},
                "$push": {"remarks_timeline": {
                    "text": f"Asset {asset_id} removed from accountability",
                    "by": current_user.username,
                    "date": datetime.utcnow().isoformat()
                }}
            },
            session=session
        )
        # If no assets left, close the accountability
        updated = mongo.db.accountabilities.find_one({"_id": acc["_id"]}, session=session)
        if not updated.get("asset_ids") or len(updated.get("asset_ids", [])) == 0:
            mongo.db.accountabilities.update_one(
                {"_id": acc["_id"]},
                {"$set": {"status": "Returned", "updated_at": datetime.utcnow()}},
                session=session
            )
        return True
    return False


def validate_asset_transfer(asset_id, target_type, target_id):
    """
    Comprehensive validation for any asset transfer.
    target_type: 'employee', 'workstation', 'stockroom'
    """
    errors = []
    warnings = []
    info = []

    # 1. Get the asset
    oid = safe_object_id(asset_id)
    if not oid:
        errors.append("Invalid asset ID")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info, "asset": None}

    asset = mongo.db.assets.find_one({"_id": oid})
    if not asset:
        errors.append("Asset not found")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info, "asset": None}

    # 2. Check asset status
    if asset.get("status") in ["Retired", "Disposed", "Lost"]:
        errors.append(f"Asset {asset['asset_tag']} is {asset['status']} and cannot be transferred.")

    if asset.get("status") == "Under Maintenance":
        errors.append(f"Asset {asset['asset_tag']} is under maintenance. Transfer not allowed.")
        return {"valid": False, "errors": errors, "warnings": warnings, "info": info, "asset": asset}

    # 3. Check if asset is already assigned
    current_assigned_to = asset.get("assigned_to")
    current_ws = asset.get("workstation_id")

    if target_type == "employee" and current_assigned_to and str(current_assigned_to) == target_id:
        info.append("Asset is already assigned to this employee.")
        return {"valid": True, "errors": errors, "warnings": warnings, "info": info, "asset": asset}

    if target_type == "workstation" and current_ws and str(current_ws) == target_id:
        info.append("Asset is already at this workstation.")
        return {"valid": True, "errors": errors, "warnings": warnings, "info": info, "asset": asset}

    if current_assigned_to or current_ws:
        warnings.append("Asset is currently assigned. It will be reassigned.")

    # 4. Check target validity
    if target_type == "employee":
        emp = validate_employee(target_id)
        if not emp:
            errors.append("Target employee not found or inactive.")
        else:
            # Check if employee already has similar asset
            similar_assets = mongo.db.assets.count_documents({
                "assigned_to": target_id,
                "device_type": asset.get("device_type"),
                "status": "Assigned"
            })

            # Business rules
            if asset.get("device_type") == "Laptop" and similar_assets >= 1:
                warnings.append(f"Employee {emp['full_name']} already has a laptop assigned.")

            if asset.get("device_type") == "Monitor" and similar_assets >= 2:
                warnings.append(f"Employee {emp['full_name']} already has 2 monitors assigned.")

    elif target_type == "workstation":
        ws = validate_workstation(target_id)
        if not ws:
            errors.append("Target workstation not found or inactive.")
        else:
            # Check if workstation already has this asset type
            same_type_count = mongo.db.assets.count_documents({
                "workstation_id": target_id,
                "device_type": asset.get("device_type"),
                "status": {"$in": ["Assigned", "Available"]}
            })

            if same_type_count >= 1:
                warnings.append(f"Workstation already has a {asset.get('device_type')}.")

    elif target_type == "stockroom":
        # Stockroom can always accept assets
        pass
    else:
        errors.append(f"Unknown target type: {target_type}")

    # 5. Check for active accountability
    if asset.get("assigned_to"):
        active_acc = mongo.db.accountabilities.find_one({
            "asset_ids": {"$in": [asset_id]},
            "status": "Active"
        })
        if active_acc:
            warnings.append("Asset has active accountability. It will be updated during transfer.")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "asset": asset
    }


def perform_batch_transfer(asset_ids, target_type, target_id, reason, notes):
    """Transfer a set of pre-validated assets to an employee or a workstation.

    Used by the selectable batch-transfer pages (from a workstation or from an
    employee). Each asset follows the same rules as a single transfer: the old
    accountability is unwound per asset (closing it when empty), the asset is
    relinked, and transfer history is recorded. asset_ids must already have
    passed validate_asset_transfer().
    """
    if target_type == "employee":
        target = validate_employee(target_id)
        if not target:
            return {"ok": False, "error": "Invalid employee selected."}
        target_emp = None
    elif target_type == "workstation":
        target = validate_workstation(target_id)
        if not target:
            return {"ok": False, "error": "Invalid workstation selected."}
        target_emp = get_workstation_employee(target_id)
    else:
        return {"ok": False, "error": "Unknown transfer target."}

    def _do(session):
        for aid in asset_ids:
            current = mongo.db.assets.find_one({"_id": safe_object_id(aid)}, session=session)
            if not current:
                continue
            old_ws = current.get("workstation_id")
            old_emp = current.get("assigned_to")
            update = {"updated_at": datetime.utcnow()}
            if target_type == "employee":
                update["assigned_to"] = target_id
                update["status"] = "Assigned"
            else:
                update["workstation_id"] = target_id
                if target_emp:
                    update["assigned_to"] = str(target_emp["_id"])
                    update["status"] = "Assigned"
                else:
                    update["assigned_to"] = None
                    update["status"] = "Available"
                if old_ws and str(old_ws) != target_id:
                    mongo.db.workstations.update_one(
                        {"_id": ObjectId(old_ws)}, {"$pull": {"assets": aid}}, session=session)
                mongo.db.workstations.update_one(
                    {"_id": ObjectId(target_id)}, {"$addToSet": {"assets": aid}}, session=session)
            if old_emp or old_ws:
                close_accountability_for_asset(aid, session=session)
            mongo.db.assets.update_one(
                {"_id": current["_id"]},
                {
                    "$set": update,
                    "$push": {"history": {
                        "type": "Batch Transfer",
                        "from_employee": old_emp,
                        "to_employee": update.get("assigned_to"),
                        "from_workstation": old_ws,
                        "to_workstation": update.get("workstation_id"),
                        "reason": reason,
                        "notes": notes,
                        "date": datetime.utcnow().isoformat(),
                        "by": current_user.username
                    }}
                },
                session=session
            )
        return asset_ids

    _run_in_transaction(_do)

    if target_type == "employee":
        create_accountability(target_id, asset_ids, "Batch Transfer", notes)
    elif target_emp:
        create_accountability(str(target_emp["_id"]), asset_ids, "Batch Transfer", notes)

    return {"ok": True, "count": len(asset_ids), "target": target}


# =============================================================================
# User model
# =============================================================================
class User(UserMixin):
    def __init__(self, user_doc):
        self.id = str(user_doc["_id"])
        self.username = user_doc["username"]
        self.email = user_doc.get("email", "")
        self.role = user_doc.get("role", "viewer")
        self.full_name = user_doc.get("full_name", "")
        self.is_active_user = user_doc.get("is_active", True)

    def get_id(self):
        return self.id

    @property
    def is_active(self):
        return self.is_active_user

    def is_admin(self):
        return self.role in ("admin", "superadmin")

    def can_edit(self):
        return self.role in ("admin", "superadmin", "editor")


@login_manager.user_loader
def load_user(user_id):
    oid = safe_object_id(user_id)
    if oid is None:
        return None
    doc = mongo.db.users.find_one({"_id": oid})
    return User(doc) if doc else None


# =============================================================================
# WTForms
# =============================================================================
class OptionalDateField(DateField):
    """DateField that gracefully accepts empty strings without raising ValueError."""

    def process_formdata(self, valuelist):
        if valuelist and valuelist[0].strip():
            super().process_formdata(valuelist)
        else:
            self.data = None

    def _value(self):
        if self.data:
            return self.data.strftime(self.format[0])
        return ""


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Remember Me")


class EmployeeForm(FlaskForm):
    employee_id = StringField("Employee ID", validators=[DataRequired(), Length(max=30)])
    full_name = StringField("Full Name", validators=[DataRequired(), Length(max=100)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    department = StringField("Department", validators=[DataRequired()])
    position = StringField("Position", validators=[DataRequired()])
    site = StringField("Site / Location", validators=[DataRequired()])
    contact_number = StringField("Contact Number", validators=[Optional()])
    date_hired = OptionalDateField("Date Hired", validators=[Optional()])
    status = SelectField("Status", choices=[
        ("Active", "Active"), ("Inactive", "Inactive"),
        ("Resigned", "Resigned"), ("On Leave", "On Leave")
    ])
    notes = TextAreaField("Notes", validators=[Optional()])


class AssetForm(FlaskForm):
    asset_tag = StringField("Asset Tag", validators=[DataRequired(), Length(max=50)])
    endpoint_name = StringField("Endpoint Name / Hostname", validators=[Optional()])
    serial_number = StringField("Serial Number", validators=[DataRequired(), Length(max=100)])
    device_type = SelectField("Device Type", choices=[
        ("Laptop", "Laptop"), ("Desktop", "Desktop"), ("Monitor", "Monitor"),
        ("Printer", "Printer"), ("Phone", "Phone"), ("Tablet", "Tablet"),
        ("Network Equipment", "Network Equipment"), ("Peripheral", "Peripheral"),
        ("Server", "Server"), ("Other", "Other")
    ])
    model_name = StringField("Model Name", validators=[DataRequired()])
    manufacturer = StringField("Manufacturer", validators=[Optional()])
    os_version = StringField("OS Version", validators=[Optional()])
    cpu = StringField("CPU", validators=[Optional()])
    ram = StringField("RAM", validators=[Optional()])
    storage = StringField("Storage", validators=[Optional()])
    purchase_date = OptionalDateField("Purchase Date", validators=[Optional()])
    warranty_expiry = OptionalDateField("Warranty Expiry", validators=[Optional()])
    purchase_cost = StringField("Purchase Cost", validators=[Optional()])
    vendor = StringField("Vendor", validators=[Optional()])
    location = StringField("Location", validators=[Optional()])
    status = SelectField("Status", choices=[
        ("Available", "Available"), ("Assigned", "Assigned"),
        ("Under Maintenance", "Under Maintenance"), ("Retired", "Retired"),
        ("Disposed", "Disposed"), ("Lost", "Lost")
    ])
    notes = TextAreaField("Notes", validators=[Optional()])


class WorkstationForm(FlaskForm):
    workstation_code = StringField("Workstation Code", validators=[DataRequired(), Length(max=50)])
    workstation_name = StringField("Workstation Name", validators=[Optional()])
    location = StringField("Location / Site", validators=[DataRequired()])
    floor_area = StringField("Floor / Area", validators=[Optional()])
    department = StringField("Department", validators=[Optional()])
    status = SelectField("Status", choices=[
        ("Active", "Active"), ("Inactive", "Inactive"),
        ("Under Maintenance", "Under Maintenance"), ("Archived", "Archived")
    ])
    notes = TextAreaField("Notes", validators=[Optional()])


class AccountabilityForm(FlaskForm):
    employee_id = HiddenField("Employee ID", validators=[DataRequired()])
    workstation_id = HiddenField("Workstation ID", validators=[Optional()])
    asset_ids = HiddenField("Asset IDs (JSON)", validators=[Optional()])
    accountability_type = SelectField("Type", choices=[
        ("Onboarding", "Onboarding"), ("Transfer", "Transfer"),
        ("Return", "Return"), ("Resignation", "Resignation")
    ])
    effective_date = OptionalDateField("Effective Date", validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional()])
    send_email = BooleanField("Email receive link to employee")


class RemarkForm(FlaskForm):
    record_id = HiddenField("Record ID", validators=[DataRequired()])
    record_type = HiddenField("Record Type", validators=[DataRequired()])
    remark = TextAreaField("Remark", validators=[DataRequired(), Length(max=1000)])


class AuditForm(FlaskForm):
    audit_type = SelectField("Audit Type", choices=[
        ("Physical Inventory", "Physical Inventory"),
        ("Accountability Audit", "Accountability Audit"),
        ("Spot Check", "Spot Check"),
        ("Annual Audit", "Annual Audit"),
    ])
    asset_id = HiddenField("Asset ID", validators=[Optional()])
    workstation_id = HiddenField("Workstation ID", validators=[Optional()])
    result = SelectField("Result", choices=[
        ("Pass", "Pass"), ("Fail", "Fail"), ("Partial", "Partial")
    ])
    findings = TextAreaField("Findings", validators=[Optional()])
    audit_date = OptionalDateField("Audit Date", validators=[DataRequired()])


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=50)])
    full_name = StringField("Full Name", validators=[DataRequired()])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[Optional(), Length(min=8)])
    role = SelectField("Role", choices=[
        ("viewer", "Viewer"), ("editor", "Editor"), ("admin", "Admin")
    ])
    is_active = BooleanField("Active", default=True)


# =============================================================================
# Blueprint: auth
# =============================================================================
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/", methods=["GET", "POST"])
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard.index"))
    form = LoginForm()
    ip = get_client_ip()
    if request.method == "POST" and is_login_limited(ip):
        flash("Too many failed login attempts. Please try again later.", "error")
        return render_template("auth/login.html", form=form), 429
    if form.validate_on_submit():
        user_doc = mongo.db.users.find_one({"username": form.username.data, "is_active": True})
        if user_doc and bcrypt.checkpw(form.password.data.encode(), user_doc["password"]):
            clear_login_failures(ip)
            user = User(user_doc)
            login_user(user, remember=form.remember.data)
            mongo.db.users.update_one({"_id": user_doc["_id"]}, {"$set": {"last_login": datetime.utcnow()}})
            audit_log("Auth", "Login")
            next_page = request.args.get("next")
            if next_page and next_page.startswith("/") and not next_page.startswith("//"):
                return redirect(next_page)
            return redirect(url_for("dashboard.index"))
        record_login_failure(ip)
        flash("Invalid credentials. Please try again.", "error")
    return render_template("auth/login.html", form=form)


@auth_bp.route("/logout")
@login_required
def logout():
    audit_log("Auth", "Logout")
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("auth.login"))


# =============================================================================
# Blueprint: dashboard
# =============================================================================
dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/dashboard")
@login_required
def index():
    cached = get_dashboard_cache("index", ttl_seconds=30)
    if cached is not None:
        return render_template("dashboard/index.html", **cached)

    now = datetime.utcnow()
    warranty_threshold = now + timedelta(days=90)

    stats = {
        "total_assets": mongo.db.assets.count_documents({"status": {"$nin": ["Disposed", "Retired"]}}),
        "assigned_assets": mongo.db.assets.count_documents({"status": "Assigned"}),
        "available_assets": mongo.db.assets.count_documents({"status": "Available"}),
        "total_workstations": mongo.db.workstations.count_documents({"status": {"$ne": "Archived"}}),
        "total_employees": mongo.db.employees.count_documents({"status": "Active"}),
        "active_accountabilities": mongo.db.accountabilities.count_documents({"status": "Active"}),
        "warranty_alerts": mongo.db.assets.count_documents({
            "warranty_expiry": {"$lte": warranty_threshold, "$gte": now},
            "status": {"$nin": ["Disposed", "Retired"]}
        }),
        "under_maintenance": mongo.db.assets.count_documents({"status": "Under Maintenance"}),
    }

    by_location = list(mongo.db.assets.aggregate([
        {"$match": {"status": {"$nin": ["Disposed", "Retired"]}}},
        {"$group": {"_id": "$location", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8}
    ]))
    by_type = list(mongo.db.assets.aggregate([
        {"$match": {"status": {"$nin": ["Disposed", "Retired"]}}},
        {"$group": {"_id": "$device_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}}
    ]))
    accountability_status = list(mongo.db.accountabilities.aggregate([
        {"$group": {"_id": "$status", "count": {"$sum": 1}}}
    ]))
    audit_results = list(mongo.db.audits.aggregate([
        {"$group": {"_id": "$result", "count": {"$sum": 1}}}
    ]))

    recent_logs = list(mongo.db.audit_logs.find().sort("timestamp", -1).limit(10))
    warnings = list(mongo.db.assets.find({
        "warranty_expiry": {"$lte": warranty_threshold, "$gte": now}
    }).sort("warranty_expiry", 1).limit(5))

    payload = {
        "stats": stats,
        "by_location": json.dumps([{"label": d["_id"] or "Unknown", "value": d["count"]} for d in by_location]),
        "by_type": json.dumps([{"label": d["_id"] or "Unknown", "value": d["count"]} for d in by_type]),
        "accountability_status": json.dumps([{"label": d["_id"], "value": d["count"]} for d in accountability_status]),
        "audit_results": json.dumps([{"label": d["_id"], "value": d["count"]} for d in audit_results]),
        "recent_logs": [serialize_doc(l) for l in recent_logs],
        "warnings": [serialize_doc(w) for w in warnings],
    }
    set_dashboard_cache("index", payload)
    return render_template("dashboard/index.html", **payload)


@dashboard_bp.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return render_template("dashboard/search.html", q="", results={})

    regex = qre(q)
    assets = list(mongo.db.assets.find({"$or": [
        {"asset_tag": regex}, {"serial_number": regex},
        {"endpoint_name": regex}, {"model_name": regex}
    ]}).limit(20))
    employees = list(mongo.db.employees.find({"$or": [
        {"full_name": regex}, {"employee_id": regex}, {"email": regex}
    ]}).limit(20))
    workstations = list(mongo.db.workstations.find({"$or": [
        {"workstation_code": regex}, {"workstation_name": regex}
    ]}).limit(20))

    results = {
        "assets": [serialize_doc(a) for a in assets],
        "employees": [serialize_doc(e) for e in employees],
        "workstations": [serialize_doc(w) for w in workstations],
    }
    return render_template("dashboard/search.html", q=q, results=results)


# =============================================================================
# Blueprint: employees
# =============================================================================
employees_bp = Blueprint("employees", __name__, url_prefix="/employees")


@employees_bp.route("")
@login_required
def list_view():
    q = request.args.get("q", "")
    status_filter = request.args.get("status", "")
    query = {}
    if q:
        query["$or"] = [
            {"full_name": qre(q)},
            {"employee_id": qre(q)},
            {"email": qre(q)},
            {"department": qre(q)},
        ]
    if status_filter:
        query["status"] = status_filter
    page = request.args.get("page", 1, type=int)
    employees, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.employees, "full_name", 1, page=page
    )
    return render_template("employees/list.html", employees=[serialize_doc(e) for e in employees],
                            q=q, status_filter=status_filter, page=page, total=total,
                            per_page=per_page, total_pages=total_pages)


@employees_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = EmployeeForm()
    if form.validate_on_submit():
        existing = mongo.db.employees.find_one({"employee_id": form.employee_id.data})
        if existing:
            flash("Employee ID already exists.", "error")
            return render_template("employees/form.html", form=form, title="New Employee")
        doc = {
            "employee_id": form.employee_id.data,
            "full_name": form.full_name.data,
            "email": form.email.data,
            "department": form.department.data,
            "position": form.position.data,
            "site": form.site.data,
            "contact_number": form.contact_number.data,
            "date_hired": to_datetime(form.date_hired.data),
            "status": form.status.data,
            "notes": form.notes.data,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = mongo.db.employees.insert_one(doc)
        audit_log("Employees", "Create",
                   new_value={"employee_id": doc["employee_id"], "full_name": doc["full_name"]},
                   record_id=result.inserted_id)
        clear_reference_data("active_employees")
        flash(f"Employee {form.full_name.data} created successfully.", "success")
        return redirect(url_for("employees.list_view"))
    return render_template("employees/form.html", form=form, title="New Employee")


@employees_bp.route("/<employee_id>")
@login_required
def detail(employee_id):
    emp = get_or_404("employees", employee_id)
    accountabilities = list(mongo.db.accountabilities.find({"employee_id": employee_id}).sort("created_at", -1))
    assets = list(mongo.db.assets.find({"assigned_to": employee_id}))
    remarks = list(mongo.db.remarks.find({"record_id": employee_id}).sort("created_at", -1))

    # Workstation (fixed) assets the employee is accountable for: everything on
    # the workstation(s) covered by their active accountability records.
    ws_ids = {a["workstation_id"] for a in accountabilities
              if a.get("status") == "Active" and a.get("workstation_id")}
    ws_assets = []
    ws_names = {}
    if ws_ids:
        for ws in mongo.db.workstations.find({"_id": {"$in": list(ws_ids)}}, {"workstation_code": 1, "workstation_name": 1, "location": 1}):
            ws_names[str(ws["_id"])] = ws.get("workstation_name") or ws.get("workstation_code") or ""
        ws_assets = list(mongo.db.assets.find(
            {"workstation_id": {"$in": list(ws_ids)}}, {"asset_tag": 1, "device_type": 1,
                                                       "model_name": 1, "serial_number": 1,
                                                       "status": 1})
        )
    for a in assets:
        a["workstation_name"] = ws_names.get(str(a.get("workstation_id")), "")
    ws_assets.sort(key=lambda a: a.get("workstation_name", ""))

    return render_template("employees/detail.html",
                            emp=serialize_doc(emp),
                            accountabilities=[serialize_doc(a) for a in accountabilities],
                            assets=[serialize_doc(a) for a in assets],
                            ws_assets=[serialize_doc(a) for a in ws_assets],
                            remarks=[serialize_doc(r) for r in remarks])


@employees_bp.route("/<employee_id>/transfer-assets", methods=["GET", "POST"])
@login_required
@editor_required
def transfer_assets(employee_id):
    """Transfer selected assets assigned to an employee to another employee or workstation."""
    emp = get_or_404("employees", employee_id)
    source = {
        "kind": "employee",
        "label": emp["full_name"],
        "assets_query": {"assigned_to": employee_id, "status": "Assigned"},
        "detail_url": url_for("employees.detail", employee_id=employee_id),
        "back_label": emp["full_name"]
    }
    return _transfer_batch(source, emp)


@employees_bp.route("/<employee_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(employee_id):
    emp = get_or_404("employees", employee_id)
    form = EmployeeForm(data={
        "employee_id": emp.get("employee_id"),
        "full_name": emp.get("full_name"),
        "email": emp.get("email"),
        "department": emp.get("department"),
        "position": emp.get("position"),
        "site": emp.get("site"),
        "contact_number": emp.get("contact_number"),
        "status": emp.get("status", "Active"),
        "notes": emp.get("notes"),
    })
    if form.validate_on_submit():
        old = serialize_doc(emp)
        update = {
            "full_name": form.full_name.data,
            "email": form.email.data,
            "department": form.department.data,
            "position": form.position.data,
            "site": form.site.data,
            "contact_number": form.contact_number.data,
            "date_hired": to_datetime(form.date_hired.data),
            "status": form.status.data,
            "notes": form.notes.data,
            "updated_at": datetime.utcnow(),
        }
        mongo.db.employees.update_one({"_id": emp["_id"]}, {"$set": update})
        audit_log("Employees", "Update", old_value=old, new_value=update, record_id=emp["_id"])
        clear_reference_data("active_employees")
        flash("Employee updated successfully.", "success")
        return redirect(url_for("employees.detail", employee_id=employee_id))
    return render_template("employees/form.html", form=form, title="Edit Employee", emp=serialize_doc(emp))


@employees_bp.route("/<employee_id>/archive", methods=["POST"])
@login_required
@editor_required
def archive(employee_id):
    emp = get_or_404("employees", employee_id)
    # Do not deactivate an employee who still holds active accountabilities.
    active_acc = mongo.db.accountabilities.find_one(
        {"employee_id": employee_id, "status": "Active"})
    if active_acc:
        flash("Cannot archive: this employee still has an active accountability.", "error")
        return redirect(url_for("employees.list_view"))
    mongo.db.employees.update_one({"_id": emp["_id"]},
                                   {"$set": {"status": "Inactive", "updated_at": datetime.utcnow()}})
    audit_log("Employees", "Archive", record_id=emp["_id"])
    clear_reference_data("active_employees")
    flash("Employee archived.", "info")
    return redirect(url_for("employees.list_view"))


# =============================================================================
# Blueprint: assets (with full transfer functionality)
# =============================================================================
assets_bp = Blueprint("assets", __name__, url_prefix="/assets")


@assets_bp.route("")
@login_required
def list_view():
    q = request.args.get("q", "")
    status_filter = request.args.get("status", "")
    type_filter = request.args.get("type", "")
    query = {}
    if q:
        query["$or"] = [
            {"asset_tag": qre(q)},
            {"serial_number": qre(q)},
            {"endpoint_name": qre(q)},
            {"model_name": qre(q)},
        ]
    if status_filter:
        query["status"] = status_filter
    if type_filter:
        query["device_type"] = type_filter

    page = request.args.get("page", 1, type=int)
    assets, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.assets, "asset_tag", 1, page=page
    )

    # Batch-fetch assigned employees
    employee_ids = {safe_object_id(a["assigned_to"]) for a in assets if a.get("assigned_to")}
    employee_ids.discard(None)
    employees_by_id = {}
    if employee_ids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(employee_ids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "")

    for a in assets:
        a["employee_name"] = employees_by_id.get(a.get("assigned_to"), "")

    return render_template("assets/list.html", assets=[serialize_doc(a) for a in assets],
                            q=q, status_filter=status_filter, type_filter=type_filter,
                            page=page, total=total, per_page=per_page, total_pages=total_pages)


@assets_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = AssetForm()
    if form.validate_on_submit():
        existing = mongo.db.assets.find_one({"serial_number": form.serial_number.data})
        if existing:
            flash("An asset with this serial number already exists.", "error")
            return render_template("assets/form.html", form=form, title="New Asset")
        doc = {
            "asset_tag": form.asset_tag.data,
            "endpoint_name": form.endpoint_name.data,
            "serial_number": form.serial_number.data,
            "device_type": form.device_type.data,
            "model_name": form.model_name.data,
            "manufacturer": form.manufacturer.data,
            "os_version": form.os_version.data,
            "cpu": form.cpu.data,
            "ram": form.ram.data,
            "storage": form.storage.data,
            "purchase_date": to_datetime(form.purchase_date.data),
            "warranty_expiry": to_datetime(form.warranty_expiry.data),
            "purchase_cost": form.purchase_cost.data,
            "vendor": form.vendor.data,
            "location": form.location.data,
            "status": form.status.data,
            "notes": form.notes.data,
            "assigned_to": None,
            "workstation_id": None,
            "history": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = mongo.db.assets.insert_one(doc)
        audit_log("Assets", "Create",
                   new_value={"asset_tag": doc["asset_tag"], "serial_number": doc["serial_number"]},
                   record_id=result.inserted_id)
        flash(f"Asset {form.asset_tag.data} created successfully.", "success")
        return redirect(url_for("assets.list_view"))
    return render_template("assets/form.html", form=form, title="New Asset")


@assets_bp.route("/<asset_id>")
@login_required
def detail(asset_id):
    asset = get_or_404("assets", asset_id)
    emp = None
    emp_oid = safe_object_id(asset.get("assigned_to"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})
    ws = None
    ws_oid = safe_object_id(asset.get("workstation_id"))
    if ws_oid:
        ws = mongo.db.workstations.find_one({"_id": ws_oid})
    remarks = list(mongo.db.remarks.find({"record_id": asset_id}).sort("created_at", -1))
    audits = list(mongo.db.audits.find({"asset_id": asset_id}).sort("audit_date", -1))
    qr_b64 = generate_asset_qr(asset)

    # Available employees and workstations for transfer modals
    available_employees = get_active_employees()
    available_workstations = get_active_workstations()

    return render_template("assets/detail.html",
                            asset=serialize_doc(asset),
                            emp=serialize_doc(emp) if emp else None,
                            ws=serialize_doc(ws) if ws else None,
                            remarks=[serialize_doc(r) for r in remarks],
                            audits=[serialize_doc(a) for a in audits],
                            qr_b64=qr_b64,
                            available_employees=[serialize_doc(e) for e in available_employees],
                            available_workstations=[serialize_doc(w) for w in available_workstations])


@assets_bp.route("/<asset_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(asset_id):
    asset = get_or_404("assets", asset_id)
    form = AssetForm(data={k: v for k, v in asset.items() if k != "_id"})
    if form.validate_on_submit():
        old = serialize_doc(asset)
        update = {
            "asset_tag": form.asset_tag.data,
            "endpoint_name": form.endpoint_name.data,
            "serial_number": form.serial_number.data,
            "device_type": form.device_type.data,
            "model_name": form.model_name.data,
            "manufacturer": form.manufacturer.data,
            "os_version": form.os_version.data,
            "cpu": form.cpu.data,
            "ram": form.ram.data,
            "storage": form.storage.data,
            "purchase_date": to_datetime(form.purchase_date.data),
            "warranty_expiry": to_datetime(form.warranty_expiry.data),
            "purchase_cost": form.purchase_cost.data,
            "vendor": form.vendor.data,
            "location": form.location.data,
            "status": form.status.data,
            "notes": form.notes.data,
            "updated_at": datetime.utcnow(),
        }
        mongo.db.assets.update_one({"_id": asset["_id"]}, {"$set": update})
        audit_log("Assets", "Update", old_value=old, new_value=update, record_id=asset["_id"])
        flash("Asset updated.", "success")
        return redirect(url_for("assets.detail", asset_id=asset_id))
    return render_template("assets/form.html", form=form, title="Edit Asset", asset=serialize_doc(asset))


@assets_bp.route("/<asset_id>/archive", methods=["POST"])
@login_required
@editor_required
def archive(asset_id):
    asset = get_or_404("assets", asset_id)
    reason = request.form.get("reason", "Retired")
    mongo.db.assets.update_one({"_id": asset["_id"]},
                                {"$set": {"status": reason, "updated_at": datetime.utcnow()}})
    audit_log("Assets", "Archive", record_id=asset["_id"])
    flash(f"Asset marked as {reason}.", "info")
    return redirect(url_for("assets.list_view"))


@assets_bp.route("/<asset_id>/transfer", methods=["GET", "POST"])
@login_required
@editor_required
def transfer(asset_id):
    asset = get_or_404("assets", asset_id)

    # GET: Show transfer form
    if request.method == "GET":
        current_employee = None
        current_workstation = None

        if asset.get("assigned_to"):
            emp_oid = safe_object_id(asset["assigned_to"])
            if emp_oid:
                current_employee = mongo.db.employees.find_one({"_id": emp_oid})

        if asset.get("workstation_id"):
            ws_oid = safe_object_id(asset["workstation_id"])
            if ws_oid:
                current_workstation = mongo.db.workstations.find_one({"_id": ws_oid})

        available_employees = get_active_employees()
        available_workstations = get_active_workstations()

        validation_errors = []
        if asset.get("status") in ["Retired", "Disposed", "Lost"]:
            validation_errors.append(f"Asset is {asset['status']} - transfers are not allowed.")
        elif asset.get("status") == "Under Maintenance":
            validation_errors.append("Asset is under maintenance - transfer not recommended.")

        return render_template("assets/transfer.html",
            asset=serialize_doc(asset),
            current_employee=serialize_doc(current_employee),
            current_workstation=serialize_doc(current_workstation),
            available_employees=[serialize_doc(e) for e in available_employees],
            available_workstations=[serialize_doc(w) for w in available_workstations],
            validation_errors=validation_errors
        )

    # POST: Process transfer
    target_type = request.form.get("target_type")
    target_id = request.form.get("target_id")
    reason = request.form.get("reason", "Transfer")
    notes = request.form.get("notes", "")

    # Validate
    validation = validate_asset_transfer(asset_id, target_type, target_id)

    if not validation["valid"]:
        for error in validation["errors"]:
            flash(error, "error")
        return redirect(url_for("assets.transfer", asset_id=asset_id))

    if validation["warnings"]:
        for warning in validation["warnings"]:
            flash(warning, "warning")

    # Process transfer
    asset_doc = validation["asset"]
    old_assigned_to = asset_doc.get("assigned_to")
    old_workstation = asset_doc.get("workstation_id")

    # Resolve targets up-front (validated by validate_asset_transfer above).
    target_emp = None
    target_ws = None
    if target_type == "employee":
        target_emp = validate_employee(target_id)
        if not target_emp:
            flash("Invalid employee", "error")
            return redirect(url_for("assets.transfer", asset_id=asset_id))
    elif target_type == "workstation":
        target_ws = validate_workstation(target_id)
        if not target_ws:
            flash("Invalid workstation", "error")
            return redirect(url_for("assets.transfer", asset_id=asset_id))
    else:
        target_type = "stockroom"

    def _do_transfer(session):
        update = {"updated_at": datetime.utcnow()}

        if target_type == "stockroom":
            update["status"] = "Available"
            update["assigned_to"] = None
            update["workstation_id"] = None
            if old_assigned_to:
                close_accountability_for_asset(asset_id, session=session)

        elif target_type == "employee":
            update["status"] = "Assigned"
            update["assigned_to"] = target_id
            if old_workstation:
                update["workstation_id"] = None  # Remove from workstation
            create_accountability(target_id, asset_id, "Direct Assignment", notes, session=session)
            if old_workstation:
                mongo.db.workstations.update_one(
                    {"_id": ObjectId(old_workstation)},
                    {"$pull": {"assets": asset_id}},
                    session=session
                )

        elif target_type == "workstation":
            update["workstation_id"] = target_id
            ws_employee = get_workstation_employee(target_id) if target_ws else None
            if ws_employee:
                update["assigned_to"] = str(ws_employee["_id"])
                update["status"] = "Assigned"
                create_accountability(str(ws_employee["_id"]), asset_id,
                                      "Workstation Assignment", notes, session=session)
            else:
                update["assigned_to"] = None
                update["status"] = "Available"
            mongo.db.workstations.update_one(
                {"_id": ObjectId(target_id)},
                {"$addToSet": {"assets": asset_id}},
                session=session
            )
            if old_assigned_to:
                close_accountability_for_asset(asset_id, session=session)

        # Add transfer history
        mongo.db.assets.update_one(
            {"_id": asset["_id"]},
            {
                "$set": update,
                "$push": {
                    "history": {
                        "type": "Transfer",
                        "from_employee": old_assigned_to,
                        "to_employee": update.get("assigned_to"),
                        "from_workstation": old_workstation,
                        "to_workstation": update.get("workstation_id"),
                        "reason": reason,
                        "notes": notes,
                        "date": datetime.utcnow().isoformat(),
                        "by": current_user.username
                    }
                }
            },
            session=session
        )
        return {"assigned_to": update.get("assigned_to"),
                "workstation_id": update.get("workstation_id")}

    result = _run_in_transaction(_do_transfer)

    audit_log("Assets", "Transfer",
        old_value={"assigned_to": old_assigned_to, "workstation_id": old_workstation},
        new_value={"assigned_to": result.get("assigned_to"), "workstation_id": result.get("workstation_id")},
        record_id=asset["_id"]
    )

    flash(f"Asset {asset['asset_tag']} transferred successfully.", "success")
    return redirect(url_for("assets.detail", asset_id=asset_id))


# =============================================================================
# Blueprint: workstations (with batch transfer)
# =============================================================================
workstations_bp = Blueprint("workstations", __name__, url_prefix="/workstations")


@workstations_bp.route("")
@login_required
def list_view():
    q = request.args.get("q", "")
    query = {}
    if q:
        query["$or"] = [
            {"workstation_code": qre(q)},
            {"workstation_name": qre(q)},
            {"location": qre(q)},
        ]
    page = request.args.get("page", 1, type=int)
    workstations, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.workstations, "workstation_code", 1, page=page
    )

    ws_ids = [str(w["_id"]) for w in workstations]
    counts = {}
    if ws_ids:
        for row in mongo.db.assets.aggregate([
            {"$match": {"workstation_id": {"$in": ws_ids}}},
            {"$group": {"_id": "$workstation_id", "count": {"$sum": 1}}},
        ]):
            counts[row["_id"]] = row["count"]
    for w in workstations:
        w["asset_count"] = counts.get(str(w["_id"]), 0)

    return render_template("workstations/list.html", workstations=[serialize_doc(w) for w in workstations],
                            q=q, page=page, total=total, per_page=per_page, total_pages=total_pages)


@workstations_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = WorkstationForm()
    if form.validate_on_submit():
        existing = mongo.db.workstations.find_one({"workstation_code": form.workstation_code.data})
        if existing:
            flash("Workstation code already exists.", "error")
            return render_template("workstations/form.html", form=form, title="New Workstation")
        doc = {
            "workstation_code": form.workstation_code.data,
            "workstation_name": form.workstation_name.data,
            "location": form.location.data,
            "floor_area": form.floor_area.data,
            "department": form.department.data,
            "status": form.status.data,
            "notes": form.notes.data,
            "assets": [],  # List of asset IDs
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = mongo.db.workstations.insert_one(doc)
        audit_log("Workstations", "Create", new_value={"workstation_code": doc["workstation_code"]},
                   record_id=result.inserted_id)
        clear_reference_data("active_workstations")
        flash(f"Workstation {form.workstation_code.data} created.", "success")
        return redirect(url_for("workstations.list_view"))
    return render_template("workstations/form.html", form=form, title="New Workstation")


@workstations_bp.route("/<ws_id>")
@login_required
def detail(ws_id):
    ws = get_or_404("workstations", ws_id)
    assets = list(mongo.db.assets.find({"workstation_id": ws_id}))

    employee_ids = {safe_object_id(a["assigned_to"]) for a in assets if a.get("assigned_to")}
    employee_ids.discard(None)
    employees_by_id = {}
    if employee_ids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(employee_ids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "")
    for a in assets:
        a["employee_name"] = employees_by_id.get(a.get("assigned_to"), "")

    remarks = list(mongo.db.remarks.find({"record_id": ws_id}).sort("created_at", -1))
    audits = list(mongo.db.audits.find({"workstation_id": ws_id}).sort("audit_date", -1))

    acc = mongo.db.accountabilities.find_one({"workstation_id": ws_id, "status": "Active"})
    emp = None
    employee_assets = []
    if acc:
        emp_oid = safe_object_id(acc.get("employee_id"))
        if emp_oid:
            emp = mongo.db.employees.find_one({"_id": emp_oid})
            employee_assets = list(mongo.db.assets.find({"assigned_to": str(emp_oid)}))

    qr_b64 = generate_workstation_qr(ws, emp, assets)
    all_employees = get_active_employees()
    available_assets = list(mongo.db.assets.find({"status": "Available"}).sort("asset_tag", 1))

    return render_template("workstations/detail.html",
                            ws=serialize_doc(ws),
                            assets=[serialize_doc(a) for a in assets],
                            employee_assets=[serialize_doc(a) for a in employee_assets],
                            remarks=[serialize_doc(r) for r in remarks],
                            audits=[serialize_doc(a) for a in audits],
                            acc=serialize_doc(acc) if acc else None,
                            emp=serialize_doc(emp) if emp else None,
                            qr_b64=qr_b64,
                            all_employees=[serialize_doc(e) for e in all_employees],
                            available_assets=[serialize_doc(a) for a in available_assets])


@workstations_bp.route("/<ws_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(ws_id):
    ws = get_or_404("workstations", ws_id)
    form = WorkstationForm(data={k: v for k, v in ws.items() if k != "_id"})
    if form.validate_on_submit():
        old = serialize_doc(ws)
        update = {
            "workstation_code": form.workstation_code.data,
            "workstation_name": form.workstation_name.data,
            "location": form.location.data,
            "floor_area": form.floor_area.data,
            "department": form.department.data,
            "status": form.status.data,
            "notes": form.notes.data,
            "updated_at": datetime.utcnow(),
        }
        mongo.db.workstations.update_one({"_id": ws["_id"]}, {"$set": update})
        audit_log("Workstations", "Update", old_value=old, new_value=update, record_id=ws["_id"])
        clear_reference_data("active_workstations")
        flash("Workstation updated.", "success")
        return redirect(url_for("workstations.detail", ws_id=ws_id))
    return render_template("workstations/form.html", form=form, title="Edit Workstation", ws=serialize_doc(ws))


@workstations_bp.route("/<ws_id>/archive", methods=["POST"])
@login_required
@editor_required
def archive(ws_id):
    ws = get_or_404("workstations", ws_id)
    # Do not archive a workstation that still holds assets or an active accountability.
    active_acc = mongo.db.accountabilities.find_one({"workstation_id": ws_id, "status": "Active"})
    linked_assets = mongo.db.assets.count_documents({"workstation_id": ws_id})
    if active_acc or linked_assets > 0:
        flash("Cannot archive this workstation: it still has active assets or an active accountability.", "error")
        return redirect(url_for("workstations.detail", ws_id=ws_id))
    mongo.db.workstations.update_one({"_id": ws["_id"]},
                                      {"$set": {"status": "Archived", "updated_at": datetime.utcnow()}})
    audit_log("Workstations", "Archive", record_id=ws["_id"])
    clear_reference_data("active_workstations")
    flash("Workstation archived.", "info")
    return redirect(url_for("workstations.list_view"))


def _transfer_batch_source_ctx():
    all_employees = [serialize_doc(e) for e in get_active_employees()]
    all_workstations = [serialize_doc(w) for w in get_active_workstations()]
    return all_employees, all_workstations


def _transfer_batch(source, doc):
    """Common GET/POST handler for the selectable transfer page.

    source = {
        "kind": "workstation" | "employee",
        "label": human-readable name of the source,
        "assets_query": MongoDB query for the selectable assets,
        "detail_url": where to redirect back,
        "back_label": label for the back link
    }
    """
    if request.method == "GET":
        employees = list(mongo.db.employees.find({}))
        workstations = list(mongo.db.workstations.find({}))
        emp_map = {str(e["_id"]): e["full_name"] for e in employees}
        ws_map = {str(w["_id"]): w["workstation_code"] for w in workstations}
        assets = [serialize_doc(a) for a in mongo.db.assets.find(source["assets_query"])]
        for asset in assets:
            asset["_cur_emp"] = emp_map.get(str(asset.get("assigned_to")))
            asset["_cur_ws"] = ws_map.get(str(asset.get("workstation_id")))
        all_employees, all_workstations = _transfer_batch_source_ctx()
        return render_template("transfers/batch.html",
            source=source,
            doc=serialize_doc(doc),
            assets=assets,
            all_employees=all_employees,
            all_workstations=all_workstations,
            asset_count=len(assets)
        )

    # POST: process the selected assets
    target_type = request.form.get("target_type")
    reason = request.form.get("reason", "").strip() or "Batch Transfer"
    notes = request.form.get("notes", "").strip()

    if target_type == "employee":
        target_id = request.form.get("employee_id", "")
    elif target_type == "workstation":
        target_id = request.form.get("workstation_id", "")
    else:
        flash("Choose a transfer target (employee or workstation).", "error")
        return redirect(source["detail_url"])

    asset_ids = request.form.getlist("asset_ids")
    if not asset_ids:
        flash("Select at least one asset to transfer.", "error")
        return redirect(source["detail_url"])

    for aid in asset_ids:
        validation = validate_asset_transfer(aid, target_type, target_id)
        if not validation["valid"]:
            tag = validation["asset"].get("asset_tag", aid)
            flash(f"{tag}: " + "; ".join(validation["errors"]), "error")
            return redirect(source["detail_url"])

    result = perform_batch_transfer(asset_ids, target_type, target_id, reason, notes)
    if not result["ok"]:
        flash(result["error"], "error")
        return redirect(source["detail_url"])

    target = result["target"]
    target_label = target["full_name"] if target_type == "employee" else target["workstation_code"]
    audit_log("Assets", "Batch Transfer",
        old_value={"from": source["label"], "count": len(asset_ids)},
        new_value={"to_type": target_type, "to": target_label, "count": len(asset_ids)})
    flash(f"Successfully transferred {len(asset_ids)} asset(s) to {target_label}.", "success")
    return redirect(source["detail_url"])


@workstations_bp.route("/<ws_id>/transfer-batch", methods=["GET", "POST"])
@login_required
@editor_required
def transfer_batch(ws_id):
    """Transfer selected assets from a workstation to an employee or workstation."""
    ws = get_or_404("workstations", ws_id)
    source = {
        "kind": "workstation",
        "label": f"{ws['workstation_code']} ({ws.get('location', 'No location')})",
        "assets_query": {"workstation_id": ws_id},
        "detail_url": url_for("workstations.detail", ws_id=ws_id),
        "back_label": ws["workstation_code"]
    }
    return _transfer_batch(source, ws)


@workstations_bp.route("/<ws_id>/assign-asset", methods=["POST"])
@login_required
@editor_required
def assign_asset(ws_id):
    ws = get_or_404("workstations", ws_id)
    asset_id = request.form.get("asset_id", "").strip()
    asset_oid = safe_object_id(asset_id)
    if not asset_oid:
        flash("No asset selected.", "error")
        return redirect(url_for("workstations.detail", ws_id=ws_id))
    asset = mongo.db.assets.find_one({"_id": asset_oid})
    if not asset:
        flash("Invalid asset.", "error")
        return redirect(url_for("workstations.detail", ws_id=ws_id))

    # Conflict checks: asset already linked to another workstation, or claimed by an
    # active accountability under a different employee.
    if asset.get("workstation_id") and str(asset["workstation_id"]) != ws_id:
        flash("Asset is already linked to another workstation. Unlink it there first.", "error")
        return redirect(url_for("workstations.detail", ws_id=ws_id))
    other_acc = mongo.db.accountabilities.find_one(
        {"asset_ids": {"$in": [asset_id]}, "status": "Active"})
    if other_acc and str(other_acc.get("workstation_id")) != ws_id:
        flash("Asset is already in an active accountability elsewhere.", "error")
        return redirect(url_for("workstations.detail", ws_id=ws_id))

    acc = mongo.db.accountabilities.find_one({"workstation_id": ws_id, "status": "Active"})
    assigned_to = acc["employee_id"] if acc else None

    def _do_assign(session):
        mongo.db.assets.update_one({"_id": asset["_id"]}, {
            "$set": {
                "workstation_id": ws_id,
                "assigned_to": assigned_to,
                "status": "Assigned" if assigned_to else asset.get("status", "Available"),
                "updated_at": datetime.utcnow(),
            },
            "$push": {"history": {
                "type": "Linked to Workstation",
                "workstation": ws_id,
                "workstation_code": ws.get("workstation_code", ""),
                "date": datetime.utcnow().isoformat(),
                "by": current_user.username,
            }}
        }, session=session)
        # Add to workstation's asset list
        mongo.db.workstations.update_one(
            {"_id": ws["_id"]},
            {"$addToSet": {"assets": asset_id}},
            session=session
        )
        if acc:
            mongo.db.accountabilities.update_one(
                {"_id": acc["_id"]}, {"$addToSet": {"asset_ids": asset_id}}, session=session)

    _run_in_transaction(_do_assign)

    audit_log("Workstations", "AssignAsset",
               new_value={"asset_tag": asset.get("asset_tag"), "workstation": ws.get("workstation_code")},
               record_id=ws["_id"])
    flash(f"Asset {asset.get('asset_tag')} linked to workstation {ws.get('workstation_code')}.", "success")
    return redirect(url_for("workstations.detail", ws_id=ws_id))


@workstations_bp.route("/<ws_id>/unlink-asset/<asset_id>", methods=["POST"])
@login_required
@editor_required
def unlink_asset(ws_id, asset_id):
    ws = get_or_404("workstations", ws_id)
    asset = get_or_404("assets", asset_id)

    def _do_unlink(session):
        mongo.db.assets.update_one({"_id": asset["_id"]}, {
            "$set": {"workstation_id": None, "assigned_to": None, "status": "Available", "updated_at": datetime.utcnow()},
            "$push": {"history": {
                "type": "Unlinked from Workstation",
                "workstation": ws_id,
                "date": datetime.utcnow().isoformat(),
                "by": current_user.username,
            }}
        }, session=session)

        # Remove from workstation's asset list
        mongo.db.workstations.update_one(
            {"_id": ws["_id"]},
            {"$pull": {"assets": asset_id}},
            session=session
        )

        acc = mongo.db.accountabilities.find_one(
            {"workstation_id": ws_id, "status": "Active"}, session=session)
        if acc:
            mongo.db.accountabilities.update_one(
                {"_id": acc["_id"]}, {"$pull": {"asset_ids": asset_id}}, session=session)
            remaining = mongo.db.accountabilities.find_one({"_id": acc["_id"]},
                                                            {"asset_ids": 1}, session=session)
            if not remaining.get("asset_ids"):
                mongo.db.accountabilities.update_one(
                    {"_id": acc["_id"]},
                    {"$set": {"status": "Returned", "updated_at": datetime.utcnow()}},
                    session=session
                )

    _run_in_transaction(_do_unlink)

    audit_log("Workstations", "UnlinkAsset", record_id=ws["_id"])
    flash(f"Asset {asset.get('asset_tag')} removed from workstation.", "info")
    return redirect(url_for("workstations.detail", ws_id=ws_id))


# =============================================================================
# Blueprint: accountabilities
# =============================================================================
accountabilities_bp = Blueprint("accountabilities", __name__, url_prefix="/accountabilities")


@accountabilities_bp.route("")
@login_required
def list_view():
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "")
    query = {}
    if q:
        employee_ids = [str(e["_id"]) for e in mongo.db.employees.find(
            {"$or": [
                {"full_name": qre(q)},
                {"employee_id": qre(q)},
            ]}, {"_id": 1})]
        query["employee_id"] = {"$in": employee_ids}
    if status_filter:
        query["status"] = status_filter
    page = request.args.get("page", 1, type=int)
    accs, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.accountabilities, "created_at", -1, page=page
    )

    employee_ids = {safe_object_id(a["employee_id"]) for a in accs if a.get("employee_id")}
    employee_ids.discard(None)
    employees_by_id = {}
    if employee_ids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(employee_ids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "Unknown")
    for a in accs:
        a["employee_name"] = employees_by_id.get(a.get("employee_id"), "")

    return render_template("accountabilities/list.html", accs=[serialize_doc(a) for a in accs],
                            q=q, status_filter=status_filter, page=page, total=total,
                            per_page=per_page, total_pages=total_pages)


@accountabilities_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = AccountabilityForm()
    employees = get_active_employees()
    workstations = get_active_workstations()
    assets_available = list(mongo.db.assets.find({"status": "Available"}).sort("asset_tag", 1))
    if form.validate_on_submit():
        try:
            asset_ids = json.loads(form.asset_ids.data) if form.asset_ids.data else []
        except (json.JSONDecodeError, TypeError):
            asset_ids = []
        asset_oids = [oid for oid in (safe_object_id(a) for a in asset_ids) if oid]
        errors = []

        emp_oid = safe_object_id(form.employee_id.data)
        emp = mongo.db.employees.find_one({"_id": emp_oid}) if emp_oid else None
        if not emp or emp.get("status") != "Active":
            errors.append("Selected employee is not active or does not exist.")

        # Validate selected assets are available and not claimed by another active accountability.
        locked_oids = []
        if asset_oids:
            locked_oids = [str(a["_id"]) for a in mongo.db.accountabilities.find(
                {"asset_ids": {"$in": asset_oids}, "status": "Active"},
                {"asset_ids": 1}) if a.get("asset_ids")]
        locked = set(locked_oids)
        for oid in asset_oids:
            if str(oid) in locked:
                asset = mongo.db.assets.find_one({"_id": oid}, {"asset_tag": 1})
                errors.append(f"Asset {asset['asset_tag'] if asset else oid} is already in an active accountability.")
                continue
            asset = mongo.db.assets.find_one({"_id": oid}, {"status": 1})
            if not asset:
                errors.append(f"Asset no longer exists: {oid}")
            elif asset.get("status") != "Available":
                errors.append(f"Asset is not available (status: {asset.get('status', 'unknown')}): {oid}")

        # Workstation must not already belong to an active accountability by someone else.
        ws_oid = safe_object_id(form.workstation_id.data) if form.workstation_id.data else None
        if ws_oid:
            existing_ws_acc = mongo.db.accountabilities.find_one(
                {"workstation_id": ws_oid, "status": "Active"})
            if existing_ws_acc and str(existing_ws_acc.get("employee_id")) != str(form.employee_id.data):
                errors.append("Selected workstation is already in an active accountability by another employee.")

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("accountabilities/form.html", form=form,
                                  title="New Accountability",
                                  employees=[serialize_doc(e) for e in employees],
                                  workstations=[serialize_doc(w) for w in workstations],
                                  assets_available=[serialize_doc(a) for a in assets_available])
        doc = {
            "employee_id": form.employee_id.data,
            "workstation_id": form.workstation_id.data or None,
            "asset_ids": asset_ids,
            "accountability_type": form.accountability_type.data,
            "effective_date": to_datetime(form.effective_date.data),
            "status": "Active",
            "notes": form.notes.data,
            "remarks_timeline": [{
                "text": f"{form.accountability_type.data} recorded",
                "by": current_user.username,
                "date": datetime.utcnow().isoformat()
            }],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        def _do_insert(session):
            result = mongo.db.accountabilities.insert_one(doc, session=session)
            valid_asset_oids = [oid for oid in (safe_object_id(a) for a in asset_ids) if oid]
            if valid_asset_oids:
                mongo.db.assets.update_many(
                    {"_id": {"$in": valid_asset_oids}},
                    {"$set": {"status": "Assigned", "assigned_to": form.employee_id.data,
                              "updated_at": datetime.utcnow()}},
                    session=session
                )
            return str(result.inserted_id)

        inserted_id = _run_in_transaction(_do_insert)
        audit_log("Accountabilities", "Create",
                   new_value={"type": doc["accountability_type"], "employee_id": doc["employee_id"]},
                   record_id=inserted_id)

        if form.send_email.data and mail_configured():
            emp_oid = safe_object_id(form.employee_id.data)
            emp = mongo.db.employees.find_one({"_id": emp_oid}) if emp_oid else None
            if emp and emp.get("email"):
                try:
                    send_receive_email(doc, emp)
                    mongo.db.accountabilities.update_one(
                        {"_id": inserted_id},
                        {"$set": {"email_sent_at": datetime.utcnow(),
                                  "email_sent_to": emp["email"]},
                         "$push": {"remarks_timeline": {
                             "text": f"Receive email sent to {emp['email']}",
                             "by": current_user.username,
                             "date": datetime.utcnow().isoformat()}}}
                    )
                    audit_log("Accountabilities", "Email Sent", record_id=inserted_id)
                    flash("Accountability created and receive email sent.", "success")
                except Exception as exc:
                    logger.exception("Failed to send receive email")
                    flash(f"Accountability created, but email failed: {exc}", "error")
            else:
                flash("Accountability created, but the employee has no email address on file.", "warning")
        else:
            flash("Accountability record created.", "success")
        return redirect(url_for("accountabilities.list_view"))
    return render_template("accountabilities/form.html", form=form, title="New Accountability",
                            employees=[serialize_doc(e) for e in employees],
                            workstations=[serialize_doc(w) for w in workstations],
                            assets_available=[serialize_doc(a) for a in assets_available])


@accountabilities_bp.route("/<acc_id>")
@login_required
def detail(acc_id):
    acc = get_or_404("accountabilities", acc_id)
    emp, ws = None, None
    emp_oid = safe_object_id(acc.get("employee_id"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})
    ws_oid = safe_object_id(acc.get("workstation_id"))
    if ws_oid:
        ws = mongo.db.workstations.find_one({"_id": ws_oid})

    asset_oids = [oid for oid in (safe_object_id(a) for a in acc.get("asset_ids", [])) if oid]
    assets = [serialize_doc(a) for a in mongo.db.assets.find({"_id": {"$in": asset_oids}})] if asset_oids else []

    return render_template("accountabilities/detail.html",
                            acc=serialize_doc(acc),
                            emp=serialize_doc(emp) if emp else None,
                            ws=serialize_doc(ws) if ws else None,
                            assets=assets)


@accountabilities_bp.route("/<acc_id>/close", methods=["POST"])
@login_required
@editor_required
def close(acc_id):
    acc = get_or_404("accountabilities", acc_id)
    status = request.form.get("status", "Returned")
    if status not in ACC_STATUSES:
        status = "Returned"
    remark_text = request.form.get("remark", "Accountability closed.")
    mongo.db.accountabilities.update_one({"_id": acc["_id"]}, {
        "$set": {"status": status, "updated_at": datetime.utcnow()},
        "$push": {"remarks_timeline": {
            "text": remark_text, "by": current_user.username, "date": datetime.utcnow().isoformat()
        }}
    })
    asset_oids = [oid for oid in (safe_object_id(a) for a in acc.get("asset_ids", [])) if oid]
    if asset_oids:
        mongo.db.assets.update_many(
            {"_id": {"$in": asset_oids}},
            {"$set": {"status": "Available", "assigned_to": None, "updated_at": datetime.utcnow()}}
        )
    audit_log("Accountabilities", "Close", record_id=acc["_id"])
    flash("Accountability closed.", "info")
    return redirect(url_for("accountabilities.detail", acc_id=acc_id))


@accountabilities_bp.route("/<acc_id>/receive/<token>")
def receive(acc_id, token):
    """Public endpoint hit from the email link â€” no login required.

    Validates the signed, time-limited token, marks the record as received,
    and shows a standalone confirmation page.
    """
    acc = get_or_404("accountabilities", acc_id)
    emp = None
    emp_oid = safe_object_id(acc.get("employee_id"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})

    try:
        data = get_receive_serializer().loads(
            token, max_age=current_app.config["RECEIVE_TOKEN_MAX_AGE"])
    except Exception:
        return render_template("accountabilities/received.html",
                               ok=False, reason="expired or invalid", acc=serialize_doc(acc),
                               emp=serialize_doc(emp) if emp else None), 400

    if str(data.get("acc_id")) != str(acc["_id"]):
        return render_template("accountabilities/received.html",
                               ok=False, reason="mismatch", acc=serialize_doc(acc),
                               emp=serialize_doc(emp) if emp else None), 400

    if not acc.get("received_at"):
        received_by = emp.get("full_name", "Employee") if emp else "Employee"
        mongo.db.accountabilities.update_one(
            {"_id": acc["_id"]},
            {"$set": {"received_at": datetime.utcnow(),
                      "received_by": received_by,
                      "received_method": "email"},
             "$push": {"remarks_timeline": {
                 "text": "Assets received via email link",
                 "by": received_by,
                 "date": datetime.utcnow().isoformat()}}}
        )
        audit_log("Accountabilities", "Receive", record_id=acc["_id"])

    return render_template("accountabilities/received.html",
                           ok=True, acc=serialize_doc(acc),
                           emp=serialize_doc(emp) if emp else None)


@accountabilities_bp.route("/<acc_id>/mark-received", methods=["POST"])
@login_required
@editor_required
def mark_received(acc_id):
    """Manually mark an accountability as received (for assets already handed over)."""
    acc = get_or_404("accountabilities", acc_id)
    mongo.db.accountabilities.update_one({"_id": acc["_id"]}, {
        "$set": {"received_at": datetime.utcnow(),
                 "received_by": current_user.username,
                 "received_method": "manual"},
        "$push": {"remarks_timeline": {
            "text": f"Marked as received by {current_user.username}",
            "by": current_user.username,
            "date": datetime.utcnow().isoformat()}}
    })
    audit_log("Accountabilities", "Mark Received", record_id=acc["_id"])
    flash("Accountability marked as received.", "success")
    return redirect(url_for("accountabilities.detail", acc_id=acc_id))


@accountabilities_bp.route("/<acc_id>/approve", methods=["POST"])
@login_required
@editor_required
def approve(acc_id):
    """Approve a finalized accountability record."""
    acc = get_or_404("accountabilities", acc_id)
    mongo.db.accountabilities.update_one({"_id": acc["_id"]}, {
        "$set": {"approved_at": datetime.utcnow(),
                 "approved_by": current_user.username},
        "$push": {"remarks_timeline": {
            "text": f"Accountability approved by {current_user.username}",
            "by": current_user.username,
            "date": datetime.utcnow().isoformat()}}
    })
    audit_log("Accountabilities", "Approve", record_id=acc["_id"])
    flash("Accountability approved.", "success")
    return redirect(url_for("accountabilities.detail", acc_id=acc_id))


@accountabilities_bp.route("/<acc_id>/send-email", methods=["POST"])
@login_required
@editor_required
def send_email(acc_id):
    """(Re)send the 'Receive Assets' email to the assigned employee."""
    acc = get_or_404("accountabilities", acc_id)
    emp_oid = safe_object_id(acc.get("employee_id"))
    emp = mongo.db.employees.find_one({"_id": emp_oid}) if emp_oid else None
    if not emp or not emp.get("email"):
        flash("Employee has no email address on file.", "error")
        return redirect(url_for("accountabilities.detail", acc_id=acc_id))
    if not mail_configured():
        flash("Email is not configured on this server (MAIL_SERVER not set).", "error")
        return redirect(url_for("accountabilities.detail", acc_id=acc_id))
    try:
        send_receive_email(acc, emp)
        mongo.db.accountabilities.update_one({"_id": acc["_id"]}, {
            "$set": {"email_sent_at": datetime.utcnow(),
                     "email_sent_to": emp["email"]},
            "$push": {"remarks_timeline": {
                "text": f"Receive email sent to {emp['email']}",
                "by": current_user.username,
                "date": datetime.utcnow().isoformat()}}
        })
        audit_log("Accountabilities", "Email Sent", record_id=acc["_id"])
        flash("Receive email sent.", "success")
    except Exception as exc:
        logger.exception("Failed to send receive email")
        flash(f"Email failed: {exc}", "error")
    return redirect(url_for("accountabilities.detail", acc_id=acc_id))


# =============================================================================
# Blueprint: remarks
# =============================================================================
remarks_bp = Blueprint("remarks", __name__, url_prefix="/remarks")

ALLOWED_REMARK_TYPES = {"Employees", "Assets", "Workstations", "Accountabilities"}


@remarks_bp.route("/add", methods=["POST"])
@login_required
@editor_required
def add():
    record_id = request.form.get("record_id")
    record_type = request.form.get("record_type")
    remark = request.form.get("remark", "").strip()
    if record_type not in ALLOWED_REMARK_TYPES:
        flash("Invalid remark target.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))
    if not remark:
        flash("Remark cannot be empty.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))
    doc = {
        "record_id": record_id,
        "record_type": record_type,
        "remark": remark,
        "author": current_user.username,
        "created_at": datetime.utcnow(),
    }
    mongo.db.remarks.insert_one(doc)
    audit_log(record_type, "Remark", new_value={"remark": remark}, record_id=record_id)
    flash("Remark added.", "success")
    return redirect(request.referrer or url_for("dashboard.index"))


# =============================================================================
# Blueprint: audits
# =============================================================================
audits_bp = Blueprint("audits", __name__, url_prefix="/audits")


@audits_bp.route("")
@login_required
def list_view():
    q = request.args.get("q", "").strip()
    query = {}
    if q:
        query["$or"] = [
            {"audit_type": qre(q)},
            {"result": qre(q)},
            {"findings": qre(q)},
            {"auditor": qre(q)},
        ]
    page = request.args.get("page", 1, type=int)
    audits, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.audits, "audit_date", -1, page=page
    )
    return render_template("audits/list.html", audits=[serialize_doc(a) for a in audits],
                            q=q, page=page, total=total, per_page=per_page, total_pages=total_pages)


@audits_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = AuditForm()
    if form.validate_on_submit():
        doc = {
            "audit_type": form.audit_type.data,
            "asset_id": form.asset_id.data or None,
            "workstation_id": form.workstation_id.data or None,
            "result": form.result.data,
            "findings": form.findings.data,
            "audit_date": to_datetime(form.audit_date.data),
            "auditor": current_user.username,
            "created_at": datetime.utcnow(),
        }
        result = mongo.db.audits.insert_one(doc)
        audit_log("Audits", "Create", new_value=doc, record_id=result.inserted_id)
        flash("Audit recorded.", "success")
        return redirect(url_for("audits.list_view"))
    return render_template("audits/form.html", form=form, title="New Audit")


@audits_bp.route("/trail")
@login_required
@admin_required
def trail():
    query = {}
    search_term = request.args.get("search", "").strip()
    module = request.args.get("module", "").strip()

    if search_term:
        query["$or"] = [
            {"username": qre(search_term)},
            {"module": qre(search_term)},
            {"action": qre(search_term)},
        ]
    if module:
        query["module"] = module

    page = request.args.get("page", 1, type=int)
    logs, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.audit_logs, "timestamp", -1, page=page,
        projection={"username": 1, "ip_address": 1, "module": 1, "action": 1,
                    "record_id": 1, "old_value": 1, "new_value": 1, "timestamp": 1}
    )

    preload_oids = set()
    for log in logs:
        oid = safe_object_id(log.get("record_id"))
        if oid:
            preload_oids.add(oid)
        for value_key in ("old_value", "new_value"):
            val = log.get(value_key)
            if isinstance(val, dict):
                for ref_field in ("assigned_to", "workstation_id", "asset_id", "employee_id"):
                    ref = val.get(ref_field)
                    r_oid = safe_object_id(ref)
                    if r_oid:
                        preload_oids.add(r_oid)
    name_map = _build_name_map(preload_oids)
    enriched_logs = [enrich_audit_log(serialize_doc(log), name_map) for log in logs]

    return render_template("audits/trail.html", logs=enriched_logs, page=page, total=total,
                            per_page=per_page, total_pages=total_pages, request=request)


# =============================================================================
# Blueprint: users (admin only)
# =============================================================================
users_bp = Blueprint("users", __name__, url_prefix="/users")


@users_bp.route("")
@login_required
@admin_required
def list_view():
    q = request.args.get("q", "").strip()
    query = {}
    if q:
        query["$or"] = [
            {"username": qre(q)},
            {"full_name": qre(q)},
            {"email": qre(q)},
        ]
    page = request.args.get("page", 1, type=int)
    users, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.users, "username", 1, page=page, projection={"password": 0},
    )
    return render_template("users/list.html", users=[serialize_doc(u) for u in users],
                            q=q, page=page, total=total, per_page=per_page, total_pages=total_pages)


@users_bp.route("/new", methods=["GET", "POST"])
@login_required
@admin_required
def new():
    form = UserForm()
    if form.validate_on_submit():
        if not form.password.data:
            flash("Password is required for new users.", "error")
            return render_template("users/form.html", form=form, title="New User")
        existing = mongo.db.users.find_one({"username": form.username.data})
        if existing:
            flash("Username already taken.", "error")
            return render_template("users/form.html", form=form, title="New User")
        hashed = bcrypt.hashpw(form.password.data.encode(), bcrypt.gensalt())
        doc = {
            "username": form.username.data,
            "full_name": form.full_name.data,
            "email": form.email.data,
            "password": hashed,
            "role": form.role.data,
            "is_active": form.is_active.data,
            "created_at": datetime.utcnow(),
        }
        result = mongo.db.users.insert_one(doc)
        audit_log("Users", "Create", new_value={"username": doc["username"], "role": doc["role"]},
                   record_id=result.inserted_id)
        flash(f"User {form.username.data} created.", "success")
        return redirect(url_for("users.list_view"))
    return render_template("users/form.html", form=form, title="New User")


@users_bp.route("/<user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit(user_id):
    user_doc = get_or_404("users", user_id)
    form = UserForm(data={k: v for k, v in user_doc.items() if k not in ("_id", "password")})
    if form.validate_on_submit():
        update = {
            "full_name": form.full_name.data,
            "email": form.email.data,
            "role": form.role.data,
            "is_active": form.is_active.data,
            "updated_at": datetime.utcnow(),
        }
        if form.password.data:
            update["password"] = bcrypt.hashpw(form.password.data.encode(), bcrypt.gensalt())
        mongo.db.users.update_one({"_id": user_doc["_id"]}, {"$set": update})
        audit_log("Users", "Update", record_id=user_doc["_id"])
        flash("User updated.", "success")
        return redirect(url_for("users.list_view"))
    return render_template("users/form.html", form=form, title="Edit User", user_doc=serialize_doc(user_doc))


# =============================================================================
# Blueprint: import / export / reports
# =============================================================================
io_bp = Blueprint("io", __name__)

REQUIRED_IMPORT_COLUMNS = ["Endpoint Name", "Site", "Last Logged In User", "Serial Number",
                            "Device Type", "Model Name", "OS Version"]


@io_bp.route("/import", methods=["GET", "POST"])
@login_required
@editor_required
def import_inventory():
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("No file uploaded.", "error")
            return redirect(url_for("io.import_inventory"))
        filename = f.filename.lower()
        try:
            import pandas as pd
            if filename.endswith(".csv"):
                df = pd.read_csv(f)
            elif filename.endswith(".xlsx"):
                df = pd.read_excel(f, engine="openpyxl")
            else:
                flash("Unsupported file format. Use .xlsx or .csv", "error")
                return redirect(url_for("io.import_inventory"))
        except Exception:
            logger.exception("Failed to parse import file %s", filename)
            flash("Error reading file â€” check it's a valid CSV/XLSX.", "error")
            return redirect(url_for("io.import_inventory"))

        missing = [c for c in REQUIRED_IMPORT_COLUMNS if c not in df.columns]
        if missing:
            flash(f"Missing required columns: {', '.join(missing)}", "error")
            return redirect(url_for("io.import_inventory"))

        success, failed, duplicates = 0, [], 0
        for _, row in df.iterrows():
            sn = str(row.get("Serial Number", "")).strip()
            if not sn or sn.lower() == "nan":
                failed.append({"row": {k: str(v) for k, v in row.items()}, "reason": "Missing serial number"})
                continue
            existing = mongo.db.assets.find_one({"serial_number": sn})
            if existing:
                duplicates += 1
                continue
            doc = {
                "asset_tag": f"IMP-{sn[:8]}",
                "endpoint_name": str(row.get("Endpoint Name", "")).strip(),
                "serial_number": sn,
                "device_type": str(row.get("Device Type", "Other")).strip(),
                "model_name": str(row.get("Model Name", "")).strip(),
                "os_version": str(row.get("OS Version", "")).strip(),
                "location": str(row.get("Site", "")).strip(),
                "status": "Available",
                "assigned_to": None,
                "workstation_id": None,
                "history": [],
                "import_source": filename,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            mongo.db.assets.insert_one(doc)
            success += 1

        audit_log("Assets", "Import",
                   new_value={"file": filename, "imported": success, "duplicates": duplicates, "failed": len(failed)})
        flash(f"Import complete: {success} imported, {duplicates} duplicates skipped, {len(failed)} failed.", "success")
        session["import_failed"] = failed[:50]
        return redirect(url_for("io.import_inventory"))

    failed_rows = session.pop("import_failed", [])
    return render_template("reports/import.html", failed_rows=failed_rows)


@io_bp.route("/export/assets")
@login_required
def export_assets():
    assets = list(mongo.db.assets.find())
    rows = [{
        "Asset Tag": a.get("asset_tag", ""),
        "Endpoint Name": a.get("endpoint_name", ""),
        "Serial Number": a.get("serial_number", ""),
        "Device Type": a.get("device_type", ""),
        "Model Name": a.get("model_name", ""),
        "OS Version": a.get("os_version", ""),
        "Location": a.get("location", ""),
        "Status": a.get("status", ""),
        "Assigned To": a.get("assigned_to", ""),
        "Workstation": a.get("workstation_id", ""),
        "Warranty Expiry": a.get("warranty_expiry", ""),
        "Created": a.get("created_at", ""),
    } for a in assets]
    import pandas as pd
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Assets")
    buf.seek(0)
    audit_log("Assets", "Export")
    return _deliver_download(
        "assets_export.xlsx", buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@io_bp.route("/export/employees")
@login_required
def export_employees():
    employees = list(mongo.db.employees.find())
    rows = [{"Employee ID": e.get("employee_id"), "Full Name": e.get("full_name"),
             "Email": e.get("email"), "Department": e.get("department"),
             "Position": e.get("position"), "Site": e.get("site"),
             "Status": e.get("status")} for e in employees]
    import pandas as pd
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Employees")
    buf.seek(0)
    audit_log("Employees", "Export")
    return _deliver_download(
        "employees_export.xlsx", buf.getvalue(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _pdf_header(elements, title, styles):
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Spacer
    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Paragraph(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC", styles["Normal"]))
    elements.append(Spacer(1, 0.3 * inch))


@io_bp.route("/reports/assets/pdf")
@login_required
def report_assets_pdf():
    assets = list(mongo.db.assets.find({"status": {"$nin": ["Disposed", "Retired"]}}))
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    _pdf_header(elements, "Asset Inventory Report", styles)
    data = [["Asset Tag", "Serial Number", "Type", "Model", "Location", "Status"]]
    for a in assets:
        data.append([a.get("asset_tag", ""), a.get("serial_number", ""), a.get("device_type", ""),
                     a.get("model_name", ""), a.get("location", ""), a.get("status", "")])
    t = Table(data, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2FF")]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(t)
    doc.build(elements)
    buf.seek(0)
    audit_log("Reports", "Export PDF Assets")
    return _deliver_download("asset_inventory.pdf", buf.getvalue(), "application/pdf")


@io_bp.route("/reports/accountability/<acc_id>/pdf")
@login_required
def accountability_pdf(acc_id):
    acc = get_or_404("accountabilities", acc_id)
    emp, ws = None, None
    emp_oid = safe_object_id(acc.get("employee_id"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})
    ws_oid = safe_object_id(acc.get("workstation_id"))
    if ws_oid:
        ws = mongo.db.workstations.find_one({"_id": ws_oid})

    asset_oids = [oid for oid in (safe_object_id(a) for a in acc.get("asset_ids", [])) if oid]
    assets = list(mongo.db.assets.find({"_id": {"$in": asset_oids}})) if asset_oids else []

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4)
    styles = getSampleStyleSheet()
    elements = [Paragraph("Accountability Form", styles["Title"]), Spacer(1, 0.2 * inch)]
    if emp:
        info = [
            ["Employee", emp.get("full_name", "")],
            ["Employee ID", emp.get("employee_id", "")],
            ["Department", emp.get("department", "")],
            ["Position", emp.get("position", "")],
            ["Site", emp.get("site", "")],
            ["Type", acc.get("accountability_type", "")],
            ["Effective Date", str(acc.get("effective_date", ""))],
            ["Status", acc.get("status", "")],
        ]
        t = Table(info, colWidths=[2 * inch, 4 * inch])
        t.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("PADDING", (0, 0), (-1, -1), 6),
        ]))
        elements += [t, Spacer(1, 0.2 * inch)]
    if assets:
        elements.append(Paragraph("Assets Covered", styles["Heading2"]))
        adata = [["Asset Tag", "Serial Number", "Type", "Model"]]
        for a in assets:
            adata.append([a.get("asset_tag", ""), a.get("serial_number", ""), a.get("device_type", ""), a.get("model_name", "")])
        at = Table(adata, repeatRows=1)
        at.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        elements += [at, Spacer(1, 0.3 * inch)]

    sig = Table([["Employee Signature", "IT Department", "Manager"]], colWidths=[2 * inch, 2 * inch, 2 * inch])
    sig.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.HexColor("#F5F5F5")]),
    ]))
    elements += [Paragraph("Signatures", styles["Heading2"]), sig]
    doc.build(elements)
    buf.seek(0)
    return _deliver_download(f"accountability_{acc_id}.pdf", buf.getvalue(), "application/pdf")


@io_bp.route("/reports/stickers/assets")
@login_required
def asset_stickers():
    asset_ids = request.args.getlist("ids")
    if not asset_ids:
        assets = list(mongo.db.assets.find({"status": {"$nin": ["Disposed", "Retired"]}}).limit(50))
    else:
        oids = [oid for oid in (safe_object_id(i) for i in asset_ids) if oid]
        assets = list(mongo.db.assets.find({"_id": {"$in": oids}})) if oids else []
    stickers = [{"asset": serialize_doc(a), "qr": generate_asset_qr(a)} for a in assets]
    return render_template("reports/stickers.html", stickers=stickers, sticker_type="Asset")


@io_bp.route("/reports/stickers/workstations")
@login_required
def workstation_stickers():
    ws_ids = request.args.getlist("ids")
    if not ws_ids:
        workstations = list(mongo.db.workstations.find({"status": {"$ne": "Archived"}}).limit(50))
    else:
        oids = [oid for oid in (safe_object_id(i) for i in ws_ids) if oid]
        workstations = list(mongo.db.workstations.find({"_id": {"$in": oids}})) if oids else []

    stickers = []
    for w in workstations:
        acc = mongo.db.accountabilities.find_one({"workstation_id": str(w["_id"]), "status": "Active"})
        ws_emp = None
        if acc:
            emp_oid = safe_object_id(acc.get("employee_id"))
            if emp_oid:
                ws_emp = mongo.db.employees.find_one({"_id": emp_oid})
        stickers.append({"ws": serialize_doc(w), "qr": generate_workstation_qr(w, ws_emp),
                          "emp": serialize_doc(ws_emp) if ws_emp else None})
    return render_template("reports/stickers_ws.html", stickers=stickers)


# =============================================================================
# Blueprint: JSON API (used by the Flutter app)
# =============================================================================
api_bp = Blueprint("api", __name__, url_prefix="/api")


@api_bp.route("/employees/search")
@login_required
def employees_search():
    q = request.args.get("q", "")
    employees = list(mongo.db.employees.find({
        "$or": [
            {"full_name": qre(q)},
            {"employee_id": qre(q)},
        ],
        "status": "Active"
    }).limit(10))
    return jsonify({"success": True,
                    "data": [{"id": str(e["_id"]), "text": f"{e['full_name']} ({e['employee_id']})"}
                             for e in employees]})


@api_bp.route("/assets/search")
@login_required
def assets_search():
    q = request.args.get("q", "")
    status = request.args.get("status", "")
    query = {"$or": [
        {"asset_tag": qre(q)},
        {"serial_number": qre(q)},
        {"model_name": qre(q)},
    ]}
    if status:
        query["status"] = status
    assets = list(mongo.db.assets.find(query).limit(10))
    return jsonify({"success": True,
                    "data": [{"id": str(a["_id"]), "text": f"{a['asset_tag']} - {a['model_name']} ({a['status']})"}
                             for a in assets]})


@api_bp.route("/stats")
@login_required
def stats():
    return jsonify({"success": True, "data": {
        "assets": mongo.db.assets.count_documents({}),
        "employees": mongo.db.employees.count_documents({"status": "Active"}),
        "workstations": mongo.db.workstations.count_documents({"status": "Active"}),
    }})


def _iso_or_none(value):
    return value.isoformat() if isinstance(value, datetime) else None


@api_bp.route("/employees")
@login_required
def employees_all():
    employees = get_active_employees()
    return jsonify({"success": True, "data": [{
        "id": str(e["_id"]),
        "employee_id": e.get("employee_id", ""),
        "full_name": e.get("full_name", ""),
        "email": e.get("email", ""),
        "department": e.get("department", ""),
        "position": e.get("position", ""),
        "site": e.get("site", ""),
        "status": e.get("status", ""),
    } for e in employees]})


@api_bp.route("/assets")
@login_required
def assets_all():
    assets = list(mongo.db.assets.find({"status": {"$nin": ["Disposed", "Retired"]}}).sort("asset_tag", 1))

    employee_ids = {safe_object_id(a["assigned_to"]) for a in assets if a.get("assigned_to")}
    employee_ids.discard(None)
    employees_by_id = {}
    if employee_ids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(employee_ids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "")

    return jsonify({"success": True, "data": [{
        "id": str(a["_id"]),
        "asset_tag": a.get("asset_tag", ""),
        "serial_number": a.get("serial_number", ""),
        "device_type": a.get("device_type", ""),
        "model_name": a.get("model_name", ""),
        "status": a.get("status", ""),
        "location": a.get("location", ""),
        "assigned_to": employees_by_id.get(a.get("assigned_to"), ""),
        "workstation": a.get("workstation_id", ""),
        "warranty_expiry": _iso_or_none(a.get("warranty_expiry")),
    } for a in assets]})


@api_bp.route("/workstations")
@login_required
def workstations_all():
    workstations = list(mongo.db.workstations.find({"status": {"$ne": "Archived"}}).sort("workstation_code", 1))
    ws_ids = [str(w["_id"]) for w in workstations]

    counts = {}
    if ws_ids:
        for row in mongo.db.assets.aggregate([
            {"$match": {"workstation_id": {"$in": ws_ids}}},
            {"$group": {"_id": "$workstation_id", "count": {"$sum": 1}}},
        ]):
            counts[row["_id"]] = row["count"]

    active_accs = list(mongo.db.accountabilities.find(
        {"workstation_id": {"$in": ws_ids}, "status": "Active"})) if ws_ids else []
    emp_ids_by_ws = {a["workstation_id"]: a.get("employee_id") for a in active_accs}
    emp_oids = {safe_object_id(v) for v in emp_ids_by_ws.values() if v}
    employees_by_id = {}
    if emp_oids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(emp_oids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "")

    return jsonify({"success": True, "data": [{
        "id": str(w["_id"]),
        "workstation_code": w.get("workstation_code", ""),
        "workstation_name": w.get("workstation_name", ""),
        "location": w.get("location", ""),
        "department": w.get("department", ""),
        "status": w.get("status", ""),
        "asset_count": counts.get(str(w["_id"]), 0),
        "assigned_to": employees_by_id.get(emp_ids_by_ws.get(str(w["_id"])), ""),
    } for w in workstations]})


@api_bp.route("/accountabilities")
@login_required
def accountabilities_all():
    accs = list(mongo.db.accountabilities.find({"status": "Active"}).sort("created_at", -1))

    emp_oids = {safe_object_id(a["employee_id"]) for a in accs if a.get("employee_id")}
    emp_oids.discard(None)
    employees_by_id = {}
    if emp_oids:
        for emp in mongo.db.employees.find({"_id": {"$in": list(emp_oids)}}, {"full_name": 1}):
            employees_by_id[str(emp["_id"])] = emp.get("full_name", "")

    ws_oids = {safe_object_id(a["workstation_id"]) for a in accs if a.get("workstation_id")}
    ws_oids.discard(None)
    workstations_by_id = {}
    if ws_oids:
        for ws in mongo.db.workstations.find({"_id": {"$in": list(ws_oids)}}, {"workstation_code": 1}):
            workstations_by_id[str(ws["_id"])] = ws.get("workstation_code", "")

    return jsonify({"success": True, "data": [{
        "id": str(a["_id"]),
        "employee_name": employees_by_id.get(a.get("employee_id"), ""),
        "workstation_code": workstations_by_id.get(a.get("workstation_id"), ""),
        "accountability_type": a.get("accountability_type", ""),
        "effective_date": _iso_or_none(a.get("effective_date")),
        "status": a.get("status", ""),
        "asset_count": len(a.get("asset_ids", [])),
    } for a in accs]})


@api_bp.route("/asset/<asset_id>")
@login_required
def asset_detail_api(asset_id):
    oid = safe_object_id(asset_id)
    if not oid:
        return jsonify({"error": "Asset not found"}), 404
    asset = mongo.db.assets.find_one({"_id": oid})
    if not asset:
        return jsonify({"error": "Asset not found"}), 404

    employee_name = ""
    emp_oid = safe_object_id(asset.get("assigned_to"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})
        if emp:
            employee_name = emp.get("full_name", "")

    return jsonify({"success": True, "data": {
        "id": str(asset["_id"]),
        "asset_tag": asset.get("asset_tag", ""),
        "serial_number": asset.get("serial_number", ""),
        "device_type": asset.get("device_type", ""),
        "model_name": asset.get("model_name", ""),
        "manufacturer": asset.get("manufacturer", ""),
        "os_version": asset.get("os_version", ""),
        "cpu": asset.get("cpu", ""),
        "ram": asset.get("ram", ""),
        "storage": asset.get("storage", ""),
        "location": asset.get("location", ""),
        "status": asset.get("status", ""),
        "assigned_to": employee_name,
        "purchase_date": _iso_or_none(asset.get("purchase_date")),
        "warranty_expiry": _iso_or_none(asset.get("warranty_expiry")),
        "notes": asset.get("notes", ""),
    }})


# =============================================================================
# Application factory
# =============================================================================
def create_app(config_name=None):
    app = Flask(__name__)

    config_name = config_name or os.environ.get("FLASK_ENV", "production")
    app.config.from_object(CONFIG_MAP.get(config_name, ProductionConfig))
    app.config["IS_DESKTOP"] = os.environ.get("ASSETSYS_DESKTOP") == "1"
    app.config["EXPORTS_DIR"] = os.environ.get("ASSETSYS_EXPORTS_DIR", "")

    logging.basicConfig(
        level=logging.DEBUG if app.config.get("DEBUG") else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # CORS: only allow the configured app origin (same-origin frontend) plus a
    # couple of loopback origins for local development. Requests without an
    # allowed origin, or same-origin requests, are unaffected.
    _app_origin = os.environ.get("APP_BASE_URL", "").rstrip("/")
    _allowed_origins = [o for o in {_app_origin, "http://localhost:5000",
                                    "http://127.0.0.1:5000"} if o]
    CORS(app, resources={r"/api/*": {"origins": _allowed_origins}},
         supports_credentials=True)

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy",
                                    "camera=(), microphone=(), geolocation=()")
        return response

    mongo.init_app(app)
    csrf.init_app(app)
    login_manager.init_app(app)

    for bp in (auth_bp, dashboard_bp, employees_bp, assets_bp, workstations_bp,
               accountabilities_bp, remarks_bp, audits_bp, users_bp, io_bp, api_bp,
               scan_bp):
        app.register_blueprint(bp)

    # AI module
    app.config["MONGO_DB"] = mongo.db
    from ai.blueprint import ai_bp
    app.register_blueprint(ai_bp)
    from ai.scheduler import init_scheduler
    init_scheduler(app, mongo.db)

    register_error_handlers(app)
    register_cli(app)

    return app


def register_error_handlers(app):
    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": {"code": 403,
                            "message": "Access denied."}}), 403
        return render_template("auth/error.html", code=403, message="Access denied."), 403

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": {"code": 404,
                            "message": "Not found."}}), 404
        return render_template("auth/error.html", code=404, message="Page not found."), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception("Unhandled server error")
        message = "Internal server error." if not app.debug else str(e)
        if request.path.startswith("/api/"):
            return jsonify({"success": False, "error": {"code": 500,
                            "message": message}}), 500
        return render_template("auth/error.html", code=500, message=message), 500


def register_cli(app):
    @app.cli.command("seed-admin")
    def seed_admin_command():
        """Create the default admin user if the users collection is empty."""
        with app.app_context():
            if mongo.db.users.count_documents({}) == 0:
                password = os.environ.get("SEED_ADMIN_PASSWORD", "admin123")
                hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
                mongo.db.users.insert_one({
                    "username": "admin",
                    "full_name": "System Administrator",
                    "email": "admin@company.com",
                    "password": hashed,
                    "role": "admin",
                    "is_active": True,
                    "created_at": datetime.utcnow(),
                })
                print("[SEED] Default admin user created: admin /", password)
            else:
                print("[SEED] Users already exist â€” skipping.")

    @app.cli.command("create-indexes")
    def create_indexes_command():
        """Create MongoDB indexes."""
        with app.app_context():
            mongo.db.assets.create_index("serial_number", unique=True, sparse=True)
            mongo.db.assets.create_index("asset_tag")
            mongo.db.assets.create_index("workstation_id")
            mongo.db.assets.create_index("assigned_to")
            mongo.db.assets.create_index([("status", 1), ("asset_tag", 1)])
            mongo.db.employees.create_index("employee_id", unique=True)
            mongo.db.employees.create_index("email")
            mongo.db.employees.create_index([("status", 1), ("full_name", 1)])
            mongo.db.workstations.create_index("workstation_code", unique=True)
            mongo.db.workstations.create_index([("status", 1), ("workstation_code", 1)])
            mongo.db.accountabilities.create_index("workstation_id")
            mongo.db.accountabilities.create_index("employee_id")
            mongo.db.accountabilities.create_index([("status", 1), ("created_at", -1)])
            mongo.db.accountabilities.create_index("asset_ids")
            mongo.db.audit_logs.create_index([("timestamp", -1)])
            mongo.db.audit_logs.create_index("action")
            mongo.db.remarks.create_index([("record_id", 1), ("record_type", 1)])
            mongo.db.users.create_index("username", unique=True)
            mongo.db.ai_anomalies.create_index([("detected_at", -1)])
            mongo.db.ai_anomalies.create_index([("acknowledged", 1), ("severity", 1)])
            mongo.db.ai_reports.create_index([("generated_at", -1)])
            print("[INDEXES] Created.")


# WSGI entrypoint for gunicorn/uwsgi: `gunicorn 'app:create_app()'`
app = create_app()

if __name__ == "__main__":
    # APP_HOST: bind to a specific interface (e.g. "172.31.201.79") or all ("0.0.0.0")
    app.run(host=os.environ.get("APP_HOST", "0.0.0.0"),
            port=int(os.environ.get("PORT", 5000)),
            debug=app.config.get("DEBUG", False))
