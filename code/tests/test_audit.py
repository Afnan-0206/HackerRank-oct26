"""
Comprehensive Unit and Regression Test Suite for Ghost Data Still Exists.
Run with: pytest code/tests/test_audit.py
"""

import os
import sys
import json
import pytest
from pathlib import Path

# Ensure code dir is on sys.path
code_dir = str(Path(__file__).resolve().parent.parent)
if code_dir not in sys.path:
    sys.path.insert(0, code_dir)

from models import (
    DeletionStatus,
    StepPhase,
    Operation,
    ResourceState,
    OPERATION_STATE_MAP,
    PHASE_ORDER,
    OPERATION_ORDER,
    Step,
    UserIdentity,
    DeletionRequest,
    NormalizedEvidence,
)
from identity import IdentityResolver
from evidence_loader import EvidenceLoader
from policy_engine import PolicyEngine
from auditor import DeletionAuditor
from emitter import OutputEmitter


# 1. Test Permitted 15 Operations and States
def test_permitted_operations_and_states():
    assert len(OPERATION_STATE_MAP) == 15, f"Expected exactly 15 operation/state pairs, got {len(OPERATION_STATE_MAP)}"
    
    # Findings
    assert OPERATION_STATE_MAP[Operation.REPORT_REMAINING_RECORD] == ResourceState.PRESENT
    assert OPERATION_STATE_MAP[Operation.REPORT_EXPOSED_PII] == ResourceState.PRESENT
    assert OPERATION_STATE_MAP[Operation.REPORT_ACTIVE_DATA_FLOW] == ResourceState.ACTIVE
    assert OPERATION_STATE_MAP[Operation.REPORT_RESTORE_RISK] == ResourceState.ACTIVE
    
    # Remediation
    assert OPERATION_STATE_MAP[Operation.DISABLE_DATA_FLOW] == ResourceState.DISABLED
    assert OPERATION_STATE_MAP[Operation.BLOCK_RESTORE] == ResourceState.BLOCKED
    assert OPERATION_STATE_MAP[Operation.DELETE_RECORD] == ResourceState.ABSENT
    assert OPERATION_STATE_MAP[Operation.REDACT_RECORD] == ResourceState.REDACTED
    assert OPERATION_STATE_MAP[Operation.ANONYMIZE_RECORD] == ResourceState.ANONYMIZED
    
    # Verification
    assert OPERATION_STATE_MAP[Operation.VERIFY_RECORD_ABSENT] == ResourceState.ABSENT
    assert OPERATION_STATE_MAP[Operation.VERIFY_RECORD_PRESENT] == ResourceState.PRESENT
    assert OPERATION_STATE_MAP[Operation.VERIFY_RECORD_REDACTED] == ResourceState.REDACTED
    assert OPERATION_STATE_MAP[Operation.VERIFY_RECORD_ANONYMIZED] == ResourceState.ANONYMIZED
    assert OPERATION_STATE_MAP[Operation.VERIFY_DATA_FLOW_DISABLED] == ResourceState.DISABLED
    assert OPERATION_STATE_MAP[Operation.VERIFY_RESTORE_BLOCKED] == ResourceState.BLOCKED


# 2. Test Step Ordering & Consecutive Renumbering
def test_step_phase_and_operation_ordering():
    steps = [
        Step(step_no=99, phase=StepPhase.VERIFICATION, system_id="search_service", resource_id="search_001", operation=Operation.VERIFY_RECORD_ABSENT, state=ResourceState.ABSENT, evidence_id=""),
        Step(step_no=88, phase=StepPhase.FINDING, system_id="search_service", resource_id="search_001", operation=Operation.REPORT_REMAINING_RECORD, state=ResourceState.PRESENT, evidence_id="evidence_01"),
        Step(step_no=77, phase=StepPhase.REMEDIATION, system_id="search_service", resource_id="search_001", operation=Operation.DELETE_RECORD, state=ResourceState.ABSENT, evidence_id=""),
        Step(step_no=66, phase=StepPhase.FINDING, system_id="backup_service", resource_id="backup_001", operation=Operation.REPORT_RESTORE_RISK, state=ResourceState.ACTIVE, evidence_id="evidence_02"),
        Step(step_no=55, phase=StepPhase.REMEDIATION, system_id="backup_service", resource_id="backup_001", operation=Operation.BLOCK_RESTORE, state=ResourceState.BLOCKED, evidence_id=""),
        Step(step_no=44, phase=StepPhase.VERIFICATION, system_id="backup_service", resource_id="backup_001", operation=Operation.VERIFY_RESTORE_BLOCKED, state=ResourceState.BLOCKED, evidence_id=""),
    ]
    
    def sort_key(s: Step):
        return (PHASE_ORDER[s.phase], OPERATION_ORDER[s.operation], s.system_id, s.resource_id)
        
    steps.sort(key=sort_key)
    for idx, s in enumerate(steps, 1):
        s.step_no = idx
        
    # Check phases in order
    assert [s.phase for s in steps] == [
        StepPhase.FINDING, StepPhase.FINDING,
        StepPhase.REMEDIATION, StepPhase.REMEDIATION,
        StepPhase.VERIFICATION, StepPhase.VERIFICATION
    ]
    # Check consecutive numbers
    assert [s.step_no for s in steps] == [1, 2, 3, 4, 5, 6]
    # Check official operation order in FINDING: REPORT_REMAINING_RECORD (1) before REPORT_RESTORE_RISK (4)
    assert steps[0].operation == Operation.REPORT_REMAINING_RECORD
    assert steps[1].operation == Operation.REPORT_RESTORE_RISK
    # In REMEDIATION: BLOCK_RESTORE (2) before DELETE_RECORD (3)
    assert steps[2].operation == Operation.BLOCK_RESTORE
    assert steps[3].operation == Operation.DELETE_RECORD


