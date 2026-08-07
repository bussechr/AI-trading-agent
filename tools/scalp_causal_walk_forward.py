from __future__ import annotations

# AGENT: ROLE: Sealed M1 CSV snapshot, replay, and audit helper for the standalone scalp research engine.
# AGENT: ENTRYPOINT: Imported by `tools/run_causal_walk_forward.py`; private `replay` child-process CLI.
# AGENT: PRIMARY INPUTS: Explicit UTC train/test windows and source `{PAIR}_M1.csv` files.
# AGENT: PRIMARY OUTPUTS: Bundle-local content manifests, raw observations,
# point-in-time audits, and advisory economics.
# AGENT: DEPENDS ON: `fxstack.scalp.backtest` only inside the scrubbed replay child.
# AGENT: CALLED BY: `tools/run_causal_walk_forward.py --research-engine scalp`.
# AGENT: STATE / SIDE EFFECTS: Writes only below the caller-supplied disposable
# research root; never reads runtime state.
# AGENT: HANDSHAKES: `research.scalp_causal_bundle` and `research.artifacts`.
# AGENT: SEE: `docs/agents/causal-research-and-runtime-validation.md`.

import argparse
import contextlib
import csv
import datetime as dt
import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


SNAPSHOT_VERSION = "scalp_m1_point_in_time_snapshot_v1"
BUNDLE_VERSION = "scalp_causal_input_bundle_v1"
OBSERVATIONS_VERSION = "scalp_causal_replay_observations_v1"
AUDIT_VERSION = "scalp_causal_point_in_time_audit_v1"
RUN_VERSION = "scalp_causal_walk_forward_run_v1"
ECONOMICS_VERSION = "scalp_causal_advisory_economics_v1"
FILL_DELAY_BARS = 1
TARGET_WIN_RATE = 0.90
M1_KNOWLEDGE_DELAY = dt.timedelta(minutes=1)
_PAIR_RE = re.compile(r"^[A-Z0-9]{6,12}$")
_WINDOW_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CSV_COLUMNS = (
    "timestamp",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)
_FORBIDDEN_ENV_KEYS = {
    "BRIDGE_URL",
    "DATABASE_URL",
    "FXSCALP_API_KEY_FILE",
    "FXSCALP_BRIDGE_URL",
    "FXSTACK_BRIDGE_API_KEY",
    "FXSTACK_DATABASE_URL",
    "FXSTACK_MODEL_ACTIVATION_MANIFEST",
    "FXSTACK_REGISTRY_ROOT",
    "MT4_BRIDGE_URL",
    "TRADER_BRIDGE_API_KEY",
    "TRADER_BRIDGE_URL",
}


def _utc(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _iso(value: Any) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _canonical_bytes(payload: dict[str, Any]) -> bytes:
    body = dict(payload)
    body.pop("manifest_content_sha256", None)
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _manifest_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _engine_source_hashes() -> dict[str, str]:
    sources = {
        "fxstack.scalp.backtest": FXSTACK_SRC / "fxstack" / "scalp" / "backtest.py",
        "fxstack.scalp.bars": FXSTACK_SRC / "fxstack" / "scalp" / "bars.py",
        "fxstack.scalp.config": FXSTACK_SRC / "fxstack" / "scalp" / "config.py",
        "fxstack.scalp.costs": FXSTACK_SRC / "fxstack" / "scalp" / "costs.py",
        "fxstack.scalp.families": FXSTACK_SRC / "fxstack" / "scalp" / "families.py",
        "fxstack.scalp.gates": FXSTACK_SRC / "fxstack" / "scalp" / "gates.py",
        "fxstack.scalp.panel": FXSTACK_SRC / "fxstack" / "scalp" / "panel.py",
        "fxstack.scalp.shadow": FXSTACK_SRC / "fxstack" / "scalp" / "shadow.py",
        "fxstack.scalp.signals": FXSTACK_SRC / "fxstack" / "scalp" / "signals.py",
        "tools.scalp_causal_walk_forward": Path(__file__).resolve(),
    }
    missing = [name for name, path in sources.items() if not path.is_file()]
    if missing:
        raise RuntimeError("scalp research engine source is incomplete: " + ",".join(missing))
    return {name: _file_sha256(path) for name, path in sorted(sources.items())}


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & reparse_flag)


def _safe_output_target(bundle_root: Path, path: Path) -> Path:
    root = Path(bundle_root).resolve()
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"output path escapes scalp research bundle: {candidate}") from exc
    cursor = root
    for part in relative.parent.parts:
        cursor = cursor / part
        if (cursor.exists() or cursor.is_symlink()) and _is_link_or_reparse(cursor):
            raise RuntimeError(f"output parent is a link or reparse point: {cursor}")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    cursor = root
    for part in relative.parent.parts:
        cursor = cursor / part
        if _is_link_or_reparse(cursor):
            raise RuntimeError(f"output parent became a link or reparse point: {cursor}")
    if candidate.exists() or candidate.is_symlink():
        if _is_link_or_reparse(candidate):
            raise RuntimeError(f"output target is a link or reparse point: {candidate}")
        metadata = candidate.stat()
        if not candidate.is_file() or int(getattr(metadata, "st_nlink", 1) or 1) > 1:
            raise RuntimeError(f"output target is not a private regular file: {candidate}")
    return candidate


def _atomic_write_text(bundle_root: Path, path: Path, value: str) -> Path:
    target = _safe_output_target(bundle_root, path)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        _safe_output_target(bundle_root, target)
        os.replace(temp_path, target)
        temp_path = None
        return target
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _write_json(
    bundle_root: Path,
    path: Path,
    payload: dict[str, Any],
    *,
    self_hash: bool = False,
) -> None:
    body = dict(payload)
    if self_hash:
        body["manifest_content_sha256"] = _manifest_sha256(body)
    _atomic_write_text(
        bundle_root,
        path,
        json.dumps(body, indent=2, sort_keys=True, allow_nan=False),
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = str(payload.get("manifest_content_sha256") or "")
    if len(expected) != 64 or expected != _manifest_sha256(payload):
        raise RuntimeError(f"manifest content hash mismatch: {path.name}")
    return payload


def _pairs(values: list[str]) -> list[str]:
    pairs = list(dict.fromkeys(str(value).strip().upper() for value in values if str(value).strip()))
    if not pairs:
        raise ValueError("at least one scalp pair is required")
    invalid = [pair for pair in pairs if not _PAIR_RE.fullmatch(pair)]
    if invalid:
        raise ValueError("invalid scalp pair identifiers: " + ",".join(invalid))
    return pairs


def _bundle_member(bundle_root: Path, relative_path: str) -> Path:
    raw = str(relative_path or "").strip().replace("\\", "/")
    if not raw or "://" in raw or Path(raw).is_absolute():
        raise RuntimeError(f"bundle reference must be relative: {raw!r}")
    root = Path(bundle_root).resolve()
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise RuntimeError(f"bundle reference escapes root: {raw}") from exc
    return candidate


def _relative_to_bundle(bundle_root: Path, path: Path) -> str:
    root = Path(bundle_root).resolve()
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"path escapes scalp research bundle: {resolved}") from exc


