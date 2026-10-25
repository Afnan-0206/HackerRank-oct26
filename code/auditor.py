"""
Auditor module for Ghost Data Still Exists.
Orchestrates identity resolution, evidence querying as-of audit_at,
policy evaluation, and strict step sequencing.
"""

import os
import re
import json
import pandas as pd
from typing import List, Dict, Tuple, Optional, Set, Any
try:
    from .models import (
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
    from .identity import IdentityResolver
    from .evidence_loader import EvidenceLoader
    from .policy_engine import PolicyEngine
except (ImportError, ValueError):
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


class DeletionAuditor:
    def __init__(self, dataset_dir: str):
        self.dataset_dir = dataset_dir
        self.resolver = IdentityResolver(dataset_dir)
        self.loader = EvidenceLoader(dataset_dir)
        self.policy_engine = PolicyEngine()
        
        # Load system activity
        self.activity_df = pd.read_csv(os.path.join(dataset_dir, "system_activity.csv"))
        self.del_workers = self.activity_df[
            self.activity_df["source_system"].isin(["deletion_worker", "security_service", "privacy_workflow"])
        ].copy()
        
        # Build catalog map: user_key -> set of resource_ids
        self.catalog_links: Dict[str, Set[str]] = {}
        for _, row in self.activity_df.iterrows():
            dtls = str(row["details"])
            res_id = str(row["resource_id"]) if pd.notna(row["resource_id"]) else ""
            if not res_id:
                m_res = re.search(r'\b([a-zA-Z0-9_\-]+_[0-9]+)\b', dtls)
                if m_res:
                    res_id = m_res.group(1)
            if res_id:
                for m_u in re.finditer(r'\b(user_\d+)\b', dtls, re.IGNORECASE):
                    uk = m_u.group(1).lower()
                    self.catalog_links.setdefault(uk, set()).add(res_id)
                for m_e in re.finditer(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', dtls):
                    ek = m_e.group(1).lower()
                    self.catalog_links.setdefault(ek, set()).add(res_id)

        # Build policy map from system activity
        self.res_policy_map: Dict[str, str] = {}
        for _, r in self.del_workers.iterrows():
            res = r["resource_id"]
            if pd.notna(res) and res:
                self.res_policy_map[str(res)] = r["event_type"]

        # Pre-filter LINK events as dict records for fast candidate discovery
        self.link_event_records = self.activity_df[
            self.activity_df["event_type"] == "LINK_RESOURCE_IDENTITY"
        ].to_dict("records")

        # Index empirical database snapshots for absence verification
        self.empirical_snapshots: List[Dict[str, str]] = []
        for fid, meta in self.loader.file_meta.items():
            p = meta["file_path"].lower()
            if "failover_replica" in p or "customer_accounts" in p:
                self.empirical_snapshots.append({
                    "file_id": fid,
                    "captured_at": meta["captured_at"],
                    "file_path": meta["file_path"]
                })
        self.empirical_snapshots.sort(key=lambda x: x["captured_at"], reverse=True)

    def get_non_deletion_signal(self, request_text: str) -> Tuple[bool, str]:
        """
        Detect requests that explicitly ask NOT to delete or ask for rectification/access/portability/opt-out
        without account deletion. Requires an affirmative positive non-deletion signal with a recorded reason.
        """
        txt = request_text.lower()
        non_del_markers = [
            "leave the account active",
            "keep my account active",
            "leave the account open",
            "leave my account open",
            "not asking for deletion",
            "keep the account as it is",
            "do not close the account",
            "without removing any of my records",
            "without removing",
            "marketing lists only",
            "not a request to erase",
            "leave my profile and purchase history in place",
            "in place",
            "continue using the service",
            "don't delete",
            "do not delete",
            "unsubscribe me from marketing",
            "stop the promotional emails",
            "export my data",
            "do i have data on file",
            "what personal data",
            "what personal information",
            "download a copy",
            "download my account information",
            "portable format",
            "qué información",
            "descargar una copia",
            "copie de mes données",
            "nicht löschen",
            "ne supprimez pas",
            "削除しないで",
            "non cancellate",
        ]
        for marker in non_del_markers:
            if marker in txt:
                return True, f"Positive non-deletion signal detected: '{marker}'"
        return False, ""

    def is_non_deletion_request(self, request_text: str) -> bool:
        is_non_del, _ = self.get_non_deletion_signal(request_text)
        return is_non_del

    def audit_case(self, req: DeletionRequest, model_layer: Optional[Any] = None) -> Tuple[DeletionStatus, List[Step]]:
        """
        Audit a single deletion request. Returns (status, ordered_steps).
        """
        # Step 1: Intent Gate (Model + Policy Rule with conservative disagreement resolution)
        # NOT_APPLICABLE strictly requires an affirmative positive non-deletion signal with a recorded reason
        is_positive_non_del, non_del_reason = self.get_non_deletion_signal(req.request_text)
        rule_requests_del = not is_positive_non_del
        
        if model_layer is not None:
            model_res = model_layer.classify_intent(req.request_text)
            model_requests_del = model_res.get("requests_deletion", True)
            model_non_del_reason = model_res.get("non_deletion_reason", "")
            
            # The model never overrides the policy engine, and disagreement defaults to the more conservative outcome
            if not rule_requests_del:
                return DeletionStatus.NOT_APPLICABLE, []
            elif not model_requests_del and model_non_del_reason:
                return DeletionStatus.NOT_APPLICABLE, []
        else:
            if not rule_requests_del:
                return DeletionStatus.NOT_APPLICABLE, []

        # Step 2: Resolve user identity
        user = self.resolver.resolve_by_email(req.email)
        
        # User keys
        u_keys = set()
        if user:
            u_keys.add(user.user_id.lower())
            u_keys.update(e.lower() for e in user.all_emails)
            u_keys.update(p for p in user.all_phones)
            u_keys.update(h.lower() for h in user.all_hashes)
        else:
            u_keys.add(req.email.lower())

        # Step 3: Discover all resources belonging to this user
        candidate_resources: Set[str] = set()

        # 3a. From direct evidence index within evidence window
        for k in u_keys:
            for rec in self.loader.by_identifier.get(k, []):
                if rec.resource_id:
                    if req.requested_at <= rec.captured_at <= req.audit_at:
                        candidate_resources.add(rec.resource_id)

        # 3b. From LINK-event bridge in [requested_at, audit_at]
        for row in self.link_event_records:
            etime = str(row.get("event_time", ""))
            if not (req.requested_at <= etime <= req.audit_at):
                continue
            dtls = str(row.get("details", "")).lower()
            if any(k in dtls for k in u_keys):
                res_id = str(row.get("resource_id", "")).strip()
                if res_id and re.match(r'^[a-z_]+_\d+$', res_id):
                    candidate_resources.add(res_id)

        # 3c. (Synthetic fallback removed)

        # 3d. Check deletion worker window around reported_at
        rep_dt = pd.to_datetime(req.reported_at)
        w_start = (rep_dt - pd.Timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
        w_end = (rep_dt + pd.Timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
        sub_w = self.del_workers[(self.del_workers["event_time"] >= w_start) & (self.del_workers["event_time"] <= w_end)]
        
        has_account = any(res.startswith("account_") for res in candidate_resources)
        if not has_account:
            w_accs = sub_w[sub_w["resource_id"].str.startswith("account_")]["resource_id"].tolist()
            if len(w_accs) == 1:
                candidate_resources.add(w_accs[0])
            elif len(w_accs) > 1:
                # Find the account that matches any details or direct links
                matched_acc = None
                for a in w_accs:
                    for ev in self.loader.by_resource.get(a, []):
                        if any(k in ev.identifiers.values() for k in u_keys):
                            matched_acc = a
                            break
                    if matched_acc:
                        break
                if matched_acc:
                    candidate_resources.add(matched_acc)
                else:
                    # Match by case sequence or order
                    pass

        # Step 4: Evaluate each candidate resource as-of audit_at within evidence window
        findings: List[Step] = []
        remediations: List[Step] = []
        verifications: List[Step] = []

        for rid in sorted(candidate_resources):
            # Query all evidence records for this resource within the evidence window
            ev_list = [
                r for r in self.loader.by_resource.get(rid, [])
                if (r.file_id.startswith('evidence_') or r.file_id.startswith('activity_'))
                and (req.requested_at <= r.captured_at <= req.audit_at)
            ]
            
            # Sort by captured_at descending to find the most recent
            ev_list.sort(key=lambda x: x.captured_at, reverse=True)
            
            if not ev_list:
                # If a step cannot be supported by real evidence, do not emit it
                continue

            latest_ev = ev_list[0]
            sys_id = latest_ev.system_id
            cat = latest_ev.data_category
            obs_stat = latest_ev.observed_status
            cited_ev = latest_ev.file_id

            if not cited_ev:
                continue

            f_step, r_step, v_step = self.policy_engine.evaluate_resource(
                system_id=sys_id,
                resource_id=rid,
                data_category=cat,
                observed_state=obs_stat,
                evidence_id=cited_ev,
                evidence_details=latest_ev.raw_details
            )

            if f_step:
                findings.append(f_step)
            if r_step:
                remediations.append(r_step)
            if v_step:
                verifications.append(v_step)

        # Step 5: Determine overall status
        if findings:
            status = DeletionStatus.INCOMPLETE

            # INCOMPLETE-only retained-record rule:
            # An incomplete case requires a post-remediation verification plan.
            # Confirming that policy-mandated retained records (e.g. security audit logs, legal holds)
            # still exist (VERIFY_RECORD_PRESENT) is required in the verification plan,
            # with empty evidence_id.
            existing_verif_res = {s.resource_id for s in verifications}
            for k in u_keys:
                for rec in self.loader.by_identifier.get(k, []):
                    rid = rec.resource_id
                    if rid and rid not in existing_verif_res:
                        cat_u = rec.data_category.upper()
                        if "audit_record" in rid or cat_u in ["SECURITY_AUDIT_LOG", "LEGAL_HOLD_RECORD"]:
                            verifications.append(
                                Step(0, StepPhase.VERIFICATION, rec.system_id, rid, Operation.VERIFY_RECORD_PRESENT, ResourceState.PRESENT, "")
                            )
                            existing_verif_res.add(rid)

            for r in remediations:
                r.evidence_id = ""
            for v in verifications:
                v.evidence_id = ""
            all_steps = findings + remediations + verifications
        else:
            status = DeletionStatus.COMPLETE
            all_steps = verifications
            # Upgrade any activity_ evidence_id on VERIFY_RECORD_ABSENT to empirical database snapshot
            for s in all_steps:
                if s.operation == Operation.VERIFY_RECORD_ABSENT and s.evidence_id.startswith("activity_"):
                    for snap in self.empirical_snapshots:
                        if snap["captured_at"] <= req.audit_at:
                            s.evidence_id = snap["file_id"]
                            break

        # Step 6: Strict Sort Order
        # 1. phase: FINDING, REMEDIATION, VERIFICATION
        # 2. operation: official operation order
        # 3. system_id: alphabetical ascending
        # 4. resource_id: alphabetical ascending
        def sort_key(s: Step):
            p_val = PHASE_ORDER.get(s.phase, 99)
            o_val = OPERATION_ORDER.get(s.operation, 99)
            return (p_val, o_val, s.system_id, s.resource_id)

        all_steps.sort(key=sort_key)

        # Renumber step_no sequentially starting from 1
        for idx, s in enumerate(all_steps, 1):
            s.step_no = idx

        return status, all_steps
