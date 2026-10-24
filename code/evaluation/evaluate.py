"""
Evaluation tool for Ghost Data Still Exists.
Computes case-level status accuracy, exact step match, sequence accuracy,
per-field accuracy (both overall and over aligned steps), step precision/recall/F1,
and per-phase & per-operation breakdowns against sample ground truth.
"""

import os
import sys
import json
import argparse
from pathlib import Path
import pandas as pd
from typing import Dict, List, Any, Optional, Tuple, Set

# Ensure code is on sys.path
code_dir = str(Path(__file__).resolve().parent.parent)
if code_dir not in sys.path:
    sys.path.insert(0, code_dir)

try:
    from code.models import DeletionRequest, DeletionStatus
    from code.auditor import DeletionAuditor
    from code.emitter import OutputEmitter
except ImportError:
    from models import DeletionRequest, DeletionStatus
    from auditor import DeletionAuditor
    from emitter import OutputEmitter

FIELDS = ["step_no", "phase", "system_id", "resource_id", "operation", "state", "evidence_id"]

ALL_SAMPLE_CASES = [f"case_{i:02d}" for i in range(1, 33)]
HOLDOUT_CASES = set([f"case_{i:02d}" for i in range(4, 33, 4)])
DEV_CASES = set([c for c in ALL_SAMPLE_CASES if c not in HOLDOUT_CASES])


