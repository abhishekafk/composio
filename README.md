# Composio take-home: researching 100 apps with an agent

An agent pipeline that researches 100 apps (auth, self-serve vs gated, API type, MCP status, buildability, evidence), plus a gold set and scoring script that measure how accurate it is, and a one-page case study built from the results.

- **Live case study:** `LIVE_LINK`
- **Final data:** `data/final_100.jsonl` and `data/final_100.csv`
- **Results in one line:** accuracy when the agent answered rose from 73% (v1) to 81% (v4) on a 48-app gold set, and answers backed by a vendor quote rose from 74% to 97%. The agent now says "unknown" instead of guessing, so overall correctness moved little (44% to 45%). Details and limits are on the case-study page.

## What it does

Four versions, each a stage that builds on the one before and each kept runnable:

| Version | Command | What it adds |
|---|---|---|
| v1 | `research-v1` | Tavily search, then an LLM extracts each field with a quote |
| v2 | `verify` | Verification rules (A to E), Composio catalog cross-check |
| v3 | `verify-v3` | Strict check that a vendor quote supports each answer, MCP Registry lookup |
| v4 | `verify-v4` | Zero-credit crawler with link discovery and chunk ranking, Jina reader fallback |

Where a human was needed: building and auditing the gold set (`data/gold.json`), hand-checking vendor pages, rejecting wrong Composio catalog matches, and correcting answers after the run. Every manual change is logged in `data/manual_overrides.csv`.

## Setup

Needs Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env               # then fill in your keys
```

`.env` variables:

| Variable | Needed for |
|---|---|
| `GROQ_API_KEY` | LLM calls (default provider). Optional `GROQ_API_KEY_2` to `GROQ_API_KEY_5` are rotated automatically when a key hits a rate limit |
| `TAVILY_API_KEY` | Search and the crawler |
| `COMPOSIO_API_KEY` | Only for pulling the Composio catalog (`benchmarks --composio`) |
| `GEMINI_API_KEY`, `LLM_PROVIDER=gemini` | Optional fallback provider |

Everything runs on free tiers. Never commit `.env`.

## Run

```bash
python -m pipeline.cli benchmarks --all   # optional: cache the Composio catalog and MCP Registry
python -m pipeline.cli research-v1 --limit 1   # quick test on one app
python -m pipeline.cli research-v1        # v1, all 100 apps
python -m pipeline.cli verify             # v2
python -m pipeline.cli verify-v3          # v3
python -m pipeline.cli verify-v4          # v4
```

Flags: `--limit N` runs the first N apps. `--force` re-runs and overwrites finished records. Without `--force`, each stage skips apps it already finished, so a run interrupted by a rate limit resumes where it stopped. Outputs go to `data/runs/v1.jsonl` to `v4.jsonl`.

## Score it

```bash
python -m pipeline.score
```

This checks the SHA-256 of `data/gold.json` before scoring, so a changed gold set fails immediately. It writes `data/scores_v1.json` to `scores_v4.json` and `data/misses_v4.csv`. A readable summary is in `data/final_report_tables.md`.

## Rebuild the case-study page

```bash
python build.py data/final_100.jsonl site/index.html
```

`build.py` embeds the data into `template.html`, adding the one-line descriptions from `descriptions.json`. The page also embeds all 100 records as JSON (`#research-data`) so an agent can read it directly.

## Repository map

- `pipeline/`: agent stages, verifiers, scoring (`cli.py` is the entry point)
- `data/`: inputs, run outputs, scores, gold set, final dataset, manual overrides
- `prompts/v1.md`: the extraction prompt
- `site/`: the built case-study page
- `docs/RUN_NOTES.md`: operational notes, including fallbacks

## Honest limits

- The gold set is 48 apps, so the version-to-version intervals overlap. 73% to 81% is a trend, not proof.
- Gold was built partly with a research agent: 44 of 48 labels were blind. It has no "no MCP" labels, so those claims are unscored.
- The tiered-record score (63.2%) and the api_key-vs-bearer sensitivity check were designed after seeing the scores.
- 63 apps look buildable, but only 7 have access proven by a vendor quote.
- I did not build the agent on Composio's SDK or MCP, and I did not use browser automation for verification. Composio is used only through its catalog API.
- The one-line app descriptions on the page were written by hand from general knowledge, not by the agent.
- Gating (self-serve vs gated) is the weakest field. Shopify and Airtable were wrongly labelled gated by the agent and corrected by hand.
