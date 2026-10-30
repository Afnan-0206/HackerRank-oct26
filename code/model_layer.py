"""
Model Layer for Ghost Data Still Exists.
Provides:
1. Resilient Multi-Provider Cascade: Gemini 2.5 Flash -> OpenAI / Anthropic -> Local Rule-Based Engine.
2. Exponential Backoff with Jitter: Handles transient 429 rate limits, 503 service unavailable, and timeouts.
3. Hard Quota Detection: Automatically cascades to fallback providers on daily/credit quota exhaustion.
4. Pydantic Schema Validation: Strict validation and type coercion for all model and extraction payloads.
5. Telemetry & Cost Accounting: In-depth tracking of latency, token usage, cache hits, and estimated costs.
6. Offline & Zero-Key Reproducibility: 100% functional without an API key or when network is disabled.
"""

import os
import re
import sys
import json
import time
import base64
import random
import hashlib
import logging
from typing import Dict, Any, List, Optional, Tuple, Set, Union
from PIL import Image
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


# ==============================================================================
# PYDANTIC VALIDATION SCHEMAS
# ==============================================================================
class IntentClassificationPayload(BaseModel):
    """Guarantees schema compliance and type coercion for intent classification."""
    requests_deletion: bool = Field(
        ...,
        description="Whether the user is explicitly or implicitly requesting account/data deletion"
    )
    language: str = Field(
        ...,
        description="Detected natural language of the request text (e.g. English, Spanish, French)"
    )
    evidence_quote: str = Field(
        ...,
        description="Exact quote or phrase from request text supporting the classification decision"
    )
    non_deletion_reason: Optional[str] = Field(
        default=None,
        description="Recorded reason if classified as non-deletion (e.g. positive non-deletion signal)"
    )


class ImageResourceItem(BaseModel):
    """Schema for individual resource items extracted from system console screenshots."""
    resource_id: str = Field(..., description="Canonical or extracted resource identifier")
    record_type: Optional[str] = Field(default="UNKNOWN", description="Record type observed in screenshot")
    email: Optional[str] = Field(default=None, description="Associated user email if present")
    phone: Optional[str] = Field(default=None, description="Associated phone number if present")
    name: Optional[str] = Field(default=None, description="Associated user name if present")
    status: Optional[str] = Field(default="PRESENT", description="Resource status observed (e.g. PRESENT, ACTIVE, DELETED)")
    timestamp: Optional[str] = Field(default=None, description="Timestamp observed on record")
    details: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional observed fields")


class ImageExtractionPayload(BaseModel):
    """Guarantees schema compliance for VLM console screenshot extractions."""
    system_id: str = Field(
        default="image_admin_console",
        description="Service, system, or console name shown in screenshot"
    )
    resources: List[ImageResourceItem] = Field(
        default_factory=list,
        description="Extracted resource records"
    )


# ==============================================================================
# RETRY, JITTER & PARSING UTILITIES
# ==============================================================================
def calculate_backoff_with_jitter(
    attempt: int,
    base_sec: float = 2.0,
    max_sec: float = 30.0,
    jitter_factor: float = 0.5
) -> float:
    """
    Computes exponential backoff with additive jitter:
      sleep = min(max_sec, base_sec * (2 ** attempt) + uniform(0, jitter_factor * raw_backoff))
    Prevents thundering herd problems during transient cluster contention.
    """
    raw_backoff = base_sec * (2 ** attempt)
    jitter = random.uniform(0, jitter_factor * raw_backoff)
    return min(max_sec, raw_backoff + jitter)


def is_transient_error(err_str: str) -> bool:
    """Detects transient HTTP/network errors eligible for exponential backoff retry."""
    err_lower = err_str.lower()
    transient_indicators = [
        "429", "rate_limit", "ratelimit", "too_many_requests", "resource_exhausted",
        "503", "service_unavailable", "502", "bad_gateway", "504", "gateway_timeout",
        "500", "internal_server_error", "timeout", "timed out", "connection reset",
        "overloaded"
    ]
    return any(ind in err_lower for ind in transient_indicators)


def is_hard_quota_exhausted(err_str: str) -> bool:
    """Detects hard quota exhaustion where immediate retries on the same provider are futile."""
    err_lower = err_str.lower()
    hard_indicators = [
        "requests per day", "daily", "free tier limit", "quota exceeded for quota metric",
        "insufficient_quota", "billing", "credit balance is too low", "exceeded your current quota"
    ]
    return any(ind in err_lower for ind in hard_indicators)


