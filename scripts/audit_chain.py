"""Tamper-evident audit trail support.

Every audit_log entry is signed with an HMAC that also chains to the previous
entry's signature (prev_sig). Recomputing the chain detects ANY manual edit,
insertion, or deletion in the audit_logs collection:

  entry.sig      = HMAC(key, canonical(entry_fields)   + prev_sig)
  entry.prev_sig = the sig of the chronological predecessor
  entry.prev_id  = the _id of the predecessor

Key source: env AUDIT_CHAIN_KEY, else SECRET_KEY (from .env / app config).

Logic lives in integrity.py so the web UI (Admin > System Integrity) and this
CLI always share the same signing/verification.
"""

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env"))
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/itsystem")

from integrity import (  # noqa: E402
    chain_backfill,
    chain_verify,
)


def main():
    from pymongo import MongoClient

    cmd = sys.argv[1] if len(sys.argv) > 1 else "verify"
    db = MongoClient(MONGO_URI).get_database()

    if cmd == "backfill":
        n = chain_backfill(db)
        print(f"Signed {n} audit entries.")
    elif cmd == "verify":
        result = chain_verify(db)
        print(f"audit_logs={result['count']}")
        if not result["issues"]:
            print("AUDIT CHAIN OK")
            return
        for issue in result["issues"]:
            print(f"  [{issue['kind']}] log[{issue['index']}] {issue['_id']} — {issue['detail']}")
        sys.exit(1)
    else:
        print(__doc__)
        print("usage: python scripts/audit_chain.py [backfill|verify]")
        sys.exit(2)


if __name__ == "__main__":
    main()