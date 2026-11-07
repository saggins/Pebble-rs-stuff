"""Classify a voice-note transcript into one category using an OpenAI-compatible LLM server
(Ollama by default; LM Studio, llama.cpp etc. work too).

Confidence is the model's actual probability for the chosen label token
(from logprobs), not a number the model writes about itself.

Adding a category: append to CATEGORIES. Its `label` must be one short
lowercase word whose first token differs from every other label's, since
confidence is read from the first token the model emits.
"""

import math
import os
from dataclasses import dataclass, field
from typing import Any

import httpx

LLM_URL = os.environ.get("LLM_URL", "http://127.0.0.1:11434/v1").rstrip("/")
LLM_MODEL = os.environ.get("LLM_MODEL", "gemma4:e2b")
LLM_REASONING_EFFORT = os.environ.get("LLM_REASONING_EFFORT", "none")
LLM_TIMEOUT_S = 180


@dataclass(frozen=True)
class Category:
    label: str  # single word the model answers with
    name: str  # human-readable
    description: str  # what belongs here; this is what the model actually decides on


CATEGORIES: list[Category] = [
    Category(
        "home",
        "Home Assistant Commands",
        "An instruction to control or query a smart-home device right now: lights, switches, "
        "thermostat, locks, blinds, media players, scenes. E.g. 'turn off the kitchen lights', "
        "'set the heat to 70', 'is the garage door closed'.",
    ),
    Category(
        "todo",
        "CalDav TODO",
        "A task the user needs to do, with no fixed meeting time: chores, errands, things to buy, "
        "follow-ups. May have a due date. E.g. 'remind me to buy milk', 'call the dentist this week'.",
    ),
    Category(
        "event",
        "CalDav Events",
        "Something happening at a specific date/time that belongs on a calendar: appointments, "
        "meetings, reservations, plans with people. E.g. 'dinner with Sam Friday at 7', "
        "'dentist appointment on the 14th at 3pm'.",
    ),
    Category(
        "note",
        "Notes-to-self",
        "An idea, thought, observation or piece of information to remember, with no action and no "
        "time attached. E.g. 'idea: app that tracks plant watering', 'the wifi password is on the router'.",
    ),
]

_BY_LABEL = {c.label: c for c in CATEGORIES}


def _system_prompt() -> str:
    lines = [
        "You sort voice notes dictated into a smart ring. The text is an automatic transcript "
        "and may contain recognition errors.",
        "Pick the single best category:",
        "",
    ]
    lines += [f"{c.label}: {c.description}" for c in CATEGORIES]
    lines += ["", f"Answer with exactly one word from: {', '.join(_BY_LABEL)}. No other text."]
    return "\n".join(lines)


@dataclass
class Classification:
    category: Category | None  # None if the model answered something off-list
    raw_answer: str
    confidence: float | None  # P(chosen label) from logprobs, None if unavailable
    # P(label) for every label that appeared in the model's top alternatives.
    distribution: dict[str, float] = field(default_factory=dict)


async def classify(text: str) -> Classification:
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 5,
        "logprobs": True,
        "top_logprobs": max(5, len(CATEGORIES) + 2),
    }
    if LLM_REASONING_EFFORT:
        payload["reasoning_effort"] = LLM_REASONING_EFFORT

    async with httpx.AsyncClient(timeout=LLM_TIMEOUT_S) as client:
        r = await client.post(f"{LLM_URL}/chat/completions", json=payload)
    if r.status_code != 200:
        raise RuntimeError(f"LLM server {r.status_code}: {r.text[:500]}")

    choice = r.json()["choices"][0]
    raw = (choice["message"].get("content") or "").strip()
    category = _BY_LABEL.get(raw.lower().strip(" .\"'"))

    confidence, distribution = None, {}
    tokens = (choice.get("logprobs") or {}).get("content") or []
    if tokens:
        first = tokens[0]
        for alt in first.get("top_logprobs", []):
            label = alt["token"].strip().lower()
            if label in _BY_LABEL:
                distribution[label] = distribution.get(label, 0.0) + math.exp(alt["logprob"])
        if category is not None:
            confidence = distribution.get(category.label, math.exp(first["logprob"]))

    return Classification(category, raw, confidence, distribution)


def _pct(p: float) -> str:
    if p >= 0.9995:
        return ">99.9%"
    if p < 0.001:
        return "<0.1%"
    return f"{p:.1%}"


def format_result(transcript: str, c: Classification) -> str:
    name = c.category.name if c.category else f"UNKNOWN (model said {c.raw_answer!r})"
    lines = [f"transcript: {transcript}", f"category:   {name}"]
    if c.confidence is not None:
        lines.append(f"confidence: {_pct(c.confidence)}")
        others = sorted(
            ((l, p) for l, p in c.distribution.items() if c.category is None or l != c.category.label),
            key=lambda x: -x[1],
        )
        if others:
            lines.append("runner-up:  " + ", ".join(f"{_BY_LABEL[l].name} {_pct(p)}" for l, p in others))
    return "\n".join(lines)
