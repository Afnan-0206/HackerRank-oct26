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
    from code.model_layer import ModelLayer
except ImportError:
    from models import DeletionRequest, DeletionStatus, Step
    from auditor import DeletionAuditor
    from emitter import OutputEmitter
    from model_layer import ModelLayer


def parse_args():
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists Deletion Audit System")
    parser.add_argument("--dataset-dir", default="dataset", help="Path to dataset directory")
    parser.add_argument("--output-csv", default="output.csv", help="Path to write output.csv")
    parser.add_argument("--requests-file", default=None, help="Specific deletion requests CSV file")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases to process")
    parser.add_argument("--pricing-config", default="code/pricing_config.json", help="Path to pricing config")
    parser.add_argument("--usage-report", default=None, help="Optional path to write usage report (default None preserves curated reports)")
    return parser.parse_args()


def run_pipeline(
    dataset_dir: str,
    output_csv: str,
    requests_file: str = None,
    limit: int = None,
    pricing_config: str = "code/pricing_config.json",
    usage_report: str = None
):
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

    # Initialize auditor and model layer
    print(f"Initializing DeletionAuditor with dataset: {dataset_dir} ...")
    auditor = DeletionAuditor(dataset_dir)
    emitter = OutputEmitter()
    model_layer = ModelLayer(pricing_config_path=pricing_config)
    
    run_start_idx = len(model_layer.telemetry.records)
    results = []
    status_counts = {
        DeletionStatus.COMPLETE: 0,
        DeletionStatus.INCOMPLETE: 0,
        DeletionStatus.NOT_APPLICABLE: 0,
    }
    
    total_steps = 0
    print("Auditing cases with active Intent Gate...")
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
        status, steps = auditor.audit_case(req, model_layer=model_layer)
        results.append((req.case_id, status, steps))
        status_counts[status] += 1
        total_steps += len(steps)
        
        if (idx + 1) % 50 == 0 or (idx + 1) == len(req_df):
            print(f"  Processed {idx + 1}/{len(req_df)} cases...")

    # Write output CSV
    print(f"Writing output CSV to: {output_csv}")
    emitter.write_csv(output_csv, results)
    
    # Generate Telemetry & Usage Report if explicitly requested
    run_records = model_layer.telemetry.records[run_start_idx:]
    if usage_report:
        print(f"Generating usage and cost report from this run to: {usage_report}")
        model_layer.telemetry.generate_usage_report(usage_report, run_records=run_records)
    else:
        print("Curated reports preserved (pass --usage-report <path> to write run telemetry report)")
    
    elapsed = time.time() - start_time
    summary = model_layer.telemetry.get_summary(run_records)
    print("\n================ AUDIT SUMMARY ================")
    print(f"Total Cases Processed: {len(results)}")
    print(f"COMPLETE:             {status_counts[DeletionStatus.COMPLETE]}")
    print(f"INCOMPLETE:           {status_counts[DeletionStatus.INCOMPLETE]}")
    print(f"NOT_APPLICABLE:       {status_counts[DeletionStatus.NOT_APPLICABLE]}")
    print(f"Total Steps Emitted:  {total_steps}")
    print(f"Elapsed Time:         {elapsed:.2f}s ({len(results)/elapsed:.1f} cases/sec)")
    print(f"Output File:          {output_csv} ({os.path.getsize(output_csv)} bytes)")
    print("\n============== TELEMETRY & COST ===============")
    print(f"Total Model Calls:    {summary['total_calls']}")
    print(f"Cache Hits:           {summary['cache_hits']} ({summary['cache_hit_rate_pct']}%)")
    print(f"Network Calls:        {summary['network_calls']}")
    print(f"Total Tokens:         {summary['total_tokens']:,}")
    print(f"Avg Tokens/Request:   {summary['avg_total_tokens']}")
    print(f"Estimated Total Cost: ${summary['total_cost_usd']:.4f} USD")
    print(f"Avg Cost/Request:     ${summary['avg_cost_usd']:.6f} USD")
    print("===============================================\n")


if __name__ == "__main__":
    args = parse_args()
    run_pipeline(
        dataset_dir=args.dataset_dir,
        output_csv=args.output_csv,
        requests_file=args.requests_file,
        limit=args.limit,
        pricing_config=args.pricing_config,
        usage_report=args.usage_report
    )