def _assert_regular_bundle_file(bundle_root: Path, relative_path: str) -> Path:
    path = _bundle_member(bundle_root, relative_path)
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"bundle member is not a regular file: {relative_path}")
    return path


def _copy_truncated_m1_csv(
    *,
    source_path: Path,
    destination_path: Path,
    cutoff: dt.datetime,
) -> dict[str, Any]:
    if source_path.is_symlink() or not source_path.is_file():
        raise FileNotFoundError(f"required M1 source is missing or not a regular file: {source_path.name}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    partial = destination_path.with_suffix(destination_path.suffix + ".partial")
    rows = 0
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    try:
        with source_path.open("r", encoding="utf-8", newline="") as source, partial.open(
            "w", encoding="utf-8", newline=""
        ) as destination:
            reader = csv.reader(source)
            writer = csv.writer(destination, lineterminator="\n")
            header = next(reader, None)
            if not header or tuple(header[: len(_CSV_COLUMNS)]) != _CSV_COLUMNS:
                raise ValueError(f"{source_path.name}: unexpected M1 header {header!r}")
            writer.writerow(header)
            for line_number, row in enumerate(reader, start=2):
                if not row:
                    continue
                try:
                    timestamp = _utc(row[0])
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{source_path.name}:{line_number}: invalid timestamp {row[0]!r}"
                    ) from exc
                if last_ts is not None and timestamp <= last_ts:
                    raise ValueError(
                        f"{source_path.name}:{line_number}: M1 rows are not strictly ordered"
                    )
                if timestamp + M1_KNOWLEDGE_DELAY > cutoff:
                    break
                try:
                    quotes = [float(value) for value in row[1:9]]
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"{source_path.name}:{line_number}: invalid bid/ask OHLC"
                    ) from exc
                if len(quotes) != 8 or not all(
                    math.isfinite(value) and value > 0.0 for value in quotes
                ):
                    raise ValueError(
                        f"{source_path.name}:{line_number}: invalid bid/ask OHLC"
                    )
                if any(quotes[index + 4] < quotes[index] for index in range(4)):
                    raise ValueError(
                        f"{source_path.name}:{line_number}: ask quote is below bid"
                    )
                writer.writerow(row)
                rows += 1
                first_ts = timestamp if first_ts is None else first_ts
                last_ts = timestamp
        if rows < 1 or first_ts is None or last_ts is None:
            raise RuntimeError(f"{source_path.name}: no causally known rows at {_iso(cutoff)}")
        partial.replace(destination_path)
    finally:
        if partial.exists():
            partial.unlink()
    return {
        "rows": rows,
        "min_bar_open_ts": _iso(first_ts),
        "max_bar_open_ts": _iso(last_ts),
        "max_knowledge_ts": _iso(last_ts + M1_KNOWLEDGE_DELAY),
        "content_sha256": _file_sha256(destination_path),
        "size_bytes": destination_path.stat().st_size,
    }


def _inspect_m1_csv(path: Path) -> dict[str, Any]:
    rows = 0
    first_ts: dt.datetime | None = None
    last_ts: dt.datetime | None = None
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or tuple(header[: len(_CSV_COLUMNS)]) != _CSV_COLUMNS:
            raise RuntimeError(f"sealed M1 snapshot has an invalid header: {path.name}")
        for line_number, row in enumerate(reader, start=2):
            if not row:
                continue
            try:
                timestamp = _utc(row[0])
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"sealed M1 snapshot has an invalid timestamp: {path.name}:{line_number}"
                ) from exc
            if last_ts is not None and timestamp <= last_ts:
                raise RuntimeError(f"sealed M1 snapshot is not strictly ordered: {path.name}")
            first_ts = timestamp if first_ts is None else first_ts
            last_ts = timestamp
            rows += 1
    if rows < 1 or first_ts is None or last_ts is None:
        raise RuntimeError(f"sealed M1 snapshot is empty: {path.name}")
    return {
        "rows": rows,
        "min_bar_open_ts": _iso(first_ts),
        "max_bar_open_ts": _iso(last_ts),
        "max_knowledge_ts": _iso(last_ts + M1_KNOWLEDGE_DELAY),
    }


def build_m1_snapshot(
    *,
    bundle_root: Path,
    source_csv_root: Path,
    snapshot_relative_root: str,
    role: str,
    requested_pairs: list[str],
    cutoff: Any,
) -> dict[str, Any]:
    """Copy a complete, physically truncated, content-inventoried M1 snapshot."""

    pairs = _pairs(requested_pairs)
    root = Path(bundle_root).resolve()
    snapshot_root = _bundle_member(root, snapshot_relative_root)
    if snapshot_root.exists() and any(snapshot_root.iterdir()):
        raise FileExistsError(f"scalp snapshot output must be empty: {snapshot_relative_root}")
    snapshot_root.mkdir(parents=True, exist_ok=True)
    source_root = Path(source_csv_root).resolve()
    cutoff_utc = _utc(cutoff)
    files: list[dict[str, Any]] = []
    for pair in pairs:
        source = source_root / f"{pair}_M1.csv"
        destination = snapshot_root / f"{pair}_M1.csv"
        evidence = _copy_truncated_m1_csv(
            source_path=source,
            destination_path=destination,
            cutoff=cutoff_utc,
        )
        files.append(
            {
                "pair": pair,
                "path": _relative_to_bundle(root, destination),
                **evidence,
            }
        )
    if {item["pair"] for item in files} != set(pairs):
        raise RuntimeError(f"incomplete {role} M1 snapshot")
    manifest_path = snapshot_root / "snapshot_manifest.json"
    manifest = {
        "version": SNAPSHOT_VERSION,
        "research_only": True,
        "advisory_only": True,
        "future_data_access": "forbidden",
        "source_paths_recorded": False,
        "snapshot_role": str(role),
        "source_format": "bid_ask_m1_csv_v1",
        "timeframe": "M1",
        "cutoff_inclusive": _iso(cutoff_utc),
        "knowledge_time_rule": "bar_open_ts + 1 minute <= cutoff",
        "requested_pairs": pairs,
        "files": files,
        "manifest_path": _relative_to_bundle(root, manifest_path),
    }
    _write_json(root, manifest_path, manifest, self_hash=True)
    return _load_manifest(manifest_path)


