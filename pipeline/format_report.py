"""Format final scoring results into GitHub-flavored Markdown tables."""
import json
from pathlib import Path

runs = ['v1', 'v2', 'v3', 'v4']
subsets = ['all', 'random30', 'hard', 'zero_hit', 'blind_only']
fields = ['auth_methods', 'official_mcp', 'access_coarse', 'access_model', 'api_type']

data = {r: json.load(open(f'data/scores_{r}.json', encoding='utf-8')) for r in runs}

print("# Phase 5: Benchmark Evaluation Across Versions (v1, v2, v3, v4)\n")

for r in runs:
    print(f"## Version {r.upper()} Scoring Summary")
    v_info = data[r]
    tot_ans = v_info['total_answered_fields']
    tot_vq = v_info['total_vendor_quoted_fields']
    vq_share = v_info['vendor_quote_share'] * 100
    print(f"- **Answered Fields with Vendor-Domain Verbatim Quote**: **{vq_share:.1f}%** ({tot_vq} / {tot_ans})\n")

    for sub in subsets:
        s_data = v_info['subsets'][sub]
        n_apps = s_data['subset_size']
        print(f"### Version {r.upper()} — Subset: `{sub}` (N={n_apps} apps)")
        print("| Field | Scored | Coverage | Acc (Ans) | Exact Match Only | Partial Credit | Overall Correct | Wilson 95% CI | Vendor Quote % |")
        print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        for fld in fields:
            m = s_data['metrics'][fld]
            ci_s = f"[{m['wilson_ci'][0]*100:.1f}%, {m['wilson_ci'][1]*100:.1f}%]"
            exact_s = f"{m['exact_count']}/{m['scored']} ({m['exact_overall']*100:.1f}%)"
            part_s = f"{m['partial_count']}/{m['scored']} ({m['partial_overall']*100:.1f}%)"
            comb_s = f"{m['overall_correct']*100:.1f}%"
            cov_s = f"{m['coverage']*100:.1f}%"
            acc_s = f"{m['accuracy_when_answered']*100:.1f}%"
            vq_s = f"{m['vendor_quote_share']*100:.1f}%"
            print(f"| `{fld}` | {m['scored']} | {cov_s} | {acc_s} | {exact_s} | {part_s} | **{comb_s}** | {ci_s} | {vq_s} |")
        sm = s_data['summary']
        ci_s = f"[{sm['wilson_ci'][0]*100:.1f}%, {sm['wilson_ci'][1]*100:.1f}%]"
        exact_s = f"{sm['exact_count']}/{sm['scored']} ({sm['exact_overall']*100:.1f}%)"
        part_s = f"{sm['partial_count']}/{sm['scored']} ({sm['partial_overall']*100:.1f}%)"
        comb_s = f"{sm['overall_correct']*100:.1f}%"
        cov_s = f"{sm['coverage']*100:.1f}%"
        acc_s = f"{sm['accuracy_when_answered']*100:.1f}%"
        vq_s = f"{sm['vendor_quote_share']*100:.1f}%"
        print(f"| **OVERALL** | **{sm['scored']}** | **{cov_s}** | **{acc_s}** | **{exact_s}** | **{part_s}** | **{comb_s}** | **{ci_s}** | **{vq_s}** |\n")

print("## Extra Analysis 1: Calibration (`needs_human=false` vs `needs_human=true`)")
print("| Version | Group | Apps | Scored Fields | Coverage | Acc (Ans) | Overall Correct | Wilson 95% CI |")
print("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
for r in runs:
    calib = data[r]['calibration']
    for flag_str, label in [("False", "`needs_human=false`"), ("True", "`needs_human=true`")]:
        c = calib.get(flag_str, {})
        ci_s = f"[{c.get('wilson_ci', (0,0))[0]*100:.1f}%, {c.get('wilson_ci', (0,0))[1]*100:.1f}%]"
        cov_s = f"{c.get('coverage', 0)*100:.1f}%"
        acc_s = f"{c.get('accuracy_when_answered', 0)*100:.1f}%"
        comb_s = f"{c.get('overall_correct', 0)*100:.1f}%"
        print(f"| **{r.upper()}** | {label} | {c.get('records_count', 0)} | {c.get('scored_fields', 0)} | {cov_s} | {acc_s} | **{comb_s}** | {ci_s} |")
print()

print("## Extra Analysis 2: v1 Accuracy Split by Model")
print("| Model Group | Apps | Scored Fields | Coverage | Acc (Ans) | Overall Correct | Wilson 95% CI |")
print("| :--- | :---: | :---: | :---: | :---: | :---: | :---: |")
from pipeline.score import load_run_records, load_gold_records, evaluate_v1_model_split
gold = load_gold_records(Path('data/gold.json'))
v1_recs = load_run_records(Path('data/runs/v1.jsonl'))
v1_split = evaluate_v1_model_split(v1_recs, gold)
for grp, res in v1_split.items():
    ci_s = f"[{res['wilson_ci'][0]*100:.1f}%, {res['wilson_ci'][1]*100:.1f}%]"
    cov_s = f"{res['coverage']*100:.1f}%"
    acc_s = f"{res['accuracy_when_answered']*100:.1f}%" if res['answered_fields'] > 0 else "N/A"
    comb_s = f"{res['overall_correct']*100:.1f}%"
    print(f"| `{grp}` | {res['app_count']} | {res['scored_fields']} | {cov_s} | {acc_s} | **{comb_s}** | {ci_s} |")
print()

print("## Extra Analysis 3: `official_mcp` Confusion Tables (Agent Rows vs Gold Columns)")
from pipeline.score import generate_mcp_confusion_matrix
classes = ["official", "none_found", "community_only", "unknown"]
for r in runs:
    recs = load_run_records(Path(f'data/runs/{r}.jsonl'))
    matrix = generate_mcp_confusion_matrix(recs, gold)
    print(f"### Version {r.upper()} — `official_mcp` Confusion Matrix")
    print("| Agent \\ Gold | `official` | `none_found` | `community_only` | `unknown` | Total |")
    print("| :--- | :---: | :---: | :---: | :---: | :---: |")
    for a_cls in classes:
        row = matrix[a_cls]
        tot = sum(row.values())
        print(f"| `{a_cls}` | {row['official']} | {row['none_found']} | {row['community_only']} | {row['unknown']} | {tot} |")
    col_tots = {c: sum(matrix[row_c][c] for row_c in classes) for c in classes}
    print(f"| **Total** | **{col_tots['official']}** | **{col_tots['none_found']}** | **{col_tots['community_only']}** | **{col_tots['unknown']}** | **{sum(col_tots.values())}** |\n")
    false_none = matrix["none_found"]["official"]
    print(f"- **False `none_found` calls in {r.upper()}**: **{false_none}** (Agent predicted `none_found`, but Gold is verified `official`)\n")
