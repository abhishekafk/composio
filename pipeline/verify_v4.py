"""Verification Pipeline (Phase v4): Zero-Credit Link Crawler & Chunk-Ranked Verification.

Rules for v4:
1. Reads frozen data/runs/v3.jsonl; does NOT read data/gold.json.
2. Link Discovery:
   - Starts at vendor docs host from apps.csv vendor_url.
   - Checks /llms.txt and /sitemap.xml on vendor docs host before keyword matching on docs home.
   - Checks robots.txt on the host and records which hosts blocked automated access.
   - Parses links strictly within the official vendor domain.
   - Categorizes links into: auth, getting started, pricing, mcp (drops /overview).
   - Fetches up to 5 pages per app, taking at least one from each category before a second from any.
3. Page Fetching:
   - Direct requests.get with polite user-agent and 10s timeout.
   - If requests fails, times out, or returns empty/JS-only page (< 300 chars or JS shell), falls back to https://r.jina.ai/<url>.
   - Records which hosts needed Jina fallback in _run_meta.
   - Caches all fetched pages in data/cache/pages/<hash>.txt.
   - Consumes 0 Tavily search credits!
4. Chunking & Ranking:
   - Splits fetched pages into text chunks (~800 chars, 150 overlap).
   - Ranks chunks by keyword hits: ["authenticat", "token", "api key", "oauth", "pricing", "plan", "free", "trial", "approval", "mcp"].
   - Passes top ranked chunks (up to ~6,000 chars) to LLM.
5. Strict Entailment & Derivation:
   - Strict verbatim vendor-domain quotes (no '...', verbatim substring).
   - access_model quote must state credential acquisition AND cost/approval.
   - auth_methods quote must name the scheme.
   - official_mcp quote must prove vendor-provided MCP server.
   - buildability derived ONLY when BOTH access_model and auth_methods have supported vendor quotes.
   - needs_human = true if any of access_model, auth_methods, buildability, or official_mcp lacks supported quote or is unknown.
6. Writes data/runs/v4.jsonl.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple, Set
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET

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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
PAGES_CACHE_DIR = CACHE_DIR / "pages"
RUNS_DIR = Path("data/runs")
V3_PATH = RUNS_DIR / "v3.jsonl"
V4_PATH = RUNS_DIR / "v4.jsonl"

_groq_key_idx = 4  # Start with fresh 5th key
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# Crawler metrics across the run
_crawler_stats = {
    "robots_blocked_hosts": set(),
    "jina_fallback_hosts": set(),
    "pages_fetched_per_app": {},
}


def get_groq_api_keys() -> List[str]:
    load_dotenv()
    keys = []
    for k in ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4", "GROQ_API_KEY_5"]:
        v = os.getenv(k)
        if v and v.strip():
            keys.append(v.strip())
    return keys


def is_valid_domain(domain: str) -> bool:
    if not domain or any(c in domain for c in [" ", "(", ")", "/", "\\", ":"]):
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


def check_robots_blocked(host: str) -> bool:
    """Check robots.txt for disallow all on automated agents."""
    robots_url = f"https://{host}/robots.txt"
    try:
        r = requests.get(robots_url, headers={"User-Agent": USER_AGENT}, timeout=4)
        if r.status_code == 200:
            lines = r.text.lower().splitlines()
            agent_star = False
            for line in lines:
                line = line.strip()
                if line.startswith("user-agent:") and "*" in line:
                    agent_star = True
                elif line.startswith("user-agent:"):
                    agent_star = False
                elif agent_star and line.startswith("disallow:") and line.split(":", 1)[1].strip() == "/":
                    return True
    except Exception:
        pass
    return False


def fetch_url_with_jina_fallback(url: str) -> Tuple[str, bool]:
    """Fetch URL directly with requests, falling back to r.jina.ai on failure or JS shell.

    Returns: (text, used_jina_fallback)
    """
    PAGES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    page_file = PAGES_CACHE_DIR / f"{url_hash}.txt"

    if page_file.exists():
        try:
            return page_file.read_text(encoding="utf-8"), False
        except Exception:
            pass

    used_jina = False
    text = ""
    host = urlparse(url).netloc.lower().split(":")[0]

    # Attempt 1: direct requests.get
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code == 200 and resp.text:
            raw_html = resp.text
            # Simple clean
            cleaned = re.sub(r"<script.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<style.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            text = " ".join(cleaned.split())

            # Check if empty or JS-shell
            js_shell_triggers = ["enable javascript", "you need to enable javascript to run this app", "javascript is disabled"]
            is_js_shell = len(text) < 300 or any(t in text.lower() for t in js_shell_triggers)
            if is_js_shell:
                text = ""  # Force fallback
    except Exception as e:
        logger.debug(f"Direct fetch failed for {url}: {e}")

    # Attempt 2: Jina AI reader fallback
    if not text or len(text) < 300:
        jina_url = f"https://r.jina.ai/{url}"
        try:
            r = requests.get(jina_url, headers={"User-Agent": USER_AGENT}, timeout=12)
            if r.status_code == 200 and len(r.text) >= 150:
                text = r.text
                used_jina = True
                _crawler_stats["jina_fallback_hosts"].add(host)
        except Exception as e:
            logger.debug(f"Jina fallback failed for {url}: {e}")

    # Cache result if successful
    if text:
        try:
            page_file.write_text(text, encoding="utf-8")
        except Exception:
            pass

    return text, used_jina


def discover_targeted_links(vendor_url: str) -> List[Tuple[str, str]]:
    """Discover links from /llms.txt, /sitemap.xml, and docs home page.

    Categorized into: 'auth', 'getting_started', 'pricing', 'mcp' (drops 'overview').
    Returns up to 5 prioritized links (taking >=1 from each category before a 2nd from any).
    """
    clean_url = vendor_url.strip()
    if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
        clean_url = "https://" + clean_url
    parsed = urlparse(clean_url)
    host = parsed.netloc.lower().split(":")[0]

    if not is_valid_domain(host):
        return []

    # Check robots.txt
    if check_robots_blocked(host):
        _crawler_stats["robots_blocked_hosts"].add(host)

    discovered_urls: Set[str] = set()

    # 1. Check /llms.txt
    llms_url = f"https://{host}/llms.txt"
    try:
        r = requests.get(llms_url, headers={"User-Agent": USER_AGENT}, timeout=4)
        if r.status_code == 200:
            found = re.findall(r"https?://[^\s)\]\"'>]+", r.text)
            for u in found:
                discovered_urls.add(u)
    except Exception:
        pass

    # 2. Check /sitemap.xml
    sitemap_url = f"https://{host}/sitemap.xml"
    try:
        r = requests.get(sitemap_url, headers={"User-Agent": USER_AGENT}, timeout=5)
        if r.status_code == 200 and "<urlset" in r.text.lower():
            locs = re.findall(r"<loc>(.*?)</loc>", r.text, flags=re.IGNORECASE)
            for loc in locs[:200]:  # limit loc inspection
                discovered_urls.add(loc.strip())
    except Exception:
        pass

    # 3. Check docs home page
    try:
        r = requests.get(clean_url, headers={"User-Agent": USER_AGENT}, timeout=8)
        if r.status_code == 200:
            hrefs = re.findall(r'href=["\'](.*?)["\']', r.text, flags=re.IGNORECASE)
            for h in hrefs:
                if h.startswith("#") or h.startswith("mailto:") or h.startswith("javascript:"):
                    continue
                full_u = urljoin(clean_url, h)
                discovered_urls.add(full_u)
    except Exception:
        pass

    # Filter and categorize valid vendor URLs
    static_exts = {".png", ".jpg", ".jpeg", ".gif", ".css", ".js", ".pdf", ".zip", ".svg", ".ico", ".woff", ".woff2"}
    categories = {
        "auth": [],
        "getting_started": [],
        "pricing": [],
        "mcp": [],
    }

    # Regex patterns (drop /overview as instructed!)
    p_auth = re.compile(r"(auth|authentication|oauth|api-?key|token|credential)", re.IGNORECASE)
    p_getting_started = re.compile(r"(getting-started|quickstart|developer-access|developer-guide|start)", re.IGNORECASE)
    p_pricing = re.compile(r"(pricing|plan|rate-limit|billing|enterprise|free|cost|tier)", re.IGNORECASE)
    p_mcp = re.compile(r"(mcp|model-context-protocol)", re.IGNORECASE)

    for u in discovered_urls:
        u_clean = u.split("#")[0].split("?")[0].strip()
        if not is_official_vendor_domain(u_clean, vendor_url):
            continue
        p_path = urlparse(u_clean).path.lower()
        if any(p_path.endswith(ext) for ext in static_exts):
            continue

        matched_cat = None
        if p_mcp.search(u_clean):
            matched_cat = "mcp"
        elif p_auth.search(u_clean):
            matched_cat = "auth"
        elif p_pricing.search(u_clean):
            matched_cat = "pricing"
        elif p_getting_started.search(u_clean):
            matched_cat = "getting_started"

        if matched_cat and u_clean not in categories[matched_cat]:
            categories[matched_cat].append(u_clean)

    # Balanced selection: up to 5 pages, taking >=1 from each category before a second from any
    selected_pages: List[Tuple[str, str]] = []
    cat_keys = ["auth", "getting_started", "pricing", "mcp"]

    # Round 1: one from each category
    for cat in cat_keys:
        if categories[cat]:
            selected_pages.append((categories[cat].pop(0), cat))
            if len(selected_pages) >= 5:
                break

    # Round 2: fill remaining slots up to 5
    if len(selected_pages) < 5:
        for cat in cat_keys:
            while categories[cat] and len(selected_pages) < 5:
                selected_pages.append((categories[cat].pop(0), cat))

    return selected_pages


def split_and_rank_chunks(text: str, chunk_size: int = 800, overlap: int = 150) -> str:
    """Split page into chunks and rank by keyword hits, returning top chunks up to 6,000 chars."""
    if not text:
        return ""
    keywords = ["authenticat", "token", "api key", "oauth", "pricing", "plan", "free", "trial", "approval", "mcp"]

    # Split into sliding chunks
    chunks = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + chunk_size, text_len)
        chunk = text[start:end]
        chunks.append(chunk)
        if end == text_len:
            break
        start += (chunk_size - overlap)

    # Score chunks by keyword occurrences
    scored_chunks = []
    for c in chunks:
        c_low = c.lower()
        score = sum(c_low.count(kw) for kw in keywords)
        scored_chunks.append((score, c))

    # Sort descending by score
    scored_chunks.sort(key=lambda x: x[0], reverse=True)

    selected = []
    total_len = 0
    for score, chunk in scored_chunks:
        if total_len + len(chunk) > 6000:
            break
        selected.append(chunk)
        total_len += len(chunk)

    if not selected and chunks:
        return chunks[0][:4000]

    return "\n\n---\n\n".join(selected)


def call_groq_llm_v4(prompt: str, max_retries: int = 12) -> Tuple[Dict[str, Any], str, int]:
    global _groq_key_idx
    keys = get_groq_api_keys()
    if not keys:
        raise ValueError("No GROQ API keys available!")

    url = "https://api.groq.com/openai/v1/chat/completions"
    current_model = "openai/gpt-oss-120b"
    consecutive_429 = 0

    for attempt in range(max_retries):
        key = keys[_groq_key_idx % len(keys)]
        current_key_num = (_groq_key_idx % len(keys)) + 1
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": "You are a strict technical auditor. You extract facts strictly entailed by official documentation excerpts. Always output valid JSON only."},
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
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "{}")
                    try:
                        parsed = json.loads(content)
                        return parsed, current_model, current_key_num
                    except Exception:
                        pass

            if resp.status_code == 429:
                consecutive_429 += 1
                _groq_key_idx += 1
                if consecutive_429 >= 2 and current_model == "openai/gpt-oss-120b":
                    logger.info("Multiple 429s on 120B. Seamlessly switching model to openai/gpt-oss-20b...")
                    current_model = "openai/gpt-oss-20b"
                time.sleep(1.0)
                continue

            if resp.status_code == 400:
                logger.warning(f"Groq 400 on {current_model}: {resp.text[:150]}")
                time.sleep(1.0)
                continue

            resp.raise_for_status()

        except Exception as e:
            logger.warning(f"Groq API error on key {current_key_num} ({current_model}): {e}")
            _groq_key_idx += 1
            consecutive_429 += 1
            if consecutive_429 >= 2:
                current_model = "openai/gpt-oss-20b"
            time.sleep(1.0)

    logger.warning("All Groq attempts exhausted. Returning empty extraction.")
    return {}, "error", 0


def check_verbatim_quote(quote: str, raw_text: str) -> bool:
    if not quote or not raw_text:
        return False
    if "..." in quote or "…" in quote:
        return False
    norm_quote = " ".join(quote.split()).lower()
    norm_page = " ".join(raw_text.split()).lower()
    return norm_quote in norm_page


def verify_app_v4(v3_rec: Dict[str, Any], vendor_url: str) -> Dict[str, Any]:
    """Execute v4 verification for a single app record using link discovery + chunk ranking."""
    app_id = v3_rec.get("id", 0)
    app_name = v3_rec.get("app", "")
    vendor_root = extract_root_domain(vendor_url)

    logger.info(f"[{app_id}/100] Starting Phase v4 verification for '{app_name}'...")

    v4_rec = json.loads(json.dumps(v3_rec))
    field_changes: Dict[str, Dict[str, Any]] = {}
    last_key_num = 1
    last_model_used = "openai/gpt-oss-120b"

    def record_change(field: str, old_val: Any, new_val: Any, reason: str):
        field_changes[field] = {"old": old_val, "new": new_val, "reason": reason}

    # 1. Discover targeted links
    targeted_links = discover_targeted_links(vendor_url)
    _crawler_stats["pages_fetched_per_app"][app_id] = len(targeted_links)
    logger.info(f"[{app_id}/100] Discovered {len(targeted_links)} targeted vendor pages for '{app_name}'")

    # 2. Fetch pages with Jina fallback
    fetched_docs: List[Tuple[str, str, str]] = []  # (url, category, raw_text)
    for u, cat in targeted_links:
        txt, used_jina = fetch_url_with_jina_fallback(u)
        if txt and len(txt) > 100:
            fetched_docs.append((u, cat, txt))

    # Also include existing official pages from evidence
    for ev in v4_rec.get("evidence", []):
        u = ev.get("url", "")
        if is_official_vendor_domain(u, vendor_url):
            url_hash = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
            pf = PAGES_CACHE_DIR / f"{url_hash}.txt"
            if pf.exists():
                try:
                    cached_t = pf.read_text(encoding="utf-8")
                    if cached_t and len(cached_t) > 100:
                        fetched_docs.append((u, "cached", cached_t))
                except Exception:
                    pass

    # 3. Chunk and rank text across all fetched docs
    combined_ranked_chunks = []
    for u, cat, txt in fetched_docs:
        ranked = split_and_rank_chunks(txt, chunk_size=800, overlap=150)
        if ranked:
            combined_ranked_chunks.append(f"DOCUMENT SOURCE ({cat.upper()}): {u}\n{ranked}")

    ranked_context = "\n\n========================================\n\n".join(combined_ranked_chunks)[:6500]

    # 4. Strict Extraction with LLM if context is available
    if ranked_context:
        prompt = (
            f"You are a strict technical compliance auditor evaluating SaaS API documentation for '{app_name}'.\n"
            f"Vendor Root Domain: {vendor_root}\n\n"
            f"OFFICIAL DOCUMENTATION EXCERPTS (RANKED BY RELEVANCE):\n"
            f"{ranked_context}\n\n"
            f"STRICT EXTRACTION INSTRUCTIONS:\n"
            f"1. 'access_model': One from ['self_serve_free', 'self_serve_trial', 'self_serve_paid_plan', 'approval_required', 'partner_or_contract', 'customer_only', 'no_public_api', 'unknown'].\n"
            f"   - You MUST extract a verbatim substring quote (<= 200 chars, NO ELLIPSES) that explicitly states how credentials are obtained AND whether it costs money or needs approval.\n"
            f"   - A quote that merely mentions 'OAuth 2.0', 'Bearer tokens', or 'create an app' WITHOUT stating account tier, cost, or approval process DOES NOT support access_model (set to 'unknown' and quote to '').\n"
            f"2. 'auth_methods': List from ['oauth2', 'api_key', 'bearer_token', 'basic', 'oauth1', 'jwt_service_account', 'signed_request', 'mtls'].\n"
            f"   - You MUST extract a verbatim substring quote (<= 200 chars, NO ELLIPSES) that explicitly names the authentication scheme.\n"
            f"3. 'official_mcp': One from ['official', 'none_found', 'unknown'].\n"
            f"   - Set to 'official' ONLY if the excerpt contains an explicit verbatim quote mentioning a vendor-provided Model Context Protocol (MCP) server or endpoint.\n\n"
            f"Output JSON with exact fields:\n"
            f'{{"access_model": "...", "access_quote": "...", "access_url": "...", '
            f'"auth_methods": [...], "auth_quote": "...", "auth_url": "...", '
            f'"official_mcp": "...", "mcp_quote": "...", "mcp_url": "..."}}\n'
        )
        try:
            parsed, model_used, key_num = call_groq_llm_v4(prompt)
            last_model_used = model_used
            last_key_num = key_num

            # A. Access Model
            new_acc = parsed.get("access_model", "unknown")
            acc_q = parsed.get("access_quote", "").strip()
            acc_u = parsed.get("access_url", "").strip()
            if new_acc != "unknown" and acc_q:
                # Find matching doc for verbatim check
                matching_doc = next((txt for u, _, txt in fetched_docs if (not acc_u or u == acc_u) and check_verbatim_quote(acc_q, txt)), None)
                if matching_doc:
                    old_acc = v4_rec.get("access_model")
                    if old_acc != new_acc:
                        record_change("access_model", old_acc, new_acc, "Extracted from chunk-ranked vendor documentation")
                        v4_rec["access_model"] = new_acc
                    # Update evidence
                    v4_rec["evidence"] = [e for e in v4_rec.get("evidence", []) if e.get("field") != "access_model"]
                    v4_rec["evidence"].append({
                        "field": "access_model",
                        "url": acc_u or vendor_url,
                        "quote": acc_q,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "is_official_domain": True,
                    })

            # B. Auth Methods
            new_auth = parsed.get("auth_methods", [])
            auth_q = parsed.get("auth_quote", "").strip()
            auth_u = parsed.get("auth_url", "").strip()
            valid_auths = {a.value for a in AuthMethod}
            clean_new_auth = [a for a in new_auth if a in valid_auths]
            if clean_new_auth and auth_q:
                matching_doc = next((txt for u, _, txt in fetched_docs if (not auth_u or u == auth_u) and check_verbatim_quote(auth_q, txt)), None)
                if matching_doc:
                    old_auth = v4_rec.get("auth_methods", [])
                    if set(old_auth) != set(clean_new_auth):
                        record_change("auth_methods", old_auth, clean_new_auth, "Extracted from chunk-ranked vendor documentation")
                        v4_rec["auth_methods"] = clean_new_auth
                    v4_rec["evidence"] = [e for e in v4_rec.get("evidence", []) if e.get("field") != "auth_methods"]
                    v4_rec["evidence"].append({
                        "field": "auth_methods",
                        "url": auth_u or vendor_url,
                        "quote": auth_q,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "is_official_domain": True,
                    })

            # C. Official MCP
            new_mcp = parsed.get("official_mcp", "unknown")
            mcp_q = parsed.get("mcp_quote", "").strip()
            mcp_u = parsed.get("mcp_url", "").strip()
            if new_mcp == "official" and mcp_q:
                matching_doc = next((txt for u, _, txt in fetched_docs if (not mcp_u or u == mcp_u) and check_verbatim_quote(mcp_q, txt)), None)
                if matching_doc:
                    old_mcp = v4_rec.get("official_mcp")
                    if old_mcp != "official":
                        record_change("official_mcp", old_mcp, "official", "Official MCP server verified on crawled vendor page")
                        v4_rec["official_mcp"] = "official"
                        v4_rec["official_mcp_url"] = mcp_u
                    v4_rec["evidence"] = [e for e in v4_rec.get("evidence", []) if e.get("field") != "official_mcp"]
                    v4_rec["evidence"].append({
                        "field": "official_mcp",
                        "url": mcp_u or vendor_url,
                        "quote": mcp_q,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "is_official_domain": True,
                    })
        except Exception as e:
            logger.warning(f"v4 extraction failed for {app_name}: {e}")

    # 5. Strict Buildability Derivation (Requirement 5)
    # Check if access_model and auth_methods have verified vendor quotes
    final_access = v4_rec.get("access_model", "unknown")
    has_vendor_access = (final_access != "unknown") and any(
        e.get("field") == "access_model" and is_official_vendor_domain(e.get("url", ""), vendor_url)
        and not any(x in e.get("quote", "") for x in ["...", "…"])
        for e in v4_rec.get("evidence", [])
    )
    final_auth = v4_rec.get("auth_methods", [])
    has_vendor_auth = bool(final_auth) and any(
        e.get("field") == "auth_methods" and is_official_vendor_domain(e.get("url", ""), vendor_url)
        and not any(x in e.get("quote", "") for x in ["...", "…"])
        for e in v4_rec.get("evidence", [])
    )

    old_build = v4_rec.get("buildability", "unknown")
    old_blocker = v4_rec.get("primary_blocker", "unknown")

    if has_vendor_access and has_vendor_auth:
        supported_auth = {"oauth2", "api_key", "bearer_token", "basic"}
        has_sup_auth = any(a in supported_auth for a in final_auth)
        if has_sup_auth and final_access == "self_serve_free":
            v4_rec["buildability"] = "buildable_now"
            v4_rec["primary_blocker"] = "none"
        elif has_sup_auth and final_access == "self_serve_paid_plan":
            v4_rec["buildability"] = "buildable_with_paid_account"
            v4_rec["primary_blocker"] = "paid_plan"
        elif has_sup_auth and final_access == "self_serve_trial":
            v4_rec["buildability"] = "buildable_now"
            v4_rec["primary_blocker"] = "none"
        elif final_access == "approval_required":
            v4_rec["buildability"] = "buildable_after_approval"
            v4_rec["primary_blocker"] = "app_review"
        elif final_access in ["partner_or_contract", "customer_only"]:
            v4_rec["buildability"] = "needs_partnership"
            v4_rec["primary_blocker"] = "partnership"
        elif final_access == "no_public_api":
            v4_rec["buildability"] = "not_buildable"
            v4_rec["primary_blocker"] = "no_api"
        else:
            v4_rec["buildability"] = "unknown"
            v4_rec["primary_blocker"] = "unknown"
    else:
        v4_rec["buildability"] = "unknown"
        v4_rec["primary_blocker"] = "unknown"

    if v4_rec["buildability"] != old_build:
        record_change("buildability", old_build, v4_rec["buildability"], "Derived strictly from supported access and auth")
    if v4_rec["primary_blocker"] != old_blocker:
        record_change("primary_blocker", old_blocker, v4_rec["primary_blocker"], "Aligned with derived buildability")

    # 6. Evaluate needs_human
    lacks_access = not has_vendor_access or v4_rec["access_model"] == "unknown"
    lacks_auth = not has_vendor_auth or not v4_rec["auth_methods"]
    lacks_build = v4_rec["buildability"] == "unknown"
    lacks_mcp = v4_rec["official_mcp"] == "unknown"

    needs_human = lacks_access or lacks_auth or lacks_build or lacks_mcp
    v4_rec["needs_human"] = needs_human
    v4_rec["confidence"] = "high" if not needs_human else "medium"

    # Store verification metadata
    v4_rec["_run_meta"]["v4_verification"] = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "key_idx": last_key_num,
        "verifier_model": last_model_used,
        "fields_changed_count": len(field_changes),
        "changes": field_changes,
        "targeted_links_count": len(targeted_links),
        "pages_fetched_count": len(fetched_docs),
        "has_vendor_access": has_vendor_access,
        "has_vendor_auth": has_vendor_auth,
    }

    validated = AppResearchResult.model_validate(v4_rec)
    dumped = validated.model_dump()
    dumped["id"] = app_id
    dumped["_run_meta"] = v4_rec.get("_run_meta", {})
    return dumped


def run_phase_v4(limit: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
    if not V3_PATH.exists():
        raise FileNotFoundError(f"Frozen baseline {V3_PATH} does not exist!")

    v3_records = []
    with open(V3_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                v3_records.append(json.loads(line))

    apps_df = pd.read_csv("data/apps.csv")
    vendor_urls = {int(row["id"]): row["vendor_url"] for _, row in apps_df.iterrows()}

    if limit:
        v3_records = v3_records[:limit]

    logger.info(f"Starting Phase v4 verification on {len(v3_records)} records...")
    verified_records = []
    existing_v4: Dict[int, Dict[str, Any]] = {}

    if V4_PATH.exists() and not force:
        with open(V4_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        r = json.loads(line)
                        existing_v4[int(r["id"])] = r
                    except Exception:
                        pass
        logger.info(f"Found {len(existing_v4)} previously verified records in {V4_PATH}")

    for rec in v3_records:
        app_id = int(rec["id"])
        if app_id in existing_v4 and not force:
            logger.info(f"[{app_id}/100] Skipping already verified app '{rec['app']}'")
            verified_records.append(existing_v4[app_id])
            continue

        v_url = vendor_urls.get(app_id, rec.get("vendor_url", ""))
        v4_rec = verify_app_v4(rec, v_url)
        verified_records.append(v4_rec)
        existing_v4[app_id] = v4_rec

        # Incrementally persist to disk
        with open(V4_PATH, "w", encoding="utf-8") as f:
            for _, r in sorted(existing_v4.items()):
                f.write(json.dumps(r) + "\n")

    logger.info(f"Phase v4 verification completed! {len(verified_records)} records written to {V4_PATH}")
    return verified_records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Phase v4 verification pipeline")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of apps")
    parser.add_argument("--force", action="store_true", help="Force re-verification")
    args = parser.parse_args()

    run_phase_v4(limit=args.limit, force=args.force)
