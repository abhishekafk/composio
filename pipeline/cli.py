"""Main CLI entrypoint for Composio App Research Agent pipeline.

Usage:
  python -m pipeline.cli benchmarks [--all | --composio | --mcp] [--force]
  python -m pipeline.cli research-v1 [--limit N] [--force]
"""

import sys
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def main():
    parser = argparse.ArgumentParser(description="Composio App Research Pipeline CLI")
    subparsers = parser.add_subparsers(dest="command", help="Pipeline command to execute")

    # benchmarks command
    bench_parser = subparsers.add_parser("benchmarks", help="Run benchmarks (Composio catalog & MCP registry)")
    bench_parser.add_argument("--composio", action="store_true", help="Pull/match Composio catalog")
    bench_parser.add_argument("--mcp", action="store_true", help="Query/extract MCP registry")
    bench_parser.add_argument("--all", action="store_true", help="Run all benchmarks")
    bench_parser.add_argument("--force", action="store_true", help="Force refresh bypassing cache")

    # research-v1 command
    v1_parser = subparsers.add_parser("research-v1", help="Run naive v1 research agent")
    v1_parser.add_argument("--limit", type=int, default=None, help="Limit number of apps to research")
    v1_parser.add_argument("--force", action="store_true", help="Force re-research overwriting existing runs")

    args = parser.parse_args()

    if args.command == "benchmarks":
        from pipeline.benchmarks import run_benchmarks
        do_all = args.all or (not args.composio and not args.mcp)
        run_benchmarks(pull_composio=do_all or args.composio, pull_mcp=do_all or args.mcp, force=args.force)

    elif args.command == "research-v1":
        from pipeline.research import run_v1_pipeline
        run_v1_pipeline(limit=args.limit, force=args.force)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
