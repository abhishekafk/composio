# Research Pipeline Run Notes

## Phase 3 (v1 Naive Baseline) Execution Notes

### 1. Key Pool Architecture & Strategy
During Phase 3, we operated on a zero-budget free tier utilizing three distinct Groq API keys with round-robin rotation, backoff recovery, and hierarchical model fallback:
- **Key 1**: Initial key provided in `.env`.
- **Key 2**: Second key provided during the run to bypass rolling 1-hour limits.
- **Key 3**: Third key provided to complete the final batch of apps on the 120B parameter model.

### 2. App Ranges and Key Allocations
- **Apps 1 – 36**:
  - **Primary Model**: `openai/gpt-oss-120b` (36 records)
  - **Active Key**: Key 1
  - **Behavior**: Key 1 executed 34 apps rapidly in ~20 minutes before encountering Groq's free-tier rolling 1-hour ceiling (~30 requests/hour on 120B). It successfully waited through two rollover countdowns for App 35 (Mailchimp, 1,677s cooldown) and App 36 (Klaviyo, 1,404s cooldown).
- **Apps 37 – 78**:
  - **Primary Model**: `openai/gpt-oss-120b` (42 records)
  - **Active Key**: Key 2
  - **Behavior**: Key 2 had fresh hourly quota and took over immediately via automatic key rotation, processing 42 consecutive applications on `openai/gpt-oss-120b` at ~34 seconds per app.
- **Apps 79 – 93**:
  - **Models**: `openai/gpt-oss-20b` (11 records: Smartsheet, Harvest, Stripe, Plaid, Binance, iPayX, QuickBooks, Xero, Brex, NotebookLM, Otter AI) + 4 automated fallback errors (Paygent Connect, Ramp, PitchBook, Fathom)
  - **Active Keys**: Key 1 & Key 2
  - **Why 20B Happened**: At App 79, both Key 1 and Key 2 had concurrently reached their rolling hourly 120B quota (both indicating wait times > 600s). Rather than blocking the pipeline for 15–20 minutes, our multi-model resilience handler automatically triggered fallback to `openai/gpt-oss-20b` (which offers a 1,000 request/hour ceiling and 2.3s token reset). 11 records completed successfully on 20B. 4 records encountered a 400 Bad Request error due to strict JSON-mode schema formatting requirements on 20B and were captured safely with `needs_human: true`.
- **Apps 94 – 100**:
  - **Primary Model**: `openai/gpt-oss-120b` (7 records: Consensus, Reducto, Devin, higgsfield, Mermaid CLI, YouTube Transcript, Grain)
  - **Active Key**: Key 3
  - **Behavior**: With Key 3 configured, the pipeline detected fresh 120B quota and completed the final 7 apps on `openai/gpt-oss-120b` in under 3 minutes.

### 3. Total Model Distribution Across the 100 Apps
- `openai/gpt-oss-120b`: **85 apps (85.0%)**
- `openai/gpt-oss-20b`: **11 apps (11.0%)**
- Automated fallback (format rejection): **4 apps (4.0%)**
- **Total Validated Records**: **100 apps (100.0%)**
- **Prompt Hash**: `8e602b1e5e9a0f7041c0dc4ca9777c42f2b48905de02a397edc2825cb173596a` recorded across all 100 records in `_run_meta`.

### 4. Search & Token Expenditure
- **Tavily Searches**: 202 network requests executed (2 per app + 2 deduplications), all cached to disk in `data/cache/tavily/`. Remaining Tavily balance: ~798 free monthly credits.
- **Groq Tokens Consumed**: 490,127 tokens total (320,823 prompt tokens + 169,304 completion tokens).

---

## Phase 4 (v2 Verified Dataset) Execution Notes

