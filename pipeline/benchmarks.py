"""Benchmarks module for Composio 100-App Research Agent.

Fetches and caches:
1. Composio catalog (ground truth: does Composio already have a toolkit?)
   Endpoint: GET https://backend.composio.dev/api/v3.1/toolkits
2. MCP Registry (ground truth: does an MCP server exist?)
   Endpoint: GET https://registry.modelcontextprotocol.io/v0.1/servers?search=<app>&version=latest
"""

import os
import re
import time
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, quote_plus
from typing import Dict, List, Any, Optional, Tuple

import requests
import pandas as pd
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
BENCHMARKS_DIR = Path("data/benchmarks")
MCP_CACHE_DIR = CACHE_DIR / "mcp_registry"

COMPOSIO_URL = "https://backend.composio.dev/api/v3.1/toolkits"
MCP_REGISTRY_URL = "https://registry.modelcontextprotocol.io/v0.1/servers"


def normalize_token(text: str) -> str:
    """Normalize a name or slug: lowercase alphanumeric only, stripping parentheticals."""
    if not text:
        return ""
    text = re.sub(r"\(.*?\)", "", text)
    return re.sub(r"[^a-z0-9]", "", text.lower())


def extract_base_domain(url: str) -> str:
    """Extract registered domain (e.g., salesforce.com from https://developer.salesforce.com/docs)."""
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    try:
        netloc = urlparse(url).netloc.lower()
        # Remove port if present
        netloc = netloc.split(":")[0]
        parts = netloc.split(".")
        if len(parts) >= 2:
            return ".".join(parts[-2:])
        return netloc
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Composio Catalog
# ---------------------------------------------------------------------------