def align_and_score_case_steps(
    pred_step_dicts: List[Dict[str, Any]],
    gt_steps: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Aligns steps by (phase, operation, system_id, resource_id).
    Computes matches and totals for each field including step_no.
    Returns full alignment metrics, aligned field matches, missing, extra,
    and per-phase/operation statistics.
    """
    field_matches = {fld: 0 for fld in FIELDS}
    field_totals = {fld: 0 for fld in FIELDS}
    aligned_field_matches = {fld: 0 for fld in FIELDS}

    pred_by_key: Dict[Tuple, List[Dict[str, Any]]] = {}
    for s in pred_step_dicts:
        k = (s.get("phase"), s.get("operation"), s.get("system_id"), s.get("resource_id"))
        pred_by_key.setdefault(k, []).append(s)

    gt_by_key: Dict[Tuple, List[Dict[str, Any]]] = {}
    for s in gt_steps:
        k = (s.get("phase"), s.get("operation"), s.get("system_id"), s.get("resource_id"))
        gt_by_key.setdefault(k, []).append(s)

    all_keys = list(dict.fromkeys(list(gt_by_key.keys()) + list(pred_by_key.keys())))

    aligned_count = 0
    missing_count = 0
    extra_count = 0

    aligned_pairs = []
    missing_steps = []
    extra_steps = []

    for k in all_keys:
        p_list = pred_by_key.get(k, [])
        g_list = gt_by_key.get(k, [])

        num_common = min(len(p_list), len(g_list))
        for i in range(num_common):
            ps = p_list[i]
            gs = g_list[i]
            aligned_count += 1
            aligned_pairs.append((ps, gs))

            for fld in FIELDS:
                field_totals[fld] += 1
                p_val = ps.get(fld)
                g_val = gs.get(fld)
                is_match = False
                if fld == "step_no":
                    try:
                        if int(p_val) == int(g_val):
                            is_match = True
                    except (TypeError, ValueError):
                        if p_val == g_val:
                            is_match = True
                elif fld == "evidence_id":
                    p_str = "" if p_val is None else str(p_val).strip()
                    g_str = "" if g_val is None else str(g_val).strip()
                    if p_str == g_str:
                        is_match = True
                else:
                    if str(p_val) == str(g_val):
                        is_match = True

                if is_match:
                    field_matches[fld] += 1
                    aligned_field_matches[fld] += 1

        # Missing steps in ground truth (not predicted)
        for i in range(num_common, len(g_list)):
            missing_count += 1
            missing_steps.append(g_list[i])
            for fld in FIELDS:
                field_totals[fld] += 1

        # Extra steps in predicted (not in ground truth)
        for i in range(num_common, len(p_list)):
            extra_count += 1
            extra_steps.append(p_list[i])
            for fld in FIELDS:
                field_totals[fld] += 1

    exact_step_match = (pred_step_dicts == gt_steps)

    pred_ops = [(s.get("phase"), s.get("operation"), s.get("system_id"), s.get("resource_id")) for s in pred_step_dicts]
    gt_ops = [(s.get("phase"), s.get("operation"), s.get("system_id"), s.get("resource_id")) for s in gt_steps]
    exact_seq_match = (pred_ops == gt_ops)

    return {
        "field_matches": field_matches,
        "field_totals": field_totals,
        "aligned_field_matches": aligned_field_matches,
        "aligned_count": aligned_count,
        "missing_count": missing_count,
        "extra_count": extra_count,
        "pred_count": len(pred_step_dicts),
        "gt_count": len(gt_steps),
        "exact_step_match": exact_step_match,
        "exact_seq_match": exact_seq_match,
        "aligned_pairs": aligned_pairs,
        "missing_steps": missing_steps,
        "extra_steps": extra_steps,
    }


def score_case_predictions(
    predictions: Dict[str, Dict[str, Any]],
    gt_map: Dict[str, Dict[str, Any]],
    case_ids: List[str],
    name: str = "SET"
) -> Dict[str, Any]:
    total_cases = len(case_ids)
    status_correct = 0
    exact_step_matches = 0
    seq_matches = 0

    field_matches = {f: 0 for f in FIELDS}
    field_totals = {f: 0 for f in FIELDS}
    aligned_field_matches = {f: 0 for f in FIELDS}

    total_pred_steps = 0
    total_gt_steps = 0
    total_aligned_steps = 0

    phase_stats: Dict[str, Dict[str, int]] = {}
    op_stats: Dict[str, Dict[str, int]] = {}

    for cid in case_ids:
        if cid not in gt_map:
            continue
        gt = gt_map[cid]
        if cid not in predictions:
            pred_status = None
            pred_steps = []
        else:
            pred = predictions[cid]
            pred_status = pred["status"]
            pred_steps = pred["steps"]

        if pred_status == gt["status"]:
            status_correct += 1

        res = align_and_score_case_steps(pred_steps, gt["steps"])

        if res["exact_step_match"]:
            exact_step_matches += 1
        if res["exact_seq_match"]:
            seq_matches += 1

        total_pred_steps += res["pred_count"]
        total_gt_steps += res["gt_count"]
        total_aligned_steps += res["aligned_count"]

        for f in FIELDS:
            field_matches[f] += res["field_matches"][f]
            field_totals[f] += res["field_totals"][f]
            aligned_field_matches[f] += res["aligned_field_matches"][f]

        # Breakdowns
        for s in pred_steps:
            ph = str(s.get("phase", "UNKNOWN"))
            op = str(s.get("operation", "UNKNOWN"))
            phase_stats.setdefault(ph, {"pred": 0, "gt": 0, "aligned": 0})["pred"] += 1
            op_stats.setdefault(op, {"pred": 0, "gt": 0, "aligned": 0})["pred"] += 1

        for s in gt["steps"]:
            ph = str(s.get("phase", "UNKNOWN"))
            op = str(s.get("operation", "UNKNOWN"))
            phase_stats.setdefault(ph, {"pred": 0, "gt": 0, "aligned": 0})["gt"] += 1
            op_stats.setdefault(op, {"pred": 0, "gt": 0, "aligned": 0})["gt"] += 1

        for ps, gs in res["aligned_pairs"]:
            ph = str(gs.get("phase", "UNKNOWN"))
            op = str(gs.get("operation", "UNKNOWN"))
            phase_stats.setdefault(ph, {"pred": 0, "gt": 0, "aligned": 0})["aligned"] += 1
            op_stats.setdefault(op, {"pred": 0, "gt": 0, "aligned": 0})["aligned"] += 1

    status_acc = (status_correct / total_cases * 100.0) if total_cases > 0 else 0.0
    exact_step_acc = (exact_step_matches / total_cases * 100.0) if total_cases > 0 else 0.0
    seq_acc = (seq_matches / total_cases * 100.0) if total_cases > 0 else 0.0

    step_precision = (total_aligned_steps / total_pred_steps * 100.0) if total_pred_steps > 0 else 100.0
    step_recall = (total_aligned_steps / total_gt_steps * 100.0) if total_gt_steps > 0 else 100.0
    if step_precision + step_recall > 0:
        step_f1 = (2 * step_precision * step_recall) / (step_precision + step_recall)
    else:
        step_f1 = 0.0

    overall_field_accs = {
        f: (field_matches[f] / field_totals[f] * 100.0) if field_totals[f] > 0 else 100.0
        for f in FIELDS
    }

    aligned_field_accs = {
        f: (aligned_field_matches[f] / total_aligned_steps * 100.0) if total_aligned_steps > 0 else 100.0
        for f in FIELDS
    }

    print(f"\n==================== {name} EVALUATION ({total_cases} cases) ====================")
    print(f"Status Accuracy:         {status_correct}/{total_cases} ({status_acc:.1f}%)")
    print(f"Exact Step Match Acc:    {exact_step_matches}/{total_cases} ({exact_step_acc:.1f}%)")
    print(f"Exact Sequence Acc:      {seq_matches}/{total_cases} ({seq_acc:.1f}%)")
    print("----------------------------------------------------------------------")
    print(f"Step Alignment Counts:   Aligned={total_aligned_steps}, Pred={total_pred_steps}, GT={total_gt_steps}")
    print(f"Step Precision:          {step_precision:.1f}% ({total_aligned_steps}/{total_pred_steps})")
    print(f"Step Recall:             {step_recall:.1f}% ({total_aligned_steps}/{total_gt_steps})")
    print(f"Step F1 Score:           {step_f1:.1f}%")
    print("----------------------------------------------------------------------")
    print("Field Accuracy (Aligned Steps Only, N = " + str(total_aligned_steps) + "):")
    for f in FIELDS:
        acc = aligned_field_accs[f]
        print(f"  - {f:12s}: {acc:5.1f}% ({aligned_field_matches[f]}/{total_aligned_steps})")
    print("----------------------------------------------------------------------")
    print("Field Accuracy (Penalized / All Steps Denominator, N = " + str(field_totals.get("phase", 0)) + "):")
    for f in FIELDS:
        acc = overall_field_accs[f]
        print(f"  - {f:12s}: {acc:5.1f}% ({field_matches[f]}/{field_totals[f]})")
    print("----------------------------------------------------------------------")
    print("Per-Phase Breakdown:")
    print(f"  {'Phase':<14s} | {'Pred':>5s} | {'GT':>5s} | {'Aligned':>7s} | {'Precision':>9s} | {'Recall':>8s} | {'F1':>6s}")
    for ph in ["FINDING", "REMEDIATION", "VERIFICATION"]:
        st = phase_stats.get(ph, {"pred": 0, "gt": 0, "aligned": 0})
        p = (st["aligned"] / st["pred"] * 100.0) if st["pred"] > 0 else (100.0 if st["gt"] == 0 else 0.0)
        r = (st["aligned"] / st["gt"] * 100.0) if st["gt"] > 0 else (100.0 if st["pred"] == 0 else 0.0)
        f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
        print(f"  {ph:<14s} | {st['pred']:5d} | {st['gt']:5d} | {st['aligned']:7d} | {p:8.1f}% | {r:7.1f}% | {f1:5.1f}%")
    print("----------------------------------------------------------------------")
    print("Per-Operation Breakdown:")
    print(f"  {'Operation':<28s} | {'Pred':>5s} | {'GT':>5s} | {'Aligned':>7s} | {'Precision':>9s} | {'Recall':>8s} | {'F1':>6s}")
    for op in sorted(op_stats.keys()):
        st = op_stats[op]
        p = (st["aligned"] / st["pred"] * 100.0) if st["pred"] > 0 else (100.0 if st["gt"] == 0 else 0.0)
        r = (st["aligned"] / st["gt"] * 100.0) if st["gt"] > 0 else (100.0 if st["pred"] == 0 else 0.0)
        f1 = (2 * p * r / (p + r)) if (p + r) > 0 else 0.0
        print(f"  {op:<28s} | {st['pred']:5d} | {st['gt']:5d} | {st['aligned']:7d} | {p:8.1f}% | {r:7.1f}% | {f1:5.1f}%")
    print("======================================================================\n")

    return {
        "split": name,
        "total_cases": total_cases,
        "status_accuracy": status_acc,
        "status_correct": status_correct,
        "exact_step_accuracy": exact_step_acc,
        "exact_step_matches": exact_step_matches,
        "sequence_accuracy": seq_acc,
        "sequence_matches": seq_matches,
        "step_precision": step_precision,
        "step_recall": step_recall,
        "step_f1": step_f1,
        "aligned_steps": total_aligned_steps,
        "pred_steps": total_pred_steps,
        "gt_steps": total_gt_steps,
        "aligned_field_accuracies": aligned_field_accs,
        "aligned_field_matches": aligned_field_matches,
        "overall_field_accuracies": overall_field_accs,
        "field_matches": field_matches,
        "field_totals": field_totals,
        "phase_breakdown": phase_stats,
        "op_breakdown": op_stats,
    }


def evaluate_output_csv(
    output_csv_path: str,
    gt_csv_path: str = "dataset/sample_output.csv",
    split: str = "both"
) -> Dict[str, Any]:
    out_df = pd.read_csv(output_csv_path)
    gt_df = pd.read_csv(gt_csv_path)

    predictions = {}
    for _, row in out_df.iterrows():
        cid = str(row["case_id"])
        predictions[cid] = {
            "status": str(row["deletion_status"]),
            "steps": json.loads(str(row["steps_json"])) if pd.notna(row["steps_json"]) else []
        }

    gt_map = {}
    for _, row in gt_df.iterrows():
        cid = str(row["case_id"])
        gt_map[cid] = {
            "status": str(row["deletion_status"]),
            "steps": json.loads(str(row["steps_json"])) if pd.notna(row["steps_json"]) else []
        }

    results = {}
    if split in ("dev", "both"):
        results["dev"] = score_case_predictions(predictions, gt_map, sorted(list(DEV_CASES)), "DEV")
    if split in ("holdout", "both"):
        results["holdout"] = score_case_predictions(predictions, gt_map, sorted(list(HOLDOUT_CASES)), "HOLDOUT")
    if split == "all":
        results["all"] = score_case_predictions(predictions, gt_map, sorted(ALL_SAMPLE_CASES), "ALL")

    return results


def evaluate_sample_cases(
    dataset_dir: str = "dataset",
    split: str = "both"
) -> Dict[str, Any]:
    sample_req_path = os.path.join(dataset_dir, "sample_deletion_requests.csv")
    sample_out_path = os.path.join(dataset_dir, "sample_output.csv")

    req_df = pd.read_csv(sample_req_path)
    gt_df = pd.read_csv(sample_out_path)

    gt_map = {}
    for _, row in gt_df.iterrows():
        cid = str(row["case_id"])
        gt_map[cid] = {
            "status": str(row["deletion_status"]),
            "steps": json.loads(str(row["steps_json"])) if pd.notna(row["steps_json"]) else []
        }

    auditor = DeletionAuditor(dataset_dir)
    predictions = {}
    for _, r in req_df.iterrows():
        cid = str(r["case_id"])
        req = DeletionRequest(
            case_id=cid,
            email=str(r["email"]),
            requested_at=str(r["requested_at"]),
            reported_status=str(r["reported_status"]),
            reported_at=str(r["reported_at"]),
            audit_at=str(r["audit_at"]),
            request_text=str(r["request_text"]),
        )
        pred_status, pred_steps = auditor.audit_case(req)
        predictions[cid] = {
            "status": pred_status.value,
            "steps": [s.to_dict() for s in pred_steps]
        }

    results = {}
    if split in ("dev", "both"):
        results["dev"] = score_case_predictions(predictions, gt_map, sorted(list(DEV_CASES)), "DEV")
    if split in ("holdout", "both"):
        results["holdout"] = score_case_predictions(predictions, gt_map, sorted(list(HOLDOUT_CASES)), "HOLDOUT")
    if split == "all":
        results["all"] = score_case_predictions(predictions, gt_map, sorted(ALL_SAMPLE_CASES), "ALL")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluate Ghost Data Still Exists predictions.")
    parser.add_argument("--output", "-o", type=str, default=None, help="Path to output.csv to score")
    parser.add_argument("--ground-truth", "-gt", type=str, default="dataset/sample_output.csv", help="Path to sample ground truth CSV")
    parser.add_argument("--dataset", "-d", type=str, default="dataset", help="Dataset directory for live evaluation")
    parser.add_argument("--split", "-s", type=str, default="both", choices=["dev", "holdout", "all", "both"], help="Split to evaluate")

    args = parser.parse_args()
    if args.output:
        evaluate_output_csv(args.output, args.ground_truth, split=args.split)
    else:
        evaluate_sample_cases(dataset_dir=args.dataset, split=args.split)