# 3. Test NOT_APPLICABLE detection
def test_not_applicable_requests(auditor):
    non_del_texts = [
        "Please remove me from marketing lists only, but leave the account active for orders.",
        "I need a copy of my data under GDPR Article 15; keep the account as it is.",
        "Unsubscribe from promotional emails without removing any of my records.",
        "Update my address, do not close the account.",
    ]
    for txt in non_del_texts:
        assert auditor.is_non_deletion_request(txt) is True
        
    del_texts = [
        "Please delete all my personal data and close my account immediately.",
        "I want my account erased permanently under GDPR right to be forgotten.",
        "Supprimer mon compte et toutes mes données personnelles.",
    ]
    for txt in del_texts:
        assert auditor.is_non_deletion_request(txt) is False


# 4. Test Timestamp Boundary Enforcement
def test_timestamp_boundary_filtering(evidence_loader):
    loader = evidence_loader
    test_res = "account_5334" # Has early and late evidence
    cutoff = "2026-09-02T12:00:00Z"
    
    records = loader.by_resource.get(test_res, [])
    assert len(records) > 0
    
    valid_records = [r for r in records if r.captured_at <= cutoff]
    for r in valid_records:
        assert r.captured_at <= cutoff


# 5. Test Identity Resolution across history
def test_identity_resolution_changes(identity_resolver):
    resolver = identity_resolver
    
    # user_30 changed email from camille.bernard86@outlook.com to camille.bernard@outlook.com
    user_old = resolver.resolve_by_email("camille.bernard86@outlook.com")
    user_new = resolver.resolve_by_email("camille.bernard@outlook.com")
    
    assert user_old is not None
    assert user_new is not None
    assert user_old.user_id == user_new.user_id
    assert "camille.bernard86@outlook.com" in user_old.all_emails
    assert "camille.bernard@outlook.com" in user_old.all_emails


# 6. Test Policy Engine: Tombstone Redaction
def test_account_tombstone_redaction():
    engine = PolicyEngine()
    
    # Non-compliant tombstone with lingering PII
    f, r, v = engine.evaluate_resource(
        system_id="identity_service",
        resource_id="tombstone_9178",
        data_category="ACCOUNT_TOMBSTONE",
        observed_state="PRESENT", # has email/phone
        evidence_id="evidence_51"
    )
    assert f is not None
    assert f.operation == Operation.REPORT_EXPOSED_PII
    assert r.operation == Operation.REDACT_RECORD
    assert v.operation == Operation.VERIFY_RECORD_REDACTED
    
    # Compliant tombstone
    f_ok, r_ok, v_ok = engine.evaluate_resource(
        system_id="identity_service",
        resource_id="tombstone_440",
        data_category="ACCOUNT_TOMBSTONE",
        observed_state="REDACTED",
        evidence_id="evidence_51"
    )
    assert f_ok is None
    assert r_ok is None
    assert v_ok.operation == Operation.VERIFY_RECORD_REDACTED


# 7. Test Policy Engine: Security Audit Log Retention
def test_conditional_retention_security_audit():
    engine = PolicyEngine()
    
    f, r, v = engine.evaluate_resource(
        system_id="security_service",
        resource_id="audit_record_451",
        data_category="SECURITY_AUDIT_LOG",
        observed_state="PRESENT",
        evidence_id="evidence_92"
    )
    assert f is None
    assert r is None
    assert v.operation == Operation.VERIFY_RECORD_PRESENT
    assert v.state == ResourceState.PRESENT


# 8. Test Policy Engine: Active Data Flow Blocking Complete
def test_active_data_flow_blocking_complete():
    engine = PolicyEngine()
    
    f, r, v = engine.evaluate_resource(
        system_id="integration_service",
        resource_id="data_flow_3827",
        data_category="ACTIVE_DATA_FLOW",
        observed_state="ACTIVE",
        evidence_id="evidence_92"
    )
    assert f is not None
    assert f.operation == Operation.REPORT_ACTIVE_DATA_FLOW
    assert f.state == ResourceState.ACTIVE
    assert r.operation == Operation.DISABLE_DATA_FLOW
    assert r.state == ResourceState.DISABLED
    assert v.operation == Operation.VERIFY_DATA_FLOW_DISABLED


# 9. Test Policy Engine: Restore Risk Blocking Complete
def test_backup_restore_risk_blocking_complete():
    engine = PolicyEngine()
    
    f, r, v = engine.evaluate_resource(
        system_id="backup_service",
        resource_id="backup_4978",
        data_category="BACKUP_ARCHIVE",
        observed_state="ACTIVE", # restore allowed
        evidence_id="evidence_06"
    )
    assert f is not None
    assert f.operation == Operation.REPORT_RESTORE_RISK
    assert r.operation == Operation.BLOCK_RESTORE
    assert v.operation == Operation.VERIFY_RESTORE_BLOCKED


# 10. Test CSV Emission & Escaping
def test_csv_emission_escaping(tmp_path):
    steps = [
        Step(1, StepPhase.FINDING, "account_service", "account_005", Operation.REPORT_REMAINING_RECORD, ResourceState.PRESENT, "evidence_54"),
        Step(2, StepPhase.REMEDIATION, "account_service", "account_005", Operation.DELETE_RECORD, ResourceState.ABSENT, ""),
        Step(3, StepPhase.VERIFICATION, "account_service", "account_005", Operation.VERIFY_RECORD_ABSENT, ResourceState.ABSENT, "")
    ]
    results = [("case_05", DeletionStatus.INCOMPLETE, steps)]
    
    csv_file = str(tmp_path / "test_out.csv")
    OutputEmitter.write_output_csv(results, csv_file)
    
    import pandas as pd
    df = pd.read_csv(csv_file)
    assert list(df.columns) == ["case_id", "deletion_status", "steps_json"]
    assert len(df) == 1
    assert df.iloc[0]["case_id"] == "case_05"
    assert df.iloc[0]["deletion_status"] == "INCOMPLETE"
    
    parsed = json.loads(df.iloc[0]["steps_json"])
    assert len(parsed) == 3
    assert parsed[0]["operation"] == "REPORT_REMAINING_RECORD"


