from __future__ import annotations

from definitions import FEATURE_VIEWS, PUSH_SOURCES

for push_source in PUSH_SOURCES:
    globals()[str(getattr(push_source, "name", ""))] = push_source

for feature_view in FEATURE_VIEWS:
    globals()[str(getattr(feature_view, "name", ""))] = feature_view
