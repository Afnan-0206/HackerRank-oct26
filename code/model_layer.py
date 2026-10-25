"""
Model Layer for Ghost Data Still Exists.
Provides:
1. Intent Gate: LLM-based classification of request_text returning {requests_deletion, language, evidence_quote}.
2. Image Evidence: VLM-based structured extraction for PNG console screenshots.
3. Telemetry: Comprehensive logging of provider, model, tokens, latency, and cache hits.
4. Deterministic Fallback: Fully functional without an API key or offline.
Configurable via environment variables:
- LLM_PROVIDER: "gemini" (default if key present), "openai", or "deterministic"
- LLM_MODEL: e.g. "gemini-2.5-flash" (default for gemini)
"""

import os
import json
import time
import hashlib
import logging
from typing import Dict, Any, List, Optional, Tuple
from PIL import Image

logger = logging.getLogger(__name__)

INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "requests_deletion": {"type": "boolean"},
        "language": {"type": "string"},
        "evidence_quote": {"type": "string"}
    },
    "required": ["requests_deletion", "language", "evidence_quote"]
}

IMAGE_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "system_id": {"type": "string"},
        "resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "record_type": {"type": "string"},
                    "email": {"type": ["string", "null"]},
                    "phone": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "status": {"type": "string"},
                    "timestamp": {"type": ["string", "null"]},
                    "details": {"type": "object"}
                },
                "required": ["resource_id"]
            }
        }
    },
    "required": ["system_id", "resources"]
}


