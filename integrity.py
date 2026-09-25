"""Shared system-integrity logic: data consistency + tamper-evident audit chain.

Single source of truth used by BOTH the CLI scripts and the web UI
(templates/admin/integrity.html via routes). Pure functions over a pymongo
db handle — no Flask imports, so it stays lightweight and reusable.
"""

import hashlib
import hmac
import json
from datetime import datetime

from bson import ObjectId

# ---------------------------------------------------------------------------
# 1) Cross-document consistency
# ---------------------------------------------------------------------------
ASSIGNED = {"Assigned"}
ACTIVE_ACC = {"Active"}


def _oid(value):
    """Normalize a stored id reference to the ObjectId used as a map key.

    Accepts an ``ObjectId`` as-is, or a 24-hex string (which the DB may store
    for older/paper records). Returns ``None`` for anything else so lookups on
    ``employees``/``assets`` never silently key on a non-id.
    """
    if value is None:
        return None
    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str):
        try:
            return ObjectId(value)
        except Exception:
            return None
    return None


def _oid_hex(value):
    """24-hex string form of an id, or ``""`` if it is not a valid id.

    Used only for embedding ids inside signed event/token payloads (never for
    map lookups, which rely on real ``ObjectId`` keys), so a host's decoder
    can rebuild ``ObjectId``s with no repr-noise.
    """
    oid = _oid(value)
    return str(oid) if oid is not None else ""


def check_consistency(db):
    """Return {'ok', 'issues', 'assets', 'employees', 'accountabilities'}.

    Mirrors scripts/consistency_check.py: orphaned assignments, dangling
    references, and accountability<->asset mismatch in both directions.
    """
    employees = {e["_id"]: e for e in db.employees.find({})}
    assets = {a["_id"]: a for a in db.assets.find({})}
    accs = list(db.accountabilities.find({}))
    problems = []

    acc_for_asset = {}
    for acc in accs:
        for aid in acc.get("asset_ids") or []:
            acc_for_asset.setdefault(aid, []).append(acc)

    for aid, a in assets.items():
        if a.get("status") in ASSIGNED and aid not in acc_for_asset:
            emp = employees.get(_oid(a.get("assigned_to")))
            problems.append(
                f"[orphan-assigned] {a.get('asset_tag', '?')} status='{a.get('status')}' "
                f"assigned_to='{emp['full_name'] if emp else a.get('assigned_to')}' "
                f"but no accountability")

    for acc in accs:
        for aid in acc.get("asset_ids") or []:
            if aid not in assets:
                problems.append(
                    f"[missing-asset] accountability {acc.get('_id')} "
                    f"(status={acc.get('status')}) references missing asset {aid}")

    for aid, acc_list in acc_for_asset.items():
        a = assets.get(aid)
        if not a:
            continue
        if any(x.get("status") in ACTIVE_ACC for x in acc_list) and a.get("status") not in ASSIGNED:
            problems.append(
                f"[mismatch] {a.get('asset_tag', '?')} has an Active accountability "
                f"but status='{a.get('status')}'")

    for aid, a in assets.items():
        emp_id = _oid(a.get("assigned_to"))
        if not emp_id:
            continue
        emp = employees.get(emp_id)
        if emp is None:
            problems.append(
                f"[dangling-employee] {a.get('asset_tag', '?')} assigned_to "
                f"{a.get('assigned_to')} no longer exists")
        elif a.get("status") in ASSIGNED and emp.get("status") != "Active":
            problems.append(
                f"[inactive-employee] {a.get('asset_tag', '?')} assigned to "
                f"{emp.get('full_name')} who is {emp.get('status') or 'inactive'}")

    for acc in accs:
        emp = employees.get(_oid(acc.get("employee_id")))
        if acc.get("status") in ACTIVE_ACC and emp is None:
            problems.append(
                f"[missing-holder] accountability {acc.get('_id')} references "
                f"missing employee {acc.get('employee_id')}")
        elif acc.get("status") in ACTIVE_ACC and emp and emp.get("status") != "Active":
            problems.append(
                f"[inactive-holder] accountability {acc.get('_id')} held by "
                f"{emp.get('full_name')} who is {emp.get('status')}")

    # 6. Stockroom custodian setting must point at an existing Active employee.
    cust = db.settings.find_one({"_id": "stockroom_custodian"})
    if cust and cust.get("employee_id"):
        try:
            cust_oid = ObjectId(cust["employee_id"])
        except Exception:
            cust_oid = None
        if cust_oid is None:
            problems.append(
                f"[custodian] stockroom custodian setting has an invalid employee_id "
                f"('{cust['employee_id']}')")
        else:
            emp = employees.get(cust_oid)
            if emp is None:
                problems.append(
                    f"[custodian] stockroom custodian {cust['employee_id']} "
                    f"no longer exists")
            elif emp.get("status") != "Active":
                problems.append(
                    f"[custodian] stockroom custodian "
                    f"{emp.get('full_name')} is {emp.get('status') or 'inactive'}")

    return {
        "ok": not problems,
        "issues": sorted(set(problems)),
        "assets": len(assets),
        "employees": len(employees),
        "accountabilities": len(accs),
    }


