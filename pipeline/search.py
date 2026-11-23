"""Search and page fetching utilities for Composio App Research Agent."""

import os
import re
import time
import json
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from tavily import TavilyClient

logger = logging.getLogger(__name__)

CACHE_DIR = Path("data/cache")
TAVILY_CACHE_DIR = CACHE_DIR / "tavily"
PAGES_CACHE_DIR = CACHE_DIR / "pages"

_tavily_client: Optional[TavilyClient] = None
_tavily_search_count: int = 0


def get_tavily_search_count() -> int:
    """Return total number of network Tavily searches made during current process."""
    global _tavily_search_count
    return _tavily_search_count


def init_tavily_client() -> TavilyClient:
    """Initialize TavilyClient singleton using TAVILY_API_KEY from environment."""
    global _tavily_client
    if _tavily_client is None:
        load_dotenv()
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ValueError("TAVILY_API_KEY environment variable is not set!")
        _tavily_client = TavilyClient(api_key=api_key)
    return _tavily_client


def search_tavily(
    query: str,
    max_results: int = 5,
    force: bool = False,
    include_domains: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Execute a Tavily web search with disk caching to conserve free-tier credits."""
    global _tavily_search_count
    TAVILY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    domains_key = "-".join(sorted(include_domains)) if include_domains else "all"
    cache_key = hashlib.sha256(f"{query}:{max_results}:{domains_key}".encode("utf-8")).hexdigest()[:16]
    cache_file = TAVILY_CACHE_DIR / f"{cache_key}.json"

    if cache_file.exists() and not force:
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            return cached_data.get("results", [])
        except Exception as e:
            logger.warning(f"Error reading Tavily cache for query '{query}': {e}")

    client = init_tavily_client()
    logger.info(f"Executing Tavily search: '{query}' (domains={include_domains})")

    kwargs: Dict[str, Any] = {"max_results": max_results}
    if include_domains:
        clean_domains = [d for d in include_domains if d and "." in d and not any(c in d for c in [" ", "(", ")", "/"])]
        if clean_domains:
            kwargs["include_domains"] = clean_domains
        else:
            logger.warning(f"All domains in include_domains were invalid: {include_domains}. Skipping domain filter.")

    last_err = None
    for attempt in range(4):
        try:
            response = client.search(query=query, **kwargs)
            _tavily_search_count += 1

            results = response.get("results", [])
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump({"query": query, "include_domains": include_domains, "results": results}, f, indent=2)

            return results
        except Exception as e:
            last_err = e
            wait = 2.0 * (attempt + 1)
            logger.warning(f"Tavily search error on attempt {attempt + 1}: {e}. Retrying in {wait}s...")
            time.sleep(wait)

    logger.error(f"Tavily search failed after all retries for query '{query}': {last_err}")
    return []


def clean_html_text(html_text: str) -> str:
    """Extract readable text from HTML by removing scripts, styles, tags, and extra whitespace."""
    if not html_text:
        return ""
    # Strip script and style blocks
    cleaned = re.sub(r"<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    # Strip all remaining tags
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    # Decode common HTML entities
    cleaned = cleaned.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    # Collapse multiple whitespaces and linebreaks
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n\s*\n+", "\n\n", cleaned)
    return cleaned.strip()


def fetch_page(url: str, force: bool = False, max_chars: int = 10000) -> str:
    """Fetch web page content using requests with automatic r.jina.ai fallback and caching."""
    PAGES_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    url_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    cache_file = PAGES_CACHE_DIR / f"{url_hash}.txt"

    if cache_file.exists() and not force:
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            pass

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    content_text = ""
    # Step 1: Direct requests fetch
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        if resp.status_code == 200:
            content_type = resp.headers.get("Content-Type", "")
            if "text" in content_type or "html" in content_type or "json" in content_type:
                content_text = clean_html_text(resp.text)
    except Exception as e:
        logger.debug(f"Direct fetch failed for {url}: {e}")

    # Step 2: Fallback to r.jina.ai for JS-rendered or blocked pages
    if len(content_text) < 250:
        jina_url = f"https://r.jina.ai/{url}"
        try:
            logger.debug(f"Falling back to r.jina.ai for {url}")
            jina_resp = requests.get(jina_url, headers=headers, timeout=15)
            if jina_resp.status_code == 200 and len(jina_resp.text.strip()) > 100:
                content_text = jina_resp.text.strip()
        except Exception as e:
            logger.debug(f"Jina fallback failed for {url}: {e}")

    # Truncate to sane size
    if len(content_text) > max_chars:
        content_text = content_text[:max_chars] + f"\n... [Truncated to {max_chars} chars]"

    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(content_text)

    return content_text