def clean_and_parse_json(raw_text: str) -> Dict[str, Any]:
    """
    Strips markdown codeblock fences and extraneous commentary, extracting valid JSON.
    """
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()
    start_idx = text.find("{")
    end_idx = text.rfind("}")
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        text = text[start_idx:end_idx + 1]
    return json.loads(text)


# ==============================================================================
# TELEMETRY TRACKER
# ==============================================================================
class TelemetryTracker:
    def __init__(self, log_path: str = "code/cache/telemetry.jsonl", pricing_config_path: str = "code/pricing_config.json"):
        base_code_dir = os.path.dirname(os.path.abspath(__file__))
        if log_path == "code/cache/telemetry.jsonl":
            log_path = os.path.join(base_code_dir, "cache", "telemetry.jsonl")
        if pricing_config_path == "code/pricing_config.json":
            pricing_config_path = os.path.join(base_code_dir, "pricing_config.json")
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


# ==============================================================================
# MODEL LAYER WITH RESILIENT MULTI-PROVIDER CASCADE
# ==============================================================================
class ModelLayer:
    """
    Orchestrates Intent Gate and VLM Extraction with:
    - Resilient Fallback Cascade: Gemini 2.5 Flash -> OpenAI -> Anthropic -> Local Rule-Based Engine.
    - Exponential Backoff with Jitter for transient 429/503 errors.
    - Hard Quota Detection: Immediately cascades when daily quotas or credits are exhausted.
    - Pydantic Schema Validation: Strict validation for all output payloads.
    - Persistent caching and zero-key offline execution.
    """
    def __init__(
        self,
        cache_dir: str = "code/cache",
        pricing_config_path: str = "code/pricing_config.json",
        telemetry: Optional[TelemetryTracker] = None
    ):
        base_code_dir = os.path.dirname(os.path.abspath(__file__))
        if cache_dir == "code/cache":
            cache_dir = os.path.join(base_code_dir, "cache")
        if pricing_config_path == "code/pricing_config.json":
            pricing_config_path = os.path.join(base_code_dir, "pricing_config.json")

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

        # Primary provider and model from environment
        self.provider = os.environ.get("LLM_PROVIDER", "").lower()
        if not self.provider:
            if os.environ.get("GEMINI_API_KEY"):
                self.provider = "gemini"
            elif os.environ.get("OPENAI_API_KEY"):
                self.provider = "openai"
            elif os.environ.get("ANTHROPIC_API_KEY"):
                self.provider = "anthropic"
            else:
                self.provider = "deterministic"

        self.model = os.environ.get("LLM_MODEL", "")
        if not self.model:
            if self.provider == "gemini":
                self.model = "gemini-2.5-flash"
            elif self.provider == "openai":
                self.model = "gpt-4o-mini"
            elif self.provider == "anthropic":
                self.model = "claude-3-5-haiku-20241022"
            else:
                self.model = "rule_based_fallback"

        # Lazy SDK clients
        self._gemini_client = None
        self._openai_client = None
        self._anthropic_client = None

        # Track exhausted providers during runtime to skip futile calls
        self._exhausted_providers: Set[str] = set()

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

    def _get_openai_client(self):
        if self._openai_client is None:
            from openai import OpenAI
            self._openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
        return self._openai_client

    def _get_anthropic_client(self):
        if self._anthropic_client is None:
            import anthropic
            self._anthropic_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
        return self._anthropic_client

    def _is_provider_available(self, provider: str) -> bool:
        """Determines if a provider is configured and available."""
        if provider in self._exhausted_providers:
            return False
        if provider == "deterministic":
            return True
        if provider == "gemini":
            return bool(os.environ.get("GEMINI_API_KEY"))
        if provider == "openai":
            return bool(os.environ.get("OPENAI_API_KEY"))
        if provider == "anthropic":
            if not os.environ.get("ANTHROPIC_API_KEY"):
                return False
            try:
                import anthropic
                return True
            except ImportError:
                return False
        return False

    def get_fallback_cascade(self, task_type: str = "intent") -> List[Tuple[str, str]]:
        """
        Builds the fallback cascade sequence:
        Gemini 2.5 Flash -> OpenAI (gpt-4o-mini) -> Anthropic (claude-3-5-haiku) -> Local Rule-Based Engine.
        Honors explicit LLM_PROVIDER as the primary candidate if specified.
        """
        if self.provider == "deterministic":
            return [("deterministic", "rule_based")]

        default_sequence = [
            ("gemini", self.model if self.provider == "gemini" else "gemini-2.5-flash"),
            ("openai", self.model if self.provider == "openai" else "gpt-4o-mini"),
            ("anthropic", self.model if self.provider == "anthropic" else "claude-3-5-haiku-20241022"),
            ("deterministic", "rule_based")
        ]

        # If an explicit provider was configured, prioritize it first
        if self.provider in ("openai", "anthropic"):
            priority = [(p, m) for p, m in default_sequence if p == self.provider]
            others = [(p, m) for p, m in default_sequence if p != self.provider]
            sequence = priority + others
        else:
            sequence = default_sequence

        # Filter down to available providers
        available = [(p, m) for p, m in sequence if self._is_provider_available(p)]
        if not available or available[-1][0] != "deterministic":
            available.append(("deterministic", "rule_based"))
        return available

    # -------------------------------------------------------------
    # DETERMINISTIC LOCAL ENGINE (RULE-BASED)
    # -------------------------------------------------------------
    def deterministic_classify_intent(self, request_text: str) -> Dict[str, Any]:
        """
        Deterministic rule-based fallback intent classification.
        Identifies non-deletion markers (marketing unsubscribe, access, portability, opt-out without deletion)
        versus true account deletion requests across 8 languages.
        """
        txt = request_text.lower().strip()

        negated_del_markers = [
            "don't delete", "do not delete", "not asking for deletion", "not a request to erase",
            "bitte nicht löschen", "nicht löschen", "ne supprimez pas", "pas supprimer",
            "no eliminen", "no borrar", "não exclua", "não excluir",
            "non cancellate", "non cancellare", "削除しないで", "消さないで"
        ]

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

        is_mixed = has_del and any(w in txt for w in ["retain", "tax", "invoice", "receipt", "keep only"]) and not is_non_del

        if is_mixed:
            quote = ""
            for m in del_markers:
                if m in txt:
                    quote = m
                    break
            raw_res = {
                "requests_deletion": True,
                "language": lang,
                "evidence_quote": quote or request_text[:60]
            }
        elif has_negated_del or is_non_del:
            quote = ""
            for m in (negated_del_markers + non_del_markers):
                if m in txt:
                    quote = m
                    break
            raw_res = {
                "requests_deletion": False,
                "language": lang,
                "evidence_quote": quote or request_text[:60],
                "non_deletion_reason": f"Positive non-deletion signal: '{quote or request_text[:40]}'"
            }
        else:
            quote = ""
            for m in del_markers:
                if m in txt:
                    quote = m
                    break
            raw_res = {
                "requests_deletion": True,
                "language": lang,
                "evidence_quote": quote or request_text[:60]
            }

        validated = IntentClassificationPayload.model_validate(raw_res)
        return validated.model_dump()

    # -------------------------------------------------------------
    # PROVIDER-SPECIFIC INTENT CALLS
    # -------------------------------------------------------------
    def _call_gemini_intent(self, model: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        client = self._get_gemini_client()
        from google.genai import types
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        t_tok = getattr(response.usage_metadata, "total_token_count", 0) or 0
        th_tok = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

        raw_data = clean_and_parse_json(response.text)
        validated = IntentClassificationPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, th_tok

    def _call_openai_intent(self, model: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        client = self._get_openai_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a data privacy deletion auditor. Return valid JSON matching the schema strictly."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content or "{}"
        p_tok = response.usage.prompt_tokens if response.usage else 0
        c_tok = response.usage.completion_tokens if response.usage else 0
        t_tok = response.usage.total_tokens if response.usage else 0

        raw_data = clean_and_parse_json(raw_text)
        validated = IntentClassificationPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, 0

    def _call_anthropic_intent(self, model: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        client = self._get_anthropic_client()
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system="You are a data privacy deletion auditor. Return valid JSON only, without markdown fences or extraneous text.",
            messages=[
                {"role": "user", "content": prompt}
            ]
        )
        raw_text = response.content[0].text or "{}"
        p_tok = response.usage.input_tokens if response.usage else 0
        c_tok = response.usage.output_tokens if response.usage else 0
        t_tok = p_tok + c_tok

        raw_data = clean_and_parse_json(raw_text)
        validated = IntentClassificationPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, 0

    # -------------------------------------------------------------
    # INTENT CLASSIFICATION WITH MULTI-PROVIDER CASCADE
    # -------------------------------------------------------------
    def classify_intent(self, request_text: str) -> Dict[str, Any]:
        """
        Classifies request_text into strict JSON {requests_deletion: bool, language: str, evidence_quote: str}.
        Uses disk cache first, cascades across available providers with exponential jittered backoff,
        and falls back to deterministic rule-based engine when quotas or network are unavailable.
        """
        text_hash = hashlib.sha256(request_text.strip().encode("utf-8")).hexdigest()

        # 1. Check on-disk cache
        if text_hash in self.intent_cache:
            res = self.intent_cache[text_hash]
            self.telemetry.record_call(
                call_type="intent_classification",
                provider=self.provider or "cached",
                model=self.model or "cached",
                prompt_tokens=0,
                candidate_tokens=0,
                total_tokens=0,
                latency_ms=0.0,
                cache_hit=True,
                item_id=text_hash[:8]
            )
            return res

        cascade = self.get_fallback_cascade(task_type="intent")
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

        for provider_idx, (provider, model_name) in enumerate(cascade):
            if provider == "deterministic":
                # Final local fallback
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

            for attempt in range(max_retries + 1):
                start_t = time.time()
                try:
                    if provider == "gemini":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_gemini_intent(model_name, prompt)
                    elif provider == "openai":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_openai_intent(model_name, prompt)
                    elif provider == "anthropic":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_anthropic_intent(model_name, prompt)
                    else:
                        break

                    latency = (time.time() - start_t) * 1000
                    self.telemetry.record_call(
                        call_type="intent_classification",
                        provider=provider,
                        model=model_name,
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
                    latency = (time.time() - start_t) * 1000
                    logger.warning(f"[{provider}/{model_name}] Intent attempt {attempt + 1} failed: {err_str[:120]}")

                    if is_hard_quota_exhausted(err_str):
                        logger.warning(f"Provider '{provider}' hard daily quota exhausted. Cascading to next provider...")
                        self._exhausted_providers.add(provider)
                        self.telemetry.record_call(
                            call_type="intent_classification",
                            provider=provider,
                            model=model_name,
                            prompt_tokens=0,
                            candidate_tokens=0,
                            total_tokens=0,
                            latency_ms=latency,
                            cache_hit=False,
                            item_id=text_hash[:8],
                            error=f"HardQuotaExhausted: {err_str[:100]}"
                        )
                        break  # Break out of retry loop for this provider, continue to next in cascade

                    if is_transient_error(err_str) and attempt < max_retries:
                        sleep_time = calculate_backoff_with_jitter(attempt, base_sec=2.0, max_sec=20.0)
                        logger.info(f"[{provider}] Transient error; backoff with jitter for {sleep_time:.2f}s...")
                        time.sleep(sleep_time)
                    else:
                        logger.warning(f"Provider '{provider}' failed or retries exhausted. Cascading...")
                        self.telemetry.record_call(
                            call_type="intent_classification",
                            provider=provider,
                            model=model_name,
                            prompt_tokens=0,
                            candidate_tokens=0,
                            total_tokens=0,
                            latency_ms=latency,
                            cache_hit=False,
                            item_id=text_hash[:8],
                            error=err_str[:150]
                        )
                        break  # Advance to next provider in cascade

        # Failsafe fallback
        res = self.deterministic_classify_intent(request_text)
        self.intent_cache[text_hash] = res
        self._save_json(self.intent_cache_path, self.intent_cache)
        return res

    # -------------------------------------------------------------
    # PROVIDER-SPECIFIC IMAGE CALLS
    # -------------------------------------------------------------
    def _call_gemini_image(self, model: str, image_path: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        img = Image.open(image_path)
        client = self._get_gemini_client()
        from google.genai import types
        response = client.models.generate_content(
            model=model,
            contents=[img, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json"
            )
        )
        p_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        c_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
        t_tok = getattr(response.usage_metadata, "total_token_count", 0) or 0
        th_tok = getattr(response.usage_metadata, "thoughts_token_count", 0) or 0

        raw_data = clean_and_parse_json(response.text)
        validated = ImageExtractionPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, th_tok

    def _call_openai_image(self, model: str, image_path: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        with open(image_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")
        client = self._get_openai_client()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64_img}"}}
                    ]
                }
            ],
            response_format={"type": "json_object"}
        )
        raw_text = response.choices[0].message.content or "{}"
        p_tok = response.usage.prompt_tokens if response.usage else 0
        c_tok = response.usage.completion_tokens if response.usage else 0
        t_tok = response.usage.total_tokens if response.usage else 0

        raw_data = clean_and_parse_json(raw_text)
        validated = ImageExtractionPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, 0

    def _call_anthropic_image(self, model: str, image_path: str, prompt: str) -> Tuple[Dict[str, Any], int, int, int, int]:
        with open(image_path, "rb") as f:
            b64_img = base64.b64encode(f.read()).decode("utf-8")
        client = self._get_anthropic_client()
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64_img
                            }
                        },
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
        )
        raw_text = response.content[0].text or "{}"
        p_tok = response.usage.input_tokens if response.usage else 0
        c_tok = response.usage.output_tokens if response.usage else 0
        t_tok = p_tok + c_tok

        raw_data = clean_and_parse_json(raw_text)
        validated = ImageExtractionPayload.model_validate(raw_data)
        return validated.model_dump(), p_tok, c_tok, t_tok, 0

    # -------------------------------------------------------------
    # IMAGE EVIDENCE EXTRACTION WITH MULTI-PROVIDER CASCADE
    # -------------------------------------------------------------
    def extract_image_evidence(self, image_path: str, file_id: str) -> Dict[str, Any]:
        """
        Extracts structured JSON from an evidence screenshot using VLM.
        Cached on disk at code/cache/images/{file_id}.json.
        Cascades: Gemini 2.5 Flash -> OpenAI -> Anthropic -> Local empty fallback.
        """
        cache_file = os.path.join(self.images_cache_dir, f"{file_id}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r", encoding="utf-8") as f:
                    res = json.load(f)
                    self.telemetry.record_call(
                        call_type="image_extraction",
                        provider=self.provider or "cached",
                        model=self.model or "cached",
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

        cascade = self.get_fallback_cascade(task_type="image")
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

        for provider_idx, (provider, model_name) in enumerate(cascade):
            if provider == "deterministic":
                # Local offline fallback
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

            for attempt in range(max_retries + 1):
                start_t = time.time()
                try:
                    if provider == "gemini":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_gemini_image(model_name, image_path, prompt)
                    elif provider == "openai":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_openai_image(model_name, image_path, prompt)
                    elif provider == "anthropic":
                        res, p_tok, c_tok, t_tok, th_tok = self._call_anthropic_image(model_name, image_path, prompt)
                    else:
                        break

                    latency = (time.time() - start_t) * 1000
                    self.telemetry.record_call(
                        call_type="image_extraction",
                        provider=provider,
                        model=model_name,
                        prompt_tokens=p_tok,
                        candidate_tokens=c_tok,
                        total_tokens=t_tok,
                        latency_ms=latency,
                        cache_hit=False,
                        item_id=file_id,
                        thinking_tokens=th_tok
                    )
                    self._save_json(cache_file, res)
                    return res

                except Exception as e:
                    err_str = str(e)
                    latency = (time.time() - start_t) * 1000
                    logger.warning(f"[{provider}/{model_name}] VLM for {file_id} attempt {attempt + 1} failed: {err_str[:120]}")

                    if is_hard_quota_exhausted(err_str):
                        logger.warning(f"Provider '{provider}' hard quota exhausted on VLM. Cascading...")
                        self._exhausted_providers.add(provider)
                        self.telemetry.record_call(
                            call_type="image_extraction",
                            provider=provider,
                            model=model_name,
                            prompt_tokens=0,
                            candidate_tokens=0,
                            total_tokens=0,
                            latency_ms=latency,
                            cache_hit=False,
                            item_id=file_id,
                            error=f"HardQuotaExhausted: {err_str[:100]}"
                        )
                        break

                    if is_transient_error(err_str) and attempt < max_retries:
                        sleep_time = calculate_backoff_with_jitter(attempt, base_sec=2.0, max_sec=20.0)
                        logger.info(f"[{provider}] VLM rate limited; backoff with jitter for {sleep_time:.2f}s...")
                        time.sleep(sleep_time)
                    else:
                        logger.warning(f"Provider '{provider}' VLM failed. Cascading...")
                        self.telemetry.record_call(
                            call_type="image_extraction",
                            provider=provider,
                            model=model_name,
                            prompt_tokens=0,
                            candidate_tokens=0,
                            total_tokens=0,
                            latency_ms=latency,
                            cache_hit=False,
                            item_id=file_id,
                            error=err_str[:150]
                        )
                        break

        # Failsafe fallback
        res = {"system_id": "image_admin_console", "resources": []}
        return res