def fetch_composio_catalog(api_key: str, force: bool = False) -> Dict[str, Any]:
    """Page the full Composio toolkits catalog and cache locally."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = CACHE_DIR / "composio_toolkits_raw.json"

    if cache_file.exists() and not force:
        logger.info(f"Loading cached Composio catalog from {cache_file}")
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    logger.info("Fetching full Composio catalog via API...")
    headers = {"x-api-key": api_key}
    all_toolkits: List[Dict[str, Any]] = []
    cursor: Optional[str] = None
    page = 1

    while True:
        params: Dict[str, Any] = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(COMPOSIO_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])
        all_toolkits.extend(items)
        cursor = data.get("next_cursor")
        logger.info(f"Composio page {page}: retrieved {len(items)} items. Total: {len(all_toolkits)}")

        if not cursor or not items:
            break
        page += 1

    catalog_data = {
        "fetched_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "total_items": len(all_toolkits),
        "items": all_toolkits,
    }

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(catalog_data, f, indent=2)

    logger.info(f"Saved {len(all_toolkits)} toolkits to {cache_file}")
    return catalog_data


def match_composio_toolkits(apps_df: pd.DataFrame, catalog_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Match each of the 100 apps to Composio toolkits by normalized name and app domain."""
    toolkits = catalog_data.get("items", [])
    raw_total = catalog_data.get("total_items", len(toolkits))
    catalog_date = catalog_data.get("fetched_date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    # Pre-index toolkits
    indexed_toolkits = []
    for it in toolkits:
        slug = it.get("slug", "")
        name = it.get("name", "")
        app_field = it.get("app", "")
        meta = it.get("meta") or {}
        app_url = meta.get("app_url", "")
        base_domain = extract_base_domain(app_url)

        # Categories extraction
        raw_cats = meta.get("categories", [])
        categories = []
        for c in raw_cats:
            if isinstance(c, dict):
                categories.append(c.get("name") or c.get("id"))
            elif isinstance(c, str):
                categories.append(c)

        indexed_toolkits.append({
            "raw": it,
            "slug": slug,
            "name": name,
            "norm_slug": normalize_token(slug),
            "norm_name": normalize_token(name),
            "norm_app": normalize_token(app_field),
            "domain": base_domain,
            "auth_schemes": it.get("auth_schemes", []),
            "composio_managed_auth_schemes": it.get("composio_managed_auth_schemes", []),
            "tools_count": meta.get("tools_count", 0),
            "triggers_count": meta.get("triggers_count", 0),
            "categories": categories,
        })

    # Common brand aliases / hint mappings for accurate resolution
    brand_aliases = {
        "google ads": ["google_ads", "googleads"],
        "meta ads": ["facebook_ads", "facebook_marketing", "meta_ads"],
        "linkedin ads": ["linkedin_ads", "linkedinads"],
        "gohighlevel": ["highlevel", "highlevel_agency", "highlevel_location", "highlevel_mcp"],
        "threads (meta)": ["threads"],
        "lark (larksuite)": ["lark", "larksuite"],
        "magento (adobe commerce)": ["magento", "adobe_commerce"],
        "amazon selling partner": ["amazon_selling_partner", "amazon_sp", "amazon"],
        "mongodb atlas": ["mongodb", "mongodb_atlas"],
        "zoho crm": ["zoho", "zoho_crm"],
        "zoho cliq": ["zoho_cliq", "cliq"],
        "harvest": ["harvest"],
        "twenty": ["twenty"],
        "pylon": ["pylon", "pylon_mcp"],
        "plain": ["plain", "plain_mcp"],
        "devin": ["devin", "devin_mcp"],
        "otter ai": ["otter_ai", "otter_ai_mcp"],
        "netlify": ["netlify", "netlify_mcp"],
        "close": ["close", "close_mcp"],
        "intercom": ["intercom", "intercom_mcp"],
        "consensus": ["consensus", "consensus_mcp"],
    }

    results = []

    for _, row in apps_df.iterrows():
        app_id = int(row["id"])
        app_name = str(row["app"])
        vendor_url = str(row["vendor_url"]) if pd.notna(row["vendor_url"]) else ""
        app_domain = extract_base_domain(vendor_url)
        app_norm = normalize_token(app_name)
        aliases = brand_aliases.get(app_name.lower(), [])

        matched_candidates = []

        for it in indexed_toolkits:
            conf = None

            # 1. Exact alias match
            if it["slug"].lower() in aliases or it["norm_slug"] in [normalize_token(a) for a in aliases]:
                conf = "exact"
            # 2. Exact match on normalized slug or name
            elif it["norm_slug"] == app_norm or it["norm_name"] == app_norm or it["norm_app"] == app_norm:
                conf = "exact"
            # 3. Exact domain match where name matches or is a prefix/suffix
            elif app_domain and it["domain"] and app_domain == it["domain"]:
                if (app_norm in it["norm_slug"] or it["norm_slug"] in app_norm or
                        app_norm in it["norm_name"] or it["norm_name"] in app_norm):
                    conf = "exact"
                else:
                    conf = "fuzzy"
            # 4. Suffix variants like '<app>_mcp'
            elif it["norm_slug"] == f"{app_norm}mcp" or it["norm_name"] == f"{app_norm}mcp":
                conf = "exact"
            # 5. Fuzzy match: substantive substring match
            elif len(app_norm) >= 4 and (app_norm in it["norm_slug"] or it["norm_slug"] in app_norm):
                # Guard against false positives like Square vs Squarespace
                if app_norm == "squarespace" and it["norm_slug"] == "square":
                    continue
                if app_norm == "binance" and "bin" in it["norm_slug"]:
                    continue
                conf = "fuzzy"

            if conf:
                candidate_summary = {
                    "slug": it["slug"],
                    "name": it["name"],
                    "auth_schemes": it["auth_schemes"],
                    "composio_managed_auth_schemes": it["composio_managed_auth_schemes"],
                    "tools_count": it["tools_count"],
                    "triggers_count": it["triggers_count"],
                    "categories": it["categories"],
                    "match_confidence": conf,
                }
                matched_candidates.append(candidate_summary)

        # Prioritize exact matches over fuzzy
        matched_candidates.sort(key=lambda c: (0 if c["match_confidence"] == "exact" else 1, -c["tools_count"]))

        primary = matched_candidates[0] if matched_candidates else None
        overall_conf = primary["match_confidence"] if primary else "none"

        record = {
            "app_id": app_id,
            "app": app_name,
            "matched": bool(primary),
            "match_confidence": overall_conf,
            "slug": primary["slug"] if primary else None,
            "name": primary["name"] if primary else None,
            "auth_schemes": primary["auth_schemes"] if primary else [],
            "composio_managed_auth_schemes": primary["composio_managed_auth_schemes"] if primary else [],
            "tools_count": primary["tools_count"] if primary else 0,
            "triggers_count": primary["triggers_count"] if primary else 0,
            "categories": primary["categories"] if primary else [],
            "all_matches": matched_candidates,
            "raw_catalog_total": raw_total,
            "catalog_date": catalog_date,
        }
        results.append(record)

    return results


# ---------------------------------------------------------------------------
# MCP Registry
# ---------------------------------------------------------------------------

def fetch_mcp_registry_for_app(app_name: str, force: bool = False, delay_sec: float = 0.15) -> Dict[str, Any]:
    """Query the MCP Registry search API for a given app with caching and rate politeness."""
    MCP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    slug = normalize_token(app_name)
    cache_file = MCP_CACHE_DIR / f"{slug}.json"

    if cache_file.exists() and not force:
        with open(cache_file, "r", encoding="utf-8") as f:
            return json.load(f)

    # Search queries: try clean app name
    # Clean name for search query: e.g. "Lark (Larksuite)" -> "Lark", "Threads (Meta)" -> "Threads"
    clean_query = re.sub(r"\(.*?\)", "", app_name).strip()
    encoded_query = quote_plus(clean_query)
    url = f"{MCP_REGISTRY_URL}?search={encoded_query}&version=latest"

    logger.debug(f"Querying MCP registry for '{clean_query}'...")
    try:
        resp = requests.get(url, timeout=20)
        time.sleep(delay_sec)  # Rate politeness
        if resp.status_code == 200:
            data = resp.json()
        else:
            logger.warning(f"MCP registry returned {resp.status_code} for '{clean_query}'")
            data = {"servers": [], "metadata": {}}
    except Exception as e:
        logger.error(f"Error querying MCP registry for '{clean_query}': {e}")
        data = {"servers": [], "metadata": {}, "error": str(e)}

    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return data


def extract_mcp_candidates(app_name: str, registry_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract candidate servers, their namespaces, remote URLs, and repository details."""
    servers = registry_response.get("servers", [])
    clean_app = re.sub(r"\(.*?\)", "", app_name).strip()
    app_norm = normalize_token(clean_app)

    candidates = []
    for item in servers:
        server = item.get("server", {})
        server_name = server.get("name", "")
        desc = server.get("description", "") or ""
        repo = server.get("repository", {}) or {}
        repo_url = repo.get("url", "") or ""
        subfolder = repo.get("subfolder", "") or ""

        # Extract namespace: prefix before '/'
        namespace = server_name.split("/")[0] if "/" in server_name else ""

        # Extract remotes and packages
        remotes = [r.get("url") for r in server.get("remotes", []) if r.get("url")]
        packages = [p.get("url") or p.get("name") for p in server.get("packages", []) if (p.get("url") or p.get("name"))]

        # Relevance filtering: verify the server relates to the target app
        name_part = server_name.split("/")[-1].lower() if "/" in server_name else server_name.lower()
        search_blob = f"{server_name.lower()} {desc.lower()} {repo_url.lower()} {subfolder.lower()}"

        # Distinct word boundary / substring checks
        is_relevant = False
        if app_norm in normalize_token(name_part) or app_norm in normalize_token(subfolder):
            is_relevant = True
        elif len(app_norm) >= 4 and re.search(rf"\b{re.escape(clean_app.lower())}\b", search_blob):
            is_relevant = True

        # False positive exclusions
        if app_norm == "twenty" and ("twentyclubs" in server_name or "twentyone" in server_name or "eightytwenty" in server_name):
            is_relevant = False
        if app_norm == "clay" and "clay" not in name_part and "crm" not in search_blob and "enrich" not in search_blob:
            is_relevant = False

        if is_relevant:
            candidates.append({
                "name": server_name,
                "namespace": namespace,
                "description": desc[:200] if desc else "",
                "repository_url": repo_url,
                "remotes": remotes,
                "packages": packages,
            })

    return candidates


def pull_mcp_registry(apps_df: pd.DataFrame, force: bool = False) -> List[Dict[str, Any]]:
    """Query and extract MCP registry candidates for all 100 apps."""
    results = []
    total = len(apps_df)
    now_iso = datetime.now(timezone.utc).isoformat()

    logger.info(f"Processing MCP Registry queries for {total} apps...")
    for idx, row in apps_df.iterrows():
        app_id = int(row["id"])
        app_name = str(row["app"])

        raw_resp = fetch_mcp_registry_for_app(app_name, force=force)
        candidates = extract_mcp_candidates(app_name, raw_resp)

        has_hit = len(candidates) > 0
        record = {
            "app_id": app_id,
            "app": app_name,
            "has_mcp_hit": has_hit,
            "server_count": len(candidates),
            "candidates": candidates,
            "retrieved_at": now_iso,
        }
        results.append(record)

        if (idx + 1) % 20 == 0 or (idx + 1) == total:
            logger.info(f"Processed {idx + 1}/{total} apps for MCP registry")

    return results


# ---------------------------------------------------------------------------
# Runner & Exporter
# ---------------------------------------------------------------------------

def run_benchmarks(pull_composio: bool = True, pull_mcp: bool = True, force: bool = False) -> Dict[str, Any]:
    """Run benchmark extraction and write data/benchmarks/*.jsonl."""
    load_dotenv()
    BENCHMARKS_DIR.mkdir(parents=True, exist_ok=True)
    apps_df = pd.read_csv("data/apps.csv")

    stats: Dict[str, Any] = {}

    if pull_composio:
        api_key = os.getenv("COMPOSIO_API_KEY")
        if not api_key:
            raise ValueError("COMPOSIO_API_KEY environment variable is not set!")

        catalog_data = fetch_composio_catalog(api_key, force=force)
        composio_records = match_composio_toolkits(apps_df, catalog_data)

        out_file = BENCHMARKS_DIR / "composio_catalog.jsonl"
        with open(out_file, "w", encoding="utf-8") as f:
            for rec in composio_records:
                f.write(json.dumps(rec) + "\n")
        logger.info(f"Wrote {len(composio_records)} records to {out_file}")

        matched_count = sum(1 for r in composio_records if r["matched"])
        exact_count = sum(1 for r in composio_records if r["match_confidence"] == "exact")
        fuzzy_count = sum(1 for r in composio_records if r["match_confidence"] == "fuzzy")
        stats["composio_matched"] = matched_count
        stats["composio_exact"] = exact_count
        stats["composio_fuzzy"] = fuzzy_count
        stats["composio_total_catalog"] = catalog_data.get("total_items")
        stats["composio_records"] = composio_records

    if pull_mcp:
        mcp_records = pull_mcp_registry(apps_df, force=force)

        out_file = BENCHMARKS_DIR / "mcp_registry.jsonl"
        with open(out_file, "w", encoding="utf-8") as f:
            for rec in mcp_records:
                f.write(json.dumps(rec) + "\n")
        logger.info(f"Wrote {len(mcp_records)} records to {out_file}")

        mcp_hit_count = sum(1 for r in mcp_records if r["has_mcp_hit"])
        stats["mcp_hits"] = mcp_hit_count
        stats["mcp_records"] = mcp_records

    # Summary analysis
    if pull_composio and pull_mcp:
        composio_matched_ids = {r["app_id"] for r in stats["composio_records"] if r["matched"]}
        mcp_hit_ids = {r["app_id"] for r in stats["mcp_records"] if r["has_mcp_hit"]}

        zero_hits_apps = []
        for _, row in apps_df.iterrows():
            aid = int(row["id"])
            if aid not in composio_matched_ids and aid not in mcp_hit_ids:
                zero_hits_apps.append({
                    "id": aid,
                    "app": str(row["app"]),
                    "category": str(row["category"]),
                    "vendor_url": str(row["vendor_url"])
                })
        stats["zero_hits_apps"] = zero_hits_apps

    return stats


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run benchmarks for Composio 100-App Research")
    parser.add_argument("--composio", action="store_true", help="Run Composio catalog matching")
    parser.add_argument("--mcp", action="store_true", help="Run MCP registry extraction")
    parser.add_argument("--all", action="store_true", help="Run both benchmarks")
    parser.add_argument("--force", action="store_true", help="Force fresh downloads, bypassing cache")

    args = parser.parse_args()

    do_all = args.all or (not args.composio and not args.mcp)
    do_composio = do_all or args.composio
    do_mcp = do_all or args.mcp

    run_benchmarks(pull_composio=do_composio, pull_mcp=do_mcp, force=args.force)
