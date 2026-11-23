# V2 Verification Targets & Defect Catalog

This document cataloges specific failure modes and naive behaviors observed during the **v1 baseline** run. As per protocol, **none of these defects are patched in v1**; v1 remains the frozen, unverified baseline so that the impact of automated verification loops in v2 can be rigorously evaluated.

---

### Defect 1: Third-Party Aggregator / Blog Evidence
- **Observation**: Naive web searches often return high-SEO third-party comparison sites, aggregators, or unofficial blogs instead of primary documentation.
- **Example App**:
  - **Salesforce** (ID 1): Cites `frontal.so/gtm-apis/crm-apis/salesforce-api-review` and `pipeline.zoominfo.com/sales/salesforce-api` as evidence for auth methods and API breadth.
  - **Twenty** (ID 5): Cites `mindcloud.co/docs/universal/rest/twenty/...` for API key authentication.
- **Targeted V2 Improvement**:
  - **Check A (Deterministic Domain Filter)**: Validate `is_official_domain` against vendor root domains and known documentation subdomains. Flag non-official sources.
  - **Check C (Targeted Domain Re-search)**: Re-issue Tavily queries with `include_domains=[vendor_domain, "docs." + vendor_domain, "developer." + vendor_domain]`.

---

### Defect 2: False-Positive Official MCP Server Attribution
- **Observation**: When an app's developer documentation mentions "partner app directory", "marketplace listing", or "app certification", the baseline extractor frequently conflates this with an official Model Context Protocol (MCP) server.
- **Example App**:
  - **HubSpot** (ID 2): Marked `official_mcp: "official"` citing *"Listing your app Apply for certification Provide testing credentials for your app"*.
- **Targeted V2 Improvement**:
  - **Check A (MCP Registry Cross-Check)**: Compare against `data/benchmarks/mcp_registry.jsonl`. Require `namespace_matches_vendor == True` before accepting `official_mcp: "official"`.
  - **Check B (LLM Verifier)**: Single-field verification prompt asking explicitly whether the cited quote proves an MCP server vs. general app store certification.

---

### Defect 3: Incomplete Auth Scheme Capture (Under-Reporting)
- **Observation**: The extractor captures only the first prominent authentication mechanism mentioned in top snippets (e.g., Bearer token) and misses OAuth2 or API key options.
- **Example App**:
  - **Salesforce** (ID 1): Extracted only `["bearer_token"]`, missing Connected App OAuth2 (`oauth2`).
- **Targeted V2 Improvement**:
  - **Check A (Composio Catalog Benchmark Cross-Check)**: Compare extracted `auth_methods` against `composio_catalog.jsonl`'s `auth_schemes`.
  - **Check C (Targeted Auth Search)**: Query specifically for `"<app> OAuth2 developer client ID"` if OAuth is absent but standard in the category.

---

### Defect 4: Ambiguous Access Model / Gating Information
- **Observation**: General pricing pages describe seat costs rather than API access tiers, causing the extractor to conservatively fall back to `access_model: "unknown"` with `needs_human: true`.
- **Example Apps**:
  - **Salesforce** (ID 1), **HubSpot** (ID 2), **Twenty** (ID 5).
- **Targeted V2 Improvement**:
  - **Check C (Gating-Focused Re-search)**: Query specific queries like `"<app> API tier restriction Enterprise only OR self-serve free sandbox"`.

---

### Defect 5: Exact Substring Quote Verification
- **Observation**: Some extracted quotes contain subtle tokenization or formatting discrepancies (e.g. whitespace or ellipsis) relative to raw page text.
- **Example App**:
  - **Pipedrive** (ID 3): Returned a quote exceeding 200 characters with formatting breaks.
- **Targeted V2 Improvement**:
  - **Check A (Deterministic String Search)**: Check that the normalized quote is an exact character-for-character substring of the fetched page text. If not found, mark quote invalid.

---

### Defect 6: Extraction Fallback on Format Rejection
- **Observation**: In 4 of the 100 apps, the fast fallback model rejected JSON formatting constraints with a 400 Client Error, triggering the automated fallback mechanism which marked all fields `unknown` with `needs_human: true`.
- **Example Apps**:
  - **Paygent Connect** (ID 84), **Ramp** (ID 89), **PitchBook** (ID 90), **Fathom** (ID 93).
- **Targeted V2 Improvement**:
  - **Check C (Targeted Re-research & Multi-Key Retry)**: Re-run extraction for these 4 apps through the verified multi-key 120B pool with clean prompt payload, resolving all fallbacks.

---

### Defect 7: High Unknown Rate on Gating & API Breadth
- **Observation**: Naive v1 searches for "API pricing access" frequently returned high-level enterprise marketing sheets that did not explicitly state developer sandbox rules or endpoint counts (49/100 apps left `access_model: unknown`; 60/100 left `api_breadth: unknown`).
- **Example Apps**:
  - **Pinterest** (ID 38), **Smartsheet** (ID 79), **Brex** (ID 88).
- **Targeted V2 Improvement**:
  - **Check C (Domain-Specific Re-search)**: Query targeted endpoints like `site:<domain> "create sandbox account" OR "API rate limits"`.

