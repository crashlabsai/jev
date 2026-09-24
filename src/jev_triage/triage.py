"""Support-ticket triage powered by Jev (TypeSafe System One).

Shows the core TypeSafe patterns:
  - speculative fan-out (ask severity even if it might not be a bug)
  - confidence-gated routing (don't act when the model is unsure)
  - composite scoring (combine spam nouls with weights in code)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

# --- Thresholds you own in code (tune these; don't bake into prompts) ---
TOPIC_CONFIDENCE_FLOOR = 0.75
SPAM_QUARANTINE = 0.60
SPAM_UNCERTAIN_LOW = 0.40
SPAM_UNCERTAIN_HIGH = 0.60
REFUND_LIKELY = 0.70
HIGH_FRUSTRATION = 1.5
HIGH_SEVERITY = 1.5
REPRO_LIKELY = 0.60


def build_state(message: str, customer_plan: str = "pro") -> dict[str, Any]:
    """Only send context the questions need — no noise."""
    return {
        "ticket": {"message": message},
        "customer": {"plan": customer_plan},
        "policy": {
            "sensitive_credentials": ["password", "security code", "API key"],
        },
    }


def build_questions() -> dict[str, Choice | Noul | Score]:
    """One request, many independent questions (speculative fan-out)."""
    return {
        # Choice: closed set of teams
        "topic": Choice(
            instructions="Which team should handle `ticket.message`?",
            criteria={
                "billing": "Charges, invoices, refunds, or subscriptions",
                "technical": "Bugs, outages, integrations, or product failures",
                "account": "Login, profile, permissions, or security",
                "other": "None of the above / unclear",
            },
        ),
        # Score: ordered spectrum of emotion
        "frustration": Score(
            instructions="How frustrated does the customer appear in `ticket.message`?",
            criteria=[
                "Calm, just stating facts",
                "Frustrated but civil",
                "Very angry, strong language, or threatening to leave",
            ],
        ),
        # Speculative: only used if topic == technical
        "bug_severity": Score(
            instructions="If this is a bug report, how severe is the issue in `ticket.message`?",
            criteria=[
                "Cosmetic; no impact to functionality",
                "Broken or degraded feature, but a workaround exists",
                "Blocking issue; no workaround",
            ],
        ),
        # Noul: P(yes) is the signal
        "is_urgent": Noul(
            instructions="Does `ticket.message` convey urgency or time-sensitivity?",
        ),
        "refund_requested": Noul(
            instructions="Does the customer explicitly request a refund or credit in `ticket.message`?",
        ),
        "has_repro_steps": Noul(
            instructions="Does `ticket.message` include steps to reproduce or environment details?",
        ),
        # Spam signals — composed in code below
        "requests_credentials": Noul(
            instructions=(
                "Does `ticket.message` ask the recipient to disclose a sensitive "
                "credential listed in `policy.sensitive_credentials`?"
            ),
        ),
        "unexpected_reward": Noul(
            instructions="Does `ticket.message` announce an unexpected prize, payment, or reward?",
        ),
    }


@dataclass
class Decision:
    action: str
    team: str | None
    priority: str
    reasons: list[str]
    spam_risk: float
    raw: dict[str, Any]


def decide(answers: dict[str, Any]) -> Decision:
    """Compose typed answers into a routing decision. Code owns control flow."""
    topic = answers["topic"]
    frustration = answers["frustration"]
    severity = answers["bug_severity"]
    urgent = answers["is_urgent"].noul
    refund = answers["refund_requested"].noul
    repro = answers["has_repro_steps"].noul

    spam_risk = (
        0.55 * answers["requests_credentials"].noul
        + 0.45 * answers["unexpected_reward"].noul
    )

    reasons: list[str] = []
    priority = "normal"

    if frustration.score >= HIGH_FRUSTRATION and frustration.confidence >= 0.7:
        priority = "high"
        reasons.append(
            f"frustration={frustration.score:.2f} (conf {frustration.confidence:.2f})"
        )
    if urgent >= 0.7:
        priority = "high"
        reasons.append(f"urgency noul={urgent:.2f}")

    # Spam first — independent of topic routing
    if spam_risk >= SPAM_QUARANTINE:
        return Decision(
            action="quarantine_spam",
            team=None,
            priority=priority,
            reasons=reasons + [f"spam_risk={spam_risk:.2f} ≥ {SPAM_QUARANTINE}"],
            spam_risk=spam_risk,
            raw=_raw(answers),
        )

    if SPAM_UNCERTAIN_LOW < spam_risk < SPAM_UNCERTAIN_HIGH:
        return Decision(
            action="human_review",
            team=None,
            priority=priority,
            reasons=reasons + [f"spam_risk={spam_risk:.2f} in uncertain band"],
            spam_risk=spam_risk,
            raw=_raw(answers),
        )

    # Confidence gate: answer says what, confidence says whether to act
    if topic.confidence < TOPIC_CONFIDENCE_FLOOR:
        return Decision(
            action="human_review",
            team=None,
            priority=priority,
            reasons=reasons
            + [
                f"topic confidence {topic.confidence:.2f} < {TOPIC_CONFIDENCE_FLOOR} "
                f"(top pick was {topic.choice!r})"
            ],
            spam_risk=spam_risk,
            raw=_raw(answers),
        )

    team = topic.choice
    reasons.append(f"topic={team} (conf {topic.confidence:.2f})")

    if team == "billing":
        action = "route_billing_refund" if refund >= REFUND_LIKELY else "route_billing"
        if refund >= REFUND_LIKELY:
            reasons.append(f"refund_requested noul={refund:.2f}")
        return Decision(action, team, priority, reasons, spam_risk, _raw(answers))

    if team == "technical":
        if severity.score >= HIGH_SEVERITY and repro >= REPRO_LIKELY:
            reasons.append(
                f"severity={severity.score:.2f}, repro noul={repro:.2f} → escalate"
            )
            return Decision(
                "escalate_engineering",
                team,
                "high",
                reasons,
                spam_risk,
                _raw(answers),
            )
        reasons.append(f"severity={severity.score:.2f}, repro noul={repro:.2f} → backlog")
        return Decision(
            "bug_backlog", team, priority, reasons, spam_risk, _raw(answers)
        )

    if team == "account":
        return Decision(
            "route_account", team, priority, reasons, spam_risk, _raw(answers)
        )

    return Decision(
        "human_review",
        team,
        priority,
        reasons + ["topic=other"],
        spam_risk,
        _raw(answers),
    )


def _raw(answers: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, ans in answers.items():
        if ans.type == "choice":
            out[key] = {
                "choice": ans.choice,
                "confidence": round(ans.confidence, 3),
                "probabilities": {k: round(v, 3) for k, v in ans.probabilities.items()},
            }
        elif ans.type == "score":
            out[key] = {
                "score": round(ans.score, 3),
                "confidence": round(ans.confidence, 3),
                "probabilities": {k: round(v, 3) for k, v in ans.probabilities.items()},
            }
        else:
            out[key] = {"noul": round(ans.noul, 3)}
    return out


def triage(message: str, customer_plan: str = "pro") -> Decision:
    with TypeSafeClient() as client:
        response = client.system_one(
            state=build_state(message, customer_plan),
            questions=build_questions(),
            model="jev-latest",
        )
    return decide(response.answers)
