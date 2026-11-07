# Handoff

State as of 2026-11-06. See `CLAUDE.md` for how the code is organized.

## Working

- Real ring -> tailscale -> server: signature verification, test events, real recordings.
- whisper.cpp transcription (`base.en`); end-to-end (upload -> LLM result) was ~9 s for a short
  note in testing.
- Classification into 4 categories with logprob confidence; correct on all synthetic samples.
- Home Assistant via MCP: dry-run planning verified against the real HA; first live call failed
  with `invalid slot info` (since fixed by arg sanitizing + validation). **Live control after
  the fixes has not been confirmed yet.**

## Should look at next

### Correctness / reliability
1. **Live HA verification.** Run the bedroom commands without `HA_DRY_RUN` and confirm
   `outcome: success`. Watch for HA errors that `_check_target` doesn't anticipate.
2. **Whisper clips the first word** ("on the bedroom lights"). Check whether the ring audio
   starts late or ffmpeg/whisper trims it. Options: pad leading silence before whisper, try a
   bigger model (`small.en`), or whisper `--prompt` with HA device names to bias recognition.
3. **Background failures are silent to the ring.** The ring got 202, so it never retries a
   pipeline error. Consider a retry/queue, or at least a `scripts/reprocess_errors.py` that
   reruns stems with `.error.txt`.
4. **In-memory state is lost on restart**: `seen_deliveries` and `recent_events`. Acceptable for
   now (5-min replay window + idempotent filenames); move to SQLite if this matters.
5. **`request._body` is a private Starlette attribute** used to re-parse the multipart body after
   HMAC. A Starlette upgrade may break it. Fallback: parse multipart from the bytes directly
   (`python-multipart` is already installed).
6. **No automated tests.** Highest value: signature verification (good/bad/expired/missing
   prefix), dedup, `_parse_live_context`, `_sanitize_args`, `_check_target`, classification
   label parsing. Everything except LM Studio/HA can run offline.

### Home Assistant behaviour
7. **Calls with no target.** "turn on the garage door" produced `HassTurnOn(device_class=garage,
   domain=switch)` with no name/area. Decide whether to require `name` or `area` for action
   tools (except `HassCancelAllTimers`).
8. **Duplicate device names in HA**: three lights named `Living Room`/`Living room`, two of them
   unavailable. Clean up in HA (rename / un-expose); ambiguous names make HA refuse or pick wrong.
9. **Sensitive domains.** `HassTurnOn` also locks locks and opens covers/garage doors. Consider a
   per-domain policy (e.g. never open `cover`/`lock` by voice, or require higher confidence).
10. **`HA_MIN_CONFIDENCE` is barely exercised**: the classifier is >99.9% sure on clear notes.
    Collect real ring transcripts to see where confidence actually drops.

### Product
11. **Handlers for the other categories**: CalDav TODO (VTODO), CalDav Events (VEVENT, needs
    date/time extraction: use `llm__GetDateTime`-style "now" context), Notes-to-self (append to a
    file / notes app). The `HANDLERS` dict in `app/pipeline.py` is the extension point.
12. **More categories** are planned; see "Adding a category" in `CLAUDE.md`. Re-run the sample
    set after each addition. Labels with similar first tokens break confidence.
13. **Feedback to the user.** Results only go to stdout. Possible: Pebble timeline pin /
    notification back to the watch, or HA `assist_satellite__HassBroadcast` (currently blocked).

### Ops / security
14. **Rotate `INDEX_WEBHOOK_SECRET`**: an earlier value was pasted into a chat log. Store it in
    `.env` so it survives shell restarts.
15. **Run as a service** (launchd / nix-darwin module) instead of a foreground uvicorn, with
    `tailscale serve` config persisted.
16. **Funnel vs serve.** Currently works with `tailscale serve` (tailnet only), which implies the
    phone posts directly. Keep it off Funnel unless delivery from outside the tailnet is needed.
17. **direnv didn't pick up flake changes** once (ffmpeg missing on PATH). The startup binary
    check now catches it; root cause in the fish + nix-direnv setup not investigated.
