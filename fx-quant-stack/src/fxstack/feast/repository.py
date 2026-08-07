from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fxstack.feast.types import FeatureServiceRef
from fxstack.features.session_contract import feature_contract_metadata
from fxstack.settings import get_settings
from fxstack.utils.hashing import hash_mapping


@dataclass(slots=True)
class FeatureViewSpec:
    name: str
    timeframe: str
    kind: str
    description: str
    prefixes: list[str] = field(default_factory=list)
    online: bool = False
    include_columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelArtifactSpec:
    pair: str
    component_key: str
    timeframe: str
    features: tuple[str, ...]
    artifact_path: Path
    service_name: str


@dataclass(slots=True)
class FeatureServiceSpec:
    name: str
    features: tuple[str, ...]
    pair: str
    timeframe: str
    component_key: str
    feature_views: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "features": list(self.features),
            "pair": self.pair,
            "timeframe": self.timeframe,
            "component_key": self.component_key,
            "feature_views": list(self.feature_views),
        }


@dataclass(slots=True)
class FeatureRepoDefinition:
    root: Path
    views: list[FeatureViewSpec]
    services: list[FeatureServiceSpec]
    project: str = "fxstack"
    provider: str = "local"

    def write(self) -> dict[str, Path]:
        root = Path(self.root)
        root.mkdir(parents=True, exist_ok=True)
        views_dir = root / "views"
        services_dir = root / "services"
        views_dir.mkdir(parents=True, exist_ok=True)
        services_dir.mkdir(parents=True, exist_ok=True)
        entity_path = root / "entities.py"
        feature_store_path = root / "feature_store.yaml"

        entity_path.write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "",
                    "try:",
                    "    from feast import Entity  # type: ignore",
                    "    from feast.types import String  # type: ignore",
                    "except Exception:",
                    "    Entity = None  # type: ignore",
                    "    String = None  # type: ignore",
                    "",
                    'pair = Entity(name="pair", join_keys=["pair"], value_type=String) if Entity is not None else None',
                    "",
                ]
            ),
            encoding="utf-8",
        )

        feature_store_path.write_text(
            "\n".join(
                [
                    f"project: {self.project}",
                    "registry: data/registry.db",
                    f"provider: {self.provider}",
                    "",
                    "offline_store:",
                    "  type: file",
                    "",
                    "online_store:",
                    "  type: sqlite",
                    "  path: data/online.db",
                    "",
                    "feature_views:",
                    *[f"  - {view.name}" for view in self.views],
                    "feature_services:",
                    *[f"  - {service.name}" for service in self.services],
                    "",
                ]
            ),
            encoding="utf-8",
        )

        for view in self.views:
            (views_dir / f"{view.name}.json").write_text(json.dumps(view.to_dict(), indent=2), encoding="utf-8")
        for service in self.services:
            (services_dir / f"{service.name.replace('.', '_')}.json").write_text(
                json.dumps(service.to_dict(), indent=2),
                encoding="utf-8",
            )
        return {
            "root": root,
            "feature_store": feature_store_path,
            "entity": entity_path,
            "views_dir": views_dir,
            "services_dir": services_dir,
        }


_COMPONENT_TIMEFRAME = {
    "regime_hmm": "H4",
    "regime": "H4",
    "swing_xgb": "D",
    "swing_transformer": "D",
    "swing_patchtst": "D",
    "intraday_xgb": "M5",
    "intraday_tcn": "M5",
    "intraday_patchtst": "M5",
    "meta_filter": "M5",
    "meta": "M5",
    "exit_policy_xgb": "M5",
    "exit_policy": "M5",
    "reversal_failure_xgb": "M5",
    "reversal_failure": "M5",
    "reversal_opportunity_xgb": "M5",
    "reversal_opportunity": "M5",
    "directional_belief": "M5",
    "cross_pair_intelligence": "M5",
}


def feature_repo_root() -> Path:
    s = get_settings()
    return Path(getattr(s, "feast_repo_root", s.project_root / "feature_repo"))


def feature_repo_manifest_path() -> Path:
    return feature_repo_root() / "services_manifest.json"


def _dot_service_name(*, pair: str, component_key: str, timeframe: str | None = None) -> str:
    raw_tf = str(timeframe or "").strip()
    tf = raw_tf.upper() if raw_tf else "na"
    if tf == "NA":
        tf = "na"
    return f"fx.{str(component_key).strip()}.{str(pair).upper()}.{tf}"


