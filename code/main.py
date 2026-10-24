"""
Ghost Data Still Exists - Main CLI Entry Point.
Audits all requests in dataset/deletion_requests.csv and emits root output.csv.
"""

import os
import sys
import argparse
import time
from pathlib import Path
import pandas as pd
from typing import List

# Ensure repository root is on sys.path
repo_root = str(Path(__file__).resolve().parent.parent)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

try:
    from code.models import DeletionRequest, DeletionStatus, Step
    from code.auditor import DeletionAuditor
    from code.emitter import OutputEmitter
except ImportError:
    from models import DeletionRequest, DeletionStatus, Step
    from auditor import DeletionAuditor
    from emitter import OutputEmitter


def parse_args():
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists Deletion Audit System")
    parser.add_argument("--dataset-dir", default="dataset", help="Path to dataset directory")
    parser.add_argument("--output-csv", default="output.csv", help="Path to write output.csv")
    parser.add_argument("--requests-file", default=None, help="Specific deletion requests CSV file")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases to process")
    return parser.parse_args()


def run_pipeline(dataset_dir: str, output_csv: str, requests_file: str = None, limit: int = None):
    start_time = time.time()
    
    # Locate deletion requests file
    if requests_file is None:
        requests_file = os.path.join(dataset_dir, "deletion_requests.csv")
    
    if not os.path.exists(requests_file):
        raise FileNotFoundError(f"Requests file not found: {requests_file}")
        
    print(f"Loading deletion requests from: {requests_file}")
    req_df = pd.read_csv(requests_file)
    total_cases = len(req_df)
    print(f"Total requests to process: {total_cases}")
    
    if limit:
        req_df = req_df.head(limit)
        print(f"Limited processing to first {len(req_df)} cases")

    # Initialize auditor
    print(f"Initializing DeletionAuditor with dataset: {dataset_dir} ...")
    auditor = DeletionAuditor(dataset_dir)
    emitter = OutputEmitter()
    
    results = []
    status_counts = {
        DeletionStatus.COMPLETE: 0,
        DeletionStatus.INCOMPLETE: 0,
        DeletionStatus.NOT_APPLICABLE: 0,
    }
    
    total_steps = 0
    print("Auditing cases...")
    for idx, row in req_df.iterrows():
        req = DeletionRequest(
            case_id=str(row["case_id"]),
            email=str(row["email"]),
            requested_at=str(row["requested_at"]),
            reported_status=str(row["reported_status"]),
            reported_at=str(row["reported_at"]),
            audit_at=str(row["audit_at"]),
            request_text=str(row["request_text"]),
        )
        status, steps = auditor.audit_case(req)
        results.append((req.case_id, status, steps))
        status_counts[status] += 1
        total_steps += len(steps)
        
        if (idx + 1) % 50 == 0 or (idx + 1) == len(req_df):
            print(f"  Processed {idx + 1}/{len(req_df)} cases...")

    # Write output CSV
    print(f"Writing output CSV to: {output_csv}")
    emitter.write_csv(output_csv, results)
    
    elapsed = time.time() - start_time
    print("\n================ AUDIT SUMMARY ================")
    print(f"Total Cases Processed: {len(results)}")
    print(f"COMPLETE:             {status_counts[DeletionStatus.COMPLETE]}")
    print(f"INCOMPLETE:           {status_counts[DeletionStatus.INCOMPLETE]}")
    print(f"NOT_APPLICABLE:       {status_counts[DeletionStatus.NOT_APPLICABLE]}")
    print(f"Total Steps Emitted:  {total_steps}")
    print(f"Elapsed Time:         {elapsed:.2f}s ({len(results)/elapsed:.1f} cases/sec)")
    print(f"Output File:          {output_csv} ({os.path.getsize(output_csv)} bytes)")
    print("===============================================\n")


if __name__ == "__main__":
    args = parse_args()
    run_pipeline(
        dataset_dir=args.dataset_dir,
        output_csv=args.output_csv,
        requests_file=args.requests_file,
        limit=args.limit
    )
