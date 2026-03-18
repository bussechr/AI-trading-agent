from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fxstack.utils.paths import ensure_dir


class ArtifactRegistry:
    def __init__(self, root: Path) -> None:
        self.root = ensure_dir(Path(root))

    def register(self, name: str, metadata: dict[str, Any]) -> Path:
        out = self.root / f"{name}.json"
        out.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")
        return out