# ---------------------------------------------------------------------------
# 2) Tamper-evident audit chain (HMAC-SHA256, linked to predecessor)
# ---------------------------------------------------------------------------
AUDIT_SIGNED_FIELDS = (
    "timestamp", "username", "ip_address", "module", "action",
    "record_id", "record_name", "old_value", "new_value",
)


def chain_key():
    import os
    return (os.environ.get("AUDIT_CHAIN_KEY") or os.environ.get("SECRET_KEY")
            or "itsystem-dev-chain-key").encode("utf-8")


def _iso(value):
    if isinstance(value, datetime):
        s = value.isoformat()
        if "." in s:
            head, micro = s.rsplit(".", 1)
            return head + "." + micro[:3]  # Mongo stores dates at ms precision
        return s
    return str(value) if value is not None else None


def chain_canonical_bytes(doc):
    """Deterministic byte payload over the signed fields (JSON, stable order)."""
    payload = {f: (_iso(doc.get(f)) if f == "timestamp" else doc.get(f))
               for f in AUDIT_SIGNED_FIELDS}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def chain_sign(prev_sig, prev_id, doc, key=None):
    """Return (sig, prev_sig, prev_id) for a new entry given its predecessor."""
    key = key or chain_key()
    prev_sig = prev_sig or ""
    prev_id = str(prev_id) if prev_id is not None else ""
    payload = chain_canonical_bytes(doc) + b"|" + prev_sig.encode("utf-8") \
        + b"|" + prev_id.encode("utf-8")
    return hmac.new(key, payload, hashlib.sha256).hexdigest(), prev_sig, prev_id


def chain_verify(db, key=None):
    """Return {'ok', 'issues', 'count'}. issues entries:
    {'kind': 'unsigned'|'modified'|'fork', '_id', 'detail', 'index'}."""
    key = key or chain_key()
    issues = []
    expected_prev_sig = ""
    expected_prev_id = ""
    count = 0
    prev = None
    for doc in db.audit_logs.find({}).sort("_id", 1):
        count += 1
        sig, prev_sig, prev_id = chain_sign(expected_prev_sig, expected_prev_id, doc, key)
        if not doc.get("sig"):
            issues.append({"kind": "unsigned", "_id": str(doc["_id"]),
                           "detail": "legacy/unsigned entry", "index": count})
        elif doc.get("sig") != sig:
            issues.append({"kind": "modified", "_id": str(doc["_id"]),
                           "detail": "entry content or chain link does not match", "index": count})
        if doc.get("prev_sig") and doc.get("prev_sig") != expected_prev_sig:
            issues.append({"kind": "fork", "_id": str(doc["_id"]),
                           "detail": "prev_sig mismatch (edited predecessor or concurrent insert)",
                           "index": count})
        expected_prev_sig = doc.get("sig") or ""
        expected_prev_id = str(doc["_id"])
        prev = doc
    return {"ok": not issues, "issues": issues, "count": count}


