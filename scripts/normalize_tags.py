"""One-time legacy asset-tag normalization.

Tag prefixes encode the physical site with short codes:
  HO = Head Office (KPIMNL)   BLN = Bustos (KPIBLN)
  BTS = Batangas (KPIBTS)     DVO = Davao (KPID)

Bare "KPI" tags and tags that already carry a recognized site token are never
touched. Legacy IMP-* tags are converted to the conventional scheme using the
endpoint name when it parses (KPIMNLD51 -> HO-D-01) and are otherwise
re-prefixed from their free-text location; what can't be derived is listed as
unresolved and left alone. QR stickers are unaffected (they encode the asset
_id, not the tag).

Usage:
  python scripts/normalize_tags.py            # dry-run (no changes)
  python scripts/normalize_tags.py --apply    # write to MongoDB + audit log
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import (  # noqa: E402
    create_app, mongo, SITE_BY_PREFIX, SITE_SHORT_CODES,
    parse_endpoint_name, ENDPOINT_DEVICE_CODE_MAP, DEVICE_CODE_DEFAULTS,
)

APPLY = "--apply" in sys.argv

SITE_SEGMENTS = set(SITE_BY_PREFIX) | set(SITE_SHORT_CODES.values())

LOCATION_GUESS = [
    ("bustos", "BLN"),
    ("batangas", "BTS"),
    ("davao", "DVO"),
    ("kpi manila", "HO"),
    ("manila", "HO"),
    ("head office", "HO"),
    ("hq", "HO"),
]


def main():
    app = create_app()
    with app.app_context():
        changed = skipped = derived_cnt = 0
        unresolved = []

        # Highest existing sequence number per (site, device code), counted from
        # conventional tags (HO-D-05, KPIMNL-D-05, ...). The counter below then
        # mirrors exactly what an --apply run writes, so a dry-run preview shows
        # the final, sequential tags (HO-L-01, HO-L-02, ...) not repeats.
        base_max = {}
        for a in mongo.db.assets.find({}, {"asset_tag": 1}):
            t = (a.get("asset_tag") or "").strip().upper()
            parts = t.split("-")
            if len(parts) >= 3 and parts[-1].isdigit() and parts[1] and parts[1].isalnum():
                seg0 = parts[0]
                site = SITE_BY_PREFIX.get(seg0) or next(
                    (s for s, sh in SITE_SHORT_CODES.items() if sh == seg0), None)
                if site and not parts[1].isdigit():
                    key = (site, parts[1])
                    base_max[key] = max(base_max.get(key, 0), int(parts[-1]))
        counters = {}

        for a in mongo.db.assets.find({}):
            tag = (a.get("asset_tag") or "").strip()
            if not tag:
                continue
            upper = tag.upper()
            if upper.split("-", 1)[0] in SITE_SEGMENTS:
                skipped += 1
                continue
            if upper.startswith("KPI"):
                # generic KPI tag - "if it's just KPI, leave it as is"
                skipped += 1
                continue

            new_tag = None
            source = ""

            # (1) Prefer the endpoint name: KPIMNLD51 -> HO-D-01 (conventional).
            endpoint = (a.get("endpoint_name") or "").strip()
            site, letter, _ = parse_endpoint_name(endpoint)
            if site:
                dc = ""
                if letter:
                    dc = ENDPOINT_DEVICE_CODE_MAP.get(letter, letter)
                else:
                    dc = DEVICE_CODE_DEFAULTS.get((a.get("device_type") or "").strip(), "")
                if dc:
                    key = (site, dc)
                    counters[key] = counters.get(key, 0) + 1
                    seq = base_max.get(key, 0) + counters[key]
                    seq_s = str(seq)
                    new_tag = "{}-{}-{}".format(
                        SITE_SHORT_CODES[site], dc, "0" + seq_s if len(seq_s) < 2 else seq_s)
                    source = "endpoint"
                    derived_cnt += 1

            # (2) Fallback: re-prefix from free-text location (short code).
            if not new_tag:
                loc = (a.get("location") or "").lower()
                prefix = None
                for kw, p in LOCATION_GUESS:
                    if kw in loc:
                        prefix = p
                        break
                if prefix:
                    new_tag = "{}-{}".format(prefix, tag)
                    source = "location"

            if not new_tag:
                unresolved.append((str(a["_id"]), tag, a.get("location"),
                                   a.get("endpoint_name")))
                continue

            print("[{}][{:8}] {!r:16} endpoint={:16} device={:6} -> {!r}".format(
                "APPLY" if APPLY else "DRY ",
                source, tag, endpoint or "-", a.get("device_type") or "-", new_tag))
            if APPLY:
                mongo.db.assets.update_one(
                    {"_id": a["_id"]},
                    {"$set": {"asset_tag": new_tag, "updated_at": datetime.utcnow()}})
                mongo.db.audit_logs.insert_one({
                    "timestamp": datetime.utcnow(),
                    "username": "system",
                    "ip_address": None,
                    "module": "Assets",
                    "action": "Tag Normalize",
                    "record_id": str(a["_id"]),
                    "record_name": new_tag,
                    "old_value": {"asset_tag": tag},
                    "new_value": {"asset_tag": new_tag},
                })
            changed += 1

        print("\nSkipped (already conventional / generic KPI): {}".format(skipped))
        print("{} ({} from endpoint, {} from location): {}".format(
            "Renamed" if APPLY else "Would rename", derived_cnt,
            changed - derived_cnt, changed))
        if unresolved:
            print("\nUNRESOLVED - no site from endpoint or location hint ({}). "
                  "Edit these manually in the Asset form:".format(len(unresolved)))
            for oid, tag, loc, endpoint in unresolved:
                print("  {}  {}  location={}  endpoint={}".format(
                    oid, tag or "", loc or "", endpoint or ""))
        if not APPLY:
            print("\nDry-run only. Re-run with --apply to write changes.")


if __name__ == "__main__":
    main()