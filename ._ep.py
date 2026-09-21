import re, logging
logging.disable(logging.CRITICAL)
from collections import Counter
from app import create_app, mongo
app = create_app()
with app.app_context():
    eps = mongo.db.assets.distinct("endpoint_name", {"endpoint_name": {"$nin": ["", None]}})
    print("total distinct endpoints:", len(eps))
    pats = Counter()
    for e in eps:
        m = re.match(r"^([A-Za-z]+)([A-Za-z]+?)(\\d+)$", e.strip())
        if m:
            pats[(m.group(1).upper(), m.group(2).upper())] += 1
        else:
            pats[("??", e.strip()[:20])] += 1
    for k, v in pats.most_common(25):
        print(v, k)
    print("--- matching KPIMNLD51 ---")
    for e in eps[:0]:
        pass
    sm = [e for e in eps if e.upper().startswith("KPIMNL")][:8]
    print(sm)
    print("--- devices by letter for KPIMNL ---")
    letters = Counter()
    for e in eps:
        u = e.upper()
        if u.startswith("KPIMNLL"): letters["L"] += 1
        elif u.startswith("KPIMNLD"): letters["D"] += 1
        elif u.startswith("KPIMNLM"): letters["M"] += 1
        elif u.startswith("KPIMNLP"): letters["P"] += 1
    print(letters)