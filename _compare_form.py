# -*- coding: utf-8 -*-
"""Compare sample (image 14.png) vs our intake form geometry."""
import os
import re
import io
os.environ.setdefault("IT_ASSET_MONGO_URI", "")
import cv2
import numpy as np
from app import app
import app as appmod

# --- render our current blank form (4 rows) ---
c = app.test_client()
r = c.get("/login")
m = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', r.data)
token = m.group(1).decode()
c.post("/login", data={"username": "admin", "password": "testpass123",
                       "csrf_token": token})
r = c.get("/reports/intake-form/pdf?rows=4")
pdf_bytes = r.data
with open("samples/OUR_form.pdf", "wb") as fh:
    fh.write(pdf_bytes)
pages = appmod._rasterize_pdf_pages(pdf_bytes, scale=2.0)
with open("samples/OUR_form_p0.png", "wb") as fh:
    fh.write(pages[0])
img = cv2.imdecode(np.frombuffer(pages[0], np.uint8), cv2.IMREAD_COLOR)
print("OUR page0 render:", img.shape)

# --- sample geometry (same pipeline as before but compact) ---
sample = cv2.imread("samples/image (14).png", cv2.IMREAD_GRAYSCALE)
print("SAMPLE:", sample.shape)

# checkboxes in sample: locate the 18x15 & 27x15 & 30x15 rect boxes
def find_boxes(gray, win=6):
    binimg = 255 - gray
    cnts, _ = cv2.findContours(binimg, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        if win - 2 <= w <= 34 and win - 2 <= h <= 34:
            boxes.append((x, y, w, h))
    return boxes

print("== SAMPLE structure ==")
b = find_boxes(sample)
# group by x-center rounded to 25
cols = {}
for x, y, w, h in b:
    k = round((x + w / 2) / 25) * 25
    cols.setdefault(k, []).append((x, y, w, h))
for k in sorted(cols):
    items = cols[k]
    ys = sorted(i[1] for i in items)
    print("sample col cx~%-4d n=%2d y=%s..%s sizes=%s" % (
        k, len(items), ys[0], ys[-1], sorted(set((i[2], i[3]) for i in items))))

print("== OUR page0 (scale2) checkbox cols ==")
ob = find_boxes(img)
ocols = {}
for x, y, w, h in ob:
    k = round((x + w / 2) / 25) * 25
    ocols.setdefault(k, []).append((x, y, w, h))
for k in sorted(ocols):
    items = ocols[k]
    ys = sorted(i[1] for i in items)
    print("our col cx~%-4d n=%2d y=%s..%s sizes=%s" % (
        k, len(items), ys[0], ys[-1], sorted(set((i[2], i[3]) for i in items))))