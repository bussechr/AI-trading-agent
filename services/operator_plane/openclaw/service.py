from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from fxstack.settings import get_settings
from services.operator_plane.common import ensure_within, read_json, repo_root, stable_hash, write_json


TaskFlowMode = Literal["managed", "mirrored"]
WorkspaceMode = Literal["staging_write"]


_FORBIDDEN_RESEARCH_PAYLOAD_KEYS = frozenset(
    {
        "artifact_dir",
        "capture_dir",
        "config_path",
        "database_url",
        "live_api_key",
        "out_dir",
        "replay_root",
        "research_root",
    }
)


@dataclass(frozen=True)
class SessionClassPolicy:
    name: str
    scope: str
    sandbox_required: bool
    workspace_mode: WorkspaceMode
    workspace_root: str
    scratch_root: str
    allow_workspace_writes: bool = False


@dataclass(frozen=True)
class FlowSpec:
    name: str
    title: str
    description: str
    session_name: str
    taskflow_mode: TaskFlowMode
    writes_workspace: bool


@dataclass(slots=True)
class OpenClawServiceConfig:
    enabled: bool
    sandbox_required: bool
    state_root: str
    staging_workspace_root: str
    release_root: str
    github_cli_enabled: bool
    sessions: dict[str, SessionClassPolicy]
    flows: dict[str, FlowSpec]


class OpenClawPermissionError(PermissionError):
    """Raised when a session attempts a disallowed flow."""


def _default_state_root() -> Path:
    return Path.home() / ".fxstack_operator_plane"


def default_config(
    *,
    enabled: bool | None = None,
    repo_root_path: Path | None = None,
    state_root: Path | None = None,
    staging_workspace_root: Path | None = None,
    release_root: Path | None = None,
    github_cli_enabled: bool = False,
) -> OpenClawServiceConfig:
    settings = get_settings()
    repo = (repo_root_path or repo_root()).resolve()
    state_dir = (state_root or _default_state_root()).resolve()
    staging_root = (staging_workspace_root or (repo.parent / f"{repo.name}-staging")).resolve()
    effective_release_root = (release_root or (staging_root / str(settings.phase5_release_root))).resolve()
    sessions = {
        "operator-write-staging": SessionClassPolicy(
            name="operator-write-staging",
            scope="operator.write",
            sandbox_required=True,
            workspace_mode="staging_write",
            workspace_root=str(staging_root),
            scratch_root=str((state_dir / "staging_session").resolve()),
            allow_workspace_writes=True,
        ),
    }
    flows = {
        "draft_experiment": FlowSpec(
            name="draft_experiment",
            title="Draft Experiment",
            description="Draft an operator-authored experiment proposal in the staging workspace.",
            session_name="operator-write-staging",
            taskflow_mode="managed",
            writes_workspace=True,
        ),
        "collect_approval_pack": FlowSpec(
            name="collect_approval_pack",
            title="Collect Approval Pack",
            description="Assemble approval evidence from an operator-authored staged proposal.",
            session_name="operator-write-staging",
            taskflow_mode="managed",
            writes_workspace=True,
        ),
        "open_pr": FlowSpec(
            name="open_pr",
            title="Open PR",
            description="Open or prepare a reviewed PR from the staging workspace.",
            session_name="operator-write-staging",
            taskflow_mode="mirrored",
            writes_workspace=True,
        ),
        "prepare_paper_pack": FlowSpec(
            name="prepare_paper_pack",
            title="Prepare Paper Pack",
            description="Build a paper-trading release bundle under the release root.",
            session_name="operator-write-staging",
            taskflow_mode="managed",
            writes_workspace=True,
        ),
    }
    return OpenClawServiceConfig(
        enabled=bool(settings.openclaw_enabled) if enabled is None else bool(enabled),
        sandbox_required=bool(settings.openclaw_sandbox_required),
        state_root=str(state_dir),
        staging_workspace_root=str(staging_root),
        release_root=str(effective_release_root),
        github_cli_enabled=bool(github_cli_enabled),
        sessions=sessions,
        flows=flows,
    )


