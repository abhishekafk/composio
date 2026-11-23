import json
from pathlib import Path
import pandas as pd

def test_phase2():
    apps_df = pd.read_csv("data/apps.csv")
    assert len(apps_df) == 100

    composio_file = Path("data/benchmarks/composio_catalog.jsonl")
    mcp_file = Path("data/benchmarks/mcp_registry.jsonl")

    assert composio_file.exists(), "composio_catalog.jsonl does not exist!"
    assert mcp_file.exists(), "mcp_registry.jsonl does not exist!"

    # 1. Parse Composio records
    composio_records = []
    with open(composio_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                composio_records.append(json.loads(line))

    assert len(composio_records) == 100, f"Expected 100 composio records, got {len(composio_records)}"

    matched_composio = [r for r in composio_records if r["matched"]]
    exact_composio = [r for r in composio_records if r["match_confidence"] == "exact"]
    fuzzy_composio = [r for r in composio_records if r["match_confidence"] == "fuzzy"]

    print("=== Composio Catalog Benchmark ===")
    print(f"Total apps: {len(composio_records)}")
    print(f"Matched in Composio: {len(matched_composio)} / 100")
    print(f"  - Exact matches: {len(exact_composio)}")
    print(f"  - Fuzzy matches: {len(fuzzy_composio)}")
    print(f"  - No match: {len(composio_records) - len(matched_composio)}")
    print(f"Raw Composio catalog total: {composio_records[0].get('raw_catalog_total')}")
    print(f"Catalog date: {composio_records[0].get('catalog_date')}")

    # 2. Parse MCP Registry records
    mcp_records = []
    with open(mcp_file, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                mcp_records.append(json.loads(line))

    assert len(mcp_records) == 100, f"Expected 100 MCP records, got {len(mcp_records)}"

    mcp_hits = [r for r in mcp_records if r["has_mcp_hit"]]
    print("\n=== MCP Registry Benchmark ===")
    print(f"Total apps queried: {len(mcp_records)}")
    print(f"Apps with at least one MCP hit: {len(mcp_hits)} / 100")
    print(f"Apps with 0 MCP hits: {len(mcp_records) - len(mcp_hits)}")

    # 3. Check combined overlap and zero-hits in both
    composio_matched_ids = {r["app_id"] for r in matched_composio}
    mcp_hit_ids = {r["app_id"] for r in mcp_hits}

    zero_hits = []
    for _, row in apps_df.iterrows():
        aid = int(row["id"])
        if aid not in composio_matched_ids and aid not in mcp_hit_ids:
            zero_hits.append({
                "id": aid,
                "app": row["app"],
                "category": row["category"],
                "vendor_url": row["vendor_url"],
                "hint": row["hint"]
            })

    print(f"\n=== Zero Hits in BOTH Composio & MCP Registry ({len(zero_hits)} apps) ===")
    for z in zero_hits:
        print(f"ID {z['id']:2d} | {z['app']:25s} | {z['category']:18s} | {z['hint']}")

    print("\nBenchmark validation completed successfully!")

if __name__ == "__main__":
    test_phase2()
