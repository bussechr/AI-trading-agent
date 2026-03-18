from __future__ import annotations

from pathlib import Path

from fxstack.utils.hashing import hash_files, hash_mapping


def dataset_fingerprint(*, data_paths: list[Path], feature_schema: dict, run_id: str) -> str:
    payload_hash = hash_mapping({"feature_schema": feature_schema, "run_id": run_id})
    file_hash = hash_files(data_paths)
    return f"{payload_hash[:16]}-{file_hash[:16]}"
