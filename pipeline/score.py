"""Evaluation and Scoring Pipeline (Phase 5).

Rules & Requirements:
1. Verify SHA-256 of data/gold.json before scoring:
   ae2429ee40a6416bb82ea13e75967a97db6ac15c1fe858a50d91a359b4055c15
2. Skip any gold field that is null.
3. Subsets reported for each version (v1, v2, v3, v4):
   - all (overall scored)
   - random30 (set == "random30")
   - hard (set == "hard")
   - zero_hit (set == "zero_hit")
   - blind_only (blind == True)
4. Scored fields:
   - auth_methods: exact set match = 1.0 (exact); overlap or auth_partial_ok = 0.5 (partial); else 0.0.
   - official_mcp: exact enum match = 1.0 (exact); else 0.0.
   - access_coarse: mapped from access_model (self_serve, gated, none_needed, no_public_api).
   - access_model (fine label): exact match = 1.0; access_partial_ok = 0.5; else 0.0. Only scored where gold is non-null.
   - api_type: exact set match = 1.0; overlap = 0.5; else 0.0. Only scored where gold is non-null.
5. Metrics per field and overall per subset:
   - Coverage: share answered (not null, not unknown, not [])
   - Accuracy when answered: score among answered items
   - Overall correct: score over total scored items (unknown counts as 0, never excluded)
   - Exact-match-only numbers shown separately from partial-credit numbers
   - Wilson 95% confidence intervals on overall correct and exact match
   - Vendor-quote share: share of answered fields with vendor-domain verbatim quotes (no ellipses)
6. Extra Analyses:
   (1) Calibration: accuracy of fields in records with needs_human=false vs needs_human=true, per version.
   (2) v1 only: accuracy split by model (gpt-oss-120b, gpt-oss-20b, fallback failures counted as misses).
   (3) official_mcp confusion table: agent value vs gold value (official, none_found, community_only, unknown),
       highlighting false "none_found" calls.
7. Output full misses list for v4 as a CSV (data/misses_v4.csv).
8. Save data/scores_v1.json ... scores_v4.json.
"""

import os
import sys
import json
import math
import hashlib
import argparse
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set

EXPECTED_GOLD_SHA256 = "ae2429ee40a6416bb82ea13e75967a97db6ac15c1fe858a50d91a359b4055c15"


def verify_file_hash(filepath: Path, expected_sha256: str) -> bool:
    """Verify SHA-256 hash of a file."""
    if not filepath.exists():
        raise FileNotFoundError(f"File not found: {filepath}")
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        for byte_block in iter(lambda: f.read(65536), b""):
            sha256_hash.update(byte_block)
    actual_hash = sha256_hash.hexdigest().lower()
    if actual_hash != expected_sha256.lower():
        raise ValueError(
            f"Hash mismatch for {filepath}!\nExpected: {expected_sha256}\nActual:   {actual_hash}"
        )
    return True


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
    acc = str(access_model).lower().strip()
    if acc in ["none_needed", "local_tool"]:
        return "none_needed"
    if acc.startswith("self_serve") or acc == "self_serve":
        return "self_serve"
    if acc in ["approval_required", "partner_or_contract", "customer_only", "gated", "enterprise_only"]:
        return "gated"
    if acc in ["no_public_api"]:
        return "no_public_api"
    return "unknown"


