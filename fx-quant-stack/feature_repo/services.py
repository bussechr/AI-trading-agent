from __future__ import annotations

from definitions import FEATURE_SERVICES

for feature_service in FEATURE_SERVICES:
    globals()[str(getattr(feature_service, "name", ""))] = feature_service
