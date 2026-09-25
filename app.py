"""
IT Asset Accountability & Asset Management System
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
import hashlib
import hmac
from uuid import uuid4
import sys
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
from flask import (Blueprint, Flask, Response, abort, current_app, flash, jsonify,
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

# Only load .env in source/dev runs; a frozen exe must never pick up a stray
# .env sitting next to it (package config lives in config.json).
if not getattr(sys, "frozen", False):
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
    # Remembered sessions expire after this long (non-remembered = browser-session cookie).
    PERMANENT_SESSION_LIFETIME = timedelta(
        hours=int(os.environ.get("SESSION_LIFETIME_HOURS", "12")))
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


def send_mail(to_addr, subject, html_body, cc_addrs=None):
    """Send an HTML email via the configured SMTP server. Raises on failure."""
    cfg = current_app.config
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{cfg['MAIL_FROM_NAME']} <{cfg['MAIL_FROM']}>"
    msg["To"] = to_addr
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg.set_content("This email requires an HTML-capable client. "
                    "Please open it in a modern web/email application.")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(cfg["MAIL_SERVER"], cfg["MAIL_PORT"], timeout=30) as server:
        if cfg["MAIL_USE_TLS"]:
            server.starttls()
        server.login(cfg["MAIL_USERNAME"], cfg["MAIL_PASSWORD"])
        server.send_message(msg)


def send_receive_email(acc, emp, cc_emps=None):
    """Email the accountability record's recipient (primary) the 'Receive Assets' link.

    cc_emps is an optional list of secondary (agency) employees in hold positions;
    they receive a copy for visibility but do not own the record.
    """
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
    cc_addrs = [e["email"] for e in (cc_emps or []) if e and e.get("email")]
    send_mail(emp["email"], "IT Asset Accountability â€” Please Confirm Receipt", html, cc_addrs=cc_addrs or None)


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


def _transfer_history_text(h, names=None):
    """Human-readable summary of a single asset history (transfer) entry.

    History entries store employees as either display names (older data) or
    hex employee ids; a names map (id -> full_name) is used to resolve ids.
    """
    names = names or {}

    def _name(value):
        if not value:
            return None
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{24}", value):
            return names.get(value) or value
        return value

    from_name = _name(h.get("from_employee"))
    to_name = _name(h.get("to_employee"))
    if from_name and to_name:
        text = "from {} to {}".format(from_name, to_name)
    elif to_name:
        text = "assigned to {}".format(to_name)
    else:
        text = "transferred"
    if h.get("location"):
        text += " to {}".format(h["location"])
    if h.get("reason"):
        text += " (Reason: {})".format(h["reason"])
    return text


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
    except Exception:
        logger.exception("Audit display-name lookup failed for %s", oid)
    return None


# ─── Tamper-evident audit chain (see scripts/audit_chain.py) ──────────────
AUDIT_SIGNED_FIELDS = (
    "timestamp", "username", "ip_address", "module", "action",
    "record_id", "record_name", "old_value", "new_value",
)


def _audit_iso(value):
    if isinstance(value, datetime):
        s = value.isoformat()
        if "." in s:
            head, micro = s.rsplit(".", 1)
            return head + "." + micro[:3]  # Mongo stores dates at ms precision
        return s
    return str(value) if value is not None else None


def _audit_chained_sig(doc, prev_sig, prev_id):
    """HMAC-SHA256 over the signed fields + the previous chain link.

    Single source of truth is ``integrity.py`` (used by both the CLI verify
    script and the Admin > System Integrity page). Falls back to the local
    copy below when the module is unavailable (e.g. frozen desktop exe).
    """
    try:
        from integrity import chain_sign
        sig, _, _ = chain_sign(prev_sig, prev_id, doc)
        return sig
    except ImportError:
        pass
    return _audit_chained_sig_fallback(doc, prev_sig, prev_id)


def _audit_chained_sig_fallback(doc, prev_sig, prev_id):
    prev_sig = prev_sig or ""
    prev_id = str(prev_id) if prev_id is not None else ""
    payload = {}
    for field in AUDIT_SIGNED_FIELDS:
        payload[field] = _audit_iso(doc[field]) if field == "timestamp" else doc.get(field)
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    body += b"|" + prev_sig.encode("utf-8") + b"|" + prev_id.encode("utf-8")
    key = os.environ.get("AUDIT_CHAIN_KEY") or os.environ.get("SECRET_KEY") or "itsystem-dev-chain-key"
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def audit_log(module, action, old_value=None, new_value=None, record_id=None, username=None):
    record_name = None
    record_oid = safe_object_id(record_id)
    if record_oid:
        record_name = _lookup_display_name(record_oid)
    if username is None:
        username = current_user.username if getattr(current_user, "is_authenticated", False) else "system"
    entry = {
        "timestamp": datetime.utcnow(),
        "username": username,
        "ip_address": get_client_ip(),
        "module": module,
        "action": action,
        "record_id": str(record_id) if record_id else None,
        "record_name": record_name,
        "old_value": _process_audit_value(old_value),
        "new_value": _process_audit_value(new_value),
    }
    # Tamper-evident HMAC chain: each entry links to the previous one's sig.
    # Any manual edit/deletion in audit_logs becomes detectable via
    # ``python scripts/audit_chain.py verify``.
    try:
        prev = mongo.db.audit_logs.find_one({}, sort=[("_id", -1)])
    except Exception:
        logger.exception("Audit chain tail lookup failed")
        prev = None
    prev_sig = (prev or {}).get("sig", "")
    prev_id = str(prev["_id"]) if prev else None
    entry["sig"] = _audit_chained_sig(entry, prev_sig, prev_id)
    entry["prev_sig"] = prev_sig
    entry["prev_id"] = prev_id
    mongo.db.audit_logs.insert_one(entry)


def get_client_ip():
    try:
        if current_app.config.get("TRUST_PROXY_HEADERS"):
            return request.headers.get("X-Forwarded-For", request.remote_addr)
        return request.remote_addr
    except RuntimeError:  # no request context (e.g. background export job)
        return None


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
    after employee writes when freshness matters immediately.
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


def _responsible_choices(exclude_id=None):
    """Choices for the 'Reports To / Accountable Officer' dropdown."""
    exclude = str(exclude_id) if exclude_id else None
    choices = [("", "— None (standalone employee) —")]
    for e in mongo.db.employees.find({"status": "Active"}).sort("full_name", 1):
        if str(e["_id"]) == exclude:
            continue
        choices.append((str(e["_id"]), f"{e['full_name']} ({e.get('employee_id', '')})"))
    return choices


def accountability_scope(employee_id):
    """Employee ids whose assigned assets fall under employee_id's accountability.

    A primary (direct hire) scope includes the agency/secondary staff reporting
    to them; an agency employee resolves to themselves only (their assets are
    surfaced through their primary's scope).
    """
    emp = mongo.db.employees.find_one({"_id": safe_object_id(employee_id)})
    scope = [employee_id]
    if emp and not emp.get("responsible_employee_id"):
        for r in mongo.db.employees.find({"responsible_employee_id": employee_id}, {"_id": 1}):
            scope.append(str(r["_id"]))
    return scope


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
                              ("assets", "asset_tag")):
        coll = getattr(mongo.db, collection, None)
        if coll is None:
            continue
        for doc in coll.find({"_id": {"$in": ids}}, {field: 1}):
            name_map[str(doc["_id"])] = doc.get(field)
    return name_map


FIELD_LABELS = {
    "asset_tag": "Asset Tag", "endpoint_name": "Endpoint Name", "serial_number": "Serial Number",
    "device_type": "Device Type", "model_name": "Model", "manufacturer": "Manufacturer",
    "os_version": "OS Version", "cpu": "CPU", "ram": "RAM", "storage": "Storage",
    "purchase_date": "Purchase Date", "warranty_expiry": "Warranty Expiry",
    "purchase_cost": "Purchase Cost", "vendor": "Vendor", "location": "Location",
    "status": "Status", "notes": "Notes", "assigned_to": "Assigned To",
    "workstation_id": "Workstation", "asset_id": "Asset", "employee_id": "Employee",
    "workstation_code": "Workstation Code", "workstation_name": "Workstation Name",
    "floor_area": "Floor / Area", "department": "Department", "full_name": "Full Name",
    "email": "Email", "contact_number": "Contact Number", "site": "Site",
    "date_hired": "Date Hired", "position": "Position", "accountability_type": "Type",
    "effective_date": "Effective Date", "asset_ids": "Assets", "audit_type": "Audit Type",
    "result": "Result", "findings": "Findings", "audit_date": "Audit Date",
    "username": "Username", "role": "Role", "is_active": "Active",
    "email_sent_at": "Email Sent At", "received_at": "Received At", "approved_at": "Approved At",
    "received_by": "Received By", "approved_by": "Approved By",
    "file": "File", "imported": "Imported", "duplicates": "Duplicates Skipped",
    "failed": "Failed Rows", "from": "From", "to": "To", "to_type": "Target Type", "count": "Count",
}
AUDIT_NOISE_FIELDS = {"_id", "updated_at", "created_at", "history", "remarks_timeline"}
ACTION_SUMMARY = {
    ("Assets", "Create"): "Registered a new asset",
    ("Assets", "Update"): "Updated asset details",
    ("Assets", "Archive"): "Archived asset",
    ("Assets", "Transfer"): "Transferred asset",
    ("Assets", "Batch Transfer"): "Batch-transferred assets",
    ("Assets", "Export"): "Exported assets to file",
    ("Assets", "Import"): "Imported assets from file",
    ("Employees", "Create"): "Added a new employee",
    ("Employees", "Update"): "Updated employee details",
    ("Employees", "Archive"): "Archived employee",
    ("Employees", "Export"): "Exported employees to file",
    ("Workstations", "Create"): "Registered a new workstation",
    ("Workstations", "Update"): "Updated workstation details",
    ("Workstations", "Archive"): "Archived workstation",
    ("Workstations", "AssignAsset"): "Linked asset to workstation",
    ("Workstations", "UnlinkAsset"): "Unlinked asset from workstation",
    ("Accountabilities", "Create"): "Created accountability record",
    ("Accountabilities", "Close"): "Closed accountability record",
    ("Accountabilities", "Receive"): "Employee confirmed receipt of assets",
    ("Accountabilities", "Mark Received"): "Marked accountability as received",
    ("Accountabilities", "Approve"): "Approved accountability record",
    ("Accountabilities", "Email Sent"): "Sent receive-confirmation email",
    ("Audits", "Create"): "Recorded a new audit",
    ("Users", "Create"): "Created a new user account",
    ("Users", "Update"): "Updated user account",
    ("Auth", "Login"): "Signed in",
    ("Auth", "Logout"): "Signed out",
}
_DATETIME_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def _format_audit_value(value):
    """Render a stored audit value as short human-readable text (None if empty)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (list, tuple)):
        return f"{len(value)} item(s)" if value else None
    if isinstance(value, dict):
        return None
    text = str(value).strip()
    if not text:
        return None
    if _DATETIME_TEXT_RE.match(text):
        return text[:16].replace("T", " ")
    return text


def _audit_change_rows(log_dict):
    """Build readable field-change rows (label / old / new) from stored values."""
    old_v, new_v = log_dict.get("old_value"), log_dict.get("new_value")
    rows = []

    def _display(container, key):
        raw = container.get(key)
        pretty = container.get(f"{key}_name")
        return pretty or _format_audit_value(raw)

    if isinstance(old_v, dict) and isinstance(new_v, dict):
        keys = list(dict.fromkeys(list(new_v.keys()) + list(old_v.keys())))
        for key in keys:
            if key in AUDIT_NOISE_FIELDS or key.endswith("_name"):
                continue
            old_disp, new_disp = _display(old_v, key), _display(new_v, key)
            if old_disp == new_disp:
                continue
            rows.append({"label": FIELD_LABELS.get(key, key.replace("_", " ").title()),
                         "old": old_disp, "new": new_disp})
    elif isinstance(new_v, dict):
        for key, raw in new_v.items():
            if key in AUDIT_NOISE_FIELDS or key.endswith("_name"):
                continue
            disp = new_v.get(f"{key}_name") or _format_audit_value(raw)
            if disp is None:
                continue
            rows.append({"label": FIELD_LABELS.get(key, key.replace("_", " ").title()),
                         "old": None, "new": disp})
    elif isinstance(old_v, dict):
        for key, raw in old_v.items():
            if key in AUDIT_NOISE_FIELDS or key.endswith("_name"):
                continue
            disp = old_v.get(f"{key}_name") or _format_audit_value(raw)
            if disp is None:
                continue
            rows.append({"label": FIELD_LABELS.get(key, key.replace("_", " ").title()),
                         "old": disp, "new": None})
    return rows


def _audit_detail_text(log_dict):
    """One-line sentence for actions better described as prose than field diffs."""
    module, action = log_dict.get("module"), log_dict.get("action")
    new_v = log_dict.get("new_value") or {}
    old_v = log_dict.get("old_value") or {}
    if module == "Assets" and action == "Batch Transfer":
        return (f"Moved {new_v.get('count', '?')} asset(s) from "
                f"{old_v.get('from', '?')} to {new_v.get('to', '?')}")
    if action == "Import":
        return (f"Imported {new_v.get('imported', 0)} asset(s) from {new_v.get('file', 'file')} "
                f"— {new_v.get('duplicates', 0)} duplicate(s), {new_v.get('failed', 0)} failed")
    if action == "Remark":
        remark = new_v.get("remark")
        return f"\"{remark}\"" if remark else None
    return None


