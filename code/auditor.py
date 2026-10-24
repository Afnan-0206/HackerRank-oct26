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

    def is_non_deletion_request(self, request_text: str) -> bool:
        """
        Detect requests that explicitly ask NOT to delete or ask for rectification/access/portability/opt-out
        without account deletion.
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
        ]
        return any(marker in txt for marker in non_del_markers)

    def audit_case(self, req: DeletionRequest) -> Tuple[DeletionStatus, List[Step]]:
        """
        Audit a single deletion request. Returns (status, ordered_steps).
        """
        # Step 1: Check if NOT_APPLICABLE
        if self.is_non_deletion_request(req.request_text):
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

        # 3a. From direct evidence index
        for k in u_keys:
            for rec in self.loader.by_identifier.get(k, []):
                if rec.resource_id:
                    candidate_resources.add(rec.resource_id)

        # 3b. From catalog links
        for k in u_keys:
            if k in self.catalog_links:
                candidate_resources.update(self.catalog_links[k])

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

        # Step 4: Evaluate each candidate resource as-of audit_at
        findings: List[Step] = []
        remediations: List[Step] = []
        verifications: List[Step] = []

        for rid in sorted(candidate_resources):
            # Query all evidence records for this resource at or before audit_at
            ev_list = [r for r in self.loader.by_resource.get(rid, []) if r.file_id.startswith('evidence_') and r.captured_at <= req.audit_at]
            
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
                evidence_id=cited_ev
            )

            if f_step:
                findings.append(f_step)
            if r_step:
                remediations.append(r_step)
            verifications.append(v_step)

        # Step 5: Determine overall status
        if findings:
            status = DeletionStatus.INCOMPLETE
            for r in remediations:
                r.evidence_id = ""
            for v in verifications:
                v.evidence_id = ""
            all_steps = findings + remediations + verifications
        else:
            status = DeletionStatus.COMPLETE
            all_steps = verifications

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
