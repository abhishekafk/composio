"""Evaluation and Scoring Pipeline (Phase 5).

Rules:
1. Skip any gold field that is null.
2. Per run report separately for subsets:
   - all (overall scored)
   - random30 (set == "random30")
   - hard (set == "hard")
   - zero_hit (set == "zero_hit")
   - blind-only (blind == True)
3. Fields scored:
   - auth_methods: exact set match = correct (1.0); overlap or a value in auth_partial_ok = partial (0.5); else wrong (0.0).
   - official_mcp: exact enum match (1.0 vs 0.0).
   - access_coarse: mapped from access_model:
     - self_serve_* -> self_serve
     - approval_required, partner_or_contract, customer_only -> gated
     - no_public_api -> no_public_api
   - access_model (fine label): exact match, only where gold is non-null; honour access_partial_ok (0.5).
   - api_type: set match: exact set match (1.0); overlap (0.5); else wrong (0.0).
4. Metrics reported per field:
   - Coverage: share answered (not null, not unknown, not [])
   - Accuracy when answered: score among answered items
   - Overall correct over scored: score over total scored items (unknown counts as 0, never excluded)
   - Wilson 95% confidence interval on overall correct over scored
5. Also report:
   - Share of answered fields that have a vendor-domain verbatim quote
   - Complete miss listing: (app, field, agent value, gold value, evidence URL)
"""

import os
import sys
import json
import math
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set


def wilson_score_interval(k: float, n: int, confidence: float = 0.95) -> Tuple[float, float]:
    """Calculate Wilson score interval (default 95% confidence)."""
    if n <= 0:
        return 0.0, 0.0
    z = 1.95996  # 95% confidence z-score
    p = k / n
    denominator = 1.0 + (z * z) / n
    center = (p + (z * z) / (2.0 * n)) / denominator
    margin = (z / denominator) * math.sqrt((p * (1.0 - p) / n) + ((z * z) / (4.0 * n * n)))
    return max(0.0, center - margin), min(1.0, center + margin)


def map_access_coarse(access_model: Optional[str]) -> Optional[str]:
    """Map fine-grained access_model to coarse categories."""
    if not access_model or access_model == "unknown":
        return "unknown"
    acc = str(access_model).lower()
    if acc.startswith("self_serve_") or acc == "self_serve":
        return "self_serve"
    if acc in ["approval_required", "partner_or_contract", "customer_only", "gated"]:
        return "gated"
    if acc in ["no_public_api"]:
        return "no_public_api"
    return "unknown"


def has_vendor_verbatim_quote(evidence_list: List[Dict[str, Any]], field: str) -> Tuple[bool, Optional[str]]:
    """Check if evidence list contains an official vendor quote without ellipses for a field."""
    for e in evidence_list:
        if e.get("field") == field:
            u = e.get("url")
            q = e.get("quote", "")
            is_off = e.get("is_official_domain", False)
            if is_off and q and not any(x in q for x in ["...", "…"]) and not e.get("_quote_unsupported"):
                return True, u
    return False, None


def score_single_field(
    field: str,
    agent_val: Any,
    gold_val: Any,
    gold_rec: Dict[str, Any],
) -> Tuple[bool, float, str]:
    """Score an individual field against gold.

    Returns:
      (is_answered, score, outcome_label) where outcome_label in ['correct', 'partial', 'wrong', 'unanswered']
    """
    # 1. Determine if answered
    is_answered = True
    if agent_val is None or agent_val == "unknown" or agent_val == [] or agent_val == "":
        is_answered = False

    if not is_answered:
        return False, 0.0, "unanswered"

    # 2. Field-specific scoring
    if field == "auth_methods":
        agent_set = set(agent_val) if isinstance(agent_val, (list, set, tuple)) else {str(agent_val)}
        gold_set = set(gold_val) if isinstance(gold_val, (list, set, tuple)) else {str(gold_val)}

        if agent_set == gold_set:
            return True, 1.0, "correct"

        # Check partial credit: overlap or in auth_partial_ok
        partial_ok = set(gold_rec.get("auth_partial_ok", []))
        if (agent_set & gold_set) or (agent_set & partial_ok):
            return True, 0.5, "partial"

        return True, 0.0, "wrong"

    elif field == "official_mcp":
        if str(agent_val).lower() == str(gold_val).lower():
            return True, 1.0, "correct"
        return True, 0.0, "wrong"

    elif field == "access_coarse":
        agent_coarse = map_access_coarse(agent_val)
        gold_coarse = map_access_coarse(gold_val)
        if agent_coarse == "unknown":
            return False, 0.0, "unanswered"
        if agent_coarse == gold_coarse:
            return True, 1.0, "correct"
        return True, 0.0, "wrong"

    elif field == "access_model":
        if str(agent_val).lower() == str(gold_val).lower():
            return True, 1.0, "correct"

        # Check access_partial_ok
        partial_ok = [str(p).lower() for p in gold_rec.get("access_partial_ok", [])]
        if str(agent_val).lower() in partial_ok:
            return True, 0.5, "partial"

        return True, 0.0, "wrong"

    elif field == "api_type":
        agent_set = set(agent_val) if isinstance(agent_val, (list, set, tuple)) else {str(agent_val)}
        gold_set = set(gold_val) if isinstance(gold_val, (list, set, tuple)) else {str(gold_val)}

        if agent_set == gold_set:
            return True, 1.0, "correct"
        if agent_set & gold_set:
            return True, 0.5, "partial"
        return True, 0.0, "wrong"

    else:
        # Default equality
        if agent_val == gold_val:
            return True, 1.0, "correct"
        return True, 0.0, "wrong"


