"""Generate, validate, and load datasets without exposing evaluation labels."""

import argparse
import json
import os
from datetime import date

from revenue_leakage.generator import Config, generate
from revenue_leakage.quality import validate
from revenue_leakage.storage import engine_for, load_operational, read_dataset, write_dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--output", default="data/synthetic")
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--customers", type=int, default=10_000)
    gen.add_argument("--invoices", type=int, default=50_000)
    gen.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    gen.add_argument("--months", type=int, default=24)
    gen.add_argument("--leakage-rate", type=float, default=0.10)
    for name in ("validate", "load"):
        p = sub.add_parser(name)
        p.add_argument("--input", default="data/synthetic")
    args = parser.parse_args()
    if args.command == "generate":
        config = Config(
            args.seed, args.customers, args.invoices, args.start, args.months, args.leakage_rate
        )
        frames, truth = generate(config)
        manifest = write_dataset(args.output, frames, truth, config)
        print(json.dumps(manifest, indent=2))
    else:
        frames, truth = read_dataset(args.input)
        counts = validate(frames, truth)
        if args.command == "load":
            url = os.environ.get("DATABASE_URL")
            if not url:
                parser.error("Set DATABASE_URL to a PostgreSQL connection URL")
            engine = engine_for(url)
            if engine.dialect.name != "postgresql":
                parser.error("load requires PostgreSQL")
            try:
                load_operational(engine, frames)
            finally:
                engine.dispose()
        print(json.dumps({"status": "passed", "row_counts": counts}, indent=2))


if __name__ == "__main__":
    main()
