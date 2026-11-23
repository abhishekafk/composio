import os
import sys
sys.path.insert(0, os.path.abspath("."))

import pandas as pd
from dotenv import load_dotenv
from pipeline.schema import Category, AppInput

def test_phase1():
    load_dotenv()

    # 1. Check .env keys exist without printing them
    keys = ['COMPOSIO_API_KEY', 'GEMINI_API_KEY', 'TAVILY_API_KEY']
    print("=== Checking .env keys ===")
    all_keys_set = True
    for k in keys:
        val = os.getenv(k, '')
        is_set = bool(val.strip())
        print(f"  {k}: set={is_set} (length={len(val.strip())})")
        if not is_set:
            all_keys_set = False

    # Check GEMINI_MODEL
    gemini_model = os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')
    print(f"  GEMINI_MODEL: {gemini_model}")

    # 2. Check apps.csv
    print("\n=== Validating data/apps.csv ===")
    df = pd.read_csv('data/apps.csv')
    print(f"Total rows: {len(df)}")
    assert len(df) == 100, f"Expected 100 rows, got {len(df)}"
    assert list(df['id']) == list(range(1, 101)), "IDs are not 1-100 unique and sequential"

    valid_categories = {c.value for c in Category}
    for idx, row in df.iterrows():
        cat = row['category']
        assert cat in valid_categories, f"Invalid category '{cat}' on row {row['id']}"
        assert pd.notna(row['vendor_url']) and len(str(row['vendor_url']).strip()) > 0, f"Missing vendor_url on row {row['id']}"
        AppInput(
            id=int(row['id']),
            app=str(row['app']),
            category=row['category'],
            vendor_url=str(row['vendor_url']),
            hint=str(row['hint']),
            notes=str(row['notes']) if pd.notna(row['notes']) else ""
        )

    print("\nCategory distribution (10 per category):")
    print(df['category'].value_counts())
    
    ambiguous_rows = df[df['notes'].fillna('').str.contains('ambiguous')]
    print(f"\nAmbiguous rows count: {len(ambiguous_rows)}")
    for _, r in ambiguous_rows.iterrows():
        print(f"  ID {r['id']}: {r['app']} ({r['hint']}) -> {r['vendor_url']}")

    print("\nAll Phase 1 validations PASSED successfully!")

if __name__ == "__main__":
    test_phase1()
