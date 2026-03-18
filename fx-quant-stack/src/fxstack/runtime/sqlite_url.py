from __future__ import annotations

from pathlib import Path


def normalize_sqlite_database_url(database_url: str, *, base_dir: Path | None = None) -> str:
    url = str(database_url or "").strip()
    lower = url.lower()
    prefix = ""
    if lower.startswith("sqlite+pysqlite:///"):
        prefix = "sqlite+pysqlite:///"
    elif lower.startswith("sqlite:///"):
        prefix = "sqlite:///"
    else:
        return url

    tail = url[len(prefix) :].strip()
    if not tail or tail == ":memory:":
        return url

    query = ""
    if "?" in tail:
        tail, q = tail.split("?", 1)
        query = f"?{q}"
    tail = tail.replace("\\", "/")

    # Absolute (POSIX) path or Windows drive path.
    is_windows_abs = len(tail) >= 3 and tail[1] == ":" and tail[2] in ("/", "\\")
    if tail.startswith("/") or is_windows_abs:
        abs_path = Path(tail)
    else:
        root = (base_dir or Path.cwd()).resolve()
        abs_path = (root / tail).resolve()

    return f"{prefix}{abs_path.as_posix()}{query}"


def ensure_sqlite_database_dir(database_url: str, *, base_dir: Path | None = None) -> str:
    normalized = normalize_sqlite_database_url(database_url, base_dir=base_dir)
    lower = normalized.lower()
    if not (lower.startswith("sqlite+pysqlite:///") or lower.startswith("sqlite:///")):
        return normalized
    if normalized.endswith(":memory:"):
        return normalized

    _, tail = normalized.split(":///", 1)
    path_part = tail.split("?", 1)[0]
    if not path_part or path_part == ":memory:":
        return normalized

    db_path = Path(path_part)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return normalized
