import logging
from datetime import datetime, timedelta

logger = logging.getLogger("itsystem.ai.detector")

RULES = [
    {
        "id": "rapid_changes",
        "label": "Rapid consecutive changes",
        "description": "Multiple changes to the same record within 5 minutes",
        "severity": "medium",
        "window_minutes": 5,
        "min_actions": 3,
    },
    {
        "id": "off_hours_access",
        "label": "Off-hours activity",
        "description": "System access outside business hours (10PM–5AM)",
        "severity": "low",
        "off_start": 22,
        "off_end": 5,
    },
    {
        "id": "mass_operation",
        "label": "Mass operation spike",
        "description": "Unusual spike in operations (10+ actions in 1 minute)",
        "severity": "high",
        "window_minutes": 1,
        "threshold": 10,
    },
    {
        "id": "failed_login_chain",
        "label": "Failed login chain",
        "description": "3+ failed login attempts within 10 minutes",
        "severity": "high",
        "window_minutes": 10,
        "threshold": 3,
    },
    {
        "id": "bulk_transfer",
        "label": "Bulk transfer or archive",
        "description": "3+ transfer/archive actions within 1 minute",
        "severity": "high",
        "window_minutes": 1,
        "threshold": 3,
    },
]


def _resolve_record(mongo_db, record_id):
    """Look up a record_id across assets, workstations, employees and return readable info."""
    from bson.objectid import ObjectId
    try:
        oid = ObjectId(record_id)
    except Exception:
        return None
    for coll, type_label, tag_field, model_field in [
        (mongo_db.assets, "Asset", "asset_tag", "model_name"),
        (mongo_db.workstations, "Workstation", "workstation_code", "workstation_name"),
        (mongo_db.employees, "Employee", "employee_id", "full_name"),
    ]:
        doc = coll.find_one({"_id": oid}, {tag_field: 1, model_field: 1, "device_type": 1})
        if doc:
            return {
                "record_type": type_label,
                "record_tag": str(doc.get(tag_field, "")),
                "record_model": str(doc.get(model_field, "")),
                "record_subtype": str(doc.get("device_type", "")),
            }
    return None


def run_anomaly_detection(mongo_db):
    found = []
    now = datetime.utcnow()

    for rule in RULES:
        rule_id = rule["id"]

        if rule_id == "off_hours_access":
            hour = now.hour
            if rule["off_start"] <= hour or hour < rule["off_end"]:
                count = mongo_db.audit_logs.count_documents({
                    "timestamp": {"$gte": now - timedelta(hours=1)},
                })
                if count > 0:
                    found.append({
                        "rule_id": rule_id,
                        "label": rule["label"],
                        "description": rule["description"],
                        "severity": rule["severity"],
                        "count": count,
                        "detail": f"{count} actions during off-hours in the last hour",
                    })
            continue

        window = rule.get("window_minutes", 5)
        since = now - timedelta(minutes=window)

        if rule_id == "rapid_changes":
            pipeline = [
                {"$match": {"timestamp": {"$gte": since}}},
                {"$group": {"_id": "$record_id", "count": {"$sum": 1}, "actions": {"$addToSet": "$action"}}},
                {"$match": {"count": {"$gte": rule["min_actions"]}, "_id": {"$ne": None}}},
                {"$sort": {"count": -1}},
                {"$limit": 10},
            ]
        elif rule_id == "mass_operation":
            pipeline = [
                {"$match": {"timestamp": {"$gte": since}}},
                {"$group": {"_id": None, "count": {"$sum": 1}}},
            ]
        elif rule_id == "failed_login_chain":
            pipeline = [
                {"$match": {"timestamp": {"$gte": since}, "module": "Auth", "action": "Login"}},
                {"$group": {"_id": "$username", "count": {"$sum": 1}}},
                {"$match": {"count": {"$gte": rule["threshold"]}}},
                {"$sort": {"count": -1}},
                {"$limit": 5},
            ]
        elif rule_id == "bulk_transfer":
            pipeline = [
                {"$match": {"timestamp": {"$gte": since}, "action": {"$in": ["Transfer", "Archive"]}}},
                {"$group": {"_id": None, "count": {"$sum": 1}}},
            ]
        else:
            continue

        try:
            results = list(mongo_db.audit_logs.aggregate(pipeline))
        except Exception:
            logger.exception("Anomaly pipeline failed for %s", rule_id)
            continue

        if not results:
            continue

        if rule_id in ("mass_operation", "bulk_transfer"):
            total = results[0]["count"] if results else 0
            if total >= rule["threshold"]:
                found.append({
                    "rule_id": rule_id,
                    "label": rule["label"],
                    "description": rule["description"],
                    "severity": rule["severity"],
                    "count": total,
                    "detail": f"{total} actions in {window} minute(s)",
                })
        else:
            for r in results[:5]:
                item = {
                    "rule_id": rule_id,
                    "label": rule["label"],
                    "description": rule["description"],
                    "severity": rule["severity"],
                    "count": r["count"],
                    "detail": f"Record {r['_id']} — {r['count']} actions in {window} min",
                }
                if rule_id == "rapid_changes" and r.get("_id"):
                    info = _resolve_record(mongo_db, r["_id"])
                    if info:
                        item["record_type"] = info["record_type"]
                        item["record_tag"] = info["record_tag"]
                        item["record_model"] = info["record_model"]
                        item["record_subtype"] = info["record_subtype"]
                        item["detail"] = f"{info['record_type']}: {info['record_tag']} ({info['record_model']}) — {r['count']} changes in {window} min"
                elif rule_id == "failed_login_chain":
                    item["record_tag"] = r.get("_id", "")
                found.append(item)

    return found
