# -*- coding: utf-8 -*-
import time, traceback
import integrity
from bson import ObjectId

k1 = integrity.chain_key()
k2 = integrity.chain_key()
print("chain_key deterministic:", k1 == k2, "| type:", type(k1).__name__)

emp = ObjectId(); asset = ObjectId()
tok = integrity.inventory_token_sign(emp, asset)
print("TOKEN:", tok[:20] + "..." + tok[-12:], "| chars |:", tok.count("|"))

body = tok[len("itsys:"):]
raw, sig = body.rsplit("|", 1)
print("RAW :", raw)
print("SIG :", sig)
try:
    exp = integrity._sign_inv(raw)
    print("EXPECTED==SIG:", exp == sig)
    print("_inv_sig_ok(raw, sig):", integrity._inv_sig_ok(raw, sig))
except Exception as e:
    print("sig-verify raised:", type(e).__name__, e)

parsed = integrity.inventory_token_parse(tok)
print("PARSED:", parsed)
print("MATCH :", parsed == (emp, asset))
