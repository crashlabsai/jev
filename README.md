# jev-triage

A tiny learning project for [Jev](https://docs.typesafe.ai/) (TypeSafe's System One model).

**Idea:** your code owns control flow. Jev only answers narrow typed questions about a support ticket. You combine those answers into a routing decision.

## What it teaches

| Concept | Where |
|---------|--------|
| Choice / Score / Noul | `src/jev_triage/triage.py` → `build_questions()` |
| Speculative fan-out | `bug_severity` + `has_repro_steps` asked even when it might not be a bug |
| Confidence-gated routing | `TOPIC_CONFIDENCE_FLOOR` → `human_review` |
| Composite scoring | `spam_risk` = weighted nouls in `decide()` |

## Setup

```bash
cp .env.example .env
# Add your TypeSafe API key to .env (never commit it).
uv sync
```

## Run

```bash
# Built-in fixtures (billing, bug, account, spam, ambiguous)
uv run jev-triage --demo

# Your own ticket
uv run jev-triage "My payouts have been failing for 3 days. Please help ASAP."
```

## How one call works

```
state (ticket + policy) + ~8 questions
        │
        ▼
  POST /v1/systemone   (jev-latest)
        │
        ▼
 typed answers + probabilities + confidence
        │
        ▼
 decide() in Python  →  route / escalate / quarantine / human_review
```

Questions in one request are evaluated **in parallel and independently**. Asking an extra speculative question is cheap; a second sequential API call is usually the wrong optimization.

---

# jev-dj

> **New to Jev?** Start with [How jev-dj works](docs/how-jev-dj-works.md): one real spin, stop by stop, with diagrams.

A creative second project: describe your moment and the DJ picks the music. Jev never names a song. It answers five typed questions, and `spin()` in Python maps those answers onto a crate of genres we own.

| Concept | Where |
|---------|--------|
| Structured Choice criteria (`what` / `not_for`) | `src/jev_dj/dj.py` → `vibe` in `build_questions()` |
| Score between levels → a discrete pick | `energy_level()` |
| Code-supplied state (the clock) | `build_state()`; try `--time 07:00` vs `--time 23:30` |
| Speculative fan-out | `wants_lift` is asked every time, used only when the vibe is melancholy |
| Confidence gate → ask the human | `VIBE_CONFIDENCE_FLOOR`; the CLI prompts you to break the tie |
| Typed responses | `DJAnswers(SystemOneResponse)` + `response_model=` |

## Run

```bash
uv run jev-dj --demo                         # 8 sample moments
uv run jev-dj "Folding laundry."             # low confidence → the DJ asks you
uv run jev-dj --raw --time 23:30 "Can't sleep, thinking about her."
```

## How one spin works

![Eight stops from your moment through Jev and Python to Spotify](docs/images/jev-dj-overview.png)

```
state (listener.message + clock) + 5 questions
        │
        ▼
  POST /v1/systemone   (jev-latest, response_model=DJAnswers)
        │
        ▼
 vibe (Choice) · energy (Score) · lyrics_ok / wants_lift / nostalgic (Noul)
        │
        ▼
 spin() in Python → CRATE[vibe][energy_level] + "throwback"/"instrumental" → Spotify search
```

### End-to-end sequence

![Sequence diagram of the browser, Python server, Jev API, and Spotify](docs/images/jev-dj-sequence.png)

### How spin() picks a query

![Decision flow from five typed answers to a Spotify search query](docs/images/jev-dj-decision-flow.png)

## Web UI

```bash
uv run jev-dj-ui              # http://127.0.0.1:8000  (--port to change)
```

The same `ask_jev()` + `spin()`, drawn instead of printed, in a retro pixel style. **jevbot** (a pixel crab in DJ headphones) runs a pixel turntable, and there's one LED-segment chart per primitive: vibe probabilities (Choice), energy levels with the expected-score line (Score), and Noul meters with the thresholds your code uses. "How jevbot decided" draws `spin()` as a row of gates, next to the `CRATE` grid with the picked cell lit.

**Inside jevbot** is an 8-stop strip (you → state → ask → Jev → answers → spin() → crate → play) that lights up live during each spin; the packet waits at *Jev* for as long as the API call takes. Click any stop, or **? How it works**, to open a step-by-step walkthrough of that exact spin: the `state` that was sent, the five typed questions, the one parallel call, the answers, each gate in `spin()`, and the crate lookup. Use ← → to step, or turn on Auto.

The server (`src/jev_dj/web.py`, stdlib only) keeps your API key off the page and keeps recent answers in memory, so breaking a tie in the browser re-spins without calling Jev again.

### Screenshots

#### Light theme

![Jev DJ web UI in the light theme after a sample spin](docs/images/jev-dj-light.png)

#### Dark theme

![Jev DJ web UI in the dark theme after a sample spin](docs/images/jev-dj-dark.png)

## Play on your Spotify

Jev picks the crate query; `src/jev_dj/spotify.py` searches Spotify for a matching playlist and starts it on your device.

1. Create an app at [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) (needs Spotify Premium since Feb 2026). Tick **Web API** and add your redirect URI, e.g. `http://127.0.0.1:8000/callback` when running locally.
2. Add it to `.env` (no client secret needed: login uses PKCE):
   ```bash
   SPOTIFY_CLIENT_ID=...
   SPOTIFY_REDIRECT_URI=http://127.0.0.1:8000/callback   # must match the dashboard exactly
   ```
3. `uv run jev-dj-ui`, click **Connect Spotify**, open Spotify on your phone or laptop, spin, then **▶ Play on my Spotify** (or tick auto-play).
4. From the CLI after connecting once: `uv run jev-dj --play "Leg day, let's GO."`

Tokens are saved in `.spotify-token.json` (gitignored). Spotify makes you log in again six months after connecting.

If the vibe confidence is low, the CLI asks you to choose and runs `spin()` again with your pick. It doesn't call Jev a second time: the other four answers were independent of the vibe, so they still hold.