def evaluate_run(
    run_records: Dict[Any, Dict[str, Any]],
    gold_records: Dict[Any, Dict[str, Any]],
    run_name: str = "run",
) -> Dict[str, Any]:
    """Evaluate a run against gold labels."""
    fields_to_score = ["auth_methods", "official_mcp", "access_coarse", "access_model", "api_type"]

    subsets = {
        "all": lambda r: True,
        "random30": lambda r: r.get("set") == "random30",
        "hard": lambda r: r.get("set") == "hard",
        "zero_hit": lambda r: r.get("set") == "zero_hit",
        "blind_only": lambda r: bool(r.get("blind", False)),
    }

    results = {}
    misses = []
    total_answered_fields = 0
    total_vendor_quoted_fields = 0

    for subset_name, subset_filter in subsets.items():
        subset_gold_keys = [k for k, g in gold_records.items() if subset_filter(g)]
        if not subset_gold_keys:
            continue

        subset_metrics = {}
        for fld in fields_to_score:
            scored_count = 0
            answered_count = 0
            correct_points = 0.0
            exact_correct_count = 0
            partial_count = 0

            for k in subset_gold_keys:
                g_rec = gold_records[k]
                # If field is access_coarse, derive gold value from access_coarse or access_model
                if fld == "access_coarse":
                    gold_val = g_rec.get("access_coarse", g_rec.get("access_model"))
                else:
                    gold_val = g_rec.get(fld)

                # Skip any gold field that is null
                if gold_val is None or gold_val == "":
                    continue

                scored_count += 1
                agent_rec = run_records.get(k, {})

                if fld == "access_coarse":
                    agent_val = agent_rec.get("access_model")
                else:
                    agent_val = agent_rec.get(fld)

                is_ans, points, outcome = score_single_field(fld, agent_val, gold_val, g_rec)

                # Check vendor-domain verbatim quote for answered fields (counted for subset 'all')
                if subset_name == "all" and is_ans:
                    total_answered_fields += 1
                    ev_field = "access_model" if fld == "access_coarse" else fld
                    has_quote, ev_url = has_vendor_verbatim_quote(agent_rec.get("evidence", []), ev_field)
                    if has_quote:
                        total_vendor_quoted_fields += 1

                if is_ans:
                    answered_count += 1
                    correct_points += points
                    if points == 1.0:
                        exact_correct_count += 1
                    elif points == 0.5:
                        partial_count += 1

                # Record miss (if wrong, partial, or unanswered)
                if subset_name == "all" and points < 1.0:
                    ev_field = "access_model" if fld == "access_coarse" else fld
                    _, ev_url = has_vendor_verbatim_quote(agent_rec.get("evidence", []), ev_field)
                    if not ev_url and agent_rec.get("evidence"):
                        ev_matches = [e.get("url") for e in agent_rec.get("evidence", []) if e.get("field") == ev_field]
                        ev_url = ev_matches[0] if ev_matches else "No evidence URL"

                    misses.append({
                        "app": g_rec.get("app", agent_rec.get("app", f"ID {k}")),
                        "field": fld,
                        "agent_value": agent_val,
                        "gold_value": gold_val,
                        "outcome": outcome,
                        "evidence_url": ev_url or "None",
                    })

            coverage = (answered_count / scored_count) if scored_count > 0 else 0.0
            acc_answered = (correct_points / answered_count) if answered_count > 0 else 0.0
            overall_correct = (correct_points / scored_count) if scored_count > 0 else 0.0
            ci_low, ci_high = wilson_score_interval(correct_points, scored_count)

            subset_metrics[fld] = {
                "scored": scored_count,
                "answered": answered_count,
                "coverage": coverage,
                "accuracy_when_answered": acc_answered,
                "overall_correct": overall_correct,
                "wilson_ci": (ci_low, ci_high),
                "exact": exact_correct_count,
                "partial": partial_count,
            }

        results[subset_name] = {
            "subset_size": len(subset_gold_keys),
            "metrics": subset_metrics,
        }

    vendor_quote_share = (total_vendor_quoted_fields / total_answered_fields) if total_answered_fields > 0 else 0.0

    return {
        "run_name": run_name,
        "subsets": results,
        "vendor_quote_share": vendor_quote_share,
        "total_answered_fields": total_answered_fields,
        "total_vendor_quoted_fields": total_vendor_quoted_fields,
        "misses": misses,
    }


