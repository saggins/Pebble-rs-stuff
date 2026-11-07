"""Recording pipeline: m4a -> whisper.cpp transcript -> category (LLM) -> stdout.

Env:
  WHISPER_MODEL          path to ggml model (flake shellHook sets ./models/ggml-base.en.bin)
  WHISPER_LANG           spoken language, or 'auto' (default: en)
  WHISPER_THREADS        whisper-cli threads (default: 4)
  LLM_URL / LLM_MODEL / LLM_REASONING_EFFORT: see app/classify.py

Reprocess a saved recording by hand:
  python -m app.pipeline recordings/<stem>.m4a
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from datetime import datetime, timezone

from app.classify import Classification, classify, format_result
from app.gitea_notes import format_note_result, save_note
from app.home_assistant import format_home_result, run_home_command

log = logging.getLogger("pipeline")

# Acting on real devices: only when the classifier is sure this is a home command.
HA_MIN_CONFIDENCE = float(os.environ.get("HA_MIN_CONFIDENCE", "0.9"))


async def _handle_home(text: str, c: Classification, event: dict[str, Any]) -> str:
    if c.confidence is not None and c.confidence < HA_MIN_CONFIDENCE:
        return f"home assistant: skipped, confidence {c.confidence:.0%} < {HA_MIN_CONFIDENCE:.0%}"
    return format_home_result(await run_home_command(text))


async def _handle_note(text: str, c: Classification, event: dict[str, Any]) -> str:
    ts = event.get("recorded_at")
    recorded_at = datetime.fromisoformat(ts) if ts else datetime.now(timezone.utc)
    return format_note_result(await save_note(text, recorded_at, c.confidence))


# category label -> action(text, classification, event). Categories without a handler
# are only printed (for now).
HANDLERS = {"home": _handle_home, "note": _handle_note}


def _require_bin(env_var: str, name: str) -> str:
    """Resolve an executable to an absolute path at import time, so a missing tool
    stops the server at startup instead of failing on the first recording."""
    path = shutil.which(os.environ.get(env_var, name))
    if path is None:
        raise SystemExit(
            f"'{name}' not found on PATH. Start uvicorn from inside the dev shell "
            f"(direnv / nix develop), or set {env_var}=/path/to/{name}.\n"
            f"PATH={os.environ.get('PATH', '')}"
        )
    return path


FFMPEG = _require_bin("FFMPEG", "ffmpeg")
WHISPER_CLI = _require_bin("WHISPER_CLI", "whisper-cli")

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "models/ggml-base.en.bin")
WHISPER_LANG = os.environ.get("WHISPER_LANG", "en")
WHISPER_THREADS = os.environ.get("WHISPER_THREADS", "4")

SUBPROCESS_TIMEOUT_S = 300

# whisper is CPU/GPU heavy: process one recording at a time.
_pipeline_lock = asyncio.Lock()


class PipelineError(RuntimeError):
    pass


async def _run(*cmd: str) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        raise PipelineError(f"executable not found: {cmd[0]}")
    try:
        out, err = await asyncio.wait_for(proc.communicate(), SUBPROCESS_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise PipelineError(f"{cmd[0]} timed out after {SUBPROCESS_TIMEOUT_S}s")
    if proc.returncode != 0:
        raise PipelineError(f"{cmd[0]} exited {proc.returncode}: {err.decode(errors='replace')[-500:]}")
    return out.decode(errors="replace")


async def transcribe(audio_path: Path) -> str:
    """m4a -> 16kHz mono wav (ffmpeg) -> text (whisper-cli)."""
    if not Path(WHISPER_MODEL).is_file():
        raise PipelineError(f"whisper model not found: {WHISPER_MODEL}")
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        await _run(
            FFMPEG, "-nostdin", "-loglevel", "error", "-y",
            "-i", str(audio_path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav),
        )
        text = await _run(
            WHISPER_CLI, "-m", WHISPER_MODEL, "-f", str(wav),
            "-l", WHISPER_LANG, "-t", WHISPER_THREADS, "--no-timestamps", "--no-prints",
        )
    return " ".join(text.split())  # collapse whisper's per-segment newlines


async def process_recording(
    stem: Path,
    audio_path: Path | None,
    ring_transcription: str | None,
    event: dict[str, Any],
    delete_audio: bool = False,
) -> None:
    """Background task. Writes <stem>.whisper.txt, prints transcript + category to stdout,
    updates event in place. With delete_audio, removes audio_path when done (success or not).

    Never raises: errors go to the log, <stem>.error.txt and event["pipeline_error"].
    """
    event["pipeline_status"] = "queued"
    async with _pipeline_lock:
        try:
            event["pipeline_status"] = "transcribing"
            if audio_path is not None:
                text = await transcribe(audio_path)
                stem.with_suffix(".whisper.txt").write_text(text, encoding="utf-8")
                event["whisper_text"] = text
            elif ring_transcription:
                text = ring_transcription  # transcription-only payload mode: no audio to run
            else:
                raise PipelineError("no audio and no transcription")

            if not text:
                raise PipelineError("transcript is empty (silence?)")

            event["pipeline_status"] = "classifying"
            result = await classify(text)
            event["category"] = result.category.name if result.category else None
            event["confidence"] = result.confidence
            report = format_result(text, result)

            handler = HANDLERS.get(result.category.label) if result.category else None
            if handler is not None:
                event["pipeline_status"] = "acting"
                try:
                    action = await handler(text, result, event)
                except Exception as e:  # classification still printed even if the action fails
                    log.exception("handler failed stem=%s", stem.name)
                    action = f"action failed: {e}"
                event["action"] = action
                report += "\n" + action

            event["pipeline_status"] = "done"
            print(f"\n=== {stem.name} ===\n{report}\n", flush=True)
        except Exception as e:
            event["pipeline_status"] = "error"
            event["pipeline_error"] = str(e)
            stem.with_suffix(".error.txt").write_text(str(e), encoding="utf-8")
            log.exception("pipeline failed stem=%s", stem.name)
        finally:
            if delete_audio and audio_path is not None:
                audio_path.unlink(missing_ok=True)
                event["audio_file"] = None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m app.pipeline <recording.m4a>")
    src = Path(sys.argv[1])
    event: dict[str, Any] = {}
    ms = src.stem.split("_", 1)[0]  # saved recordings are <recordedAt ms>_<deliveryId>.m4a
    if ms.isdigit():
        event["recorded_at"] = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).isoformat()
    asyncio.run(process_recording(src.with_suffix(""), src, None, event))
