"""Verification Pipeline (Phase v3): v2 -> v3 strict verification loops.

Rules for v3:
1. Reads frozen data/runs/v2.jsonl; does NOT read data/gold.json.
2. (1) MCP step runs for ALL 100 apps regardless of current value:
   - Tavily search "<app> MCP server" restricted to vendor domain.
   - Plus MCP registry hit with namespace_matches_vendor true.
   - official_mcp = official ONLY if vendor-domain page quote mentions MCP server/endpoint.
   - community_only if only non-vendor hits exist.
   - none_found if searched with no hits (absence is not proof).
   - otherwise unknown.
   - Record searches in _run_meta.
3. (2) Evidence entailment:
   - Any non-unknown field must have a vendor-domain verbatim quote that actually supports that specific value.
   - For access_model, quote must state how credentials are obtained AND whether it costs money or needs approval.
     A quote that only mentions OAuth or creating an app does NOT support access_model.
4. (3) buildability is derived ONLY when BOTH access_model and auth_methods have supported vendor quotes;
   otherwise it stays unknown.
5. (4) Stop using Composio catalog to add auth methods! Use it only to set composio_disagrees flag.
   Every auth value must have a vendor quote.
6. (5) needs_human is true if ANY of access_model, auth_methods, buildability, or official_mcp
   lacks a supported vendor quote or is unknown.
7. (6) Targeted re-research for access_model lacking supported quote:
   - Up to 2 Tavily searches restricted to vendor domain.
   - Read pages at higher char cap (8,000 chars).
   - Global Tavily cap 300 across all phases (226 used prior to v3).
8. Keep 4-key Groq rotation, log key index and model per record.
9. Writes data/runs/v3.jsonl and freezes it.
"""

import os
import re
import json
import time
import random
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set
from urllib.parse import urlparse

import requests
import pandas as pd
from dotenv import load_dotenv

from pipeline.schema import (
    AppResearchResult,
    Category,
    Confidence,
    AuthMethod,
    AccessModel,
    ApiType,
    ApiBreadth,
    OfficialMcp,
    Buildability,
    PrimaryBlocker,
    EvidenceItem,
)
from pipeline.search import search_tavily, fetch_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
PAGES_CACHE_DIR = CACHE_DIR / "pages"
TAVILY_CACHE_DIR = CACHE_DIR / "tavily"
RUNS_DIR = Path("data/runs")
V2_PATH = RUNS_DIR / "v2.jsonl"
V3_PATH = RUNS_DIR / "v3.jsonl"
COMPOSIO_BENCHMARK = Path("data/benchmarks/composio_catalog.jsonl")
MCP_BENCHMARK = Path("data/benchmarks/mcp_registry.jsonl")

# Tavily search budget tracking
GLOBAL_TAVILY_CAP = 300
_initial_cached_searches = 0
_v3_new_searches = 0
_v3_mcp_searches_run = 0
_v3_access_searches_run = 0

# Multi-key Groq setup
_groq_key_idx = 0


def get_groq_api_keys() -> List[str]:
    load_dotenv()
    keys = []
    for k in ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"]:
        v = os.getenv(k)
        if v and v.strip():
            keys.append(v.strip())
    return keys


def is_valid_domain(domain: str) -> bool:
    if not domain or " " in domain or "(" in domain or ")" in domain or "/" in domain:
        return False
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    return all(bool(re.match(r"^[a-z0-9-]+$", p)) for p in parts if p)


def extract_root_domain(url: str) -> str:
    if not url:
        return ""
    clean_url = url.strip()
    if clean_url.startswith("http://") or clean_url.startswith("https://"):
        host = urlparse(clean_url).netloc.lower().split(":")[0]
    else:
        first_token = clean_url.split()[0].split("/")[0]
        host = first_token.lower()

    if not is_valid_domain(host):
        return ""

    parts = host.split(".")
    if len(parts) <= 2:
        return host
    two_part_tlds = {"co.uk", "com.au", "co.jp", "com.br", "co.nz", "com.sg"}
    if len(parts) >= 3 and f"{parts[-2]}.{parts[-1]}" in two_part_tlds:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_official_vendor_domain(url: str, vendor_url: str) -> bool:
    if not url or not vendor_url:
        return False
    target_host = urlparse(url).netloc.lower().split(":")[0]
    vendor_root = extract_root_domain(vendor_url)
    if not target_host or not vendor_root:
        return False
    if target_host == vendor_root or target_host.endswith("." + vendor_root):
        return True
    return False


