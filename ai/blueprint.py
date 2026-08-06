import logging
from datetime import datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from flask import Blueprint, abort, current_app, jsonify, render_template, request
from flask_login import current_user, login_required

from .detector import run_anomaly_detection
from .reporter import make_summary_report, analyze_anomalies

logger = logging.getLogger("itsystem.ai")

ai_bp = Blueprint("ai", __name__, url_prefix="/ai")


def _db():
    return current_app.config["MONGO_DB"]


def _to_oid(raw):
    """Coerce a raw id to ObjectId, returning None for malformed input."""
    try:
        return ObjectId(raw)
    except (InvalidId, TypeError, ValueError):
        return None


def editor_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_edit():
            abort(403)
        return f(*args, **kwargs)
    return decorated


@ai_bp.route("/dashboard")
@login_required
def dashboard():
    db = _db()
    anomalies = list(db.ai_anomalies.find(
        {"acknowledged": False}
    ).sort("detected_at", -1).limit(20))

    summary = None
    last_report = db.ai_reports.find_one(
        {"type": "weekly_summary"},
        sort=[("generated_at", -1)],
    )
    if last_report:
        summary = last_report.get("content")

    anomaly_count = db.ai_anomalies.count_documents({"acknowledged": False})
    high_count = db.ai_anomalies.count_documents({
        "acknowledged": False, "severity": "high",
    })

    return render_template("ai/dashboard.html",
        anomalies=[_serialize(a) for a in anomalies],
        summary=summary,
        anomaly_count=anomaly_count,
        high_count=high_count,
    )


@ai_bp.route("/anomalies")
@login_required
def anomalies():
    db = _db()
    show = request.args.get("show", "active")
    query = {"acknowledged": False} if show == "active" else {}
    per_page = current_app.config["PER_PAGE_DEFAULT"]
    page = max(request.args.get("page", 1, type=int), 1)
    total = db.ai_anomalies.count_documents(query)
    total_pages = max((total + per_page - 1) // per_page, 1)
    items = list(db.ai_anomalies.find(query).sort("detected_at", -1)
                 .skip((page - 1) * per_page).limit(per_page))

    return render_template("ai/anomalies.html",
        anomalies=[_serialize(a) for a in items],
        show=show, page=page, total=total,
        per_page=per_page, total_pages=total_pages,
    )


@ai_bp.route("/anomalies/<anomaly_id>/acknowledge", methods=["POST"])
@login_required
def acknowledge(anomaly_id):
    db = _db()
    oid = _to_oid(anomaly_id)
    if not oid:
        return jsonify({"ok": False, "error": "invalid id"}), 400
    db.ai_anomalies.update_one({"_id": oid}, {"$set": {"acknowledged": True}})
    return jsonify({"ok": True})


@ai_bp.route("/anomalies/<anomaly_id>/delete", methods=["POST"])
@login_required
def delete_anomaly(anomaly_id):
    db = _db()
    oid = _to_oid(anomaly_id)
    if not oid:
        return jsonify({"ok": False, "error": "invalid id"}), 400
    db.ai_anomalies.delete_one({"_id": oid})
    return jsonify({"ok": True})


@ai_bp.route("/anomalies/clear-acknowledged", methods=["POST"])
@login_required
@editor_required
def clear_acknowledged():
    db = _db()
    result = db.ai_anomalies.delete_many({"acknowledged": True})
    return jsonify({"deleted": result.deleted_count})


@ai_bp.route("/anomalies/delete-all", methods=["POST"])
@login_required
@editor_required
def delete_all_anomalies():
    db = _db()
    result = db.ai_anomalies.delete_many({})
    return jsonify({"deleted": result.deleted_count})


@ai_bp.route("/scan", methods=["POST"])
@login_required
@editor_required
def scan():
    db = _db()
    anomalies = run_anomaly_detection(db)
    for a in anomalies:
        db.ai_anomalies.insert_one({
            **a,
            "detected_at": datetime.utcnow(),
            "acknowledged": False,
        })
    return jsonify({"found": len(anomalies), "anomalies": anomalies})


@ai_bp.route("/report/generate", methods=["POST"])
@login_required
@editor_required
def generate_report():
    db = _db()
    summary = make_summary_report(db)
    db.ai_reports.insert_one({
        "type": "weekly_summary",
        "content": summary,
        "generated_at": datetime.utcnow(),
    })

    anomalies = list(db.ai_anomalies.find(
        {"acknowledged": False, "severity": {"$in": ["high", "medium"]}},
    ).sort("detected_at", -1).limit(20))
    analysis = analyze_anomalies(db, anomalies)

    if anomalies:
        db.ai_reports.insert_one({
            "type": "anomaly_analysis",
            "content": analysis,
            "generated_at": datetime.utcnow(),
        })

    return jsonify({"summary": summary, "analysis": analysis})


@ai_bp.route("/reports")
@login_required
def reports():
    db = _db()
    per_page = current_app.config["PER_PAGE_DEFAULT"]
    page = max(request.args.get("page", 1, type=int), 1)
    total = db.ai_reports.count_documents({})
    total_pages = max((total + per_page - 1) // per_page, 1)
    items = list(db.ai_reports.find().sort("generated_at", -1)
                 .skip((page - 1) * per_page).limit(per_page))
    return render_template("ai/reports.html", reports=[_serialize(r) for r in items],
                           page=page, total=total, per_page=per_page, total_pages=total_pages)


def _serialize(doc):
    if doc is None:
        return None
    result = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            result[k] = str(v)
        elif isinstance(v, datetime):
            result[k] = v.isoformat()
        elif isinstance(v, list):
            result[k] = [_serialize(i) if isinstance(i, dict) else str(i) if isinstance(i, ObjectId) else i for i in v]
        elif isinstance(v, dict):
            result[k] = _serialize(v)
        else:
            result[k] = v
    if "_id" in doc:
        result["id"] = str(doc["_id"])
    return result
