"""Send an Index-v1-signed webhook, mimicking the ring app. Stdlib only.

Usage:
  INDEX_WEBHOOK_SECRET=... python scripts/send_test.py [URL] [--audio FILE] [--text TEXT]
                                                       [--test] [--delivery ID] [--bad-sig] [--skew SECONDS]
"""

import argparse
import hashlib
import hmac
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def build_multipart(fields: dict[str, str], audio: bytes | None, boundary: str) -> bytes:
    parts = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    if audio is not None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="audio"; filename="rec.m4a"\r\n'
            f"Content-Type: audio/mp4\r\n\r\n".encode()
            + audio
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("url", nargs="?", default="http://127.0.0.1:8000/webhooks/ring")
    p.add_argument("--audio", type=Path)
    p.add_argument("--text", default="hello from send_test")
    p.add_argument("--test", action="store_true", help="send as 'Send test event'")
    p.add_argument("--delivery", default=None, help="reuse a delivery ID to test dedup")
    p.add_argument("--bad-sig", action="store_true")
    p.add_argument("--skew", type=int, default=0, help="shift timestamp by N seconds")
    a = p.parse_args()

    secret = os.environ["INDEX_WEBHOOK_SECRET"].encode()
    boundary = str(uuid.uuid4())
    audio = a.audio.read_bytes() if a.audio else None
    fields = {"recordedAt": str(int(time.time() * 1000)), "client": "ring"}
    if a.text:
        fields["transcription"] = a.text
    if a.test:
        fields["test"] = "true"
    body = build_multipart(fields, audio, boundary)

    version, ts = "1", str(int(time.time()) + a.skew)
    delivery = a.delivery or str(uuid.uuid4())
    trigger = "test-event" if a.test else "single-click-hold"
    prefix = f"v{version}\n{ts}\n{delivery}\n{trigger}\n{'1' if a.test else '0'}\n".encode()
    sig = hmac.new(secret, prefix + body, hashlib.sha256).hexdigest()
    if a.bad_sig:
        sig = "0" * 64

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "X-Index-Webhook-Version": version,
        "X-Index-Trigger": trigger,
        "X-Index-Delivery": delivery,
        "X-Index-Timestamp": ts,
        "X-Index-Signature": sig,
    }
    if audio is not None:
        headers["X-Audio-Size"] = str(len(audio))
    if a.test:
        headers["X-Index-Test"] = "true"

    req = urllib.request.Request(a.url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            print(r.status, r.read().decode())
    except urllib.error.HTTPError as e:
        print(e.code, e.read().decode())
    print("delivery:", delivery, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
