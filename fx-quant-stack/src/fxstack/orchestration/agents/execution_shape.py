"""Execution-shape helper used inside aggregate/govern nodes."""

from __future__ import annotations

from typing import Any

from fxstack.orchestration.contracts import AgentProposal


class ExecutionShapeAgent:
    def shape(
        self,
        *,
        baseline_action: dict[str, Any],
        winning_proposal: AgentProposal | None,
        selected_action: str,
        blocking_reasons: list[str] | None,
        fault_classification: str | None,
    ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
        reasons = [str(item) for item in list(blocking_reasons or []) if str(item).strip()]
        if fault_classification:
            reasons = list(dict.fromkeys([*reasons, str(fault_classification)]))

        preview = dict(baseline_action.get("command_preview") or {})
        side = str(baseline_action.get("side") or "FLAT")
        confidence = 0.0
        if winning_proposal is not None:
            side = str(winning_proposal.side or side)
            confidence = float(winning_proposal.confidence)
            proposal_preview = dict(winning_proposal.constraints or {}).get("command_preview") or {}
            if isinstance(proposal_preview, dict) and proposal_preview:
                preview = {**preview, **proposal_preview}

        action = str(selected_action or "hold").strip().lower()
        if action == "enter":
            normalized_side = side if side in {"BUY", "SELL"} else str(preview.get("cmd") or "FLAT").upper()
            preview = dict(preview or {})
            if normalized_side in {"BUY", "SELL"}:
                preview.setdefault("cmd", normalized_side)
                preview.setdefault("symbol", str(baseline_action.get("symbol") or baseline_action.get("pair") or ""))
            return (
                {
                    "action": "enter",
                    "intent": "enter",
                    "side": normalized_side if normalized_side in {"BUY", "SELL"} else "FLAT",
                    "confidence": float(confidence),
                    "advisory_only": True,
                },
                preview,
                reasons,
            )
        if action in {"exit", "reduce"}:
            preview = dict(preview or {})
            if action == "exit":
                preview.pop("lots", None)
            return (
                {
                    "action": action,
                    "intent": action,
                    "side": "FLAT" if action == "exit" else (side if side in {"BUY", "SELL"} else "FLAT"),
                    "confidence": float(confidence),
                    "advisory_only": True,
                },
                preview,
                reasons,
            )
        if action == "no_trade":
            return (
                {
                    "action": "no_trade",
                    "intent": "no_trade",
                    "side": "FLAT",
                    "confidence": float(confidence),
                    "advisory_only": True,
                },
                {},
                reasons,
            )
        return (
            {
                "action": "hold",
                "intent": "hold",
                "side": side if side in {"BUY", "SELL"} else "FLAT",
                "confidence": float(confidence),
                "advisory_only": True,
            },
            {},
            reasons,
        )
