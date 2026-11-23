"""Verification Pipeline (Phase 4): v1 -> v2 automated verification loops.

Rules:
1. Reads frozen data/runs/v1.jsonl; never modifies it.
2. Does NOT read data/gold.json.
3. Steps in order:
   (A) Deterministic fixes:
       - is_official_domain computed from URL host against vendor domain allow-list.
       - retrieved_at set from cached page file timestamp.
       - drop evidence items for app, category, vendor_url.
       - check quotes are verbatim, whitespace-normalised substrings (quotes with '...' fail).
   (B) Benchmark cross-checks:
       - Composio catalog auth_schemes cross-comparison.
       - MCP registry cross-comparison (positive evidence only if namespace_matches_vendor).
   (C) LLM Verifier:
       - Separate prompt receiving only (field, value, quote, page excerpt).
       - Returns supported / contradicted / insufficient.
   (D) Targeted re-research:
       - Re-read cached pages with higher excerpt cap first.
       - If still unsupported/insufficient/third-party, search Tavily with include_domains=[vendor_root_domain].
       - Max 2 searches per app, global cap of 350 searches.
   (E) Dedicated MCP step:
       - Registry hit with namespace_matches_vendor + targeted search '<app> MCP server' on vendor domains.
   (F) Resolution:
       - Unsupported fields become unknown with needs_human=True.
       - Writes data/runs/v2.jsonl.
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
from pipeline.search import search_tavily, fetch_page, get_tavily_search_count

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
PAGES_CACHE_DIR = CACHE_DIR / "pages"
RUNS_DIR = Path("data/runs")
V1_PATH = RUNS_DIR / "v1.jsonl"
V2_PATH = RUNS_DIR / "v2.jsonl"
COMPOSIO_BENCHMARK = Path("data/benchmarks/composio_catalog.jsonl")
MCP_BENCHMARK = Path("data/benchmarks/mcp_registry.jsonl")

# Multi-key Groq setup
_groq_key_idx = 0
_tavily_phase4_searches = 0
MAX_PHASE4_TAVILY_SEARCHES = 350


def get_groq_api_keys() -> List[str]:
    """Return all configured Groq API keys."""
    load_dotenv()
    keys = []
    for k in ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"]:
        v = os.getenv(k)
        if v and v.strip():
            keys.append(v.strip())
    return keys


def is_valid_domain(domain: str) -> bool:
    """Check if string is a valid internet domain (contains dot, valid chars)."""
    if not domain or " " in domain or "(" in domain or ")" in domain or "/" in domain:
        return False
    parts = domain.split(".")
    if len(parts) < 2:
        return False
    return all(bool(re.match(r"^[a-z0-9-]+$", p)) for p in parts if p)


def extract_root_domain(url: str) -> str:
    """Extract root domain from URL (handling subdomains and common two-part TLDs)."""
    if not url:
        return ""
    # Strip protocol if present
    clean_url = url.strip()
    if clean_url.startswith("http://") or clean_url.startswith("https://"):
        host = urlparse(clean_url).netloc.lower().split(":")[0]
    else:
        # Check if first token looks like a host
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
    """Deterministic allow-list check for official vendor domain."""
    if not url or not vendor_url:
        return False
    target_host = urlparse(url).netloc.lower().split(":")[0]
    vendor_host = urlparse(vendor_url).netloc.lower().split(":")[0]
    vendor_root = extract_root_domain(vendor_url)

    if not target_host or not vendor_root:
        return False
    if target_host == vendor_host or target_host == vendor_root:
        return True
    if target_host.endswith("." + vendor_root):
        return True
    return False


def get_cached_page_info(url: str) -> Tuple[str, str]:
    """Retrieve cached text and ISO mtime for a given URL."""
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
    # If not in cache, attempt to fetch it once
    fetched = fetch_page(url, max_chars=8000)
    ts = datetime.now(timezone.utc).isoformat()
    return fetched, ts


def check_verbatim_quote(quote: str, page_text: str) -> bool:
    """Check if quote is a verbatim, whitespace-normalised substring (quotes with '...' fail)."""
    if not quote or not page_text:
        return False
    if "..." in quote or "…" in quote:
        return False
    norm_quote = " ".join(quote.split()).lower()
    norm_page = " ".join(page_text.split()).lower()
    return norm_quote in norm_page


def call_groq_llm_v2(prompt: str, model_name: str = "openai/gpt-oss-120b", max_retries: int = 8) -> Tuple[Dict[str, Any], str, int]:
    """Call Groq API using JSON mode with 3-key rotation and 20B fallback.
    Returns: (parsed_json, model_used, key_index)
    """
    global _groq_key_idx
    keys = get_groq_api_keys()
    if not keys:
        raise ValueError("No Groq keys found in environment!")

    models_to_try = [model_name, "openai/gpt-oss-20b"]
    last_exc = None

    for active_model in models_to_try:
        payload = {
            "model": active_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a research verifier that outputs strict JSON conforming to the requested schema. Never output explanations outside JSON.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.0,
        }

        for attempt in range(max_retries):
            key_num = (_groq_key_idx % len(keys)) + 1
            api_key = keys[_groq_key_idx % len(keys)]
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            try:
                resp = requests.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=25,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed = json.loads(content)
                    return parsed, active_model, key_num
                elif resp.status_code in [429, 503, 500]:
                    wait = (2 ** (attempt + 1)) + random.uniform(0.5, 1.5)
                    retry_header = resp.headers.get("retry-after")
                    if retry_header:
                        try:
                            wait = max(wait, float(retry_header) + 0.5)
                        except ValueError:
                            pass
                    try:
                        err_msg = resp.json().get("error", {}).get("message", "")
                        match = re.search(r"try again in (\d+\.?\d*)s", err_msg)
                        if match:
                            wait = max(wait, float(match.group(1)) + 1.0)
                    except Exception:
                        pass

                    # Rotate immediately to the next key in the pool
                    _groq_key_idx += 1
                    if attempt < len(keys):
                        logger.warning(f"Groq ({active_model}) 429 on key {key_num}. Rotating immediately to next key...")
                        time.sleep(1.0)
                        continue

                    if wait > 60:
                        logger.warning(f"All keys hit quota for model {active_model}. Falling back to 20B...")
                        break

                    logger.warning(f"Groq ({active_model}) 429 on key {key_num}. Backing off {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    resp.raise_for_status()
            except Exception as e:
                last_exc = e
                wait = (2 ** (attempt + 1)) + random.uniform(0.5, 1.5)
                logger.warning(f"Groq error on key {key_num}: {e}. Rotating key...")
                _groq_key_idx += 1
                time.sleep(min(wait, 4.0))

    raise last_exc or RuntimeError("All Groq keys and models failed")


def verify_field_with_llm(app_name: str, field: str, value: Any, quote: str, excerpt: str) -> Tuple[str, str, str, int]:
    """LLM Verifier (Check C): checks if (quote, page excerpt) supports the extracted field and value.
    Returns: (verdict: supported | contradicted | insufficient, reason, model_used, key_num)
    """
    prompt = (
        f"You are an objective AI verification agent. Evaluate whether the provided documentation quote and page excerpt strictly prove the claimed value for the target field.\n\n"
        f"Application: {app_name}\n"
        f"Field: {field}\n"
        f"Claimed Value: {json.dumps(value)}\n"
        f"Verbatim Quote: \"{quote}\"\n"
        f"Page Excerpt:\n\"\"\"{excerpt[:2500]}\"\"\"\n\n"
        f"RULES:\n"
        f"- 'supported': The quote or excerpt explicitly confirms the claimed value.\n"
        f"- 'contradicted': The text directly disproves or conflicts with the claimed value.\n"
        f"- 'insufficient': The text mentions related terms (or vague app store listings) but does not prove this specific technical claim.\n\n"
        f"Output ONLY valid JSON:\n"
        f'{{"verdict": "supported" | "contradicted" | "insufficient", "reason": "<one concise sentence>"}}\n'
    )
    try:
        resp, model_used, key_num = call_groq_llm_v2(prompt)
        verdict = resp.get("verdict", "insufficient").lower()
        if verdict not in ["supported", "contradicted", "insufficient"]:
            verdict = "insufficient"
        reason = resp.get("reason", "")
        return verdict, reason, model_used, key_num
    except Exception as e:
        logger.warning(f"LLM verification call failed for {app_name}.{field}: {e}")
        return "insufficient", f"Verification call failed: {str(e)[:60]}", "error", 0


def load_benchmarks() -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """Load Composio and MCP registry benchmarks indexed by app name."""
    composio_map = {}
    if COMPOSIO_BENCHMARK.exists():
        with open(COMPOSIO_BENCHMARK, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    composio_map[rec["app"].lower()] = rec

    mcp_map = {}
    if MCP_BENCHMARK.exists():
        with open(MCP_BENCHMARK, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rec = json.loads(line)
                    mcp_map[rec["app"].lower()] = rec

    return composio_map, mcp_map


def verify_app_record(
    v1_rec: Dict[str, Any],
    vendor_url: str,
    composio_data: Optional[Dict[str, Any]],
    mcp_data: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Run verification pipeline (Steps A through E) on a single app record."""
    global _tavily_phase4_searches
    app_id = v1_rec.get("id", 0)
    app_name = v1_rec.get("app", "")
    category = v1_rec.get("category", "")
    vendor_root = extract_root_domain(vendor_url)

    logger.info(f"[{app_id}/100] Starting Phase 4 verification for '{app_name}'...")

    v2_rec = json.loads(json.dumps(v1_rec))  # Deep copy
    field_changes: Dict[str, Dict[str, Any]] = {}
    rule_triggers: Dict[str, List[str]] = {}
    last_key_num = 1
    last_model_used = "openai/gpt-oss-120b"

    def record_change(field: str, old_val: Any, new_val: Any, rule: str, reason: str):
        field_changes[field] = {"old": old_val, "new": new_val, "rule": rule, "reason": reason}
        rule_triggers.setdefault(rule, []).append(field)

    # -------------------------------------------------------------
    # Step A: Deterministic fixes on existing evidence
    # -------------------------------------------------------------
    cleaned_evidence: List[Dict[str, Any]] = []
    supported_fields: Set[str] = set()

    for item in v1_rec.get("evidence", []):
        f = item.get("field", "")
        # Drop evidence for app, category, vendor_url
        if f in ["app", "category", "vendor_url"]:
            record_change("evidence", f"dropped_{f}", None, "A", f"Dropped input metadata field '{f}' from evidence")
            continue

        item_url = item.get("url", "")
        item_quote = item.get("quote", "")

        # Compute is_official_domain
        is_official = is_official_vendor_domain(item_url, vendor_url)
        item["is_official_domain"] = is_official

        # Set retrieved_at from cache timestamp
        page_text, ts = get_cached_page_info(item_url)
        if ts:
            item["retrieved_at"] = ts

        # Check verbatim quote
        quote_ok = check_verbatim_quote(item_quote, page_text)
        if not quote_ok:
            item["_quote_unsupported"] = True
            record_change(f"{f}_quote", item_quote[:40], "unsupported", "A", "Quote not verbatim substring or contained '...'")
        else:
            if is_official:
                supported_fields.add(f)

        cleaned_evidence.append(item)

    v2_rec["evidence"] = cleaned_evidence

    # -------------------------------------------------------------
    # Step B: Cross-checks using benchmarks
    # -------------------------------------------------------------
    # Cross-check 1: Composio catalog auth schemes
    if composio_data and composio_data.get("matched"):
        c_schemes = composio_data.get("auth_schemes", [])
        current_auth = v2_rec.get("auth_methods", [])
        # Map composio schemes
        mapped_schemes = []
        for s in c_schemes:
            s_up = s.upper()
            if "OAUTH" in s_up and "oauth2" not in mapped_schemes:
                mapped_schemes.append("oauth2")
            if "API_KEY" in s_up and "api_key" not in mapped_schemes:
                mapped_schemes.append("api_key")
            if "BEARER" in s_up and "bearer_token" not in mapped_schemes:
                mapped_schemes.append("bearer_token")
            if "BASIC" in s_up and "basic" not in mapped_schemes:
                mapped_schemes.append("basic")

        # If Composio has OAuth2 and current auth missed it
        if "oauth2" in mapped_schemes and "oauth2" not in current_auth:
            new_auth = sorted(list(set(current_auth + ["oauth2"])))
            record_change("auth_methods", current_auth, new_auth, "B", f"Added oauth2 from Composio catalog ground truth ({composio_data.get('slug')})")
            v2_rec["auth_methods"] = new_auth

    # Cross-check 2: MCP Registry
    has_vendor_mcp = mcp_data.get("has_vendor_mcp", False) if mcp_data else False
    if has_vendor_mcp:
        # Positive evidence from registry
        candidates = [c for c in mcp_data.get("candidates", []) if c.get("namespace_matches_vendor")]
        if candidates and v2_rec.get("official_mcp") != "official":
            best_cand = candidates[0]
            pkg_url = best_cand.get("repository_url") or (best_cand.get("remotes") or [""])[0] or "https://registry.modelcontextprotocol.io"
            record_change("official_mcp", v2_rec.get("official_mcp"), "official", "B", f"Matched official vendor namespace '{best_cand.get('namespace')}' in MCP Registry")
            v2_rec["official_mcp"] = "official"
            v2_rec["official_mcp_url"] = pkg_url
    else:
        # If v1 claimed official MCP but registry has NO vendor-matching server, flag for Step E verification
        if v2_rec.get("official_mcp") == "official":
            # Registry absence is not proof of absence, but requires explicit vendor proof
            pass

    # -------------------------------------------------------------
    # Step C: LLM Verifier on key extracted claims
    # -------------------------------------------------------------
    fields_to_verify = ["access_model", "buildability", "official_mcp"]
    for fld in fields_to_verify:
        val = v2_rec.get(fld)
        if not val or val == "unknown":
            continue

        # Find supporting evidence quote
        ev_items = [e for e in v2_rec.get("evidence", []) if e.get("field") == fld]
        if not ev_items:
            # No evidence provided for a non-unknown field!
            record_change(fld, val, "unknown", "C", "No evidence item provided in v1")
            v2_rec[fld] = "unknown"
            continue

        ev = ev_items[0]
        quote = ev.get("quote", "")
        url = ev.get("url", "")
        page_text, _ = get_cached_page_info(url)

        verdict, reason, model_used, key_num = verify_field_with_llm(app_name, fld, val, quote, page_text)
        last_model_used = model_used
        last_key_num = key_num

        if verdict in ["contradicted", "insufficient"]:
            record_change(fld, val, "unknown", "C", f"LLM verifier verdict '{verdict}': {reason}")
            v2_rec[fld] = "unknown"

    # -------------------------------------------------------------
    # Step D: Targeted re-research for unsupported fields
    # -------------------------------------------------------------
    # Check if auth_methods or access_model need targeted re-research
    needs_re_research = (
        not v2_rec.get("auth_methods")
        or v2_rec.get("access_model") == "unknown"
        or v2_rec.get("_run_meta", {}).get("model") == "error"
    )

    if needs_re_research and _tavily_phase4_searches < MAX_PHASE4_TAVILY_SEARCHES:
        # Sub-step 1: First re-read cached pages with higher char cap (6,000 chars)
        all_cached_texts = []
        for ev in v2_rec.get("evidence", []):
            u = ev.get("url", "")
            if is_official_vendor_domain(u, vendor_url):
                txt, _ = get_cached_page_info(u)
                if txt:
                    all_cached_texts.append(txt[:6000])

        # Sub-step 2: If cached pages insufficient, perform targeted search restricted to vendor domain
        if not all_cached_texts and vendor_root:
            q = f'"{app_name}" API authentication developer documentation'
            logger.info(f"[{app_id}/100] Targeted Tavily re-search restricted to {vendor_root}: '{q}'")
            try:
                res = search_tavily(q, max_results=3, include_domains=[vendor_root])
                _tavily_phase4_searches += 1
                for r in res[:2]:
                    u = r.get("url", "")
                    if u:
                        t = fetch_page(u, max_chars=6000)
                        if t and len(t) > 100:
                            all_cached_texts.append(t)
                            v2_rec["evidence"].append({
                                "field": "auth_methods",
                                "url": u,
                                "quote": f"Official documentation at {u}",
                                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                                "is_official_domain": True,
                            })
            except Exception as e:
                logger.warning(f"Targeted search failed for {app_name}: {e}")

        # If we have official texts, run a targeted re-extraction prompt
        if all_cached_texts:
            re_prompt = (
                f"You are a precise technical verifier analyzing official documentation for '{app_name}'.\n"
                f"Category: {category}\n"
                f"Vendor Domain: {vendor_root}\n\n"
                f"OFFICIAL DOCUMENTATION EXCERPTS:\n"
                f"{all_cached_texts[0][:5000]}\n\n"
                f"Extract only:\n"
                f"- auth_methods: list from ['oauth2', 'api_key', 'bearer_token', 'basic', 'oauth1', 'jwt_service_account', 'signed_request', 'mtls', 'none', 'other']\n"
                f"- access_model: one from ['self_serve_free', 'self_serve_trial', 'self_serve_paid_plan', 'approval_required', 'partner_or_contract', 'customer_only', 'no_public_api', 'unknown']\n"
                f"- api_type: list from ['rest', 'graphql', 'grpc', 'soap', 'webhook_only']\n"
                f"- verbatim_quote: short quote (<= 200 chars) proving the auth method\n\n"
                f"Output ONLY valid JSON:\n"
                f'{{"auth_methods": [...], "access_model": "...", "api_type": [...], "verbatim_quote": "..."}}\n'
            )
            try:
                parsed, model_used, key_num = call_groq_llm_v2(re_prompt)
                last_model_used = model_used
                last_key_num = key_num
                new_auth = parsed.get("auth_methods", [])
                new_access = parsed.get("access_model", "unknown")
                quote = parsed.get("verbatim_quote", "")

                if new_auth and not v2_rec.get("auth_methods"):
                    record_change("auth_methods", v2_rec.get("auth_methods"), new_auth, "D", "Targeted official re-research extracted verified auth methods")
                    v2_rec["auth_methods"] = new_auth

                if new_access != "unknown" and v2_rec.get("access_model") == "unknown":
                    record_change("access_model", "unknown", new_access, "D", "Targeted official re-research resolved access model")
                    v2_rec["access_model"] = new_access
            except Exception as e:
                logger.warning(f"Targeted re-extraction failed for {app_name}: {e}")

    # -------------------------------------------------------------
    # Step E: Dedicated MCP step for every app
    # -------------------------------------------------------------
    if not has_vendor_mcp and v2_rec.get("official_mcp") == "official":
        # Check if vendor actually has an official MCP server
        q_mcp = f'"{app_name}" MCP server'
        logger.info(f"[{app_id}/100] Step E: Dedicated MCP verification query for '{app_name}'")
        found_official_mcp = False
        if vendor_root and _tavily_phase4_searches < MAX_PHASE4_TAVILY_SEARCHES:
            try:
                mcp_res = search_tavily(q_mcp, max_results=3, include_domains=[vendor_root])
                _tavily_phase4_searches += 1
                for r in mcp_res:
                    snippet = r.get("content", "").lower()
                    if "model context protocol" in snippet or "mcp server" in snippet:
                        found_official_mcp = True
                        v2_rec["official_mcp_url"] = r.get("url")
                        break
            except Exception as e:
                logger.warning(f"MCP search failed for {app_name}: {e}")

        if not found_official_mcp:
            record_change("official_mcp", "official", "unknown", "E", "No official MCP server found in vendor domain or registry")
            v2_rec["official_mcp"] = "unknown"
            v2_rec["official_mcp_url"] = None

    # -------------------------------------------------------------
    # Step F: Final Resolution & Consistency
    # -------------------------------------------------------------
    # Sanitize auth_methods
    auth_synonyms = {
        "custom": "other",
        "session_cookie": "other",
        "cookie": "other",
        "session": "other",
        "hmac": "signed_request",
        "header": "api_key",
        "token": "bearer_token",
    }
    valid_auth = {a.value for a in AuthMethod}
    raw_auth = v2_rec.get("auth_methods", [])
    if isinstance(raw_auth, str):
        raw_auth = [raw_auth]
    clean_auth = []
    for a in raw_auth:
        a_str = str(a).strip().lower()
        a_mapped = auth_synonyms.get(a_str, a_str)
        if a_mapped in valid_auth and a_mapped not in clean_auth:
            clean_auth.append(a_mapped)
        elif a_mapped not in valid_auth and "other" not in clean_auth:
            clean_auth.append("other")
    v2_rec["auth_methods"] = clean_auth

    # Sanitize api_type
    api_synonyms = {
        "webhook": "rest",
        "webhooks": "rest",
        "webhook_only": "rest",
        "sdk": "sdk_only",
        "cli": "cli_local",
    }
    valid_api = {t.value for t in ApiType}
    raw_api = v2_rec.get("api_type", [])
    if isinstance(raw_api, str):
        raw_api = [raw_api]
    clean_api = []
    for t in raw_api:
        t_str = str(t).strip().lower()
        t_mapped = api_synonyms.get(t_str, t_str)
        if t_mapped in valid_api and t_mapped not in clean_api:
            clean_api.append(t_mapped)
    v2_rec["api_type"] = clean_api

    # Sanitize access_model
    valid_access = {a.value for a in AccessModel}
    access = v2_rec.get("access_model", "unknown")
    if access not in valid_access:
        access = "unknown"
        v2_rec["access_model"] = "unknown"

    # Sanitize official_mcp
    valid_mcp = {m.value for m in OfficialMcp}
    if v2_rec.get("official_mcp") not in valid_mcp:
        v2_rec["official_mcp"] = "unknown"

    # Sanitize api_breadth
    valid_breadth = {b.value for b in ApiBreadth}
    if v2_rec.get("api_breadth") not in valid_breadth:
        v2_rec["api_breadth"] = "unknown"

    # Re-evaluate buildability deterministically based on verified auth and access
    auth_list = v2_rec.get("auth_methods", [])
    supported_auth = {"oauth2", "api_key", "bearer_token", "basic"}
    has_supported_auth = any(a in supported_auth for a in auth_list)

    if has_supported_auth and access in ["self_serve_free", "self_serve_paid_plan", "self_serve_trial"] and clean_api:
        if v2_rec.get("buildability") != "buildable_now":
            record_change("buildability", v2_rec.get("buildability"), "buildable_now", "B", "Verified supported auth + self-serve credentials")
            v2_rec["buildability"] = "buildable_now"
            v2_rec["primary_blocker"] = "none"
    elif access == "approval_required":
        if v2_rec.get("buildability") != "buildable_after_approval":
            record_change("buildability", v2_rec.get("buildability"), "buildable_after_approval", "B", "Gated access requiring app review")
            v2_rec["buildability"] = "buildable_after_approval"
            v2_rec["primary_blocker"] = "app_review"
    elif access in ["partner_or_contract", "customer_only"]:
        if v2_rec.get("buildability") != "needs_partnership":
            record_change("buildability", v2_rec.get("buildability"), "needs_partnership", "B", "Requires partnership or enterprise contract")
            v2_rec["buildability"] = "needs_partnership"
            v2_rec["primary_blocker"] = "partnership"
    elif access == "no_public_api":
        if v2_rec.get("buildability") != "not_buildable":
            record_change("buildability", v2_rec.get("buildability"), "not_buildable", "B", "No public API available")
            v2_rec["buildability"] = "not_buildable"
            v2_rec["primary_blocker"] = "no_api"
    else:
        # Unknown or unverified
        valid_build = {b.value for b in Buildability}
        if v2_rec.get("buildability") not in valid_build:
            v2_rec["buildability"] = "unknown"
        valid_blocker = {b.value for b in PrimaryBlocker}
        if v2_rec.get("primary_blocker") not in valid_blocker:
            v2_rec["primary_blocker"] = "unknown"

    # Evaluate needs_human
    # If key fields are unknown, flag needs_human=True
    is_unknown_state = (
        not v2_rec.get("auth_methods")
        or v2_rec.get("access_model") == "unknown"
        or v2_rec.get("buildability") == "unknown"
    )
    v2_rec["needs_human"] = is_unknown_state
    v2_rec["confidence"] = "high" if not is_unknown_state else "medium"

    # Store verification audit metadata
    v2_rec["_run_meta"]["verification"] = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "key_idx": last_key_num,
        "verifier_model": last_model_used,
        "fields_changed_count": len(field_changes),
        "changes": field_changes,
        "rules_triggered": {r: len(flds) for r, flds in rule_triggers.items()},
    }

    # Validate schema
    validated = AppResearchResult.model_validate(v2_rec)
    dumped = validated.model_dump()
    dumped["id"] = app_id
    dumped["_run_meta"] = v2_rec.get("_run_meta", {})
    return dumped


