from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def hash_mapping(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def hash_files(paths: list[Path]) -> str:
    sha = hashlib.sha256()
    for p in sorted(paths):
        sha.update(str(p).encode("utf-8"))
        if p.exists() and p.is_file():
            sha.update(p.read_bytes())
    return sha.hexdigest()
