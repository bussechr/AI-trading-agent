"""Audit the repo's agentic-navigation graph: resolve every cross-reference in the
agent-orchestration docs/config and the `# AGENT:` source breadcrumbs, report broken links.

Scope:
  1. Markdown links [text](path) in AGENTS.md + docs/agents/*.md (+ a few referenced hub md).
  2. `path` / entrypoint / consumer fields in docs/agents/system-map.yaml (JSON).
  3. `# AGENT:` header breadcrumbs in source (ENTRYPOINT / CALLED BY / DEPENDS ON / SEE / CALLED BY).
Outputs a grouped report of references that do NOT resolve to a real file/dir.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
# path-like token: has a slash and a known extension, or is a known top dir reference
PATH_TOKEN = re.compile(r"[A-Za-z0-9_./-]+\.(?:py|md|ts|tsx|js|bat|ps1|ya?ml|json|tsx)\b|[A-Za-z0-9_./-]+/(?:[A-Za-z0-9_./-]+)?")

SKIP_PREFIX = ("http://", "https://", "mailto:", "#")

# These paths are generated locally by documented installer/runtime workflows and
# are intentionally absent from clean source checkouts.
GENERATED_BREADCRUMBS = {"ops/windows/installed_env.bat"}

SYSTEM_MAP_ID_SECTIONS = (
    "systems",
    "files",
    "handshakes",
    "entrypoints",
    "state_stores",
    "environment_sources",
    "dashboard_consumers",
)

SYSTEM_MAP_REFERENCE_FIELDS = {
    "systems": ("depends_on",),
    "files": ("depends_on", "called_by", "handshakes"),
    "handshakes": ("from", "to"),
}


def _norm(p: str) -> str:
    return p.split("#", 1)[0].strip().strip("`")


def _exists_rel(base_dir: Path, ref: str) -> bool:
    ref = _norm(ref)
    if not ref:
        return True
    cand = (base_dir / ref).resolve()
    return cand.exists()


def audit_markdown(md_files: list[Path]) -> list[tuple[str, str]]:
    broken: list[tuple[str, str]] = []
    for md in md_files:
        if not md.exists():
            broken.append((str(md.relative_to(REPO)), "<<HUB FILE MISSING>>"))
            continue
        text = md.read_text(encoding="utf-8", errors="ignore")
        for m in MD_LINK.finditer(text):
            target = m.group(1).strip()
            if target.startswith(SKIP_PREFIX):
                continue
            if not _exists_rel(md.parent, target):
                broken.append((str(md.relative_to(REPO)), target))
    return broken


def audit_system_map(yaml_path: Path) -> list[tuple[str, str]]:
    broken: list[tuple[str, str]] = []
    data = json.loads(yaml_path.read_text(encoding="utf-8"))
    # collect all string values under keys that hold repo-relative paths
    path_keys = {"path", "entrypoints", "shared_logic", "prod_consumers", "research_consumers"}

    def walk(obj, ctx_is_path=False):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, ctx_is_path=(k in path_keys))
        elif isinstance(obj, list):
            for it in obj:
                walk(it, ctx_is_path=ctx_is_path)
        elif isinstance(obj, str) and ctx_is_path:
            if not (REPO / _norm(obj)).exists():
                broken.append((f"system-map.yaml::{obj}", obj))

    walk(data)
    return broken


def audit_system_map_ids(yaml_path: Path) -> list[tuple[str, str]]:
    """Report duplicate IDs and semantic references to undeclared nodes."""

    data = json.loads(yaml_path.read_text(encoding="utf-8"))
    declared: set[str] = set()
    broken: list[tuple[str, str]] = []

    for section in SYSTEM_MAP_ID_SECTIONS:
        section_ids: set[str] = set()
        for item in list(data.get(section) or []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if not item_id:
                broken.append((f"system-map.yaml::{section}", "<missing-id>"))
                continue
            if item_id in section_ids:
                broken.append(
                    (
                        f"system-map.yaml::{section}::{item_id}",
                        f"duplicate-id:first-declared-in:{section}",
                    )
                )
                continue
            section_ids.add(item_id)
            declared.add(item_id)

    for section, fields in SYSTEM_MAP_REFERENCE_FIELDS.items():
        for item in list(data.get(section) or []):
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "<missing-id>").strip()
            for field in fields:
                raw_refs = item.get(field)
                if raw_refs is None:
                    continue
                refs = raw_refs if isinstance(raw_refs, list) else [raw_refs]
                for raw_ref in refs:
                    ref = str(raw_ref or "").strip()
                    if ref and ref not in declared:
                        broken.append(
                            (f"system-map.yaml::{section}::{item_id}::{field}", ref)
                        )

    return broken


def audit_agent_breadcrumbs() -> list[tuple[str, str]]:
    broken: list[tuple[str, str]] = []
    exts = ("*.py", "*.bat", "*.ps1", "*.ts", "*.tsx")
    src_dirs = [
        REPO / "fx-quant-stack" / "src",
        REPO / "ops" / "windows",
        REPO / "tools",
        REPO / "src",
        REPO / "components",
        REPO / "lib",
        REPO / "app",
    ]
    seen_files: set[Path] = set()
    for d in src_dirs:
        if not d.exists():
            continue
        for ext in exts:
            for f in d.rglob(ext):
                if any(part in {"node_modules", ".venv", ".venv_win", ".next", "obj", "bin"} for part in f.parts):
                    continue
                seen_files.add(f)
    for f in sorted(seen_files):
        try:
            lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            if "AGENT:" not in line and "AGENT " not in line:
                continue
            # only lines that declare references
            if not any(tag in line for tag in ("ENTRYPOINT", "CALLED BY", "DEPENDS ON", "SEE", "CALLED_BY")):
                continue
            for tok in PATH_TOKEN.findall(line):
                tok = _norm(tok.rstrip(".,"))
                # only check tokens that carry a real extension (avoid module.dotted false positives)
                if not tok.endswith((".py", ".md", ".ts", ".tsx", ".bat", ".ps1", ".yaml", ".yml", ".json")):
                    continue
                if tok in GENERATED_BREADCRUMBS:
                    continue
                # Breadcrumbs use several base conventions:
                #   fxstack/...  -> fx-quant-stack/src/fxstack/...
                #   tests/..., docker/... -> fx-quant-stack/...
                #   docs/..., ops/... -> repo root; or relative to the file's dir.
                bases = [
                    REPO,
                    f.parent,
                    REPO / "fx-quant-stack",
                    REPO / "fx-quant-stack" / "src",
                ]
                if any((b / tok).exists() for b in bases):
                    continue
                broken.append((str(f.relative_to(REPO)), tok))
    return broken


def main() -> int:
    md_files = [REPO / "AGENTS.md"] + sorted((REPO / "docs" / "agents").glob("*.md"))
    yaml_path = REPO / "docs" / "agents" / "system-map.yaml"

    md_broken = audit_markdown(md_files)
    map_broken = audit_system_map(yaml_path)
    map_id_broken = audit_system_map_ids(yaml_path)
    crumb_broken = audit_agent_breadcrumbs()

    print("=== MARKDOWN LINK BROKEN REFS ===")
    for src, ref in md_broken:
        print(f"  {src}  ->  {ref}")
    print(f"  total: {len(md_broken)}")

    print("\n=== system-map.yaml BROKEN PATHS ===")
    for src, ref in map_broken:
        print(f"  {ref}")
    print(f"  total: {len(map_broken)}")

    print("\n=== system-map.yaml BROKEN SEMANTIC REFERENCES ===")
    for src, ref in map_id_broken:
        print(f"  {src}  ->  {ref}")
    print(f"  total: {len(map_id_broken)}")

    print("\n=== AGENT BREADCRUMB BROKEN REFS (path-like tokens that don't resolve) ===")
    # group by file
    by_file: dict[str, list[str]] = defaultdict(list)
    for src, ref in crumb_broken:
        by_file[src].append(ref)
    for src in sorted(by_file):
        print(f"  {src}:")
        for ref in sorted(set(by_file[src])):
            print(f"      {ref}")
    print(f"  total broken tokens: {len(crumb_broken)}  across {len(by_file)} files")

    print("\n=== SUMMARY ===")
    print(
        f"markdown_broken={len(md_broken)} "
        f"system_map_broken={len(map_broken)} "
        f"system_map_id_broken={len(map_id_broken)} "
        f"breadcrumb_broken={len(crumb_broken)}"
    )
    return 1 if md_broken or map_broken or map_id_broken or crumb_broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