def enrich_audit_log(log_dict, name_map=None):
    """Attach human-readable names, summary and change rows for display."""
    record_id = log_dict.get("record_id")
    if record_id:
        oid = safe_object_id(record_id)
        if oid:
            name = (log_dict.get("record_name") or (name_map or {}).get(str(oid))
                    or _lookup_display_name(oid))
            if name:
                log_dict["record_name"] = name
    for value_key in ("old_value", "new_value"):
        val = log_dict.get(value_key)
        if isinstance(val, dict):
            for ref_field in ("assigned_to", "asset_id", "employee_id"):
                ref = val.get(ref_field)
                if ref and f"{ref_field}_name" not in val:
                    oid = safe_object_id(ref)
                    if oid:
                        name = (name_map or {}).get(str(oid)) or _lookup_display_name(oid)
                        if name:
                            val[f"{ref_field}_name"] = name
    log_dict["summary"] = ACTION_SUMMARY.get(
        (log_dict.get("module"), log_dict.get("action")), log_dict.get("action") or "Action")
    log_dict["detail_text"] = _audit_detail_text(log_dict)
    log_dict["change_rows"] = _audit_change_rows(log_dict)
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
    resp = send_file(buf, download_name=filename, as_attachment=True,
                     mimetype=mimetype, max_age=0)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "Thu, 01 Jan 1970 00:00:00 GMT"
    return resp


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


def generate_employee_bundle_qr(emp_doc):
    """QR encodes ONLY the stable scan URL (/scan/bundle/<employee_id>), never
    the item data. The badge is generated once; its image never changes when the
    bundle's contents change. The scan page always reads the live assignment
    state from the database at request time."""
    emp_id = str(emp_doc.get("_id", emp_doc.get("id", "")))
    scan_url = _reachable_scan_url("scan.scan_bundle", emp_id)
    return generate_qr(scan_url, fill_color="#2F7D52", back_color="white")


def get_stockroom_custodian(db):
    """The single employee accountable for stockroom (Available) inventory.

    Stored as {"_id": "stockroom_custodian", "employee_id": <hex>} in the
    settings collection. Returns the employee doc or None when unset/broken.
    """
    doc = db.settings.find_one({"_id": "stockroom_custodian"})
    if not doc or not doc.get("employee_id"):
        return None
    oid = safe_object_id(doc["employee_id"])
    if not oid:
        return None
    return db.employees.find_one({"_id": oid})


def is_scan_qr_enabled():
    """Master override for the QR scan / scan-back importer feature.

    Defaults to ON so existing behaviour is unchanged until an admin flips it.
    While OFF every scan route resolves to a "scanning disabled" page so the
    team can still print & physically stick asset stickers during data
    reconciliation without accidentally writing half-verified/duplicate rows.
    """
    doc = mongo.db.settings.find_one({"_id": "scan_qr_enabled"})
    if doc is None:
        return True
    return doc.get("enabled", True) is not False


def set_scan_qr_enabled(enabled, username):
    mongo.db.settings.update_one(
        {"_id": "scan_qr_enabled"},
        {"$set": {"enabled": bool(enabled),
                  "updated_by": username,
                  "updated_at": datetime.utcnow()}},
        upsert=True)


# =============================================================================
# QR Scan Routes
# =============================================================================
scan_bp = Blueprint("scan", __name__)

@scan_bp.route("/scan/asset/<record_id>")
def scan_asset(record_id):
    if not is_scan_qr_enabled():
        return render_template("scan/disabled.html",
                               feature="asset QR scanning")
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

    custodian = get_stockroom_custodian(mongo.db)
    stockroom_custodian = custodian["full_name"] if (
        custodian and doc.get("status") == "Available") else None

    return render_template("scan/result.html",
        type="Asset",
        tag=doc.get("asset_tag", ""),
        model=doc.get("model_name", ""),
        device_type=doc.get("device_type", ""),
        serial=doc.get("serial_number", ""),
        status=doc.get("status", ""),
        location=doc.get("location", ""),
        assigned_to=emp.get("full_name", "Unassigned") if emp else "Unassigned",
        assigned_to_id=str(emp["_id"]) if emp else None,
        stockroom_custodian=stockroom_custodian,
        employee_id=emp.get("employee_id", "") if emp else "",
        brand=doc.get("brand", ""),
        children=None,
    )


@scan_bp.route("/scan/bundle/<record_id>")
def scan_bundle(record_id):
    if not is_scan_qr_enabled():
        return render_template("scan/disabled.html",
                               feature="accessory bundle scanning")
    """Accessory-bundle scan page for an employee.

    Reads the bundle's current item list fresh from the database on every
    request (no TTL caching, unlike the dashboard) so edits made seconds ago
    are reflected. A zero-item bundle renders a clear empty state.
    """
    oid = safe_object_id(record_id)
    if not oid:
        abort(404)
    emp = mongo.db.employees.find_one({"_id": oid})
    if not emp:
        abort(404)

    # Agency/secondary staff scan their own badge but the accountability they
    # belong to lives with their responsible (primary) direct hire. Resolve the
    # accountable employee, then list every asset under that accountability.
    primary_emp = emp
    if emp.get("responsible_employee_id"):
        primary_oid = safe_object_id(emp["responsible_employee_id"])
        if primary_oid:
            primary_emp = mongo.db.employees.find_one({"_id": primary_oid}) or emp

    scope = [str(primary_emp["_id"])]
    for r in mongo.db.employees.find(
            {"responsible_employee_id": str(primary_emp["_id"])}, {"_id": 1}):
        if str(r["_id"]) not in scope:
            scope.append(str(r["_id"]))

    assets = list(mongo.db.assets.find(
        {"assigned_to": {"$in": scope}},
        {"asset_tag": 1, "device_type": 1, "model_name": 1, "serial_number": 1},
    ))
    groups = {}
    for a in assets:
        key = a.get("device_type") or "Other"
        groups.setdefault(key, []).append(a)
    ordered = [{"type": t, "items": items}
               for t, items in sorted(groups.items(),
                                      key=lambda kv: (-len(kv[1]), kv[0]))]

    return render_template("scan/bundle.html",
                           emp=serialize_doc(emp),
                           primary_emp=serialize_doc(primary_emp),
                           groups=ordered,
                           total=len(assets))


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


def resolve_accountability_parties(employee_id):
    """Resolve who holds the assets vs who is accountable for them.

    Agency/secondary staff carry `responsible_employee_id` pointing at their
    direct-hire officer. Assets are assigned to the holder (the person using
    them day-to-day) but the accountability record belongs to the primary
    officer. Returns (primary_emp, holder_emp).
    """
    emp = mongo.db.employees.find_one({"_id": safe_object_id(employee_id)})
    if emp is None:
        return None, None
    if emp.get("responsible_employee_id"):
        primary_oid = safe_object_id(emp["responsible_employee_id"])
        primary = mongo.db.employees.find_one({"_id": primary_oid}) if primary_oid else None
        if primary:
            return primary, emp
    return emp, emp


