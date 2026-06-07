#!/usr/bin/env python3
"""Hermes self-improving trading agent — CLI entry point."""

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def main():
    parser = argparse.ArgumentParser(description="Hermes Self-Improving Trading Agent")
    parser.add_argument("--demo", action="store_true", help="Use isolated .demo.json memory files")
    parser.add_argument("--once", action="store_true", help="Run a single learning cycle and exit")
    parser.add_argument("--status", action="store_true", help="Show baseline and recent experiments")
    parser.add_argument("--interval", type=int, default=None, help="Loop interval in seconds")
    args = parser.parse_args()

    if args.demo:
        os.environ["DEMO_MODE"] = "1"
        print("🧪 Hermes demo mode — using hermes/memory/*.demo.json")

    from hermes.agent import HermesAgent

    agent = HermesAgent()

    if args.status:
        print(agent.status())
        return 0

    if args.once:
        print("🔄 Running one Hermes learning cycle...")
        result = agent.run_cycle()
        print(result.summary)
        print(f"Verdict: {result.verdict} | Sharpe {result.baseline_sharpe} → {result.variant_sharpe}")
        return 0

    print("🤖 Hermes agent starting 24/7 learning loop (Ctrl+C to stop)")
    try:
        agent.run_loop(interval_sec=args.interval)
    except KeyboardInterrupt:
        print("\nHermes agent stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())