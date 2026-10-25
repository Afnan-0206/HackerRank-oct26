# LLM / VLM Telemetry & Usage Report

## 1. Final Production Run Telemetry (`python code/main.py`)
This section covers telemetry strictly for the final production execution of `python code/main.py` across all 518 cases in `dataset/deletion_requests.csv`:

- **Execution Command**: `python code/main.py --dataset-dir dataset --output-csv output.csv`
- **Total Requests Evaluated**: 518 cases
- **Persistent Disk Cache Hits**: 518 (100.0%)
- **Actual Remote Network API Calls**: 0 (0.0%)
- **Input (Prompt) Tokens**: 0 tokens
- **Output (Candidate) Tokens**: 0 tokens
- **Thinking / Reasoning Tokens**: 0 tokens
- **Total Network Tokens Consumed**: 0 tokens
- **Tokens Per Case (Over 518 Cases)**: **0.00 tokens/case** (100% served from local persistent disk cache)
- **Estimated Cost**: **$0.0000 USD**
- **Runtime & Throughput**: ~8.39 seconds (61.7 cases/second)

---

## 2. Separate Diagnostic Runs & Historical Cache-Building Activity

### A. Cache-Cold No-Key Diagnostic Run (Offline Verification)
To verify behavior when caches are completely absent and no API keys are provided:
- **Environment**: Isolated clean room without `intent_cache.json` or `images/` cache; outbound network sockets blocked.
- **Provider / Model**: Default configuration with credentials unset (`provider="deterministic"`, `model="rule_based_fallback"`).
- **Execution**: Evaluated all 518 cases entirely via local rule-based intent and parser logic.
- **Outcome**: 0 network calls attempted, 0 tokens consumed, $0.00 cost. Produced identical status and step outputs across all 518 cases.
- **Terminology Note**: Local deterministic fallback decisions are internal rule evaluations; they are not network calls or external model invocations.

### B. Prior Cache-Building & Offline Preparation Runs
This section covers the historical preparation activity that populated disk caches (`code/cache/`):

| Component | Provider / Mode | Total Unique Texts | Gemini API Classified | Deterministic Fallback | Notes |
|---|---|---|---|---|---|
| **Image Evidence (VLM)** | `gemini-2.5-flash` | 11 PNGs | 11 | 0 | All 11 screenshots parsed to disk cache |
| **Intent Classification** | Gemini + Local Fallback | 392 texts | **5** | **387** | 387 used deterministic fallback after `429 RESOURCE_EXHAUSTED` (20 req/day limit) |

- **Synthetic Intent Suite**: All synthetic multilingual intent test results were evaluated via the deterministic rule-based fallback unless specifically produced by Gemini.
- **No Model-Rule Consensus**: We explicitly avoid claiming "model and rule consensus" because 387 of the 392 intent texts were classified solely by the deterministic fallback engine due to API quota exhaustion.

---

## 3. Pricing Configuration (Unverified Reference Estimate)
Rates are configured in [`code/pricing_config.json`](file:///code/pricing_config.json) as unverified reference estimates:
- **Pricing Status**: **Unverified** (reference rates derived from published documentation, not verified via live billing API responses)
- **Reference URL**: [https://ai.google.dev/pricing](https://ai.google.dev/pricing)
- **Reference Date**: October 2024 published tier
- **Gemini 2.5 Flash Input Rate**: $0.075 per 1,000,000 prompt tokens (unverified reference)
- **Gemini 2.5 Flash Output Rate**: $0.30 per 1,000,000 candidate tokens (unverified reference)
- **Deterministic Rule Fallbacks & Cache Reads**: $0.00 (Zero cost)

---

## 4. Telemetry Analysis & Technical Explanations

### Why `total_tokens ≠ prompt_tokens + candidate_tokens`
In our telemetry records (e.g. `image_extraction: P: 452, C: 612, Total: 2,996`):
1. **Multimodal Image Modality Tokens**: In the Gemini VLM architecture, `prompt_tokens` in the raw response metadata counts only textual tokens in the prompt instructions. High-resolution screenshot PNGs are transformed into multimodal image embeddings by the vision encoder, which consume vision tokens (1,932 to 2,544 tokens per image). The Gemini API reports `total_token_count = prompt_text_tokens + image_modality_tokens + candidates_token_count`.
2. **Internal Reasoning Tokens**: In Gemini 2.0/2.5 thinking-capable models, internal chain-of-thought tokens are accounted under total tokens, while candidates reflect the emitted JSON payload.

### Explanation of the 679 Recorded Intent Network Attempt Entries
Across `dataset/deletion_requests.csv`, there are **375 unique request texts** across 518 cases, plus 17 synthetic benchmark test requests, totaling **392 unique evaluation texts**.

During initial pipeline development before disk caching was unified across scripts:
1. Scripts made initial calls to the Gemini API. Exactly **5 calls succeeded** and returned Gemini intent classifications.
2. Immediately following the 5th call, the Gemini API returned HTTP `429 RESOURCE_EXHAUSTED` due to the free tier quota limit of **20 requests/day**.
3. Telemetry trackers logged repeated retry attempts across concurrent test scripts (totalling 679 logged attempt entries) before the deterministic fallback caught the exception and populated the remaining **387 cache entries** offline.
4. Therefore, exactly 5 entries in the intent cache were Gemini-classified, and 387 entries were produced by the deterministic rule fallback. Subsequent runs, including the final production run, operate at **100% cache hit rate (0 network calls)**.
