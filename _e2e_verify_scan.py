# -*- coding: utf-8 -*-
"""Authoritative pre-ship verify for the paper-inventory scan feature.

Read-only over the current working tree. No network. No real DB content
needed (tokens are pure-HMAC over ObjectIds). AST-based so it also proves
the *wiring* (routes registered on the io blueprint + every render_template
target that actually exists on disk).

Exit code: 0 = all green, 1 = problems.
"""
import ast
import glob
import os
import py_compile
import re
import subprocess
import sys

problems = []

# 1) compile the whole app + integrity (catches syntax AND duplicate defs at
#    the same name would only be a warning, so we ALSO do an AST dup-def scan)
for f in ("app.py", "integrity.py"):
    try:
        py_compile.compile(f, doraise=True)
    except Exception as e:
        problems.append(f"compile fail {f}: {e}")

# AST scans
src = open("app.py", encoding="utf-8").read()
tree = ast.parse(src)
problems_ast = []

# duplicate top-level function names
top_fns = {}
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        top_fns.setdefault(node.name, []).append(node.lineno)
for name, locs in top_fns.items():
    if len(locs) > 1:
        problems.append(f"dup top-level def: {name} at {locs}")

# 2) route decorators on io_bp -> collect (path -> [fns])
io_bp_name = None
io_routes = {}
for node in tree.body:
    if isinstance(node, ast.FunctionDef):
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                if getattr(dec.func.value, "id", None) != "io_bp":
                    continue
                if dec.func.attr != "route":
                    continue
                path = None
                for a in dec.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        path = a.value
                if path:
                    io_routes.setdefault(path, []).append(node.name)

bad_dup_routes = [p for p, fns in io_routes.items() if len(fns) > 1]
if bad_dup_routes:
    problems.append(f"duplicate io routes: {bad_dup_routes}")

# 3) every render_template('reports/...') must have a real template file
render_refs = set()
for node in ast.walk(tree):
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "render_template"):
        continue
    for a in node.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                and a.value.startswith("reports/"):
            render_refs.add(a.value)
for r in sorted(render_refs):
    p = os.path.join("templates", r)
    if not os.path.isfile(p):
        problems.append(f"render_template target missing: {p}")

# 4) the NEW routes must exist by name + path
expected_routes = {
    "/reports/employee-inventory-form/<employee_id>/pdf": "employee_inventory_form_pdf",
    "/reports/inventory-scan/import": "inventory_scan_import",
}
for path, fn in expected_routes.items():
    if path not in io_routes:
        problems.append(f"missing io route: {path}")
    elif fn not in io_routes[path]:
        problems.append(f"route {path} has fn {io_routes[path]} not {fn}")

# 5) helper defs must exist
for h in ("_decode_qrs_from_image_bytes", "_rasterize_pdf_pages",
          "_scan_all_qr_payloads"):
    if h not in top_fns:
        problems.append(f"missing helper: {h}")

# 6) integrity inventory-token API must exist and round-trip (no tamper)
from integrity import (inventory_token_sign, inventory_token_parse,
                       inventory_token_pairs)
from bson import ObjectId
emp_oid = ObjectId()
asset_oid = ObjectId()
tok = inventory_token_sign(emp_oid, asset_oid)
parsed = inventory_token_parse(tok)
if parsed is None or parsed != (emp_oid, asset_oid):
    problems.append(f"token roundtrip mismatch: got {parsed!r}")
tampered = tok[:-4] + ("XXXX" if not tok.endswith("XXXX") else "YYYY")
if inventory_token_parse(tampered) is not None:
    problems.append("tampered token was accepted")

# 7) pairs helper honors per-row = one token per asset
pairs = inventory_token_pairs(None, {"_id": emp_oid},
                              [{"_id": ObjectId()}, {"_id": ObjectId()}])
if len(pairs) != 2:
    problems.append(f"inventory_token_pairs returned {len(pairs)} (wanted 2)")

print("io routes registered:", len(io_routes))
print("render_template(reports/...) targets:", sorted(render_refs))
print("PROBLEMS:")
print("\n".join(problems) if problems else "NONE")
print("RESULT=" + ("FAIL" if problems else "OK"))
sys.exit(1 if problems else 0)
