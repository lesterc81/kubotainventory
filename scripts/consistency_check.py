"""Cross-document integrity check for the IT Asset System.

Detects the failure mode where hand-edited (or merely deleted) database
documents silently desync the accountability <-> asset relationship:

  - assets in "Assigned" status but not covered by any accountability
  - accountability documents referencing missing assets
  - assets that are "Assigned" but whose employee no longer exists / is inactive
  - active accountabilities referencing missing or not-Active employees
  - asset.assigned_to pointing anywhere inconsistent with its status

Read-only. Exits non-zero when issues are found (for CI/scheduled tasks).

Logic lives in integrity.py so the web UI (Admin > System Integrity) and this
CLI always share the same checks.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/itsystem")

from integrity import check_consistency  # noqa: E402


def main():
    db = MongoClient(MONGO_URI).get_database()
    result = check_consistency(db)

    if not result["ok"]:
        print(f"CONSISTENCY ISSUES ({len(result['issues'])}):")
        for p in result["issues"]:
            print("  " + p)
        sys.exit(1)

    print("CONSISTENCY OK")
    print(f"  assets={result['assets']} employees={result['employees']} "
          f"accountabilities={result['accountabilities']}")


if __name__ == "__main__":
    main()