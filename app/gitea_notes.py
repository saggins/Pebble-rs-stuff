"""Save "note" transcripts as Markdown files in a Gitea repo (no local clone).

Uses the Gitea contents API: POST /repos/{owner}/{repo}/contents/{path} creates
the file and a commit server-side in one request.

Filenames follow the vault's Zettelkasten scheme: YYYYMMDDHHMM.md in local time
of the recording. If that minute is taken, the next free minute is used.

Env:
  GITEA_TOKEN       required; token with write:repository scope
  GITEA_URL         default https://workhorse.comet-court.ts.net:8445
  GITEA_REPO        default sangmin/school-notes
  GITEA_BRANCH      default main
  GITEA_NOTES_DIR   default "💭Fleeting Notes"
  NOTES_TZ          IANA zone for filenames (default: system local time)
  NOTES_DRY_RUN     1 = print the note instead of uploading
"""

import base64
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

GITEA_URL = os.environ.get("GITEA_URL", "https://workhorse.comet-court.ts.net:8445").rstrip("/")
GITEA_REPO = os.environ.get("GITEA_REPO", "sangmin/school-notes")
GITEA_BRANCH = os.environ.get("GITEA_BRANCH", "main")
GITEA_NOTES_DIR = os.environ.get("GITEA_NOTES_DIR", "💭Fleeting Notes").strip("/")
NOTES_DRY_RUN = os.environ.get("NOTES_DRY_RUN", "") == "1"
_TZ: tzinfo | None = ZoneInfo(os.environ["NOTES_TZ"]) if os.environ.get("NOTES_TZ") else None

MAX_MINUTE_BUMPS = 10
TIMEOUT_S = 30


@dataclass
class NoteResult:
    path: str
    url: str | None  # web URL of the created file, None on dry run


def note_filename(recorded_at: datetime) -> str:
    return recorded_at.astimezone(_TZ).strftime("%Y%m%d%H%M") + ".md"


def render_note(transcript: str, recorded_at: datetime, confidence: float | None) -> str:
    """Markdown file contents for one ring note: frontmatter like the vault's hand-made
    fleeting notes, then the raw transcript as a blockquote."""
    lines = [
        "---",
        "tags:",
        "  - ring",
        "Category: 🔵 Inbox (unsorted voice note)",
        f"recorded: {recorded_at.astimezone(_TZ).isoformat(timespec='seconds')}",
    ]
    if confidence is not None:
        lines.append(f"classifier_confidence: {confidence:.2f}")
    lines += ["---", "", f"> {transcript.strip()}", ""]
    return "\n".join(lines)


async def save_note(transcript: str, recorded_at: datetime, confidence: float | None) -> NoteResult:
    body = render_note(transcript, recorded_at, confidence)
    if NOTES_DRY_RUN:
        path = f"{GITEA_NOTES_DIR}/{note_filename(recorded_at)}"
        print(f"--- dry run: would create {path} ---\n{body}", flush=True)
        return NoteResult(path, None)

    token = os.environ.get("GITEA_TOKEN")
    if not token:
        raise RuntimeError("GITEA_TOKEN is not set")

    payload = {
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": GITEA_BRANCH,
    }
    headers = {"Authorization": f"token {token}"}
    async with httpx.AsyncClient(timeout=TIMEOUT_S, headers=headers) as client:
        for bump in range(MAX_MINUTE_BUMPS):
            path = f"{GITEA_NOTES_DIR}/{note_filename(recorded_at + timedelta(minutes=bump))}"
            api = f"{GITEA_URL}/api/v1/repos/{GITEA_REPO}/contents/{quote(path)}"
            r = await client.post(api, json={**payload, "message": f"ring note {path.rsplit('/', 1)[-1]}"})
            if r.status_code == 201:
                return NoteResult(path, r.json().get("content", {}).get("html_url"))
            if r.status_code == 422 and "exist" in r.text.lower():
                continue  # minute already taken: try the next one
            raise RuntimeError(f"Gitea {r.status_code}: {r.text[:300]}")
    raise RuntimeError(f"no free filename within {MAX_MINUTE_BUMPS} minutes of {recorded_at}")


def format_note_result(r: NoteResult) -> str:
    return f"note saved: {r.url or r.path}" + ("" if r.url else " (dry run)")
