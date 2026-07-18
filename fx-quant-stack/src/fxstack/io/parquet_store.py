from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import time
import uuid

import pandas as pd
from filelock import FileLock

from fxstack.utils.hashing import hash_mapping
from fxstack.utils.paths import ensure_dir


class ParquetStore:
    _PARTITION_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
    _CONTENT_HASH_CHUNK_BYTES = 1024 * 1024
    _SCOPE_GENERATION_VERSION = "parquet_scope_generation_v1"
    _SCOPE_LOCK_TIMEOUT_SECS = 60.0

    def __init__(self, root: Path, *, partition_cache_ttl_secs: float = 15.0) -> None:
        self.root = ensure_dir(Path(root))
        self._partition_cache_ttl_secs = max(0.0, float(partition_cache_ttl_secs))
        self._partition_cache: dict[tuple[str, str, str], tuple[float, list[Path]]] = {}
        self._scope_locks: dict[str, FileLock] = {}

    def _partition_cache_key(self, *, provider: str, pair: str, timeframe: str) -> tuple[str, str, str]:
        return (str(provider), str(pair), str(timeframe))

    @classmethod
    def _validated_partition_component(cls, *, field: str, value: object) -> str:
        text = str(value)
        if text in {".", ".."} or cls._PARTITION_COMPONENT_RE.fullmatch(text) is None:
            raise ValueError(
                f"invalid partition {field}: expected one non-empty path component "
                "containing only ASCII letters, digits, '.', '_', or '-'"
            )
        return text

    def _partition_base(self, *, provider: str, pair: str, timeframe: str) -> Path:
        provider_txt = self._validated_partition_component(field="provider", value=provider)
        pair_txt = self._validated_partition_component(field="pair", value=pair)
        timeframe_txt = self._validated_partition_component(field="timeframe", value=timeframe)
        return self.root / f"provider={provider_txt}" / f"pair={pair_txt}" / f"timeframe={timeframe_txt}"

    def _scope_identity(self, *, provider: str, pair: str, timeframe: str) -> tuple[Path, str]:
        target = self._partition_base(provider=provider, pair=pair, timeframe=timeframe)
        normalized = os.path.normcase(str(target.resolve(strict=False)))
        identity = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return target, identity

    def _scope_state_dir(self) -> Path:
        return ensure_dir(self.root / ".locks")

    def _scope_lock(self, *, provider: str, pair: str, timeframe: str) -> FileLock:
        _, identity = self._scope_identity(provider=provider, pair=pair, timeframe=timeframe)
        candidate = FileLock(
            str(self._scope_state_dir() / f"{identity}.lock"),
            timeout=self._SCOPE_LOCK_TIMEOUT_SECS,
        )
        return self._scope_locks.setdefault(identity, candidate)

    def _scope_generation_path(self, *, provider: str, pair: str, timeframe: str) -> Path:
        _, identity = self._scope_identity(provider=provider, pair=pair, timeframe=timeframe)
        return self._scope_state_dir() / f"{identity}.generation.json"

    @staticmethod
    def _best_effort_unlink(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _fsync_file(path: Path) -> None:
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_scope_generation_locked(self, *, provider: str, pair: str, timeframe: str) -> int:
        path = self._scope_generation_path(provider=provider, pair=pair, timeframe=timeframe)
        if not path.exists():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise TypeError("generation manifest must be a JSON object")
            if str(payload.get("version") or "") != self._SCOPE_GENERATION_VERSION:
                raise ValueError("generation manifest version mismatch")
            generation = payload.get("generation")
            if isinstance(generation, bool) or not isinstance(generation, int):
                raise TypeError("generation must be an integer")
            if generation < 0:
                raise ValueError("generation must be non-negative")
            return generation
        except Exception as exc:
            raise RuntimeError(f"invalid partition generation manifest: {path}") from exc

    def _advance_scope_generation_locked(self, *, provider: str, pair: str, timeframe: str) -> int:
        path = self._scope_generation_path(provider=provider, pair=pair, timeframe=timeframe)
        generation = self._read_scope_generation_locked(
            provider=provider,
            pair=pair,
            timeframe=timeframe,
        ) + 1
        pending = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
        try:
            with pending.open("w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "version": self._SCOPE_GENERATION_VERSION,
                        "generation": generation,
                    },
                    handle,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, path)
            self._fsync_directory(path.parent)
        finally:
            self._best_effort_unlink(pending)
        return generation

    def _invalidate_partition_cache(self, *, provider: str, pair: str, timeframe: str) -> None:
        self._partition_cache.pop(self._partition_cache_key(provider=provider, pair=pair, timeframe=timeframe), None)

    def _recover_interrupted_replacement(self, target: Path) -> bool:
        """Restore the newest last-known-good snapshot left by an interrupted swap."""
        if target.exists() or not target.parent.exists():
            return False

        candidates: list[tuple[int, str, Path]] = []
        for path in target.parent.glob(f".{target.name}.backup-*"):
            try:
                candidates.append((int(path.stat().st_mtime_ns), path.name, path))
            except OSError:
                continue

        for _, _, backup in sorted(candidates, reverse=True):
            if target.exists():
                return True
            try:
                backup.replace(target)
                return True
            except FileNotFoundError:
                # Another reader may have recovered this exact backup first.
                continue
            except OSError as exc:
                if target.exists():
                    return True
                raise RuntimeError(
                    f"failed to recover interrupted partition replacement: {target}"
                ) from exc
        return target.exists()

    def _list_partition_files(self, *, provider: str, pair: str, timeframe: str) -> list[Path]:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._list_partition_files_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
            )

    def _list_partition_files_locked(self, *, provider: str, pair: str, timeframe: str) -> list[Path]:
        base = self._partition_base(provider=provider, pair=pair, timeframe=timeframe)
        if self._recover_interrupted_replacement(base):
            self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        if not base.exists():
            return []

        cache_key = self._partition_cache_key(provider=provider, pair=pair, timeframe=timeframe)
        now = time.time()
        cached = self._partition_cache.get(cache_key)
        if cached and (now - cached[0]) <= self._partition_cache_ttl_secs:
            return list(cached[1])

        date_dirs = sorted(
            path for path in base.iterdir() if path.is_dir() and path.name.startswith("date=")
        )
        files = [path / "bars.parquet" for path in date_dirs if (path / "bars.parquet").exists()]
        self._partition_cache[cache_key] = (now, files)
        return list(files)

    def _list_partition_files_in_range(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        start_ts: object | None = None,
        end_ts: object | None = None,
    ) -> list[Path]:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._list_partition_files_in_range_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                start_ts=start_ts,
                end_ts=end_ts,
            )

    def _list_partition_files_in_range_locked(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        start_ts: object | None = None,
        end_ts: object | None = None,
    ) -> list[Path]:
        start_bound = self._normalize_bound(start_ts)
        end_bound = self._normalize_bound(end_ts)
        if start_bound is None and end_bound is None:
            return self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe)

        base = self._partition_base(provider=provider, pair=pair, timeframe=timeframe)
        if self._recover_interrupted_replacement(base):
            self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        if not base.exists():
            return []

        if start_bound is None:
            return self._filter_partition_files(
                self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe),
                start_ts=start_bound,
                end_ts=end_bound,
            )
        if end_bound is None:
            return self._filter_partition_files(
                self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe),
                start_ts=start_bound,
                end_ts=end_bound,
            )

        start_day = start_bound.normalize()
        end_day = end_bound.normalize()
        if start_day > end_day:
            return []

        span_days = int((end_day - start_day) / pd.Timedelta(days=1)) + 1
        if span_days > 400:
            return self._filter_partition_files(
                self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe),
                start_ts=start_bound,
                end_ts=end_bound,
            )

        files: list[Path] = []
        current = start_day
        one_day = pd.Timedelta(days=1)
        while current <= end_day:
            day_path = base / f"date={current.strftime('%Y-%m-%d')}" / "bars.parquet"
            if day_path.exists():
                files.append(day_path)
            current = current + one_day
        return files

    @staticmethod
    def _read_partition(path: Path) -> pd.DataFrame:
        # Reads fail closed. Automatic quarantine would be a scope mutation and
        # could turn a transient access error into permanent data loss.
        return pd.read_parquet(path)

    @staticmethod
    def _normalize_bound(value: object) -> pd.Timestamp | None:
        if value is None:
            return None
        ts = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(ts):
            return None
        return pd.Timestamp(ts)

    @staticmethod
    def _canonicalize_timestamp_rows(
        frame: pd.DataFrame,
        *,
        date_col: str = "date",
        require_valid: bool,
    ) -> pd.DataFrame:
        """Make UTC ``ts`` authoritative for ordering, deduplication, and partition dates."""
        out = frame.copy()
        if out.empty:
            return out
        if "ts" not in out.columns:
            if require_valid:
                raise ValueError("ParquetStore rows require a ts column")
            return pd.DataFrame(columns=out.columns)
        parsed = pd.to_datetime(out["ts"], utc=True, errors="coerce")
        valid = parsed.notna()
        if require_valid and not bool(valid.all()):
            invalid_count = int((~valid).sum())
            raise ValueError(f"ParquetStore rows contain {invalid_count} invalid UTC timestamp(s)")
        out = out.loc[valid].copy()
        parsed = parsed.loc[valid]
        out["ts"] = parsed
        if date_col:
            out[date_col] = parsed.dt.strftime("%Y-%m-%d")
        return out

    def _filter_partition_files(
        self,
        paths: list[Path],
        *,
        start_ts: object | None = None,
        end_ts: object | None = None,
    ) -> list[Path]:
        start_bound = self._normalize_bound(start_ts)
        end_bound = self._normalize_bound(end_ts)
        if start_bound is None and end_bound is None:
            return list(paths)

        start_date = start_bound.normalize() if start_bound is not None else None
        end_date = end_bound.normalize() if end_bound is not None else None
        filtered: list[Path] = []
        for path in paths:
            part_name = path.parent.name
            if not part_name.startswith("date="):
                filtered.append(path)
                continue
            part_date = pd.to_datetime(part_name.split("=", 1)[1], utc=True, errors="coerce")
            if pd.isna(part_date):
                filtered.append(path)
                continue
            partition_day = pd.Timestamp(part_date).normalize()
            if start_date is not None and partition_day < start_date:
                continue
            if end_date is not None and partition_day > end_date:
                continue
            filtered.append(path)
        return filtered

    def write_partitioned(self, df: pd.DataFrame, *, provider: str, pair: str, timeframe: str, date_col: str = "date") -> Path:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._write_partitioned_locked(
                df,
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                date_col=date_col,
            )

    def _write_partitioned_locked(
        self,
        df: pd.DataFrame,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        date_col: str,
    ) -> Path:
        out_dir = self._partition_base(provider=provider, pair=pair, timeframe=timeframe)
        if self._recover_interrupted_replacement(out_dir):
            self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        out_dir = ensure_dir(out_dir)
        if df.empty:
            return out_dir
        canonical = self._canonicalize_timestamp_rows(df, date_col=date_col, require_valid=True)
        prepared: list[tuple[Path, Path]] = []
        rollback_paths: dict[Path, Path | None] = {}
        try:
            for day, part in canonical.groupby(date_col, dropna=False):
                day_str = str(day)
                destination = ensure_dir(out_dir / f"date={day_str}") / "bars.parquet"
                existing = pd.DataFrame()
                if destination.exists():
                    existing = self._read_partition(destination)
                    existing = self._canonicalize_timestamp_rows(
                        existing,
                        date_col=date_col,
                        require_valid=False,
                    )
                    merged = pd.concat([existing, part], ignore_index=True).drop_duplicates(
                        subset=["pair", "ts", "timeframe"],
                        keep="last",
                    )
                else:
                    merged = part
                merged = merged.sort_values("ts", kind="mergesort").reset_index(drop=True)
                if destination.exists() and not existing.empty:
                    comparable_existing = existing.sort_values(
                        "ts",
                        kind="mergesort",
                    ).reset_index(drop=True)
                    if (
                        list(comparable_existing.columns) == list(merged.columns)
                        and comparable_existing.equals(merged)
                    ):
                        continue
                pending = destination.with_name(
                    f".{destination.name}.tmp-{uuid.uuid4().hex}"
                )
                try:
                    merged.to_parquet(pending, index=False)
                    self._fsync_file(pending)
                except Exception:
                    self._best_effort_unlink(pending)
                    raise
                prepared.append((destination, pending))

            if not prepared:
                return out_dir

            for destination, _ in prepared:
                backup: Path | None = None
                if destination.exists():
                    backup = destination.with_name(
                        f".{destination.name}.rollback-{uuid.uuid4().hex}"
                    )
                    try:
                        os.link(destination, backup)
                    except OSError:
                        shutil.copy2(destination, backup)
                rollback_paths[destination] = backup

            # Persist the monotonic commit generation before publishing bytes.
            # A failed publish can therefore cause only a safe false invalidation.
            self._advance_scope_generation_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
            )
            published: list[Path] = []
            try:
                for destination, pending in prepared:
                    os.replace(pending, destination)
                    published.append(destination)
                for parent in {destination.parent for destination in published}:
                    self._fsync_directory(parent)
            except Exception as publish_exc:
                rollback_errors: list[Exception] = []
                for destination, _ in reversed(prepared):
                    backup = rollback_paths.get(destination)
                    try:
                        if backup is None:
                            destination.unlink(missing_ok=True)
                        elif backup.exists():
                            os.replace(backup, destination)
                    except Exception as rollback_exc:
                        rollback_errors.append(rollback_exc)
                for parent in {destination.parent for destination, _ in prepared}:
                    self._fsync_directory(parent)
                if rollback_errors:
                    raise RuntimeError(
                        "partition publish failed and rollback was incomplete"
                    ) from publish_exc
                raise
        finally:
            for _, pending in prepared:
                self._best_effort_unlink(pending)
            for backup in rollback_paths.values():
                if backup is not None:
                    self._best_effort_unlink(backup)
        self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        return out_dir

    def replace_partitioned(
        self,
        df: pd.DataFrame,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        date_col: str = "date",
    ) -> Path:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._replace_partitioned_locked(
                df,
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                date_col=date_col,
            )

    def _replace_partitioned_locked(
        self,
        df: pd.DataFrame,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        date_col: str,
    ) -> Path:
        """Replace one complete provider/pair/timeframe scope via a staged swap.

        Feature regeneration is a snapshot operation: retaining rows omitted by
        a newer contract can silently mix schemas.  Build the replacement next
        to the live directory and swap only after every parquet file succeeds.
        """
        target = self._partition_base(provider=provider, pair=pair, timeframe=timeframe)
        root = self.root.resolve()
        target_resolved = target.resolve(strict=False)
        if target_resolved == root or root not in target_resolved.parents:
            raise ValueError(f"partition replacement target escapes store root: {target}")

        parent = ensure_dir(target.parent)
        if self._recover_interrupted_replacement(target):
            self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        stage = Path(tempfile.mkdtemp(prefix=f".{target.name}.stage-", dir=str(parent)))
        backup = parent / f".{target.name}.backup-{uuid.uuid4().hex}"
        canonical = self._canonicalize_timestamp_rows(df, date_col=date_col, require_valid=True)
        try:
            for day, part in canonical.groupby(date_col, dropna=False):
                day_dir = ensure_dir(stage / f"date={day}")
                dedupe_keys = [key for key in ("pair", "ts", "timeframe") if key in part.columns]
                replacement = part.drop_duplicates(subset=dedupe_keys, keep="last") if dedupe_keys else part
                staged_partition = day_dir / "bars.parquet"
                replacement.sort_values("ts", kind="mergesort").to_parquet(
                    staged_partition,
                    index=False,
                )
                self._fsync_file(staged_partition)
                self._fsync_directory(day_dir)

            self._fsync_directory(stage)

            self._advance_scope_generation_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
            )
            moved_existing = False
            if target.exists():
                target.replace(backup)
                moved_existing = True
            try:
                stage.replace(target)
                self._fsync_directory(parent)
            except Exception:
                if moved_existing and backup.exists() and not target.exists():
                    backup.replace(target)
                    self._fsync_directory(parent)
                raise
            if backup.exists():
                try:
                    shutil.rmtree(backup)
                except OSError:
                    # The live replacement is already committed. A locked
                    # backup is harmless and can be cleaned up out of band.
                    pass
        finally:
            if stage.exists():
                try:
                    shutil.rmtree(stage)
                except OSError:
                    pass
        self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        return target

    def source_contract(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        tail_files: int | None = None,
    ) -> dict[str, object]:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._source_contract_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                tail_files=tail_files,
            )

    def _source_contract_locked(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        tail_files: int | None,
    ) -> dict[str, object]:
        """Return a content-backed raw-partition watermark and change fingerprint."""
        # Source contracts are consistency boundaries, so never reuse a cached
        # path listing that could hide a concurrently added date partition.
        self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        paths = self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe)
        contract_scope = "all"
        if tail_files is not None:
            bounded_tail = max(1, int(tail_files))
            paths = paths[-bounded_tail:]
            contract_scope = f"tail:{bounded_tail}"
        partitions: list[dict[str, object]] = []
        for path in paths:
            try:
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as handle:
                    while chunk := handle.read(self._CONTENT_HASH_CHUNK_BYTES):
                        digest.update(chunk)
                        size += len(chunk)
                partitions.append(
                    {
                        "date": path.parent.name.removeprefix("date="),
                        "size": int(size),
                        "sha256": digest.hexdigest(),
                    }
                )
            except OSError:
                partitions.append(
                    {
                        "date": path.parent.name.removeprefix("date="),
                        "missing": True,
                    }
                )
        latest = self.read_latest_row(
            provider=provider,
            pair=pair,
            timeframe=timeframe,
            tail_files=1,
        )
        watermark = ""
        if not latest.empty and "ts" in latest.columns:
            parsed = pd.to_datetime(latest.iloc[-1]["ts"], utc=True, errors="coerce")
            if not pd.isna(parsed):
                watermark = pd.Timestamp(parsed).isoformat()
        payload: dict[str, object] = {
            "contract_version": "parquet_source_v2",
            "content_hash_algorithm": "sha256_file_bytes_v1",
            "generation_contract": self._SCOPE_GENERATION_VERSION,
            "generation": self._read_scope_generation_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
            ),
            "provider": str(provider),
            "pair": str(pair).upper(),
            "timeframe": str(timeframe).upper(),
            "partition_scope": contract_scope,
            "watermark": watermark,
            "partitions": partitions,
        }
        return {**payload, "fingerprint": hash_mapping(payload)}

    def read_pair_timeframe(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        start_ts: object | None = None,
        end_ts: object | None = None,
    ) -> pd.DataFrame:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._read_pair_timeframe_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                start_ts=start_ts,
                end_ts=end_ts,
            )

    def _read_pair_timeframe_locked(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        start_ts: object | None,
        end_ts: object | None,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        paths = self._list_partition_files_in_range(
            provider=provider,
            pair=pair,
            timeframe=timeframe,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        for p in paths:
            df = self._read_partition(p)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = self._canonicalize_timestamp_rows(out, require_valid=False)
        if out.empty:
            return out
        out = out.drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
        out = out.sort_values("ts", kind="mergesort").reset_index(drop=True)

        start_bound = self._normalize_bound(start_ts)
        end_bound = self._normalize_bound(end_ts)
        if start_bound is not None or end_bound is not None:
            ts = out["ts"]
            mask = ts.notna()
            if start_bound is not None:
                mask &= ts >= start_bound
            if end_bound is not None:
                mask &= ts <= end_bound
            out = out.loc[mask].reset_index(drop=True)
        return out

    def read_latest_row(self, *, provider: str, pair: str, timeframe: str, tail_files: int = 3) -> pd.DataFrame:
        """Read only the latest row without scanning the full partition history."""
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._read_latest_row_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                tail_files=tail_files,
            )

    def _read_latest_row_locked(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        tail_files: int,
    ) -> pd.DataFrame:
        paths = self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe)
        if not paths:
            return pd.DataFrame()

        n_files = max(1, int(tail_files))
        frames: list[pd.DataFrame] = []
        for p in paths[-n_files:]:
            df = self._read_partition(p)
            df = self._canonicalize_timestamp_rows(df, require_valid=False)
            if not df.empty:
                frames.append(df.sort_values("ts", kind="mergesort").tail(1))
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values("ts", kind="mergesort").tail(1).reset_index(drop=True)
        return out

    def read_recent_rows(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        tail_files: int = 10,
        max_rows: int = 5000,
    ) -> pd.DataFrame:
        with self._scope_lock(provider=provider, pair=pair, timeframe=timeframe):
            return self._read_recent_rows_locked(
                provider=provider,
                pair=pair,
                timeframe=timeframe,
                tail_files=tail_files,
                max_rows=max_rows,
            )

    def _read_recent_rows_locked(
        self,
        *,
        provider: str,
        pair: str,
        timeframe: str,
        tail_files: int,
        max_rows: int,
    ) -> pd.DataFrame:
        paths = self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe)
        if not paths:
            return pd.DataFrame()

        n_files = max(1, int(tail_files))
        frames: list[pd.DataFrame] = []
        for p in paths[-n_files:]:
            df = self._read_partition(p)
            df = self._canonicalize_timestamp_rows(df, require_valid=False)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()

        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last").sort_values("ts", kind="mergesort")
        n_rows = max(1, int(max_rows))
        return out.tail(n_rows).reset_index(drop=True)