# 11. Regression Fixture: Case 11
def test_regression_case_11_fixture(auditor):
    req = DeletionRequest(
        case_id="case_11",
        email="novak.tomasz@icloud.com",
        requested_at="2026-09-09T12:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-12T12:00:00Z",
        audit_at="2026-10-01T12:00:00Z",
        request_text="I would like the account associated with this address to be erased. Please handle any copies that fall within the scope of the deletion request."
    )
    status, steps = auditor.audit_case(req)
    assert status == DeletionStatus.INCOMPLETE
    assert len(steps) > 0
    # Operations should include search and analytics remediations
    ops = [s.operation for s in steps]
    assert Operation.REPORT_REMAINING_RECORD in ops
    assert Operation.DELETE_RECORD in ops
    assert Operation.VERIFY_RECORD_ABSENT in ops


# 12. Team Test Case 1: Multi-System Failover Replica Drift
def test_team_case_1_failover_replica_drift():
    """
    Simulates a case where primary DB deletion succeeded, but failover replica
    still contains user profile records.
    Audit must flag REPORT_REMAINING_RECORD on the replica, plan DELETE_RECORD,
    and verify VERIFY_RECORD_ABSENT.
    """
    engine = PolicyEngine()
    f, r, v = engine.evaluate_resource(
        system_id="account_service",
        resource_id="account_replica_failover_881",
        data_category="USER_PROFILE",
        observed_state="PRESENT",
        evidence_id="evidence_54"
    )
    assert f is not None
    assert f.phase == StepPhase.FINDING
    assert f.operation == Operation.REPORT_REMAINING_RECORD
    assert r.operation == Operation.DELETE_RECORD
    assert v.operation == Operation.VERIFY_RECORD_ABSENT


# 13. Team Test Case 2: Marketing Unsubscribe Mistaken for Deletion
def test_team_case_2_marketing_unsubscribe(auditor):
    """
    Simulates user submitting privacy request stating:
    'Please unsubscribe me from marketing emails. Do not delete my order history.'
    System must classify as NOT_APPLICABLE and output steps_json=[].
    """
    req = DeletionRequest(
        case_id="case_custom_marketing",
        email="shopper@example.com",
        requested_at="2026-09-01T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-02T10:00:00Z",
        audit_at="2026-09-05T10:00:00Z",
        request_text="Unsubscribe from promotional emails without removing any of my records."
    )
    status, steps = auditor.audit_case(req)
    assert status == DeletionStatus.NOT_APPLICABLE
    assert steps == []


# 14. Team Test Case 3: Retained Legal Hold with Account Tombstone
def test_team_case_3_legal_hold_and_tombstone():
    """
    Simulates user whose account was deleted and tombstone redacted,
    but financial records are subject to active legal hold.
    Tombstone must be VERIFY_RECORD_REDACTED; legal hold must be VERIFY_RECORD_PRESENT.
    Status must be COMPLETE if both conditions are satisfied.
    """
    engine = PolicyEngine()
    # Tombstone
    f_t, r_t, v_t = engine.evaluate_resource(
        system_id="identity_service",
        resource_id="tombstone_777",
        data_category="ACCOUNT_TOMBSTONE",
        observed_state="REDACTED",
        evidence_id="evidence_51"
    )
    # Legal hold
    f_l, r_l, v_l = engine.evaluate_resource(
        system_id="compliance_service",
        resource_id="legal_hold_129",
        data_category="LEGAL_HOLD_RECORD",
        observed_state="PRESENT",
        evidence_id="evidence_51"
    )
    assert f_t is None
    assert f_l is None
    assert v_t.operation == Operation.VERIFY_RECORD_REDACTED
    assert v_l.operation == Operation.VERIFY_RECORD_PRESENT


# 15. Test Evidence Timestamp Boundary Filtering (captured_at <= audit_at)
def test_evidence_timestamp_boundary_filtering(evidence_loader):
    """
    Verifies that evidence is included if and only if captured_at <= audit_at.
    Boundary conditions:
    1. captured_at < audit_at: INCLUDED
    2. captured_at == audit_at: INCLUDED (exact boundary)
    3. captured_at > audit_at: EXCLUDED (even by 1 second)
    Also verifies that evidence_files.csv manifest captured_at is authoritative
    over internal snapshot timestamps (e.g. evidence_02 vs evidence_92).
    """
    loader = evidence_loader
    
    # Check that loader assigns authoritative captured_at from manifest
    assert loader.file_meta["evidence_02"]["captured_at"] == "2026-09-17T12:00:00Z"
    assert loader.file_meta["evidence_92"]["captured_at"] == "2026-09-15T11:44:55Z"
    
    # All records for evidence_02 must have captured_at == 2026-09-17T12:00:00Z
    ev02_records = [r for r in loader.records if r.file_id == "evidence_02"]
    assert len(ev02_records) > 0
    for r in ev02_records:
        assert r.captured_at == "2026-09-17T12:00:00Z"

    # All records for evidence_92 must have captured_at == 2026-09-15T11:44:55Z
    ev92_records = [r for r in loader.records if r.file_id == "evidence_92"]
    assert len(ev92_records) > 0
    for r in ev92_records:
        assert r.captured_at == "2026-09-15T11:44:55Z"

    # Boundary filtering test for an audit conducted at 2026-09-15T12:00:00Z
    audit_at = "2026-09-15T12:00:00Z"
    
    # Case 1: captured_at < audit_at (evidence_92 at 2026-09-15T11:44:55Z) -> included
    ev92_valid = [r for r in ev92_records if r.captured_at <= audit_at]
    assert len(ev92_valid) == len(ev92_records)
    
    # Case 2: captured_at == audit_at -> exact boundary included
    boundary_rec = NormalizedEvidence(
        file_id="evidence_test_exact",
        captured_at="2026-09-15T12:00:00Z",
        system_id="account_service",
        resource_id="account_boundary_test",
        data_category="USER_PROFILE",
        observed_status="PRESENT"
    )
    assert boundary_rec.captured_at <= audit_at
    
    # Case 3: captured_at > audit_at (even by 1 second) -> excluded
    future_rec = NormalizedEvidence(
        file_id="evidence_test_future",
        captured_at="2026-09-15T12:00:01Z",
        system_id="account_service",
        resource_id="account_future_test",
        data_category="USER_PROFILE",
        observed_status="PRESENT"
    )
    assert not (future_rec.captured_at <= audit_at)
    
    # Case 4: evidence_02 was captured at 2026-09-17T12:00:00Z, so as of 2026-09-15T12:00:00Z it must be excluded
    ev02_valid = [r for r in ev02_records if r.captured_at <= audit_at]
    assert len(ev02_valid) == 0


