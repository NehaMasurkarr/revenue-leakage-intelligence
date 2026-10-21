"""Generate data, migrate PostgreSQL, load operations, and run financial SQL."""

import argparse
import json
import os
from datetime import date, datetime
from decimal import Decimal

from revenue_leakage.analytics import QUERIES, explain, query, validate_analytics
from revenue_leakage.config import configured_engine
from revenue_leakage.generator import Config, generate
from revenue_leakage.migrations import migrate
from revenue_leakage.quality import validate
from revenue_leakage.storage import (
    load_operational,
    read_dataset,
    read_operational,
    write_dataset,
)


def json_value(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Unsupported result: {type(value)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--output", default=os.getenv("DATA_DIR", "data/synthetic"))
    gen.add_argument("--seed", type=int, default=42)
    gen.add_argument("--customers", type=int, default=10_000)
    gen.add_argument("--invoices", type=int, default=50_000)
    gen.add_argument("--start", type=date.fromisoformat, default=date(2024, 1, 1))
    gen.add_argument("--months", type=int, default=24)
    gen.add_argument("--leakage-rate", type=float, default=0.10)
    for name in ("validate", "load"):
        p = sub.add_parser(name)
        p.add_argument("--input", default=os.getenv("DATA_DIR", "data/synthetic"))
    sub.add_parser("migrate")
    for name in ("analytics", "sql-check", "explain"):
        p = sub.add_parser(name)
        p.add_argument("--as-of", type=date.fromisoformat, default=os.getenv("ANALYTICS_AS_OF"))
        if name != "sql-check":
            p.add_argument("--query", choices=QUERIES, default="summary")
    args = parser.parse_args()
    if args.command == "generate":
        config = Config(
            args.seed, args.customers, args.invoices, args.start, args.months, args.leakage_rate
        )
        frames, truth = generate(config)
        result = write_dataset(args.output, frames, truth, config)
    elif args.command == "validate":
        frames, truth = read_dataset(args.input)
        result = {"status": "passed", "row_counts": validate(frames, truth)}
    else:
        if args.command in ("analytics", "sql-check", "explain") and not args.as_of:
            parser.error("Supply --as-of YYYY-MM-DD or ANALYTICS_AS_OF")
        engine = configured_engine()
        try:
            if args.command == "migrate":
                result = {"applied": migrate(engine)}
            elif args.command == "load":
                result = load_operational(
                    engine, read_operational(args.input), int(os.getenv("LOAD_BATCH_SIZE", "1000"))
                )
            else:
                # One stable, read-only snapshot for multi-query reconciliations.
                with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                    with conn.begin():
                        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                        if args.command == "sql-check":
                            result = validate_analytics(conn, args.as_of)
                        elif args.command == "explain":
                            result = explain(conn, args.query, args.as_of)
                        else:
                            result = query(conn, args.query, args.as_of)
        finally:
            engine.dispose()
    print(json.dumps(result, default=json_value, indent=2))


if __name__ == "__main__":
    main()