def has_vendor_verbatim_quote(evidence_list: List[Dict[str, Any]], field: str) -> Tuple[bool, Optional[str]]:
    """Check if evidence list contains an official vendor quote without ellipses for a field."""
    target_fields = [field]
    if field == "access_coarse":
        target_fields = ["access_model"]
    elif field == "official_mcp":
        target_fields = ["official_mcp", "official_mcp_url"]

    for e in evidence_list:
        if e.get("field") in target_fields:
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
    if agent_val is None or agent_val == "unknown" or agent_val == "" or agent_val == []:
        is_answered = False
    elif isinstance(agent_val, list) and (len(agent_val) == 0 or agent_val == ["unknown"]):
        is_answered = False

    if not is_answered:
        return False, 0.0, "unanswered"

    # 2. Field-specific scoring
    if field == "auth_methods":
        agent_set = {str(x).strip().lower() for x in agent_val} if isinstance(agent_val, (list, set, tuple)) else {str(agent_val).strip().lower()}
        gold_set = {str(x).strip().lower() for x in gold_val} if isinstance(gold_val, (list, set, tuple)) else {str(gold_val).strip().lower()}

        if agent_set == gold_set:
            return True, 1.0, "correct"

        partial_ok = {str(p).strip().lower() for p in gold_rec.get("auth_partial_ok", [])}
        if (agent_set & gold_set) or (agent_set & partial_ok):
            return True, 0.5, "partial"

        return True, 0.0, "wrong"

    elif field == "official_mcp":
        a_str = str(agent_val).strip().lower()
        g_str = str(gold_val).strip().lower()
        if a_str == g_str:
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
        a_str = str(agent_val).strip().lower()
        g_str = str(gold_val).strip().lower()
        if a_str == g_str:
            return True, 1.0, "correct"

        partial_ok = [str(p).strip().lower() for p in gold_rec.get("access_partial_ok", [])]
        if a_str in partial_ok:
            return True, 0.5, "partial"

        return True, 0.0, "wrong"

    elif field == "api_type":
        agent_set = {str(x).strip().lower() for x in agent_val} if isinstance(agent_val, (list, set, tuple)) else {str(agent_val).strip().lower()}
        gold_set = {str(x).strip().lower() for x in gold_val} if isinstance(gold_val, (list, set, tuple)) else {str(gold_val).strip().lower()}

        if agent_set == gold_set:
            return True, 1.0, "correct"
        if agent_set & gold_set:
            return True, 0.5, "partial"
        return True, 0.0, "wrong"

    else:
        if agent_val == gold_val:
            return True, 1.0, "correct"
        return True, 0.0, "wrong"


