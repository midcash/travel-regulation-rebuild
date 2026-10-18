"""Shared supplier facts; no planning or conversation policy."""
from datetime import timedelta, timezone
import hashlib
import json
from pathlib import Path

PACKAGE = Path(__file__).parent
CHINA = timezone(timedelta(hours=8))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
