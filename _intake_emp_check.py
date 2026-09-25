# -*- coding: utf-8 -*-
"""Verify intake scan now binds the employee at upload time.
   - generic QR tokens still decode from an employee-printed form (header only)
   - form PDF renders with employee header and decodes all 6 rows
   - phase-1 with employee_id returns preview carrying employee + hidden field
   - phase-2 confirm creates Assigned assets for that employee (accountability)
"""
import re
import sys
import os

sys.path.insert(0, os.getcwd())
from app import app, mongo

problems = []
emp = mongo.db.employees.find_one({"status": "Active"})
if not emp:
    problems.append("no active employee")
    print("PROBLEMS:\n" + "\n".join(problems))
    print("RESULT=FAIL"); sys.exit(1)
emp_id = str(emp["_id"])
emp_tag = "INTK-EMP-ZZZZZZZZZZZZZZZZZZZZZZZZ-ROW-007Z"  # unused

with app.test_client() as client:
    # login
    html = client.get("/login").data.decode("utf-8", "ignore")
    tok = re.search(r'name="csrf_token"[^>]+value="([^"]+)"', html)
    token = tok.group(1) if tok else ""
    client.post("/login", data={"csrf_token": token, "username": "admin",
                                "password": "testpass123", "remember": "y"},
                follow_redirects=True)

    # ---- 1) form PDF with employee header (generic QR tokens) ----
    resp = client.get("/reports/intake-form/pdf?employee_id=%s&rows=6" % emp_id)
    if resp.status_code != 200:
        problems.append("form pdf not 200: %s" % resp.status_code)
    else:
        from app import _rasterize_pdf_pages, _decode_qrs_with_positions, _detect_intake_rows
        pages = _rasterize_pdf_pages(resp.data, scale=4.0)
        payloads = [p for pg in pages for p, _ in _decode_qrs_with_positions(pg)]
        print("decoded", len(payloads), "rows", [p[-3:] for p in payloads])
        if len(payloads) != 6:
            problems.append(f"form decoded {len(payloads)} QRs, want 6")
        if any(not p.startswith("INTK-ROW-") for p in payloads):
            problems.append("non-generic token found: " + str(payloads[:3]))

    # ---- 2) phase 1 upload with employee_id -> preview carries employee ----
    # build a fake multi-page PDF from the form (rasterized PNG) to simulate scan
    from io import BytesIO
    from PIL import Image
    pages2 = _rasterize_pdf_pages(resp.data, scale=4.0)
    png_buf = BytesIO()
    Image.open(BytesIO(pages2[0])).save(png_buf, format="PNG")
    png_buf.seek(0)
    resp = client.post("/reports/intake-import", data={
        "csrf_token": token, "employee_id": emp_id,
        "file": (png_buf, "scan.png")}, content_type="multipart/form-data")
    body = resp.data.decode("utf-8", "ignore")
    print("preview status", resp.status_code)
    # preview has the individual upload form only when GET; POST goes to preview
    if resp.status_code != 200:
        problems.append("phase1 preview not 200: %s" % resp.status_code)
    else:
        if 'name="employee_id"' not in body:
            problems.append("preview missing employee_id hidden field")
        if "Intake Scan Preview" not in body:
            problems.append("preview page not rendered")

    # ---- 3) confirm: create Intk-* assets assigned to the employee ----
    row_nos = re.findall(r'name="row_no"\s+value="(\d+)"', body)
    print("preview rows", row_nos)
    if not row_nos:
        problems.append("no rows in preview")
    else:
        data = {"csrf_token": token, "confirm": "1", "employee_id": emp_id,
                "row_no": row_nos}
        for rn in row_nos:
            data["device_type_%s" % rn] = "Laptop"
            data["asset_tag_%s" % rn] = "INTK-TEST-E%d" % int(rn)
            data["serial_%s" % rn] = "TESTSER-%s" % rn
            data["model_%s" % rn] = "Test Model"
            data["remarks_%s" % rn] = "auto test"
        resp = client.post("/reports/intake-import", data=data)
        resp2 = resp.data.decode("utf-8", "ignore")
        print("confirm status", resp.status_code)
        created_tags = re.findall(r"INTK-TEST-E\d+", resp2)
        print("created tags in result", sorted(set(created_tags)))
        if resp.status_code != 200:
            problems.append("confirm not 200: %s" % resp.status_code)
        want = set("INTK-TEST-E%d" % int(rn) for rn in row_nos)
        if set(created_tags) != want:
            problems.append("result page missing created tags: got %s want %s"
                            % (sorted(set(created_tags)), sorted(want)))
        # verify DB state
        made = list(mongo.db.assets.find({"asset_tag": {"$in": list(want)}}))
        print("db assets", len(made), "assigned",
              sum(1 for a in made if a.get("assigned_to") == emp_id))
        for a in made:
            row_name = a["asset_tag"]
            if a.get("status") != "Assigned":
                problems.append("%s status=%s" % (row_name, a.get("status")))
            if a.get("assigned_to") != emp_id:
                problems.append("%s assigned_to=%s" % (row_name, a.get("assigned_to")))
            if not any(h.get("event") == "Assigned" for h in a.get("history", [])):
                problems.append("%s missing Assigned history" % row_name)
        active_acc = mongo.db.accountabilities.find_one(
            {"employee_id": emp_id, "status": "Active"}) or \
            mongo.db.accountabilities.find_one(
            {"employee": emp_id, "status": "Active"})
        if active_acc is None:
            problems.append("no active accountability found for employee")
        else:
            acc_assets = active_acc.get("asset_ids", [])
            for a in made:
                if a["_id"] not in acc_assets:
                    problems.append("%s not in accountability" % a["asset_tag"])
        # cleanup
        ids = [a["_id"] for a in made]
        mongo.db.assets.delete_many({"_id": {"$in": ids}})
        if active_acc is not None:
            mongo.db.accountabilities.update_one(
                {"_id": active_acc["_id"]},
                {"$pull": {"asset_ids": {"$in": ids}}})
        mongo.db.audits.delete_many({"record_id": {"$in": ids}})
        print("cleanup done")

print("PROBLEMS:")
print("\n".join(problems) if problems else "NONE")
print("RESULT=" + ("FAIL" if problems else "OK"))
sys.exit(1 if problems else 0)