class OpenClawSupervisor:
    def __init__(self, *, config: OpenClawServiceConfig | None = None) -> None:
        self.config = config or default_config()
        self.state_root = Path(self.config.state_root).resolve()
        self.staging_workspace_root = Path(self.config.staging_workspace_root).resolve()
        if self.config.enabled and not self.config.sandbox_required:
            raise OpenClawPermissionError("OpenClaw requires sandbox_required=true")
        if self.config.enabled:
            self.release_root = ensure_within(
                self.staging_workspace_root,
                Path(self.config.release_root),
            )
            for policy in self.config.sessions.values():
                ensure_within(self.staging_workspace_root, Path(policy.workspace_root))
            self.state_root.mkdir(parents=True, exist_ok=True)
        else:
            self.release_root = Path(self.config.release_root).resolve()
        self._registry_path = self.state_root / "taskflow_runs.json"

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.config.enabled),
            "sandbox_required": bool(self.config.sandbox_required),
            "state_root": str(self.state_root),
            "staging_workspace_root": str(self.staging_workspace_root),
            "release_root": str(self.release_root),
            "sessions": {name: asdict(policy) for name, policy in self.config.sessions.items()},
            "flows": {name: asdict(flow) for name, flow in self.config.flows.items()},
        }

    def _load_registry(self) -> dict[str, Any]:
        return read_json(self._registry_path)

    def _save_registry(self, payload: dict[str, Any]) -> None:
        write_json(self._registry_path, payload)

    def _compute_idempotency_key(
        self,
        *,
        flow_name: str,
        experiment_id: str,
        window: str,
        revision: str,
        payload: dict[str, Any],
    ) -> str:
        return stable_hash(
            {
                "flow_name": flow_name,
                "experiment_id": experiment_id,
                "window": window,
                "revision": revision,
                "payload": payload,
            }
        )

    def _session_policy(self, session_name: str) -> SessionClassPolicy:
        policy = self.config.sessions.get(session_name)
        if policy is None:
            raise OpenClawPermissionError(f"unknown session: {session_name}")
        if self.config.sandbox_required and not bool(policy.sandbox_required):
            raise OpenClawPermissionError(f"sandbox disabled for session: {session_name}")
        return policy

    def _flow_spec(self, flow_name: str) -> FlowSpec:
        spec = self.config.flows.get(flow_name)
        if spec is None:
            raise KeyError(f"unknown flow: {flow_name}")
        return spec

    def _require_session(self, flow: FlowSpec, session: SessionClassPolicy) -> None:
        if str(flow.session_name) != str(session.name):
            raise OpenClawPermissionError(
                f"flow {flow.name} requires {flow.session_name}, received {session.name}"
            )
        if flow.writes_workspace and not bool(session.allow_workspace_writes):
            raise OpenClawPermissionError(f"session {session.name} cannot run write flow {flow.name}")

    def _scratch_root(self, session: SessionClassPolicy) -> Path:
        root = Path(session.scratch_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _workspace_root(self, session: SessionClassPolicy) -> Path:
        root = Path(session.workspace_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _reject_research_payload(payload: dict[str, Any]) -> None:
        blocked = sorted(
            key
            for key in _FORBIDDEN_RESEARCH_PAYLOAD_KEYS
            if payload.get(key) not in (None, "")
        )
        if blocked:
            raise OpenClawPermissionError(
                "offline research inputs are outside the operator-plane trust boundary: " + ", ".join(blocked)
            )

    def _read_staged_json(self, path: Path) -> dict[str, Any]:
        safe_path = ensure_within(self.staging_workspace_root, path)
        if not safe_path.is_file():
            raise FileNotFoundError(f"required staged artifact is missing: {safe_path}")
        return read_json(safe_path)

    def _draft_dir(self, *, experiment_id: str, window: str, revision: str) -> Path:
        return self.staging_workspace_root / "artifacts" / "operator_plane" / "drafts" / experiment_id / window / revision

    def _approval_dir(self, *, experiment_id: str, window: str, revision: str) -> Path:
        return self.staging_workspace_root / "artifacts" / "operator_plane" / "approval_packs" / experiment_id / window / revision

    def _pr_request_dir(self, *, experiment_id: str, window: str, revision: str) -> Path:
        return self.staging_workspace_root / "artifacts" / "operator_plane" / "pr_requests" / experiment_id / window / revision

    def _paper_pack_dir(self, *, experiment_id: str, window: str, revision: str) -> Path:
        return self.release_root / experiment_id / window / revision

    def _handle_draft_experiment(
        self,
        *,
        payload: dict[str, Any],
        experiment_id: str,
        window: str,
        revision: str,
    ) -> tuple[str, dict[str, Any], list[str]]:
        draft_dir = self._draft_dir(experiment_id=experiment_id, window=window, revision=revision)
        ensure_within(self.staging_workspace_root, draft_dir)
        summary = payload.get("summary")
        requested_checks = payload.get("requested_checks")
        proposal = {
            "experiment_id": experiment_id,
            "window": window,
            "revision": revision,
            "input_mode": "operator_authored",
            "hypothesis": str(payload.get("hypothesis") or ""),
            "summary": dict(summary) if isinstance(summary, dict) else {},
            "requested_checks": [str(item) for item in requested_checks]
            if isinstance(requested_checks, (list, tuple))
            else [],
            "created_at": float(time.time()),
        }
        proposal_path = write_json(draft_dir / "experiment_proposal.json", proposal)
        notes_path = draft_dir / "experiment_notes.md"
        notes_path.parent.mkdir(parents=True, exist_ok=True)
        notes_path.write_text(
            "\n".join(
                [
                    f"# Experiment Draft `{experiment_id}`",
                    "",
                    f"- Window: `{window}`",
                    f"- Revision: `{revision}`",
                    "- Input: `operator-authored staging payload`",
                    f"- Hypothesis: {proposal['hypothesis']}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return "completed", proposal, [str(proposal_path), str(notes_path)]

    def _handle_collect_approval_pack(
        self,
        *,
        payload: dict[str, Any],
        experiment_id: str,
        window: str,
        revision: str,
    ) -> tuple[str, dict[str, Any], list[str]]:
        approval_dir = self._approval_dir(experiment_id=experiment_id, window=window, revision=revision)
        ensure_within(self.staging_workspace_root, approval_dir)
        proposal_path = self._draft_dir(
            experiment_id=experiment_id,
            window=window,
            revision=revision,
        ) / "experiment_proposal.json"
        proposal = self._read_staged_json(proposal_path)
        review = payload.get("review")
        approval = {
            "experiment_id": experiment_id,
            "window": window,
            "revision": revision,
            "proposal_path": str(proposal_path),
            "proposal_digest": stable_hash(proposal),
            "decision": str(payload.get("decision") or "pending"),
            "reviewer": str(payload.get("reviewer") or ""),
            "review": dict(review) if isinstance(review, dict) else {},
            "created_at": float(time.time()),
        }
        approval_json = write_json(approval_dir / "approval_pack.json", approval)
        approval_md = approval_dir / "approval_pack.md"
        approval_md.parent.mkdir(parents=True, exist_ok=True)
        approval_md.write_text(
            "\n".join(
                [
                    f"# Approval Pack `{experiment_id}`",
                    "",
                    f"- Window: `{window}`",
                    f"- Revision: `{revision}`",
                    f"- Proposal: `{approval['proposal_path']}`",
                    f"- Decision: `{approval['decision']}`",
                    f"- Reviewer: `{approval['reviewer']}`",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return "completed", approval, [str(approval_json), str(approval_md)]

    def _handle_open_pr(
        self,
        *,
        payload: dict[str, Any],
        experiment_id: str,
        window: str,
        revision: str,
        execute: bool,
    ) -> tuple[str, dict[str, Any], list[str]]:
        request_dir = self._pr_request_dir(experiment_id=experiment_id, window=window, revision=revision)
        ensure_within(self.staging_workspace_root, request_dir)
        raw_body_path = str(payload.get("body_path") or "").strip()
        body_path = ""
        if raw_body_path:
            candidate = Path(raw_body_path)
            if not candidate.is_absolute():
                candidate = self.staging_workspace_root / candidate
            body_path = str(ensure_within(self.staging_workspace_root, candidate))
        request = {
            "experiment_id": experiment_id,
            "window": window,
            "revision": revision,
            "branch": str(payload.get("branch") or f"operator/{experiment_id}/{window}/{revision}"),
            "title": str(payload.get("title") or f"Operator review for {experiment_id} {window}"),
            "body_path": body_path,
            "created_at": float(time.time()),
        }
        request_path = write_json(request_dir / "open_pr_request.json", request)
        result = {"request": request, "request_path": str(request_path)}
        status = "prepared"
        if execute and bool(self.config.github_cli_enabled):
            command = [
                "gh",
                "pr",
                "create",
                "--title",
                request["title"],
                "--head",
                request["branch"],
                "--draft",
            ]
            if str(request.get("body_path") or "").strip():
                command.extend(["--body-file", str(request["body_path"])])
            completed = subprocess.run(
                command,
                cwd=self.staging_workspace_root,
                capture_output=True,
                text=True,
                check=False,
            )
            result["command"] = command
            result["returncode"] = int(completed.returncode)
            result["stdout"] = str(completed.stdout)
            result["stderr"] = str(completed.stderr)
            status = "completed" if int(completed.returncode) == 0 else "failed"
        return status, result, [str(request_path)]

    def _handle_prepare_paper_pack(
        self,
        *,
        payload: dict[str, Any],
        experiment_id: str,
        window: str,
        revision: str,
    ) -> tuple[str, dict[str, Any], list[str]]:
        pack_dir = ensure_within(
            self.release_root,
            self._paper_pack_dir(
                experiment_id=experiment_id,
                window=window,
                revision=revision,
            ),
        )
        pack_dir.mkdir(parents=True, exist_ok=True)
        approval_path = self._approval_dir(experiment_id=experiment_id, window=window, revision=revision) / "approval_pack.json"
        approval = self._read_staged_json(approval_path)
        pack = {
            "experiment_id": experiment_id,
            "window": window,
            "revision": revision,
            "approval_pack_path": str(approval_path),
            "approval_digest": stable_hash(approval),
            "decision": str(approval.get("decision") or "pending"),
            "paper_only": True,
            "created_at": float(time.time()),
        }
        pack_json = write_json(pack_dir / "paper_pack.json", pack)
        pack_md = pack_dir / "release_notes.md"
        pack_md.write_text(
            "\n".join(
                [
                    f"# Paper Pack `{experiment_id}`",
                    "",
                    f"- Window: `{window}`",
                    f"- Revision: `{revision}`",
                    "- Target: `paper`",
                    f"- Approval Pack: `{pack['approval_pack_path']}`",
                    f"- Decision: `{pack['decision']}`",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return "completed", pack, [str(pack_json), str(pack_md)]

    def start_flow(
        self,
        flow_name: str,
        *,
        session_name: str,
        experiment_id: str = "",
        window: str = "",
        revision: str = "r1",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        execute: bool = True,
    ) -> dict[str, Any]:
        if not bool(self.config.enabled):
            return {
                "flow_name": flow_name,
                "status": "disabled",
                "reason": "openclaw_disabled",
            }
        flow = self._flow_spec(flow_name)
        session = self._session_policy(session_name)
        self._require_session(flow, session)
        payload_dict = dict(payload or {})
        self._reject_research_payload(payload_dict)
        key = str(idempotency_key or self._compute_idempotency_key(
            flow_name=flow.name,
            experiment_id=str(experiment_id),
            window=str(window),
            revision=str(revision),
            payload=payload_dict,
        ))
        registry = self._load_registry()
        existing = dict((registry.get("runs") or {}).get(key) or {})
        if existing:
            return {**existing, "status": "duplicate", "duplicate": True}

        if flow.name == "draft_experiment":
            status, result, artifact_refs = self._handle_draft_experiment(
                payload=payload_dict,
                experiment_id=str(experiment_id),
                window=str(window),
                revision=str(revision),
            )
        elif flow.name == "collect_approval_pack":
            status, result, artifact_refs = self._handle_collect_approval_pack(
                payload=payload_dict,
                experiment_id=str(experiment_id),
                window=str(window),
                revision=str(revision),
            )
        elif flow.name == "open_pr":
            status, result, artifact_refs = self._handle_open_pr(
                payload=payload_dict,
                experiment_id=str(experiment_id),
                window=str(window),
                revision=str(revision),
                execute=bool(execute),
            )
        elif flow.name == "prepare_paper_pack":
            status, result, artifact_refs = self._handle_prepare_paper_pack(
                payload=payload_dict,
                experiment_id=str(experiment_id),
                window=str(window),
                revision=str(revision),
            )
        else:
            raise KeyError(f"unsupported flow: {flow.name}")

        run_id = f"taskflow-{key[:12]}"
        record = {
            "run_id": run_id,
            "flow_name": flow.name,
            "taskflow_mode": flow.taskflow_mode,
            "session_name": session.name,
            "scope": session.scope,
            "sandbox_required": bool(session.sandbox_required),
            "workspace_mode": session.workspace_mode,
            "workspace_root": str(self._workspace_root(session)),
            "scratch_root": str(self._scratch_root(session)),
            "idempotency_key": key,
            "experiment_id": str(experiment_id),
            "window": str(window),
            "revision": str(revision),
            "status": status,
            "result": result,
            "artifact_refs": list(artifact_refs),
            "created_at": float(time.time()),
            "duplicate": False,
        }
        registry.setdefault("runs", {})[key] = record
        self._save_registry(registry)
        return record


def _parse_payload(raw: str) -> dict[str, Any]:
    txt = str(raw).strip()
    if not txt:
        return {}
    return dict(json.loads(txt) or {})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or describe the OpenClaw supervisory service.")
    parser.add_argument("--describe", action="store_true")
    parser.add_argument("--flow", default="")
    parser.add_argument("--session", default="operator-write-staging")
    parser.add_argument("--experiment-id", default="")
    parser.add_argument("--window", default="")
    parser.add_argument("--revision", default="r1")
    parser.add_argument("--payload-json", default="")
    parser.add_argument("--no-execute", action="store_true")
    args = parser.parse_args(argv)

    supervisor = OpenClawSupervisor()
    if args.describe:
        print(json.dumps(supervisor.describe(), indent=2, sort_keys=True))
        return 0
    if not str(args.flow).strip():
        parser.error("--flow is required unless --describe is used")
    result = supervisor.start_flow(
        str(args.flow),
        session_name=str(args.session),
        experiment_id=str(args.experiment_id),
        window=str(args.window),
        revision=str(args.revision),
        payload=_parse_payload(str(args.payload_json)),
        execute=not bool(args.no_execute),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 2 if result.get("status") == "disabled" else 0


if __name__ == "__main__":
    raise SystemExit(main())
