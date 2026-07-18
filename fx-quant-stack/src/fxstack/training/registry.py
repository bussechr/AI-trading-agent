from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from fxstack.utils.paths import ensure_dir


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=str(destination.parent),
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        try:
            directory_descriptor = os.open(destination.parent, os.O_RDONLY)
        except OSError:
            directory_descriptor = None
        if directory_descriptor is not None:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
            finally:
                os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


class ArtifactRegistry:
    def __init__(self, root: Path) -> None:
        self.root = ensure_dir(Path(root))

    def register(self, name: str, metadata: dict[str, Any]) -> Path:
        out = self.root / f"{name}.json"
        write_json_atomic(out, metadata)
        return out
