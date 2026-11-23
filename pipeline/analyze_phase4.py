"""Analysis and Reporting script for Phase 4 Verification (v1 vs v2)."""

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Any


def analyze_v1_v2():
    v1_path = Path("data/runs/v1.jsonl")
    v2_path = Path("data/runs/v2.jsonl")

    if not v1_path.exists() or not v2_path.exists():
        print("v1 or v2 run file not found.")
        return

    v1_recs = {}
    with open(v1_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v1_recs[int(r["id"])] = r

    v2_recs = {}
    with open(v2_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                v2_recs[int(r["id"])] = r

    print(f"Total v1 records: {len(v1_recs)}, Total v2 records: {len(v2_recs)}")

    # 1. Unknown counts per field vs v1
    fields_to_check = [
        "description", "auth_methods", "access_model", "gate_detail",
        "api_type", "api_breadth", "official_mcp", "buildability",
        "primary_blocker", "needs_human"
    ]

    v1_unknowns = Counter()
    v2_unknowns = Counter()

    for r in v1_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v1_unknowns[f] += 1

    for r in v2_recs.values():
        for f in fields_to_check:
            val = r.get(f)
            if val is None or val == "unknown" or val == [] or val == "":
                v2_unknowns[f] += 1

    # 2. Changes per field & by Rule
    field_changes_count = Counter()
    rule_counts = Counter()
    changes_by_field_rule = defaultdict(Counter)
    all_corrections = []

    for app_id, v2 in v2_recs.items():
        v1 = v1_recs.get(app_id, {})
        v_meta = v2.get("_run_meta", {}).get("verification", {})
        changes = v_meta.get("changes", {})
        if isinstance(changes, dict):
            change_items = list(changes.items())
        elif isinstance(changes, list):
            change_items = [(ch.get("field", ""), ch) if isinstance(ch, dict) else ("", {}) for ch in changes]
        else:
            change_items = []

        for fld, ch in change_items:
            if not isinstance(ch, dict):
                continue
            rule = ch.get("rule", "")
            old_val = ch.get("old")
            new_val = ch.get("new")
            reason = ch.get("reason", "")
            field_changes_count[fld] += 1
            rule_counts[rule] += 1
            changes_by_field_rule[fld][rule] += 1
            all_corrections.append({
                "app_id": app_id,
                "app": v2.get("app"),
                "field": fld,
                "old": old_val,
                "new": new_val,
                "rule": rule,
                "reason": reason,
            })

    # 3. Model & Key usage
    model_counts = Counter()
    key_counts = Counter()
    for v2 in v2_recs.values():
        v_meta = v2.get("_run_meta", {}).get("verification", {})
        model_counts[v_meta.get("verifier_model", "unknown")] += 1
        key_counts[v_meta.get("key_idx", "unknown")] += 1

    # Print summary tables
    print("\n=================== 1. UNKNOWNS: V1 VS V2 ===================")
    print(f"{'Field':<20} | {'v1 Unknowns':<12} | {'v2 Unknowns':<12} | {'Diff':<8}")
    print("-" * 60)
    for f in fields_to_check:
        u1 = v1_unknowns[f]
        u2 = v2_unknowns[f]
        diff = u2 - u1
        sign = "+" if diff > 0 else ""
        print(f"{f:<20} | {u1:<12} | {u2:<12} | {sign}{diff:<8}")

    print("\n=================== 2. FIELD CHANGES BY RULE ===================")
    print(f"{'Field':<20} | {'Total Changes':<14} | Rule Breakdown")
    print("-" * 60)
    for f, count in field_changes_count.most_common():
        rules_str = ", ".join(f"Rule {r}: {c}" for r, c in changes_by_field_rule[f].items())
        print(f"{f:<20} | {count:<14} | {rules_str}")

    print("\n=================== 3. RULE TOTALS ===================")
    rule_desc = {
        "A": "Deterministic fixes (URL hosts, quote substrings, timestamps)",
        "B": "Benchmark cross-checks (Composio auth & MCP registry)",
        "C": "LLM Verifier (contradicted / insufficient evidence)",
        "D": "Targeted domain re-research (vendor docs extraction)",
        "E": "Dedicated MCP verification",
    }
    for r, count in sorted(rule_counts.items()):
        print(f"Rule {r} ({rule_desc.get(r, '')}): {count} modifications")

    print("\n=================== 4. VERIFIER MODELS & KEYS ===================")
    for m, c in model_counts.items():
        print(f"Model {m}: {c} apps")
    for k, c in key_counts.items():
        print(f"Key Index {k}: {c} apps")

    # 10 biggest corrections (prioritize buildability, auth_methods, official_mcp, access_model)
    impact_priority = {"buildability": 1, "access_model": 2, "official_mcp": 3, "auth_methods": 4, "primary_blocker": 5}
    ranked_corrections = sorted(
        all_corrections,
        key=lambda x: impact_priority.get(x["field"], 10)
    )

    print("\n=================== 5. TOP 10 CORRECTIONS ===================")
    for i, corr in enumerate(ranked_corrections[:10], 1):
        print(f"{i}. [{corr['app']}] {corr['field']}:")
        print(f"   Before (v1): {corr['old']}")
        print(f"   After  (v2): {corr['new']}")
        print(f"   Rule {corr['rule']}: {corr['reason']}\n")


if __name__ == "__main__":
    analyze_v1_v2()
