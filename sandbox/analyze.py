import argparse
import csv
import json
import os
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--task", default="")
    p.parse_args()
    input_path = Path(os.getenv("SLIPSTREAM_INPUT", "/workspace/input/sales.csv"))
    output_path = Path(os.getenv("SLIPSTREAM_OUTPUT", "/workspace/output/report.json"))
    with open(input_path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    totals = {}
    for row in rows:
        name = row["product"]
        totals[name] = totals.get(name, 0) + float(row["quantity"]) * float(row["unit_price"])
    top = sorted(totals.items(), key=lambda x: x[1], reverse=True)[:5]
    result = {
        "title": "Top products by revenue",
        "labels": [x[0] for x in top],
        "values": [round(x[1], 2) for x in top],
        "rows_processed": len(rows),
        "currency": "USD",
    }
    output_path.write_text(json.dumps(result))
    print("CSV loaded: sales.csv")
    print(f"Rows processed: {len(rows)}")
    for n, v in top:
        print(f"{n}: ${v:,.2f}")
    print("Chart data written to report.json")


if __name__ == "__main__":
    main()
