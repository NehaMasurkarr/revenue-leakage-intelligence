"""Generate data, migrate PostgreSQL, load operations, and run financial SQL."""

import argparse
import json
import os
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import text

from revenue_leakage.analytics import QUERIES, explain, query, validate_analytics
from revenue_leakage.config import configured_engine
from revenue_leakage.etl import ETLOptions, PipelineFailure, run_etl
from revenue_leakage.freshness import freshness_status
from revenue_leakage.generator import Config, generate
from revenue_leakage.migrations import migrate
from revenue_leakage.quality import validate
from revenue_leakage.run_log import recover_run
from revenue_leakage.source import read_snapshot
from revenue_leakage.storage import (
    load_operational,
    read_dataset,
    read_operational,
    write_dataset,
)


def json_value(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else str(value)
    if isinstance(value, UUID):
        return str(value)
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
    etl = sub.add_parser("etl-run")
    etl.add_argument("--input", default=os.getenv("DATA_DIR", "data/synthetic"))
    etl.add_argument("--snapshot-version", type=int, default=os.getenv("ETL_SNAPSHOT_VERSION"))
    etl.add_argument("--replay", action="store_true", help="Process committed raw changes only")
    etl.add_argument("--source", default=os.getenv("ETL_SOURCE", "financials"))
    etl.add_argument(
        "--mode", choices=("incremental", "full"), default=os.getenv("ETL_MODE", "incremental")
    )
    etl.add_argument("--max-attempts", type=int, default=os.getenv("ETL_MAX_ATTEMPTS", "3"))
    etl.add_argument(
        "--retry-delay", type=float, default=os.getenv("ETL_RETRY_DELAY_SECONDS", "0.5")
    )
    health = sub.add_parser("etl-freshness")
    health.add_argument(
        "--max-run-age-seconds", type=int, default=os.getenv("ETL_MAX_RUN_AGE_SECONDS", "86400")
    )
    for p in (etl, health):
        p.add_argument("--as-of", type=date.fromisoformat, default=os.getenv("ANALYTICS_AS_OF"))
        p.add_argument(
            "--max-data-age-days", type=int, default=os.getenv("ETL_MAX_DATA_AGE_DAYS", "45")
        )
    runs = sub.add_parser("etl-runs")
    runs.add_argument("--limit", type=int, default=20)
    recover = sub.add_parser("etl-recover")
    recover.add_argument("--run-id", type=UUID, required=True)
    args = parser.parse_args()
    if args.command in ("etl-run", "etl-freshness") and not args.as_of:
        parser.error("Supply --as-of YYYY-MM-DD or ANALYTICS_AS_OF")
    if args.command == "etl-run":
        if args.replay and args.snapshot_version is not None:
            parser.error("--replay reads existing raw data; omit --snapshot-version")
        if not args.replay and args.snapshot_version is None:
            parser.error("Supply --snapshot-version for source ingestion")
    if args.command == "etl-runs" and not 1 <= args.limit <= 1000:
        parser.error("--limit must be between 1 and 1000")
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
            elif args.command == "etl-run":
                options = ETLOptions(
                    as_of=args.as_of,
                    source_name=args.source,
                    mode=args.mode,
                    batch_size=int(os.getenv("LOAD_BATCH_SIZE", "1000")),
                    max_attempts=args.max_attempts,
                    retry_delay_seconds=args.retry_delay,
                    max_data_age_days=args.max_data_age_days,
                )
                try:
                    result = run_etl(
                        engine,
                        None if args.replay else lambda: read_snapshot(args.input),
                        snapshot_version=args.snapshot_version,
                        options=options,
                    )
                except PipelineFailure as exc:
                    parser.exit(1, f"{exc}\n")
            elif args.command == "etl-recover":
                result = recover_run(engine, args.run_id)
            else:
                # One stable, read-only snapshot for multi-query reconciliations.
                with engine.connect().execution_options(isolation_level="REPEATABLE READ") as conn:
                    with conn.begin():
                        conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                        if args.command == "etl-runs":
                            result = [
                                dict(r)
                                for r in conn.execute(
                                    text(
                                        "SELECT * FROM etl.pipeline_runs "
                                        "ORDER BY started_at DESC LIMIT :limit"
                                    ),
                                    {"limit": args.limit},
                                ).mappings()
                            ]
                        elif args.command == "etl-freshness":
                            if args.max_run_age_seconds <= 0 or args.max_data_age_days < 0:
                                parser.error(
                                    "Freshness thresholds must be positive (days may be zero)"
                                )
                            result = freshness_status(
                                conn, args.as_of, args.max_data_age_days, args.max_run_age_seconds
                            )
                        elif args.command == "sql-check":
                            result = validate_analytics(conn, args.as_of)
                        elif args.command == "explain":
                            result = explain(conn, args.query, args.as_of)
                        else:
                            result = query(conn, args.query, args.as_of)
        finally:
            engine.dispose()
    print(json.dumps(result, default=json_value, indent=2))
    if args.command == "etl-freshness" and result["status"] != "passed":
        parser.exit(1)


if __name__ == "__main__":
    main()