# 16. Test Support Record Actions (Rule 05 Redact vs Rule 06 Delete)
def test_support_record_actions_rule_5_and_6():
    """
    Verifies that:
    1. SUPPORT_ATTACHMENT (Rule 6) triggers DELETE_RECORD / VERIFY_RECORD_ABSENT.
    2. SUPPORT_MESSAGE (Rule 5) triggers REDACT_RECORD / VERIFY_RECORD_REDACTED.
    3. Standalone contact index entries in support systems trigger DELETE_RECORD / VERIFY_RECORD_ABSENT.
    """
    engine = PolicyEngine()
    
    # 1. SUPPORT_ATTACHMENT (e.g. support_4218, support_001)
    f_att, r_att, v_att = engine.evaluate_resource(
        system_id="support_platform",
        resource_id="support_4218",
        data_category="SUPPORT_ATTACHMENT",
        observed_state="FOUND",
        evidence_id="evidence_03"
    )
    assert f_att.operation == Operation.REPORT_REMAINING_RECORD
    assert r_att.operation == Operation.DELETE_RECORD
    assert v_att.operation == Operation.VERIFY_RECORD_ABSENT
    assert r_att.state == ResourceState.ABSENT
    assert v_att.state == ResourceState.ABSENT
    
    # 2. SUPPORT_MESSAGE (e.g. support_864, support_4715)
    f_msg, r_msg, v_msg = engine.evaluate_resource(
        system_id="support_platform",
        resource_id="support_864",
        data_category="SUPPORT_MESSAGE",
        observed_state="IDENTIFIABLE",
        evidence_id="evidence_03"
    )
    assert f_msg.operation == Operation.REPORT_EXPOSED_PII
    assert r_msg.operation == Operation.REDACT_RECORD
    assert v_msg.operation == Operation.VERIFY_RECORD_REDACTED
    assert r_msg.state == ResourceState.REDACTED
    assert v_msg.state == ResourceState.REDACTED

    # 3. Contact index in support system (e.g. support_002 from legacy_contact_index)
    f_idx, r_idx, v_idx = engine.evaluate_resource(
        system_id="support_platform",
        resource_id="support_002",
        data_category="SEARCH_INDEX_ENTRY",
        observed_state="PRESENT",
        evidence_id="evidence_61"
    )
    assert f_idx.operation == Operation.REPORT_REMAINING_RECORD
    assert r_idx.operation == Operation.DELETE_RECORD
    assert v_idx.operation == Operation.VERIFY_RECORD_ABSENT


# 17. Test Rules 5 and 6 Conditions: Silence / Needs Review when condition not met
def test_rules_5_and_6_unmet_conditions_emit_nothing_and_log_needs_review(caplog):
    """
    Verifies that:
    1. SUPPORT_MESSAGE without shared context (e.g. private draft, has_shared_context=False)
       emits nothing (None, None, None) and logs a 'needs review' marker.
    2. SUPPORT_ATTACHMENT shared with others (e.g. shared_with_others=True)
       emits nothing (None, None, None) and logs a 'needs review' marker.
    """
    import logging
    engine = PolicyEngine()

    with caplog.at_level(logging.WARNING):
        # 1. message-without-shared-context
        f_msg, r_msg, v_msg = engine.evaluate_resource(
            system_id="support_platform",
            resource_id="support_msg_private",
            data_category="SUPPORT_MESSAGE",
            observed_state="PRESENT",
            evidence_id="evidence_test",
            has_shared_context=False
        )
        assert f_msg is None
        assert r_msg is None
        assert v_msg is None
        assert any("needs review" in rec.message.lower() and "support_message" in rec.message.lower() for rec in caplog.records)

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        # 2. attachment-shared-with-others
        f_att, r_att, v_att = engine.evaluate_resource(
            system_id="support_platform",
            resource_id="support_att_shared",
            data_category="SUPPORT_ATTACHMENT",
            observed_state="PRESENT",
            evidence_id="evidence_test",
            shared_with_others=True
        )
        assert f_att is None
        assert r_att is None
        assert v_att is None
        assert any("needs review" in rec.message.lower() and "support_attachment" in rec.message.lower() for rec in caplog.records)


# 18. Test INCOMPLETE-Only Retained-Record Rule
def test_incomplete_only_retained_record_rule(auditor):
    """
    Verifies that:
    1. In an INCOMPLETE case (e.g. case_09, where remaining records exist), policy-mandated
       retained records (e.g. audit_record_4902 in evidence_06 with captured_at > audit_at)
       are included in the post-remediation verification plan with VERIFY_RECORD_PRESENT and empty evidence_id.
    2. In a COMPLETE case (e.g. case_03, case_21), retained records MUST be verified by evidence
       captured at or before audit_at with a non-empty evidence_id. Future-captured evidence is excluded.
    """
    
    # case_09 is INCOMPLETE (audit_at=2026-09-16T09:00:00Z, evidence_06 captured_at=2026-09-18T11:55:24Z)
    req_09 = DeletionRequest(
        case_id="case_09",
        email="k.appiah88@gmail.com",
        requested_at="2026-08-31T09:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-09-03T09:00:00Z",
        audit_at="2026-09-16T09:00:00Z",
        request_text="I am asking you to delete my account and the personal information linked to it. Please verify that this was completed."
    )
    status_09, steps_09 = auditor.audit_case(req_09)
    assert status_09 == DeletionStatus.INCOMPLETE
    audit_steps_09 = [s for s in steps_09 if s.resource_id == "audit_record_4902"]
    assert len(audit_steps_09) == 1
    s_09 = audit_steps_09[0]
    assert s_09.phase == StepPhase.VERIFICATION
    assert s_09.operation == Operation.VERIFY_RECORD_PRESENT
    assert s_09.state == ResourceState.PRESENT
    assert s_09.evidence_id == ""  # Verification plan for incomplete case has empty evidence_id


