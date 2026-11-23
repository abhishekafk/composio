"""Research agent v1 (the deliberately naive first pass).

Pipeline:
1. Tavily search for "<app> API authentication" and "<app> API pricing access" (max 5 results each).
2. Fetch the top few result pages (requests; fall back to https://r.jina.ai/<url>). Truncate to sane size.
3. Call LLM (Groq openai/gpt-oss-120b or Gemini) with the pages and the schema (JSON mode) filling schema fields.
4. Write data/runs/v1.jsonl. Log per app: searches, pages fetched, tokens, errors.
5. Blind to benchmark files (no benchmark data is read or injected).
"""

import os
import re
import json
import time
import random
import logging
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple

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

RUNS_DIR = Path("data/runs")
V1_PATH = RUNS_DIR / "v1.jsonl"


def get_v1_prompt_hash() -> str:
    """Return SHA-256 hash of prompts/v1.md to ensure baseline reproducibility."""
    p = Path("prompts/v1.md")
    if p.exists():
        return hashlib.sha256(p.read_bytes()).hexdigest()
    return "unknown"


def get_llm_provider() -> str:
    """Determine LLM provider from environment. Prefers Groq if GROQ_API_KEY is present."""
    load_dotenv()
    provider = os.getenv("LLM_PROVIDER", "").lower()
    if provider in ["groq", "gemini"]:
        return provider
    if os.getenv("GROQ_API_KEY"):
        return "groq"
    return "gemini"


def get_groq_api_keys() -> List[str]:
    """Return all available Groq API keys for rotation."""
    load_dotenv()
    keys = []
    for k in ["GROQ_API_KEY", "GROQ_API_KEY_2", "GROQ_API_KEY_3", "GROQ_API_KEY_4"]:
        val = os.getenv(k)
        if val and val.strip():
            keys.append(val.strip())
    return keys


_groq_key_idx = 0


def call_groq_llm(prompt: str, model_name: str = "openai/gpt-oss-120b", max_retries: int = 8) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """Call Groq API using JSON mode with multi-key rotation and resilient fallback."""
    global _groq_key_idx
    keys = get_groq_api_keys()
    if not keys:
        raise ValueError("No GROQ_API_KEY found in environment or .env!")

    models_to_try = [model_name, "openai/gpt-oss-20b"]

    last_exc = None
    for active_model in models_to_try:
        payload = {
            "model": active_model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are a research agent that outputs strict JSON adhering precisely to the requested schema. Never output markdown fences or explanatory text outside the JSON object.",
                },
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.1,
        }

        for attempt in range(max_retries):
            api_key = keys[_groq_key_idx % len(keys)]
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            try:
                resp = requests.post("https://api.groq.com/openai/v1/chat/completions", headers=headers, json=payload, timeout=25)
                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    parsed_json = json.loads(content)
                    usage = data.get("usage", {})
                    tokens = {
                        "prompt_tokens": usage.get("prompt_tokens"),
                        "completion_tokens": usage.get("completion_tokens"),
                        "total_tokens": usage.get("total_tokens"),
                    }
                    return parsed_json, active_model, tokens
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

                    # If this key has a long hourly wait (> 60s), immediately switch to next key
                    if wait > 60:
                        logger.warning(f"Groq key {_groq_key_idx % len(keys) + 1} quota limit hit ({wait:.1f}s). Rotating to next API key...")
                        _groq_key_idx += 1
                        if attempt >= len(keys) - 1:
                            logger.warning(f"All keys hit quota for model {active_model}. Falling back to next candidate model...")
                            break
                        continue

                    logger.warning(f"Groq API ({active_model}) returned {resp.status_code} on key {_groq_key_idx % len(keys) + 1} (attempt {attempt + 1}). Backing off {wait:.1f}s...")
                    time.sleep(wait)
                else:
                    resp.raise_for_status()
            except Exception as e:
                last_exc = e
                wait = (2 ** (attempt + 1)) + random.uniform(0.5, 1.5)
                logger.warning(f"Groq error on attempt {attempt + 1}: {e}. Retrying in {wait:.1f}s...")
                _groq_key_idx += 1
                time.sleep(min(wait, 5.0))

    raise last_exc or RuntimeError("Groq call failed after all retries across all keys and models")


