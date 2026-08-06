"""
MongoDB backup script for the IT Asset System.

Dumps every collection in the configured database to a timestamped folder
under BACKUP_DIR (default: backups/) as JSON files using bson.json_util,
so ObjectIds and dates survive the round-trip. Old backups are pruned so only
BACKUP_KEEP (default: 14) remain.

Usage:
    python backup_db.py            # one-shot backup
    python backup_db.py --restore <folder>   # restore from a backup folder

Scheduling (Windows):
    schtasks /Create /TN "ITAssetBackup" /TR "<python> backup_db.py" /SC DAILY /ST 03:00
"""

import argparse
import os
import sys
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from bson import json_util

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/itsystem")
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "backups"))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "14"))
EXCLUDE = {"system.views", "system.users"}


def db_name_from_uri(uri):
    return uri.rsplit("/", 1)[-1].split("?", 1)[0] or "itsystem"


def dump(uri, backup_dir, keep):
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    dbname = db_name_from_uri(uri)
    db = client[dbname]

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = backup_dir / f"{dbname}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    collections = db.list_collection_names()
    written = 0
    for name in collections:
        if name in EXCLUDE:
            continue
        docs = list(db[name].find({}))
        target = out_dir / f"{name}.json"
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(json_util.dumps(docs, indent=2))
        written += len(docs)
        print(f"  {name}: {len(docs)} docs")

    manifest = {
        "database": dbname,
        "backed_up_at": datetime.utcnow().isoformat(),
        "collections": [n for n in collections if n not in EXCLUDE],
        "total_docs": written,
    }
    with open(out_dir / "_manifest.json", "w", encoding="utf-8") as fh:
        fh.write(json_util.dumps(manifest, indent=2))

    print(f"BACKUP OK -> {out_dir} ({written} docs total)")

    # prune old backups
    backups = sorted(
        [p for p in backup_dir.glob(f"{dbname}-*") if p.is_dir()],
        key=lambda p: p.name, reverse=True,
    )
    for old in backups[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        print(f"  pruned {old.name}")

    client.close()
    return out_dir


def restore(uri, folder):
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    dbname = db_name_from_uri(uri)
    db = client[dbname]
    folder = Path(folder)

    if not folder.is_dir():
        print(f"ERROR: {folder} is not a directory")
        sys.exit(1)

    for f in sorted(folder.glob("*.json")):
        if f.name == "_manifest.json":
            continue
        collection = f.stem
        with open(f, encoding="utf-8") as fh:
            docs = json_util.loads(fh.read())
        if docs:
            db[collection].insert_many(docs)
        print(f"  restored {collection}: {len(docs)} docs")
    print(f"RESTORE OK from {folder}")


def main():
    parser = argparse.ArgumentParser(description="IT Asset System MongoDB backup/restore")
    parser.add_argument("--restore", metavar="FOLDER", help="restore from a backup folder instead of backing up")
    parser.add_argument("--dir", default=str(BACKUP_DIR), help="backup directory (default: backups)")
    parser.add_argument("--keep", type=int, default=BACKUP_KEEP, help="number of backups to keep (default: 14)")
    args = parser.parse_args()

    if args.restore:
        restore(MONGO_URI, args.restore)
    else:
        dump(MONGO_URI, Path(args.dir), args.keep)


if __name__ == "__main__":
    main()
