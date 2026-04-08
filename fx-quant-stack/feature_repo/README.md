Phase 2 Feast repository scaffold.

This repo keeps Feast configuration and the generated service manifest close to the
existing `fx-quant-stack` training and runtime code. Development uses the local
provider with file-backed offline storage and SQLite online serving. Shared
environments can override the online store to Redis without changing the Python
call sites.