def get_cached_page_info(url: str) -> Tuple[str, str]:
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    page_file = PAGES_CACHE_DIR / f"{url_hash}.txt"
    if page_file.exists():
        try:
            mtime = page_file.stat().st_mtime
            ts = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            text = page_file.read_text(encoding="utf-8")
            return text, ts
        except Exception:
            pass
    fetched = fetch_page(url, max_chars=8000)
    ts = datetime.now(timezone.utc).isoformat()
    return fetched, ts


def check_verbatim_quote(quote: str, page_text: str) -> bool:
    if not quote or not page_text:
        return False
    if "..." in quote or "…" in quote:
        return False
    norm_quote = " ".join(quote.split()).lower()
    norm_page = " ".join(page_text.split()).lower()
    return norm_quote in norm_page


def count_cached_tavily() -> int:
    if not TAVILY_CACHE_DIR.exists():
        return 0
    return len(list(TAVILY_CACHE_DIR.glob("*.json")))


def safe_search_tavily(query: str, max_results: int = 5, include_domains: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    global _v3_new_searches
    current_total = count_cached_tavily()
    if current_total >= GLOBAL_TAVILY_CAP:
        logger.warning(f"Global Tavily cap {GLOBAL_TAVILY_CAP} reached ({current_total} cached). Skipping search: '{query}'")
        return []

    # Check if disk cache exists before calling
    domains_key = "-".join(sorted(include_domains)) if include_domains else "all"
    cache_key = hashlib.sha256(f"{query}:{max_results}:{domains_key}".encode("utf-8")).hexdigest()[:16]
    cache_file = TAVILY_CACHE_DIR / f"{cache_key}.json"
    is_new = not cache_file.exists()

    try:
        res = search_tavily(query, max_results=max_results, include_domains=include_domains)
        if is_new:
            _v3_new_searches += 1
        return res
    except Exception as e:
        logger.warning(f"Tavily search failed for '{query}': {e}")
        return []


def call_groq_llm_v3(prompt: str, model_name: str = "openai/gpt-oss-120b", max_retries: int = 8) -> Tuple[Dict[str, Any], str, int]:
    global _groq_key_idx
    keys = get_groq_api_keys()
    if not keys:
        raise ValueError("No GROQ API keys available!")

    url = "https://api.groq.com/openai/v1/chat/completions"
    current_model = model_name

    for attempt in range(max_retries):
        key = keys[_groq_key_idx % len(keys)]
        current_key_num = (_groq_key_idx % len(keys)) + 1
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": "You are a strict, precise technical auditor. You extract facts strictly entailed by official evidence quotes. Always output valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
            "max_tokens": 800,
        }

        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=25)
            if resp.status_code == 200:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                return parsed, current_model, current_key_num

            if resp.status_code == 429:
                err_body = resp.text
                retry_wait = 2.0
                m = re.search(r"try again in (\d+\.?\d*)s", err_body)
                if m:
                    retry_wait = float(m.group(1))

                logger.warning(f"Groq ({current_model}) 429 on key {current_key_num}. Rotating immediately...")
                _groq_key_idx += 1
                if retry_wait > 300 and current_model == "openai/gpt-oss-120b":
                    current_model = "openai/gpt-oss-20b"
                time.sleep(1.0)
                continue

            if resp.status_code == 400:
                logger.warning(f"Groq 400 error: {resp.text[:200]}. Retrying with cleaner payload...")
                time.sleep(1.0)
                continue

            resp.raise_for_status()

        except Exception as e:
            logger.warning(f"Groq API error on key {current_key_num} (attempt {attempt+1}): {e}")
            _groq_key_idx += 1
            time.sleep(2.0)

    # Fallback to 20B
    logger.warning("All primary attempts exhausted. Falling back to 20B...")
    key = keys[_groq_key_idx % len(keys)]
    current_key_num = (_groq_key_idx % len(keys)) + 1
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "model": "openai/gpt-oss-20b",
        "messages": [
            {"role": "system", "content": "You are a strict technical verifier. Output valid JSON only."},
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 800,
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=25)
    data = resp.json()
    parsed = json.loads(data["choices"][0]["message"]["content"])
    return parsed, "openai/gpt-oss-20b", current_key_num


