"""Ad-hoc inspector: what are the orchestration/agent tables actually recording?

Read-only. Connects to the live Postgres bridge DB and, for each agent-related
table, prints the row count and the most-recent row (as JSON), picking a
timestamp/serial column automatically so we don't have to hard-code schemas.
"""

from __future__ import annotations

import json
import os

from sqlalchemy import create_engine, text

URL = os.environ.get("FXSTACK_DATABASE_URL", "postgresql+psycopg://fx:fx@localhost:5432/fxstack")

TABLES = [
    "orchestration_runs",
    "agent_traces",
    "agent_proposals",
    "governed_decisions",
    "decision_snapshots",
    "governance_events",
    "commands",
    "command_events",
    "reports",
]

ORDER_PREFS = ["created_at", "ts", "updated_at", "decided_at", "event_ts", "id"]


def _cols(conn, table: str) -> list[str]:
    rows = conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name=:t ORDER BY ordinal_position"
        ),
        {"t": table},
    ).fetchall()
    return [r[0] for r in rows]


def main() -> None:
    eng = create_engine(URL)
    with eng.connect() as conn:
        for table in TABLES:
            cols = _cols(conn, table)
            if not cols:
                print(f"\n### {table}: (no such table)")
                continue
            order_col = next((c for c in ORDER_PREFS if c in cols), cols[0])
            try:
                n = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
            except Exception as exc:  # noqa: BLE001
                print(f"\n### {table}: count failed: {exc}")
                continue
            print(f"\n### {table}: {n} rows (order by {order_col})")
            if not n:
                continue
            try:
                row = conn.execute(
                    text(f"SELECT to_jsonb(t) FROM {table} t ORDER BY {order_col} DESC LIMIT 1")
                ).scalar()
                obj = row if isinstance(row, dict) else json.loads(row)
                # Trim very long values for readability.
                trimmed = {}
                for k, v in obj.items():
                    s = json.dumps(v) if not isinstance(v, str) else v
                    trimmed[k] = (s[:400] + "…") if len(s) > 400 else v
                print(json.dumps(trimmed, indent=2, default=str)[:2400])
            except Exception as exc:  # noqa: BLE001
                print(f"  latest-row fetch failed: {exc}")


if __name__ == "__main__":
    main()
