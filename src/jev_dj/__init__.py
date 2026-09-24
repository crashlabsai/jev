"""CLI entry: `uv run jev-dj "your moment"` or `uv run jev-dj --demo`."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from jev_dj import spotify
from jev_dj.dj import ENERGY_LEVELS, Spin, ask_jev, spin

# Load .env from project root (TYPESAFE_API_KEY)
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

SAMPLES = [
    ("deep_work", "Big deadline tomorrow. Need to crank through this code for the next three hours."),
    ("leg_day", "Leg day. About to go for a new squat PR, let's GO."),
    ("heartbreak", "She left last night. I just want to lie here and feel it."),
    ("cheer_me_up", "Rough week and I'm feeling low, but I want something to pull me out of it."),
    ("sunday", "Rainy Sunday morning, making pancakes, no plans."),
    ("road_trip", "Driving back to my hometown for the first time in ten years."),
    ("finals_heartbreak", "Studying for finals but honestly I'm heartbroken."),
    ("laundry", "Folding laundry."),
]


def bar(p: float, width: int = 24) -> str:
    filled = round(p * width)
    return "█" * filled + "·" * (width - filled)


def format_spin(label: str, message: str, s: Spin, show_raw: bool) -> str:
    a = s.answers
    lines = [
        f"═══ {label} ═══",
        f"moment: {message}",
        "",
        "vibe probabilities (Choice):",
    ]
    for vibe, p in sorted(a.vibe.probabilities.items(), key=lambda kv: -kv[1]):
        lines.append(f"  {vibe:<11} {bar(p)} {p:.2f}")
    lines += [
        "",
        f"♪ vibe:   {s.vibe}",
        f"♪ energy: {a.energy.score:.2f} → level {s.energy_level} ({ENERGY_LEVELS[s.energy_level]})",
        f"♪ crate:  {s.query}",
        f"♪ play:   {s.url}",
        "→ reasons:",
    ]
    for r in s.reasons:
        lines.append(f"    • {r}")
    if show_raw:
        lines.append("")
        lines.append("raw answers (what Jev returned):")
        raw = {k: v.model_dump() for k, v in a.answers.items()}
        lines.append(json.dumps(raw, indent=2))
    lines.append("")
    return "\n".join(lines)


def settle_vibe(s: Spin) -> str | None:
    """Confidence gate, human side: let the listener break the tie."""
    options = " / ".join(s.ask_listener)
    pick = input(f"🎧 Not sure what you need — {options}? ").strip().lower()
    return pick if pick in s.ask_listener else None


def parse_time(value: str) -> datetime:
    t = datetime.strptime(value, "%H:%M")
    return datetime.now().replace(hour=t.hour, minute=t.minute)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pick music for your moment with Jev (TypeSafe System One)."
    )
    parser.add_argument(
        "message",
        nargs="?",
        help="Describe your moment. Omit with --demo to run fixtures.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run the built-in sample moments.",
    )
    parser.add_argument(
        "--time",
        type=parse_time,
        default=None,
        help="Pretend it's this local time (HH:MM). Try 07:00 vs 23:30.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Also print the raw typed answers.",
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Start the pick on your Spotify device (connect once in `jev-dj-ui` first).",
    )
    args = parser.parse_args()

    if args.demo:
        for label, message in SAMPLES:
            s = spin(ask_jev(message, args.time))
            print(format_spin(label, message, s, args.raw))
        return

    if not args.message:
        parser.print_help()
        sys.exit(1)

    answers = ask_jev(args.message, args.time)
    s = spin(answers)
    if s.ask_listener and sys.stdin.isatty():
        pick = settle_vibe(s)
        if pick:
            # No second Jev call: the other answers were independent of the vibe
            s = spin(answers, vibe_override=pick)
    print(format_spin("moment", args.message, s, args.raw))
    if args.play:
        play(s)


def play(s: Spin) -> None:
    try:
        played = spotify.play_query(s.query)
    except spotify.SpotifyError as error:
        print(f"⚠ Spotify: {error}")
        sys.exit(1)
    p = played.playlist
    by = f" by {p['owner']}" if p["owner"] else ""
    print(f"▶ Playing {p['name']!r}{by} on {played.device['name']}")
    print(f"  {p['url']}")


if __name__ == "__main__":
    main()
