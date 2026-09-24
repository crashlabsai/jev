"""CLI entry: `uv run jev-triage "your ticket text"` or `uv run jev-triage --demo`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from jev_triage.triage import Decision, triage

# Load .env from project root (TYPESAFE_API_KEY)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SAMPLES = [
    (
        "billing_refund",
        "I was charged twice for order A-104. Please refund the duplicate ASAP — I'm losing sales.",
    ),
    (
        "tech_bug",
        "Export to PDF hangs forever on Chrome 128 / macOS. Steps: open report → Export → PDF. CSV still works.",
    ),
    (
        "account",
        "I can't sign in after resetting my password. The reset email never arrives.",
    ),
    (
        "spammy",
        "Congratulations! You have been selected for a $1,000 bonus. Reply with your password to claim.",
    ),
    (
        "ambiguous",
        "Something feels off with my account and also the invoice looks weird maybe?",
    ),
]


def format_decision(label: str, message: str, d: Decision) -> str:
    lines = [
        f"═══ {label} ═══",
        f"ticket: {message}",
        "",
        f"→ action:   {d.action}",
        f"→ team:     {d.team}",
        f"→ priority: {d.priority}",
        f"→ spam_risk:{d.spam_risk:.3f}",
        "→ reasons:",
    ]
    for r in d.reasons:
        lines.append(f"    • {r}")
    lines.append("")
    lines.append("raw answers (what Jev returned):")
    lines.append(json.dumps(d.raw, indent=2))
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Triage a support ticket with Jev (TypeSafe System One)."
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Ticket text to triage. Omit with --demo to run fixtures.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the built-in sample tickets.",
    )
    parser.add_argument(
        "--plan",
        default="pro",
        help="Customer plan label included in state (default: pro).",
    )
    args = parser.parse_args()

    if args.demo:
        for label, message in SAMPLES:
            decision = triage(message, customer_plan=args.plan)
            print(format_decision(label, message, decision))
        return

    if not args.message:
        parser.print_help()
        sys.exit(1)

    decision = triage(args.message, customer_plan=args.plan)
    print(format_decision("ticket", args.message, decision))


if __name__ == "__main__":
    main()
