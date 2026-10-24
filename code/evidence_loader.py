"""
Unified Evidence Loader for Ghost Data Still Exists.
Parses and indexes all evidence files (SQL, JSON, LOG, TXT, PDF, and image records)
as well as system_activity.csv to provide fast, deterministic as-of lookups.
"""

import os
import re
import json
import sqlite3
import pandas as pd
from typing import Dict, List, Optional, Set, Tuple, Any
try:
    from .models import NormalizedEvidence, UserIdentity
except (ImportError, ValueError):
    from models import NormalizedEvidence, UserIdentity


class EvidenceLoader:
    def __init__(self, dataset_dir: str):
        self.dataset_dir = dataset_dir
        self.evidence_files_df = pd.read_csv(os.path.join(dataset_dir, "evidence_files.csv"))
        self.activity_df = pd.read_csv(os.path.join(dataset_dir, "system_activity.csv"))
        
        # File metadata: file_id -> row dict
        self.file_meta: Dict[str, Dict[str, str]] = {}
        for _, row in self.evidence_files_df.iterrows():
            self.file_meta[row["file_id"]] = {
                "file_path": row["file_path"],
                "origin": row["origin"],
                "captured_at": row["captured_at"],
            }
        
        # Ingested evidence records: list of NormalizedEvidence
        self.records: List[NormalizedEvidence] = []
        
        # Index lookups:
        # (user_key, resource_id) -> list of NormalizedEvidence
        self.by_resource: Dict[str, List[NormalizedEvidence]] = {}
        self.by_identifier: Dict[str, List[NormalizedEvidence]] = {}
        
        # Load everything
        self._load_system_activity()
        self._load_all_evidence_files()

    def _add_record(self, record: NormalizedEvidence):
        # Authoritative manifest timestamp from evidence_files.csv overrides any inner timestamps
        if record.file_id in self.file_meta:
            record.captured_at = self.file_meta[record.file_id]["captured_at"]
        self.records.append(record)
        if record.resource_id:
            self.by_resource.setdefault(record.resource_id, []).append(record)
        for k, v in record.identifiers.items():
            if v:
                val_str = str(v).strip().lower()
                self.by_identifier.setdefault(val_str, []).append(record)

    def _load_system_activity(self):
        """Index linking events and activity traces."""
        for _, row in self.activity_df.iterrows():
            act_id = str(row["activity_id"])
            event_type = str(row["event_type"])
            src = str(row["source_system"])
            tgt = str(row["target_system"])
            res = str(row["resource_id"]) if pd.notna(row["resource_id"]) else ""
            dtls = str(row["details"]) if pd.notna(row["details"]) else ""
            time_str = str(row["event_time"])
            res_val = str(row["reported_result"]) if pd.notna(row["reported_result"]) else ""
            
            # Extract any user_id or email in details
            identifiers = {}
            m_user = re.search(r'\b(user_\d+)\b', dtls, re.IGNORECASE)
            if m_user:
                identifiers["user_id"] = m_user.group(1).lower()
            m_email = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', dtls)
            if m_email:
                identifiers["email"] = m_email.group(1).lower()
                
            rec = NormalizedEvidence(
                file_id=act_id,
                captured_at=time_str,
                system_id=tgt,
                resource_id=res,
                data_category="SYSTEM_ACTIVITY",
                observed_status=res_val,
                identifiers=identifiers,
                raw_details={"event_type": event_type, "source_system": src, "details": dtls}
            )
            self._add_record(rec)

    def _load_all_evidence_files(self):
        for file_id, meta in self.file_meta.items():
            rel_path = meta["file_path"]
            full_path = os.path.join(self.dataset_dir, rel_path)
            if not os.path.exists(full_path):
                continue
            captured_at = meta["captured_at"]
            ext = rel_path.split(".")[-1].lower()
            
            if ext == "txt":
                self._parse_txt(file_id, captured_at, full_path)
            elif ext == "json":
                self._parse_json(file_id, captured_at, full_path)
            elif ext == "sql":
                self._parse_sql(file_id, captured_at, full_path)
            elif ext == "log":
                self._parse_log(file_id, captured_at, full_path)
            elif ext == "pdf":
                self._parse_pdf(file_id, captured_at, full_path)

    def _parse_txt(self, file_id: str, captured_at: str, path: str):
        # Format: timestamp | system_id | resource_id | category | status | identifier
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("- "):
                    continue
                parts = [p.strip() for p in line[2:].split("|")]
                if len(parts) >= 6:
                    t_str, sys_id, res_id, cat, stat, ident = parts[:6]
                    identifiers = {}
                    if "@" in ident:
                        identifiers["email"] = ident.lower()
                    elif ident.lower().startswith("user_"):
                        identifiers["user_id"] = ident.lower()
                    
                    rec = NormalizedEvidence(
                        file_id=file_id,
                        captured_at=captured_at,
                        system_id=sys_id,
                        resource_id=res_id,
                        data_category=cat,
                        observed_status=stat,
                        identifiers=identifiers,
                        raw_details={"raw_line": line}
                    )
                    self._add_record(rec)

    def _parse_json(self, file_id: str, captured_at: str, path: str):
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            try:
                data = json.load(f)
            except Exception:
                return

        # Pattern 1: snapshots bundle
        if isinstance(data, dict) and "snapshots" in data:
            for snap in data["snapshots"]:
                store = snap.get("data_store", "")
                collection = snap.get("collection", "")
                sys_id = store or collection
                for r in snap.get("records", []):
                    res_id = r.get("resource_id", "")
                    cat = self._infer_category(collection, res_id)
                    stat = "PRESENT"
                    if "restore_allowed" in r:
                        ra = r["restore_allowed"]
                        stat = "ACTIVE" if (ra is True or ra == 1 or str(ra).lower() == 'true') else "BLOCKED"
                    elif "enabled" in r:
                        en = r["enabled"]
                        stat = "ACTIVE" if (en is True or en == 1 or str(en).lower() == 'true') else "DISABLED"
                    elif "account_status" in r and r["account_status"]:
                        stat = str(r["account_status"])
                    elif "status" in r and r["status"]:
                        stat = str(r["status"])

                    # Support message text check
                    if r.get("message_text") == "[REDACTED]":
                        stat = "REDACTED"
                    
                    identifiers = {}
                    for k in ["customer_reference", "user_id", "subject_reference"]:
                        if r.get(k):
                            identifiers["user_id"] = str(r[k]).lower()
                    for k in ["email", "owner_email", "requester_email"]:
                        if r.get(k):
                            identifiers["email"] = str(r[k]).lower()
                    for k in ["phone", "owner_phone", "requester_phone"]:
                        if r.get(k):
                            identifiers["phone"] = str(r[k])
                    if r.get("email_hash"):
                        identifiers["email_hash"] = str(r["email_hash"])
                    if r.get("full_name") or r.get("name") or r.get("requester_name"):
                        identifiers["name"] = str(r.get("full_name") or r.get("name") or r.get("requester_name"))

                    # Determine if redacted or anonymized
                    has_direct_pii = bool(r.get("email") or r.get("phone") or r.get("email_hash") or r.get("name") or r.get("full_name"))
                    if "tombstone" in res_id:
                        if not has_direct_pii:
                            stat = "REDACTED"
                        else:
                            stat = "PRESENT"
                    elif "analytics" in res_id:
                        if not has_direct_pii:
                            stat = "ANONYMIZED"
                        else:
                            stat = "PRESENT"

                    rec = NormalizedEvidence(
                        file_id=file_id,
                        captured_at=captured_at,
                        system_id=self._normalize_system(sys_id, res_id),
                        resource_id=res_id,
                        data_category=cat,
                        observed_status=stat,
                        identifiers=identifiers,
                        raw_details=r
                    )
                    self._add_record(rec)

        # Pattern 2: service_resource_export
        elif isinstance(data, dict) and "service_exports" in data:
            exp_time = data.get("exported_at", captured_at)
            for svc_name, content in data["service_exports"].items():
                sys_id = svc_name
                # Look at all lists in content
                for subk, items in content.items():
                    if isinstance(items, list):
                        for it in items:
                            res_id = it.get("resource_id", "")
                            cat = self._infer_category(subk, res_id)
                            
                            # Check status
                            stat = "PRESENT"
                            if it.get("status_code") == 404 or it.get("profile") is None and svc_name == "account_service":
                                stat = "NOT_FOUND"
                            elif svc_name == "analytics_platform":
                                if it.get("email") is None and it.get("customer_reference") is None and it.get("phone") is None:
                                    stat = "ANONYMIZED"
                            elif svc_name == "backup_service":
                                if it.get("restore_allowed") is False:
                                    stat = "BLOCKED"
                                else:
                                    stat = "ACTIVE"
                            elif svc_name == "integration_service":
                                if it.get("enabled") is False or it.get("last_delivery_status") == "stopped":
                                    stat = "DISABLED"
                                else:
                                    stat = "ACTIVE"
                            elif svc_name == "search_service":
                                hits = it.get("hits", {})
                                if isinstance(hits, dict) and hits.get("total", 0) > 0:
                                    stat = "PRESENT"
                                else:
                                    stat = "NOT_FOUND"
                            elif svc_name == "support_platform":
                                if it.get("requester_email") is None and it.get("requester_name") is None:
                                    stat = "REDACTED"
                            elif svc_name == "security_service":
                                stat = "PRESENT"

                            identifiers = {}
                            for k in ["customer_reference", "user_id", "subject_reference"]:
                                if it.get(k):
                                    identifiers["user_id"] = str(it[k]).lower()
                            for k in ["email", "owner_email", "requester_email", "customer_email"]:
                                if it.get(k):
                                    identifiers["email"] = str(it[k]).lower()
                            if "query" in it and "@" in str(it["query"]):
                                identifiers["email"] = str(it["query"]).lower()

                            rec = NormalizedEvidence(
                                file_id=file_id,
                                captured_at=captured_at,
                                system_id=sys_id,
                                resource_id=res_id,
                                data_category=cat,
                                observed_status=stat,
                                identifiers=identifiers,
                                raw_details=it
                            )
                            self._add_record(rec)

        # Pattern 3: direct records list
        elif isinstance(data, dict) and "records" in data:
            rec_time = data.get("captured_at") or data.get("exported_at", captured_at)
            origin = data.get("source") or data.get("origin") or data.get("collection", "")
            for it in data.get("records", []):
                res_id = it.get("resource_id", "")
                cat = self._infer_category(origin, res_id)
                sys_id = self._normalize_system(origin, res_id)
                stat = it.get("result") or it.get("status") or "PRESENT"

                identifiers = {}
                for k in ["customer_reference", "user_id", "subject_reference"]:
                    if it.get(k):
                        val = str(it[k]).strip()
                        if "@" in val:
                            identifiers["email"] = val.lower()
                        else:
                            identifiers["user_id"] = val.lower()
                for k in ["email", "owner_email", "requester_email"]:
                    if it.get(k):
                        identifiers["email"] = str(it[k]).lower()

                # Analytics or export checks
                has_direct_pii = bool(it.get("email") or it.get("phone") or it.get("email_hash"))
                if "analytics" in res_id:
                    stat = "ANONYMIZED" if not has_direct_pii else "PRESENT"
                elif "export" in res_id:
                    stat = "PRESENT" if (it.get("owner_email") or it.get("email")) else "NOT_FOUND"

                rec = NormalizedEvidence(
                    file_id=file_id,
                    captured_at=captured_at,
                    system_id=sys_id,
                    resource_id=res_id,
                    data_category=cat,
                    observed_status=stat,
                    identifiers=identifiers,
                    raw_details=it
                )
                self._add_record(rec)

    def _parse_sql(self, file_id: str, captured_at: str, path: str):
        """Parse SQLite snapshot tables."""
        try:
            conn = sqlite3.connect(":memory:")
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                sql_script = f.read()
            conn.executescript(sql_script)
            cursor = conn.cursor()
            
            # List tables
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = [r[0] for r in cursor.fetchall()]

            for tname in tables:
                cursor.execute(f"PRAGMA table_info({tname});")
                cols = [c[1] for c in cursor.fetchall()]
                cursor.execute(f"SELECT * FROM {tname};")
                rows = cursor.fetchall()
                
                for row_data in rows:
                    rdict = dict(zip(cols, row_data))
                    res_id = str(rdict.get("resource_id", ""))
                    if not res_id:
                        continue

                    # If record_json is present, parse it and merge fields
                    rec_json_data = {}
                    if "record_json" in rdict and rdict["record_json"]:
                        try:
                            rec_json_data = json.loads(str(rdict["record_json"]))
                        except Exception:
                            pass

                    origin_system = str(rdict.get("origin_system", ""))
                    collection_name = str(rdict.get("collection_name", ""))
                    cat_ctx = collection_name or origin_system or tname
                    cat = self._infer_category(cat_ctx, res_id)
                    sys_id = self._normalize_system(cat_ctx, res_id)
                    
                    # Status determination
                    stat = "PRESENT"
                    # Check in rdict or rec_json_data
                    merged_dict = {**rdict, **rec_json_data}
                    for nested_k in ["document", "profile", "record", "ticket", "attributes"]:
                        if nested_k in rec_json_data and isinstance(rec_json_data[nested_k], dict):
                            for nk, nv in rec_json_data[nested_k].items():
                                if nk not in merged_dict or not merged_dict[nk]:
                                    merged_dict[nk] = nv
                    
                    if "restore_allowed" in merged_dict:
                        ra = merged_dict["restore_allowed"]
                        stat = "ACTIVE" if (ra == 1 or ra is True or str(ra).lower() == 'true') else "BLOCKED"
                    elif "enabled" in merged_dict:
                        en = merged_dict["enabled"]
                        stat = "ACTIVE" if (en == 1 or en is True or str(en).lower() == 'true') else "DISABLED"
                    elif "account_status" in merged_dict:
                        stat = str(merged_dict["account_status"])

                    has_direct_pii = bool(
                        merged_dict.get("email") or 
                        merged_dict.get("phone") or 
                        merged_dict.get("email_hash") or 
                        merged_dict.get("full_name") or 
                        merged_dict.get("display_name")
                    )
                    
                    if "analytics" in res_id:
                        stat = "ANONYMIZED" if not has_direct_pii else "PRESENT"
                    elif "tombstone" in res_id:
                        stat = "REDACTED" if not has_direct_pii else "PRESENT"
                    elif "support" in res_id:
                        msg_text = str(merged_dict.get("message_text", "")).strip()
                        if msg_text == "[REDACTED]" and not has_direct_pii:
                            stat = "REDACTED"

                    identifiers = {}
                    for k in ["customer_reference", "user_id", "subject_reference", "subject_selector"]:
                        if merged_dict.get(k):
                            identifiers["user_id"] = str(merged_dict[k]).lower()
                    for k in ["email", "customer_email", "owner_email", "requester_email"]:
                        if merged_dict.get(k):
                            identifiers["email"] = str(merged_dict[k]).lower()
                    for k in ["phone", "owner_phone", "requester_phone"]:
                        if merged_dict.get(k):
                            identifiers["phone"] = str(merged_dict[k])
                    if merged_dict.get("email_hash"):
                        identifiers["email_hash"] = str(merged_dict["email_hash"])
                    if merged_dict.get("full_name") or merged_dict.get("display_name") or merged_dict.get("requester_name"):
                        identifiers["name"] = str(merged_dict.get("full_name") or merged_dict.get("display_name") or merged_dict.get("requester_name"))

                    rec = NormalizedEvidence(
                        file_id=file_id,
                        captured_at=captured_at,
                        system_id=sys_id,
                        resource_id=res_id,
                        data_category=cat,
                        observed_status=stat,
                        identifiers=identifiers,
                        raw_details=merged_dict
                    )
                    self._add_record(rec)
            conn.close()
        except Exception:
            pass

    def _parse_log(self, file_id: str, captured_at: str, path: str):
        """Parse structured and unstructured service logs with trace correlation."""
        traces = {}
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                trace_id = None
                m_trace = re.search(r'trace_id["\s:=]+([a-zA-Z0-9_\-]+)', line)
                if m_trace:
                    trace_id = m_trace.group(1)
                else:
                    m_req = re.search(r'requestId["\s:=]+([a-zA-Z0-9_\-]+)', line)
                    if m_req:
                        trace_id = m_req.group(1)

                # Extract line timestamp if available
                m_time = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)', line)
                log_time = m_time.group(1) if m_time else captured_at

                # Extract service
                m_svc = re.search(r'(?:service|logger|function)["\s:=]+["\']?([a-zA-Z0-9_]+)', line)
                svc = m_svc.group(1) if m_svc else ""

                # Extract keys
                keys = set()
                for m_k in re.finditer(r'(?:lookup_key|subject_key|subject_reference|subject_ref|correlation_key|match_key|owner_key|customer_reference|lookup_value|customer_email)["\s:=]+["\']?([^"\'\s,>]+)', line):
                    val = m_k.group(1).strip()
                    if val and val.lower() != 'null':
                        keys.add(val)

                # Extract resource_id
                m_res = re.search(r'resource_id["\s:=]+["\']?([a-zA-Z0-9_\-]+)', line)
                res_id = m_res.group(1) if m_res else ""

                if trace_id:
                    traces.setdefault(trace_id, {"keys": set(), "events": []})
                    traces[trace_id]["keys"].update(keys)
                    traces[trace_id]["events"].append((log_time, svc, res_id, line))
                elif keys or res_id:
                    # Single un-traced line
                    self._process_log_event(file_id, log_time, svc, res_id, keys, line)

        # Now process grouped traces
        for trace_id, tdata in traces.items():
            all_keys = tdata["keys"]
            for log_time, svc, res_id, raw_line in tdata["events"]:
                if res_id:
                    if "_started" in raw_line and ("lookup_started" in raw_line or "query_started" in raw_line):
                        continue
                    self._process_log_event(file_id, log_time, svc, res_id, all_keys, raw_line)

    def _process_log_event(self, file_id: str, log_time: str, svc: str, res_id: str, keys: Set[str], line: str):
        if not res_id:
            return
            
        line_l = line.lower()
        m_sys = re.search(r'system["\s:=]+["\']?([a-zA-Z0-9_\-]+)', line)
        sys_hint = m_sys.group(1) if m_sys else svc
        
        m_cat = re.search(r'record_type["\s:=]+["\']?([a-zA-Z0-9_\-]+)', line)
        if m_cat:
            cat = m_cat.group(1)
        elif "contact_index" in line_l or "legacy_contact" in line_l:
            cat = "SEARCH_INDEX_ENTRY"
        else:
            cat = self._infer_category(sys_hint, res_id)
        sys_id = self._normalize_system(sys_hint, res_id)
        
        line_l = line.lower()
        stat = "PRESENT"
        if "rows=\"0\"" in line_l or "rows=0" in line_l or "http_status=\"404\"" in line_l or "http_status=404" in line_l or "result=\"miss\"" in line_l or "result=\"not_found\"" in line_l or "result=not_found" in line_l:
            stat = "NOT_FOUND"
        elif "result=\"active\"" in line_l or "result=active" in line_l:
            stat = "ACTIVE"
        elif "result=\"disabled\"" in line_l or "result=disabled" in line_l or "status=\"stopped\"" in line_l or "enabled=\"false\"" in line_l or "enabled=false" in line_l:
            stat = "DISABLED"
        elif "result=\"blocked\"" in line_l or "result=blocked" in line_l or "deletion_block=\"true\"" in line_l or "restore_allowed=\"false\"" in line_l or "restore_allowed=false" in line_l:
            stat = "BLOCKED"
        elif "result=\"anonymized\"" in line_l or "result=anonymized" in line_l or "direct_identifiers=\"0\"" in line_l or "anonymized" in line_l:
            stat = "ANONYMIZED"
        elif "result=\"redacted\"" in line_l or "result=redacted" in line_l or "message_text=\"[redacted]\"" in line_l or "redacted" in line_l:
            stat = "REDACTED"
        elif "deletion_block=\"false\"" in line_l and "eligible=\"true\"" in line_l:
            stat = "ACTIVE" # restore risk active

        identifiers = {}
        for k in keys:
            k_str = str(k).strip()
            if len(k_str) == 64 and re.match(r'^[a-fA-F0-9]{64}$', k_str):
                identifiers["email_hash"] = k_str.lower()
            elif k_str.startswith('+') or (len(k_str) >= 7 and re.match(r'^\+?[\d\s\-()]{7,}$', k_str)):
                identifiers["phone"] = k_str
            elif "@" in k_str:
                identifiers["email"] = k_str.lower()
            elif k_str.lower().startswith("user_"):
                identifiers["user_id"] = k_str.lower()

        rec = NormalizedEvidence(
            file_id=file_id,
            captured_at=log_time,
            system_id=sys_id,
            resource_id=res_id,
            data_category=cat,
            observed_status=stat,
            identifiers=identifiers,
            raw_details={"line": line}
        )
        self._add_record(rec)

    def _infer_category(self, context: str, resource_id: str) -> str:
        ctx = (context + "_" + resource_id).lower()
        if "tombstone" in ctx:
            return "ACCOUNT_TOMBSTONE"
        if "account" in ctx or "profile" in ctx:
            return "USER_PROFILE"
        if "attachment" in ctx or "object_" in ctx:
            return "SUPPORT_ATTACHMENT"
        if "search" in ctx:
            return "SEARCH_INDEX_ENTRY"
        if "analytics" in ctx or "telemetry" in ctx:
            return "ANALYTICS_PROFILE"
        if "support" in ctx or "ticket" in ctx or "message" in ctx:
            return "SUPPORT_MESSAGE"
        if "data_flow" in ctx or "connector" in ctx:
            return "DATA_FLOW"
        if "backup" in ctx or "archive" in ctx:
            return "BACKUP_ARCHIVE"
        if "export" in ctx:
            return "USER_EXPORT"
        if "marketing" in ctx:
            return "MARKETING_PROFILE"
        if "audit" in ctx or "security" in ctx:
            return "SECURITY_AUDIT_LOG"
        return "UNKNOWN"

    def _normalize_system(self, system_hint: str, resource_id: str) -> str:
        hint = (system_hint + "_" + resource_id).lower()
        if "account" in hint or "profile" in hint:
            return "account_service"
        if "search" in hint:
            return "search_service"
        if "analytics" in hint:
            return "analytics_platform"
        if "support" in hint:
            return "support_platform"
        if "connector" in hint or "data_flow" in hint or "bridge" in hint:
            return "integration_service"
        if "backup" in hint or "recovery" in hint:
            return "backup_service"
        if "export" in hint:
            return "export_service"
        if "security" in hint or "audit" in hint:
            return "security_service"
        if "tombstone" in hint or "identity" in hint:
            return "identity_service"
        if "marketing" in hint:
            return "marketing_platform"
        if "object" in hint:
            return "object_storage"
        return system_hint.lower()

    def _parse_pdf(self, file_id: str, captured_at: str, path: str):
        try:
            import pymupdf
            doc = pymupdf.open(path)
            text = ""
            for page in doc:
                text += page.get_text() + "\n"
            doc.close()

            # Extract resources: support_XXX, CS-XXXX, export_XXXX, audit_record_XXXX
            raw_matches = set(re.findall(r'\b([a-zA-Z0-9_\-]+_[0-9]+)\b', text))
            res_matches = {r for r in raw_matches if not r.lower().startswith('user_')}
            emails = set(re.findall(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', text))
            users = set(re.findall(r'\b(user_\d+)\b', text, re.IGNORECASE))
            phones = set(re.findall(r'(\+\d[\d\s\-()]{7,}\d)', text))

            # Special case for support attachment: delivery_correction_attachment.pdf
            if "delivery_correction" in path or "attachment" in path:
                res_matches.add("support_001")

            for rid in res_matches:
                cat = self._infer_category(path, rid)
                sys_id = self._normalize_system(path, rid)

                identifiers = {}
                if emails:
                    identifiers["email"] = list(emails)[0].lower()
                if users:
                    identifiers["user_id"] = list(users)[0].lower()
                if phones:
                    identifiers["phone"] = list(phones)[0]

                stat = "PRESENT"
                rec = NormalizedEvidence(
                    file_id=file_id,
                    captured_at=captured_at,
                    system_id=sys_id,
                    resource_id=rid,
                    data_category=cat,
                    observed_status=stat,
                    identifiers=identifiers,
                    raw_details={"text_snippet": text[:200]}
                )
                self._add_record(rec)
        except Exception:
            pass

