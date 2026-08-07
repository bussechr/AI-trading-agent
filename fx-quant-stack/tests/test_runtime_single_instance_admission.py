from __future__ import annotations

import pytest

from fxstack.runtime.feature_push_worker import (
    _require_baseline_instance_id as require_worker_baseline,
)
from fxstack.runtime.runner import _require_baseline_instance_id as require_runtime_baseline


@pytest.mark.parametrize(
    "require_baseline",
    [require_runtime_baseline, require_worker_baseline],
)
def test_production_python_entrypoints_accept_literal_baseline_only(require_baseline) -> None:
    assert require_baseline("baseline") == "baseline"

    for rejected in ("candidate", "BASELINE", " baseline", "baseline ", "", None):
        with pytest.raises(SystemExit, match="external isolated host or VM"):
            require_baseline(rejected)