def evaluate_run(
    run_records: Dict[int, Dict[str, Any]],
    gold_records: Dict[int, Dict[str, Any]],
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

    for subset_name, subset_filter in subsets.items():
        subset_gold_keys = [k for k, g in gold_records.items() if subset_filter(g)]
        if not subset_gold_keys:
            continue

        subset_metrics = {}
        sub_total_scored = 0
        sub_total_answered = 0
        sub_total_points = 0.0
        sub_total_exact = 0
        sub_total_partial = 0
        sub_vendor_quoted = 0

        for fld in fields_to_score:
            scored_count = 0
            answered_count = 0
            correct_points = 0.0
            exact_correct_count = 0
            partial_count = 0
            field_vendor_quotes = 0

            for k in subset_gold_keys:
                g_rec = gold_records[k]
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

                # Check vendor-domain verbatim quote for answered fields
                has_quote = False
                ev_url = None
                if is_ans:
                    ev_fld = "access_model" if fld == "access_coarse" else fld
                    has_quote, ev_url = has_vendor_verbatim_quote(agent_rec.get("evidence", []), ev_fld)
                    if has_quote:
                        field_vendor_quotes += 1

                if is_ans:
                    answered_count += 1
                    correct_points += points
                    if points == 1.0:
                        exact_correct_count += 1
                    elif points == 0.5:
                        partial_count += 1

                # Record miss (if wrong, partial, or unanswered) in subset 'all'
                if subset_name == "all" and points < 1.0:
                    ev_fld = "access_model" if fld == "access_coarse" else fld
                    if not ev_url and agent_rec.get("evidence"):
                        ev_matches = [e.get("url") for e in agent_rec.get("evidence", []) if e.get("field") == ev_fld]
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
            exact_overall = (exact_correct_count / scored_count) if scored_count > 0 else 0.0
            partial_overall = (partial_count / scored_count) if scored_count > 0 else 0.0
            v_share = (field_vendor_quotes / answered_count) if answered_count > 0 else 0.0
            ci_low, ci_high = wilson_score_interval(correct_points, scored_count)
            exact_ci_low, exact_ci_high = wilson_score_interval(exact_correct_count, scored_count)

            subset_metrics[fld] = {
                "scored": scored_count,
                "answered": answered_count,
                "coverage": coverage,
                "accuracy_when_answered": acc_answered,
                "overall_correct": overall_correct,
                "exact_overall": exact_overall,
                "partial_overall": partial_overall,
                "exact_count": exact_correct_count,
                "partial_count": partial_count,
                "vendor_quotes": field_vendor_quotes,
                "vendor_quote_share": v_share,
                "wilson_ci": (ci_low, ci_high),
                "exact_wilson_ci": (exact_ci_low, exact_ci_high),
            }

            sub_total_scored += scored_count
            sub_total_answered += answered_count
            sub_total_points += correct_points
            sub_total_exact += exact_correct_count
            sub_total_partial += partial_count
            sub_vendor_quoted += field_vendor_quotes

        sub_cov = (sub_total_answered / sub_total_scored) if sub_total_scored > 0 else 0.0
        sub_acc_ans = (sub_total_points / sub_total_answered) if sub_total_answered > 0 else 0.0
        sub_overall = (sub_total_points / sub_total_scored) if sub_total_scored > 0 else 0.0
        sub_exact_rate = (sub_total_exact / sub_total_scored) if sub_total_scored > 0 else 0.0
        sub_partial_rate = (sub_total_partial / sub_total_scored) if sub_total_scored > 0 else 0.0
        sub_v_share = (sub_vendor_quoted / sub_total_answered) if sub_total_answered > 0 else 0.0
        sub_ci_l, sub_ci_h = wilson_score_interval(sub_total_points, sub_total_scored)
        sub_exact_ci_l, sub_exact_ci_h = wilson_score_interval(sub_total_exact, sub_total_scored)

        results[subset_name] = {
            "subset_size": len(subset_gold_keys),
            "summary": {
                "scored": sub_total_scored,
                "answered": sub_total_answered,
                "coverage": sub_cov,
                "accuracy_when_answered": sub_acc_ans,
                "overall_correct": sub_overall,
                "exact_overall": sub_exact_rate,
                "partial_overall": sub_partial_rate,
                "exact_count": sub_total_exact,
                "partial_count": sub_total_partial,
                "vendor_quotes": sub_vendor_quoted,
                "vendor_quote_share": sub_v_share,
                "wilson_ci": (sub_ci_l, sub_ci_h),
                "exact_wilson_ci": (sub_exact_ci_l, sub_exact_ci_h),
            },
            "metrics": subset_metrics,
        }

    # Calibration analysis: needs_human=False vs needs_human=True
    calib = {}
    for nh_val in [False, True]:
        nh_recs = [k for k in gold_records if bool(run_records.get(k, {}).get("needs_human", False)) == nh_val]
        nh_scored = 0
        nh_answered = 0
        nh_correct = 0.0
        nh_exact = 0
        nh_partial = 0
        for k in nh_recs:
            g_rec = gold_records[k]
            agent_rec = run_records.get(k, {})
            for fld in fields_to_score:
                g_val = g_rec.get("access_coarse", g_rec.get("access_model")) if fld == "access_coarse" else g_rec.get(fld)
                if g_val is None or g_val == "":
                    continue
                nh_scored += 1
                a_val = agent_rec.get("access_model") if fld == "access_coarse" else agent_rec.get(fld)
                is_ans, pts, _ = score_single_field(fld, a_val, g_val, g_rec)
                if is_ans:
                    nh_answered += 1
                    nh_correct += pts
                    if pts == 1.0:
                        nh_exact += 1
                    elif pts == 0.5:
                        nh_partial += 1

        ci_l, ci_h = wilson_score_interval(nh_correct, nh_scored)
        calib[str(nh_val)] = {
            "records_count": len(nh_recs),
            "scored_fields": nh_scored,
            "answered_fields": nh_answered,
            "exact_count": nh_exact,
            "partial_count": nh_partial,
            "coverage": (nh_answered / nh_scored) if nh_scored > 0 else 0.0,
            "accuracy_when_answered": (nh_correct / nh_answered) if nh_answered > 0 else 0.0,
            "overall_correct": (nh_correct / nh_scored) if nh_scored > 0 else 0.0,
            "wilson_ci": (ci_l, ci_h),
        }

    return {
        "run_name": run_name,
        "subsets": results,
        "vendor_quote_share": results.get("all", {}).get("summary", {}).get("vendor_quote_share", 0.0),
        "total_answered_fields": results.get("all", {}).get("summary", {}).get("answered", 0),
        "total_vendor_quoted_fields": results.get("all", {}).get("summary", {}).get("vendor_quotes", 0),
        "calibration": calib,
        "misses": misses,
    }


def print_evaluation_report(eval_data: Dict[str, Any]):
    """Format and print benchmark scoring report."""
    run_name = eval_data["run_name"]
    print(f"\n=========================================================================================================")
    print(f"BENCHMARK SCORING REPORT: {run_name.upper()}")
    print(f"=========================================================================================================")
    print(f"Share of Answered Fields with Vendor-Domain Verbatim Quote: "
          f"{eval_data['vendor_quote_share']*100:.1f}% "
          f"({eval_data['total_vendor_quoted_fields']} / {eval_data['total_answered_fields']})")

    for subset_name, sub_info in eval_data["subsets"].items():
        print(f"\n--- SUBSET: {subset_name.upper()} (N={sub_info['subset_size']} apps) ---")
        print(f"{'Field':<15} | {'Scored':<6} | {'Coverage':<8} | {'Acc(Ans)':<9} | {'Exact Only':<12} | {'Partial':<9} | {'Combined':<9} | {'Wilson 95% CI':<16} | {'V-Quote':<7}")
        print("-" * 105)
        for fld, m in sub_info["metrics"].items():
            ci_str = f"[{m['wilson_ci'][0]*100:.1f}%, {m['wilson_ci'][1]*100:.1f}%]"
            exact_str = f"{m['exact_count']}/{m['scored']} ({m['exact_overall']*100:.1f}%)"
            part_str = f"{m['partial_count']}/{m['scored']} ({m['partial_overall']*100:.1f}%)"
            comb_str = f"{m['overall_correct']*100:.1f}%"
            vq_str = f"{m['vendor_quote_share']*100:.1f}%"
            print(f"{fld:<15} | {m['scored']:<6} | {m['coverage']*100:>6.1f}%  | {m['accuracy_when_answered']*100:>7.1f}%  | {exact_str:<12} | {part_str:<9} | {comb_str:<9} | {ci_str:<16} | {vq_str:<7}")

        sm = sub_info["summary"]
        ci_str = f"[{sm['wilson_ci'][0]*100:.1f}%, {sm['wilson_ci'][1]*100:.1f}%]"
        exact_str = f"{sm['exact_count']}/{sm['scored']} ({sm['exact_overall']*100:.1f}%)"
        part_str = f"{sm['partial_count']}/{sm['scored']} ({sm['partial_overall']*100:.1f}%)"
        comb_str = f"{sm['overall_correct']*100:.1f}%"
        vq_str = f"{sm['vendor_quote_share']*100:.1f}%"
        print("-" * 105)
        print(f"{'OVERALL':<15} | {sm['scored']:<6} | {sm['coverage']*100:>6.1f}%  | {sm['accuracy_when_answered']*100:>7.1f}%  | {exact_str:<12} | {part_str:<9} | {comb_str:<9} | {ci_str:<16} | {vq_str:<7}")


def load_gold_records(gold_path: Path) -> Dict[int, Dict[str, Any]]:
    """Load gold records from JSON."""
    with open(gold_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    labels_dict = data.get("labels", data) if isinstance(data, dict) else {}
    gold = {}
    if isinstance(data, list):
        for idx, item in enumerate(data):
            k = int(item.get("id", idx))
            item["id"] = k
            gold[k] = item
    elif isinstance(labels_dict, dict):
        for k, v in labels_dict.items():
            if isinstance(v, dict):
                app_id = int(k) if str(k).isdigit() else v.get("id", k)
                v["id"] = app_id
                gold[app_id] = v
    return gold


def load_run_records(run_path: Path) -> Dict[int, Dict[str, Any]]:
    """Load agent run records from JSONL."""
    recs = {}
    with open(run_path, "r", encoding="utf-8") as f:
        for idx, line in enumerate(f):
            if line.strip():
                r = json.loads(line)
                k = int(r.get("id", idx))
                recs[k] = r
    return recs


def generate_mcp_confusion_matrix(run_records: Dict[int, Dict[str, Any]], gold_records: Dict[int, Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Compute 4x4 confusion table for official_mcp (Agent Rows vs Gold Columns)."""
    classes = ["official", "none_found", "community_only", "unknown"]
    matrix = {a: {g: 0 for g in classes} for a in classes}
    for k, g_rec in gold_records.items():
        g_val = g_rec.get("official_mcp")
        if not g_val:
            g_cls = "unknown"
        else:
            g_cls = str(g_val).lower().strip()
            if g_cls not in classes:
                g_cls = "unknown"

        agent_rec = run_records.get(k, {})
        a_val = agent_rec.get("official_mcp")
        if not a_val:
            a_cls = "unknown"
        else:
            a_cls = str(a_val).lower().strip()
            if a_cls not in classes:
                a_cls = "unknown"

        matrix[a_cls][g_cls] += 1
    return matrix


def evaluate_v1_model_split(run_records: Dict[int, Dict[str, Any]], gold_records: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
    """Evaluate v1 accuracy split by model (120b, 20b, fallback failures)."""
    fields_to_score = ["auth_methods", "official_mcp", "access_coarse", "access_model", "api_type"]
    model_groups = {"gpt-oss-120b": [], "gpt-oss-20b": [], "fallback_failures": []}

    for k, g_rec in gold_records.items():
        agent_rec = run_records.get(k, {})
        m = str(agent_rec.get("_run_meta", {}).get("model", "")).lower()
        if "120b" in m:
            model_groups["gpt-oss-120b"].append(k)
        elif "20b" in m:
            model_groups["gpt-oss-20b"].append(k)
        else:
            model_groups["fallback_failures"].append(k)

    results = {}
    for grp_name, keys in model_groups.items():
        scored = 0
        answered = 0
        correct = 0.0
        exact_cnt = 0
        partial_cnt = 0
        is_fallback = (grp_name == "fallback_failures")

        for k in keys:
            g_rec = gold_records[k]
            agent_rec = run_records.get(k, {})
            for fld in fields_to_score:
                g_val = g_rec.get("access_coarse", g_rec.get("access_model")) if fld == "access_coarse" else g_rec.get(fld)
                if g_val is None or g_val == "":
                    continue
                scored += 1
                if is_fallback:
                    continue  # fallback failures count as misses (unanswered = 0 points)
                a_val = agent_rec.get("access_model") if fld == "access_coarse" else agent_rec.get(fld)
                is_ans, pts, _ = score_single_field(fld, a_val, g_val, g_rec)
                if is_ans:
                    answered += 1
                    correct += pts
                    if pts == 1.0:
                        exact_cnt += 1
                    elif pts == 0.5:
                        partial_cnt += 1

        cov = (answered / scored) if scored > 0 else 0.0
        acc_ans = (correct / answered) if answered > 0 else 0.0
        overall = (correct / scored) if scored > 0 else 0.0
        ci_l, ci_h = wilson_score_interval(correct, scored)
        results[grp_name] = {
            "app_count": len(keys),
            "scored_fields": scored,
            "answered_fields": answered,
            "exact_count": exact_cnt,
            "partial_count": partial_cnt,
            "coverage": cov,
            "accuracy_when_answered": acc_ans,
            "overall_correct": overall,
            "wilson_ci": (ci_l, ci_h),
        }
    return results


def main():
    parser = argparse.ArgumentParser(description="Composio Research Scoring Pipeline")
    parser.add_argument("--gold", type=str, default="data/gold.json", help="Path to gold JSON file")
    args = parser.parse_args()

    gold_path = Path(args.gold)
    if not gold_path.exists():
        print(f"Gold file not found: {gold_path}")
        sys.exit(1)

    # 1. Verify SHA-256 hash of gold file
    print(f"Verifying SHA-256 of {gold_path}...")
    verify_file_hash(gold_path, EXPECTED_GOLD_SHA256)
    print(f"Verification successful: Hash matches {EXPECTED_GOLD_SHA256}")

    gold_records = load_gold_records(gold_path)
    print(f"Loaded {len(gold_records)} gold records from {gold_path}")

    runs_to_score = []
    for run_name in ["v1", "v2", "v3", "v4"]:
        p = Path(f"data/runs/{run_name}.jsonl")
        if p.exists():
            runs_to_score.append((run_name, p))

    all_eval_results = {}
    all_mcp_matrices = {}

    for run_name, run_path in runs_to_score:
        run_recs = load_run_records(run_path)
        eval_result = evaluate_run(run_recs, gold_records, run_name=run_name)
        all_eval_results[run_name] = eval_result
        all_mcp_matrices[run_name] = generate_mcp_confusion_matrix(run_recs, gold_records)

        # Save scores JSON
        score_out_path = Path(f"data/scores_{run_name}.json")
        with open(score_out_path, "w", encoding="utf-8") as f:
            json.dump(eval_result, f, indent=2)
        print(f"Saved evaluation metrics to {score_out_path}")

        print_evaluation_report(eval_result)

        # If v4, export misses CSV
        if run_name == "v4":
            csv_path = Path("data/misses_v4.csv")
            import csv
            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["app", "field", "agent_value", "gold_value", "outcome", "evidence_url"])
                for m in eval_result["misses"]:
                    writer.writerow([m["app"], m["field"], json.dumps(m["agent_value"]), json.dumps(m["gold_value"]), m["outcome"], m["evidence_url"]])
            print(f"Saved {len(eval_result['misses'])} v4 misses to {csv_path}")

    # Extra Analysis 1: Calibration across versions
    print("\n=========================================================================================================")
    print("EXTRA ANALYSIS 1: CALIBRATION (needs_human=False vs needs_human=True)")
    print("=========================================================================================================")
    print(f"{'Version':<8} | {'Flag':<18} | {'Apps':<6} | {'Coverage':<10} | {'Acc (Ans)':<12} | {'Overall Correct':<16} | {'Wilson 95% CI':<16}")
    print("-" * 95)
    for run_name in ["v1", "v2", "v3", "v4"]:
        if run_name not in all_eval_results:
            continue
        calib = all_eval_results[run_name]["calibration"]
        for flag_str, label in [("False", "needs_human=False"), ("True", "needs_human=True")]:
            c = calib.get(flag_str, {})
            ci_s = f"[{c.get('wilson_ci', (0,0))[0]*100:.1f}%, {c.get('wilson_ci', (0,0))[1]*100:.1f}%]"
            print(f"{run_name:<8} | {label:<18} | {c.get('records_count', 0):<6} | {c.get('coverage', 0)*100:>8.1f}% | {c.get('accuracy_when_answered', 0)*100:>10.1f}% | {c.get('overall_correct', 0)*100:>14.1f}% | {ci_s:<16}")

    # Extra Analysis 2: v1 model split
    if "v1" in all_eval_results:
        print("\n=========================================================================================================")
        print("EXTRA ANALYSIS 2: V1 ACCURACY SPLIT BY MODEL")
        print("=========================================================================================================")
        v1_recs = load_run_records(Path("data/runs/v1.jsonl"))
        v1_split = evaluate_v1_model_split(v1_recs, gold_records)
        print(f"{'Model Group':<20} | {'Apps':<6} | {'Scored':<7} | {'Coverage':<9} | {'Acc (Ans)':<10} | {'Overall Correct':<16} | {'Wilson 95% CI':<16}")
        print("-" * 100)
        for grp, res in v1_split.items():
            ci_s = f"[{res['wilson_ci'][0]*100:.1f}%, {res['wilson_ci'][1]*100:.1f}%]"
            print(f"{grp:<20} | {res['app_count']:<6} | {res['scored_fields']:<7} | {res['coverage']*100:>7.1f}%  | {res['accuracy_when_answered']*100:>8.1f}%  | {res['overall_correct']*100:>14.1f}% | {ci_s:<16}")

    # Extra Analysis 3: MCP Confusion Matrices
    print("\n=========================================================================================================")
    print("EXTRA ANALYSIS 3: OFFICIAL_MCP CONFUSION MATRICES (Agent Rows vs Gold Columns)")
    print("=========================================================================================================")
    classes = ["official", "none_found", "community_only", "unknown"]
    for run_name, matrix in all_mcp_matrices.items():
        print(f"\n--- {run_name.upper()} OFFICIAL_MCP CONFUSION TABLE (N={len(gold_records)} apps) ---")
        print(f"{'Agent \\ Gold':<16} | {'official':<10} | {'none_found':<10} | {'community':<10} | {'unknown':<10} | {'Total':<6}")
        print("-" * 72)
        for a_cls in classes:
            row = matrix[a_cls]
            row_tot = sum(row.values())
            print(f"{a_cls:<16} | {row['official']:<10} | {row['none_found']:<10} | {row['community_only']:<10} | {row['unknown']:<10} | {row_tot:<6}")
        col_totals = {c: sum(matrix[r][c] for r in classes) for c in classes}
        print("-" * 72)
        print(f"{'Total':<16} | {col_totals['official']:<10} | {col_totals['none_found']:<10} | {col_totals['community_only']:<10} | {col_totals['unknown']:<10} | {sum(col_totals.values()):<6}")

        false_none = matrix["none_found"]["official"]
        print(f"False 'none_found' calls in {run_name}: {false_none} (Agent claimed none_found, but gold is official)")


if __name__ == "__main__":
    main()