def _ordered_unique(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for value in list(values or []):
        txt = str(value).strip()
        if not txt or txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    return tuple(out)


def default_feature_views() -> list[FeatureViewSpec]:
    return [
        FeatureViewSpec("anchor_m5", "M5", "anchor", "Primary M5 lifecycle and contract features."),
        FeatureViewSpec("anchor_h4", "H4", "anchor", "Primary H4 lifecycle features."),
        FeatureViewSpec("anchor_d", "D", "anchor", "Primary D lifecycle features."),
        FeatureViewSpec("context_m15", "M15", "context", "M15 context aligned to the anchor timeframe.", prefixes=["m15_"]),
        FeatureViewSpec("context_h1", "H1", "context", "H1 context aligned to the anchor timeframe.", prefixes=["h1_"]),
        FeatureViewSpec("context_h4", "H4", "context", "H4 context aligned to the anchor timeframe.", prefixes=["h4_"]),
        FeatureViewSpec("context_d", "D", "context", "D context aligned to the anchor timeframe.", prefixes=["d_"]),
        FeatureViewSpec(
            "cross_pair_context",
            "M5",
            "cross_pair",
            "Cross-pair basket and dispersion context used by intraday and belief stacks.",
            prefixes=["usd_strength_", "cross_pair_"],
            include_columns=["cross_pair_dispersion"],
        ),
        FeatureViewSpec(
            "live_diagnostics",
            "M5",
            "diagnostics",
            "Freshness, spread, and runtime-only diagnostics for live serving and parity checks.",
            prefixes=["feature_serving_", "runtime_", "tick_", "heartbeat_"],
            include_columns=["spread_bps", "spread", "transport_mode", "ticks_fresh"],
            online=True,
        ),
        FeatureViewSpec(
            "cross_pair_intelligence",
            "M5",
            "cross_pair",
            "Global cross-pair influence inputs for directional-belief ranking and runtime cross-pair intelligence.",
            prefixes=["usd_strength_", "cross_pair_", "belief_"],
            include_columns=["usd_strength_basket_ret_1", "cross_pair_dispersion"],
            online=True,
        ),
    ]


def load_model_artifact_specs(artifacts_root: Path | str) -> list[ModelArtifactSpec]:
    root = Path(artifacts_root)
    if not root.exists():
        return []
    specs: list[ModelArtifactSpec] = []
    for meta_path in sorted(root.glob("*/*/meta.json")):
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        pair = str(payload.get("pair") or meta_path.parents[1].name).upper()
        component_key = str(payload.get("model_family") or payload.get("component_key") or meta_path.parent.name).strip()
        timeframe = str(payload.get("timeframe") or "na").upper()
        features = _ordered_unique(list(payload.get("feature_columns") or []))
        specs.append(
            ModelArtifactSpec(
                pair=pair,
                component_key=component_key,
                timeframe=timeframe if timeframe else "NA",
                features=features,
                artifact_path=meta_path.parent,
                service_name=_dot_service_name(pair=pair, component_key=component_key, timeframe=timeframe if timeframe else "NA"),
            )
        )
    return specs


def derive_feature_services_from_artifacts(specs: list[ModelArtifactSpec]) -> list[FeatureServiceSpec]:
    by_name: dict[str, FeatureServiceSpec] = {}
    for spec in specs:
        current = by_name.get(spec.service_name)
        features = _ordered_unique(list(spec.features) + list(current.features if current is not None else ()))
        by_name[spec.service_name] = FeatureServiceSpec(
            name=spec.service_name,
            features=features,
            pair=spec.pair,
            timeframe=spec.timeframe,
            component_key=spec.component_key,
            feature_views=tuple(feature_views_for_component(spec.component_key)),
        )
    return [by_name[name] for name in sorted(by_name)]


def default_feature_repo(root: Path | str) -> FeatureRepoDefinition:
    sample_specs = [
        ModelArtifactSpec(
            pair="EURUSD",
            component_key="regime_hmm",
            timeframe="H4",
            features=("ret_1", "ret_5", "vol_20"),
            artifact_path=Path(root),
            service_name=_dot_service_name(pair="EURUSD", component_key="regime_hmm", timeframe="H4"),
        ),
        ModelArtifactSpec(
            pair="EURUSD",
            component_key="swing_xgb",
            timeframe="D",
            features=("ret_1", "trend_strength_20"),
            artifact_path=Path(root),
            service_name=_dot_service_name(pair="EURUSD", component_key="swing_xgb", timeframe="D"),
        ),
        ModelArtifactSpec(
            pair="EURUSD",
            component_key="intraday_xgb",
            timeframe="M5",
            features=("ret_1", "m15_ret_1", "cross_pair_dispersion"),
            artifact_path=Path(root),
            service_name=_dot_service_name(pair="EURUSD", component_key="intraday_xgb", timeframe="M5"),
        ),
        ModelArtifactSpec(
            pair="GLOBAL",
            component_key="directional_belief",
            timeframe="M5",
            features=("ret_1", "scenario_score"),
            artifact_path=Path(root),
            service_name=_dot_service_name(pair="GLOBAL", component_key="directional_belief", timeframe="M5"),
        ),
        ModelArtifactSpec(
            pair="GLOBAL",
            component_key="cross_pair_intelligence",
            timeframe="M5",
            features=("usd_strength_basket_ret_1", "cross_pair_dispersion", "belief_primary_score", "belief_primary_rank_score", "belief_gap"),
            artifact_path=Path(root),
            service_name=_dot_service_name(pair="GLOBAL", component_key="cross_pair_intelligence", timeframe="M5"),
        ),
    ]
    return FeatureRepoDefinition(
        root=Path(root),
        views=default_feature_views(),
        services=derive_feature_services_from_artifacts(sample_specs),
    )


def component_default_timeframe(component_key: str, *, fallback: str = "M5") -> str:
    return str(_COMPONENT_TIMEFRAME.get(str(component_key).strip().lower(), fallback)).upper()


def feature_views_for_component(component_key: str) -> list[str]:
    key = str(component_key).strip().lower()
    if key in {"regime_hmm", "regime"}:
        return ["anchor_h4"]
    if key in {"swing_xgb", "swing_transformer", "swing_patchtst"}:
        return ["anchor_d"]
    if key in {"directional_belief"}:
        return [
            "anchor_m5",
            "context_m15",
            "context_h1",
            "context_h4",
            "context_d",
            "cross_pair_context",
        ]
    if key in {"cross_pair_intelligence"}:
        return ["cross_pair_intelligence"]
    if key in {
        "intraday_xgb",
        "intraday_tcn",
        "intraday_patchtst",
        "meta_filter",
        "meta",
        "exit_policy_xgb",
        "exit_policy",
        "reversal_failure_xgb",
        "reversal_failure",
        "reversal_opportunity_xgb",
        "reversal_opportunity",
    }:
        return [
            "anchor_m5",
            "context_m15",
            "context_h1",
            "context_h4",
            "context_d",
            "cross_pair_context",
        ]
    return ["anchor_m5"]


def default_feature_service_name(*, pair: str, component_key: str, timeframe: str | None = None) -> str:
    key = str(component_key).strip().lower()
    tf = str(timeframe or component_default_timeframe(key)).upper()
    return f"fx_{str(pair).lower()}_{key}_{tf.lower()}"


def build_feature_service_ref(
    *,
    pair: str,
    component_key: str,
    feature_columns: list[str] | None = None,
    timeframe: str | None = None,
) -> FeatureServiceRef:
    tf = str(timeframe or component_default_timeframe(component_key)).upper()
    columns = [str(col) for col in list(feature_columns or []) if str(col).strip()]
    view_names = feature_views_for_component(component_key)
    service_name = default_feature_service_name(pair=pair, component_key=component_key, timeframe=tf)
    contract_payload = {
        "pair": str(pair).upper(),
        "timeframe": tf,
        "component_key": str(component_key),
        "feature_columns": columns,
        "feature_view_names": view_names,
        **feature_contract_metadata(),
    }
    contract_hash = hash_mapping(contract_payload)
    return FeatureServiceRef(
        name=service_name,
        version=contract_hash[:16],
        pair=str(pair).upper(),
        timeframe=tf,
        component_key=str(component_key),
        feature_contract_hash=contract_hash,
        feature_columns=columns,
        feature_view_names=view_names,
    )


def feature_repo_manifest(*, pair: str, component_columns: dict[str, list[str]]) -> dict[str, Any]:
    services = {
        key: build_feature_service_ref(pair=pair, component_key=key, feature_columns=cols).to_dict()
        for key, cols in sorted(component_columns.items())
    }
    return {
        "pair": str(pair).upper(),
        "views": [spec.to_dict() for spec in default_feature_views()],
        "services": services,
    }


def artifact_feature_service_ref(
    *,
    pair: str,
    component_key: str,
    artifact_meta: dict[str, Any] | None,
    timeframe: str | None = None,
) -> FeatureServiceRef:
    meta = dict(artifact_meta or {})
    return build_feature_service_ref(
        pair=pair,
        component_key=component_key,
        feature_columns=list(meta.get("feature_columns") or []),
        timeframe=str(meta.get("timeframe") or timeframe or component_default_timeframe(component_key)).upper(),
    )
