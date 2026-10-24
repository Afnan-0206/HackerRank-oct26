# Ghost Data Still Exists — AI-Powered Deletion-Audit Agent
### HackerRank Orchestrate (October 2026)

This repository contains the complete, reproducible submission for **Ghost Data Still Exists**.
The system audits privacy deletion requests across distributed services, databases, logs, search indexes, backups, and exports to verify whether customer personal data has been completely erased, preserved under compliance retention, or still lingers.

---

## 1. Quick Start

### Prerequisites
- Python 3.10+ (tested on Python 3.12)
- Required packages: `pandas`, `pytest`, `pymupdf` (for PDF text extraction)

```bash
pip install pandas pytest pymupdf
```

### Reproduce the Submission Output
To run the deletion audit on the full evaluation dataset (`dataset/deletion_requests.csv`) and regenerate `output.csv` at the repository root:

```bash
python code/main.py --dataset-dir dataset --output-csv output.csv
```

### Validate Output Correctness
To run the automated schema, ordering, and consistency validator on `output.csv`:

```bash
python code/evaluation/validate_output.py output.csv dataset/deletion_requests.csv
```

### Run Automated Unit and Regression Tests
To execute all 14 unit, regression, and team-created test cases:

```bash
pytest code/tests/test_audit.py
```

### Run Evaluation Benchmarks
To run the evaluation script against the 32 sample cases and print accuracy metrics:

```bash
python code/evaluation/evaluate.py
```

---

## 2. System Architecture

The solution adheres strictly to separation of concerns:

```
code/
├── __init__.py
├── models.py              # Strict dataclasses, Enums, 15 permitted pairs, and sort orders
├── identity.py            # IdentityResolver indexing user_identities.csv and user_audit_log.csv
├── evidence_loader.py     # Multi-format ingestion (JSON, SQL, LOG, TXT, PDF) with trace correlation
├── policy_engine.py       # Deterministic policy rules driven by deletion_rules.csv
├── auditor.py             # DeletionAuditor orchestrating candidate discovery & as-of state
├── emitter.py             # OutputEmitter formatting RFC-compliant CSV and compact JSON
├── main.py                # Command-line entrypoint
├── evaluation/
│   ├── evaluate.py        # Accuracy metric benchmarking script
│   ├── validate_output.py # Strict output schema and contract validator
│   ├── evaluation_report.md
│   └── usage_report.md
└── tests/
    └── test_audit.py      # 14 pytest unit and regression tests
```

### Key Modules:
- **`IdentityResolver` (`identity.py`)**: Resolves current and historical identifiers across email changes, phone changes, and email hashes. Prevents cross-user leakage by anchoring on stable `user_id`.
- **`EvidenceLoader` (`evidence_loader.py`)**: Ingests all 94 evidence files. Unpacks nested SQL tables, extracts text and references from PDF attachments, normalizes timestamps, and groups distributed log traces via `trace_id` and `requestId`.
- **`PolicyEngine` (`policy_engine.py`)**: Applies the 15 policies from `deletion_rules.csv`. Enforces tombstone redaction (keeping stable user ID, removing PII), security audit retention, support conversation redaction vs attachment deletion, and active flow/restore blocking.
- **`DeletionAuditor` (`auditor.py`)**:
  - Classifies non-deletion requests as `NOT_APPLICABLE` (`steps_json=[]`).
  - Discovers candidate resources via direct identifiers, data catalog links, and deletion worker activity batches.
  - Queries evidence strictly at or before `audit_at`.
  - Determines overall status (`COMPLETE` if all compliant; `INCOMPLETE` if any deficiency exists).
  - Emits steps sorted by: Phase (`FINDING` -> `REMEDIATION` -> `VERIFICATION`), official Operation order, `system_id` ascending, `resource_id` ascending.
  - Renumbers steps consecutively 1..N starting from 1.

---

## 3. Correctness Contract & Invariants
- **Permitted 15 Pairs**: Only the 15 official operation/state combinations are permitted.
- **`NOT_APPLICABLE`**: Strictly `steps_json=[]`, 0 steps.
- **`COMPLETE`**: Only `VERIFICATION` steps; every step cites a valid participant-visible evidence file proving compliance.
- **`INCOMPLETE`**: Every `FINDING` cites valid evidence; proposed `REMEDIATION` and future `VERIFICATION` steps strictly use `evidence_id=""`.
- **Temporal Boundary**: Never cites evidence captured after the case's `audit_at`.
- **Empirical Proof**: Never treats workflow statuses or `reported_result` alone as proof of absence.

---

## 4. Evaluation Reports
Detailed reports are available under `code/evaluation/`:
- **`evaluation_report.md`**: Measured case status accuracy (100.0% on samples), step/field accuracy, 3 custom test cases, failure analysis.
- **`usage_report.md`**: Zero remote API calls, $0.00 cost, 4.88s runtime across 518 cases (106 requests/sec).
