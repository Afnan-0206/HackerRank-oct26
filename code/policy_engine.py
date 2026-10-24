"""
Policy Engine for Ghost Data Still Exists.
Determines required actions, findings, remediations, and verifications based on
deletion_rules.csv and the resource observed state.
"""
from typing import Tuple, Optional, List

try:
    from .models import (
        StepPhase,
        Operation,
        ResourceState,
        Step,
    )
except (ImportError, ValueError):
    from models import (
        StepPhase,
        Operation,
        ResourceState,
        Step,
    )


class PolicyEngine:
    def __init__(self):
        pass

    def evaluate_resource(
        self,
        system_id: str,
        resource_id: str,
        data_category: str,
        observed_state: str, # "PRESENT", "NOT_FOUND", "ANONYMIZED", "REDACTED", "ACTIVE", "DISABLED", "BLOCKED"
        evidence_id: str
    ) -> Tuple[Optional[Step], Optional[Step], Step]:
        """
        Returns (finding_step, remediation_step, verification_step).
        finding_step and remediation_step are None if the resource is already compliant.
        """
        cat = data_category.upper()
        stat = observed_state.upper()
        res_lower = resource_id.lower()

        # Rule 15: ACCOUNT_TOMBSTONE
        if cat == "ACCOUNT_TOMBSTONE" or "tombstone" in res_lower:
            if stat in ["PRESENT", "IDENTIFIABLE", "ACTIVE"]:
                # PII still exposed
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_EXPOSED_PII, ResourceState.PRESENT, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.REDACT_RECORD, ResourceState.REDACTED, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_REDACTED, ResourceState.REDACTED, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_REDACTED, ResourceState.REDACTED, evidence_id)
                return None, None, v

        # Rule 12 / 13: SECURITY_AUDIT_LOG / LEGAL_HOLD_RECORD
        if cat in ["SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"] or "audit" in res_lower:
            v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_PRESENT, ResourceState.PRESENT, evidence_id)
            return None, None, v

        # DATA_FLOW / Connector
        if cat == "DATA_FLOW" or "data_flow" in res_lower or "connector" in res_lower:
            if stat in ["ACTIVE", "ENABLED", "PRESENT"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_ACTIVE_DATA_FLOW, ResourceState.ACTIVE, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.DISABLE_DATA_FLOW, ResourceState.DISABLED, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_DATA_FLOW_DISABLED, ResourceState.DISABLED, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_DATA_FLOW_DISABLED, ResourceState.DISABLED, evidence_id)
                return None, None, v

        # BACKUP_ARCHIVE / Restore
        if cat == "BACKUP_ARCHIVE" or "backup" in res_lower:
            if stat in ["ACTIVE", "ENABLED", "PRESENT"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_RESTORE_RISK, ResourceState.ACTIVE, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.BLOCK_RESTORE, ResourceState.BLOCKED, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RESTORE_BLOCKED, ResourceState.BLOCKED, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RESTORE_BLOCKED, ResourceState.BLOCKED, evidence_id)
                return None, None, v

        # Rule 07 / 08: ANALYTICS_PROFILE / PRODUCT_TELEMETRY
        if cat in ["ANALYTICS_PROFILE", "PRODUCT_TELEMETRY"] or "analytics" in res_lower:
            if stat in ["PRESENT", "IDENTIFIABLE", "ACTIVE"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_EXPOSED_PII, ResourceState.PRESENT, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.ANONYMIZE_RECORD, ResourceState.ANONYMIZED, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ANONYMIZED, ResourceState.ANONYMIZED, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ANONYMIZED, ResourceState.ANONYMIZED, evidence_id)
                return None, None, v

        # Rule 06: SUPPORT_ATTACHMENT (Delete)
        if cat == "SUPPORT_ATTACHMENT" or "attachment" in res_lower or "object_" in res_lower:
            if stat in ["PRESENT", "FOUND", "ACTIVE", "IDENTIFIABLE"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_REMAINING_RECORD, ResourceState.PRESENT, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.DELETE_RECORD, ResourceState.ABSENT, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
                return None, None, v

        # Contact index or secondary identifier entries in support systems (Delete)
        if cat in ["SEARCH_INDEX_ENTRY", "SECONDARY_IDENTIFIER"]:
            if stat in ["PRESENT", "FOUND", "ACTIVE", "IDENTIFIABLE"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_REMAINING_RECORD, ResourceState.PRESENT, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.DELETE_RECORD, ResourceState.ABSENT, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
                return None, None, v

        # Rule 05: SUPPORT_MESSAGE (Redact)
        if cat == "SUPPORT_MESSAGE" or "support" in res_lower:
            # If it's a message with shared context, rule 05 says REDACT
            if stat in ["PRESENT", "IDENTIFIABLE", "ACTIVE", "FOUND"]:
                f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_EXPOSED_PII, ResourceState.PRESENT, evidence_id)
                r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.REDACT_RECORD, ResourceState.REDACTED, "")
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_REDACTED, ResourceState.REDACTED, evidence_id)
                return f, r, v
            else:
                v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_REDACTED, ResourceState.REDACTED, evidence_id)
                return None, None, v

        # All other delete categories: USER_PROFILE, SEARCH_INDEX_ENTRY, CACHE_ENTRY, USER_EXPORT, MARKETING_PROFILE
        if stat in ["PRESENT", "FOUND", "ACTIVE"]:
            f = Step(0, StepPhase.FINDING, system_id, resource_id, Operation.REPORT_REMAINING_RECORD, ResourceState.PRESENT, evidence_id)
            r = Step(0, StepPhase.REMEDIATION, system_id, resource_id, Operation.DELETE_RECORD, ResourceState.ABSENT, "")
            v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
            return f, r, v
        else:
            v = Step(0, StepPhase.VERIFICATION, system_id, resource_id, Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, evidence_id)
            return None, None, v
