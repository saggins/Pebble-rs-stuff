"""Load configuration into os.environ before any app module reads it.

Runs on first import of the `app` package, so it covers both
`uvicorn app.main:app` and `python -m app.pipeline`. Sources, highest priority first:

1. Variables already set in the process environment.
2. systemd credentials: every file in $CREDENTIALS_DIRECTORY becomes an env var named
   after the file (the NixOS module passes secrets this way via LoadCredential, so
   they never appear in the unit file or `systemctl show`).
3. <project root>/.env, for local development.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_creds = os.environ.get("CREDENTIALS_DIRECTORY")
if _creds:
    for _f in Path(_creds).iterdir():
        if _f.is_file():
            os.environ.setdefault(_f.name, _f.read_text(encoding="utf-8").strip())

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