def create_accountability(employee_id, asset_ids, acc_type, notes=None,
                          secondary_employee_ids=None, session=None):
    """Create or merge into an active accountability for an employee.

    `employee_id` is the accountable officer (primary). If they already have an
    Active accountability the new assets are merged into that record instead of
    creating a duplicate. `secondary_employee_ids` tracks agency/secondary staff
    whose assets live under this record. Runs on the given session when provided
    so multi-collection writes are atomic.
    """
    if not isinstance(asset_ids, list):
        asset_ids = [asset_ids]
    secondary_ids = [s for s in (secondary_employee_ids or []) if s]

    # Merge into the employee's existing active accountability when present
    existing = mongo.db.accountabilities.find_one({
        "employee_id": employee_id,
        "status": "Active"
    }, session=session)

    if existing:
        update = {
            "$addToSet": {"asset_ids": {"$each": asset_ids}},
            "$set": {"updated_at": datetime.utcnow()},
            "$push": {"remarks_timeline": {
                "text": f"Assets added from {acc_type}",
                "by": current_user.username,
                "date": datetime.utcnow().isoformat()
            }}
        }
        if secondary_ids:
            update["$addToSet"]["secondary_employee_ids"] = {"$each": secondary_ids}
        mongo.db.accountabilities.update_one({"_id": existing["_id"]}, update, session=session)
        return existing["_id"]

    # Otherwise create a new accountability
    doc = {
        "employee_id": employee_id,
        "asset_ids": asset_ids,
        "secondary_employee_ids": secondary_ids,
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
    target_type: 'employee', 'stockroom'
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

    if target_type == "employee" and current_assigned_to and str(current_assigned_to) == target_id:
        info.append("Asset is already assigned to this employee.")
        return {"valid": True, "errors": errors, "warnings": warnings, "info": info, "asset": asset}

    if current_assigned_to:
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
    """Transfer a set of pre-validated assets to an employee.

    Each asset follows the same rules as a single transfer: the old
    accountability is unwound per asset (closing it when empty), the asset is
    relinked to the holder, and transfer history is recorded. Assets assigned to
    agency/secondary staff land under their primary officer's accountability.
    asset_ids must already have passed validate_asset_transfer().
    """
    if target_type != "employee":
        return {"ok": False, "error": "Unknown transfer target."}

    primary_emp, holder_emp = resolve_accountability_parties(target_id)
    if not primary_emp or not holder_emp:
        return {"ok": False, "error": "Invalid employee selected."}
    target = holder_emp

    def _do(session):
        for aid in asset_ids:
            current = mongo.db.assets.find_one({"_id": safe_object_id(aid)}, session=session)
            if not current:
                continue
            old_emp = current.get("assigned_to")
            update = {
                "assigned_to": str(holder_emp["_id"]),
                "status": "Assigned",
                "updated_at": datetime.utcnow(),
            }
            if old_emp:
                close_accountability_for_asset(aid, session=session)
            mongo.db.assets.update_one(
                {"_id": current["_id"]},
                {
                    "$set": update,
                    "$push": {"history": {
                        "type": "Batch Transfer",
                        "from_employee": old_emp,
                        "to_employee": update.get("assigned_to"),
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

    secondary_ids = ([str(holder_emp["_id"])] if str(holder_emp["_id"]) != str(primary_emp["_id"])
                     else None)
    create_accountability(str(primary_emp["_id"]), asset_ids, "Batch Transfer", notes,
                          secondary_employee_ids=secondary_ids)

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
    division = StringField("Division", validators=[Optional()])
    section = StringField("Section", validators=[Optional()])
    group = StringField("Group", validators=[Optional()])
    contact_number = StringField("Contact Number", validators=[Optional()])
    date_hired = OptionalDateField("Date Hired", validators=[Optional()])
    status = SelectField("Status", choices=[
        ("Active", "Active"), ("Inactive", "Inactive"),
        ("Resigned", "Resigned"), ("On Leave", "On Leave")
    ])
    responsible_employee_id = SelectField("Reports To / Accountable Officer", validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional()])


# =============================================================================
# Site / Asset Tag conventions
# -----------------------------------------------------------------------------
# The asset tag prefix encodes the physical site using compact short codes,
# while the long KPI prefixes remain recognized for legacy tags:
#   KPIMNL -> Head Office (HO)   KPIBLN -> Bustos (BLN)
#   KPIBTS -> Batangas (BTS)     KPID   -> Davao (DVO)
# Recommended tag shape:  <site code>-<device code>-<number>
#   e.g. HO-L-01 (Head Office laptop #01), DVO-MS-04 (Davao mouse #04).
# The device code is a short, freely editable key per Device Type (L = laptop,
# MO = monitor, MS = mouse, KB = keyboard, ...). A bare "KPI" tag means
# generic / unspecified and is always left exactly as-is.
# =============================================================================
SITE_PREFIXES = [
    ("Head Office (KPI Manila)", "KPIMNL", "HO"),
    ("Bustos Site", "KPIBLN", "BLN"),
    ("Batangas Site", "KPIBTS", "BTS"),
    ("Davao Site", "KPID", "DVO"),
]
SITE_BY_PREFIX = {prefix: label for label, prefix, _ in SITE_PREFIXES}
SITE_BY_SHORT = {short: label for label, _, short in SITE_PREFIXES}
SITE_SHORT_CODES = {prefix: short for _, prefix, short in SITE_PREFIXES}
_ALL_SITE_SEGMENTS = set(SITE_BY_PREFIX) | set(SITE_BY_SHORT)

DEVICE_CODE_DEFAULTS = {
    "Laptop": "L", "Desktop": "D", "Monitor": "MO", "Printer": "PR",
    "Scanner": "SC", "Mouse": "MS", "Keyboard": "KB", "Headset": "HS",
    "Company Phone": "CP", "Type C Hub": "HB", "Phone": "PH", "Tablet": "T",
    "Network Equipment": "NW", "Peripheral": "PE", "Server": "SRV", "Other": "X",
}
DEVICE_CODE_SUGGESTIONS = ["L", "D", "MO", "MS", "KB", "PR", "PH",
                           "T", "NW", "PE", "SRV", "SC", "HS", "CP", "HB", "X"]

# Endpoint hostnames carry a device letter after the site prefix
# (e.g. KPIMNLD51 = Desktop, KPIMNLL192 = Laptop). Map those to the standard
# device codes / Device Types so a tag can be derived from the endpoint name.
ENDPOINT_DEVICE_CODE_MAP = {
    "L": "L", "D": "D", "M": "MO", "P": "PR", "T": "T",
    "N": "NW", "S": "SRV",
}
ENDPOINT_DEVICE_TYPE_MAP = {
    "L": "Laptop", "D": "Desktop", "M": "Monitor", "P": "Printer",
    "T": "Tablet", "N": "Network Equipment", "S": "Server",
}


def site_from_tag(tag):
    """Return the site label implied by a tag's first segment.

    Understands both short codes (HO/BLN/BTS/DVO) and long prefixes
    (KPIMNL/KPIBLN/KPIBTS/KPID, with or without a dash) for legacy tags.
    Returns None for generic/bare-KPI or unrecognized tags so they can render
    as generic ('as-is') instead of being forced into a site.
    """
    if not tag:
        return None
    up = tag.strip().upper()
    seg = up.split("-", 1)[0].strip()
    if seg in SITE_BY_PREFIX:
        return SITE_BY_PREFIX[seg]
    if seg in SITE_BY_SHORT:
        return SITE_BY_SHORT[seg]
    for prefix in sorted(SITE_BY_PREFIX, key=lambda p: -len(p)):
        if up.startswith(prefix):  # no-dash legacy e.g. "KPIMNL001"
            return SITE_BY_PREFIX[prefix]
    return None


def device_code_from_tag(tag):
    """Return the device code embedded in a tag (segment 2), if any.

    Numeric sequences alone (e.g. the '02' in 'BLN-02') are numbering, not a
    device code, so they return None.
    """
    if not tag:
        return None
    parts = tag.strip().upper().split("-")
    if len(parts) < 2:
        return None
    if parts[0].strip() not in _ALL_SITE_SEGMENTS:
        return None
    code = parts[1].strip()
    if not code or code.isdigit():
        return None
    return code


def normalize_tag_for_site(tag, site, device_code=None):
    """Align a tag to <site-code>-<device-code>-<...>.

    Only acts when `site` is one of the four real sites (the canonical long-
    prefix value, e.g. 'KPIBLN'). Recognized site segments (long or short) are
    swapped for the short code, bare-KPI input is advanced in place, and a
    missing device-code segment is inserted. Generic 'KPI' sites pass tags
    through untouched. Returns (tag, changed).
    """
    if not tag or site not in SITE_SHORT_CODES:
        return (tag or "").strip(), False
    short = SITE_SHORT_CODES[site]
    t = tag.strip()
    up = t.upper()
    seg0 = up.split("-", 1)[0].strip()
    dc = (device_code or "").strip().upper()

    if seg0 in _ALL_SITE_SEGMENTS:
        body = t[len(seg0):]
        new = (short + body).strip()
    elif seg0.startswith("KPI"):
        rest = t[3:].lstrip("-")
        new = (short + "-" + rest) if rest else short
    elif seg0.isdigit():
        new = short + "-" + (dc or "") + "-" + t
    elif dc and seg0 == dc:
        new = short + "-" + t
    else:
        new = t

    if dc:
        parts = new.split("-")
        if len(parts) < 2 or parts[1] != dc:
            new = parts[0] + "-" + dc
            if len(parts) > 1:
                new += "-" + "-".join(parts[1:])
    new = new.rstrip("-").strip()
    return (new, new != t)


def parse_endpoint_name(endpoint):
    """Break a hostname into (long site prefix, device letter, number).

    Recognizes the convention <SITE PREFIX><LETTER?><NUMBER?>:
      'KPIMNLD51' -> ('KPIMNL', 'D', '51')   'KPIBLN010' -> ('KPIBLN', None, '010')
      'KPIDL01'   -> ('KPID', 'L', '01')
    Returns (None, None, None) for hosts that don't start with a site prefix.
    """
    if not endpoint:
        return None, None, None
    up = endpoint.strip().upper()
    for prefix in sorted(SITE_BY_PREFIX, key=lambda p: -len(p)):
        if up.startswith(prefix):
            rest = up[len(prefix):]
            m = re.match(r"([A-Z]+)?([0-9]*)$", rest)
            letters = m.group(1) if m else None
            number = (m.group(2) or None) if m else None
            letter = letters if letters and len(letters) == 1 else None
            return prefix, letter, number
    return None, None, None


def next_tag_number(site, device_code):
    """Next zero-padded sequence number for a (site, device code) pair.

    Counts every existing tag shaped <short|-long>-<device_code>-<digits>
    (e.g. HO-D-05 / KPIMNL-D-05) so a derived tag never collides.
    """
    short = SITE_SHORT_CODES.get(site) or site
    dc = (device_code or "").upper()
    pat = re.compile(r"^(?:{0}|{1})-{2}-(\d+)$".format(
        re.escape(short), re.escape(site), re.escape(dc)), re.IGNORECASE)
    n = 0
    for a in mongo.db.assets.find({}, {"asset_tag": 1}):
        m = pat.match((a.get("asset_tag") or "").strip())
        if m:
            n = max(n, int(m.group(1)))
    s = str(n + 1)
    return s if len(s) >= 2 else "0" + s


def derive_asset_tag(endpoint, site=None, device_code=None):
    """Build <short>-<device code>-<next number> straight from a hostname.

    Parses the endpoint for site + device letter, falls back to caller-provided
    values, and picks the next free sequence number. Returns None when no site
    or device code can be determined.
    """
    ep_site, ep_letter, _ = parse_endpoint_name(endpoint)
    site = site or ep_site
    if not site:
        return None
    if not device_code and ep_letter:
        device_code = ENDPOINT_DEVICE_CODE_MAP.get(ep_letter, ep_letter)
    device_code = (device_code or "").strip().upper()
    if not device_code:
        return None
    return "{}-{}-{}".format(SITE_SHORT_CODES[site], device_code,
                             next_tag_number(site, device_code))


class AssetForm(FlaskForm):
    asset_tag = StringField("Asset Tag", validators=[DataRequired(), Length(max=50)])
    endpoint_name = StringField("Endpoint Name / Hostname", validators=[Optional()])
    serial_number = StringField("Serial Number", validators=[Optional()])
    device_type = SelectField("Device Type", choices=[
        ("Laptop", "Laptop"), ("Desktop", "Desktop"), ("Monitor", "Monitor"),
        ("Printer", "Printer"), ("Scanner", "Scanner"), ("Mouse", "Mouse"),
        ("Keyboard", "Keyboard"), ("Headset", "Headset"),
        ("Company Phone", "Company Phone"), ("Type C Hub", "Type C Hub"),
        ("Phone", "Phone"), ("Tablet", "Tablet"),
        ("Network Equipment", "Network Equipment"), ("Peripheral", "Peripheral"),
        ("Server", "Server"), ("Other", "Other")
    ])
    model_name = StringField("Model Name", validators=[Optional()])
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
    device_code = StringField("Device Code", validators=[Optional(), Length(max=4)],
                              filters=[lambda v: (v or "").strip().upper()],
                              render_kw={"placeholder": "L / MO / MS…",
                                         "list": "deviceCodes"})
    site = SelectField("Site", choices=[("KPI", "KPI (generic / legacy)")] +
                       [(prefix, "{} ({})".format(label, SITE_SHORT_CODES[prefix]))
                        for label, prefix, _ in SITE_PREFIXES],
                       default="KPI")
    status = SelectField("Status", choices=[
        ("Available", "Available"), ("Assigned", "Assigned"),
        ("Under Maintenance", "Under Maintenance"), ("Retired", "Retired"),
        ("Disposed", "Disposed"), ("Lost", "Lost")
    ], default="Available")
    notes = TextAreaField("Notes", validators=[Optional()])


class AccountabilityForm(FlaskForm):
    employee_id = HiddenField("Employee ID", validators=[DataRequired()])
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

    results = {
        "assets": [serialize_doc(a) for a in assets],
        "employees": [serialize_doc(e) for e in employees],
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
            {"position": qre(q)},
            {"division": qre(q)},
            {"section": qre(q)},
            {"group": qre(q)},
        ]
    if status_filter:
        query["status"] = status_filter
    page = request.args.get("page", 1, type=int)
    employees, total, total_pages, page, per_page = paginate(
        None, query, mongo.db.employees, "full_name", 1, page=page
    )
    return render_template("employees/list.html", employees=[serialize_doc(e) for e in employees],
                            q=q, status_filter=status_filter, page=page, total=total,
                            per_page=per_page, total_pages=total_pages,
                            failed_rows=session.pop("import_failed", []))


@employees_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = EmployeeForm()
    form.responsible_employee_id.choices = _responsible_choices()
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
            "division": form.division.data,
            "section": form.section.data,
            "group": form.group.data,
            "contact_number": form.contact_number.data,
            "responsible_employee_id": form.responsible_employee_id.data or None,
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
    accountabilities = list(mongo.db.accountabilities.find({"$or": [
        {"employee_id": employee_id},
        {"secondary_employee_ids": employee_id},
    ]}).sort("created_at", -1))
    scope = accountability_scope(employee_id)
    assets = list(mongo.db.assets.find({"assigned_to": {"$in": scope}}))

    primary_emp = None
    if emp.get("responsible_employee_id"):
        primary_oid = safe_object_id(emp["responsible_employee_id"])
        if primary_oid:
            primary_emp = mongo.db.employees.find_one({"_id": primary_oid})

    return render_template("employees/detail.html",
                            emp=serialize_doc(emp),
                            primary_emp=serialize_doc(primary_emp) if primary_emp else None,
                            accountabilities=[serialize_doc(a) for a in accountabilities],
                            assets=[serialize_doc(a) for a in assets],
                            qr_b64=generate_employee_bundle_qr(emp))


@employees_bp.route("/<employee_id>/transfer-assets", methods=["GET", "POST"])
@login_required
@editor_required
def transfer_assets(employee_id):
    """Transfer selected assets assigned to an employee (and their reportees) to another employee."""
    emp = get_or_404("employees", employee_id)
    source = {
        "kind": "employee",
        "label": emp["full_name"],
        "assets_query": {"assigned_to": {"$in": accountability_scope(employee_id)}, "status": "Assigned"},
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
        "division": emp.get("division"),
        "section": emp.get("section"),
        "group": emp.get("group"),
        "contact_number": emp.get("contact_number"),
        "status": emp.get("status", "Active"),
        "responsible_employee_id": emp.get("responsible_employee_id") or "",
        "notes": emp.get("notes"),
    })
    form.responsible_employee_id.choices = _responsible_choices(exclude_id=employee_id)
    if form.validate_on_submit():
        old = serialize_doc(emp)
        update = {
            "full_name": form.full_name.data,
            "email": form.email.data,
            "department": form.department.data,
            "position": form.position.data,
            "site": form.site.data,
            "division": form.division.data,
            "section": form.section.data,
            "group": form.group.data,
            "contact_number": form.contact_number.data,
            "date_hired": to_datetime(form.date_hired.data),
            "status": form.status.data,
            "responsible_employee_id": form.responsible_employee_id.data or None,
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


@employees_bp.route("/<employee_id>/resign", methods=["POST"])
@login_required
@editor_required
def resign(employee_id):
    """Retrieve everything and mark an employee as Resigned.

    Closes the exit of an employee in one action: every asset in their
    accountability scope returns to the stockroom (status Available, unassigned,
    history entry logged), each is pulled from the active accountability record
    (which flips to Returned when empty), and the employee is set to Resigned.
    Also clears the 'one action away' paperwork so the printed accountability
    sheet reads as voided rather than live.
    """
    emp = get_or_404("employees", employee_id)
    if emp.get("status") == "Resigned":
        flash("This employee is already marked as Resigned.", "info")
        return redirect(url_for("employees.detail", employee_id=employee_id))

    reportees = list(mongo.db.employees.find(
        {"responsible_employee_id": employee_id, "status": "Active"},
        {"_id": 1, "full_name": 1}))
    if reportees:
        names = ", ".join(r.get("full_name", "?") for r in reportees[:5])
        extra = " and others" if len(reportees) > 5 else ""
        flash(f"Cannot resign {emp.get('full_name', 'this employee')}: "
              f"{len(reportees)} reportee(s) still under this officer ({names}{extra}). "
              "Transfer or inactivate them first.", "error")
        return redirect(url_for("employees.detail", employee_id=employee_id))

    scope = accountability_scope(employee_id)
    assets = list(mongo.db.assets.find({"assigned_to": {"$in": scope}}))
    returned_ids = []
    for a in assets:
        mongo.db.assets.update_one(
            {"_id": a["_id"]},
            {"$set": {"status": "Available", "assigned_to": None,
                      "updated_at": datetime.utcnow()},
             "$push": {"history": {
                 "type": "Transfer",
                 "from_employee": str(emp["_id"]),
                 "to_employee": None,
                 "reason": "Resignation - retrieve all assets",
                 "date": datetime.utcnow().isoformat(),
                 "by": current_user.username,
             }}},
        )
        close_accountability_for_asset(str(a["_id"]))
        returned_ids.append(str(a["_id"]))

    mongo.db.employees.update_one(
        {"_id": emp["_id"]},
        {"$set": {"status": "Resigned", "updated_at": datetime.utcnow()}})
    audit_log("Employees", "Resign",
              old_value={"status": emp.get("status")},
              new_value={"status": "Resigned", "assets_returned": len(returned_ids)},
              record_id=emp["_id"])
    clear_reference_data("active_employees")

    if not returned_ids:
        flash("No assigned assets — employee marked as Resigned.", "success")
        return redirect(url_for("employees.detail", employee_id=employee_id))

    flash(f"{len(returned_ids)} asset(s) returned to stockroom. "
          "Print the asset QR stickers below and re-label the devices.", "success")
    return redirect(url_for("io.asset_stickers", ids=returned_ids))


def _transfer_batch_source_ctx():
    all_employees = [serialize_doc(e) for e in get_active_employees()]
    return all_employees


def _transfer_batch(source, doc):
    """GET/POST handler for the selectable batch-transfer page.

    source = {
        "kind": "employee",
        "label": human-readable name of the source,
        "assets_query": MongoDB query for the selectable assets,
        "detail_url": where to redirect after the action,
        "back_label": label for the back link
    }
    """
    if request.method == "GET":
        employees = list(mongo.db.employees.find({}))
        emp_map = {str(e["_id"]): e["full_name"] for e in employees}
        assets = [serialize_doc(a) for a in mongo.db.assets.find(source["assets_query"])]
        for asset in assets:
            asset["_cur_emp"] = emp_map.get(str(asset.get("assigned_to")))
        all_employees = _transfer_batch_source_ctx()
        return render_template("transfers/batch.html",
                               source=source,
                               doc=serialize_doc(doc),
                               assets=assets,
                               all_employees=all_employees,
                               asset_count=len(assets))

    target_type = request.form.get("target_type")
    reason = request.form.get("reason", "").strip() or "Batch Transfer"
    notes = request.form.get("notes", "").strip()

    if target_type != "employee":
        flash("Choose a transfer target.", "error")
        return redirect(source["detail_url"])
    target_id = request.form.get("employee_id", "")

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
    target_label = target.get("full_name", "")
    audit_log("Assets", "Batch Transfer",
              old_value={"from": source["label"], "count": len(asset_ids)},
              new_value={"to_type": target_type, "to": target_label, "count": len(asset_ids)})
    flash(f"Successfully transferred {len(asset_ids)} asset(s) to {target_label}.", "success")
    return redirect(source["detail_url"])


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
    site_filter = request.args.get("site", "")
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
    if site_filter:
        if site_filter == "_none":
            # legacy / no site: tags that carry no recognized site prefix
            alts = "|".join(re.escape(p) for e in SITE_PREFIXES for p in (e[2], e[1]))
            query["asset_tag"] = {"$regex": re.compile(r"^(?!(?:%s)-)" % alts, re.I)}
        else:
            entry = next((e for e in SITE_PREFIXES if e[2] == site_filter), None)
            if entry:
                alts = "|".join(re.escape(p) for p in (entry[2], entry[1]))
                query["asset_tag"] = {"$regex": re.compile(r"^(?:%s)-" % alts, re.I)}

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

    custodian = get_stockroom_custodian(mongo.db)
    custodian_name = custodian["full_name"] if custodian else None
    for a in assets:
        a["employee_name"] = employees_by_id.get(a.get("assigned_to"), "")
        if a.get("status") == "Available" and not a.get("assigned_to"):
            a["stockroom_holder"] = custodian_name
        else:
            a["stockroom_holder"] = None

    site_options = [("{} ({})".format(label, sh), sh) for label, _long, sh in SITE_PREFIXES]
    return render_template("assets/list.html", assets=[serialize_doc(a) for a in assets],
                            q=q, status_filter=status_filter, type_filter=type_filter,
                            site_filter=site_filter, site_options=site_options,
                            page=page, total=total, per_page=per_page, total_pages=total_pages)


def _collect_custom_fields():
    """Collect user-defined custom columns from the add/edit asset form.

    The form posts matching pairs as custom_key_<n> / custom_val_<n>.
    Empty keys or values are skipped; later duplicates win.
    """
    fields = {}
    for key, submitted_name in request.form.items():
        if not key.startswith("custom_key_"):
            continue
        idx = key[len("custom_key_"):]
        name = (submitted_name or "").strip()
        if not name:
            continue
        value = (request.form.get("custom_val_" + idx) or "").strip()
        if value:
            fields[name] = value
    return fields


@assets_bp.route("/new", methods=["GET", "POST"])
@login_required
@editor_required
def new():
    form = AssetForm()
    custom_fields = _collect_custom_fields()
    if request.method == "POST" and not (form.asset_tag.data or "").strip():
        # Auto-generate the tag from the endpoint name when the tag is left
        # blank: KPIMNLD51 -> site Head Office, Desktop -> HO-D-<next free>.
        ep_site, ep_letter, _ = parse_endpoint_name(form.endpoint_name.data)
        site_val = form.site.data if form.site.data != "KPI" else ep_site
        dc = (form.device_code.data or "").strip().upper()
        if not dc:
            dc = ENDPOINT_DEVICE_CODE_MAP.get(ep_letter, ep_letter) if ep_letter else \
                DEVICE_CODE_DEFAULTS.get(form.device_type.data, "")
        derived = None
        if site_val and dc:
            derived = derive_asset_tag(form.endpoint_name.data, site_val, dc)
        if derived:
            form.asset_tag.data = derived
            flash(f"Asset tag auto-generated from endpoint name: {derived}.", "info")
    if form.validate_on_submit():
        _serial_txt = (form.serial_number.data or "").strip()
        if _serial_txt:
            existing = mongo.db.assets.find_one({"serial_number": _serial_txt})
            if existing:
                flash("An asset with this serial number already exists.", "error")
            return render_template(
                "assets/form.html", form=form, title="New Asset",
                custom_fields=custom_fields, site_by_prefix=SITE_BY_PREFIX,
                site_short_codes=SITE_SHORT_CODES,
                device_code_defaults=DEVICE_CODE_DEFAULTS,
                device_code_suggestions=DEVICE_CODE_SUGGESTIONS)
        device_code = (form.device_code.data or "").strip().upper()
        if not device_code:
            device_code = DEVICE_CODE_DEFAULTS.get(form.device_type.data, "")
        raw_tag = (form.asset_tag.data or "").strip()
        asset_tag, changed = normalize_tag_for_site(raw_tag, form.site.data, device_code)
        if changed:
            flash(f"Asset tag '{raw_tag}' normalized to '{asset_tag}' "
                  f"({SITE_BY_PREFIX.get(form.site.data)}, "
                  f"code {SITE_SHORT_CODES.get(form.site.data)}).", "warning")
        dup = mongo.db.assets.find_one({"asset_tag": asset_tag})
        if dup:
            flash(f"An asset with tag '{asset_tag}' already exists.", "error")
            return render_template(
                "assets/form.html", form=form, title="New Asset",
                custom_fields=custom_fields, site_by_prefix=SITE_BY_PREFIX,
                site_short_codes=SITE_SHORT_CODES,
                device_code_defaults=DEVICE_CODE_DEFAULTS,
                device_code_suggestions=DEVICE_CODE_SUGGESTIONS)
        doc = {
            "asset_tag": asset_tag,
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
            "custom_fields": custom_fields,
            "assigned_to": None,
            "history": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        result = mongo.db.assets.insert_one(doc)
        audit_log("Assets", "Create",
                   new_value={"asset_tag": doc["asset_tag"], "serial_number": doc["serial_number"]},
                   record_id=result.inserted_id)
        flash(f"Asset {asset_tag} created successfully.", "success")
        return redirect(url_for("assets.list_view"))
    return render_template("assets/form.html", form=form, title="New Asset",
                           custom_fields=custom_fields, site_by_prefix=SITE_BY_PREFIX,
                           site_short_codes=SITE_SHORT_CODES,
                           device_code_defaults=DEVICE_CODE_DEFAULTS,
                           device_code_suggestions=DEVICE_CODE_SUGGESTIONS)


@assets_bp.route("/<asset_id>")
@login_required
def detail(asset_id):
    asset = get_or_404("assets", asset_id)
    emp = None
    emp_oid = safe_object_id(asset.get("assigned_to"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})
    remarks = list(mongo.db.remarks.find({"record_id": asset_id}).sort("created_at", -1))
    audits = list(mongo.db.audits.find({"asset_id": asset_id}).sort("audit_date", -1))
    qr_b64 = generate_asset_qr(asset)

    # Resolve history employee ids -> full names
    hex_ids = set()
    for h in asset.get("history", []):
        for k in ("from_employee", "to_employee"):
            v = h.get(k)
            if isinstance(v, str) and re.fullmatch(r"[0-9a-f]{24}", v):
                hex_ids.add(v)
    name_map = {}
    if hex_ids:
        oids = [ObjectId(x) for x in hex_ids]
        for e in mongo.db.employees.find({"_id": {"$in": oids}}, {"_id": 1, "full_name": 1}):
            name_map[str(e["_id"])] = e.get("full_name") or str(e["_id"])

    transfers = [
        {"kind": "transfer", "date": h.get("date") or "", "by": h.get("by"),
         "text": _transfer_history_text(h, name_map)}
        for h in asset.get("history", [])
    ]
    remark_events = [
        {"kind": "remark", "date": r["created_at"].isoformat() if r.get("created_at") else "",
         "by": r.get("author"),
         "text": (r.get("title") or "Untitled") + ((" — " + r.get("remark")) if r.get("remark") else "")}
        for r in remarks
    ]
    history_timeline = sorted(transfers + remark_events,
                              key=lambda e: e["date"], reverse=True)

    # Available employees for the transfer modal
    available_employees = get_active_employees()

    custodian = get_stockroom_custodian(mongo.db)
    return render_template("assets/detail.html",
                            asset=serialize_doc(asset),
                            emp=serialize_doc(emp) if emp else None,
                            remarks=[serialize_doc(r) for r in remarks],
                            audits=[serialize_doc(a) for a in audits],
                            history_timeline=history_timeline,
                            qr_b64=qr_b64,
                            stockroom_custodian=serialize_doc(custodian) if custodian else None,
                            available_employees=[serialize_doc(e) for e in available_employees])


@assets_bp.route("/<asset_id>/qr.png")
@login_required
def asset_qr(asset_id):
    """The asset's QR sticker as a raw PNG (for list-page preview modal)."""
    asset = get_or_404("assets", asset_id)
    import base64
    return Response(base64.b64decode(generate_asset_qr(asset)), mimetype="image/png")


@assets_bp.route("/<asset_id>/edit", methods=["GET", "POST"])
@login_required
@editor_required
def edit(asset_id):
    asset = get_or_404("assets", asset_id)
    form = AssetForm(data={k: v for k, v in asset.items() if k != "_id"})
    if request.method == "GET":
        dc = device_code_from_tag(asset.get("asset_tag"))
        if dc:
            form.device_code.data = dc
    custom_fields = _collect_custom_fields()
    if request.method == "GET":
        custom_fields = {**asset.get("custom_fields", {}), **custom_fields}
    if form.validate_on_submit():
        dup = mongo.db.assets.find_one({"asset_tag": form.asset_tag.data,
                                        "_id": {"$ne": asset["_id"]}})
        if dup:
            flash(f"Another asset already uses tag '{form.asset_tag.data}'.", "error")
            return render_template("assets/form.html", form=form, title="Edit Asset",
                                   asset=serialize_doc(asset), custom_fields=custom_fields,
                                   site_by_prefix=SITE_BY_PREFIX,
                                   site_short_codes=SITE_SHORT_CODES,
                                   device_code_defaults=DEVICE_CODE_DEFAULTS,
                                   device_code_suggestions=DEVICE_CODE_SUGGESTIONS)
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
            "custom_fields": custom_fields,
            "updated_at": datetime.utcnow(),
        }
        mongo.db.assets.update_one({"_id": asset["_id"]}, {"$set": update})
        audit_log("Assets", "Update", old_value=old, new_value=update, record_id=asset["_id"])
        flash("Asset updated.", "success")
        return redirect(url_for("assets.detail", asset_id=asset_id))
    return render_template("assets/form.html", form=form, title="Edit Asset",
                           asset=serialize_doc(asset), custom_fields=custom_fields,
                           site_by_prefix=SITE_BY_PREFIX,
                           site_short_codes=SITE_SHORT_CODES,
                           device_code_defaults=DEVICE_CODE_DEFAULTS,
                           device_code_suggestions=DEVICE_CODE_SUGGESTIONS)


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

        if asset.get("assigned_to"):
            emp_oid = safe_object_id(asset["assigned_to"])
            if emp_oid:
                current_employee = mongo.db.employees.find_one({"_id": emp_oid})

        available_employees = get_active_employees()

        validation_errors = []
        if asset.get("status") in ["Retired", "Disposed", "Lost"]:
            validation_errors.append(f"Asset is {asset['status']} - transfers are not allowed.")
        elif asset.get("status") == "Under Maintenance":
            validation_errors.append("Asset is under maintenance - transfer not recommended.")

        return render_template("assets/transfer.html",
            asset=serialize_doc(asset),
            current_employee=serialize_doc(current_employee),
            available_employees=[serialize_doc(e) for e in available_employees],
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

    # Resolve target up-front (validated by validate_asset_transfer above).
    primary_emp = None
    holder_emp = None
    if target_type == "employee":
        primary_emp, holder_emp = resolve_accountability_parties(target_id)
        if not primary_emp or not holder_emp:
            flash("Invalid employee", "error")
            return redirect(url_for("assets.transfer", asset_id=asset_id))
    else:
        target_type = "stockroom"

    def _do_transfer(session):
        update = {"updated_at": datetime.utcnow()}

        if target_type == "stockroom":
            update["status"] = "Available"
            update["assigned_to"] = None
            if old_assigned_to:
                close_accountability_for_asset(asset_id, session=session)

        elif target_type == "employee":
            update["status"] = "Assigned"
            update["assigned_to"] = str(holder_emp["_id"])
            secondary_ids = ([str(holder_emp["_id"])]
                             if str(holder_emp["_id"]) != str(primary_emp["_id"]) else None)
            create_accountability(str(primary_emp["_id"]), asset_id, "Direct Assignment",
                                  notes, secondary_employee_ids=secondary_ids, session=session)

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
                        "reason": reason,
                        "notes": notes,
                        "date": datetime.utcnow().isoformat(),
                        "by": current_user.username
                    }
                }
            },
            session=session
        )
        return {"assigned_to": update.get("assigned_to")}

    result = _run_in_transaction(_do_transfer)

    audit_log("Assets", "Transfer",
        old_value={"assigned_to": old_assigned_to},
        new_value={"assigned_to": result.get("assigned_to")},
        record_id=asset["_id"]
    )

    flash(f"Asset {asset['asset_tag']} transferred successfully.", "success")
    return redirect(url_for("assets.detail", asset_id=asset_id))


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

        primary_emp, holder_emp = resolve_accountability_parties(form.employee_id.data)
        if primary_emp is None or holder_emp is None:
            errors.append("Could not resolve the accountability parties for the selected employee.")

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

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template("accountabilities/form.html", form=form,
                                  title="New Accountability",
                                  employees=[serialize_doc(e) for e in employees],
                                  assets_available=[serialize_doc(a) for a in assets_available])

        primary_id = str(primary_emp["_id"])
        holder_id = str(holder_emp["_id"])
        secondary_ids = [holder_id] if holder_id != primary_id else []

        notes = form.notes.data
        acc_type = form.accountability_type.data
        effective_date = to_datetime(form.effective_date.data)

        def _do_create(session):
            acc_id = create_accountability(
                primary_id, asset_ids, acc_type,
                notes=notes, secondary_employee_ids=secondary_ids or None,
                session=session)
            if asset_oids:
                mongo.db.assets.update_many(
                    {"_id": {"$in": asset_oids}},
                    {"$set": {"status": "Assigned", "assigned_to": holder_id,
                              "updated_at": datetime.utcnow()}},
                    session=session
                )
            if effective_date:
                mongo.db.accountabilities.update_one(
                    {"_id": acc_id},
                    {"$set": {"effective_date": effective_date}},
                    session=session)
            return acc_id

        acc_id = _run_in_transaction(_do_create)
        audit_log("Accountabilities", "Create",
                   new_value={"type": acc_type, "employee_id": primary_id,
                              "holder_id": holder_id if secondary_ids else primary_id},
                   record_id=acc_id)

        if form.send_email.data and mail_configured():
            if primary_emp.get("email"):
                try:
                    acc_for_email = mongo.db.accountabilities.find_one({"_id": acc_id})
                    send_receive_email(acc_for_email, primary_emp,
                                       cc_emps=[holder_emp] if secondary_ids else None)
                    mongo.db.accountabilities.update_one(
                        {"_id": acc_id},
                        {"$set": {"email_sent_at": datetime.utcnow(),
                                  "email_sent_to": primary_emp["email"]},
                         "$push": {"remarks_timeline": {
                             "text": f"Receive email sent to {primary_emp['email']}",
                             "by": current_user.username,
                             "date": datetime.utcnow().isoformat()}}}
                    )
                    audit_log("Accountabilities", "Email Sent", record_id=acc_id)
                    flash("Accountability created and receive email sent.", "success")
                except Exception as exc:
                    logger.exception("Failed to send receive email")
                    flash(f"Accountability created, but email failed: {exc}", "error")
            else:
                flash("Accountability created, but the primary officer has no email address on file.", "warning")
        else:
            flash("Accountability record created.", "success")
        return redirect(url_for("accountabilities.list_view"))
    return render_template("accountabilities/form.html", form=form, title="New Accountability",
                            employees=[serialize_doc(e) for e in employees],
                            assets_available=[serialize_doc(a) for a in assets_available])


@accountabilities_bp.route("/<acc_id>")
@login_required
def detail(acc_id):
    acc = get_or_404("accountabilities", acc_id)
    emp = None
    emp_oid = safe_object_id(acc.get("employee_id"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})

    secondary_emps = []
    for sid in (acc.get("secondary_employee_ids") or []):
        soid = safe_object_id(sid)
        se = mongo.db.employees.find_one({"_id": soid}) if soid else None
        if se:
            secondary_emps.append(serialize_doc(se))

    asset_oids = [oid for oid in (safe_object_id(a) for a in acc.get("asset_ids", [])) if oid]
    assets = [serialize_doc(a) for a in mongo.db.assets.find({"_id": {"$in": asset_oids}})] if asset_oids else []

    return render_template("accountabilities/detail.html",
                            acc=serialize_doc(acc),
                            emp=serialize_doc(emp) if emp else None,
                            secondary_emps=secondary_emps,
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
    """(Re)send the 'Receive Assets' email to the primary employee, CC secondary holders."""
    acc = get_or_404("accountabilities", acc_id)
    emp_oid = safe_object_id(acc.get("employee_id"))
    emp = mongo.db.employees.find_one({"_id": emp_oid}) if emp_oid else None
    if not emp or not emp.get("email"):
        flash("Employee has no email address on file.", "error")
        return redirect(url_for("accountabilities.detail", acc_id=acc_id))
    if not mail_configured():
        flash("Email is not configured on this server (MAIL_SERVER not set).", "error")
        return redirect(url_for("accountabilities.detail", acc_id=acc_id))
    cc_emps = []
    for sid in (acc.get("secondary_employee_ids") or []):
        soid = safe_object_id(sid)
        se = mongo.db.employees.find_one({"_id": soid}) if soid else None
        if se and se.get("email"):
            cc_emps.append(se)
    try:
        send_receive_email(acc, emp, cc_emps=cc_emps or None)
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

ALLOWED_REMARK_TYPES = {"Assets"}


@remarks_bp.route("/add", methods=["POST"])
@login_required
@editor_required
def add():
    record_id = request.form.get("record_id")
    record_type = request.form.get("record_type")
    title = request.form.get("title", "").strip()
    owner = request.form.get("owner", "").strip()
    remark = request.form.get("remark", "").strip()
    if record_type not in ALLOWED_REMARK_TYPES:
        flash("Invalid remark target.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))
    if not (title and owner and remark):
        flash("Title, Owner and Remarks are all required.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))
    asset = mongo.db.assets.find_one({"_id": safe_object_id(record_id)})
    if not asset:
        flash("Asset not found.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))
    doc = {
        "record_id": record_id,
        "record_type": "Assets",
        "title": title,
        "owner": owner,
        "remark": remark,
        "author": current_user.username,
        "created_at": datetime.utcnow(),
    }
    mongo.db.remarks.insert_one(doc)
    audit_log("Assets", "Remark", new_value={"title": title, "remark": remark}, record_id=record_id)
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
                for ref_field in ("assigned_to", "asset_id", "employee_id"):
                    ref = val.get(ref_field)
                    r_oid = safe_object_id(ref)
                    if r_oid:
                        preload_oids.add(r_oid)
    name_map = _build_name_map(preload_oids)
    enriched_logs = [enrich_audit_log(serialize_doc(log), name_map) for log in logs]

    return render_template("audits/trail.html", logs=enriched_logs, page=page, total=total,
                            per_page=per_page, total_pages=total_pages, request=request)


# =============================================================================
# Blueprint: admin (read-only system integrity, admin only)
# =============================================================================
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.route("/integrity")
@login_required
@admin_required
def integrity():
    try:
        from integrity import check_consistency, chain_verify
        consistency = check_consistency(mongo.db)
        chain = chain_verify(mongo.db)
    except Exception:
        logger.exception("System integrity scan failed")
        consistency = {"ok": False, "issues": ["The integrity scan failed to run. "
                                               "See server logs for details."],
                       "assets": None, "employees": None, "accountabilities": None}
        chain = {"ok": False, "issues": [{"kind": "error", "_id": "-", "index": 0,
                                          "detail": "See server logs for details."}],
                 "count": None}
    return render_template("admin/integrity.html", consistency=consistency, chain=chain,
                            checked_at=datetime.utcnow())


@admin_bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    if request.method == "POST":
        toggle = request.form.get("scan_qr_action")
        if toggle in ("enable", "disable"):
            was = is_scan_qr_enabled()
            set_scan_qr_enabled(toggle == "enable", current_user.username)
            audit_log("Settings", "Toggle Scan QR Enabled",
                      old_value="ON" if was else "OFF",
                      new_value="ON" if toggle == "enable" else "OFF")
            flash("QR scanning " +
                  ("enabled for everyone." if toggle == "enable"
                   else "disabled until data is reconciled."), "success")
            return redirect(url_for("admin.settings"))
        employee_id = (request.form.get("employee_id") or "").strip()
        current = mongo.db.settings.find_one({"_id": "stockroom_custodian"})
        old_name = None
        if current and current.get("employee_id"):
            old_oid = safe_object_id(current["employee_id"])
            old_emp = mongo.db.employees.find_one({"_id": old_oid}) if old_oid else None
            old_name = old_emp.get("full_name") if old_emp else current["employee_id"]

        if not employee_id:
            mongo.db.settings.delete_one({"_id": "stockroom_custodian"})
            audit_log("Settings", "Set Stockroom Custodian",
                      old_value=old_name, new_value="(cleared)")
            flash("Stockroom custodian cleared.", "success")
            return redirect(url_for("admin.settings"))

        oid = safe_object_id(employee_id)
        emp = mongo.db.employees.find_one({"_id": oid, "status": "Active"}) if oid else None
        if not emp:
            flash("That employee is not an active, existing employee.", "error")
        else:
            mongo.db.settings.update_one(
                {"_id": "stockroom_custodian"},
                {"$set": {"employee_id": employee_id,
                          "updated_by": current_user.username,
                          "updated_at": datetime.utcnow()}},
                upsert=True)
            audit_log("Settings", "Set Stockroom Custodian",
                      old_value=old_name, new_value=emp.get("full_name"),
                      record_id=emp["_id"])
            flash(f"Stockroom custodian set to {emp.get('full_name')}.", "success")
            return redirect(url_for("admin.settings"))

    employees = mongo.db.employees.find({"status": "Active"}).sort("full_name", 1)
    custodian = get_stockroom_custodian(mongo.db)
    return render_template("admin/settings.html",
                           employees=[serialize_doc(e) for e in employees],
                           stockroom_custodian=serialize_doc(custodian) if custodian else None,
                           current_id=str(custodian["_id"]) if custodian else "",
                           scan_qr_enabled=is_scan_qr_enabled())


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
    employees = list(mongo.db.employees.find(
        {"status": "Active"}).sort("full_name", 1))
    return render_template("reports/import.html", failed_rows=failed_rows,
                           employees=employees)


@io_bp.route("/import/employees", methods=["GET", "POST"])
@login_required
@editor_required
def import_employees():
    """Import employees from CSV/XLSX.

    Required columns: employeeId, fullName. Optional (case-insensitive):
    site, email, position, division, department, section, group, status.
    Rows are matched on employeeId: an existing ID updates that employee,
    a new ID creates one. status defaults to Active.
    """
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("No file uploaded.", "error")
            return redirect(url_for("employees.list_view"))
        filename = f.filename.lower()
        try:
            import pandas as pd
            if filename.endswith(".csv"):
                df = pd.read_csv(f)
            elif filename.endswith(".xlsx"):
                df = pd.read_excel(f, engine="openpyxl")
            else:
                flash("Unsupported file format. Use .csv or .xlsx", "error")
                return redirect(url_for("employees.list_view"))
        except Exception:
            logger.exception("Failed to parse employee import file %s", filename)
            flash("Error reading file — check it's a valid CSV/XLSX.", "error")
            return redirect(url_for("employees.list_view"))

        colmap = {str(k).strip().lower(): str(k) for k in df.columns}
        missing = [c for c in ("employeeid", "fullname") if c not in colmap]
        if missing:
            pretty = {"employeeid": "employeeId", "fullname": "fullName"}
            flash("Missing required columns: " + ", ".join(pretty[m] for m in missing), "error")
            return redirect(url_for("employees.list_view"))

        STATUSES = {"Active", "Inactive", "Resigned", "On Leave"}

        def _cell(row, name):
            key = colmap.get(name)
            if key is None:
                return ""
            v = row.get(key)
            if v is None or (isinstance(v, float) and pd.isna(v)):
                return ""
            return str(v).strip()

        created = updated = 0
        failed = []
        bad_status = 0
        for idx, row in df.iterrows():
            emp_id = _cell(row, "employeeid")
            full_name = _cell(row, "fullname")
            if not emp_id or not full_name:
                failed.append({
                    "row": {str(k): str(v) for k, v in row.items()},
                    "reason": "Missing employeeId or fullName",
                })
                continue
            status = _cell(row, "status") or "Active"
            if status not in STATUSES:
                bad_status += 1
                status = "Active"
            values = {k: _cell(row, k)
                      for k in ("site", "email", "position", "division", "department", "section", "group")}
            values = {k: v for k, v in values.items() if v}
            existing = mongo.db.employees.find_one({"employee_id": emp_id})
            if existing:
                update = dict(values, full_name=full_name, status=status,
                              updated_at=datetime.utcnow())
                mongo.db.employees.update_one({"_id": existing["_id"]}, {"$set": update})
                updated += 1
            else:
                doc = {
                    "employee_id": emp_id,
                    "full_name": full_name,
                    "status": status,
                    "created_at": datetime.utcnow(),
                    "updated_at": datetime.utcnow(),
                }
                doc.update(values)
                mongo.db.employees.insert_one(doc)
                created += 1

        clear_reference_data("active_employees")
        audit_log("Employees", "Import",
                  new_value={"file": filename, "created": created, "updated": updated, "failed": len(failed)})
        msg = f"Employee import complete: {created} created, {updated} updated, {len(failed)} failed."
        if bad_status:
            msg += f" {bad_status} had an unrecognized status (set to Active)."
        if failed:
            msg += " First failures: " + "; ".join(r["reason"] for r in failed[:3]) + "."
        flash(msg, "warning" if failed else "success")
        session["import_failed"] = failed[:50]
        return redirect(url_for("employees.list_view"))

    return redirect(url_for("employees.list_view"))


# ---------------------------------------------------------------------------
# Async export jobs: bulk exports/PDFs are generated in a background thread so
# the request never blocks on pandas/reportlab. Completed payloads live in
# memory, finish quickly on a LAN-sized database, and are capped + TTL-evicted.
EXPORT_JOB_TTL = 600
EXPORT_JOB_MAX = 20
export_jobs = {}


def _export_job_new(kind, username="system"):
    job_id = uuid4().hex
    now = time.time()
    for jid in [jid for jid, s in export_jobs.items()
                if s["status"] in ("done", "error") and now - s["created_at"] > EXPORT_JOB_TTL]:
        export_jobs.pop(jid, None)
    while len(export_jobs) >= EXPORT_JOB_MAX:
        export_jobs.pop(next(iter(export_jobs)), None)
    export_jobs[job_id] = {"status": "pending", "kind": kind, "username": username,
                           "filename": None, "mimetype": None, "buf": None,
                           "error": None, "created_at": now}
    return job_id


def _start_export(job_id):
    app = current_app._get_current_object()
    threading.Thread(target=_run_export_job, args=(app, job_id), daemon=True).start()


def _run_export_job(app, job_id):
    job = export_jobs.get(job_id)
    if not job:
        return
    try:
        with app.app_context():
            filename, buf_bytes, mimetype = _build_export(job["kind"], job.get("username"))
        job.update({"status": "done", "filename": filename,
                    "mimetype": mimetype, "buf": buf_bytes})
    except Exception:
        logging.getLogger(__name__).exception("Export job %s failed", job_id)
        job.update({"status": "error", "error": "Export generation failed — see logs."})


def _build_export(kind, username=None):
    """Produce a (filename, bytes, mimetype) payload inside the job thread."""
    import pandas as pd

    if kind == "assets_xlsx":
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
            "Warranty Expiry": a.get("warranty_expiry", ""),
            "Created": a.get("created_at", ""),
        } for a in mongo.db.assets.find()]
        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Assets")
        buf.seek(0)
        audit_log("Assets", "Export", username=username)
        return ("assets_export.xlsx", buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if kind == "employees_xlsx":
        rows = [{"Employee ID": e.get("employee_id"), "Full Name": e.get("full_name"),
                 "Email": e.get("email"), "Department": e.get("department"),
                 "Position": e.get("position"), "Site": e.get("site"),
                 "Status": e.get("status")} for e in mongo.db.employees.find()]
        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False, sheet_name="Employees")
        buf.seek(0)
        audit_log("Employees", "Export", username=username)
        return ("employees_export.xlsx", buf.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    if kind == "assets_pdf":
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import inch

        assets = list(mongo.db.assets.find({"status": {"$nin": ["Disposed", "Retired"]}},
                                           sort=[("asset_tag", 1)]))
        owner_ids = {safe_object_id(a.get("assigned_to")) for a in assets if a.get("assigned_to")}
        owner_ids.discard(None)
        owner_map = {}
        if owner_ids:
            for e in mongo.db.employees.find({"_id": {"$in": list(owner_ids)}},
                                             {"full_name": 1, "_id": 1}):
                owner_map[str(e["_id"])] = e.get("full_name", "")

        buf = io.BytesIO()
        doc = SimpleDocTemplate(buf, pagesize=letter,
                                leftMargin=0.7 * inch, rightMargin=0.7 * inch,
                                topMargin=0.7 * inch, bottomMargin=0.7 * inch)
        styles = getSampleStyleSheet()
        elements = []
        _pdf_header(elements, "KPI ICT Inventory", styles,
                    subtitle=f'Inventory List \u2014 generated {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} UTC')
        data = [["No.", "Asset Tag", "Serial Number", "Type", "Model", "Location", "Status", "Assigned To"]]
        for i, a in enumerate(assets, 1):
            oid = safe_object_id(a.get("assigned_to"))
            data.append([str(i),
                         a.get("asset_tag", "") or "\u2014",
                         a.get("serial_number", "") or "\u2014",
                         a.get("device_type", "") or "\u2014",
                         a.get("model_name", "") or "\u2014",
                         a.get("location", "") or "\u2014",
                         a.get("status", "") or "\u2014",
                         owner_map.get(str(oid), "\u2014") if oid else "\u2014"])
        t = Table(data, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 7.5),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#EEF2FF")]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(t)
        elements.append(Spacer(1, 0.2 * inch))
        elements.append(Paragraph(f"Total items: {len(assets)}", styles["Normal"]))
        elements.append(Spacer(1, 0.35 * inch))
        sig = Table([["<b>Prepared by:</b>", "<b>Noted by:</b>"],
                     ["\u200b", "\u200b"]],
                    colWidths=[2.7 * inch, 2.7 * inch],
                    rowHeights=[0.3 * inch, 0.6 * inch])
        sig.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 10),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 1), (-1, 1), 0.5, colors.black),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        elements.append(sig)
        doc.build(elements)
        buf.seek(0)
        audit_log("Reports", "Export PDF Assets", username=username)
        return ("KPI_ICT_Inventory.pdf", buf.getvalue(), "application/pdf")

    raise ValueError("Unknown export kind: %r" % kind)


def _job_created(job_id):
    return jsonify({"success": True, "job_id": job_id})


def _job_status(job_id):
    job = export_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found."}), 404
    return jsonify({"success": True, "status": job["status"],
                    "error": job.get("error")})


def _job_download(job_id):
    job = export_jobs.get(job_id)
    if not job or job["status"] != "done":
        abort(404)
    return _deliver_download(job["filename"], job["buf"], job["mimetype"])


@io_bp.route("/export/assets", methods=["GET", "POST"])
@login_required
def export_assets():
    if request.method == "POST":
        job_id = _export_job_new("assets_xlsx",
                                 username=current_user.username if current_user.is_authenticated else "system")
        _start_export(job_id)
        return _job_created(job_id)
    return _deliver_download(*_build_export("assets_xlsx"))


@io_bp.route("/export/assets/status/<job_id>")
@login_required
def export_assets_status(job_id):
    return _job_status(job_id)


@io_bp.route("/export/assets/download/<job_id>")
@login_required
def export_assets_download(job_id):
    return _job_download(job_id)


@io_bp.route("/export/employees", methods=["GET", "POST"])
@login_required
def export_employees():
    if request.method == "POST":
        job_id = _export_job_new("employees_xlsx",
                                 username=current_user.username if current_user.is_authenticated else "system")
        _start_export(job_id)
        return _job_created(job_id)
    return _deliver_download(*_build_export("employees_xlsx"))


@io_bp.route("/export/employees/status/<job_id>")
@login_required
def export_employees_status(job_id):
    return _job_status(job_id)


@io_bp.route("/export/employees/download/<job_id>")
@login_required
def export_employees_download(job_id):
    return _job_download(job_id)


def _pdf_header(elements, title, styles, subtitle=None):
    from reportlab.lib.units import inch
    from reportlab.platypus import Paragraph, Spacer
    elements.append(Paragraph(title, styles["Title"]))
    elements.append(Paragraph(subtitle or f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC", styles["Normal"]))
    elements.append(Spacer(1, 0.3 * inch))


@io_bp.route("/reports/assets/pdf", methods=["GET", "POST"])
@login_required
def report_assets_pdf():
    if request.method == "POST":
        job_id = _export_job_new("assets_pdf",
                                 username=current_user.username if current_user.is_authenticated else "system")
        _start_export(job_id)
        return _job_created(job_id)
    return _deliver_download(*_build_export("assets_pdf"))


@io_bp.route("/reports/assets/pdf/status/<job_id>")
@login_required
def report_assets_pdf_status(job_id):
    return _job_status(job_id)


@io_bp.route("/reports/assets/pdf/download/<job_id>")
@login_required
def report_assets_pdf_download(job_id):
    return _job_download(job_id)


@io_bp.route("/reports/accountability/<acc_id>/pdf")
@login_required
def accountability_pdf(acc_id):
    acc = get_or_404("accountabilities", acc_id)
    emp = None
    emp_oid = safe_object_id(acc.get("employee_id"))
    if emp_oid:
        emp = mongo.db.employees.find_one({"_id": emp_oid})

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


@io_bp.route("/reports/stickers/employees")
@login_required
def employee_stickers():
    """Print sheet for employee (bundle) QR stickers — many fit per A4 page.

    Renders every sticker in a wrap-grid; shrink / print and it packs multiple
    onto one sheet. Pass ?ids=<emp_id>&ids=<emp_id> for a specific set,
    otherwise the first 50 active employees are shown.
    """
    emp_ids = request.args.getlist("ids")
    if not emp_ids:
        employees = list(mongo.db.employees.find({"status": "Active"})
                         .sort("full_name", 1).limit(50))
    else:
        oids = [oid for oid in (safe_object_id(i) for i in emp_ids) if oid]
        employees = list(mongo.db.employees.find({"_id": {"$in": oids}})) if oids else []
    stickers = [{"emp": serialize_doc(e), "qr": generate_employee_bundle_qr(e)}
                for e in employees]
    return render_template("reports/stickers_employees.html", stickers=stickers)


def _sticker_cart_ids():
    """Order-unique asset _id strings queued in the current user's sticker cart."""
    return list(dict.fromkeys(session.get("sticker_cart", []) or []))


@io_bp.route("/reports/stickers/cart")
@login_required
def sticker_cart():
    """Sticker cart page — pending asset tags queued for one print run."""
    ids = _sticker_cart_ids()
    oids = [oid for oid in (safe_object_id(i) for i in ids) if oid]
    assets = list(mongo.db.assets.find({"_id": {"$in": oids}})) if oids else []
    by_id = {str(a["_id"]): a for a in assets}
    items = [{"asset": serialize_doc(by_id[i]), "qr": generate_asset_qr(by_id[i])}
             for i in ids if i in by_id]
    return render_template("reports/sticker_cart.html", items=items)


@io_bp.route("/reports/stickers/cart/add", methods=["POST"])
@login_required
def sticker_cart_add():
    payload = request.get_json(silent=True) or {}
    ids = payload.get("ids") or request.form.getlist("ids")
    cart = _sticker_cart_ids()
    present = set(cart)
    for i in ids:
        if i and i not in present:
            cart.append(i)
            present.add(i)
    session["sticker_cart"] = cart
    return jsonify({"count": len(cart), "added": len([i for i in ids if i])})


@io_bp.route("/reports/stickers/cart/remove", methods=["POST"])
@login_required
def sticker_cart_remove():
    asset_id = request.form.get("asset_id")
    if asset_id:
        session["sticker_cart"] = [i for i in _sticker_cart_ids() if i != asset_id]
    return redirect(url_for("io.sticker_cart"))


@io_bp.route("/reports/stickers/cart/clear", methods=["POST"])
@login_required
def sticker_cart_clear():
    session.pop("sticker_cart", None)
    return redirect(url_for("io.sticker_cart"))


@io_bp.route("/reports/stickers/cart/print")
@login_required
def sticker_cart_print():
    ids = _sticker_cart_ids()
    oids = [oid for oid in (safe_object_id(i) for i in ids) if oid]
    assets = list(mongo.db.assets.find({"_id": {"$in": oids}})) if oids else []
    by_id = {str(a["_id"]): a for a in assets}
    ordered = [by_id[i] for i in ids if i in by_id]
    stickers = [{"asset": serialize_doc(a), "qr": generate_asset_qr(a)} for a in ordered]
    return render_template("reports/stickers.html", stickers=stickers, sticker_type="Asset")


@io_bp.route("/reports/accountability-sheet/<employee_id>")
@login_required
def accountability_sheet(employee_id):
    """Print-friendly one-page accountability sheet.

    The bundle QR is the same stable link the employee gets anywhere — this page
    is simply its paper home: name + QR + the full, live asset list they hold.
    """
    emp = get_or_404("employees", employee_id)
    scope = accountability_scope(employee_id)
    assets = list(mongo.db.assets.find({"assigned_to": {"$in": scope}}))
    assets.sort(key=lambda a: ((a.get("device_type") or "").lower(),
                               (a.get("asset_tag") or "").lower()))
    primary_emp = None
    if emp.get("responsible_employee_id"):
        primary_oid = safe_object_id(emp["responsible_employee_id"])
        if primary_oid:
            primary_emp = mongo.db.employees.find_one({"_id": primary_oid})
    return render_template(
        "reports/accountability_sheet.html",
        emp=serialize_doc(emp),
        primary_emp=serialize_doc(primary_emp) if primary_emp else None,
        assets=[serialize_doc(a) for a in assets],
        qr=generate_employee_bundle_qr(emp),
        generated_by=current_user.username,
        generated_at=datetime.utcnow().isoformat())


@io_bp.route("/reports/stickers/employee/<emp_id>/card")
@login_required
def employee_sticker_card(emp_id):
    """Single employee sticker card, JSON-fetched by the sheet's 'Add QR' box."""
    emp = get_or_404("employees", emp_id)
    return render_template("reports/_employee_sticker_card.html",
                           emp=serialize_doc(emp),
                           qr=generate_employee_bundle_qr(emp))


def _decode_qrs_from_image_bytes(buf_bytes):
    """Return every QR payload found in a single image (cv2, no OCR).

    Used by the scan-back importer: the paper form's rows carry signed QR
    inventory tokens, so the *QR* is the machine-readable authority — we never
    need to read handwriting.
    """
    import cv2
    import numpy as np
    arr = np.frombuffer(buf_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    detector = cv2.QRCodeDetector()
    found = set()
    # OpenCV 4.x returns (decoded_info, points, straight_qrcode);
    # OpenCV 5.x returns 4 elements. Grab whichever tuple slot holds the payloads.
    out = detector.detectAndDecodeMulti(img)
    payloads = []
    if isinstance(out, tuple):
        for item in out:
            if isinstance(item, list):
                if not payloads and all(isinstance(p, str) for p in item):
                    payloads = item
            elif isinstance(item, np.ndarray) and item.ndim == 1 and len(item) and \
                    item.dtype.kind in ("U", "O", "S"):
                if not payloads:
                    payloads = list(item)
    elif isinstance(out, list):
        payloads = out
    for payload in payloads:
        if payload:
            found.add(str(payload))
    return list(found)


def _rasterize_pdf_pages(buf_bytes, scale=2.0):
    """Rasterize every page of a PDF scan to PNG bytes (pypdfium2)."""
    from pypdfium2 import PdfDocument
    pages = []
    with PdfDocument(buf_bytes) as doc:
        for page in doc:
            bitmap = page.render(scale=scale)
            pil_img = bitmap.to_pil()
            import io as _io
            out = _io.BytesIO()
            pil_img.save(out, format="PNG")
            pages.append(out.getvalue())
    return pages


def _scan_all_qr_payloads(buf_bytes, filename):
    """Rasterize + decode every QR across an uploaded scan (PDF or image)."""
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        pages = _rasterize_pdf_pages(buf_bytes)
    else:
        pages = [buf_bytes]
    found = set()
    for page in pages:
        found.update(_decode_qrs_from_image_bytes(page))
    return found


# -----------------------------------------------------------------------------
# Asset Intake Form — print + scan-back creation (no handwriting OCR needed).
# Each physical row carries a tiny QR acting as a geometric anchor; the
# operator ticks the device-type checkbox and writes serial/model by hand.
# On upload we re-locate every row from its QR quad, sample each checkbox cell
# for ink density, so the device type is auto-detected (checkbox darkness is
# the ONLY machine-read signal — handwriting goes into the batch preview grid).
# -----------------------------------------------------------------------------
INTAKE_DEVICE_TYPES = ["Laptop", "Desktop", "Printer", "Scanner", "Mouse",
                       "Keyboard", "Headset", "Company Phone", "Type C Hub"]
_INTAKE_ROWS_DEFAULT = 20
_INTAKE_ROWS_PAGE = 10
_INTAKE_ROW_H = 55.0              # pt height of one form row (room to write by hand)
_INTAKE_BOTTOM_MARGIN = 60.0      # floor: never let the row content touch the paper edge
_INTAKE_SCAN_SCALE = 4.0          # rasterisation zoom used when detecting intake scans
_INTAKE_QR_X = 36.0               # row-anchor QR left x (form pt)
_INTAKE_QR_SIZE = 24.0            # small enough to fit a 55pt row incl. its label
_INTAKE_QR_TOP_D = 13.0           # QR top gap from row top (form pt)
_INTAKE_CHK_X0 = 118.0            # left x of the first checkbox column
_INTAKE_CHK_SIZE = 15.0           # checkbox square size (form pt)
_INTAKE_CHK_CY = 30.0             # checkbox centre y from the row top
_INTAKE_PAGE_W = 595.27
_INTAKE_PAGE_H = 841.89
_INTAKE_MARGIN_R = 34.0
_INTAKE_CHK_STEP = (_INTAKE_PAGE_W - _INTAKE_MARGIN_R - _INTAKE_CHK_X0) / \
    len(INTAKE_DEVICE_TYPES)


def _intake_qr_payload(row_no):
    """QR payload for one intake row (fixed short form keeps the QR at
    version 1 = 21 modules so the affine checkbox geometry stays calibrated)."""
    return "INTK-ROW-%03d" % int(row_no)


def _intake_checkbox_form_xy(row_no, type_idx):
    """Centre of a checkbox in the QR-local frame (pt), QR top-left = (0,0).

    The offset from the row's QR anchor is fixed for every row, so no row_no
    math is needed — only the horizontal position moves with the column.
    """
    x = (_INTAKE_CHK_X0 - _INTAKE_QR_X) + type_idx * _INTAKE_CHK_STEP + \
        _INTAKE_CHK_SIZE / 2.0
    y = _INTAKE_CHK_CY
    return x, y


@io_bp.route("/reports/intake-form/pdf")
@login_required
def inventory_intake_form_pdf():
    """Print-ready blank Asset Intake Form with QR-anchored checkbox rows."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdfcanvas
    import base64 as b64

    rows = max(1, min(60, request.args.get("rows", _INTAKE_ROWS_DEFAULT, type=int)))
    emp = None
    emp_id_arg = request.args.get("employee_id", "")
    if emp_id_arg:
        emp = mongo.db.employees.find_one({"_id": safe_object_id(emp_id_arg)})
    buf = io.BytesIO()
    c = pdfcanvas.Canvas(buf, pagesize=A4)

    def draw_page(start, count):
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(_INTAKE_QR_X, _INTAKE_PAGE_H - 44, "KPI ICT ASSET INTAKE FORM")
        c.setFont("Helvetica", 8.5)
        c.drawString(
            _INTAKE_QR_X, _INTAKE_PAGE_H - 58,
            "Tick one device type per row and write the serial/model/remarks by hand; "
            "scan this page back and upload it via 'Import Intake Scan'.")
        if emp:
            c.setFont("Helvetica-Bold", 9)
            c.drawString(_INTAKE_QR_X, _INTAKE_PAGE_H - 71,
                         "Employee: %s  ·  ID: %s"
                         % (emp.get("full_name", ""), emp.get("employee_id", "")))
            c.setFont("Helvetica", 8)
            c.drawString(
                _INTAKE_QR_X, _INTAKE_PAGE_H - 81,
                "Department: %s  ·  Position: %s  ·  Site: %s"
                % (emp.get("department", "—"), emp.get("position", "—"),
                   emp.get("site", "—")))
        dy = 22 if emp else 0
        c.setStrokeColor(colors.grey)
        c.setLineWidth(0.6)
        lx = _INTAKE_CHK_X0
        lcy = _INTAKE_PAGE_H - 84 - dy
        c.setFont("Helvetica", 6.5)
        for t in INTAKE_DEVICE_TYPES:
            c.rect(lx, lcy, _INTAKE_CHK_SIZE, _INTAKE_CHK_SIZE)
            c.drawString(lx, lcy - 9, t[:14])
            lx += _INTAKE_CHK_STEP
        c.setFont("Helvetica", 8)

        top0 = _INTAKE_PAGE_H - 106 - dy
        block_content = (count - 1) * _INTAKE_ROW_H + 50.0
        if top0 - block_content >= _INTAKE_BOTTOM_MARGIN:
            top0 = (top0 + block_content) / 2.0

        for i in range(start, start + count):
            top = top0 - (i - start) * _INTAKE_ROW_H
            if top < 70:
                break
            qr_b64 = generate_qr(_intake_qr_payload(i))
            qr_png = b64.b64decode(qr_b64)
            c.drawImage(ImageReader(io.BytesIO(qr_png)),
                        _INTAKE_QR_X, top - _INTAKE_QR_SIZE,
                        width=_INTAKE_QR_SIZE, height=_INTAKE_QR_SIZE,
                        preserveAspectRatio=True, mask="auto")
            c.setFont("Helvetica", 7)
            c.drawString(_INTAKE_QR_X, top - _INTAKE_QR_SIZE - 9, "r%03d" % i)
            cx = _INTAKE_CHK_X0
            for _t in range(len(INTAKE_DEVICE_TYPES)):
                c.rect(cx, top - _INTAKE_CHK_CY - _INTAKE_CHK_SIZE / 2.0,
                       _INTAKE_CHK_SIZE, _INTAKE_CHK_SIZE)
                cx += _INTAKE_CHK_STEP
            c.setLineWidth(0.5)
            c.drawString(_INTAKE_CHK_X0, top - 42, "Serial:")
            c.line(_INTAKE_CHK_X0 + 34, top - 39.5,
                   _INTAKE_PAGE_W - _INTAKE_MARGIN_R, top - 39.5)
            c.drawString(_INTAKE_PAGE_W - _INTAKE_MARGIN_R - 190,
                         top - 42, "Model:")
            c.line(_INTAKE_PAGE_W - _INTAKE_MARGIN_R - 150,
                   top - 39.5,
                   _INTAKE_PAGE_W - _INTAKE_MARGIN_R, top - 39.5)
            c.drawString(_INTAKE_CHK_X0, top - 50, "Remarks:")
            c.line(_INTAKE_CHK_X0 + 40, top - 47.5,
                   _INTAKE_PAGE_W - _INTAKE_MARGIN_R, top - 47.5)

    drawn = 0
    while drawn < rows:
        n = min(_INTAKE_ROWS_PAGE, rows - drawn)
        draw_page(drawn, n)
        drawn += n
        if drawn < rows:
            c.showPage()
    c.save()
    buf.seek(0)
    return _deliver_download("asset_intake_form_%drows.pdf" % rows,
                             buf.getvalue(), "application/pdf")


def _decode_qrs_with_positions(buf_bytes):
    """Return [(payload, quad)] for a single image; quad = (4, 2) float pixels.

    Robustly handles the OpenCV QRCodeDetector.detectAndDecodeMulti return
    shape across 4.x/5.x: the decoded payloads show up as a list/tuple/ndarray
    and the quad array is the (N, 4, 2) ndarray in the same tuple.
    """
    import cv2
    import numpy as np
    arr = np.frombuffer(buf_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    n_found = set()

    def _detect(sample):
        out = cv2.QRCodeDetector().detectAndDecodeMulti(sample)
        payloads, quads = [], None
        items = out if isinstance(out, tuple) else (out,)
        for item in items:
            if isinstance(item, (list, tuple)):
                if not payloads and item and all(isinstance(p, str) for p in item):
                    payloads = item
            elif isinstance(item, np.ndarray):
                if item.ndim == 3 and item.shape[1:] == (4, 2):
                    quads = item
                elif item.ndim == 1 and len(item) and \
                        item.dtype.kind in ("U", "O", "S") and not payloads:
                    payloads = list(item)
        found = []
        if quads is not None:
            for i, payload in enumerate(payloads):
                if payload and i < len(quads):
                    found.append((str(payload), np.array(quads[i],
                                                         dtype=np.float32)))
        return found

    def _nominal(item):
        try:
            return str(item[0])
        except Exception:
            return ""

    res = []
    for item in _detect(img):
        if _nominal(item) not in n_found:
            res.append(item)
            n_found.add(_nominal(item))

    big = cv2.resize(img, None, fx=2.0, fy=2.0,
                     interpolation=cv2.INTER_CUBIC)
    for payload, quad in _detect(big):
        if payload in n_found:
            continue
        res.append((payload, np.array(quad, dtype=np.float32) / 2.0))
        n_found.add(payload)
    return res


def _order_qr_quad(quad):
    """Return the four QR corners as [top-left, top-right, bottom-right, bottom-left].

    OpenCV does NOT guarantee a fixed meaning for the returned quad corners, so
    we re-derive the order: sort by polar angle around the centroid, then rotate
    so the list starts at the topmost corner (the QR's top-left).
    """
    import numpy as np
    pts = np.array(quad, dtype=np.float32)
    c = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0])
    s = np.argsort(angles).tolist()
    tl = int(np.argmin(pts[:, 1]))
    k = s.index(tl)
    seq = s[k:] + s[:k]
    return pts[seq]


def _intake_detect_filled_checkboxes(page_bytes, row_no):
    """Decode one page: for the target INTK row, return detected device types.

    The row's tiny QR anchors its location/scale; each checkbox is sampled for
    ink density with the affine built from the (angle-sorted) anchor quad.
    """
    import cv2
    import numpy as np
    arr = np.frombuffer(page_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return []
    target = []
    for payload, quad in _decode_qrs_with_positions(page_bytes):
        if not payload.startswith("INTK-ROW-"):
            continue
        if int(payload.split("-")[-1]) == row_no:
            target.append(quad)
    if not target:
        return []
    affine = _intake_affine_from_qr(_order_qr_quad(target[0]))
    found = []
    for i in range(len(INTAKE_DEVICE_TYPES)):
        fx, fy = _intake_checkbox_form_xy(row_no, i)
        half = _INTAKE_CHK_SIZE * 0.5   # sample the whole box, not just the core
        corners = np.float32([[[fx - half, fy - half]],
                              [[fx + half, fy - half]],
                              [[fx + half, fy + half]],
                              [[fx - half, fy + half]]]).reshape(4, 1, 2)
        pts = cv2.transform(corners, affine).reshape(4, 2)
        mask = np.zeros(img.shape[:2], dtype=np.uint8)
        cv2.fillConvexPoly(mask, np.int32(pts), 255)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        px = gray[mask > 0]
        if px.size and float(np.mean(px < 128)) > 0.30:
            found.append(INTAKE_DEVICE_TYPES[i])
    return found


def _intake_affine_from_qr(qr_quad):
    """Build an affine (QR-local form-pt => image-px) from the anchor quad.

    OpenCV reports the QR's 21-module DATA square (the 2-module quiet zone is
    outside its quad), so the form corner tuple is placed at the data-region
    border of the full 25-module anchor square rather than at (0,0). That
    keeps the mapping aligned with where the checkbox offsets actually live.
    """
    import cv2
    import numpy as np
    border = _INTAKE_QR_SIZE * 2.0 / 25.0
    edge = _INTAKE_QR_SIZE - 2.0 * border
    form_pts = np.float32([[border, border],
                           [border + edge, border],
                           [border, border + edge]]).reshape(3, 1, 2)
    img_pts = np.float32([qr_quad[0], qr_quad[1], qr_quad[3]]).reshape(3, 1, 2)
    return cv2.getAffineTransform(form_pts, img_pts)


def _detect_intake_rows(buf_bytes, filename):
    """Decode an intake-form scan -> list of rows with detected checkboxes.

    Returns: [{row_no, token, device_types: [..], asset_tag, serial, model, remarks}]
    Only QR anchors + checkbox ink density drive detection; handwriting is left
    blank for the operator to fill in the batch preview grid.
    """
    lower = (filename or "").lower()
    if lower.endswith(".pdf"):
        pages = _rasterize_pdf_pages(buf_bytes, scale=_INTAKE_SCAN_SCALE)
    else:
        pages = [buf_bytes]

    seen = {}
    seen_list = []
    for page_bytes in pages:
        for payload, _quad in _decode_qrs_with_positions(page_bytes):
            if not payload.startswith("INTK-ROW-"):
                continue
            row_no = int(payload.split("-")[-1])
            if row_no not in seen:
                seen[row_no] = \
                    _intake_detect_filled_checkboxes(page_bytes, row_no)
                seen_list.append({"row_no": row_no, "token": payload,
                                  "device_types": seen[row_no],
                                  "asset_tag": "", "serial": "",
                                  "model": "", "remarks": ""})
    seen_list.sort(key=lambda r: r["row_no"])
    return seen_list


@io_bp.route("/reports/intake-import", methods=["GET", "POST"])
@login_required
def inventory_intake_import():
    """Batch importer for the blank Asset Intake Form (QR-anchored checkboxes).

    Phase 1 (upload): rasterize the scan -> locate every row's QR anchor ->
    sample the 9 device-type checkboxes for ink -> build an **editable preview**
    where the operator types the serial/model/remarks and may correct the
    auto-detected device type. The operator also picks the **employee** the
    printed form belongs to (the form prints the employee's name/header), so the
    new assets can be assigned without any handwriting OCR.

    Phase 2 (confirm): every checked row becomes a NEW asset with an
    auto-generated tag (or the tag typed in the preview grid), a created-by
    history entry and an audit log. When an employee was selected the assets are
    also set status Assigned, given assigned_to, and added to the employee's
    accountability. Generic scans without an employee stay Available.
    **No DB write happens during upload.**
    """
    if not is_scan_qr_enabled():
        return render_template("scan/disabled.html",
                               feature="asset intake import")

    if request.method == "POST":
        # ---- PHASE 2: operator reviewed the preview and confirmed.
        if request.form.get("confirm") == "1":
            created, skipped, dupes = [], 0, 0
            now = datetime.utcnow()
            who = current_user.username if current_user.is_authenticated \
                else "system"
            emp_doc = None
            emp_id_arg = request.form.get("employee_id", "").strip()
            if emp_id_arg:
                emp_doc = mongo.db.employees.find_one(
                    {"_id": safe_object_id(emp_id_arg)})
                if not emp_doc:
                    emp_doc = None
            used_tags = set()
            used_serials = set()
            for rn in request.form.getlist("row_no"):
                device_type = request.form.get("device_type_%s" % rn, "").strip()
                asset_tag = request.form.get("asset_tag_%s" % rn, "").strip()
                serial = request.form.get("serial_%s" % rn, "").strip()
                model = request.form.get("model_%s" % rn, "").strip()
                remarks = request.form.get("remarks_%s" % rn, "").strip()
                if not device_type:
                    skipped += 1
                    continue
                if serial:
                    existing = mongo.db.assets.find_one(
                        {"serial_number": serial})
                    if existing or serial in used_serials:
                        dupes += 1
                        continue
                    used_serials.add(serial)
                if not asset_tag:
                    dc = DEVICE_CODE_DEFAULTS.get(device_type, "X")
                    asset_tag = "INTK-%s-%s" % (
                        dc, next_tag_number("INTK", dc))
                    while asset_tag in used_tags:
                        asset_tag = "INTK-%s-%s" % (
                            dc, next_tag_number("INTK", dc))
                if mongo.db.assets.find_one({"asset_tag": asset_tag}) or \
                        asset_tag in used_tags:
                    dupes += 1
                    continue
                used_tags.add(asset_tag)
                doc = {
                    "asset_tag": asset_tag,
                    "endpoint_name": "",
                    "serial_number": serial or "",
                    "device_type": device_type,
                    "model_name": model or "",
                    "status": "Assigned" if emp_doc else "Available",
                    "notes": remarks or "",
                    "assigned_to": str(emp_doc["_id"]) if emp_doc else None,
                    "history": [{
                        "event": "Asset Created",
                        "method": "Intake scan import",
                        "by": who,
                        "at": now,
                        "details": "Created from scanned intake form row r%03d" % int(rn),
                    }],
                    "created_at": now,
                    "updated_at": now,
                }
                if emp_doc:
                    doc["history"].append({
                        "event": "Assigned",
                        "method": "Intake scan import",
                        "by": who,
                        "at": now,
                        "details": "Assigned to %s (%s) from scanned form"
                                   % (emp_doc.get("full_name", ""),
                                      emp_doc.get("employee_id", "")),
                    })
                res = mongo.db.assets.insert_one(doc)
                if emp_doc:
                    create_accountability(str(emp_doc["_id"]),
                                          res.inserted_id, "Intake Scan Import",
                                          notes=remarks or "Auto-assigned from scanned intake form")
                audit_log("Assets", "Create",
                          new_value={"asset_tag": asset_tag,
                                     "serial_number": doc["serial_number"],
                                     "assigned_to": doc["assigned_to"]},
                          record_id=res.inserted_id)
                created.append({"asset_tag": asset_tag,
                                "token": _intake_qr_payload(int(rn)),
                                "status": doc["status"]})

            result = {"total": len(request.form.getlist("row_no")),
                      "created": len(created),
                      "skipped": skipped, "duplicates": dupes}
            if created:
                flash("Intake import: %d asset(s) created, %d skipped, %d duplicate(s)."
                      % (len(created), skipped, dupes), "success")
            return render_template("reports/intake_result.html",
                                   result=result, created=created)

        # ---- PHASE 1: upload a scan -> detect -> editable batch preview.
        f = request.files.get("file")
        if not f or not f.filename:
            flash("No file uploaded.", "error")
            return redirect(url_for("io.inventory_intake_import"))
        selected_emp_id = request.form.get("employee_id", "").strip()
        selected_emp_name = ""
        if selected_emp_id:
            sel_emp = mongo.db.employees.find_one(
                {"_id": safe_object_id(selected_emp_id)})
            if sel_emp:
                selected_emp_name = sel_emp.get("full_name", "")
            else:
                selected_emp_id = ""
        raw = f.read()
        try:
            rows = _detect_intake_rows(raw, f.filename)
        except Exception:
            logger.exception("Failed to detect intake form %s", f.filename)
            rows = []
        if not rows:
            flash("No readable intake-form QR anchors found in that scan. "
                  "Use a clear, well-lit photo or a 300 dpi scan.", "error")
            return redirect(url_for("io.inventory_intake_import"))
        return render_template("reports/intake_preview.html",
                               rows=rows, scan_name=f.filename,
                               device_types=INTAKE_DEVICE_TYPES,
                               employee_id=selected_emp_id,
                               employee_name=selected_emp_name)
    return render_template("reports/intake_import.html",
                               employees=get_active_employees())


@io_bp.route("/reports/employee-inventory-form/<employee_id>/pdf")
@login_required
def employee_inventory_form_pdf(employee_id):
    """KPI ICT Inventory paper form (one per employee).

    Every accountability row is rendered with:
      * a checkbox column (handwritten by the counter when physically validating),
      * the asset description + tag,
      * a blank serial-number line,
      * a remarks line,
      * a signed QR token (integrity.inventory_token_sign) binding THIS employee
        to THIS asset — the machine-readable authority used by the scan-back
        importer, so no handwriting OCR is ever required.

    The same token appears in the QR the scanner reads, so scanning the form
    back in records an exact verification per row.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)
    import base64 as b64
    from integrity import inventory_token_sign

    emp = get_or_404("employees", employee_id)
    scope = accountability_scope(employee_id)
    assets = list(mongo.db.assets.find({"assigned_to": {"$in": scope}}))
    assets.sort(key=lambda a: ((a.get("device_type") or "").lower(),
                               (a.get("asset_tag") or "").lower()))
    emp_oid = emp.get("_id")

    def qr_img(asset):
        token = inventory_token_sign(emp_oid, asset.get("_id"))
        b64png = generate_qr(token)
        return Image(io.BytesIO(b64.b64decode(b64png)), width=0.55 * inch, height=0.55 * inch)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=0.6 * inch, rightMargin=0.6 * inch,
                            topMargin=0.5 * inch, bottomMargin=0.5 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("InvTitle", parent=styles["Title"], fontSize=15,
                                 spaceAfter=2)
    h2 = ParagraphStyle("InvH2", parent=styles["Heading2"], fontSize=11, spaceAfter=4)
    cell = ParagraphStyle("InvCell", parent=styles["Normal"], fontSize=8.5, leading=11)

    elements = [
        Paragraph("KPI ICT INVENTORY ACCOUNTABILITY FORM", title_style),
        Paragraph(f"Employee: {emp.get('full_name', '')} &nbsp;·&nbsp; "
                  f"ID: {emp.get('employee_id', '')}", styles["Normal"]),
        Paragraph(f"Department: {emp.get('department', '—')} &nbsp;·&nbsp; "
                  f"Position: {emp.get('position', '—')} &nbsp;·&nbsp; "
                  f"Site/Location: {emp.get('site', '—')}", styles["Normal"]),
        Spacer(1, 0.12 * inch),
    ]

    header_row = ["Check", "QR", "Description", "Serial No.", "Remarks"]
    row_data = [header_row]
    for a in assets:
        row_data.append([
            "",
            qr_img(a),
            Paragraph(f"<b>{a.get('asset_tag', '')}</b><br/>"
                      f"{a.get('device_type', '')} · {a.get('model_name', '')}", cell),
            "",
            "",
        ])
    # add a few blank "Others" rows for assets not yet on the sheet
    for _ in range(max(2, 8 - len(assets))):
        row_data.append(["", "", " ", "", ""])

    header_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1565C0")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    width_avail = A4[0] - 1.2 * inch
    t = Table(row_data, colWidths=[0.7 * inch, 0.65 * inch, 2.4 * inch,
                                   1.3 * inch, 2.0 * inch])
    t.setStyle(TableStyle(header_style))
    elements.append(t)
    elements.append(Spacer(1, 0.22 * inch))

    sig = Table([["Prepared by (IT)", "Verified by (Employee)", "Date"]],
                colWidths=[(width_avail) / 3] * 3)
    sig.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 30),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(sig)

    doc.build(elements)
    buf.seek(0)
    return _deliver_download(f"inventory_form_{emp.get('employee_id', 'emp')}.pdf",
                             buf.getvalue(), "application/pdf")


@io_bp.route("/reports/inventory-scan/import", methods=["GET", "POST"])
@login_required
def inventory_scan_import():
    """Scan-back importer for printed employee inventory forms.

    Upload a scanned PDF/photo of a filled `employee-inventory-form`; the
    importer rasterizes it and builds an editable **preview** of every decoded
    signed QR token (integrity token), showing the serial/remarks fields for
    correction FIRST.
    Only after the operator confirms does it record a per-row **verification**
    on the matching assets:

      * sets asset.last_verified / last_verified_by / last_verified_at,
      * appends an inventory verification entry to asset.history,
      * writes an audit_log+integrity-chain entry,
      * saves a copy of the uploaded scan (per row) for the record.

    It never silently flips asset status — the operator confirms after import.
    """
    if not is_scan_qr_enabled():
        return render_template("scan/disabled.html",
                               feature="scan-back inventory import")
    if request.method == "POST":
        from integrity import inventory_token_parse

        # ---- PHASE 2: operator reviewed the preview and hit "Confirm & import".
        # ---- Only the checked rows are applied; serial/remarks edits from the
        # ---- preview form are written into the matching asset.
        if request.form.get("confirm") == "1":
            verified, skipped, dupes = [], 0, 0
            now = datetime.utcnow()
            for asset_id in request.form.getlist("asset_ids"):
                token = request.form.get("token_%s" % asset_id, "")
                serial = request.form.get("serial_%s" % asset_id, "")
                remarks = request.form.get("remarks_%s" % asset_id, "")
                parsed = inventory_token_parse(token)
                if not parsed:
                    skipped += 1
                    continue
                emp_oid, asset_oid = parsed
                asset = mongo.db.assets.find_one({"_id": asset_oid})
                if not asset:
                    skipped += 1
                    continue
                last_verified = asset.get("last_verified_at")
                if last_verified and isinstance(last_verified, datetime) and \
                   (now - last_verified).total_seconds() < 60:
                    dupes += 1
                    continue
                fields = {
                    "last_verified": True,
                    "last_verified_by": current_user.username if current_user.is_authenticated else "system",
                    "last_verified_at": now,
                    "updated_at": now,
                }
                if serial and serial.strip():
                    fields["serial_number"] = serial.strip()
                if remarks and remarks.strip():
                    fields["remarks"] = remarks.strip()
                mongo.db.assets.update_one(
                    {"_id": asset_oid},
                    {"$set": fields,
                     "$push": {"history": {
                         "event": "Inventory Verified",
                         "method": "Scan-back import",
                         "by": current_user.username if current_user.is_authenticated else "system",
                         "at": now,
                         "details": f"Verified against paper form via QR token (employee {emp_oid})",
                     }}})
                audit_log("Assets", "Inventory Verified",
                          old_value={"verified": False},
                          new_value={"verified": True},
                          record_id=asset_oid,
                          username=current_user.username if current_user.is_authenticated else "system")
                verified.append({"asset_tag": asset.get("asset_tag"), "token": token})

            result = {"total": len(request.form.getlist("asset_ids")), "verified": len(verified),
                      "skipped": skipped, "duplicates": dupes}
            if verified:
                flash(f"Imported scan: {len(verified)} asset(s) verified, "
                      f"{skipped} skipped, {dupes} duplicates.", "success")
            return render_template("reports/inventory_scan_result.html",
                                   result=result, verified=verified)

        # ---- PHASE 1: upload a scan -> decode -> editable preview.
        # ---- No database write happens until the operator confirms above.
        f = request.files.get("file")
        if not f or not f.filename:
            flash("No file uploaded.", "error")
            return redirect(url_for("io.inventory_scan_import"))
        raw = f.read()
        try:
            payloads = _scan_all_qr_payloads(raw, f.filename)
        except Exception:
            logger.exception("Failed to rasterize/scan %s", f.filename)
            payloads = []
        if not payloads:
            flash("No readable inventory QR found in that scan. "
                  "Use a clear, well-lit photo or a 300 dpi scan.", "error")
            return redirect(url_for("io.inventory_scan_import"))

        verified, skipped = [], 0
        for token in payloads:
            parsed = inventory_token_parse(token)
            if not parsed:
                skipped += 1
                continue
            emp_oid, asset_oid = parsed
            asset = mongo.db.assets.find_one({"_id": asset_oid})
            if not asset:
                skipped += 1
                continue
            verified.append({
                "asset_id": str(asset_oid),          # value used by checkboxes + hidden tokens
                "token": token,                       # re-verified (HMAC+TTL) on confirm
                "asset_tag": asset.get("asset_tag", ""),
                "device_type": (asset.get("device_type") or "").title(),
                "serial_number": asset.get("serial_number", "") or "",
                "remarks": "",
            })
        if not verified:
            flash("No matching assets found in that scan — nothing to import.", "error")
            return redirect(url_for("io.inventory_scan_import"))
        return render_template("reports/inventory_scan_preview.html",
                               verified=verified,
                               scan_name=f.filename)
    return render_template("reports/inventory_scan_import.html")


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
        "warranty_expiry": _iso_or_none(a.get("warranty_expiry")),
    } for a in assets]})


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

    return jsonify({"success": True, "data": [{
        "id": str(a["_id"]),
        "employee_name": employees_by_id.get(a.get("employee_id"), ""),
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

    app.jinja_env.filters["site_from_tag"] = site_from_tag

    @app.context_processor
    def _sticker_cart_globals():
        return {"sticker_cart_count": len(_sticker_cart_ids())}

    for bp in (auth_bp, dashboard_bp, employees_bp, assets_bp,
               accountabilities_bp, remarks_bp, audits_bp, admin_bp, users_bp, io_bp,
               api_bp, scan_bp):
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
            mongo.db.assets.create_index("assigned_to")
            mongo.db.assets.create_index([("status", 1), ("asset_tag", 1)])
            mongo.db.employees.create_index("employee_id", unique=True)
            mongo.db.employees.create_index("email")
            mongo.db.employees.create_index([("status", 1), ("full_name", 1)])
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