@contextlib.contextmanager
def _without_scalp_environment() -> Iterator[None]:
    removed = {
        key: value
        for key, value in list(os.environ.items())
        if str(key).upper().startswith("FXSCALP_")
    }
    for key in removed:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        os.environ.update(removed)


def _fixed_config(requested_pairs: list[str]) -> tuple[Any, dict[str, Any], str]:
    from fxstack.scalp.config import ScalpConfig

    with _without_scalp_environment():
        config = ScalpConfig()
    config.symbols = list(_pairs(requested_pairs))
    config.mode = "shadow"
    config.bridge_url = "offline-disabled"
    config.api_key_file = ""
    config.data_root = "."
    # The first causal slice is intentionally fixed. These fields make the
    # execution-delay proof exact and prevent ambient defaults from selecting
    # a panel-dependent family or passive multi-bar entry.
    config.bar_minutes = 1
    config.entry_mode = "market"
    config.signal_family = "dislocation"
    config.signal_mode = "revert"
    problems = [problem for problem in config.validate() if "shadow" not in problem]
    if problems:
        raise RuntimeError("fixed scalp research config is invalid: " + "; ".join(problems))
    payload = asdict(config)
    config_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return config, payload, config_sha256


def _write_config_manifest(
    bundle_root: Path,
    requested_pairs: list[str],
    *,
    extra_spread_bps: float,
    sl_extra_slip_bps: float,
) -> dict[str, Any]:
    _config, config_payload, config_sha256 = _fixed_config(requested_pairs)
    path = Path(bundle_root).resolve() / "inputs" / "scalp_config.json"
    manifest = {
        "version": "scalp_fixed_research_config_v1",
        "research_only": True,
        "advisory_only": True,
        "activation_authority": False,
        "runtime_store_updated": False,
        "requested_pairs": _pairs(requested_pairs),
        "parameter_selection": "fixed_before_window_no_fit",
        "engine_source_sha256": _engine_source_hashes(),
        "spread_model": {
            "source_bid_ask_ohlc": True,
            "source_spread_intrinsic": True,
            "extra_spread_bps": float(extra_spread_bps),
            "sl_extra_slip_bps": float(sl_extra_slip_bps),
            "optimistic_mode": False,
        },
        "config": config_payload,
        "config_sha256": config_sha256,
        "manifest_path": _relative_to_bundle(bundle_root, path),
    }
    _write_json(bundle_root, path, manifest, self_hash=True)
    return _load_manifest(path)