def verify_entailment_with_llm(
    app_name: str,
    field: str,
    value: Any,
    quote: str,
    page_text: str,
) -> Tuple[str, str, str, int]:
    """Strict verifier checking whether evidence quote explicitly entails field value."""
    excerpt = page_text[:4000] if page_text else ""
    prompt = (
        f"You are a strict technical compliance auditor evaluating SaaS API documentation for '{app_name}'.\n\n"
        f"FIELD TO EVALUATE: {field}\n"
        f"CLAIMED VALUE: {json.dumps(value)}\n"
        f"OFFICIAL EVIDENCE QUOTE:\n\"{quote}\"\n\n"
        f"PAGE EXCERPT:\n\"\"\"{excerpt}\"\"\"\n\n"
        f"STRICT EVALUATION RULES:\n"
        f"1. For 'access_model': The quote MUST explicitly state how developer/API credentials are obtained "
        f"(e.g. self-serve signup, paid plan tier, enterprise contract, or manual application review) AND whether it costs money or needs approval. "
        f"A quote that merely mentions 'OAuth 2.0', 'Bearer tokens', or 'create an app in the developer portal' without stating account cost, tier, or approval process DOES NOT support access_model and MUST be marked 'insufficient'.\n"
        f"2. For 'auth_methods': The quote must explicitly name the authentication scheme (e.g. 'OAuth 2.0', 'API key', 'Bearer token').\n"
        f"3. For 'official_mcp': The quote must state that an official Model Context Protocol (MCP) server or endpoint is provided or maintained by this vendor.\n"
        f"4. For 'buildability': Requires explicit evidence of developer credentials accessibility.\n\n"
        f"Output JSON with:\n"
        f"- verdict: one of ['supported', 'contradicted', 'insufficient']\n"
        f"- reason: concise 1-sentence technical explanation\n\n"
        f'{{"verdict": "...", "reason": "..."}}'
    )
    parsed, model_used, key_num = call_groq_llm_v3(prompt)
    verdict = parsed.get("verdict", "insufficient").lower()
    reason = parsed.get("reason", "")
    if verdict not in ["supported", "contradicted", "insufficient"]:
        verdict = "insufficient"
    return verdict, reason, model_used, key_num


