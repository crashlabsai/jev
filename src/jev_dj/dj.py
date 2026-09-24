"""Mood DJ powered by Jev (TypeSafe System One).

You describe your moment; Jev answers a few typed questions about it; code
picks the music. Jev never names a song — the crate below is ours.

Shows:
  - all three primitives feeding one decision
  - structured Choice criteria (`what` / `not_for`) to separate close vibes
  - speculative fan-out (`wants_lift` only matters if the vibe is melancholy)
  - confidence gate → the DJ asks you instead of guessing
  - typed responses (`response_model=`) instead of answers["..."] lookups
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from typesafe_sdk import (
    Choice,
    ChoiceAnswer,
    Noul,
    NoulAnswer,
    Score,
    ScoreAnswer,
    SystemOneResponse,
    TypeSafeClient,
)

# --- Thresholds you own in code (tune these; don't bake into prompts) ---
VIBE_CONFIDENCE_FLOOR = 0.60
ENERGY_CONFIDENCE_FLOOR = 0.60
LIFT_LIKELY = 0.60
LYRICS_OK = 0.50
NOSTALGIA_LIKELY = 0.60

# Score levels, lowest → highest. Index = energy level = column in CRATE.
ENERGY_LEVELS = [
    "Barely there: near-silent, ambient, sleepy",
    "Low-key: soft, slow tempo",
    "Grooving: steady mid-tempo, head-nodding",
    "Full send: loud, fast, high-intensity",
]

# The DJ's crate: vibe → genre seed per energy level.
# "uplift" is not a Jev option — code derives it from melancholy + wants_lift.
CRATE: dict[str, list[str]] = {
    "focus": ["ambient drone", "lofi beats", "chillhop", "synthwave"],
    "chill": ["ambient", "acoustic folk", "neo soul", "indie pop"],
    "hype": ["downtempo", "funk", "hip hop", "drum and bass"],
    "melancholy": ["sad piano", "slowcore", "indie folk", "emo"],
    "romantic": ["jazz ballads", "bedroom r&b", "classic soul", "disco"],
    "uplift": ["morning acoustic", "feel good indie", "motown", "pop anthems"],
}


def build_state(message: str, now: datetime) -> dict[str, Any]:
    """Code supplies the facts it already knows (the clock) so Jev never guesses them."""
    return {
        "listener": {"message": message},
        "clock": {"local_time": now.strftime("%H:%M"), "weekday": now.strftime("%A")},
    }


def build_questions() -> dict[str, Choice | Noul | Score]:
    """One request, five independent questions."""
    return {
        # Choice with structured criteria: `not_for` sharpens neighbouring vibes
        "vibe": Choice(
            instructions={
                "question": "What is the vibe of the listener's moment in `listener.message`?",
                "focus": "Classify how the listener feels or what they are doing now, not the music they hope for.",
            },
            criteria={
                "focus": {
                    "what": "Concentrating: studying, coding, writing, deep work",
                    "not_for": "Winding down or relaxing with no task",
                },
                "chill": {
                    "what": "Unwinding: slow mornings, cooking, lazy afternoons",
                    "not_for": "Tasks that need concentration",
                },
                "hype": {
                    "what": "Getting pumped: workouts, pre-game, cleaning, celebrating",
                    "not_for": "Relaxing, concentrating, or feeling low",
                },
                "melancholy": {
                    "what": "Feeling sad, low, heartbroken, lonely, or grieving",
                    "not_for": "Warm, happy love",
                },
                "romantic": {
                    "what": "Date night, affection, being in love",
                    "not_for": "Heartbreak or loss",
                },
            },
        ),
        # Score: ordered spectrum; the answer can land between levels
        "energy": Score(
            instructions=(
                "How much energy should the music have for the listener in "
                "`listener.message`, given it is `clock.local_time`?"
            ),
            criteria=ENERGY_LEVELS,
        ),
        # Noul with criteria describing each side
        "lyrics_ok": Noul(
            instructions="Would vocals and lyrics fit what the listener in `listener.message` is doing?",
            criteria={
                "true": "Words won't get in the way: driving, working out, relaxing, feeling feelings",
                "false": "Words would distract: reading, writing, studying, or falling asleep",
            },
        ),
        # Speculative: only used if vibe == melancholy
        "wants_lift": Noul(
            instructions=(
                "Does the listener in `listener.message` want music to change "
                "their mood rather than match it?"
            ),
        ),
        "nostalgic": Noul(
            instructions="Is the listener in `listener.message` in the mood for throwbacks or songs from their past?",
        ),
    }


class DJAnswers(SystemOneResponse):
    """Typed response: each question ID becomes an attribute with its answer type."""

    vibe: ChoiceAnswer
    energy: ScoreAnswer
    lyrics_ok: NoulAnswer
    wants_lift: NoulAnswer
    nostalgic: NoulAnswer


@dataclass
class Spin:
    vibe: str
    energy_level: int
    query: str
    url: str
    reasons: list[str]
    ask_listener: list[str]  # non-empty → vibe confidence too low; offer these
    answers: DJAnswers


def energy_level(energy: ScoreAnswer) -> int:
    """Turn Jev's energy Score into a crate column (0 .. len(ENERGY_LEVELS) - 1).

    Available on `energy`:
      .score          expected value over levels, e.g. 1.36 (can sit between levels)
      .probabilities  {0: 0.05, 1: 0.55, 2: 0.40, 3: 0.0}  (int level → probability)
      .confidence     how peaked those probabilities are (0..1)
    """
    probs = energy.probabilities
    likeliest = max(probs, key=probs.__getitem__)
    if energy.confidence >= ENERGY_CONFIDENCE_FLOOR:
        level = likeliest
    else:
        # Unsure: too quiet is a smaller miss than too loud, so lean calmer
        level = min(likeliest, int(energy.score))
    return max(0, min(level, len(ENERGY_LEVELS) - 1))


def top_vibes(vibe: ChoiceAnswer, n: int = 2) -> list[str]:
    return sorted(vibe.probabilities, key=vibe.probabilities.__getitem__, reverse=True)[:n]


def spin(answers: DJAnswers, vibe_override: str | None = None) -> Spin:
    """Compose typed answers into a track pick. Code owns control flow.

    `vibe_override` lets the listener settle a low-confidence vibe without a
    second API call — the other answers were independent, so they still hold.
    """
    vibe = answers.vibe
    reasons: list[str] = []
    ask: list[str] = []

    if vibe_override:
        pick = vibe_override
        reasons.append(f"listener picked {pick!r}")
    else:
        pick = vibe.choice
        reasons.append(f"vibe={pick} (conf {vibe.confidence:.2f})")
        # Confidence gate: answer says what, confidence says whether to act
        if vibe.confidence < VIBE_CONFIDENCE_FLOOR:
            ask = top_vibes(vibe)
            reasons.append(
                f"conf < {VIBE_CONFIDENCE_FLOOR} → ask listener: {' or '.join(ask)}"
            )

    # Speculative fan-out: wants_lift came back for every request; only matters here
    lift = answers.wants_lift.noul
    if pick == "melancholy" and lift >= LIFT_LIKELY:
        pick = "uplift"
        reasons.append(f"wants_lift noul={lift:.2f} → flip the mood to uplift")

    level = energy_level(answers.energy)
    reasons.append(
        f"energy score={answers.energy.score:.2f} "
        f"(conf {answers.energy.confidence:.2f}) → level {level}"
    )

    words = [CRATE[pick][level]]
    nostalgic = answers.nostalgic.noul
    if nostalgic >= NOSTALGIA_LIKELY:
        words.insert(0, "throwback")
        reasons.append(f"nostalgic noul={nostalgic:.2f}")
    lyrics = answers.lyrics_ok.noul
    if lyrics < LYRICS_OK:
        words.append("instrumental")
        reasons.append(f"lyrics_ok noul={lyrics:.2f} → instrumental")

    query = " ".join(words)
    return Spin(
        vibe=pick,
        energy_level=level,
        query=query,
        url=f"https://open.spotify.com/search/{quote(query)}",
        reasons=reasons,
        ask_listener=ask,
        answers=answers,
    )


def ask_jev(message: str, now: datetime | None = None) -> DJAnswers:
    with TypeSafeClient() as client:
        return client.system_one(
            state=build_state(message, now or datetime.now()),
            questions=build_questions(),
            model="jev-latest",
            response_model=DJAnswers,
        )
