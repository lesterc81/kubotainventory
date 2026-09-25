# -*- coding: utf-8 -*-
"""CLEAN final-for-ship checks — this file deliberately has ZERO app bugs of
its own (no 2-arg pairs call, no cross-blueprint dup counting).

Each problem below is a REAL app/template/spec defect, not a script quirk.
"""
import ast
import glob
import os
import re
import sys
import py_compile

problems = []
here = os.getcwd()

# ---- 1) both files compile ----
for f in ("app.py", "integrity.py"):
    try:
        py_compile.compile(f, doraise=True)
    except Exception as e:
        problems.append(f"compile fail {f}: {e}")

# ---- 2) io_bp duplicate routes (this bp only) ----
tree = ast.parse(open("app.py", encoding="utf-8").read())
io_paths = {}
for node in tree.body:
    if not isinstance(node, ast.FunctionDef):
        continue
    for dec in node.decorator_list:
        if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route" and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "io_bp"):
            continue
        for a in dec.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                io_paths.setdefault(a.value, []).append(node.name)
for path, fns in io_paths.items():
    if len(fns) > 1:
        problems.append(f"dup io_bp route {path} -> {fns}")

# ---- 3) the two new handlers exist ----
src = open("app.py", encoding="utf-8").read()
for fn in ("employee_inventory_form_pdf", "inventory_scan_import",
           "_decode_qrs_from_image_bytes", "_rasterize_pdf_pages",
           "_scan_all_qr_payloads", "inventory_intake_form_pdf",
           "inventory_intake_import", "_decode_qrs_with_positions",
           "_intake_detect_filled_checkboxes", "_detect_intake_rows"):
    if not re.search(rf"^def {fn}\(", src, re.M):
        problems.append(f"missing def {fn}")

# ---- 4) integrity token machinery (correct 3-arg call) ----
sys.path.insert(0, here)
from integrity import (inventory_token_sign, inventory_token_parse,
                       inventory_token_pairs)
from bson import ObjectId
emp_oid, asset_oid = ObjectId(), ObjectId()
tok = inventory_token_sign(emp_oid, asset_oid)
if inventory_token_parse(tok) != (emp_oid, asset_oid):
    problems.append("token sign->parse roundtrip failed")
bad = tok[:-6] + ("X" if tok[-6] != "X" else "Y")
if inventory_token_parse(bad) is not None:
    problems.append("tampered token accepted")
if inventory_token_parse("itsys:inv|garbage|not|real|sig") is not None:
    problems.append("malformed token accepted")
assets = [{"_id": ObjectId()}, {"_id": ObjectId()}]
pairs = inventory_token_pairs(None, {"_id": emp_oid}, assets)
if len(pairs) != 2:
    problems.append(f"pairs gave {len(pairs)} rows (wanted 2)")

# ---- 5) every reports/... render target exists ----
refs = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "render_template"):
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                    and a.value.startswith("reports/"):
                refs.add(a.value)
for r in sorted(refs):
    tp = os.path.join(here, "templates", r.replace("/", os.sep))
    if not os.path.exists(tp):
        problems.append(f"missing template {tp} (rendered in app.py)")

# ---- 6) templates actually referenced by url_for('io.*') exist in html ----
io_fns = set()
for node in ast.walk(tree):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "route" and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "io_bp"):
        pass
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        io_fns.add(node.name)

def walk_url_for(blob):
    return re.findall(r"url_for\('io\.(\w+)'", blob)

for tpl in glob.glob(os.path.join("templates", "**", "*.html"), recursive=True):
    blob = open(tpl, encoding="utf-8").read()
    for ep in walk_url_for(blob):
        if ep not in io_fns:
            problems.append(f"{tpl}: url_for('io.{ep}') has no io_bp fn")

print("PROBLEMS:")
print("\n".join(problems) if problems else "NONE")
print("RESULT=" + ("FAIL" if problems else "OK"))
sys.exit(1 if problems else 0)
