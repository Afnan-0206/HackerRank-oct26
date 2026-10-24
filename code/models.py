"""
Data models, constants, and schema definitions for Ghost Data Still Exists.
Conforms strictly to HackerRank Orchestrate October 2026 problem_statement.md.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Set, Dict, Any


class DeletionStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class StepPhase(str, Enum):
    FINDING = "FINDING"
    REMEDIATION = "REMEDIATION"
    VERIFICATION = "VERIFICATION"


class Operation(str, Enum):
    # FINDING operations
    REPORT_REMAINING_RECORD = "REPORT_REMAINING_RECORD"
    REPORT_EXPOSED_PII = "REPORT_EXPOSED_PII"
    REPORT_ACTIVE_DATA_FLOW = "REPORT_ACTIVE_DATA_FLOW"
    REPORT_RESTORE_RISK = "REPORT_RESTORE_RISK"

    # REMEDIATION operations
    DISABLE_DATA_FLOW = "DISABLE_DATA_FLOW"
    BLOCK_RESTORE = "BLOCK_RESTORE"
    DELETE_RECORD = "DELETE_RECORD"
    REDACT_RECORD = "REDACT_RECORD"
    ANONYMIZE_RECORD = "ANONYMIZE_RECORD"

    # VERIFICATION operations
    VERIFY_RECORD_ABSENT = "VERIFY_RECORD_ABSENT"
    VERIFY_RECORD_PRESENT = "VERIFY_RECORD_PRESENT"
    VERIFY_RECORD_REDACTED = "VERIFY_RECORD_REDACTED"
    VERIFY_RECORD_ANONYMIZED = "VERIFY_RECORD_ANONYMIZED"
    VERIFY_DATA_FLOW_DISABLED = "VERIFY_DATA_FLOW_DISABLED"
    VERIFY_RESTORE_BLOCKED = "VERIFY_RESTORE_BLOCKED"


class ResourceState(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BLOCKED = "BLOCKED"
    REDACTED = "REDACTED"
    ANONYMIZED = "ANONYMIZED"


# Exact 15 permitted operation -> state mapping
OPERATION_STATE_MAP = {
    Operation.REPORT_REMAINING_RECORD: ResourceState.PRESENT,
    Operation.REPORT_EXPOSED_PII: ResourceState.PRESENT,
    Operation.REPORT_ACTIVE_DATA_FLOW: ResourceState.ACTIVE,
    Operation.REPORT_RESTORE_RISK: ResourceState.ACTIVE,
    Operation.DISABLE_DATA_FLOW: ResourceState.DISABLED,
    Operation.BLOCK_RESTORE: ResourceState.BLOCKED,
    Operation.DELETE_RECORD: ResourceState.ABSENT,
    Operation.REDACT_RECORD: ResourceState.REDACTED,
    Operation.ANONYMIZE_RECORD: ResourceState.ANONYMIZED,
    Operation.VERIFY_RECORD_ABSENT: ResourceState.ABSENT,
    Operation.VERIFY_RECORD_PRESENT: ResourceState.PRESENT,
    Operation.VERIFY_RECORD_REDACTED: ResourceState.REDACTED,
    Operation.VERIFY_RECORD_ANONYMIZED: ResourceState.ANONYMIZED,
    Operation.VERIFY_DATA_FLOW_DISABLED: ResourceState.DISABLED,
    Operation.VERIFY_RESTORE_BLOCKED: ResourceState.BLOCKED,
}

# Official sort order within each phase
PHASE_ORDER = {
    StepPhase.FINDING: 1,
    StepPhase.REMEDIATION: 2,
    StepPhase.VERIFICATION: 3,
}

OPERATION_ORDER = {
    # Phase 1: FINDING
    Operation.REPORT_REMAINING_RECORD: 1,
    Operation.REPORT_EXPOSED_PII: 2,
    Operation.REPORT_ACTIVE_DATA_FLOW: 3,
    Operation.REPORT_RESTORE_RISK: 4,

    # Phase 2: REMEDIATION
    Operation.DISABLE_DATA_FLOW: 1,
    Operation.BLOCK_RESTORE: 2,
    Operation.DELETE_RECORD: 3,
    Operation.REDACT_RECORD: 4,
    Operation.ANONYMIZE_RECORD: 5,

    # Phase 3: VERIFICATION
    Operation.VERIFY_RECORD_ABSENT: 1,
    Operation.VERIFY_RECORD_PRESENT: 2,
    Operation.VERIFY_RECORD_REDACTED: 3,
    Operation.VERIFY_RECORD_ANONYMIZED: 4,
    Operation.VERIFY_DATA_FLOW_DISABLED: 5,
    Operation.VERIFY_RESTORE_BLOCKED: 6,
}


@dataclass
class Step:
    step_no: int
    phase: StepPhase
    system_id: str
    resource_id: str
    operation: Operation
    state: ResourceState
    evidence_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_no": self.step_no,
            "phase": self.phase.value if isinstance(self.phase, StepPhase) else str(self.phase),
            "system_id": self.system_id,
            "resource_id": self.resource_id,
            "operation": self.operation.value if isinstance(self.operation, Operation) else str(self.operation),
            "state": self.state.value if isinstance(self.state, ResourceState) else str(self.state),
            "evidence_id": self.evidence_id,
        }


@dataclass
class UserIdentity:
    user_id: str
    current_email: str
    current_phone: str
    current_name: str
    email_hash: str
    all_emails: Set[str] = field(default_factory=set)
    all_phones: Set[str] = field(default_factory=set)
    all_names: Set[str] = field(default_factory=set)
    all_hashes: Set[str] = field(default_factory=set)


@dataclass
class DeletionRequest:
    case_id: str
    email: str
    requested_at: str
    reported_status: str
    reported_at: str
    audit_at: str
    request_text: str


@dataclass
class NormalizedEvidence:
    file_id: str
    captured_at: str
    system_id: str
    resource_id: str
    data_category: str
    observed_status: str # e.g. "PRESENT", "NOT_FOUND", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED", "FOUND"
    identifiers: Dict[str, Any] = field(default_factory=dict)
    raw_details: Dict[str, Any] = field(default_factory=dict)
