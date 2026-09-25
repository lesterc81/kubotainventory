# -*- coding: utf-8 -*-
"""Diagnose WHY inventory_token_parse returns None for a fresh self-signed token."""
import time
import integrity
from bson import ObjectId

emp_oid = ObjectId()
asset_oid = ObjectId()
tok = integrity.inventory_token_sign(emp_oid, asset_oid)
print("TOKEN:", tok)
parsed = integrity.inventory_token_parse(tok)
print("PARSED:", parsed)

# --- dig into the raw parts to see why verify fails ---
body = tok[len("itsys:"):]
raw, sig = body.rsplit("|", 1)
print("RAW  :", raw)
print("SIG  :", sig)
print("PARTS:", raw.split("|"), "len=", len(raw.split("|")))

# does _inv_sig_ok accept our own sig?
print("SIG_OK:", integrity._inv_sig_ok(raw, sig))
# TTL check
try:
    issued_ts = int(raw.split("|")[3])
    print("AGE_SECONDS:", time.time() - issued_ts,
          "TTL_SECONDS:", integrity.INVENTORY_TOKEN_TTL_DAYS * 86400)
except Exception as e:
    print("ts split err:", e)
