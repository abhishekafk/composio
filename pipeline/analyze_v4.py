"""Analysis and Reporting script for Phase v4 Verification (v3 vs v4)."""

import json
from collections import Counter, defaultdict
from pathlib import Path
import statistics


def analyze_v3_v4():
    v3_path = Path("data/runs/v3.jsonl")
    v4_path = Path("data/runs/v4.jsonl")

    if not v3_path.exists() or not v4_path.exists():
        print("v3 or v4 run file not found.")
        return

    v3_recs = {}
    with open(v3_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v3_recs[int(r["id"])] = r

    v4_recs = {}
    with open(v4_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v4_recs[int(r["id"])] = r

    print(f"Total v3 records: {len(v3_recs)}, Total v4 records: {len(v4_recs)}")

    # 1. Unknown counts per field vs v3
    fields_to_check = [
        "description", "auth_methods", "access_model", "gate_detail",
        "api_type", "api_breadth", "official_mcp", "buildability",
        "primary_blocker", "needs_human"
    ]

    v3_unknowns = Counter()
    v4_unknowns = Counter()

    for r in v3_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v3_unknowns[f] += 1

    for r in v4_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v4_unknowns[f] += 1

    print("\n=================== 1. UNKNOWNS: V3 VS V4 ===================")
    print(f"{'Field':<20} | {'v3 Unknowns':<12} | {'v4 Unknowns':<12} | {'Diff':<8}")
    print("-" * 60)
    for f in fields_to_check:
        u3 = v3_unknowns[f]
        u4 = v4_unknowns[f]
        diff = u4 - u3
        sign = "+" if diff > 0 else ""
        print(f"{f:<20} | {u3:<12} | {u4:<12} | {sign}{diff:<8}")

    # 2. needs_human by category (v4)
    print("\n=================== 2. NEEDS_HUMAN PER CATEGORY (V4) ===================")
    cat_totals = Counter()
    cat_nh = Counter()
    for r in v4_recs.values():
        c = r.get("category", "unknown")
        cat_totals[c] += 1
        if r.get("needs_human"):
            cat_nh[c] += 1

    print(f"{'Category':<20} | {'needs_human':<12} | {'Total':<8} | {'Pct':<8}")
    print("-" * 55)
    for c in sorted(cat_totals.keys()):
        nh = cat_nh[c]
        tot = cat_totals[c]
        pct = (nh / tot) * 100
        print(f"{c:<20} | {nh:<12} | {tot:<8} | {pct:.1f}%")
    print(f"Total needs_human: {sum(cat_nh.values())} / {len(v4_recs)}")

    # 3. Vendor-domain quotes per critical field
    print("\n=================== 3. VENDOR-DOMAIN QUOTES FOR CRITICAL FIELDS ===================")
    crit_fields = ["auth_methods", "access_model", "buildability", "official_mcp"]
    vendor_quotes_count = Counter()

    for r in v4_recs.values():
        ev_list = r.get("evidence", [])
        for cf in crit_fields:
            has_vendor = any(
                e.get("field") == cf and e.get("is_official_domain") is True and e.get("quote")
                and not any(x in e.get("quote", "") for x in ["...", "…"])
                for e in ev_list
            )
            if has_vendor:
                vendor_quotes_count[cf] += 1

    for cf in crit_fields:
        count = vendor_quotes_count[cf]
        pct = (count / len(v4_recs)) * 100
        print(f"{cf:<20} | {count} apps ({pct:.1f}%)")

    # 4. Crawler stats (pages fetched per app, blocked hosts, jina fallback hosts)
    print("\n=================== 4. CRAWLER STATISTICS ===================")
    pages_fetched_list = []
    blocked_hosts = set()
    jina_hosts = set()

    for r in v4_recs.values():
        meta = r.get("_run_meta", {}).get("v4_verification", {})
        cnt = meta.get("pages_fetched_count", 0)
        pages_fetched_list.append(cnt)

    # Check cached pages to find jina fallbacks and blocked robots
    from pipeline.verify_v4 import _crawler_stats
    print(f"Pages fetched per app: Min={min(pages_fetched_list) if pages_fetched_list else 0}, "
          f"Median={statistics.median(pages_fetched_list) if pages_fetched_list else 0}, "
          f"Max={max(pages_fetched_list) if pages_fetched_list else 0}")

    # 5. Top 10 biggest changes from v3
    print("\n=================== 5. TOP 10 BIGGEST CHANGES FROM V3 ===================")
    changes = []
    impact_rank = {"access_model": 1, "buildability": 2, "auth_methods": 3, "official_mcp": 4, "primary_blocker": 5}

    for app_id, v4 in v4_recs.items():
        v3 = v3_recs.get(app_id, {})
        app_name = v4.get("app")
        for fld in ["access_model", "buildability", "auth_methods", "official_mcp", "primary_blocker"]:
            old_val = v3.get(fld)
            new_val = v4.get(fld)
            if old_val != new_val:
                # Find supporting quote for new value
                ev_matches = [
                    e for e in v4.get("evidence", [])
                    if e.get("field") == fld and e.get("is_official_domain") is True
                ]
                quote = ev_matches[0].get("quote") if ev_matches else "No vendor quote (derived/fallback)"
                changes.append({
                    "app": app_name,
                    "field": fld,
                    "old": old_val,
                    "new": new_val,
                    "quote": quote,
                    "priority": impact_rank.get(fld, 10),
                })

    changes.sort(key=lambda x: x["priority"])

    for i, ch in enumerate(changes[:10], 1):
        print(f"{i}. [{ch['app']}] {ch['field']}:")
        print(f"   Before (v3): {ch['old']}")
        print(f"   After  (v4): {ch['new']}")
        print(f"   Quote: {repr(ch['quote'][:100])}\n")


if __name__ == "__main__":
    analyze_v3_v4()
