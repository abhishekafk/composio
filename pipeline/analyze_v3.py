"""Analysis and Reporting script for Phase v3 Verification (v2 vs v3)."""

import json
from collections import Counter, defaultdict
from pathlib import Path


def analyze_v2_v3():
    v2_path = Path("data/runs/v2.jsonl")
    v3_path = Path("data/runs/v3.jsonl")

    if not v2_path.exists() or not v3_path.exists():
        print("v2 or v3 run file not found.")
        return

    v2_recs = {}
    with open(v2_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v2_recs[int(r["id"])] = r

    v3_recs = {}
    with open(v3_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v3_recs[int(r["id"])] = r

    print(f"Total v2 records: {len(v2_recs)}, Total v3 records: {len(v3_recs)}")

    # 1. Unknown counts per field vs v2
    fields_to_check = [
        "description", "auth_methods", "access_model", "gate_detail",
        "api_type", "api_breadth", "official_mcp", "buildability",
        "primary_blocker", "needs_human"
    ]

    v2_unknowns = Counter()
    v3_unknowns = Counter()

    for r in v2_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v2_unknowns[f] += 1

    for r in v3_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v3_unknowns[f] += 1

    print("\n=================== 1. UNKNOWNS: V2 VS V3 ===================")
    print(f"{'Field':<20} | {'v2 Unknowns':<12} | {'v3 Unknowns':<12} | {'Diff':<8}")
    print("-" * 60)
    for f in fields_to_check:
        u2 = v2_unknowns[f]
        u3 = v3_unknowns[f]
        diff = u3 - u2
        sign = "+" if diff > 0 else ""
        print(f"{f:<20} | {u2:<12} | {u3:<12} | {sign}{diff:<8}")

    # 2. needs_human count per category
    print("\n=================== 2. NEEDS_HUMAN PER CATEGORY (V3) ===================")
    cat_totals = Counter()
    cat_nh = Counter()
    for r in v3_recs.values():
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
    print(f"Total needs_human: {sum(cat_nh.values())} / {len(v3_recs)}")

    # 3. Vendor-domain quotes per critical field
    print("\n=================== 3. VENDOR-DOMAIN QUOTES FOR CRITICAL FIELDS ===================")
    crit_fields = ["auth_methods", "access_model", "buildability", "official_mcp"]
    vendor_quotes_count = Counter()

    for r in v3_recs.values():
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
        pct = (count / len(v3_recs)) * 100
        print(f"{cf:<20} | {count} apps ({pct:.1f}%)")

    # 4. MCP searches and official_mcp distribution
    print("\n=================== 4. OFFICIAL_MCP DISTRIBUTION & SEARCHES ===================")
    mcp_dist = Counter()
    total_mcp_searches = 0
    for r in v3_recs.values():
        mcp_dist[r.get("official_mcp", "unknown")] += 1
        mcp_searches = r.get("_run_meta", {}).get("v3_verification", {}).get("mcp_searches", [])
        total_mcp_searches += len(mcp_searches)

    for val, count in mcp_dist.most_common():
        print(f"{val:<20} | {count} apps")
    print(f"Total dedicated MCP searches logged: {total_mcp_searches}")

    # 5. Top 10 biggest changes from v2
    print("\n=================== 5. TOP 10 BIGGEST CHANGES FROM V2 ===================")
    changes = []
    impact_rank = {"official_mcp": 1, "buildability": 2, "access_model": 3, "auth_methods": 4, "primary_blocker": 5}

    for app_id, v3 in v3_recs.items():
        v2 = v2_recs.get(app_id, {})
        app_name = v3.get("app")
        for fld in ["official_mcp", "buildability", "access_model", "auth_methods", "primary_blocker"]:
            old_val = v2.get(fld)
            new_val = v3.get(fld)
            if old_val != new_val:
                # Find supporting quote for new value
                ev_matches = [
                    e for e in v3.get("evidence", [])
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
        print(f"   Before (v2): {ch['old']}")
        print(f"   After  (v3): {ch['new']}")
        print(f"   Quote: {repr(ch['quote'][:100])}\n")


if __name__ == "__main__":
    analyze_v2_v3()