def chain_backfill(db, key=None):
    """Sign every audit entry in _id order (idempotent)."""
    key = key or chain_key()
    prev_sig, prev_id = "", None
    count = 0
    for doc in db.audit_logs.find({}).sort("_id", 1):
        sig, psig, pid = chain_sign(prev_sig, prev_id, doc, key)
        db.audit_logs.update_one(
            {"_id": doc["_id"]},
            {"$set": {"sig": sig, "prev_sig": psig, "prev_id": pid}},
        )
        prev_sig, prev_id = sig, doc["_id"]
        count += 1
    return count


# ---------------------------------------------------------------------------
# 5) Physical-inventory (paper accountability form) row tokens
#
# Every employee inventory form is built with one QR per accountability row.
# The QR encodes a signed, one-time token binding ONE employee <-> ONE asset.
# Scanning the returned paper form back in decodes these tokens; importing
# records a *verification* (last_verified + history + audit) against the exact
# employee/asset row -- never an unrequested status change.
# ---------------------------------------------------------------------------
import time

INVENTORY_TOKEN_TTL_DAYS = 180  # how long a printed token stays importable


def inventory_token_sign(employee_oid, asset_oid, issued_ts=None, key=None):
    """Sign a one-time inventory row token: ``itsys:inv|...``.

    Payload: employee_oid | asset_oid | issued_ts, HMAC-SHA256 signed with
    the same chain key that seals the accountability audit trail, so a host
    can only decode what it was allowed to print.
    """
    issued_ts = int(issued_ts if issued_ts is not None else time.time())
    raw = f"inv|{_oid_hex(employee_oid)}|{_oid_hex(asset_oid)}|{issued_ts}"
    return "itsys:" + raw + "|" + _sign_inv(raw, key=key)


def inventory_token_parse(token, key=None):
    """Verify + decode an inventory row token.

    Returns (employee_oid, asset_oid) on success, else None. Rejects tokens
    whose HMAC fails, whose TTL expired, or with foreign payloads.
    """
    if not token or not token.startswith("itsys:"):
        return None
    body = token[len("itsys:"):]
    try:
        raw, sig = body.rsplit("|", 1)
    except ValueError:
        return None
    if not _inv_sig_ok(raw, sig, key=key):
        return None
    parts = raw.split("|")
    if len(parts) != 4 or parts[0] != "inv":
        return None
    emp_hex, asset_hex, ts = parts[1], parts[2], parts[3]
    try:
        issued_ts = int(ts)
    except (TypeError, ValueError):
        return None
    if time.time() - issued_ts > INVENTORY_TOKEN_TTL_DAYS * 86400:
        return None
    try:
        emp_oid, asset_oid = ObjectId(emp_hex), ObjectId(asset_hex)
    except Exception:
        return None
    return emp_oid, asset_oid


def inventory_token_pairs(db, employee_doc, assets):
    """Build [(asset, token)...] for every accountability row on the form.

    Uses the same selected assets the printed PDF lists so the form and the
    tokens always agree. ``db`` is accepted for parity with future queries.
    """
    emp_oid = employee_doc.get("_id")
    return [(a, inventory_token_sign(emp_oid, a.get("_id"))) for a in assets]


def _sign_inv(raw, key=None):
    """HMAC-SHA256 signature (hex) over the token payload."""
    import hashlib
    import hmac
    key = key or chain_key()
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


def _inv_sig_ok(raw, sig, key=None):
    import hashlib
    import hmac
    key = key or chain_key()
    expected = _sign_inv(raw, key=key)
    return hmac.compare_digest(expected, sig or "")