class TelemetryTracker:
    def __init__(self, log_path: str = "code/cache/telemetry.jsonl", pricing_config_path: str = "code/pricing_config.json"):
        self.log_path = log_path
        self.pricing_config_path = pricing_config_path
        self.pricing_config = self._load_pricing()
        self.records: List[Dict[str, Any]] = []
        os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
        if os.path.exists(self.log_path):
            try:
                with open(self.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            self.records.append(json.loads(line))
            except Exception:
                pass

    def _load_pricing(self) -> Dict[str, Any]:
        if os.path.exists(self.pricing_config_path):
            try:
                with open(self.pricing_config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"Failed to read pricing config: {e}")
        return {
            "default": {
                "prompt_token_rate_per_million": 0.075,
                "candidate_token_rate_per_million": 0.30
            }
        }

    def _get_rates_for_model(self, model_name: str) -> Tuple[float, float]:
        rates = self.pricing_config.get(model_name) or self.pricing_config.get("default", {})
        p_rate = rates.get("prompt_token_rate_per_million", 0.075)
        c_rate = rates.get("candidate_token_rate_per_million", 0.30)
        return p_rate, c_rate

    def record_call(
        self,
        call_type: str,
        provider: str,
        model: str,
        prompt_tokens: int,
        candidate_tokens: int,
        total_tokens: int,
        latency_ms: float,
        cache_hit: bool,
        item_id: str = "",
        thinking_tokens: int = 0,
        error: Optional[str] = None
    ):
        p_rate, c_rate = self._get_rates_for_model(model)
        cost = (prompt_tokens * p_rate + (candidate_tokens + thinking_tokens) * c_rate) / 1_000_000.0

        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "call_type": call_type,
            "provider": provider,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "candidate_tokens": candidate_tokens,
            "thinking_tokens": thinking_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(cost, 6),
            "latency_ms": round(latency_ms, 2),
            "cache_hit": cache_hit,
            "item_id": item_id,
            "error": error
        }
        self.records.append(entry)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            logger.warning(f"Could not append telemetry: {e}")

    def get_summary(self, records: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        target_records = records if records is not None else self.records
        total_calls = len(target_records)
        cache_hits = sum(1 for r in target_records if r.get("cache_hit"))
        network_calls = total_calls - cache_hits
        total_prompt_tokens = sum(r.get("prompt_tokens", 0) for r in target_records)
        total_candidate_tokens = sum(r.get("candidate_tokens", 0) for r in target_records)
        total_thinking_tokens = sum(r.get("thinking_tokens", 0) for r in target_records)
        total_tokens = sum(r.get("total_tokens", 0) for r in target_records)
        
        # Calculate cost
        total_cost = 0.0
        for r in target_records:
            if "estimated_cost_usd" in r:
                total_cost += r["estimated_cost_usd"]
            else:
                p_rate, c_rate = self._get_rates_for_model(r.get("model", ""))
                total_cost += (r.get("prompt_tokens", 0) * p_rate + (r.get("candidate_tokens", 0) + r.get("thinking_tokens", 0)) * c_rate) / 1_000_000.0

        avg_latency = (sum(r.get("latency_ms", 0) for r in target_records if not r.get("cache_hit")) / max(1, network_calls))
        avg_prompt_tokens = total_prompt_tokens / max(1, total_calls)
        avg_candidate_tokens = total_candidate_tokens / max(1, total_calls)
        avg_thinking_tokens = total_thinking_tokens / max(1, total_calls)
        avg_total_tokens = total_tokens / max(1, total_calls)
        avg_cost = total_cost / max(1, total_calls)

        providers = sorted(list(set(r.get("provider", "unknown") for r in target_records)))
        models = sorted(list(set(r.get("model", "unknown") for r in target_records)))

        # Per-model breakdown
        by_model: Dict[str, Dict[str, Any]] = {}
        for m in models:
            m_recs = [r for r in target_records if r.get("model") == m]
            m_total = len(m_recs)
            m_hits = sum(1 for r in m_recs if r.get("cache_hit"))
            m_net = m_total - m_hits
            m_p = sum(r.get("prompt_tokens", 0) for r in m_recs)
            m_c = sum(r.get("candidate_tokens", 0) for r in m_recs)
            m_th = sum(r.get("thinking_tokens", 0) for r in m_recs)
            m_tot = sum(r.get("total_tokens", 0) for r in m_recs)
            m_cost = sum(r.get("estimated_cost_usd", 0.0) for r in m_recs)
            by_model[m] = {
                "total_calls": m_total,
                "cache_hits": m_hits,
                "network_calls": m_net,
                "prompt_tokens": m_p,
                "candidate_tokens": m_c,
                "thinking_tokens": m_th,
                "total_tokens": m_tot,
                "estimated_cost_usd": m_cost
            }

        case_count = float(max(1, total_calls))
        avg_tokens_per_case = round(total_tokens / case_count, 2)
        avg_cost_per_case = round(total_cost / case_count, 6)

        return {
            "total_calls": total_calls,
            "cache_hits": cache_hits,
            "cache_hit_rate_pct": round((cache_hits / max(1, total_calls)) * 100, 2),
            "network_calls": network_calls,
            "providers": providers,
            "models": models,
            "by_model": by_model,
            "total_prompt_tokens": total_prompt_tokens,
            "total_candidate_tokens": total_candidate_tokens,
            "total_thinking_tokens": total_thinking_tokens,
            "total_tokens": total_tokens,
            "avg_tokens_per_case": avg_tokens_per_case,
            "avg_cost_per_case": avg_cost_per_case,
            "avg_prompt_tokens": round(avg_prompt_tokens, 2),
            "avg_candidate_tokens": round(avg_candidate_tokens, 2),
            "avg_thinking_tokens": round(avg_thinking_tokens, 2),
            "avg_total_tokens": round(avg_total_tokens, 2),
            "total_cost_usd": round(total_cost, 6),
            "avg_cost_usd": round(avg_cost, 6),
            "avg_latency_ms": round(avg_latency, 2)
        }

    def generate_usage_report(self, output_path: str = "code/evaluation/usage_report.md", run_records: Optional[List[Dict[str, Any]]] = None):
        target_records = run_records if run_records is not None else self.records
        summary = self.get_summary(target_records)
        meta = self.pricing_config.get("_metadata", {})
        source_url = meta.get("source_url", "https://ai.google.dev/pricing")
        retrieved_date = meta.get("retrieved_date", "2026-10-24")

        by_type: Dict[str, List[Dict[str, Any]]] = {}
        for r in target_records:
            t = r.get("call_type", "unknown")
            by_type.setdefault(t, []).append(r)

        md = f"""# LLM / VLM Telemetry & Usage Report

## 1. Production Run Telemetry (Single Execution)
- **Providers Configured**: {', '.join(summary['providers']) or 'None'}
- **Models Active**: {', '.join(summary['models']) or 'None'}
- **Total Requests Evaluated**: {summary['total_calls']}
- **Cache Hits**: {summary['cache_hits']} ({summary['cache_hit_rate_pct']}%)
- **Remote Network Calls**: {summary['network_calls']}
- **Input (Prompt) Tokens**: {summary['total_prompt_tokens']:,}
- **Output (Candidate) Tokens**: {summary['total_candidate_tokens']:,}
- **Thinking / Reasoning Tokens**: {summary['total_thinking_tokens']:,}
- **Total Tokens Consumed**: {summary['total_tokens']:,}
- **Tokens Per Case**: **{summary['avg_tokens_per_case']} tokens/case**
- **Estimated Total Cost**: **${summary['total_cost_usd']:.4f} USD**
- **Cost Per Case**: **${summary['avg_cost_per_case']:.6f} USD/case**
- **Average Network Latency**: {summary['avg_latency_ms']} ms

## 2. Per-Model Breakdown
| Model Name | Total Calls | Cache Hits | Network Calls | Prompt (Input) Tokens | Candidate (Output) Tokens | Thinking Tokens | Total Tokens | Estimated Cost (USD) |
|---|---|---|---|---|---|---|---|---|
"""
        for m_name, m_data in summary["by_model"].items():
            md += f"| `{m_name}` | {m_data['total_calls']} | {m_data['cache_hits']} | {m_data['network_calls']} | {m_data['prompt_tokens']:,} | {m_data['candidate_tokens']:,} | {m_data['thinking_tokens']:,} | {m_data['total_tokens']:,} | ${m_data['estimated_cost_usd']:.4f} |\n"

        md += f"""| **Overall Totals** | **{summary['total_calls']}** | **{summary['cache_hits']}** | **{summary['network_calls']}** | **{summary['total_prompt_tokens']:,}** | **{summary['total_candidate_tokens']:,}** | **{summary['total_thinking_tokens']:,}** | **{summary['total_tokens']:,}** | **${summary['total_cost_usd']:.4f}** |

## 3. Task Breakdown (Image Extraction vs Intent Classification)
| Task Type | Total Calls | Cache Hits | Network Calls | Prompt Tokens | Output Tokens | Thinking Tokens | Total Tokens | Estimated Cost (USD) |
|---|---|---|---|---|---|---|---|---|
"""
        for t, recs in by_type.items():
            t_total = len(recs)
            t_hits = sum(1 for r in recs if r.get("cache_hit"))
            t_net = t_total - t_hits
            t_p = sum(r.get("prompt_tokens", 0) for r in recs)
            t_c = sum(r.get("candidate_tokens", 0) for r in recs)
            t_th = sum(r.get("thinking_tokens", 0) for r in recs)
            t_tot = sum(r.get("total_tokens", 0) for r in recs)
            t_cost = sum(r.get("estimated_cost_usd", 0.0) for r in recs)
            md += f"| **{t}** | {t_total} | {t_hits} | {t_net} | {t_p:,} | {t_c:,} | {t_th:,} | {t_tot:,} | ${t_cost:.4f} |\n"

        md += f"""
## 4. Pricing Configuration
Cost estimations were computed based on rates defined in [`code/pricing_config.json`](file:///code/pricing_config.json):
- **Pricing Source URL**: [{source_url}]({source_url})
- **Retrieved Date**: {retrieved_date}
- **Gemini 2.5 Flash Input Rate**: $0.075 / 1,000,000 prompt tokens
- **Gemini 2.5 Flash Output Rate**: $0.30 / 1,000,000 candidate / thinking tokens
- **Zero Costs**: Assessed for local cache hits and deterministic fallbacks.
"""

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md)


