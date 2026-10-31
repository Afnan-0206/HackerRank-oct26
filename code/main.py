"""
Ghost Data Still Exists - Main CLI Entry Point.
Audits all requests in dataset/deletion_requests.csv and emits root output.csv.
Supports multi-core vectorized batch processing via concurrent.futures.ProcessPoolExecutor.
"""

import os
import sys
import argparse
import time
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional
from concurrent.futures import ProcessPoolExecutor
import pandas as pd

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

# Global worker handles for ProcessPoolExecutor
_worker_auditor: Optional[DeletionAuditor] = None
_worker_model_layer: Optional[ModelLayer] = None


def _init_worker(dataset_dir: str, pricing_config: str, auditor_instance: Optional[DeletionAuditor] = None):
    """Initializes per-worker in-memory auditor and model layer instances."""
    global _worker_auditor, _worker_model_layer
    _worker_auditor = auditor_instance if auditor_instance is not None else DeletionAuditor(dataset_dir)
    _worker_model_layer = ModelLayer(pricing_config_path=pricing_config)


def _audit_case_batch(batch: List[Dict[str, Any]]) -> List[Tuple[str, DeletionStatus, List[Step]]]:
    """Processes a vectorized batch of requests within a worker process."""
    global _worker_auditor, _worker_model_layer
    out = []
    for row in batch:
        req = DeletionRequest(
            case_id=str(row["case_id"]),
            email=str(row["email"]),
            requested_at=str(row["requested_at"]),
            reported_status=str(row["reported_status"]),
            reported_at=str(row["reported_at"]),
            audit_at=str(row["audit_at"]),
            request_text=str(row["request_text"]),
        )
        status, steps = _worker_auditor.audit_case(req, model_layer=_worker_model_layer)
        out.append((req.case_id, status, steps))
    return out


def parse_args():
    parser = argparse.ArgumentParser(description="Ghost Data Still Exists Deletion Audit System")
    parser.add_argument("--dataset-dir", default="dataset", help="Path to dataset directory")
    parser.add_argument("--output-csv", default="output.csv", help="Path to write output.csv")
    parser.add_argument("--requests-file", default=None, help="Specific deletion requests CSV file")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of cases to process")
    parser.add_argument("--pricing-config", default="code/pricing_config.json", help="Path to pricing config")
    parser.add_argument("--usage-report", default=None, help="Optional path to write usage report (default None preserves curated reports)")
    parser.add_argument("--workers", "-w", type=int, default=None, help="Number of worker processes for batching (default: auto, or 1 for sequential)")
    parser.add_argument("--batch-size", "-b", type=int, default=32, help="Chunk batch size per worker task (default: 32)")
    return parser.parse_args()


def run_pipeline(
    dataset_dir: str,
    output_csv: str,
    requests_file: str = None,
    limit: int = None,
    pricing_config: str = "code/pricing_config.json",
    usage_report: str = None,
    workers: Optional[int] = None,
    batch_size: int = 32
):
    start_time = time.time()
    
    # Locate deletion requests file
    if requests_file is None:
        requests_file = os.path.join(dataset_dir, "deletion_requests.csv")
    
    if not os.path.exists(requests_file):
        raise FileNotFoundError(f"Requests file not found: {requests_file}")
        
    print(f"Loading deletion requests from: {requests_file}")
    req_df = pd.read_csv(requests_file)
    if limit:
        req_df = req_df.head(limit)
        print(f"Limited processing to first {len(req_df)} cases")

    req_dicts = req_df.to_dict(orient="records")
    total_cases = len(req_dicts)
    print(f"Total requests to process: {total_cases}")

    # Determine execution strategy (sequential vs multi-core ProcessPoolExecutor)
    if workers is None:
        cpu_cnt = os.cpu_count() or 4
        workers = min(cpu_cnt, 8) if total_cases > 64 else 1

    emitter = OutputEmitter()
    results = []
    batch_elapsed = 0.0

    if workers > 1:
        batches = [req_dicts[i:i + batch_size] for i in range(0, total_cases, batch_size)]
        print(f"Auditing cases across {workers} worker processes ({len(batches)} batches, size={batch_size})...")
        t_batch_start = time.time()
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(dataset_dir, pricing_config)) as executor:
            batch_results = list(executor.map(_audit_case_batch, batches))
        batch_elapsed = time.time() - t_batch_start
        results = [item for sublist in batch_results for item in sublist]
    else:
        print(f"Auditing cases sequentially in single process...")
        auditor = DeletionAuditor(dataset_dir)
        model_layer = ModelLayer(pricing_config_path=pricing_config)
        t_batch_start = time.time()
        for idx, row in enumerate(req_dicts):
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
            if (idx + 1) % 50 == 0 or (idx + 1) == total_cases:
                print(f"  Processed {idx + 1}/{total_cases} cases...")
        batch_elapsed = time.time() - t_batch_start

    status_counts = {
        DeletionStatus.COMPLETE: 0,
        DeletionStatus.INCOMPLETE: 0,
        DeletionStatus.NOT_APPLICABLE: 0,
    }
    total_steps = 0
    for cid, status, steps in results:
        status_counts[status] += 1
        total_steps += len(steps)

    # Write output CSV
    print(f"Writing output CSV to: {output_csv}")
    emitter.write_csv(output_csv, results)
    
    # Telemetry tracker in main process for usage report and cost summary
    main_model_layer = ModelLayer(pricing_config_path=pricing_config)
    run_records = main_model_layer.telemetry.records[-total_cases:] if main_model_layer.telemetry.records else []
    if usage_report:
        print(f"Generating usage and cost report from this run to: {usage_report}")
        main_model_layer.telemetry.generate_usage_report(usage_report, run_records=run_records)
    else:
        print("Curated reports preserved (pass --usage-report <path> to write run telemetry report)")

    elapsed = time.time() - start_time
    summary = main_model_layer.telemetry.get_summary(run_records)
    audit_throughput = total_cases / batch_elapsed if batch_elapsed > 0 else 0.0
    overall_throughput = total_cases / elapsed if elapsed > 0 else 0.0

    print("\n================ AUDIT SUMMARY ================")
    print(f"Total Cases Processed: {len(results)}")
    print(f"COMPLETE:             {status_counts[DeletionStatus.COMPLETE]}")
    print(f"INCOMPLETE:           {status_counts[DeletionStatus.INCOMPLETE]}")
    print(f"NOT_APPLICABLE:       {status_counts[DeletionStatus.NOT_APPLICABLE]}")
    print(f"Total Steps Emitted:  {total_steps}")
    print(f"Audit Batch Time:     {batch_elapsed:.2f}s ({audit_throughput:.1f} cases/sec)")
    print(f"Total Pipeline Time:  {elapsed:.2f}s ({overall_throughput:.1f} cases/sec)")
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
        usage_report=args.usage_report,
        workers=args.workers,
        batch_size=args.batch_size
    )
