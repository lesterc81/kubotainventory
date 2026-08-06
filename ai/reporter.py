import logging
from datetime import datetime, timedelta
from .groq_client import chat

logger = logging.getLogger("itsystem.ai.reporter")

SUMMARY_SYSTEM = (
    "You are an IT asset management analyst. "
    "Generate concise, data-driven summaries. Use bullet points. "
    "Highlight risks, trends, and recommended actions."
)

ANOMALY_SYSTEM = (
    "You are a security analyst reviewing IT system activity logs. "
    "Assess each anomaly for actual risk severity (low/medium/high/critical). "
    "Explain why it matters and recommend action. Be concise."
)


def make_summary_report(mongo_db):
    now = datetime.utcnow()
    start = now - timedelta(days=7)

    total_assets = mongo_db.assets.count_documents({})
    assigned = mongo_db.assets.count_documents({"status": "Assigned"})
    available = mongo_db.assets.count_documents({"status": "Available"})
    maintenance = mongo_db.assets.count_documents({"status": "Under Maintenance"})
    total_employees = mongo_db.employees.count_documents({"status": "Active"})
    active_accs = mongo_db.accountabilities.count_documents({"status": "Active"})
    recent_logs = mongo_db.audit_logs.count_documents({"timestamp": {"$gte": start}})

    inactive_emps = list(mongo_db.employees.find(
        {"status": {"$in": ["Inactive", "Resigned"]}},
        {"employee_id": 1},
    ))
    inactive_emp_ids = [e["employee_id"] for e in inactive_emps if e.get("employee_id")]
    orphaned = mongo_db.accountabilities.count_documents({
        "employee_id": {"$in": inactive_emp_ids},
        "status": "Active",
    }) if inactive_emp_ids else 0

    already_expired = mongo_db.assets.count_documents({
        "warranty_expiry": {"$lt": now},
        "status": "Assigned",
    })

    stale_cutoff = now - timedelta(days=180)
    stale_accs = list(mongo_db.accountabilities.find(
        {"status": "Active", "created_at": {"$lte": stale_cutoff}},
        {"_id": 1, "created_at": 1},
    ).sort("created_at", 1).limit(10))

    stale_avail_cutoff = now - timedelta(days=60)
    stale_available = mongo_db.assets.count_documents({
        "status": "Available",
        "updated_at": {"$lte": stale_avail_cutoff},
    })

    expiring = list(mongo_db.assets.find(
        {"warranty_expiry": {"$lte": now + timedelta(days=90), "$gte": now}},
        {"asset_tag": 1, "warranty_expiry": 1, "model_name": 1},
    ).sort("warranty_expiry", 1).limit(10))

    by_type = list(mongo_db.assets.aggregate([
        {"$group": {"_id": "$device_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]))

    by_location = list(mongo_db.assets.aggregate([
        {"$group": {"_id": "$location", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 5},
    ]))

    action_breakdown = list(mongo_db.audit_logs.aggregate([
        {"$match": {"timestamp": {"$gte": start}}},
        {"$group": {"_id": "$action", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]))

    expiring_lines = "\n".join(
        f"- {a['asset_tag']} ({a.get('model_name', 'N/A')}) — expires {a['warranty_expiry'].strftime('%Y-%m-%d') if isinstance(a.get('warranty_expiry'), datetime) else str(a.get('warranty_expiry', 'N/A'))}"
        for a in expiring
    ) or "- None"

    type_lines = "\n".join(f"- {t['_id'] or 'Unknown'}: {t['count']}" for t in by_type)
    loc_lines = "\n".join(f"- {l['_id'] or 'Unknown'}: {l['count']}" for l in by_location)
    action_breakdown_lines = "\n".join(f"  - {a['_id'] or 'Unknown'}: {a['count']}" for a in action_breakdown)

    prompt = f"""IT Asset System — Weekly Summary Report

Period: {start.strftime('%Y-%m-%d')} to {now.strftime('%Y-%m-%d')}

INVENTORY OVERVIEW
- Total assets: {total_assets}
- Assigned: {assigned}
- Available: {available}
- Under maintenance: {maintenance}
- Active employees: {total_employees}
- Active accountabilities: {active_accs}

RISK SIGNALS (prioritize these in your analysis)
- Assets assigned to inactive/resigned employees (orphaned): {orphaned}
- Warranty already expired, still in active use: {already_expired}
- Accountabilities open 180+ days without closure: {len(stale_accs)}
- Assets sitting "Available" (idle) for 60+ days: {stale_available}

ACTIVITY (last 7 days)
- Total audit log entries: {recent_logs}
- By action type:
{action_breakdown_lines}

ASSETS BY TYPE
{type_lines}

TOP LOCATIONS
{loc_lines}

WARRANTY EXPIRING WITHIN 90 DAYS
{expiring_lines}

INSTRUCTIONS
Write a concise executive summary for an IT manager audience with these sections:
1. Overall Health — one or two sentences, plain assessment (healthy / needs attention / at risk)
2. Key Risks — prioritize orphaned assets and expired-warranty items above general stats; call out anything requiring action this week
3. Trends & Activity — note any unusual concentration in audit log activity
4. Recommended Actions — 3-5 concrete, specific next steps (not generic advice), ordered by urgency

Use bullet points. Do not restate raw numbers already shown above unless flagging them as risks. Keep the entire summary under 300 words."""

    try:
        return chat(prompt, system=SUMMARY_SYSTEM, max_tokens=1500)
    except Exception:
        logger.exception("AI summary generation failed")
        return "AI summary unavailable (Groq API error)."


def analyze_anomalies(mongo_db, anomalies):
    if not anomalies:
        return "No anomalies detected in the current window."

    lines = "\n".join(
        f"- [{a['severity'].upper()}] {a['label']}: {a['detail']}"
        for a in anomalies[:10]
    )

    prompt = f"""The following anomalies were detected in the IT asset system:

{lines}

For each anomaly, provide: (1) risk assessment, (2) why it matters, (3) recommended action."""

    try:
        return chat(prompt, system=ANOMALY_SYSTEM, max_tokens=1500)
    except Exception:
        logger.exception("AI anomaly analysis failed")
        return "AI analysis unavailable (Groq API error)."
