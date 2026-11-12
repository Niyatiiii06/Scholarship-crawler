"""Scholarship Intelligence Crawler - command line.

    python main.py run                 one full cycle: re-verify stored scholarships, then discover new ones
    python main.py run --no-discover   only re-verify (fast; shows change and expiry detection)
    python main.py history             compare with older Internet Archive copies to find real past changes
    python main.py watch --hours 24    keep running: one cycle every 24 hours
    python main.py reground            rebuild all records from saved LLM answers (no LLM calls) after a code update
    python main.py check               test the LLM keys and models, and print the exact error if one fails
    python main.py status              counts
    python main.py export              write data/scholarships.csv, .json and changes.csv
    streamlit run app.py               open the dashboard
"""
import argparse
import sys
import time

from crawler import export, llm, pipeline, storage
from crawler.config import load_config, setup_logging


def print_status(conn):
    for label, value in export.stats(conn).items():
        print(f"{label:28s}: {value}")


def main():
    parser = argparse.ArgumentParser(description="Scholarship Intelligence Crawler")
    parser.add_argument("command", choices=["run", "history", "watch", "status", "export", "check", "reground"])
    parser.add_argument("--no-discover", action="store_true", help="only re-verify stored scholarships")
    parser.add_argument("--limit", type=int, default=10, help="history: how many scholarships to compare")
    parser.add_argument("--hours", type=float, default=24, help="watch: hours between runs")
    parser.add_argument("--no-llm", action="store_true", help="allow a run with pattern matching only")
    parser.add_argument("--data-dir", help="folder for the database (default: data)")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config(args.data_dir)
    conn = storage.connect(cfg["paths"]["db"])

    if args.command == "check":
        if not llm.self_test(cfg):
            print("\nNo key was found. Check that .env is next to main.py and has a GROQ_API_KEY= line.")
        conn.close()
        return 0
    if args.command in ("run", "watch", "history") and not llm.is_available(cfg):
        print("STOPPED: no LLM key found in .env. Run `python main.py check` to see what is missing.")
        print("(To run without an LLM anyway, using weak pattern matching, add --no-llm.)")
        if not args.no_llm:
            conn.close()
            return 1
    if args.command == "reground":
        pipeline.reground(cfg, conn)
        export.export_all(cfg, conn)
        print()
        print_status(conn)
    elif args.command == "run":
        pipeline.run(cfg, conn, discover=not args.no_discover)
        export.export_all(cfg, conn)
        print()
        print_status(conn)
    elif args.command == "history":
        done = pipeline.backfill_history(cfg, conn, args.limit)
        export.export_all(cfg, conn)
        print("\nCompared", done, "scholarships with archived copies.")
        print_status(conn)
    elif args.command == "watch":
        while True:
            pipeline.run(cfg, conn)
            export.export_all(cfg, conn)
            print("Next run in", args.hours, "hours. Press Ctrl+C to stop.")
            time.sleep(args.hours * 3600)
    elif args.command == "status":
        print_status(conn)
    elif args.command == "export":
        for path in export.export_all(cfg, conn):
            print(" ", path)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
