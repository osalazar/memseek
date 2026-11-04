"""Import explicitly attributed observations from JSONL, sequentially and idempotently."""
import argparse
import asyncio
import json
from pathlib import Path
from personal_context import PersonalContext, connect

async def main(path, owner, batch_size=1):
    records = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    for i, row in enumerate(records):
        if set(row) != {"text", "source", "authority", "event_id"}:
            raise ValueError(f"Line {i+1}: requires exactly text/source/authority/event_id; no inferred authorship")
        if row["authority"] not in {"owner", "external", "assistant", "tool"}:
            raise ValueError(f"Line {i+1}: invalid authority")
        if not all(isinstance(value, str) and value.strip() for value in row.values()):
            raise ValueError(f"Line {i+1}: every value must be a non-empty string")
        if len(row["text"]) > 12000 or len(row["source"]) > 200:
            raise ValueError(f"Line {i+1}: text/source exceeds the catalog limit")
    async with connect() as mem:
        kit = PersonalContext(mem, owner)
        for i, row in enumerate(records):
            await kit.observe(**row, wait=False)
            if (i+1) % batch_size == 0 or i+1 == len(records):
                await kit.flush()
                print(f"Imported and processed observation {i+1}/{len(records)}", flush=True)
    print("Review preferences/candidates and factual context before trusting the migration.")

if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("path"); p.add_argument("--owner", required=True)
    p.add_argument("--batch-size", type=int, choices=range(1, 11), default=1,
                   help="Drain native processors after this many writes (1–10)")
    args = p.parse_args()
    asyncio.run(main(args.path, args.owner, args.batch_size))
