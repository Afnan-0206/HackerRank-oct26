"""
Unified Evidence Loader for Ghost Data Still Exists.
Parses and indexes all evidence files (SQL, JSON, LOG, TXT, PDF, and image records)
as well as system_activity.csv to provide fast, deterministic as-of lookups.
"""

import os
import re
import json
import sqlite3
import logging
import pandas as pd
from typing import Dict, List, Optional, Set, Tuple, Any

logger = logging.getLogger(__name__)

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

            data_cat = "SYSTEM_ACTIVITY"
            obs_stat = res_val
            if event_type == "LINK_RESOURCE_IDENTITY":
                # Check if a screenshot image exists in file_meta within 15 minutes
                has_image = False
                e_dt = pd.to_datetime(time_str)
                for f_meta in self.file_meta.values():
                    if "evidence/images/" in f_meta.get("file_path", ""):
                        c_dt = pd.to_datetime(f_meta.get("captured_at", ""))
                        if 0 <= (e_dt - c_dt).total_seconds() <= 900:
                            has_image = True
                            break
                if has_image:
                    continue  # Image parser will create the authoritative screenshot record

                res_lower = res.lower()
                if "export" in res_lower or tgt == "export_service":
                    data_cat = "USER_EXPORT"
                elif "account" in res_lower or tgt == "account_service":
                    data_cat = "USER_PROFILE"
                elif "search" in res_lower or tgt == "search_service":
                    data_cat = "SEARCH_INDEX_ENTRY"
                elif "data_flow" in res_lower or tgt == "integration_service":
                    data_cat = "DATA_FLOW"
                elif "analytics" in res_lower or tgt == "analytics_platform":
                    data_cat = "ANALYTICS_PROFILE"
                elif "support" in res_lower or tgt == "support_platform":
                    data_cat = "SUPPORT_MESSAGE"
                elif "backup" in res_lower or tgt == "backup_service":
                    data_cat = "BACKUP_ARCHIVE"
                elif "audit" in res_lower or tgt == "security_service":
                    data_cat = "SECURITY_AUDIT_LOG"
                elif "tombstone" in res_lower or tgt == "identity_service":
                    data_cat = "ACCOUNT_TOMBSTONE"
                elif "object" in res_lower or tgt == "object_storage":
                    data_cat = "SUPPORT_ATTACHMENT"
                elif "marketing" in res_lower or tgt == "marketing_platform":
                    data_cat = "MARKETING_PROFILE"
                else:
                    data_cat = self._infer_category(tgt, res)
                obs_stat = "PRESENT"
                
            rec = NormalizedEvidence(
                file_id=act_id,
                captured_at=time_str,
                system_id=tgt,
                resource_id=res,
                data_category=data_cat,
                observed_status=obs_stat,
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
            
            try:
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
                elif ext == "png":
                    self._parse_image(file_id, captured_at, full_path)
            except Exception as e:
                logger.warning(f"Failed to parse evidence file {file_id} ({rel_path}): {e}. Skipping file.")

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

    def _clean_token(self, val: Optional[str]) -> Optional[str]:
        if val is None:
            return None
        s = str(val).strip()
        s = s.strip('"\'\\')
        s = s.replace('\\"', '').replace('\\', '')
        return s if s else None

    def _tokenize_log_line(self, line: str) -> Optional[Dict[str, Any]]:
        line = line.strip()
        if not line:
            return None

        res: Dict[str, Any] = {
            "timestamp": None,
            "level": None,
            "service": None,
            "trace_id": None,
            "event": None,
            "resource_id": None,
            "subject_keys": set(),
            "result": None,
            "http_status": None,
            "rows": None,
            "file": None,
            "raw_attributes": {}
        }

        m_ts = re.match(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)', line)
        if m_ts:
            res["timestamp"] = m_ts.group(1)
            rest = line[m_ts.end():].strip()
        else:
            rest = line

        # Case A: JSON structured log
        if rest.startswith("{") and rest.endswith("}"):
            try:
                d = json.loads(rest)
                res["level"] = self._clean_token(d.get("severity") or d.get("level"))
                res["service"] = self._clean_token(d.get("service") or d.get("logger"))
                res["trace_id"] = self._clean_token(d.get("trace_id") or d.get("requestId"))
                res["event"] = self._clean_token(d.get("event"))
                res["resource_id"] = self._clean_token(d.get("resource_id"))
                res["result"] = self._clean_token(d.get("result"))
                res["http_status"] = self._clean_token(str(d.get("http_status"))) if d.get("http_status") is not None else None
                res["rows"] = int(d.get("rows")) if d.get("rows") is not None and str(d.get("rows")).isdigit() else None
                res["file"] = self._clean_token(d.get("file"))

                for k, v in d.items():
                    if v is not None:
                        res["raw_attributes"][k] = self._clean_token(str(v))
                    if k in ("lookup_key", "subject_key", "subject_ref", "match_key", "matched_value",
                            "subject_reference", "customer_email", "owner_key", "scan_key", "correlation_key"):
                        c_val = self._clean_token(str(v))
                        if c_val and c_val.lower() != "null":
                            res["subject_keys"].add(c_val)
                return res
            except Exception:
                pass

        # Case B: Pipe-delimited
        if rest.startswith("|") or " | " in rest:
            parts = [p.strip() for p in rest.split("|") if p.strip()]
            p_offset = 0
            if not res["timestamp"] and len(parts) > 0 and re.match(r'^\d{4}-\d{2}-\d{2}', parts[0]):
                res["timestamp"] = parts[0]
                p_offset = 1

            if len(parts) >= p_offset + 4:
                res["level"] = self._clean_token(parts[p_offset].upper())
                res["service"] = self._clean_token(parts[p_offset + 1])
                res["trace_id"] = self._clean_token(parts[p_offset + 2])
                res["event"] = self._clean_token(parts[p_offset + 3])
                kv_text = " ".join(parts[p_offset + 4:])
            else:
                kv_text = " ".join(parts)
        # Case C: Syslog RFC5424
        elif rest.startswith("<") or re.match(r'^<\d+>\d\s+', rest):
            m_syslog = re.match(r'^<(\d+)>(\d+)\s+([^\s]+)\s+([^\s]+)\s+([^\s]+)\s+-\s+-\s+(.*)$', rest)
            if m_syslog:
                res["service"] = self._clean_token(m_syslog.group(4))
                res["trace_id"] = self._clean_token(m_syslog.group(5))
                kv_text = m_syslog.group(6)
            else:
                kv_text = rest
        # Case D: Bracket prefix
        elif rest.startswith("["):
            m_brack = re.match(r'^\[([A-Z]+)\]\s+\[([a-zA-Z0-9_\-]+)\]\s+\[([a-zA-Z0-9_\-]+)\]\s+([a-zA-Z0-9_\-]+)(?::\s*|\s+)(.*)$', rest)
            if m_brack:
                res["level"] = self._clean_token(m_brack.group(1).upper())
                res["service"] = self._clean_token(m_brack.group(2))
                res["trace_id"] = self._clean_token(m_brack.group(3))
                res["event"] = self._clean_token(m_brack.group(4))
                kv_text = m_brack.group(5)
            else:
                kv_text = rest
        else:
            kv_text = rest

        # Unpack nested message="..." if present
        m_msg = re.search(r'message="((?:\\.|[^"\\])*)"', kv_text)
        if m_msg:
            inner_msg = m_msg.group(1).replace(r'\"', '"')
            kv_text = kv_text + " " + inner_msg

        for m in re.finditer(r'([a-zA-Z0-9_\-]+)\s*[:=]\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s,>|]+))', kv_text):
            k = m.group(1).lower()
            v = m.group(2) if m.group(2) is not None else (m.group(3) if m.group(3) is not None else m.group(4))
            if v is None:
                continue
            v_clean = self._clean_token(v)
            if not v_clean:
                continue
            res["raw_attributes"][k] = v_clean

            if k in ("level", "severity") and not res["level"]:
                res["level"] = v_clean.upper()
            elif k in ("service", "logger", "function") and not res["service"]:
                res["service"] = v_clean
            elif k in ("trace_id", "requestid") and not res["trace_id"]:
                res["trace_id"] = v_clean
            elif k == "event" and not res["event"]:
                res["event"] = v_clean
            elif k == "resource_id" and not res["resource_id"]:
                res["resource_id"] = v_clean
            elif k == "result" and not res["result"]:
                res["result"] = v_clean
            elif k == "http_status" and not res["http_status"]:
                res["http_status"] = v_clean
            elif k == "rows" and res["rows"] is None:
                if v_clean.isdigit():
                    res["rows"] = int(v_clean)
            elif k == "file" and not res["file"]:
                res["file"] = v_clean

            if k in ("lookup_key", "subject_key", "subject_ref", "match_key", "matched_value",
                    "subject_reference", "customer_email", "owner_key", "scan_key", "correlation_key"):
                if v_clean.lower() != "null":
                    res["subject_keys"].add(v_clean)

        if not res["event"] and "event" in res["raw_attributes"]:
            res["event"] = res["raw_attributes"]["event"]

        return res

    def _parse_log(self, file_id: str, captured_at: str, path: str):
        """Parse structured and unstructured service logs with trace correlation."""
        skip_events = {
            "profile_lookup_started", "index_query_started", "audit_record_query_started",
            "audit_record_read_started", "analytics_profile_scan_started", "analytics_profile_read_started",
            "message_scan_started", "attachment_scan_started", "connector_config_read_started",
            "restore_policy_check_started", "restore_policy_read_started", "export_started",
            "export_metadata_read_started", "export_download_check_started", "connection_acquired",
            "cache_lookup", "dependency_retry", "dependency_recovered", "cache_refresh",
            "telemetry_flushed", "batch_started", "timer_tick", "disk_latency", "consumer_lag",
            "pool_sample", "checkpoint_saved", "lease_extended", "lease_renewed", "slow_call_rate",
            "certificate_checked", "connection_pool_pressure", "properties_reloaded",
            "delivery_abandoned", "contact_query_started", "retained_order_query_started",
            "support_ticket_read_started"
        }

        traces: Dict[str, Dict[str, Any]] = {}
        untraced: List[Dict[str, Any]] = []

        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                t = self._tokenize_log_line(line)
                if not t:
                    continue
                tr = t["trace_id"]
                if tr:
                    traces.setdefault(tr, {"keys": set(), "records": []})
                    traces[tr]["keys"].update(t["subject_keys"])
                    traces[tr]["records"].append(t)
                else:
                    untraced.append(t)

        def emit_log_record(t: Dict[str, Any], keys: Set[str]):
            ev = t["event"]
            res_id = t["resource_id"]
            lvl = t["level"] or "INFO"
            attrs = t["raw_attributes"]

            # Never skip WARN or ERROR carrying resource_id or matched_value
            is_warn_err = lvl in ("WARN", "WARNING", "ERROR")
            if not res_id:
                return
            if ev in skip_events and not is_warn_err:
                return

            m_rid = re.match(r'^[a-z_]+_\d+$', res_id)
            if not m_rid:
                return

            svc = attrs.get("system") or t["service"] or ""
            # Category mapping per deletion_rules.csv
            if attrs.get("record_type"):
                cat = attrs.get("record_type")
            elif ev == "attachment_pii_scan":
                cat = "SUPPORT_ATTACHMENT"
            elif ev in ("message_pii_scan", "message_read"):
                cat = "SUPPORT_MESSAGE"
            elif ev in ("analytics_identity_scan", "analytics_profile_read", "analytics_profile_read_completed"):
                cat = "ANALYTICS_PROFILE"
            elif ev in ("connector_heartbeat", "connector_config_loaded"):
                cat = "DATA_FLOW"
            elif ev in ("restore_candidate_evaluated", "restore_authorization_completed", "restore_policy_read_completed"):
                cat = "BACKUP_ARCHIVE"
            elif ev == "audit_record_read_completed":
                cat = "SECURITY_AUDIT_LOG"
            elif ev == "profile_lookup_completed":
                cat = "USER_PROFILE"
            elif ev in ("index_query_completed", "contact_query_completed", "contact_lookup_completed", "deletion_marker_check"):
                cat = "SEARCH_INDEX_ENTRY"
            elif ev in ("export_download_check_completed", "export_metadata_read_completed"):
                cat = "USER_EXPORT"
            else:
                cat = self._infer_category(svc, res_id)

            sys_id = self._normalize_system(svc, res_id)

            # Observed status
            stat = "PRESENT"
            res_val = attrs.get("result", "").lower()
            if res_val in ("not_found", "miss"):
                stat = "NOT_FOUND"
            elif res_val in ("found", "hit", "present"):
                stat = "PRESENT"
            elif res_val == "anonymized":
                stat = "ANONYMIZED"
            elif res_val == "redacted":
                stat = "REDACTED"
            elif res_val == "active":
                stat = "ACTIVE"
            elif res_val in ("disabled", "stopped"):
                stat = "DISABLED"
            elif res_val in ("blocked",):
                stat = "BLOCKED"

            if ev == "message_pii_scan":
                if is_warn_err or attrs.get("matched_value"):
                    stat = "PRESENT"
                else:
                    stat = "REDACTED"
            elif ev == "attachment_pii_scan":
                if is_warn_err or attrs.get("matched_value"):
                    stat = "PRESENT"
                else:
                    stat = "NOT_FOUND"
            elif ev == "analytics_identity_scan":
                direct_id = attrs.get("direct_identifiers")
                if direct_id is not None and str(direct_id) == "0":
                    stat = "ANONYMIZED"
                elif is_warn_err or (direct_id is not None and str(direct_id) != "0") or attrs.get("matched_value"):
                    stat = "PRESENT"
                else:
                    stat = "ANONYMIZED"
            elif ev == "connector_heartbeat":
                en = attrs.get("enabled", "").lower()
                if en == "true" or is_warn_err:
                    stat = "ACTIVE"
                else:
                    stat = "DISABLED"
            elif ev == "restore_candidate_evaluated":
                del_block = attrs.get("deletion_block", "").lower()
                elig = attrs.get("eligible", "").lower()
                if del_block == "false" and elig == "true":
                    stat = "ACTIVE"
                else:
                    stat = "BLOCKED"
            elif ev in ("index_query_completed", "contact_query_completed", "contact_lookup_completed"):
                res_val = attrs.get("result", "").lower()
                rows_val = attrs.get("rows")
                is_hit = (res_val == "hit") or (rows_val is not None and str(rows_val).isdigit() and int(rows_val) > 0)
                if is_hit:
                    stat = "PRESENT"
                else:
                    stat = "NOT_FOUND"
            elif ev == "profile_lookup_completed":
                h_stat = str(attrs.get("http_status", ""))
                rows_val = attrs.get("rows")
                is_miss = (h_stat == "404") or (rows_val is not None and str(rows_val).isdigit() and int(rows_val) == 0) or attrs.get("result") == "not_found"
                if is_miss:
                    stat = "NOT_FOUND"
                else:
                    stat = "PRESENT"
            elif ev == "audit_record_read_completed":
                stat = "PRESENT"
            elif ev == "deletion_marker_check":
                mf = attrs.get("marker_found", "").lower()
                if mf == "false" or is_warn_err:
                    stat = "PRESENT"
                else:
                    stat = "NOT_FOUND"
            elif ev == "message_read":
                if attrs.get("message_text") == "[REDACTED]":
                    stat = "REDACTED"
                else:
                    stat = "PRESENT"

            # Build identifiers
            identifiers = {}
            for k in keys:
                k_clean = self._clean_token(k)
                if not k_clean:
                    continue
                if len(k_clean) == 64 and re.match(r'^[a-fA-F0-9]{64}$', k_clean):
                    identifiers["email_hash"] = k_clean.lower()
                elif k_clean.startswith("+") or (len(k_clean) >= 7 and re.match(r'^\+?[\d\s\-()]{7,}$', k_clean)):
                    identifiers["phone"] = k_clean
                elif "@" in k_clean:
                    identifiers["email"] = k_clean.lower()
                elif k_clean.lower().startswith("user_"):
                    identifiers["user_id"] = k_clean.lower()

            rec = NormalizedEvidence(
                file_id=file_id,
                captured_at=captured_at,
                system_id=sys_id,
                resource_id=res_id,
                data_category=cat,
                observed_status=stat,
                identifiers=identifiers,
                raw_details=attrs
            )
            self._add_record(rec)

        for tr, tdata in traces.items():
            all_k = tdata["keys"]
            for t in tdata["records"]:
                emit_log_record(t, all_k)

        for t in untraced:
            emit_log_record(t, t["subject_keys"])

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

    def _parse_image(self, file_id: str, captured_at: str, path: str):
        try:
            try:
                from .model_layer import ModelLayer
            except (ImportError, ValueError):
                from model_layer import ModelLayer

            ml = ModelLayer()
            data = ml.extract_image_evidence(path, file_id)

            c_time = pd.to_datetime(captured_at)
            matched_link = None
            link_events = self.activity_df[self.activity_df["event_type"] == "LINK_RESOURCE_IDENTITY"]
            for _, le in link_events.iterrows():
                l_time = pd.to_datetime(le["event_time"])
                if 0 <= (l_time - c_time).total_seconds() <= 900:
                    matched_link = le
                    break

            if matched_link is None:
                logger.warning(f"No matching LINK event found for image {file_id}. Skipping image.")
                return

            link_res_id = str(matched_link["resource_id"]).strip()
            link_sys_id = str(matched_link["target_system"]).strip()
            link_dtls = str(matched_link["details"]).strip()
            link_act_id = str(matched_link["activity_id"]).strip()

            # Canonical resource ID must match ^[a-z_]+_\d+$
            if not re.match(r'^[a-z_]+_\d+$', link_res_id):
                logger.warning(f"Invalid canonical resource_id {link_res_id} in LINK event {link_act_id}. Skipping.")
                return

            identifiers = {}
            m_user = re.search(r'\b(user_\d+)\b', link_dtls, re.IGNORECASE)
            if m_user:
                identifiers["user_id"] = m_user.group(1).lower()
            m_email = re.search(r'([a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+)', link_dtls)
            if m_email:
                identifiers["email"] = m_email.group(1).lower()

            res_lower = link_res_id.lower()
            if "account" in res_lower or link_sys_id == "account_service":
                cat = "USER_PROFILE"
            elif "search" in res_lower or link_sys_id == "search_service":
                cat = "SEARCH_INDEX_ENTRY"
            elif "data_flow" in res_lower or link_sys_id == "integration_service":
                cat = "DATA_FLOW"
            elif "analytics" in res_lower or link_sys_id == "analytics_platform":
                cat = "ANALYTICS_PROFILE"
            elif "export" in res_lower or link_sys_id == "export_service":
                cat = "USER_EXPORT"
            elif "support" in res_lower or link_sys_id == "support_platform":
                cat = "SUPPORT_MESSAGE"
            elif "backup" in res_lower or link_sys_id == "backup_service":
                cat = "BACKUP_ARCHIVE"
            elif "audit" in res_lower or link_sys_id == "security_service":
                cat = "SECURITY_AUDIT_LOG"
            elif "tombstone" in res_lower or link_sys_id == "identity_service":
                cat = "ACCOUNT_TOMBSTONE"
            elif "object" in res_lower or link_sys_id == "object_storage":
                cat = "SUPPORT_ATTACHMENT"
            elif "marketing" in res_lower or link_sys_id == "marketing_platform":
                cat = "MARKETING_PROFILE"
            else:
                cat = self._infer_category(link_sys_id, link_res_id)

            if link_sys_id == "identity_service" or "tombstone" in res_lower:
                stat = "REDACTED"
            elif link_sys_id == "object_storage" or "object" in res_lower:
                stat = "PRESENT"
            elif link_sys_id in ("integration_service", "backup_service", "marketing_platform", "support_platform"):
                stat = "ACTIVE"
            else:
                stat = "PRESENT"

            rec = NormalizedEvidence(
                file_id=file_id,
                captured_at=captured_at,
                system_id=link_sys_id,
                resource_id=link_res_id,
                data_category=cat,
                observed_status=stat,
                identifiers=identifiers,
                raw_details={"source": "image_link_bridge", "activity_id": link_act_id, "link_details": link_dtls, "image_data": data}
            )
            self._add_record(rec)
        except Exception as e:
            logger.warning(f"Error parsing image {file_id} ({path}): {e}. Skipping image.")

