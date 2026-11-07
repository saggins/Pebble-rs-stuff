# pebble-ring

Webhook receiver for the **Index 01** smart ring (Pebble/Core Devices). The ring sends voice
recordings; this server verifies them, transcribes with whisper.cpp, classifies the note with a
local LLM (Ollama, OpenAI-compatible API), and acts on some categories (Home Assistant today).

```
ring app --HTTPS POST (multipart, HMAC-signed)--> tailscale serve --> uvicorn :8000
  app/main.py      verify signature, dedup, save m4a          -> 202 immediately
  app/pipeline.py  (background) ffmpeg -> whisper-cli -> transcript
  app/classify.py  LLM: transcript -> category + confidence (logprobs)
  HANDLERS[label]  e.g. "home" -> app/home_assistant.py (HA MCP tool loop)
  stdout           transcript, category, confidence, action outcome
```

## Layout

| Path | Role |
|---|---|
| `flake.nix` | Dev shell. **System tools only**: python313, whisper-cpp, ffmpeg-headless, curl, jq. shellHook creates `.venv`, pip-installs `requirements.txt` when its hash changes, downloads the ggml whisper model to `models/`. |
| `requirements.txt` | **All Python packages** (fastapi, uvicorn, python-multipart, httpx, mcp, python-dotenv). Never add Python packages to the flake. |
| `app/main.py` | FastAPI app. `POST /webhooks/ring` (Index protocol v1), `GET /webhooks/recent`, `GET /health`. |
| `app/pipeline.py` | Background task per recording: transcribe, classify, dispatch to `HANDLERS`, print. Also `python -m app.pipeline <file.m4a>` to reprocess by hand. |
| `app/classify.py` | `CATEGORIES` list + classifier. Confidence = real token probability from the LLM server's logprobs. |
| `app/gitea_notes.py` | `note` handler: commits transcript as Markdown to Gitea via contents API (no clone). |
| `app/home_assistant.py` | HA MCP client, tool allowlist, local call validation, LLM tool-call loop. |
| `scripts/send_test.py` | Stdlib client that signs requests exactly like the ring. Use for local/tailnet testing. |
| `recordings/` | Saved `<recordedAt>_<deliveryId>.m4a/.txt/.whisper.txt/.error.txt`. Gitignored. |
| `models/` | whisper ggml models. Gitignored. |
| `.env` | Secrets (`HA_TOKEN`, `GITEA_TOKEN`, optionally `INDEX_WEBHOOK_SECRET`). Auto-loaded by `app/__init__.py`; shell env wins. Gitignored. Never print or commit. |

## Running

```sh
nix develop                        # direnv also works; `direnv reload` after flake.nix changes
export INDEX_WEBHOOK_SECRET=...    # same value as "Sign requests" in the ring app
uvicorn app.main:app --host 127.0.0.1 --port 8000
tailscale serve --bg 8000          # https://dorado.comet-court.ts.net/webhooks/ring
python scripts/send_test.py --text "turn off the bedroom lights"   # signed test request
```

External services: Ollama (any OpenAI-compatible server works) at `http://127.0.0.1:11434/v1` with
`gemma4:e2b` pulled;
Home Assistant MCP at `http://100.78.133.58:8123/api/mcp` (tailnet).

## Configuration (env)

| Var | Default | Notes |
|---|---|---|
| `INDEX_WEBHOOK_SECRET` | required | Server refuses to start without it. |
| `RECORDINGS_DIR` | `recordings` | |
| `WHISPER_MODEL` / `WHISPER_MODEL_NAME` | `models/ggml-base.en.bin` / `base.en` | shellHook sets these. |
| `WHISPER_LANG`, `WHISPER_THREADS` | `en`, `4` | |
| `FFMPEG`, `WHISPER_CLI` | from PATH | Resolved at import; server exits if missing. |
| `LLM_URL`, `LLM_MODEL` | `http://127.0.0.1:11434/v1`, `gemma4:e2b` | Any OpenAI-compatible server. LM Studio: `http://127.0.0.1:1234/v1`, `google/gemma-4-e2b`. |
| `LLM_REASONING_EFFORT` | `none` | Gemma 4 otherwise burns tokens on reasoning; empty string re-enables. |
| `HA_TOKEN` | required for home actions | HA long-lived access token. |
| `HA_MCP_URL` | tailnet HA URL | |
| `HA_DRY_RUN` | off | `1` = only read-only tools execute; actions are printed, not sent. |
| `HA_MIN_CONFIDENCE` | `0.9` | Below this, home commands are skipped. |
| `HA_MAX_STEPS` | `5` | LLM <-> tool round trips. |
| `GITEA_TOKEN` | required for notes | Gitea token, `write:repository` scope. |
| `GITEA_URL`, `GITEA_REPO`, `GITEA_BRANCH` | workhorse tailnet URL, `sangmin/school-notes`, `main` | |
| `GITEA_NOTES_DIR` | `💭Fleeting Notes` | Notes saved as `YYYYMMDDHHMM.md` (local time; next free minute on clash). |
| `NOTES_TZ`, `NOTES_DRY_RUN` | system local, off | `NOTES_DRY_RUN=1` prints the note instead of committing. |

## Conventions and invariants

- **Webhook protocol**: spec at https://developer.repebble.com/index-01/webhooks/ (v1). Signed bytes
  are `"v1\n{timestamp}\n{deliveryId}\n{trigger}\n{0|1}\n" + raw multipart body`, lowercase hex
  HMAC-SHA256 in `X-Index-Signature`. Read the raw body **before** parsing multipart.
- **Check order in `receive_ring`**: cheap header checks -> capped body read -> HMAC -> dedup ->
  multipart parse. Don't parse or write anything for unauthenticated requests.
- **Never raise on bad auth input**: validators return `False`/401, never 500.
  Compare secrets with `hmac.compare_digest` on bytes.
- **Dedup**: `X-Index-Delivery` cached in memory; repeats return 200 without reprocessing. Mark
  seen only after success. The ring retries 408/429/5xx; 4xx is final.
- **Ack fast**: all slow work (whisper, LLM, HA) runs as a `BackgroundTasks` job after the 202.
- **Adding a category**: append to `CATEGORIES` in `app/classify.py`. `label` must be one lowercase
  word whose first token differs from all other labels (confidence is read from the first token).
  Descriptions drive accuracy more than anything else. Add a handler in `pipeline.HANDLERS` if the
  category should act.
- **Home Assistant safety**:
  - HA's MCP endpoint exposes admin tools (`call_action`, config reloads, templates, logs). Only
    `ALLOWED_TOOL_PREFIXES` / `ALLOWED_TOOLS` reach the model; anything else is refused.
  - The LLM sees short tool names (`HassTurnOn`); `name_map` maps back to `intent__HassTurnOn`.
  - Calls are sanitized against the tool schema and validated against the prefetched device list
    (`_check_target`) before reaching HA; errors are fed back so the model retries.
  - Prefer fixing small-model mistakes with code-side validation over more prompt rules.
  - Printed `outcome:` comes from tool results, not the model's reply.
- **Logging**: never log `Authorization`, signatures, tokens or `.env` contents.

## Testing

No test suite yet. Manual checks:
- `python scripts/send_test.py [--test|--bad-sig|--skew -600|--delivery ID] [--audio f.m4a]`
- Synthetic speech: `say -o x.aiff "..." && ffmpeg -i x.aiff -ac 1 -ar 16000 -c:a aac x.m4a`
- HA without side effects: `HA_DRY_RUN=1`.