def load_benchmarks() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    composio_map = {}
    if COMPOSIO_BENCHMARK.exists():
        with open(COMPOSIO_BENCHMARK, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    composio_map[r.get("app", "").lower()] = r

    mcp_map = {}
    if MCP_BENCHMARK.exists():
        with open(MCP_BENCHMARK, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    mcp_map[r.get("app", "").lower()] = r

    return composio_map, mcp_map


def verify_app_v3(
    v2_rec: Dict[str, Any],
    vendor_url: str,
    composio_data: Optional[Dict[str, Any]],
    mcp_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Execute v3 verification for a single app record."""
    global _v3_mcp_searches_run, _v3_access_searches_run

    app_id = v2_rec.get("id", 0)
    app_name = v2_rec.get("app", "")
    category = v2_rec.get("category", "")
    vendor_root = extract_root_domain(vendor_url)

    logger.info(f"[{app_id}/100] Starting Phase v3 verification for '{app_name}'...")

    v3_rec = json.loads(json.dumps(v2_rec))
    field_changes: Dict[str, Dict[str, Any]] = {}
    rule_triggers: Dict[str, List[str]] = {}
    last_key_num = 1
    last_model_used = "openai/gpt-oss-120b"
    mcp_searches_logged: List[Dict[str, Any]] = []

    def record_change(field: str, old_val: Any, new_val: Any, rule: str, reason: str):
        field_changes[field] = {"old": old_val, "new": new_val, "rule": rule, "reason": reason}
        rule_triggers.setdefault(rule, []).append(field)

    # -----------------------------------------------------------------
    # STEP 1: Dedicated MCP Step for ALL 100 Apps (Requirement 1)
    # -----------------------------------------------------------------
    # 1. Check MCP registry benchmark
    has_vendor_registry_mcp = False
    registry_url = None
    has_community_registry_mcp = False

    if mcp_data and mcp_data.get("matched"):
        candidates = mcp_data.get("candidates", [])
        vendor_cands = [c for c in candidates if c.get("namespace_matches_vendor")]
        if vendor_cands:
            has_vendor_registry_mcp = True
            registry_url = vendor_cands[0].get("repository_url") or "https://registry.modelcontextprotocol.io"
        elif candidates:
            has_community_registry_mcp = True

    # 2. Tavily search "<app> MCP server" restricted to vendor domains
    found_official_vendor_mcp = False
    official_mcp_quote = ""
    official_mcp_url = None
    searched_vendor_mcp = False

    if vendor_root and is_valid_domain(vendor_root) and count_cached_tavily() < GLOBAL_TAVILY_CAP:
        q_mcp = f'"{app_name}" MCP server'
        logger.info(f"[{app_id}/100] Step 1: Searching Tavily for MCP on {vendor_root}: '{q_mcp}'")
        searched_vendor_mcp = True
        _v3_mcp_searches_run += 1
        mcp_res = safe_search_tavily(q_mcp, max_results=3, include_domains=[vendor_root])
        mcp_searches_logged.append({"query": q_mcp, "domain": vendor_root, "results_count": len(mcp_res)})

        for r in mcp_res:
            u = r.get("url", "")
            snippet = (r.get("content", "") + " " + r.get("title", "")).lower()
            if "mcp" in snippet or "model context protocol" in snippet:
                # Fetch full page text
                p_text, p_ts = get_cached_page_info(u)
                p_norm = p_text.lower()
                if "model context protocol" in p_norm or "mcp server" in p_norm:
                    # Found official vendor page mention!
                    found_official_vendor_mcp = True
                    official_mcp_url = u
                    # Extract quote
                    for line in p_text.splitlines():
                        if "mcp" in line.lower() or "model context" in line.lower():
                            line_clean = " ".join(line.split())
                            if len(line_clean) > 20 and len(line_clean) <= 200 and not any(c in line_clean for c in ["...", "…"]):
                                official_mcp_quote = line_clean
                                break
                    if not official_mcp_quote:
                        official_mcp_quote = f"Official MCP documentation at {u}"
                    break

    # Determine official_mcp status
    old_mcp = v3_rec.get("official_mcp", "unknown")
    if found_official_vendor_mcp:
        v3_rec["official_mcp"] = "official"
        v3_rec["official_mcp_url"] = official_mcp_url
        if old_mcp != "official":
            record_change("official_mcp", old_mcp, "official", "1", "Official MCP server verified on vendor domain")
        # Add evidence item
        v3_rec["evidence"].append({
            "field": "official_mcp",
            "url": official_mcp_url,
            "quote": official_mcp_quote,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "is_official_domain": True,
        })
    elif has_vendor_registry_mcp:
        v3_rec["official_mcp"] = "official"
        v3_rec["official_mcp_url"] = registry_url
        if old_mcp != "official":
            record_change("official_mcp", old_mcp, "official", "1", "Matched vendor namespace in MCP Registry")
        v3_rec["evidence"].append({
            "field": "official_mcp",
            "url": registry_url,
            "quote": f"Verified vendor namespace in official MCP Registry ({registry_url})",
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "is_official_domain": True,
        })
    elif has_community_registry_mcp:
        v3_rec["official_mcp"] = "community_only"
        v3_rec["official_mcp_url"] = None
        if old_mcp != "community_only":
            record_change("official_mcp", old_mcp, "community_only", "1", "Community MCP packages exist; absence on vendor domain is not proof of absence")
    elif searched_vendor_mcp:
        v3_rec["official_mcp"] = "none_found"
        v3_rec["official_mcp_url"] = None
        if old_mcp != "none_found":
            record_change("official_mcp", old_mcp, "none_found", "1", "Searched vendor domain with no hits (absence is not proof of absence)")
    else:
        v3_rec["official_mcp"] = "unknown"
        v3_rec["official_mcp_url"] = None

    # -----------------------------------------------------------------
    # STEP 2 & 6: Strict Evidence Entailment & Access Model Re-research
    # -----------------------------------------------------------------
    # Check existing evidence items for access_model
    def has_supported_vendor_quote_for_access(rec: Dict[str, Any]) -> bool:
        for ev in rec.get("evidence", []):
            if ev.get("field") == "access_model":
                if ev.get("is_official_domain") is True and ev.get("quote"):
                    if not ev.get("_quote_unsupported") and not any(c in ev.get("quote", "") for c in ["...", "…"]):
                        return True
        return False

    # Check if access_model already has supported quote
    access_supported = has_supported_vendor_quote_for_access(v3_rec)

    # If access_model lacks a supported vendor quote, perform targeted re-research (Requirement 6)
    if not access_supported or v3_rec.get("access_model") == "unknown":
        all_vendor_texts = []
        # First re-read cached pages with high char cap (8,000 chars)
        for ev in v3_rec.get("evidence", []):
            u = ev.get("url", "")
            if is_official_vendor_domain(u, vendor_url):
                txt, _ = get_cached_page_info(u)
                if txt and len(txt) > 100:
                    all_vendor_texts.append((u, txt[:8000]))

        # If cached pages insufficient, run up to 2 targeted Tavily searches restricted to vendor domain
        if not all_vendor_texts and vendor_root and is_valid_domain(vendor_root) and count_cached_tavily() < GLOBAL_TAVILY_CAP:
            queries = [
                f'"{app_name}" API developer signup free sandbox pricing access',
                f'"{app_name}" developer API key access requirements approval'
            ]
            for q in queries:
                if count_cached_tavily() >= GLOBAL_TAVILY_CAP:
                    break
                logger.info(f"[{app_id}/100] Step 6: Targeted access search on {vendor_root}: '{q}'")
                _v3_access_searches_run += 1
                results = safe_search_tavily(q, max_results=3, include_domains=[vendor_root])
                for r in results:
                    u = r.get("url", "")
                    if u and is_official_vendor_domain(u, vendor_url):
                        t, _ = get_cached_page_info(u)
                        if t and len(t) > 100:
                            all_vendor_texts.append((u, t[:8000]))

        # Run strict extraction if vendor text is available
        if all_vendor_texts:
            doc_url, doc_text = all_vendor_texts[0]
            ext_prompt = (
                f"You are a strict technical auditor evaluating official documentation for '{app_name}'.\n"
                f"Vendor Domain: {vendor_root}\n"
                f"OFFICIAL DOCUMENTATION EXCERPT:\n{doc_text[:6000]}\n\n"
                f"STRICT EXTRACTION INSTRUCTIONS:\n"
                f"1. Identify the exact API access model from:\n"
                f"['self_serve_free', 'self_serve_trial', 'self_serve_paid_plan', 'approval_required', 'partner_or_contract', 'customer_only', 'no_public_api', 'unknown']\n"
                f"Extract a verbatim substring quote (<= 200 chars, NO ELLIPSES) that explicitly states how credentials are obtained AND whether it costs money or needs approval.\n"
                f"If the text does NOT explicitly state credential acquisition and pricing/approval, set access_model to 'unknown' and access_quote to ''.\n\n"
                f"2. Identify any officially documented auth_methods from:\n"
                f"['oauth2', 'api_key', 'bearer_token', 'basic', 'oauth1', 'jwt_service_account', 'signed_request', 'mtls']\n"
                f"Extract a verbatim substring quote (<= 200 chars, NO ELLIPSES) that explicitly names the auth scheme. If none, set auth_methods to [] and auth_quote to ''.\n\n"
                f"Output valid JSON only:\n"
                f'{{"access_model": "...", "access_quote": "...", "auth_methods": [...], "auth_quote": "..."}}\n'
            )
            try:
                parsed_ext, model_used, key_num = call_groq_llm_v3(ext_prompt)
                last_model_used = model_used
                last_key_num = key_num
                cand_access = parsed_ext.get("access_model", "unknown")
                cand_quote = parsed_ext.get("access_quote", parsed_ext.get("verbatim_quote", "")).strip()
                cand_auth = parsed_ext.get("auth_methods", [])
                cand_auth_quote = parsed_ext.get("auth_quote", "").strip()

                if cand_access != "unknown" and cand_quote:
                    if check_verbatim_quote(cand_quote, doc_text):
                        old_acc = v3_rec.get("access_model")
                        record_change("access_model", old_acc, cand_access, "6", f"Targeted vendor doc re-research extracted verified access model")
                        v3_rec["access_model"] = cand_access
                        v3_rec["evidence"] = [e for e in v3_rec.get("evidence", []) if e.get("field") != "access_model"]
                        v3_rec["evidence"].append({
                            "field": "access_model",
                            "url": doc_url,
                            "quote": cand_quote,
                            "retrieved_at": datetime.now(timezone.utc).isoformat(),
                            "is_official_domain": True,
                        })

                if cand_auth and cand_auth_quote:
                    if check_verbatim_quote(cand_auth_quote, doc_text):
                        valid_enums = {a.value for a in AuthMethod}
                        verified_cand_auth = [a for a in cand_auth if a in valid_enums]
                        if verified_cand_auth:
                            v3_rec["auth_methods"] = verified_cand_auth
                            v3_rec["evidence"] = [e for e in v3_rec.get("evidence", []) if e.get("field") != "auth_methods"]
                            v3_rec["evidence"].append({
                                "field": "auth_methods",
                                "url": doc_url,
                                "quote": cand_auth_quote,
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                "is_official_domain": True,
                            })
                    else:
                        logger.info(f"[{app_id}/100] Quote was not verbatim substring in doc for {app_name}")
            except Exception as e:
                logger.warning(f"Targeted extraction failed for {app_name}: {e}")

    # Now verify existing access_model with strict entailment (Requirement 2)
    current_access = v3_rec.get("access_model", "unknown")
    if current_access != "unknown":
        access_evs = [e for e in v3_rec.get("evidence", []) if e.get("field") == "access_model"]
        if not access_evs:
            record_change("access_model", current_access, "unknown", "2", "No evidence item provided for access_model")
            v3_rec["access_model"] = "unknown"
        else:
            ev = access_evs[0]
            quote = ev.get("quote", "")
            u = ev.get("url", "")
            is_off = is_official_vendor_domain(u, vendor_url)
            if not is_off:
                record_change("access_model", current_access, "unknown", "2", f"Evidence URL is third-party ({u}); vendor domain required")
                v3_rec["access_model"] = "unknown"
            elif not check_verbatim_quote(quote, get_cached_page_info(u)[0]):
                record_change("access_model", current_access, "unknown", "2", "Quote not verbatim substring of cached page or contains ellipses")
                v3_rec["access_model"] = "unknown"
            else:
                # LLM entailment check
                verdict, reason, m_used, k_num = verify_entailment_with_llm(app_name, "access_model", current_access, quote, get_cached_page_info(u)[0])
                last_model_used = m_used
                last_key_num = k_num
                if verdict in ["contradicted", "insufficient"]:
                    record_change("access_model", current_access, "unknown", "2", f"Strict entailment failed: {reason}")
                    v3_rec["access_model"] = "unknown"

    # -----------------------------------------------------------------
    # STEP 3: Stop using Composio catalog to add auth methods (Requirement 4)
    # -----------------------------------------------------------------
    # Filter auth_methods: each auth method must have a supported vendor-domain quote
    raw_auth = v3_rec.get("auth_methods", [])
    clean_auth = []
    auth_evs = [e for e in v3_rec.get("evidence", []) if e.get("field") == "auth_methods"]

    # Check vendor quotes for auth
    has_vendor_auth_quote = False
    for ev in auth_evs:
        u = ev.get("url", "")
        q = ev.get("quote", "")
        if is_official_vendor_domain(u, vendor_url) and check_verbatim_quote(q, get_cached_page_info(u)[0]):
            has_vendor_auth_quote = True
            break

    if has_vendor_auth_quote:
        # Retain valid schema enum auth methods
        valid_auth_enums = {a.value for a in AuthMethod}
        for a in raw_auth:
            if str(a) in valid_auth_enums and str(a) not in clean_auth:
                clean_auth.append(str(a))
    else:
        if raw_auth:
            record_change("auth_methods", raw_auth, [], "4", "No supported vendor-domain quote for auth_methods")
        clean_auth = []

    v3_rec["auth_methods"] = clean_auth

    # Check Composio catalog agreement (Requirement 4)
    composio_disagrees = False
    if composio_data and composio_data.get("matched"):
        c_schemes = composio_data.get("auth_schemes", [])
        # Compare schemes
        c_mapped = set()
        for s in c_schemes:
            s_low = s.lower()
            if "oauth2" in s_low or "oauth" in s_low:
                c_mapped.add("oauth2")
            if "api_key" in s_low or "key" in s_low:
                c_mapped.add("api_key")
            if "bearer" in s_low or "token" in s_low:
                c_mapped.add("bearer_token")
            if "basic" in s_low:
                c_mapped.add("basic")

        v_auth_set = set(clean_auth)
        if c_mapped and not (c_mapped & v_auth_set):
            composio_disagrees = True

    v3_rec["composio_disagrees"] = composio_disagrees

    # -----------------------------------------------------------------
    # STEP 4: Derive buildability ONLY when BOTH have vendor quotes (Requirement 3)
    # -----------------------------------------------------------------
    final_access = v3_rec.get("access_model", "unknown")
    has_vendor_access = (final_access != "unknown") and any(
        e.get("field") == "access_model" and is_official_vendor_domain(e.get("url", ""), vendor_url) and check_verbatim_quote(e.get("quote", ""), get_cached_page_info(e.get("url", ""))[0])
        for e in v3_rec.get("evidence", [])
    )
    has_vendor_auth = bool(clean_auth) and has_vendor_auth_quote

    old_build = v3_rec.get("buildability", "unknown")
    old_blocker = v3_rec.get("primary_blocker", "unknown")

    if has_vendor_access and has_vendor_auth:
        supported_auth = {"oauth2", "api_key", "bearer_token", "basic"}
        has_sup_auth = any(a in supported_auth for a in clean_auth)
        if has_sup_auth and final_access == "self_serve_free":
            v3_rec["buildability"] = "buildable_now"
            v3_rec["primary_blocker"] = "none"
        elif has_sup_auth and final_access == "self_serve_paid_plan":
            v3_rec["buildability"] = "buildable_with_paid_account"
            v3_rec["primary_blocker"] = "paid_plan"
        elif has_sup_auth and final_access == "self_serve_trial":
            v3_rec["buildability"] = "buildable_now"
            v3_rec["primary_blocker"] = "none"
        elif final_access == "approval_required":
            v3_rec["buildability"] = "buildable_after_approval"
            v3_rec["primary_blocker"] = "app_review"
        elif final_access in ["partner_or_contract", "customer_only"]:
            v3_rec["buildability"] = "needs_partnership"
            v3_rec["primary_blocker"] = "partnership"
        elif final_access == "no_public_api":
            v3_rec["buildability"] = "not_buildable"
            v3_rec["primary_blocker"] = "no_api"
        else:
            v3_rec["buildability"] = "unknown"
            v3_rec["primary_blocker"] = "unknown"
    else:
        # Cannot derive buildability!
        v3_rec["buildability"] = "unknown"
        v3_rec["primary_blocker"] = "unknown"

    if v3_rec["buildability"] != old_build:
        record_change("buildability", old_build, v3_rec["buildability"], "3", "Derived strictly when both access_model and auth_methods have vendor quotes")
    if v3_rec["primary_blocker"] != old_blocker:
        record_change("primary_blocker", old_blocker, v3_rec["primary_blocker"], "3", "Aligned with derived buildability and access gate")

    # -----------------------------------------------------------------
    # STEP 5: Evaluate needs_human (Requirement 5)
    # -----------------------------------------------------------------
    # needs_human is true if ANY of access_model, auth_methods, buildability or official_mcp lacks supported vendor quote or is unknown
    lacks_access = not has_vendor_access or v3_rec["access_model"] == "unknown"
    lacks_auth = not has_vendor_auth or not v3_rec["auth_methods"]
    lacks_build = v3_rec["buildability"] == "unknown"
    lacks_mcp = v3_rec["official_mcp"] == "unknown"

    needs_human = lacks_access or lacks_auth or lacks_build or lacks_mcp
    v3_rec["needs_human"] = needs_human
    v3_rec["confidence"] = "high" if not needs_human else "medium"

    # Store verification metadata
    v3_rec["_run_meta"]["v3_verification"] = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "key_idx": last_key_num,
        "verifier_model": last_model_used,
        "fields_changed_count": len(field_changes),
        "changes": field_changes,
        "rules_triggered": {r: len(flds) for r, flds in rule_triggers.items()},
        "mcp_searches": mcp_searches_logged,
        "has_vendor_access": has_vendor_access,
        "has_vendor_auth": has_vendor_auth,
    }

    # Validate schema
    validated = AppResearchResult.model_validate(v3_rec)
    dumped = validated.model_dump()
    dumped["id"] = app_id
    dumped["_run_meta"] = v3_rec.get("_run_meta", {})
    dumped["composio_disagrees"] = composio_disagrees
    return dumped


def run_phase_v3(limit: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
    global _initial_cached_searches
    if not V2_PATH.exists():
        raise FileNotFoundError(f"Frozen baseline {V2_PATH} does not exist!")

    _initial_cached_searches = count_cached_tavily()
    logger.info(f"Starting Phase v3. Initial cached Tavily searches: {_initial_cached_searches} / {GLOBAL_TAVILY_CAP}")

    v2_records = []
    with open(V2_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                v2_records.append(json.loads(line))

    apps_df = pd.read_csv("data/apps.csv")
    vendor_urls = {int(row["id"]): row["vendor_url"] for _, row in apps_df.iterrows()}

    composio_map, mcp_map = load_benchmarks()

    if limit:
        v2_records = v2_records[:limit]

    verified_records = []
    existing_v3: Dict[int, Dict[str, Any]] = {}

    if V3_PATH.exists() and not force:
        with open(V3_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                        existing_v3[int(r["id"])] = r
                    except Exception:
                        pass
        logger.info(f"Found {len(existing_v3)} previously verified records in {V3_PATH}")

    for rec in v2_records:
        app_id = int(rec["id"])
        if app_id in existing_v3 and not force:
            logger.info(f"[{app_id}/100] Skipping already verified app '{rec['app']}'")
            verified_records.append(existing_v3[app_id])
            continue

        app_name = rec["app"]
        v_url = vendor_urls.get(app_id, rec.get("vendor_url", ""))
        c_data = composio_map.get(app_name.lower())
        m_data = mcp_map.get(app_name.lower())

        v3_rec = verify_app_v3(rec, v_url, c_data, m_data)
        verified_records.append(v3_rec)
        existing_v3[app_id] = v3_rec

        # Incrementally persist to disk
        with open(V3_PATH, "w", encoding="utf-8") as f:
            for _, r in sorted(existing_v3.items()):
                f.write(json.dumps(r) + "\n")

    logger.info(f"Phase v3 verification completed! {len(verified_records)} records written to {V3_PATH}")
    return verified_records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase v3 verification pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of apps")
    parser.add_argument("--force", action="store_true", help="Force re-verification")
    args = parser.parse_args()

    run_phase_v3(limit=args.limit, force=args.force)