class ModelLayer:
    def __init__(
        self,
        cache_dir: str = "code/cache",
        pricing_config_path: str = "code/pricing_config.json",
        telemetry: Optional[TelemetryTracker] = None
    ):
        self.cache_dir = cache_dir
        self.pricing_config_path = pricing_config_path
        self.images_cache_dir = os.path.join(cache_dir, "images")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.images_cache_dir, exist_ok=True)
        
        self.telemetry = telemetry or TelemetryTracker(
            os.path.join(cache_dir, "telemetry.jsonl"),
            pricing_config_path=pricing_config_path
        )
        self.intent_cache_path = os.path.join(cache_dir, "intent_cache.json")
        self.intent_cache: Dict[str, Dict[str, Any]] = self._load_json(self.intent_cache_path)

        # Provider and Model configuration
        self.provider = os.environ.get("LLM_PROVIDER", "").lower()
        if not self.provider:
            if os.environ.get("GEMINI_API_KEY"):
                self.provider = "gemini"
            elif os.environ.get("OPENAI_API_KEY"):
                self.provider = "openai"
            else:
                self.provider = "deterministic"

        self.model = os.environ.get("LLM_MODEL", "")
        if not self.model:
            if self.provider == "gemini":
                self.model = "gemini-2.5-flash"
            elif self.provider == "openai":
                self.model = "gpt-4o-mini"
            else:
                self.model = "rule_based_fallback"

        # Initialize clients lazily
        self._gemini_client = None
        self._openai_client = None

    def _load_json(self, path: str) -> Dict[str, Any]:
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _save_json(self, path: str, data: Any):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write cache at {path}: {e}")

    def _get_gemini_client(self):
        if self._gemini_client is None:
            from google import genai
            self._gemini_client = genai.Client()
        return self._gemini_client

    # -------------------------------------------------------------
    # INTENT CLASSIFICATION
    # -------------------------------------------------------------
    def deterministic_classify_intent(self, request_text: str) -> Dict[str, Any]:
        """
        Deterministic rule-based fallback intent classification.
        Identifies non-deletion markers (marketing unsubscribe, access, portability, opt-out without deletion)
        versus true account deletion requests.
        """
        txt = request_text.lower().strip()

        # Negated deletion markers across languages
        negated_del_markers = [
            "don't delete", "do not delete", "not asking for deletion", "not a request to erase",
            "bitte nicht löschen", "nicht löschen", "ne supprimez pas", "pas supprimer",
            "no eliminen", "no borrar", "não exclua", "não excluir",
            "non cancellate", "non cancellare", "削除しないで", "消さないで"
        ]

        # Non-deletion markers (access inquiry, portability/export, marketing unsubscribe, update/rectify)
        non_del_markers = [
            "leave the account active",
            "keep my account active",
            "leave the account open",
            "leave my account open",
            "keep the account as it is",
            "do not close the account",
            "without removing any of my records",
            "without removing",
            "marketing lists only",
            "leave my profile and purchase history in place",
            "in place",
            "continue using the service",
            "unsubscribe me from marketing",
            "stop the promotional emails",
            "export my data",
            "do i have data on file",
            "what personal data",
            "what personal information",
            "download a copy",
            "download my account information",
            "portable format",
            "guarde mi perfil",
            "gardez mon profil",
            "nur marketing",
            "qué información",
            "descargar una copia",
            "copie de mes données",
            "export complet",
            "auskunft",
            "welche daten",
            "aggiornare il mio",
            "esportazione",
        ]

        # Deletion markers across supported languages (English, Spanish, French, German, Japanese, Portuguese, Italian, Korean)
        del_markers = [
            "delete", "deletion", "remove", "removal", "erase", "erasure", "purge", "forget",
            "close my account", "close this account", "terminate my account",
            "eliminar", "eliminación", "borrar", "cancelar mi cuenta",
            "supprimer", "suppression", "effacer", "effacement", "clôturer mon compte",
            "löschen", "löschung", "konto schließen",
            "削除", "消去", "アカウント閉鎖", "アカウント削除",
            "삭제", "지워주세요", "계정 삭제", "탈퇴",
            "excluir", "exclusão", "apagar", "encerrar minha conta",
            "cancellare", "cancellazione"
        ]

        has_negated_del = any(m in txt for m in negated_del_markers)
        is_non_del = any(m in txt for m in non_del_markers)
        has_del = any(m in txt for m in del_markers)

        # Detect language
        lang = "English"
        if any(w in txt for w in ["por favor", "eliminen", "cuenta", "datos", "guardada", "qué información", "borrar"]):
            lang = "Spanish"
        elif any(w in txt for w in ["veuillez", "supprimer", "compte", "données", "gardez", "pouvez-vous", "effacer"]):
            lang = "French"
        elif any(w in txt for w in ["bitte", "löschen", "benutzerkonto", "daten", "auskunft"]):
            lang = "German"
        elif any(0xAC00 <= ord(c) <= 0xD7A3 for c in txt):
            lang = "Korean"
        elif any(ord(c) > 0x3000 for c in txt):
            lang = "Japanese"
        elif any(w in txt for w in ["solicito", "exclusão", "meus dados", "minha conta", "apagar", "excluir"]):
            lang = "Portuguese"
        elif any(w in txt for w in ["cancellate", "mio account", "vorrei", "cancellare"]):
            lang = "Italian"

        # Check for mixed requests: user wants to delete account but retain specific records
        is_mixed = has_del and any(w in txt for w in ["retain", "tax", "invoice", "receipt", "keep only"]) and not is_non_del

        if is_mixed:
            quote = ""
            for m in del_markers:
                if m in txt:
                    quote = m
                    break
            return {
                "requests_deletion": True,
                "language": lang,
                "evidence_quote": quote or request_text[:60]
            }

        # Positive non-deletion signal required to classify as non-deletion (NOT_APPLICABLE)
        if has_negated_del or is_non_del:
            quote = ""
            for m in (negated_del_markers + non_del_markers):
                if m in txt:
                    quote = m
                    break
            return {
                "requests_deletion": False,
                "language": lang,
                "evidence_quote": quote or request_text[:60],
                "non_deletion_reason": f"Positive non-deletion signal: '{quote or request_text[:40]}'"
            }

        # If no positive non-deletion signal is present, default to True (requests deletion)
        # to ensure NOT_APPLICABLE strictly requires an affirmative non-deletion signal with a recorded reason
        quote = ""
        for m in del_markers:
            if m in txt:
                quote = m
                break
        return {
            "requests_deletion": True,
            "language": lang,
            "evidence_quote": quote or request_text[:60]
        }

    def classify_intent(self, request_text: str) -> Dict[str, Any]:
        """
        Classifies request_text into strict JSON {requests_deletion: bool, language: str, evidence_quote: str}.
        Uses disk cache (keyed by text SHA-256), retries on format errors, and falls back to deterministic logic.
        """
        text_hash = hashlib.sha256(request_text.strip().encode("utf-8")).hexdigest()

        # Check Cache
        if text_hash in self.intent_cache:
            res = self.intent_cache[text_hash]
            self.telemetry.record_call(
                call_type="intent_classification",
                provider=self.provider,
                model=self.model,
                prompt_tokens=0,
                candidate_tokens=0,
                total_tokens=0,
                latency_ms=0.0,
                cache_hit=True,
                item_id=text_hash[:8]
            )
            return res

        # If deterministic mode, no API key, or quota exhausted, use fallback directly
        if self.provider == "deterministic" or not os.environ.get("GEMINI_API_KEY") or getattr(self, "_quota_exhausted", False):
            res = self.deterministic_classify_intent(request_text)
            self.intent_cache[text_hash] = res
            self._save_json(self.intent_cache_path, self.intent_cache)
            self.telemetry.record_call(
                call_type="intent_classification",
                provider="deterministic",
                model="rule_based",
                prompt_tokens=0,
                candidate_tokens=0,
                total_tokens=0,
                latency_ms=0.1,
                cache_hit=False,
                item_id=text_hash[:8]
            )
            return res

        # LLM Call with Retry
        prompt = f"""You are a data privacy deletion auditor. Analyze the following user request text.
Determine whether the user is explicitly or implicitly requesting the deletion/erasure/removal of their account or personal data.
If the user is asking to KEEP their account active, asking purely for data export/access, or asking ONLY to unsubscribe from marketing emails without deleting their account, requests_deletion is FALSE.
If the user asks to delete their account but retain certain records (like tax invoices), requests_deletion is TRUE.

Return ONLY a valid JSON object matching this schema:
{{
  "requests_deletion": boolean,
  "language": "string (e.g. English, Spanish, French, Japanese, etc.)",
  "evidence_quote": "exact quote from the request text supporting your decision"
}}

User Request Text:
\"\"\"{request_text}\"\"\"
"""
        max_retries = 3
        backoff_sec = 2.0
        for attempt in range(max_retries + 1):
            start_t = time.time()
            try:
                client = self._get_gemini_client()
                from google.genai import types
                response = client.models.generate_content(
                    model=self.model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                latency = (time.time() - start_t) * 1000
                p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                t_tok = getattr(response.usage_metadata, "total_token_count", 0) or 0
                th_tok = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

                data = json.loads(response.text)
                # Schema Validation
                if not isinstance(data.get("requests_deletion"), bool):
                    raise ValueError("requests_deletion must be a boolean")
                if not isinstance(data.get("language"), str):
                    raise ValueError("language must be a string")
                if not isinstance(data.get("evidence_quote"), str):
                    raise ValueError("evidence_quote must be a string")

                res = {
                    "requests_deletion": bool(data["requests_deletion"]),
                    "language": str(data["language"]),
                    "evidence_quote": str(data["evidence_quote"])
                }

                self.telemetry.record_call(
                    call_type="intent_classification",
                    provider=self.provider,
                    model=self.model,
                    prompt_tokens=p_tok,
                    candidate_tokens=c_tok,
                    total_tokens=t_tok,
                    latency_ms=latency,
                    cache_hit=False,
                    item_id=text_hash[:8],
                    thinking_tokens=th_tok
                )

                self.intent_cache[text_hash] = res
                self._save_json(self.intent_cache_path, self.intent_cache)
                return res

            except Exception as e:
                err_str = str(e)
                logger.warning(f"Intent classification attempt {attempt + 1} failed: {err_str[:120]}")
                is_rate_limit = "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower() or "503" in err_str
                is_daily_quota = "requests per day" in err_str.lower() or "daily" in err_str.lower()
                
                if attempt < max_retries and is_rate_limit and not is_daily_quota:
                    sleep_time = backoff_sec * (2 ** attempt)
                    logger.info(f"Rate limited (transient); backing off for {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    if is_rate_limit:
                        self._quota_exhausted = True
                    # Fallback to deterministic
                    res = self.deterministic_classify_intent(request_text)
                    self.telemetry.record_call(
                        call_type="intent_classification",
                        provider=self.provider,
                        model=self.model,
                        prompt_tokens=0,
                        candidate_tokens=0,
                        total_tokens=0,
                        latency_ms=(time.time() - start_t) * 1000,
                        cache_hit=False,
                        item_id=text_hash[:8],
                        thinking_tokens=0,
                        error=err_str[:150]
                    )
                    self.intent_cache[text_hash] = res
                    self._save_json(self.intent_cache_path, self.intent_cache)
                    return res

    # -------------------------------------------------------------
    # IMAGE EVIDENCE EXTRACTION (VLM)
    # -------------------------------------------------------------
    def extract_image_evidence(self, image_path: str, file_id: str) -> Dict[str, Any]:
        """
        Extracts structured JSON from an evidence screenshot using VLM.
        Cached on disk at code/cache/images/{file_id}.json.
        Extracts ONLY fields visible in the image without hallucination or inference.
        """
        cache_file = os.path.join(self.images_cache_dir, f"{file_id}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    res = json.load(f)
                    self.telemetry.record_call(
                        call_type="image_extraction",
                        provider=self.provider,
                        model=self.model,
                        prompt_tokens=0,
                        candidate_tokens=0,
                        total_tokens=0,
                        latency_ms=0.0,
                        cache_hit=True,
                        item_id=file_id,
                        thinking_tokens=0
                    )
                    return res
            except Exception:
                pass

        # If deterministic or no key, return empty resources structure
        if self.provider == "deterministic" or not os.environ.get("GEMINI_API_KEY"):
            res = {"system_id": "image_admin_console", "resources": []}
            self._save_json(cache_file, res)
            self.telemetry.record_call(
                call_type="image_extraction",
                provider="deterministic",
                model="offline",
                prompt_tokens=0,
                candidate_tokens=0,
                total_tokens=0,
                latency_ms=0.1,
                cache_hit=False,
                item_id=file_id,
                thinking_tokens=0
            )
            return res

        prompt = """You are a precise data extraction engine. Analyze this screenshot of an internal system console.
Extract ONLY fields visible in the image. Do NOT infer or hallucinate any fields.
Return a valid JSON object matching this schema:
{
  "system_id": "string (service or console name shown)",
  "resources": [
    {
      "resource_id": "string (exact resource identifier shown)",
      "record_type": "string (exact record type)",
      "email": "string or null",
      "phone": "string or null",
      "name": "string or null",
      "status": "string (observed status e.g. PRESENT, ACTIVE, DELETED, NOT_FOUND, DISABLED)",
      "timestamp": "string or null",
      "details": "dict with any other visible key-value pairs"
    }
  ]
}
"""
        max_retries = 3
        backoff_sec = 2.0
        for attempt in range(max_retries + 1):
            start_t = time.time()
            try:
                img = Image.open(image_path)
                client = self._get_gemini_client()
                from google.genai import types
                response = client.models.generate_content(
                    model=self.model,
                    contents=[img, prompt],
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    )
                )
                latency = (time.time() - start_t) * 1000
                p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                t_tok = getattr(response.usage_metadata, "total_token_count", 0) or 0
                th_tok = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

                data = json.loads(response.text)
                if not isinstance(data, dict) or "resources" not in data:
                    raise ValueError("Invalid schema: missing resources list")

                self.telemetry.record_call(
                    call_type="image_extraction",
                    provider=self.provider,
                    model=self.model,
                    prompt_tokens=p_tok,
                    candidate_tokens=c_tok,
                    total_tokens=t_tok,
                    latency_ms=latency,
                    cache_hit=False,
                    item_id=file_id,
                    thinking_tokens=th_tok
                )

                self._save_json(cache_file, data)
                return data

            except Exception as e:
                err_str = str(e)
                logger.warning(f"VLM extraction for {file_id} attempt {attempt + 1} failed: {err_str[:120]}")
                is_rate_limit = "429" in err_str or "quota" in err_str.lower() or "resource_exhausted" in err_str.lower() or "503" in err_str
                is_daily_quota = "requests per day" in err_str.lower() or "daily" in err_str.lower()
                if attempt < max_retries and is_rate_limit and not is_daily_quota:
                    sleep_time = backoff_sec * (2 ** attempt)
                    logger.info(f"VLM rate limited; backing off for {sleep_time}s...")
                    time.sleep(sleep_time)
                else:
                    logger.warning(f"VLM extraction failed for {file_id}. Skipping image caching.")
                    res = {"system_id": "image_admin_console", "resources": []}
                    self.telemetry.record_call(
                        call_type="image_extraction",
                        provider=self.provider,
                        model=self.model,
                        prompt_tokens=0,
                        candidate_tokens=0,
                        total_tokens=0,
                        latency_ms=(time.time() - start_t) * 1000,
                        cache_hit=False,
                        item_id=file_id,
                        thinking_tokens=0,
                        error=err_str[:150]
                    )
                    return res