def print_evaluation_report(eval_data: Dict[str, Any]):
    """Format and print benchmark scoring report."""
    run_name = eval_data["run_name"]
    print(f"\n=========================================================================================")
    print(f"BENCHMARK SCORING REPORT: {run_name.upper()}")
    print(f"=========================================================================================")
    print(f"Share of Answered Fields with Vendor-Domain Verbatim Quote: "
          f"{eval_data['vendor_quote_share']*100:.1f}% "
          f"({eval_data['total_vendor_quoted_fields']} / {eval_data['total_answered_fields']})")

    for subset_name, sub_info in eval_data["subsets"].items():
        print(f"\n--- SUBSET: {subset_name.upper()} (N={sub_info['subset_size']}) ---")
        print(f"{'Field':<16} | {'Scored':<7} | {'Coverage':<9} | {'Acc (Ans)':<10} | {'Overall Correct':<16} | {'Wilson 95% CI':<16}")
        print("-" * 85)
        for fld, m in sub_info["metrics"].items():
            ci_str = f"[{m['wilson_ci'][0]*100:.1f}%, {m['wilson_ci'][1]*100:.1f}%]"
            print(f"{fld:<16} | {m['scored']:<7} | {m['coverage']*100:>7.1f}% | {m['accuracy_when_answered']*100:>8.1f}% | {m['overall_correct']*100:>14.1f}% | {ci_str:<16}")

    print(f"\n--- TOTAL MISSES & ERRORS: {len(eval_data['misses'])} ---")
    for idx, m in enumerate(eval_data["misses"][:20], 1):
        print(f"{idx:2d}. [{m['app']}] {m['field']}:")
        print(f"    Agent: {m['agent_value']} ({m['outcome']})")
        print(f"    Gold:  {m['gold_value']}")
        print(f"    URL:   {m['evidence_url']}")

    if len(eval_data["misses"]) > 20:
        print(f"... and {len(eval_data['misses']) - 20} more misses.")


def load_gold_records(gold_path: Path) -> Dict[Any, Dict[str, Any]]:
    """Load gold records from JSON (supporting dict of id->obj or list of objs)."""
    with open(gold_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    gold = {}
    if isinstance(data, list):
        for idx, item in enumerate(data):
            k = item.get("id", item.get("app", idx))
            gold[k] = item
    elif isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                v["id"] = v.get("id", k)
                gold[k] = v
    return gold


def load_run_records(run_path: Path) -> Dict[Any, Dict[str, Any]]:
    """Load agent run records from JSONL."""
    recs = {}
    with open(run_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if line.strip():
                r = json.loads(line)
                k = r.get("id", r.get("app", idx))
                recs[str(k)] = r
                recs[k] = r
                if "app" in r:
                    recs[r["app"].lower()] = r
    return recs


def main():
    parser = argparse.ArgumentParser(description="Composio Research Scoring Pipeline")
    parser.add_argument("--gold", type=str, required=True, help="Path to gold JSON file")
    parser.add_argument("--run", type=str, default=None, help="Path to specific run JSONL (or omit to score v1, v2, v3)")
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"Gold file not found: {gold_path}")
        sys.exit(1)

    gold_records = load_gold_records(gold_path)
    print(f"Loaded {len(gold_records)} gold records from {gold_path}")

    runs_to_score = []
    if args.run:
        runs_to_score.append((Path(args.run).stem, Path(args.run)))
    else:
        for run_name in ["v1", "v2", "v3"]:
            p = Path(f"data/runs/{run_name}.jsonl")
            if p.exists():
                runs_to_score.append((run_name, p))

    for run_name, run_path in runs_to_score:
        run_recs = load_run_records(run_path)
        eval_result = evaluate_run(run_recs, gold_records, run_name=run_name)
        print_evaluation_report(eval_result)


if __name__ == "__main__":
    main()