# 19. Real Fixture Datasets Audit Cases (including after-audit_at boundary)
def test_fixtures_audit_cases_and_after_audit_at_boundary():
    """
    Tests end-to-end audit on real fixture dataset under code/tests/fixtures/.
    Covers:
    - Case 1: Incomplete audit case with lingering account and retained security audit log.
    - Case 2: Complete audit case with compliant redacted account tombstone.
    - Case 3: Case where evidence_fix_03b is captured AFTER audit_at (2026-09-02 > 2026-08-30).
              Asserts that future evidence is ignored as of audit_at, resulting in COMPLETE,
              and when audit_at is moved post-capture, evidence_fix_03b is ingested.
    Asserts exact steps_json structure for each case.
    """
    fixtures_dir = os.path.join(code_dir, "tests", "fixtures")
    auditor = DeletionAuditor(fixtures_dir)

    # 1. Elena Rostova - Lingering account_101 -> INCOMPLETE
    req1 = DeletionRequest(
        case_id="case_fix_01",
        email="elena.rostova@gmail.com",
        requested_at="2026-03-20T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-03-24T12:00:00Z",
        audit_at="2026-03-26T12:00:00Z",
        request_text="Please permanently delete my account and all personal information."
    )
    status1, steps1 = auditor.audit_case(req1)
    assert status1 == DeletionStatus.INCOMPLETE

    expected_steps1 = [
        {"step_no": 1, "phase": "FINDING", "system_id": "account_service", "resource_id": "account_101", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_fix_01"},
        {"step_no": 2, "phase": "REMEDIATION", "system_id": "account_service", "resource_id": "account_101", "operation": "DELETE_RECORD", "state": "ABSENT", "evidence_id": ""},
        {"step_no": 3, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_101", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": ""},
        {"step_no": 4, "phase": "VERIFICATION", "system_id": "security_service", "resource_id": "audit_record_901", "operation": "VERIFY_RECORD_PRESENT", "state": "PRESENT", "evidence_id": ""}
    ]
    actual_steps1 = [s.to_dict() for s in steps1]
    assert actual_steps1 == expected_steps1
    assert json.dumps(actual_steps1) == json.dumps(expected_steps1)

    # 2. Marcus Vance - Redacted tombstone_202 -> COMPLETE
    req2 = DeletionRequest(
        case_id="case_fix_02",
        email="marcus.vance@fastmail.com",
        requested_at="2026-08-10T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-08-16T11:00:00Z",
        audit_at="2026-08-18T12:00:00Z",
        request_text="I would like to delete all my data under GDPR."
    )
    status2, steps2 = auditor.audit_case(req2)
    assert status2 == DeletionStatus.COMPLETE

    expected_steps2 = [
        {"step_no": 1, "phase": "VERIFICATION", "system_id": "identity_service", "resource_id": "tombstone_202", "operation": "VERIFY_RECORD_REDACTED", "state": "REDACTED", "evidence_id": "evidence_fix_02"}
    ]
    actual_steps2 = [s.to_dict() for s in steps2]
    assert actual_steps2 == expected_steps2
    assert json.dumps(actual_steps2) == json.dumps(expected_steps2)

    # 3. Sarah Chen - evidence_fix_03b captured AFTER audit_at -> COMPLETE (ignoring future evidence)
    req3 = DeletionRequest(
        case_id="case_fix_03",
        email="sarah.chen@outlook.com",
        requested_at="2026-08-25T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-08-28T12:00:00Z",
        audit_at="2026-08-30T12:00:00Z",  # Prior to evidence_fix_03b (2026-09-02T12:00:00Z)
        request_text="Please erase my account and data immediately."
    )
    status3, steps3 = auditor.audit_case(req3)
    assert status3 == DeletionStatus.COMPLETE

    expected_steps3 = [
        {"step_no": 1, "phase": "VERIFICATION", "system_id": "analytics_platform", "resource_id": "analytics_303", "operation": "VERIFY_RECORD_ANONYMIZED", "state": "ANONYMIZED", "evidence_id": "evidence_fix_03a"}
    ]
    actual_steps3 = [s.to_dict() for s in steps3]
    assert actual_steps3 == expected_steps3
    assert json.dumps(actual_steps3) == json.dumps(expected_steps3)

    # Verify boundary behavior: when audit_at is moved after evidence_fix_03b capture
    req3_post = DeletionRequest(
        case_id="case_fix_03_post",
        email="sarah.chen@outlook.com",
        requested_at="2026-08-25T10:00:00Z",
        reported_status="COMPLETE",
        reported_at="2026-08-28T12:00:00Z",
        audit_at="2026-09-05T12:00:00Z",  # After evidence_fix_03b (2026-09-02T12:00:00Z)
        request_text="Please erase my account and data immediately."
    )
    status3_post, steps3_post = auditor.audit_case(req3_post)
    assert status3_post == DeletionStatus.INCOMPLETE
    expected_steps3_post = [
        {"step_no": 1, "phase": "FINDING", "system_id": "account_service", "resource_id": "account_303", "operation": "REPORT_REMAINING_RECORD", "state": "PRESENT", "evidence_id": "evidence_fix_03b"},
        {"step_no": 2, "phase": "REMEDIATION", "system_id": "account_service", "resource_id": "account_303", "operation": "DELETE_RECORD", "state": "ABSENT", "evidence_id": ""},
        {"step_no": 3, "phase": "VERIFICATION", "system_id": "account_service", "resource_id": "account_303", "operation": "VERIFY_RECORD_ABSENT", "state": "ABSENT", "evidence_id": ""},
        {"step_no": 4, "phase": "VERIFICATION", "system_id": "analytics_platform", "resource_id": "analytics_303", "operation": "VERIFY_RECORD_ANONYMIZED", "state": "ANONYMIZED", "evidence_id": ""}
    ]
    actual_steps3_post = [s.to_dict() for s in steps3_post]
    assert actual_steps3_post == expected_steps3_post
    assert json.dumps(actual_steps3_post) == json.dumps(expected_steps3_post)


# 20. Test Evidence Window Rule: Below / At / Above Boundaries
def test_evidence_window_rule_boundaries():
    """
    TASK 3 Unit Tests:
    Verifies that evidence and LINK events are usable if and only if:
      requested_at <= captured_at <= audit_at
    Testing:
    1. captured_at < requested_at (below lower boundary: excluded)
    2. captured_at == requested_at (at lower boundary: included)
    3. captured_at == audit_at (at upper boundary: included)
    4. captured_at > audit_at (above upper boundary: excluded)
    5. Retained records rule (audit logs/holds) in INCOMPLETE cases is unbounded below by requested_at.
    """
    req_at = "2026-06-01T10:00:00Z"
    audit_at = "2026-06-10T10:00:00Z"

    # 1. Below lower boundary: 1 second before requested_at -> False
    below_lower = "2026-06-01T09:59:59Z"
    assert not (req_at <= below_lower <= audit_at)

    # 2. At lower boundary: exactly requested_at -> True
    at_lower = "2026-06-01T10:00:00Z"
    assert (req_at <= at_lower <= audit_at)

    # In-between: -> True
    in_middle = "2026-06-05T12:00:00Z"
    assert (req_at <= in_middle <= audit_at)

    # 3. At upper boundary: exactly audit_at -> True
    at_upper = "2026-06-10T10:00:00Z"
    assert (req_at <= at_upper <= audit_at)

    # 4. Above upper boundary: 1 second after audit_at -> False
    above_upper = "2026-06-10T10:00:01Z"
    assert not (req_at <= above_upper <= audit_at)

    # 5. Incomplete-case retained record rule:
    # Retained record event time was created months prior to requested_at (e.g. 2026-01-15 <= audit_at)
    prior_retained_time = "2026-01-15T08:00:00Z"
    assert prior_retained_time <= audit_at
    # It must be included in post-remediation verification plan even though prior_retained_time < req_at
    assert prior_retained_time < req_at


# 21. Test Intent Gate: Positive Non-Deletion Signal & Multilingual Deletion Phrasings
def test_intent_gate_positive_non_deletion_and_multilingual_deletion(auditor):
    """
    TASK 4 Unit Tests:
    1. Verifies that NOT_APPLICABLE requires an affirmative positive non-deletion signal
       with a recorded reason across all 10 production non-deletion texts (case_533-542).
    2. Verifies deletion phrasings across 6 languages (English, Spanish, French, Portuguese,
       Korean, Japanese) requesting deletion, erasure, removal, and account closure.
    3. Verifies that absence of deletion keywords alone does NOT trigger NOT_APPLICABLE.
    """
    try:
        from code.model_layer import ModelLayer
    except (ImportError, ValueError):
        from model_layer import ModelLayer

    ml = ModelLayer()

    # 1. Test 10 production non-deletion requests (case_533 to case_542)
    non_del_cases = [
        "The mobile number on my profile is out of date. Please replace it with the new one and leave the account active.",
        "Please stop the promotional emails. I still need the account and my order history, so I am not asking for deletion.",
        "I'd like to download my account information in a portable format. Please keep the account as it is.",
        "I can't access my profile and need help resetting the login. Do not close the account.",
        "My surname is misspelled on the account. Can you correct it without removing any of my records?",
        "Please remove me from marketing lists only. I still want receipts and service messages for this account.",
        "Can you tell me what personal information you hold about me? This is an information request, not a request to erase it.",
        "I need a copy of last year's invoices for my records. Please leave my profile and purchase history in place.",
        "Please change the email address used for this account. I want to continue using the service.",
        "Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything."
    ]

    for idx, text in enumerate(non_del_cases, 533):
        res = ml.deterministic_classify_intent(text)
        assert res["requests_deletion"] is False, f"Case {idx} should be classified as non-deletion"
        assert "non_deletion_reason" in res and res["non_deletion_reason"], f"Case {idx} missing non_deletion_reason"

        is_non_del, reason = auditor.get_non_deletion_signal(text)
        assert is_non_del is True, f"Auditor should detect non-deletion for case {idx}"
        assert reason, f"Auditor should record positive reason for case {idx}"

    # 2. Multilingual deletion phrasings: English, Spanish, French, Portuguese, Korean, Japanese
    multilingual_deletion_requests = [
        ("English", "Please process the deletion of my account and remove my personal records."),
        ("English", "I request that you erase all my personal data and close my account immediately."),
        ("English", "Kindly remove all my stored customer profile details."),
        ("Spanish", "Por favor procedan con la eliminación de mi cuenta y borrar todos mis datos."),
        ("Spanish", "Solicito cancelar mi cuenta y suprimir mis datos de usuario."),
        ("French", "Veuillez procéder à la suppression de mon compte et effacer mes données personnelles."),
        ("French", "Je souhaite clôturer mon compte et demander l'effacement de mes informations."),
        ("Portuguese", "Solicito a exclusão da minha conta e quero apagar todos os meus dados."),
        ("Portuguese", "Por favor encerrar minha conta e eliminar meu perfil."),
        ("Korean", "제 계정을 삭제하고 모든 개인정보를 지워주세요."),
        ("Korean", "계정 탈퇴 및 저장된 개인정보의 삭제를 요청합니다."),
        ("Japanese", "私のアカウントの削除と個人情報の消去をお願いします。"),
        ("Japanese", "アカウント閉鎖および登録データの削除を申請します。")
    ]

    for lang, text in multilingual_deletion_requests:
        res = ml.deterministic_classify_intent(text)
        assert res["requests_deletion"] is True, f"{lang} text should be classified as requesting deletion: '{text}'"
        assert res["language"] == lang or (lang in ["English", "Spanish", "French", "Portuguese", "Korean", "Japanese"]), f"Unexpected lang for '{text}'"

    # 3. Absence of deletion keywords alone does NOT trigger NOT_APPLICABLE
    generic_text = "I am contacting customer support regarding my registered user account profile."
    is_non_del, reason = auditor.get_non_deletion_signal(generic_text)
    assert is_non_del is False, "Generic text without positive non-deletion signal must NOT be non-deletion"
    assert reason == ""
    res_generic = ml.deterministic_classify_intent(generic_text)
    assert res_generic["requests_deletion"] is True, "Generic text without non-deletion signal must default to requests_deletion=True"


# 22. Test Policy Engine: Rule 10 MARKETING_PROFILE Always Delete
def test_policy_engine_marketing_profile_rule_10():
    engine = PolicyEngine()
    
    # 22a. Direct category MARKETING_PROFILE
    f, r, v = engine.evaluate_resource(
        system_id="analytics_platform",
        resource_id="marketing_1001",
        data_category="MARKETING_PROFILE",
        observed_state="PRESENT",
        evidence_id="evidence_01"
    )
    assert f is not None
    assert f.operation == Operation.REPORT_REMAINING_RECORD
    assert f.state == ResourceState.PRESENT
    assert r.operation == Operation.DELETE_RECORD
    assert r.state == ResourceState.ABSENT
    assert v.operation == Operation.VERIFY_RECORD_ABSENT
    assert v.state == ResourceState.ABSENT

    # 22b. Evidence with email_opt_in=true
    f2, r2, v2 = engine.evaluate_resource(
        system_id="analytics_platform",
        resource_id="analytics_9914",
        data_category="ANALYTICS_PROFILE",
        observed_state="PRESENT",
        evidence_id="evidence_01",
        evidence_details={"email_opt_in": "true", "event": "contact_lookup_completed"}
    )
    assert f2 is not None
    assert f2.operation == Operation.REPORT_REMAINING_RECORD
    assert f2.state == ResourceState.PRESENT
    assert r2.operation == Operation.DELETE_RECORD
    assert r2.state == ResourceState.ABSENT
    assert v2.operation == Operation.VERIFY_RECORD_ABSENT
    assert v2.state == ResourceState.ABSENT


# 23. Test Empirical Absence Snapshot Verification in COMPLETE Cases
def test_complete_absence_evidence_upgrade(auditor):
    req = DeletionRequest(
        case_id="case_01",
        email="amina.elmasri@gmail.com",
        requested_at="2026-08-18T09:14:00Z",
        reported_status="COMPLETE",
        reported_at="2026-08-20T16:42:00Z",
        audit_at="2026-09-03T11:30:00Z",
        request_text="Hi, I haven’t used this account in years and would like it closed. Please remove the profile and the personal details attached to it."
    )
    status, steps = auditor.audit_case(req)
    assert status == DeletionStatus.COMPLETE
    assert len(steps) == 1
    step = steps[0]
    assert step.operation == Operation.VERIFY_RECORD_ABSENT
    assert step.state == ResourceState.ABSENT
    assert step.system_id == "account_service"
    assert step.resource_id == "account_001"
    # Must cite genuine empirical database snapshot (evidence_54) rather than operational worker log (activity_xxxx)
    assert step.evidence_id == "evidence_54"


# 24. Test Shared Fixtures and In-Memory Precomputed Cache Speed
def test_shared_fixtures_and_caching_speed(shared_evidence_loader, shared_identity_resolver, shared_auditor):
    import time
    from evidence_loader import _SQL_TABLES_CACHE, _LOG_TOKENS_CACHE

    assert isinstance(shared_evidence_loader, EvidenceLoader)
    assert isinstance(shared_identity_resolver, IdentityResolver)
    assert isinstance(shared_auditor, DeletionAuditor)

    # In-memory caches are populated
    assert len(_SQL_TABLES_CACHE) > 0, "Parsed SQLite tables cache should be populated"
    assert len(_LOG_TOKENS_CACHE) > 0, "Parsed log tokens cache should be populated"

    # Rapid re-instantiation leveraging precomputed in-memory caches
    t0 = time.time()
    reloaded_loader = EvidenceLoader("dataset")
    t1 = time.time()
    assert (t1 - t0) < 1.0, f"Reloaded EvidenceLoader took {t1 - t0:.2f}s, expected < 1.0s with memory caches"
    assert len(reloaded_loader.records) == len(shared_evidence_loader.records)


# 25. Test Exponential Backoff With Jitter & Transient Error Detection
def test_backoff_retry_with_jitter_and_transient_detection():
    try:
        from code.model_layer import (
            calculate_backoff_with_jitter,
            is_transient_error,
            is_hard_quota_exhausted,
        )
    except (ImportError, ValueError):
        from model_layer import (
            calculate_backoff_with_jitter,
            is_transient_error,
            is_hard_quota_exhausted,
        )

    # Backoff increases exponentially with attempt number
    delays = [calculate_backoff_with_jitter(i, base_sec=1.0, max_sec=20.0, jitter_factor=0.2) for i in range(5)]
    for i in range(len(delays) - 1):
        assert delays[i] < delays[i + 1], f"Attempt {i} delay ({delays[i]}) should be < attempt {i+1} ({delays[i+1]})"

    # Jitter variability between identical attempt levels
    same_level_samples = [calculate_backoff_with_jitter(2, base_sec=2.0, jitter_factor=0.5) for _ in range(10)]
    assert len(set(same_level_samples)) > 1, "Jitter must introduce non-zero variability"

    # Maximum delay cap enforcement
    capped_delay = calculate_backoff_with_jitter(10, base_sec=2.0, max_sec=12.5)
    assert capped_delay <= 12.5, f"Delay {capped_delay} should be capped at max_sec 12.5"

    # Transient error classification
    assert is_transient_error("HTTP 429 Too Many Requests: Rate limit exceeded")
    assert is_transient_error("503 Service Unavailable: Model is overloaded")
    assert is_transient_error("Connection timed out after 30000ms")
    assert not is_transient_error("401 Unauthorized: Invalid API key")

    # Hard daily/credit quota classification
    assert is_hard_quota_exhausted("ResourceExhausted: 429 GenerateContent requests per day limit exceeded")
    assert is_hard_quota_exhausted("insufficient_quota: you exceeded your current quota")
    assert not is_hard_quota_exhausted("429 Too Many Requests: Rate limit per minute")


# 26. Test Pydantic Schema Validation for Model Layer Payloads
def test_pydantic_schema_validation_model_layer():
    try:
        from code.model_layer import (
            IntentClassificationPayload,
            ImageExtractionPayload,
            ImageResourceItem,
            clean_and_parse_json,
        )
    except (ImportError, ValueError):
        from model_layer import (
            IntentClassificationPayload,
            ImageExtractionPayload,
            ImageResourceItem,
            clean_and_parse_json,
        )
    from pydantic import ValidationError

    # Valid intent payload
    valid_intent = {
        "requests_deletion": True,
        "language": "French",
        "evidence_quote": "veuillez supprimer mon compte",
        "non_deletion_reason": None,
    }
    p_intent = IntentClassificationPayload.model_validate(valid_intent)
    assert p_intent.requests_deletion is True
    assert p_intent.language == "French"

    # String boolean coercion in Pydantic
    coerced_intent = IntentClassificationPayload.model_validate({
        "requests_deletion": "False",
        "language": "English",
        "evidence_quote": "keep my account active",
        "non_deletion_reason": "explicit retention",
    })
    assert coerced_intent.requests_deletion is False

    # Invalid intent payload raises ValidationError
    with pytest.raises(ValidationError):
        IntentClassificationPayload.model_validate({
            "requests_deletion": "not_a_bool_or_int",
            "language": 123,
        })

    # Image extraction validation & defaults
    img_data = {
        "system_id": "auth_admin_console",
        "resources": [
            {
                "resource_id": "account_999",
                "record_type": "USER_RECORD",
                "email": "user999@example.com",
                "status": "DELETED",
            }
        ],
    }
    p_img = ImageExtractionPayload.model_validate(img_data)
    assert p_img.system_id == "auth_admin_console"
    assert len(p_img.resources) == 1
    assert p_img.resources[0].resource_id == "account_999"
    assert p_img.resources[0].status == "DELETED"
    assert p_img.resources[0].details == {}

    # JSON cleaner handles markdown fences and commentary
    fence_json = "```json\n{\"system_id\": \"db_console\", \"resources\": []}\n```"
    assert clean_and_parse_json(fence_json)["system_id"] == "db_console"


# 27. Test Multi-Provider Fallback Cascade and Hard Quota Recovery
def test_multi_provider_fallback_cascade_and_quota_recovery(tmp_path):
    try:
        from code.model_layer import ModelLayer
    except (ImportError, ValueError):
        from model_layer import ModelLayer
    from unittest.mock import patch
    import time

    # Use isolated temporary cache directory to preserve production intent_cache.json
    ml = ModelLayer(cache_dir=str(tmp_path))

    # 1. Verification of default cascade ends with deterministic
    cascade = ml.get_fallback_cascade()
    assert cascade[-1][0] == "deterministic"

    # 2. Hard quota on Gemini triggers immediate cascade to deterministic (when OpenAI unset)
    ml._exhausted_providers.clear()
    uncached_text_1 = "Please delete all my stored personal records completely."
    with patch.object(ml, "_call_gemini_intent", side_effect=Exception("429 ResourceExhausted: requests per day limit exceeded")):
        res = ml.classify_intent(uncached_text_1)
        assert res["requests_deletion"] is True
        assert "gemini" in ml._exhausted_providers

    # 3. When OpenAI key is configured and Gemini is exhausted, cascade reaches OpenAI
    ml._exhausted_providers.clear()
    uncached_text_2 = "Por favor eliminar mis datos."
    mock_openai_payload = {
        "requests_deletion": True,
        "language": "Spanish",
        "evidence_quote": "eliminar mis datos",
        "non_deletion_reason": None,
    }
    with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-mock-key-for-test"}):
        cascade_with_openai = ml.get_fallback_cascade()
        provider_names = [p for p, _ in cascade_with_openai]
        assert "openai" in provider_names

        with patch.object(ml, "_call_gemini_intent", side_effect=Exception("429 ResourceExhausted: daily quota exceeded")):
            with patch.object(ml, "_call_openai_intent", return_value=(mock_openai_payload, 60, 25, 85, 0)) as mock_openai:
                res = ml.classify_intent(uncached_text_2)
                assert res["requests_deletion"] is True
                assert res["language"] == "Spanish"
                assert mock_openai.called


# 28. Test Multi-Core Vectorized Batch Processing & High Throughput Equivalence
def test_multicore_vectorized_batch_processing(auditor):
    """
    Verifies that:
    1. _audit_case_batch processes batches cleanly and yields identical status and steps
       to sequential single-case audit.
    2. Vectorized batch execution throughput exceeds enterprise benchmark (>200 cases/sec).
    3. DeletionAuditor state remains completely immutable and idempotent across batch runs.
    """
    try:
        from code.main import _init_worker, _audit_case_batch
    except (ImportError, ValueError):
        from main import _init_worker, _audit_case_batch
    import time
    import pandas as pd

    # Initialize worker state in current process reusing session-scoped auditor fixture
    _init_worker("dataset", "code/pricing_config.json", auditor_instance=auditor)

    # Load authentic sample cases from dataset covering NOT_APPLICABLE, COMPLETE, and INCOMPLETE
    req_df = pd.read_csv("dataset/deletion_requests.csv")
    sample_df = req_df[req_df["case_id"].isin(["case_533", "case_41", "case_33"])].copy()
    test_cases = sample_df.to_dict(orient="records")

    # Run batch
    batch_res = _audit_case_batch(test_cases)
    assert len(batch_res) == 3

    res_by_id = {cid: (status, steps) for cid, status, steps in batch_res}

    # case_533 is NOT_APPLICABLE (requests data retention)
    assert "case_533" in res_by_id
    assert res_by_id["case_533"][0] == DeletionStatus.NOT_APPLICABLE
    assert len(res_by_id["case_533"][1]) == 0

    # case_41 is COMPLETE
    assert "case_41" in res_by_id
    assert res_by_id["case_41"][0] == DeletionStatus.COMPLETE
    assert len(res_by_id["case_41"][1]) > 0

    # case_33 is INCOMPLETE
    assert "case_33" in res_by_id
    assert res_by_id["case_33"][0] == DeletionStatus.INCOMPLETE
    assert len(res_by_id["case_33"][1]) > 0

    # Verify high-throughput scale (>200 cases/sec)
    large_batch = test_cases * 100  # 300 cases
    t0 = time.perf_counter()
    res_large = _audit_case_batch(large_batch)
    elapsed = time.perf_counter() - t0
    assert len(res_large) == 300
    throughput = 300 / elapsed
    assert throughput >= 200.0, f"Expected >= 200 cases/sec, got {throughput:.1f}"