def call_gemini_llm(prompt: str, model_name: str = "gemini-3.6-flash", max_retries: int = 5) -> Tuple[Dict[str, Any], str, Dict[str, Any]]:
    """Call Google GenAI SDK with fallback across candidate models."""
    from google import genai
    from google.genai import types

    load_dotenv()
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set in environment or .env!")

    client = genai.Client(api_key=api_key)
    all_candidates = [model_name, "gemini-3.6-flash", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    pool = [m for m in all_candidates if m and "2.5" not in m]

    last_exc = None
    for attempt in range(max_retries):
        for candidate in pool:
            try:
                resp = client.models.generate_content(
                    model=candidate,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        response_schema=AppResearchResult,
                        temperature=0.1,
                    ),
                )
                data = json.loads(resp.text.strip())
                usage = getattr(resp, "usage_metadata", None)
                tokens = {}
                if usage:
                    tokens = {
                        "prompt_tokens": getattr(usage, "prompt_token_count", None),
                        "completion_tokens": getattr(usage, "candidates_token_count", None),
                        "total_tokens": getattr(usage, "total_token_count", None),
                    }
                return data, candidate, tokens
            except Exception as e:
                last_exc = e
                logger.warning(f"Gemini candidate {candidate} failed: {type(e).__name__}")
                time.sleep(1.0)
        wait = (2 ** (attempt + 1)) + random.uniform(1.0, 3.0)
        logger.warning(f"Gemini pool busy. Backing off {wait:.1f}s...")
        time.sleep(wait)

    raise last_exc or RuntimeError("Gemini call failed after all retries")