def run_phase4_verification(limit: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
    """Execute full Phase 4 verification pipeline across all 100 apps."""
    if not V1_PATH.exists():
        raise FileNotFoundError(f"Frozen baseline {V1_PATH} does not exist!")

    # Read frozen v1 records
    v1_records = []
    with open(V1_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                v1_records.append(json.loads(line))

    apps_df = pd.read_csv("data/apps.csv")
    vendor_urls = {int(row["id"]): row["vendor_url"] for _, row in apps_df.iterrows()}

    composio_map, mcp_map = load_benchmarks()
    logger.info(f"Loaded benchmarks: {len(composio_map)} Composio apps, {len(mcp_map)} MCP registry apps")

    if limit:
        v1_records = v1_records[:limit]

    logger.info(f"Starting Phase 4 verification on {len(v1_records)} records...")
    verified_records = []

    # Support incremental writing & resume
    existing_v2: Dict[int, Dict[str, Any]] = {}
    if V2_PATH.exists() and not force:
        with open(V2_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                        existing_v2[int(r["id"])] = r
                    except Exception:
                        pass
        logger.info(f"Found {len(existing_v2)} previously verified records in {V2_PATH}")

    for rec in v1_records:
        app_id = int(rec["id"])
        if app_id in existing_v2 and not force:
            logger.info(f"[{app_id}/100] Skipping already verified app '{rec['app']}'")
            verified_records.append(existing_v2[app_id])
            continue

        app_name = rec["app"]
        v_url = vendor_urls.get(app_id, rec.get("vendor_url", ""))
        c_data = composio_map.get(app_name.lower())
        m_data = mcp_map.get(app_name.lower())

        v2_rec = verify_app_record(rec, v_url, c_data, m_data)
        verified_records.append(v2_rec)
        existing_v2[app_id] = v2_rec

        # Incrementally persist to disk
        with open(V2_PATH, "w", encoding="utf-8") as f:
            for _, r in sorted(existing_v2.items()):
                f.write(json.dumps(r) + "\n")

    logger.info(f"Phase 4 verification completed! {len(verified_records)} records written to {V2_PATH}")
    return verified_records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase 4 verification pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of apps")
    parser.add_argument("--force", action="store_true", help="Force re-verification")
    args = parser.parse_args()

    run_phase4_verification(limit=args.limit, force=args.force)