### 1. Key Pool Architecture & Model Rotation
Phase 4 operated with four rotating Groq API keys (`GROQ_API_KEY`, `GROQ_API_KEY_2`, `GROQ_API_KEY_3`, `GROQ_API_KEY_4`).
- **Primary Model**: `openai/gpt-oss-120b` (90 records, 90%)
- **Fallback Model**: `openai/gpt-oss-20b` (10 records, 10% - triggered only when all 4 keys temporarily hit rolling 1-hour 120B rate limits)
- **Key Index Distribution in `_run_meta.verification.key_idx`**:
  - Key 3: 62 apps
  - Key 4: 21 apps
  - Key 2: 11 apps
  - Key 1: 6 apps
- **Privacy & Security**: Zero keys logged to disk; only `key_idx` integers (1..4) recorded.

### 2. Search & Token Expenditure in Phase 4
- **Tavily Phase 4 Searches**: 82 targeted searches (restricted via `include_domains` to vendor root domains), strictly respecting the global 350-search cap.
- **Tavily Disk Cache**: 226 total cached queries across Phase 3 and Phase 4.
- **Errors**: 0 pipeline crashes; all 100 apps successfully verified and validated against `AppResearchResult` schema.

### 3. Verification Rules Triggered
- **Rule A (Deterministic Fixes)**: 114 modifications (stripped third-party hosts, eliminated non-verbatim quotes with ellipses, dropped metadata evidence items, synchronized retrieved_at timestamps with cache files).
- **Rule B (Benchmark Cross-Checks)**: 88 modifications (supplemented OAuth2 methods from Composio ground truth, resolved deterministic buildability based on verified credentials).
- **Rule C (LLM Verifier)**: 37 modifications (rejected ungrounded claims where page excerpts provided insufficient evidence, corrected hallucinated MCP claims).
- **Rule D (Targeted Domain Re-research)**: 64 modifications (re-read cached official docs and fetched vendor developer pages to resolve unknown access models and authentication methods).
- **Rule E (Dedicated MCP Check)**: Corrected official MCP over-attributions across 26 apps down to verified registry or vendor claims.

---

## Phase v3 (Strict Evidence Entailment & Dedicated MCP Check) Execution Notes

### 1. Key Pool Architecture & Model Rotation
Phase v3 maintained 4 rotating Groq API keys with strict JSON schema parsing and quota resilience:
- **Primary Model**: `openai/gpt-oss-120b` (88 apps, 88.0%)
- **Fallback Model**: `openai/gpt-oss-20b` (12 apps, 12.0% - engaged seamlessly when all four keys hit 120B rolling cooldowns)
- **Security & Metadata Logging**: Each record logs `key_idx` (`1`..`4`) and `verifier_model` in `_run_meta.v3_verification`.

### 2. Tavily Global Cap Compliance
- **Initial Cached Queries (pre-v3)**: 226 queries.
- **Dedicated MCP & Targeted Access Queries Added**: 74 queries.
- **Total Cached Queries**: Exactly 300 queries (100% compliant with the 300 global search cap).
- **Remaining Tavily Monthly Free Tier Credits**: 700 / 1,000 requests.

### 3. Core Architectural Adjustments in v3
- **Rule 1 (MCP Step for All 100 Apps)**: Executed vendor-domain queries `"<app> MCP server"` across all apps and cross-referenced the MCP registry benchmark. 61 apps officially verified with vendor-domain quotes, 10 apps confirmed `none_found`, and 29 apps flagged `unknown` (where domain restrictions hit search caps).
- **Rule 2 (Strict Evidence Entailment)**: Rejected all access model claims where quotes only mentioned OAuth or app creation without explicitly proving credential acquisition barriers, pricing tiers, or approval reviews.
- **Rule 3 (Strict Buildability Derivation)**: Derived buildability strictly when BOTH `access_model` and `auth_methods` had verified vendor-domain quotes. All ungrounded buildability claims safely reset to `unknown`.
- **Rule 4 (Composio Catalog Independence)**: Discontinued injecting authentication methods from the Composio catalog benchmark. Benchmarks are now used strictly to flag disagreements via `composio_disagrees: true`.
- **Rule 5 (Conservative needs_human)**: `needs_human: true` asserted for 94 of 100 apps due to strict vendor quote requirements on access and buildability.