def research_app_v1(
    app_info: Dict[str, Any],
    force: bool = False,
) -> Dict[str, Any]:
    """Execute naive v1 research for a single app."""
    app_id = int(app_info["id"])
    app_name = str(app_info["app"])
    category_val = str(app_info["category"])
    vendor_url = str(app_info["vendor_url"])
    hint = str(app_info["hint"])

    logger.info(f"[{app_id}/100] Starting naive v1 research for '{app_name}' ({category_val})...")

    # Step 1: Tavily searches
    q1 = f'"{app_name}" API authentication'
    q2 = f'"{app_name}" API pricing access'

    results_q1 = search_tavily(q1, max_results=5, force=force)
    results_q2 = search_tavily(q2, max_results=5, force=force)

    # Step 2: Select top result pages to fetch (max 2 per query, deduplicated)
    urls_to_fetch = []
    seen_urls = set()
    for r in results_q1[:2] + results_q2[:2]:
        u = r.get("url")
        if u and u not in seen_urls:
            if "youtube.com" in u or "youtu.be" in u:
                continue
            seen_urls.add(u)
            urls_to_fetch.append(u)

    pages_fetched = []
    for u in urls_to_fetch[:3]:
        text = fetch_page(u, force=force, max_chars=3500)
        if text and len(text.strip()) > 100:
            pages_fetched.append({"url": u, "content": text})

    logger.info(f"[{app_id}/100] '{app_name}': fetched {len(pages_fetched)} pages from {len(urls_to_fetch)} URLs")

    # Step 3: Format prompt
    pages_context = ""
    for idx, p in enumerate(pages_fetched, 1):
        pages_context += f"\n--- BEGIN SOURCE PAGE {idx} (URL: {p['url']}) ---\n"
        pages_context += p["content"][:3000]
        pages_context += f"\n--- END SOURCE PAGE {idx} ---\n"

    # Enforce strict total context budget to prevent 413 on free tiers
    if len(pages_context) > 7500:
        pages_context = pages_context[:7500] + "\n... [Context truncated to stay within rate limits]"

    schema_json = json.dumps(AppResearchResult.model_json_schema(), indent=2)

    prompt = (
        "You are an expert AI product ops researcher analyzing software applications to evaluate Composio toolkit buildability.\n"
        "Composio turns apps into tools AI agents can call.\n\n"
        "CRITICAL RULES:\n"
        "1. NEVER invent data. If a field cannot be directly supported by the fetched source pages below, set it to 'unknown' (or empty list / 'none') and set 'needs_human: true'. A wrong confident answer is worse than 'unknown'.\n"
        "2. For every non-unknown field you extract, you MUST provide an entry in the 'evidence' list.\n"
        "3. The 'quote' in each evidence item must be a VERBATIM substring of the fetched page text (<= 200 characters), never paraphrased.\n"
        "4. Definitions:\n"
        "   - Self-serve: an individual developer can obtain working credentials without talking to anyone and without a paid contract. Free tier, sandbox, or trial counts. A paid plan required to unlock API access is 'self_serve_paid_plan'.\n"
        "   - Gated: requires human approval (app review, partner program), enterprise contract, or sales contact.\n"
        "   - Buildable now: public documented API + credentials obtainable self-serve + auth scheme Composio supports (OAuth2, API key, bearer, basic).\n"
        "   - Description: concise description <= 140 chars.\n\n"
        f"TARGET APPLICATION INFO:\n"
        f"- ID: {app_id}\n"
        f"- App: {app_name}\n"
        f"- Category: {category_val}\n"
        f"- Vendor URL: {vendor_url}\n"
        f"- Hint: {hint}\n\n"
        f"FETCHED SOURCE PAGES:\n"
        f"{pages_context if pages_context else '[No source pages could be fetched]'}\n\n"
        f"OUTPUT REQUIREMENTS:\n"
        f"Output ONLY a valid JSON object matching the JSON schema below:\n"
        f"{schema_json}\n"
    )

    provider = get_llm_provider()
    t0 = time.time()
    try:
        if provider == "groq":
            data_dict, used_model, tokens = call_groq_llm(prompt)
        else:
            gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
            data_dict, used_model, tokens = call_gemini_llm(prompt, model_name=gemini_model)

        elapsed = time.time() - t0

        # Validate with Pydantic
        result_obj = AppResearchResult(**data_dict)
        final_dict = result_obj.model_dump()
        final_dict["id"] = app_id
        final_dict["app"] = app_name
        final_dict["category"] = category_val
        final_dict["vendor_url"] = vendor_url

        final_dict["_run_meta"] = {
            "provider": provider,
            "model": used_model,
            "prompt_hash": get_v1_prompt_hash(),
            "elapsed_seconds": round(elapsed, 2),
            "searches_count": 2,
            "pages_fetched_count": len(pages_fetched),
            "tokens": tokens,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return final_dict

    except Exception as e:
        logger.error(f"Extraction failed for '{app_name}' (ID {app_id}): {e}")
        fallback = AppResearchResult(
            app=app_name,
            category=Category(category_val),
            vendor_url=vendor_url,
            identity_confidence=Confidence.low,
            description=f"Automated extraction failed: {str(e)[:80]}",
            confidence=Confidence.low,
            needs_human=True,
        ).model_dump()
        fallback["id"] = app_id
        fallback["_run_meta"] = {
            "provider": provider,
            "model": "error",
            "prompt_hash": get_v1_prompt_hash(),
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return fallback


def run_v1_pipeline(limit: Optional[int] = None, force: bool = False) -> List[Dict[str, Any]]:
    """Run research agent v1 across the apps dataset."""
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    apps_df = pd.read_csv("data/apps.csv")

    if limit:
        apps_df = apps_df.head(limit)

    provider = get_llm_provider()
    logger.info(f"Initialized v1 research pipeline with provider: {provider} (Total targets: {len(apps_df)})")

    # Load existing runs to support resumability unless force
    existing_ids = set()
    records_by_id = {}
    if V1_PATH.exists() and not force:
        with open(V1_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    try:
                        rec = json.loads(line)
                        rid = rec.get("id")
                        if rid:
                            existing_ids.add(int(rid))
                            records_by_id[int(rid)] = rec
                    except Exception:
                        pass
        logger.info(f"Found {len(existing_ids)} previously completed apps in {V1_PATH}")

    tavily_start_count = get_tavily_search_count()
    completed_records = []

    for _, row in apps_df.iterrows():
        app_id = int(row["id"])
        if app_id in existing_ids and not force:
            logger.info(f"[{app_id}/100] Skipping already completed app '{row['app']}'")
            completed_records.append(records_by_id[app_id])
            continue

        res = research_app_v1(row.to_dict(), force=force)
        completed_records.append(res)
        records_by_id[app_id] = res

        # Write incrementally to JSONL
        with open(V1_PATH, "w", encoding="utf-8") as f:
            for _, r in sorted(records_by_id.items()):
                f.write(json.dumps(r) + "\n")

        # Brief polite pause between apps
        time.sleep(0.5)

    tavily_used = get_tavily_search_count() - tavily_start_count
    logger.info(f"v1 Run finished! Total records: {len(completed_records)}. Tavily network searches used: {tavily_used}")
    return completed_records


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run v1 naive research agent")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of apps to research")
    parser.add_argument("--force", action="store_true", help="Force re-research overwriting existing records")
    args = parser.parse_args()

    run_v1_pipeline(limit=args.limit, force=args.force)
