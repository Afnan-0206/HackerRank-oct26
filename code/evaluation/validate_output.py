"""
Validator for Ghost Data Still Exists submission output.
Strictly validates schema, ordering, counts, and constraints.
"""

import os
import sys
import json
import pandas as pd
from typing import List, Dict, Set, Any

from pathlib import Path
repo_root = str(Path(__file__).resolve().parent.parent.parent)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

try:
    from code.models import (
        OPERATION_STATE_MAP,
        PHASE_ORDER,
        OPERATION_ORDER,
        DeletionStatus,
        StepPhase,
        Operation,
        ResourceState,
    )
except ImportError:
    from models import (
        PERMITTED_PAIRS,
        PHASE_ORDER,
        OPERATION_ORDER,
        DeletionStatus,
        StepPhase,
        Operation,
        ResourceState,
    )


def validate_output(output_csv: str, requests_csv: str) -> Dict[str, Any]:
    print(f"Validating output: {output_csv} against {requests_csv}")
    
    if not os.path.exists(output_csv):
        raise FileNotFoundError(f"Output file missing: {output_csv}")
    if not os.path.exists(requests_csv):
        raise FileNotFoundError(f"Requests file missing: {requests_csv}")
        
    req_df = pd.read_csv(requests_csv)
    expected_case_ids = set(req_df["case_id"].dropna().astype(str))
    expected_count = len(expected_case_ids)
    
    out_df = pd.read_csv(output_csv)
    
    # 1. Header check
    expected_headers = ["case_id", "deletion_status", "steps_json"]
    if list(out_df.columns) != expected_headers:
        raise ValueError(f"Header mismatch. Expected {expected_headers}, got {list(out_df.columns)}")
        
    actual_count = len(out_df)
    if actual_count != expected_count:
        raise ValueError(f"Row count mismatch. Expected {expected_count}, got {actual_count}")
        
    actual_case_ids = list(out_df["case_id"].astype(str))
    if len(actual_case_ids) != len(set(actual_case_ids)):
        raise ValueError("Duplicate case_ids detected in output")
        
    missing_ids = expected_case_ids - set(actual_case_ids)
    if missing_ids:
        raise ValueError(f"Missing case_ids: {missing_ids}")
        
    extra_ids = set(actual_case_ids) - expected_case_ids
    if extra_ids:
        raise ValueError(f"Extra case_ids: {extra_ids}")

    # Sorting check (case_XX numerical sort)
    def case_sort_key(cid):
        try:
            return int(cid.split("_")[1])
        except Exception:
            return cid
            
    expected_sorted_ids = sorted(list(expected_case_ids), key=case_sort_key)
    if actual_case_ids != expected_sorted_ids:
        raise ValueError("Output is not correctly sorted by case_id")

    # Step validation
    status_counts = {"COMPLETE": 0, "INCOMPLETE": 0, "NOT_APPLICABLE": 0}
    total_steps = 0
    phase_counts = {"FINDING": 0, "REMEDIATION": 0, "VERIFICATION": 0}
    
    violations: List[str] = []

    for idx, row in out_df.iterrows():
        cid = str(row["case_id"])
        status = str(row["deletion_status"])
        steps_raw = str(row["steps_json"])
        
        if status not in status_counts:
            violations.append(f"Invalid status '{status}' in case {cid}")
            continue
        status_counts[status] += 1
        
        try:
            steps = json.loads(steps_raw)
        except Exception as e:
            violations.append(f"Failed to parse steps_json for {cid}: {e}")
            continue
            
        if not isinstance(steps, list):
            violations.append(f"steps_json is not an array for {cid}")
            continue
            
        # NOT_APPLICABLE rule
        if status == "NOT_APPLICABLE":
            if steps != []:
                violations.append(f"NOT_APPLICABLE case {cid} must have steps_json=[], got {steps}")
            continue

        # COMPLETE invariant: needs at least one VERIFICATION step with non-empty evidence_id; all steps must be VERIFICATION with valid evidence_id
        if status == "COMPLETE":
            has_valid_verification = any(s.get("phase") == "VERIFICATION" and s.get("evidence_id") for s in steps)
            if not has_valid_verification:
                violations.append(f"COMPLETE case {cid} needs at least one VERIFICATION step with a non-empty evidence_id, got {len(steps)} steps")
            for s in steps:
                if s.get("phase") != "VERIFICATION":
                    violations.append(f"COMPLETE case {cid} contains non-VERIFICATION step: {s}")
                ev = s.get("evidence_id")
                if not ev:
                    violations.append(f"COMPLETE case {cid} step missing evidence_id: {s}")

        # INCOMPLETE invariant: needs at least one FINDING step; findings cite valid evidence_id; proposed remediation/future verification have empty evidence_id
        if status == "INCOMPLETE":
            has_finding = any(s.get("phase") == "FINDING" for s in steps)
            if not has_finding:
                violations.append(f"INCOMPLETE case {cid} needs at least one FINDING step, got {len(steps)} steps")
            for s in steps:
                p = s.get("phase")
                ev = s.get("evidence_id")
                if p == "FINDING":
                    if not ev:
                        violations.append(f"INCOMPLETE case {cid} FINDING step has empty evidence_id: {s}")
                elif p in ("REMEDIATION", "VERIFICATION"):
                    if ev != "":
                        violations.append(f"INCOMPLETE case {cid} {p} step must have empty evidence_id='', got '{ev}'")

        # Step numbering & sorting check
        prev_sort_tuple = (-1, -1, "", "")
        for s_idx, s in enumerate(steps, 1):
            total_steps += 1
            # Step numbering consecutive from 1
            if s.get("step_no") != s_idx:
                violations.append(f"Case {cid} step_no out of sequence: expected {s_idx}, got {s.get('step_no')}")
                
            p = s.get("phase")
            op = s.get("operation")
            st = s.get("state")
            sys_id = s.get("system_id")
            res_id = s.get("resource_id")
            
            try:
                op_enum = Operation(op)
                st_enum = ResourceState(st)
                if OPERATION_STATE_MAP.get(op_enum) != st_enum:
                    violations.append(f"Case {cid} state mismatch for {op}: expected {OPERATION_STATE_MAP.get(op_enum)}, got {st}")
            except Exception as ex:
                violations.append(f"Case {cid} invalid operation/state pair ({op}, {st}): {ex}")
                
            phase_counts[p] = phase_counts.get(p, 0) + 1
            
            p_val = PHASE_ORDER.get(StepPhase(p), 99)
            o_val = OPERATION_ORDER.get(Operation(op), 99)
            curr_sort_tuple = (p_val, o_val, str(sys_id), str(res_id))
            
            if curr_sort_tuple < prev_sort_tuple:
                violations.append(f"Case {cid} sorting violation: {curr_sort_tuple} < {prev_sort_tuple}")
            prev_sort_tuple = curr_sort_tuple

    if violations:
        print(f"\nVALIDATION FAILED WITH {len(violations)} VIOLATIONS:")
        for v in violations:
            print(f"  - {v}")
        raise ValueError(f"Validation failed with {len(violations)} violations. First: {violations[0]}")

    report = {
        "status": "PASS",
        "total_cases": actual_count,
        "status_distribution": status_counts,
        "total_steps": total_steps,
        "phase_distribution": phase_counts,
    }
    print("ALL VALIDATION CHECKS PASSED SUCCESSFULLY!")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    output_file = sys.argv[1] if len(sys.argv) > 1 else "output.csv"
    requests_file = sys.argv[2] if len(sys.argv) > 2 else "dataset/deletion_requests.csv"
    validate_output(output_file, requests_file)
