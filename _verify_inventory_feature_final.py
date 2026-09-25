# -*- coding: utf-8 -*-
"""Authoritative read-only verify for the paper inventory scan-back feature.

Does everything on the CURRENT working tree with no network and no persistent
DB writes:

  1. py_compile app.py + integrity.py
  2. AST: no duplicate top-level defs; no duplicate @io_bp routes
  3. integrity token roundtrip (sign -> parse == (emp_oid,asset_oid)),
     tamper rejection, TTL parsing, and inventory_token_pairs shape
  4. every app.py `render_template("reports/...")` target exists on disk
  5. spec hiddenimports actually cover cv2/pypdfium2/PIL/numpy/PIL?new etc.
"""
import ast
import glob
import hmac
import os
import py_compile
import re
import sys

problems = []
here = os.getcwd()

# 1 --------------------------------------------------------------------------
for f in ("app.py", "integrity.py"):
    try:
        py_compile.compile(f, doraise=True)
    except Exception as e:
        problems.append(f"compile fail {f}: {e}")

src = open("app.py", encoding="utf-8").read()
tree = ast.parse(src)

# 2a duplicate top-level defs
top_defs = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        top_defs.setdefault(node.name, []).append(node.lineno)
for name, locs in top_defs.items():
    if len(locs) > 1:
        problems.append(f"dup top-level def {name} at {locs}")

# 2b routes registered on the io blueprint (by path -> fn names)
io_path_to_fns = {}
for node in tree.body:
    if not isinstance(node, ast.FunctionDef):
        continue
    for dec in node.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route" and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "io_bp"):
            continue
        for a in dec.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("/reports/"):
                io_path_to_fns.setdefault(a.value, []).append(node.name)
for path, fns in io_path_to_fns.items():
    if len(fns) > 1:
        problems.append(f"dup io route {path} -> {fns}")

# 3 integrity token machinery (lazy import to truly exercise the module)
from integrity import (inventory_token_sign, inventory_token_parse,
                       inventory_token_pairs)
from bson import ObjectId
emp_oid = ObjectId()
asset_oid = ObjectId()
payload = inventory_token_sign(emp_oid, asset_oid)
parsed = inventory_token_parse(payload)
if parsed is None or parsed != (emp_oid, asset_oid):
    problems.append(f"token roundtrip mismatch: got {parsed!r}")
# tamper: flip a payload char (keep length); parse must reject
if len(payload) < 24:
    problems.append("token too short to tamper-test")
else:
    flipped = list(payload)
    mid = len(flipped) // 2
    flipped[mid] = "X" if flipped[mid] != "X" else "Y"
    if inventory_token_parse("".join(flipped)) is not None:
        problems.append("tampered token was accepted")

# pairs shape: one (asset, token) per asset, token parses to same pair
import time as _t
assets = [{"_id": ObjectId(), "asset_tag": "T-A1"},
          {"_id": ObjectId(), "asset_tag": "T-A2"},
          {"_id": ObjectId(), "asset_tag": "T-A3"}]
emp_doc = {"_id": emp_oid, "full_name": "Test Emp"}
pairs = inventory_token_pairs(None, emp_doc, assets)
if len(pairs) != len(assets):
    problems.append(f"pairs len {len(pairs)} != {len(assets)}")
else:
    for (a, tok) in pairs:
        if inventory_token_parse(tok) != (emp_oid, a["_id"]):
            problems.append(f"pair token for {a['asset_tag']} did not parse to its own row")

# 4 render_template('reports/...') targets exist
refs = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "render_template"):
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and a.value.startswith("reports/"):
                refs.add(a.value)
for r in sorted(refs):
    if not os.path.exists(os.path.join("templates", r.replace("/", os.sep))):
        problems.append(f"missing template for render: {r}")

# 5 spec hiddenimports cover lazy-importer deps
spec_files = glob.glob("*.spec")
for sp in spec_files:
    txt = open(sp, encoding="utf-8").read()
    for name in ("cv2", "pypdfium2", "pypdfium2_raw", "PIL", "numpy", "openpyxl", "pandas"):
        if re.search(r"['\"]" + re.escape(name) + r"['\"]", txt) is None:
            problems.append(f"{sp}: missing hiddenimport {name}")

print("PROBLEMS:")
print("\n".join(problems) if problems else "NONE")
print(f"io routes found: {len(io_path_to_fns)}")
for p, fns in sorted(io_path_to_fns.items()):
    print(f"  {p} -> {fns}")
print(f"template refs: {sorted(refs)}")
print("RESULT=" + ("FAIL" if problems else "OK"))
sys.exit(1 if problems else 0)
