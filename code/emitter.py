"""
CSV and JSON Emitter for Ghost Data Still Exists.
Produces output conforming strictly to HackerRank Orchestrate October 2026 schema.
"""

import csv
import json
from io import StringIO
from typing import List, Dict, Any, Tuple
try:
    from .models import DeletionStatus, Step
except (ImportError, ValueError):
    from models import DeletionStatus, Step


class OutputEmitter:
    @staticmethod
    def format_steps_json(steps: List[Step]) -> str:
        """Serializes steps into JSON string."""
        if not steps:
            return "[]"
        step_dicts = [s.to_dict() for s in steps]
        # Sort keys deterministically
        return json.dumps(step_dicts, separators=(',', ':'))

    @classmethod
    def write_output_csv(cls, arg1, arg2):
        """
        Writes the results list to CSV file at output_path.
        Supports write_output_csv(output_path, results) or write_output_csv(results, output_path).
        Headers: case_id,deletion_status,steps_json
        """
        if isinstance(arg1, str):
            output_path, results = arg1, arg2
        else:
            results, output_path = arg1, arg2
        # Sort by natural or case_id order
        # Ensure case_id numerical sorting if format is case_XX
        def case_sort_key(item):
            cid = item[0]
            try:
                num = int(cid.split("_")[1])
                return num
            except Exception:
                return cid

        sorted_results = sorted(results, key=case_sort_key)

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
            writer.writerow(["case_id", "deletion_status", "steps_json"])
            for case_id, status, steps in sorted_results:
                status_str = status.value if isinstance(status, DeletionStatus) else str(status)
                steps_json = OutputEmitter.format_steps_json(steps)
                writer.writerow([case_id, status_str, steps_json])

    write_csv = write_output_csv