def _bundle_inventory(bundle_root: Path, manifests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    inventory: list[dict[str, Any]] = []
    for manifest in manifests:
        refs = [str(manifest["manifest_path"])]
        refs.extend(str(item["path"]) for item in list(manifest.get("files") or []))
        for relative in refs:
            if relative in seen:
                continue
            seen.add(relative)
            path = _assert_regular_bundle_file(bundle_root, relative)
            inventory.append(
                {
                    "path": relative,
                    "content_sha256": _file_sha256(path),
                    "size_bytes": path.stat().st_size,
                }
            )
    return sorted(inventory, key=lambda item: str(item["path"]))


def _write_input_bundle_manifest(
    *,
    bundle_root: Path,
    requested_pairs: list[str],
    train_snapshot: dict[str, Any],
    replay_snapshot: dict[str, Any],
    config_manifest: dict[str, Any],
) -> dict[str, Any]:
    path = Path(bundle_root).resolve() / "input_bundle_manifest.json"
    manifest = {
        "version": BUNDLE_VERSION,
        "research_only": True,
        "advisory_only": True,
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "activation_authority": False,
        "registry_access": "forbidden",
        "live_bridge_access": "forbidden",
        "source_paths_recorded": False,
        "bundle_root": ".",
        "requested_pairs": _pairs(requested_pairs),
        "train_snapshot_manifest": str(train_snapshot["manifest_path"]),
        "replay_snapshot_manifest": str(replay_snapshot["manifest_path"]),
        "config_manifest": str(config_manifest["manifest_path"]),
        "files": _bundle_inventory(
            bundle_root,
            [train_snapshot, replay_snapshot, config_manifest],
        ),
        "manifest_path": _relative_to_bundle(bundle_root, path),
    }
    _write_json(bundle_root, path, manifest, self_hash=True)
    return _load_manifest(path)


def verify_input_bundle(
    bundle_root: Path,
    *,
    requested_pairs: list[str] | None = None,
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    bundle = _load_manifest(_assert_regular_bundle_file(root, "input_bundle_manifest.json"))
    if str(bundle.get("version")) != BUNDLE_VERSION:
        raise RuntimeError("unsupported scalp causal bundle version")
    if not bool(bundle.get("research_only")) or not bool(bundle.get("advisory_only")):
        raise RuntimeError("scalp causal bundle is not research-only and advisory-only")
    if str(bundle.get("future_data_access")) != "forbidden":
        raise RuntimeError("scalp causal bundle permits future data access")
    if bool(bundle.get("activation_authority")) or bool(bundle.get("runtime_store_updated")):
        raise RuntimeError("scalp causal bundle carries production authority")
    pairs = _pairs(list(bundle.get("requested_pairs") or []))
    if requested_pairs is not None and pairs != _pairs(requested_pairs):
        raise RuntimeError("scalp causal bundle requested-pair scope mismatch")
    for item in list(bundle.get("files") or []):
        relative = str(item.get("path") or "")
        path = _assert_regular_bundle_file(root, relative)
        if _file_sha256(path) != str(item.get("content_sha256") or ""):
            raise RuntimeError(f"scalp causal bundle file hash mismatch: {relative}")
        if path.stat().st_size != int(item.get("size_bytes") or -1):
            raise RuntimeError(f"scalp causal bundle file size mismatch: {relative}")
    snapshots: dict[str, dict[str, Any]] = {}
    for role, key in (("train", "train_snapshot_manifest"), ("replay", "replay_snapshot_manifest")):
        manifest = _load_manifest(_assert_regular_bundle_file(root, str(bundle.get(key) or "")))
        if str(manifest.get("version")) != SNAPSHOT_VERSION or str(manifest.get("snapshot_role")) != role:
            raise RuntimeError(f"invalid {role} M1 snapshot manifest")
        entries = list(manifest.get("files") or [])
        entry_pairs = [str(item.get("pair") or "") for item in entries]
        if entry_pairs != pairs or set(entry_pairs) != set(pairs):
            raise RuntimeError(f"incomplete requested-pair coverage in {role} M1 snapshot")
        for item in entries:
            path = _assert_regular_bundle_file(root, str(item.get("path") or ""))
            if _file_sha256(path) != str(item.get("content_sha256") or ""):
                raise RuntimeError(f"{role} M1 snapshot content hash mismatch: {item.get('pair')}")
            if int(item.get("rows") or 0) < 1:
                raise RuntimeError(f"{role} M1 snapshot is empty: {item.get('pair')}")
        snapshots[role] = manifest
    config_manifest = _load_manifest(
        _assert_regular_bundle_file(root, str(bundle.get("config_manifest") or ""))
    )
    if list(config_manifest.get("requested_pairs") or []) != pairs:
        raise RuntimeError("fixed scalp config requested-pair scope mismatch")
    return {
        "bundle": bundle,
        "train_snapshot": snapshots["train"],
        "replay_snapshot": snapshots["replay"],
        "config_manifest": config_manifest,
    }


def _assert_offline_environment(environment: dict[str, str] | None = None) -> None:
    source = dict(os.environ if environment is None else environment)
    forbidden = [key for key in _FORBIDDEN_ENV_KEYS if str(source.get(key) or "").strip()]
    for key in ("FXSTACK_EXECUTION_PROVIDER", "FXSTACK_MARKET_DATA_PROVIDER"):
        value = str(source.get(key) or "").strip().lower()
        if value and value not in {"disabled", "offline", "research"}:
            forbidden.append(key)
    if str(source.get("FXSCALP_MODE") or "").strip().lower() == "live":
        forbidden.append("FXSCALP_MODE")
    if forbidden:
        raise RuntimeError(
            "offline scalp research refuses live endpoints, credentials, registry, or activation settings: "
            + ",".join(sorted(set(forbidden)))
        )


def _finite_json(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return value


def _economic_metrics_finite_and_complete(results: list[dict[str, Any]]) -> bool:
    """Fail closed on undefined/non-finite metrics and untraded requested pairs."""

    scalar_fields = (
        "win_rate",
        "total_r",
        "mean_r",
        "mean_pnl_bps",
        "profit_factor",
        "max_drawdown_r",
        "avg_bars_held",
    )
    if not results:
        return False
    for result in results:
        try:
            if int(result.get("trades") or 0) < 1:
                return False
            if any(
                isinstance(result.get(field), bool)
                or not isinstance(result.get(field), (int, float))
                or not math.isfinite(float(result[field]))
                for field in scalar_fields
            ):
                return False
            interval = list(result.get("mean_r_ci95") or [])
            if len(interval) != 2 or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in interval
            ):
                return False
        except (TypeError, ValueError, OverflowError):
            return False
    return True


def _write_jsonl(
    bundle_root: Path,
    path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    value = "".join(
        json.dumps(
            _finite_json(row),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    )
    path = _atomic_write_text(bundle_root, path, value)
    return {
        "path": path,
        "rows": len(rows),
        "content_sha256": _file_sha256(path),
        "size_bytes": path.stat().st_size,
    }


def replay_sealed_bundle(
    *,
    bundle_root: Path,
    requested_pairs: list[str],
    test_start: Any,
    test_end: Any,
    extra_spread_bps: float,
    sl_extra_slip_bps: float,
    observations_relative_path: str = "raw_replay_observations.json",
) -> dict[str, Any]:
    """Replay only bundle-local M1 files and emit still-gated observations."""

    _assert_offline_environment()
    start = _utc(test_start)
    end = _utc(test_end)
    if not start < end:
        raise ValueError("scalp replay requires test_start < test_end")
    if not math.isfinite(float(extra_spread_bps)) or float(extra_spread_bps) < 0.0:
        raise ValueError("scalp extra spread must be finite and non-negative")
    if not math.isfinite(float(sl_extra_slip_bps)) or float(sl_extra_slip_bps) < 0.0:
        raise ValueError("scalp stop slippage must be finite and non-negative")
    root = Path(bundle_root).resolve()
    verified = verify_input_bundle(root, requested_pairs=requested_pairs)
    pairs = _pairs(requested_pairs)
    config, config_payload, config_sha256 = _fixed_config(pairs)
    expected_config_sha256 = str(verified["config_manifest"].get("config_sha256") or "")
    if config_sha256 != expected_config_sha256 or config_payload != verified["config_manifest"].get("config"):
        raise RuntimeError("fixed scalp config drifted after bundle construction")
    if dict(verified["config_manifest"].get("engine_source_sha256") or {}) != _engine_source_hashes():
        raise RuntimeError("scalp research engine source drifted after bundle construction")
    if int(config.bar_minutes) != 1 or str(config.entry_mode) != "market":
        raise RuntimeError("scalp causal delay proof requires fixed M1 market-entry config")
    expected_spread_model = dict(verified["config_manifest"].get("spread_model") or {})
    if expected_spread_model != {
        "source_bid_ask_ohlc": True,
        "source_spread_intrinsic": True,
        "extra_spread_bps": float(extra_spread_bps),
        "sl_extra_slip_bps": float(sl_extra_slip_bps),
        "optimistic_mode": False,
    }:
        raise RuntimeError("scalp replay spread/slippage model drifted after bundle construction")

    from fxstack.scalp.backtest import BacktestRunner, load_bt_bars, summarize

    class AuditedBacktestRunner(BacktestRunner):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.entry_timeline: list[dict[str, Any]] = []

        def _open_at(self, intent: Any, entry: float, *, minute: int) -> None:
            decision_minute = int(intent.minute_epoch)
            fill_minute = int(minute)
            delta = fill_minute - decision_minute
            if delta != 60:
                raise RuntimeError("scalp replay drifted from its fixed one-bar entry fill")
            self.entry_timeline.append(
                {
                    "pair": str(intent.symbol).upper(),
                    "side": str(intent.side).upper(),
                    "decision_bar_open_ts": _iso(
                        dt.datetime.fromtimestamp(decision_minute, tz=dt.timezone.utc)
                    ),
                    "fill_bar_open_ts": _iso(
                        dt.datetime.fromtimestamp(fill_minute, tz=dt.timezone.utc)
                    ),
                    "observed_fill_delay_bars": delta // 60,
                }
            )
            super()._open_at(intent, entry, minute=minute)

    replay_entries = {
        str(item["pair"]): dict(item)
        for item in list(verified["replay_snapshot"].get("files") or [])
    }
    results: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    entry_timeline: list[dict[str, Any]] = []
    replay_inputs: list[str] = []
    for pair in pairs:
        entry = replay_entries.get(pair)
        if entry is None:
            raise RuntimeError(f"replay snapshot missing requested pair: {pair}")
        relative = str(entry.get("path") or "")
        csv_path = _assert_regular_bundle_file(root, relative)
        replay_inputs.append(relative)
        runner = AuditedBacktestRunner(
            config=config,
            sl_extra_slip_bps=float(sl_extra_slip_bps),
            optimistic=False,
        )
        for bar in load_bt_bars(
            csv_path,
            symbol=pair,
            start_epoch=start.timestamp(),
            end_epoch=end.timestamp(),
            extra_spread_bps=float(extra_spread_bps),
        ):
            runner.process(bar)
        runner.finish()
        summary = summarize(pair, runner.stats)
        summary["extra_spread_bps"] = float(extra_spread_bps)
        summary["sl_extra_slip_bps"] = float(sl_extra_slip_bps)
        summary["venue"] = (
            "interbank_bid_ask_raw"
            if float(extra_spread_bps) == 0.0
            else f"interbank_bid_ask_plus_{float(extra_spread_bps):g}bps"
        )
        results.append(_finite_json(summary))
        entry_timeline.extend(runner.entry_timeline)
        trades.extend(
            {
                "pair": pair,
                **_finite_json(fill.to_dict()),
            }
            for fill in runner.stats.fills
        )

    outputs_root = root / "replay_outputs"
    trade_file = _write_jsonl(root, outputs_root / "trades.jsonl", trades)
    entry_file = _write_jsonl(root, outputs_root / "entry_timeline.jsonl", entry_timeline)
    observations_path = _bundle_member(root, observations_relative_path)
    results = [_finite_json(result) for result in results]
    economic_metrics_ready = _economic_metrics_finite_and_complete(results)
    observations = {
        "version": OBSERVATIONS_VERSION,
        "research_engine": "scalp",
        "research_only": True,
        "advisory_only": True,
        "economics_interpretation_allowed": False,
        "audit_status": "pending",
        "economic_metrics_finite_and_complete": economic_metrics_ready,
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "activation_authority": False,
        "registry_access": "forbidden",
        "live_bridge_access": "forbidden",
        "network_clients_imported": False,
        "network_isolation_required_externally": True,
        "requested_pairs": pairs,
        "test_start_inclusive": _iso(start),
        "test_end_exclusive_for_bars": _iso(end),
        "fill_delay_bars": FILL_DELAY_BARS,
        "entry_fill_rule": "decision on completed M1 bar; earliest fill at next M1 bar open",
        "spread_model": {
            "source_bid_ask_ohlc": True,
            "source_spread_intrinsic": True,
            "extra_spread_bps": float(extra_spread_bps),
            "sl_extra_slip_bps": float(sl_extra_slip_bps),
            "optimistic_mode": False,
        },
        "config_sha256": config_sha256,
        "input_bundle_manifest_sha256": str(
            verified["bundle"].get("manifest_content_sha256") or ""
        ),
        "replay_inputs": replay_inputs,
        "results": results,
        "trade_ledger": {
            **{key: value for key, value in trade_file.items() if key != "path"},
            "path": _relative_to_bundle(root, Path(trade_file["path"])),
        },
        "entry_timeline": {
            **{key: value for key, value in entry_file.items() if key != "path"},
            "path": _relative_to_bundle(root, Path(entry_file["path"])),
        },
    }
    _write_json(root, observations_path, observations, self_hash=True)
    return _load_manifest(observations_path)


def _read_jsonl(bundle_root: Path, evidence: dict[str, Any]) -> list[dict[str, Any]]:
    relative = str(evidence.get("path") or "")
    path = _assert_regular_bundle_file(bundle_root, relative)
    if _file_sha256(path) != str(evidence.get("content_sha256") or ""):
        raise RuntimeError(f"replay ledger content hash mismatch: {relative}")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(rows) != int(evidence.get("rows") or 0):
        raise RuntimeError(f"replay ledger row-count mismatch: {relative}")
    return rows


def audit_scalp_window(
    *,
    bundle_root: Path,
    name: str,
    requested_pairs: list[str],
    train_end: Any,
    test_start: Any,
    test_end: Any,
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    pairs = _pairs(requested_pairs)
    train = _utc(train_end)
    start = _utc(test_start)
    end = _utc(test_end)
    if not train < start < end:
        raise ValueError("invalid ordered scalp train/test window")
    verified = verify_input_bundle(root, requested_pairs=pairs)
    observations_path = _assert_regular_bundle_file(root, "raw_replay_observations.json")
    observations = _load_manifest(observations_path)
    if str(observations.get("version")) != OBSERVATIONS_VERSION:
        raise RuntimeError("unsupported scalp replay-observations version")
    if dict(verified["config_manifest"].get("engine_source_sha256") or {}) != _engine_source_hashes():
        raise RuntimeError("scalp research engine source drifted before causal audit")
    if bool(observations.get("economics_interpretation_allowed")):
        raise RuntimeError("raw scalp replay observations bypassed the audit gate")
    if str(observations.get("future_data_access")) != "forbidden":
        raise RuntimeError("scalp replay did not forbid future data")
    if int(observations.get("fill_delay_bars") or 0) != FILL_DELAY_BARS:
        raise RuntimeError("scalp replay fill delay is not the fixed next-bar contract")
    if list(observations.get("requested_pairs") or []) != pairs:
        raise RuntimeError("scalp replay requested-pair scope mismatch")
    if str(observations.get("input_bundle_manifest_sha256") or "") != str(
        verified["bundle"].get("manifest_content_sha256") or ""
    ):
        raise RuntimeError("scalp replay input-bundle identity mismatch")
    if str(observations.get("config_sha256") or "") != str(
        verified["config_manifest"].get("config_sha256") or ""
    ):
        raise RuntimeError("scalp replay fixed-config identity mismatch")
    if dict(observations.get("spread_model") or {}) != dict(
        verified["config_manifest"].get("spread_model") or {}
    ):
        raise RuntimeError("scalp replay spread/slippage model mismatch")
    results_by_pair = {
        str(item.get("symbol") or "").upper(): dict(item)
        for item in list(observations.get("results") or [])
    }
    if (
        len(list(observations.get("results") or [])) != len(pairs)
        or set(results_by_pair) != set(pairs)
        or any(int(results_by_pair[pair].get("bars_total") or 0) < 1 for pair in pairs)
    ):
        raise RuntimeError("scalp replay lacks test-window bars for a requested pair")
    if _utc(observations.get("test_start_inclusive")) != start:
        raise RuntimeError("scalp replay test-start mismatch")
    if _utc(observations.get("test_end_exclusive_for_bars")) != end:
        raise RuntimeError("scalp replay test-end mismatch")

    train_snapshot = verified["train_snapshot"]
    replay_snapshot = verified["replay_snapshot"]
    if _utc(train_snapshot.get("cutoff_inclusive")) != train:
        raise RuntimeError("training M1 snapshot cutoff mismatch")
    if _utc(replay_snapshot.get("cutoff_inclusive")) != end:
        raise RuntimeError("replay M1 snapshot cutoff mismatch")
    train_paths = {str(item["path"]) for item in list(train_snapshot.get("files") or [])}
    replay_paths = {str(item["path"]) for item in list(replay_snapshot.get("files") or [])}
    if train_paths & replay_paths:
        raise RuntimeError("training and replay M1 snapshots are not physically separate")
    train_by_pair = {
        str(item["pair"]): _assert_regular_bundle_file(root, str(item["path"]))
        for item in list(train_snapshot.get("files") or [])
    }
    replay_by_pair = {
        str(item["pair"]): _assert_regular_bundle_file(root, str(item["path"]))
        for item in list(replay_snapshot.get("files") or [])
    }
    if any(os.path.samefile(train_by_pair[pair], replay_by_pair[pair]) for pair in pairs):
        raise RuntimeError("training and replay M1 snapshots resolve to the same physical file")
    for manifest, cutoff, label in (
        (train_snapshot, train, "training"),
        (replay_snapshot, end, "replay"),
    ):
        for item in list(manifest.get("files") or []):
            path = _assert_regular_bundle_file(root, str(item.get("path") or ""))
            observed = _inspect_m1_csv(path)
            declared = {
                key: item.get(key)
                for key in (
                    "rows",
                    "min_bar_open_ts",
                    "max_bar_open_ts",
                    "max_knowledge_ts",
                )
            }
            if observed != declared:
                raise RuntimeError(f"{label} M1 snapshot row/timestamp evidence mismatch")
            if _utc(observed["max_knowledge_ts"]) > cutoff:
                raise RuntimeError(f"{label} M1 snapshot crosses its knowledge cutoff")

    replay_inputs = list(observations.get("replay_inputs") or [])
    if set(replay_inputs) != replay_paths or len(replay_inputs) != len(pairs):
        raise RuntimeError("scalp replay did not consume the complete sealed replay snapshot")
    for relative in replay_inputs:
        _assert_regular_bundle_file(root, str(relative))
    entries = _read_jsonl(root, dict(observations.get("entry_timeline") or {}))
    trades = _read_jsonl(root, dict(observations.get("trade_ledger") or {}))
    for entry in entries:
        if str(entry.get("pair") or "") not in pairs:
            raise RuntimeError("scalp entry escaped requested-pair scope")
        if int(entry.get("observed_fill_delay_bars") or 0) != FILL_DELAY_BARS:
            raise RuntimeError("scalp entry drifted from the fixed one-bar fill delay")
        if _utc(entry.get("fill_bar_open_ts")) <= _utc(entry.get("decision_bar_open_ts")):
            raise RuntimeError("scalp entry event clock is not causal")
        if not start <= _utc(entry.get("fill_bar_open_ts")) < end:
            raise RuntimeError("scalp entry fill escaped the test window")
    for trade in trades:
        if str(trade.get("pair") or "") not in pairs:
            raise RuntimeError("scalp trade escaped requested-pair scope")
        exit_ts = dt.datetime.fromtimestamp(float(trade.get("exit_epoch") or 0.0), tz=dt.timezone.utc)
        if not start < exit_ts <= end:
            raise RuntimeError("scalp trade exit escaped the test window")

    audit = {
        "version": AUDIT_VERSION,
        "name": str(name),
        "research_engine": "scalp",
        "research_only": True,
        "advisory_only": True,
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "activation_authority": False,
        "registry_access": "forbidden",
        "live_bridge_access": "forbidden",
        "requested_pairs": pairs,
        "pairs": pairs,
        "timeframes": ["M1"],
        "train_end": _iso(train),
        "test_start": _iso(start),
        "test_end": _iso(end),
        "embargo_seconds": (start - train).total_seconds(),
        "ordered_window": True,
        "training_snapshot_use": "sealed_reference_only_fixed_config_no_parameter_fit",
        "separate_physical_train_and_replay_snapshots": True,
        "source_paths_recorded": False,
        "all_manifest_paths_bundle_relative": True,
        "input_content_hashes_verified": True,
        "requested_pair_completeness": True,
        "input_bundle_manifest_sha256": str(
            verified["bundle"].get("manifest_content_sha256") or ""
        ),
        "training_snapshot_manifest_sha256": str(
            train_snapshot.get("manifest_content_sha256") or ""
        ),
        "replay_snapshot_manifest_sha256": str(
            replay_snapshot.get("manifest_content_sha256") or ""
        ),
        "raw_replay_observations_sha256": str(
            observations.get("manifest_content_sha256") or ""
        ),
        "fill_delay_bars": FILL_DELAY_BARS,
        "observed_entries": len(entries),
        "observed_sides_by_pair": {
            pair: sorted(
                {
                    str(item.get("side") or "")
                    for item in entries
                    if str(item.get("pair") or "") == pair and str(item.get("side") or "")
                }
            )
            for pair in pairs
        },
        "minimum_observed_fill_delay_bars": (
            min(int(item["observed_fill_delay_bars"]) for item in entries)
            if entries
            else None
        ),
        "trades": len(trades),
        "economic_metrics_finite_and_complete": bool(
            observations.get("economic_metrics_finite_and_complete")
        ),
        "spread_model": dict(observations.get("spread_model") or {}),
        "audit_pass_required_before_economics": True,
        "strategy_goal_evaluated": False,
        "strategy_goal_passed": False,
        "success_claim_authorized": False,
        "passed": True,
    }
    audit_path = root / "point_in_time_audit.json"
    _write_json(root, audit_path, audit, self_hash=True)
    return _load_manifest(audit_path)


def _validate_ordered_windows(windows: list[dict[str, Any]]) -> None:
    if not windows:
        raise ValueError("at least one explicit scalp window is required")
    names: set[str] = set()
    previous: tuple[dt.datetime, dt.datetime, dt.datetime] | None = None
    for window in windows:
        name = str(window.get("name") or "").strip()
        name_key = name.casefold()
        if not _WINDOW_RE.fullmatch(name) or name_key in names:
            raise ValueError(f"scalp window name must be a unique safe identifier: {name!r}")
        names.add(name_key)
        current = (
            _utc(window["train_end"]),
            _utc(window["test_start"]),
            _utc(window["test_end"]),
        )
        if not current[0] < current[1] < current[2]:
            raise ValueError(f"invalid causal window ordering: {name}")
        if previous is not None and any(
            current[index] <= previous[index] for index in range(len(current))
        ):
            raise ValueError("scalp windows must be supplied in chronological order")
        if previous is not None and current[1] < previous[2]:
            raise ValueError("scalp test windows must not overlap")
        previous = current


def _prepare_window(
    *,
    bundle_root: Path,
    source_csv_root: Path,
    requested_pairs: list[str],
    train_end: Any,
    test_end: Any,
    extra_spread_bps: float,
    sl_extra_slip_bps: float,
    resume: bool,
) -> None:
    root = Path(bundle_root).resolve()
    bundle_path = root / "input_bundle_manifest.json"
    if root.exists() and any(root.iterdir()):
        if not resume:
            raise FileExistsError(f"scalp window output must be empty: {root}")
        if not bundle_path.is_file():
            raise RuntimeError("cannot resume a partial scalp bundle without its input manifest")
        verified = verify_input_bundle(root, requested_pairs=requested_pairs)
        if _utc(verified["train_snapshot"].get("cutoff_inclusive")) != _utc(train_end):
            raise RuntimeError("resumed scalp training snapshot cutoff mismatch")
        if _utc(verified["replay_snapshot"].get("cutoff_inclusive")) != _utc(test_end):
            raise RuntimeError("resumed scalp replay snapshot cutoff mismatch")
        if dict(verified["config_manifest"].get("spread_model") or {}) != {
            "source_bid_ask_ohlc": True,
            "source_spread_intrinsic": True,
            "extra_spread_bps": float(extra_spread_bps),
            "sl_extra_slip_bps": float(sl_extra_slip_bps),
            "optimistic_mode": False,
        }:
            raise RuntimeError("resumed scalp spread/slippage model mismatch")
        return
    root.mkdir(parents=True, exist_ok=True)
    train_snapshot = build_m1_snapshot(
        bundle_root=root,
        source_csv_root=source_csv_root,
        snapshot_relative_root="inputs/train_m1",
        role="train",
        requested_pairs=requested_pairs,
        cutoff=train_end,
    )
    replay_snapshot = build_m1_snapshot(
        bundle_root=root,
        source_csv_root=source_csv_root,
        snapshot_relative_root="inputs/replay_m1",
        role="replay",
        requested_pairs=requested_pairs,
        cutoff=test_end,
    )
    config_manifest = _write_config_manifest(
        root,
        requested_pairs,
        extra_spread_bps=extra_spread_bps,
        sl_extra_slip_bps=sl_extra_slip_bps,
    )
    _write_input_bundle_manifest(
        bundle_root=root,
        requested_pairs=requested_pairs,
        train_snapshot=train_snapshot,
        replay_snapshot=replay_snapshot,
        config_manifest=config_manifest,
    )


def _publish_advisory_economics(
    *,
    bundle_root: Path,
    run_summary_sha256: str,
) -> dict[str, Any]:
    root = Path(bundle_root).resolve()
    audit = _load_manifest(_assert_regular_bundle_file(root, "point_in_time_audit.json"))
    if not bool(audit.get("passed")):
        raise RuntimeError("point-in-time audit did not pass; economics remain forbidden")
    observations = _load_manifest(_assert_regular_bundle_file(root, "raw_replay_observations.json"))
    if str(observations.get("manifest_content_sha256") or "") != str(
        audit.get("raw_replay_observations_sha256") or ""
    ):
        raise RuntimeError("raw scalp observations changed after the point-in-time audit")
    if not bool(observations.get("economic_metrics_finite_and_complete")):
        raise RuntimeError(
            "scalp economic metrics are null, non-finite, incomplete, or untraded; "
            "advisory economics remain forbidden"
        )
    summary_path = root.parent / "causal_walk_forward_summary.json"
    summary = _load_manifest(_assert_regular_bundle_file(root.parent, summary_path.name))
    if str(summary.get("manifest_content_sha256") or "") != str(run_summary_sha256):
        raise RuntimeError("scalp run summary identity mismatch before economics publication")
    if not bool(summary.get("passed")) or not bool(summary.get("economics_interpretation_allowed")):
        raise RuntimeError("scalp run summary did not authorize economic interpretation")
    matching_window = [
        item
        for item in list(summary.get("window_audits") or [])
        if str(item.get("name") or "") == str(audit.get("name") or "")
    ]
    if len(matching_window) != 1 or str(matching_window[0].get("content_sha256") or "") != str(
        audit.get("manifest_content_sha256") or ""
    ):
        raise RuntimeError("scalp run summary is not bound to the passing window audit")
    economics = {
        "version": ECONOMICS_VERSION,
        "research_engine": "scalp",
        "research_only": True,
        "advisory_only": True,
        "promotion_evidence": False,
        "activation_authority": False,
        "causal_audit_passed": True,
        "point_in_time_audit_sha256": str(audit.get("manifest_content_sha256") or ""),
        "causal_walk_forward_summary_sha256": str(run_summary_sha256),
        "requested_pairs": list(observations.get("requested_pairs") or []),
        "fill_delay_bars": int(observations.get("fill_delay_bars") or 0),
        "spread_model": dict(observations.get("spread_model") or {}),
        "results": list(observations.get("results") or []),
        "trade_ledger": dict(observations.get("trade_ledger") or {}),
        "entry_timeline": dict(observations.get("entry_timeline") or {}),
        "economic_claims": "not_evaluated",
        "economic_metrics_finite_and_complete": True,
        "strategy_goal": {
            "target_win_rate": TARGET_WIN_RATE,
            "status": "not_evaluated",
            "passed": False,
            "requires": "independent minimum-sample and BUY/SELL-per-pair validation",
        },
        "success_claim_authorized": False,
    }
    path = root / "advisory_economics.json"
    _write_json(root, path, economics, self_hash=True)
    return _load_manifest(path)


def scalp_child_environment(source: dict[str, str]) -> dict[str, str]:
    """Build a minimal process environment with no credential-discovery roots."""

    allow = {
        "COMSPEC",
        "LANG",
        "LC_ALL",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TZ",
        "WINDIR",
    }
    safe = {
        key: value
        for key, value in source.items()
        if str(key).upper() in allow and str(value)
    }
    safe["FXSTACK_EXECUTION_PROVIDER"] = "offline"
    safe["FXSTACK_MARKET_DATA_PROVIDER"] = "offline"
    safe.pop("FXSTACK_MODEL_ACTIVATION_MANIFEST", None)
    safe.pop("FXSTACK_REGISTRY_ROOT", None)
    return safe


def run_scalp_walk_forward(
    *,
    args: argparse.Namespace,
    windows: list[dict[str, Any]],
    requested_pairs: list[str],
    root: Path,
) -> dict[str, Any]:
    """Run every scalp window, gate the run, then publish advisory economics."""

    if int(args.fill_delay_bars) != FILL_DELAY_BARS:
        raise ValueError("the fixed scalp replay requires fill_delay_bars=1")
    if list(getattr(args, "mode", None) or []):
        raise ValueError("--mode applies only to the default model research engine")
    for name in ("scalp_extra_spread_bps", "scalp_sl_extra_slip_bps"):
        value = float(getattr(args, name))
        if not math.isfinite(value) or value < 0.0:
            raise ValueError(f"{name} must be finite and non-negative")
    _validate_ordered_windows(windows)
    pairs = _pairs(requested_pairs)
    output_root = Path(root).resolve()
    source_csv_root = Path(args.scalp_csv_root).resolve()
    source_available = source_csv_root.is_dir()
    if not source_available and not bool(getattr(args, "resume", False)):
        raise FileNotFoundError(f"scalp M1 CSV source directory is missing: {source_csv_root}")
    roots_overlap = source_available and output_root == source_csv_root
    if not roots_overlap:
        try:
            output_root.relative_to(source_csv_root)
            roots_overlap = True
        except ValueError:
            try:
                source_csv_root.relative_to(output_root)
                roots_overlap = True
            except ValueError:
                pass
    if roots_overlap:
        raise RuntimeError("scalp source and disposable output roots must be physically separate")
    env = scalp_child_environment(dict(os.environ))
    _safe_output_target(output_root, output_root / "causal_walk_forward_summary.json")
    for window in windows:
        name = str(window["name"])
        lexical_window_root = output_root / name
        if (lexical_window_root.exists() or lexical_window_root.is_symlink()) and _is_link_or_reparse(
            lexical_window_root
        ):
            raise RuntimeError(f"scalp window root is a link or reparse point: {name}")
        window_root = _bundle_member(output_root, name)
        if bool(getattr(args, "resume", False)):
            economics_path = window_root / "advisory_economics.json"
            if economics_path.exists() or economics_path.is_symlink():
                raise RuntimeError(
                    "cannot resume a scalp window with published advisory economics; "
                    "use a new output root"
                )
            _safe_output_target(window_root, window_root / "point_in_time_audit.json")
    audits: list[dict[str, Any]] = []
    for window in windows:
        name = str(window["name"])
        lexical_window_root = output_root / name
        if (lexical_window_root.exists() or lexical_window_root.is_symlink()) and _is_link_or_reparse(
            lexical_window_root
        ):
            raise RuntimeError(f"scalp window root is a link or reparse point: {name}")
        window_root = _bundle_member(output_root, name)
        _prepare_window(
            bundle_root=window_root,
            source_csv_root=source_csv_root,
            requested_pairs=pairs,
            train_end=window["train_end"],
            test_end=window["test_end"],
            extra_spread_bps=float(args.scalp_extra_spread_bps),
            sl_extra_slip_bps=float(args.scalp_sl_extra_slip_bps),
            resume=bool(getattr(args, "resume", False)),
        )
        observations_path = window_root / "raw_replay_observations.json"
        if not (bool(getattr(args, "resume", False)) and observations_path.is_file()):
            command = [
                sys.executable,
                "-I",
                "-B",
                str(Path(__file__).resolve()),
                "replay",
                "--bundle-root",
                str(window_root),
                "--pairs",
                ",".join(pairs),
                "--test-start",
                _iso(window["test_start"]),
                "--test-end",
                _iso(window["test_end"]),
                "--extra-spread-bps",
                str(float(args.scalp_extra_spread_bps)),
                "--sl-extra-slip-bps",
                str(float(args.scalp_sl_extra_slip_bps)),
            ]
            print(f"[causal-wf] window={name} stage=scalp_replay", flush=True)
            subprocess.run(command, cwd=str(REPO_ROOT), env=env, check=True)
        audits.append(
            audit_scalp_window(
                bundle_root=window_root,
                name=name,
                requested_pairs=pairs,
                train_end=window["train_end"],
                test_start=window["test_start"],
                test_end=window["test_end"],
            )
        )

    summary = {
        "version": RUN_VERSION,
        "research_engine": "scalp",
        "research_only": True,
        "advisory_only": True,
        "future_data_access": "forbidden",
        "runtime_store_updated": False,
        "activation_authority": False,
        "registry_access": "forbidden",
        "live_bridge_access": "forbidden",
        "requested_pairs": pairs,
        "pairs": pairs,
        "timeframes": ["M1"],
        "fill_delay_bars": FILL_DELAY_BARS,
        "windows_supplied_in_chronological_order": True,
        "window_audits": [
            {
                "name": str(audit["name"]),
                "path": f"{audit['name']}/point_in_time_audit.json",
                "content_sha256": str(audit.get("manifest_content_sha256") or ""),
                "passed": bool(audit.get("passed")),
                "economic_metrics_finite_and_complete": bool(
                    audit.get("economic_metrics_finite_and_complete")
                ),
            }
            for audit in audits
        ],
        "audit_pass_required_before_economics": True,
        "strategy_goal": {
            "target_win_rate": TARGET_WIN_RATE,
            "status": "not_evaluated",
            "passed": False,
            "requires": "independent minimum-sample and BUY/SELL-per-pair validation",
        },
        "success_claim_authorized": False,
        "economics_interpretation_allowed": all(
            bool(audit.get("passed"))
            and bool(audit.get("economic_metrics_finite_and_complete"))
            for audit in audits
        ),
        "passed": all(bool(audit.get("passed")) for audit in audits),
    }
    summary_path = output_root / "causal_walk_forward_summary.json"
    _write_json(output_root, summary_path, summary, self_hash=True)
    summary = _load_manifest(summary_path)
    if not bool(summary.get("passed")):
        raise RuntimeError("scalp causal run summary failed; economics remain forbidden")
    summary_sha256 = str(summary.get("manifest_content_sha256") or "")
    if bool(summary.get("economics_interpretation_allowed")):
        for audit in audits:
            _publish_advisory_economics(
                bundle_root=_bundle_member(output_root, str(audit["name"])),
                run_summary_sha256=summary_sha256,
            )
    return summary


def _replay_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Private sealed-bundle scalp replay child.")
    parser.add_argument("command", choices=["replay"])
    parser.add_argument("--bundle-root", required=True)
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--test-start", required=True)
    parser.add_argument("--test-end", required=True)
    parser.add_argument("--extra-spread-bps", type=float, default=0.0)
    parser.add_argument("--sl-extra-slip-bps", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _replay_parser().parse_args(argv)
    replay_sealed_bundle(
        bundle_root=Path(args.bundle_root),
        requested_pairs=[item for item in args.pairs.split(",") if item.strip()],
        test_start=args.test_start,
        test_end=args.test_end,
        extra_spread_bps=args.extra_spread_bps,
        sl_extra_slip_bps=args.sl_extra_slip_bps,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
