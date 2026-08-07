"""Generate an Excalidraw diagram of the live trading-agent flow.

Regenerate with:

    python tools/make_flow_diagram.py

Output: docs/diagrams/current-flow.excalidraw  (open at excalidraw.com, or with
the VS Code Excalidraw extension)

The layout snakes left-to-right then right-to-left so that every band connects to
the next with a short arrow instead of a long return line. Live-cycle boxes are
numbered 1..15 so the reading order is unambiguous regardless of direction.

Colour is meaning, not decoration:

    blue    offline research (build host)
    yellow  a gate -- something is allowed to say "no" here
    grey    computes but does not bind (diagnostic / inert)
    orange  actually decides
    green   binding runtime check
    violet  broker-facing execution
    red     known gap / improvement target
"""

from __future__ import annotations

import json
import os
import textwrap

# ----------------------------------------------------------------- palette

BLUE = ("#d0ebff", "#1971c2")
YELLOW = ("#ffec99", "#f08c00")
GREY = ("#e9ecef", "#868e96")
ORANGE = ("#ffd8a8", "#e8590c")
GREEN = ("#b2f2bb", "#2f9e44")
VIOLET = ("#eebefa", "#9c36b5")
RED = ("#ffc9c9", "#e03131")

# ----------------------------------------------------------------- geometry

BOX_W, BOX_H = 236, 126
PITCH_X = 262
ROW_PITCH_Y = 216
X0, Y0 = 70, 150
NCOLS = 6

_seq = [0]


def _nid(prefix: str) -> str:
    _seq[0] += 1
    return f"{prefix}{_seq[0]:04d}"


def _base(el_type: str, x: float, y: float, w: float, h: float, **kw) -> dict:
    _seq[0] += 1
    el: dict = {
        "id": kw.pop("id", None) or _nid("el"),
        "type": el_type,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "angle": 0,
        "strokeColor": "#1e1e1e",
        "backgroundColor": "transparent",
        "fillStyle": "solid",
        "strokeWidth": 2,
        "strokeStyle": "solid",
        "roughness": 1,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": None,
        "seed": 100000 + _seq[0] * 7919,
        "version": 1,
        "versionNonce": 200000 + _seq[0] * 6271,
        "isDeleted": False,
        "boundElements": [],
        "updated": 1,
        "link": None,
        "locked": False,
    }
    el.update(kw)
    return el


def text(
    x: float,
    y: float,
    body: str,
    *,
    size: int = 12,
    color: str = "#1e1e1e",
    width: float | None = None,
    align: str = "left",
    group: str | None = None,
) -> dict:
    lines = body.split("\n")
    w = width if width is not None else max(len(ln) for ln in lines) * size * 0.55
    h = len(lines) * size * 1.25
    el = _base(
        "text",
        x,
        y,
        w,
        h,
        strokeColor=color,
        fontSize=size,
        fontFamily=1,
        text=body,
        textAlign=align,
        verticalAlign="top",
        containerId=None,
        originalText=body,
        lineHeight=1.25,
        autoResize=True,
    )
    if group:
        el["groupIds"] = [group]
    return el


def box(
    x: float,
    y: float,
    title: str,
    body: str,
    palette: tuple[str, str],
    *,
    w: float = BOX_W,
    h: float = BOX_H,
    dashed: bool = False,
) -> tuple[str, list[dict]]:
    """A titled card. Returns (rect_id, elements)."""
    bg, stroke = palette
    group = _nid("grp")
    rect_id = _nid("box")
    rect = _base(
        "rectangle",
        x,
        y,
        w,
        h,
        id=rect_id,
        backgroundColor=bg,
        strokeColor=stroke,
        roundness={"type": 3},
        strokeStyle="dashed" if dashed else "solid",
        groupIds=[group],
    )
    wrapped = "\n".join(
        ln for raw in body.split("\n") for ln in (textwrap.wrap(raw, 34) or [""])
    )
    els = [
        rect,
        text(x + 13, y + 11, title, size=15, color=stroke, width=w - 26, group=group),
        text(x + 13, y + 38, wrapped, size=11, color="#343a40", width=w - 26, group=group),
    ]
    return rect_id, els


def arrow(
    pts: list[tuple[float, float]],
    *,
    start: str | None = None,
    end: str | None = None,
    color: str = "#495057",
    dashed: bool = False,
    label: str | None = None,
) -> list[dict]:
    sx, sy = pts[0]
    rel = [[px - sx, py - sy] for px, py in pts]
    xs = [p[0] for p in rel]
    ys = [p[1] for p in rel]
    el = _base(
        "arrow",
        sx,
        sy,
        max(xs) - min(xs),
        max(ys) - min(ys),
        strokeColor=color,
        strokeWidth=2,
        strokeStyle="dashed" if dashed else "solid",
        roundness={"type": 2},
        points=rel,
        lastCommittedPoint=None,
        startBinding={"elementId": start, "focus": 0, "gap": 4} if start else None,
        endBinding={"elementId": end, "focus": 0, "gap": 4} if end else None,
        startArrowhead=None,
        endArrowhead="arrow",
        elbowed=False,
    )
    out = [el]
    if label:
        mx = sum(p[0] for p in pts) / len(pts)
        my = sum(p[1] for p in pts) / len(pts)
        out.append(text(mx - 40, my - 26, label, size=11, color=color))
    return out


# ----------------------------------------------------------------- content
# Each row: (band title, direction, [(title, body, palette), ...])
# Direction alternates so consecutive rows connect with a short hop.

ROWS: list[tuple[str, str, list[tuple[str, str, tuple[str, str]]]]] = [
    (
        "A.  OFFLINE RESEARCH  —  build/research host, never the live box",
        "ltr",
        [
            ("Market Data", "Dukascopy H4/M5 CSV\nMT4 bridge bars\nops/windows/10_ingest_all.bat", BLUE),
            ("Features", "features/fx_lifecycle.py\nmulti_tf_contract.py\n11_features_all.bat", BLUE),
            ("Labels", "triple-barrier + labels/exit_labels.py\nvalidation/uniqueness.py:\npurge + embargo, overlap 45x", BLUE),
            ("Train", "XGB · TCN · Swing Transformer\nregime HMM\n13_train_all.bat", BLUE),
            ("Validate + Certify", "validation/: MCPT (circular rotation),\nblock bootstrap, PBO/CSCV, DSR,\ncost stress → certify_models.py", YELLOW),
            ("Activate", "active_models.json + payload\nSHA-256 digest binding\n14_activate_models.bat", YELLOW),
        ],
    ),
    (
        "B.  STARTUP ADMISSION  —  installed package only, fail-closed",
        "rtl",
        [
            ("Package Preflight", "runtime/package_preflight.py\nproves training/research/registry\nmodules are ABSENT", GREEN),
            ("Startup Admission", "Settings.validate_for_startup()\nprofile · mode · live arming\nexplicit scopes", GREEN),
            ("Manifest Seed + Load", "hash-anchored manifest\nper-pair model load\nrequired-pair seed gate", GREEN),
            ("Release Authority", "release_authority.py\nattestation → egress arm\n(advisory evidence only)", GREEN),
            ("Dry-run Inference", "startup inference on live rows\npairs that fail here are\nDISABLED before any order\n+ certificate coverage (advisory)", YELLOW),
        ],
    ),
    (
        "C.  LIVE CYCLE  —  runtime/runner.py :: run_loop  (13.9k lines)",
        "ltr",
        [
            ("1. Refresh Bars", "bridge ticks/bars → feature tail\nfreshness + staleness guards", GREEN),
            ("2. Read Positions", "broker truth + positions_snapshot_\ntoken + bridge receipt stamp", GREEN),
            ("3. Reconcile Ledgers", "pending partial/exit commands\ncommit ONLY on ACK or a newer\nsnapshot — no optimistic credit", GREEN),
            ("4. Load Features", "per-timeframe rows\nfeature_freshness.py", GREEN),
            ("5. Capital Governance", "portfolio/budgeting.py snapshot\ntaken BEFORE entries are judged", GREEN),
        ],
    ),
    (
        "",
        "rtl",
        [
            ("6. Strict Scorer", "live/scorer.py + live/policy.py\nprob · edge · regime · spread\nDIAGNOSTIC ONLY — cannot\nauthorize or veto an entry", GREY),
            ("7. Adaptive Policy", "strategy/adaptive_policy.py\nenter vs no_trade on full evidence\nmargin ≥ 0.02 required;\nmargin CONTINUOUSLY SCALES LOTS", ORANGE),
            ("8. Allocator + Sleeve", "strategy/allocator.py ranks candidates\nsleeve_governance.py budgets\nper playbook sleeve", ORANGE),
            ("9. Risk Kernel + Sizing", "risk/kernel.py _entry_budget_plan\nlots = equity·f / (stop·100k)\nf ramps DOWN with drawdown\n→ stop width is RISK-NEUTRAL", ORANGE),
            ("10. Portfolio Limits", "realized correlation (signed)\nconcentration · stress\nreserves a slot only for an\nexactly-approved order", GREEN),
        ],
    ),
    (
        "",
        "ltr",
        [
            ("11. Committee + Governor", "orchestration/agents/*\nsignal · risk · portfolio · lifecycle\nVETO ONLY — cannot create\nor enlarge an order", GREEN),
            ("12. Execution Authority", "execution_egress_control.py\nboot · scope · kill · heartbeat ·\naccount mode (demo/real) · drift", GREEN),
            ("13. Submit Exits", "protective actions go FIRST and\ndo not depend on entry budget", VIOLET),
            ("14. Submit Entries", "only if every layer above passed\nSL/TP always attached\nBLOCKED today: models_uncertified", VIOLET),
            ("15. Patch State", "governance snapshot + decisions\npersisted at admission time", GREEN),
        ],
    ),
    (
        "D.  EXECUTION  —  bridge → terminal → broker, with durable ACK",
        "rtl",
        [
            ("Bridge API", "api/app.py\ncommand queue + state gateway\nsingleton bridge consumer", VIOLET),
            ("BridgeEA.mq4", "executes BUY/SELL immediately\nSL/TP attached — refuses a\nnaked entry with HTTP 412", VIOLET),
            ("MT4 Terminal", "IG demo account\nmagic-scoped orders", VIOLET),
            ("ACK Outbox", "durable terminal ACK\nthe only success proof", VIOLET),
            ("Positions Report", "lots · P&L · OrderSwap()\n→ new snapshot token → API state", VIOLET),
        ],
    ),
]

GAPS = [
    (
        "① Gate ENFORCED — both ends",
        "ACTIVATION (build host): default flipped\nFalse→True; no new unvalidated model.\nRUNTIME: the grandfathered live set (8/8\nuncertified) can no longer OPEN positions\n— reason `models_uncertified`, in the\noperational set so the adaptive policy\ncannot override it. Exits/reduces/stops\nstill work, so nothing is stranded.",
    ),
    (
        "② Carry: reported, never charged",
        "EA sends OrderSwap(); api/app.py stores\nit; nothing reads it. EA does not poll\nMODE_SWAPLONG/SHORT at all, so there is\nno carry SIGNAL. Breakeven financing is\n0.340 bps/day vs 0.5–2 retail.",
    ),
    (
        "③ Drawdown ramp — NOW WIRED",
        "Was a CLIFF: flat 0.5% risk all the way\nto risk_max_drawdown_pct, then entries\nblocked dead. Now sizing.py\ndrawdown_scaled_fraction() ramps risk\ndown as equity falls, floored at 0.25x.\nOrthogonal to the ATR stop (account\nstate vs instrument state), so the two\ncompose without double-counting.",
    ),
    (
        "④ Compute that binds nothing",
        "VERIFIED write-only, safe to cut:\nbudgeting.py target_cap (set line 284,\nread nowhere) · promotion.py\nconfig_delta.json (written, never read).\nNOT the committee agents — all 7 are\nlive at graph_runtime.py:111-117.\nNOT improve/** — deliberately excluded\nfrom prod by startup_preflight.py:23.",
    ),
    (
        "⑤ No measured edge in the inputs",
        "5 alpha families tested vs Monte Carlo;\nnone survive cost. Price-derived\nfeatures are flat gross at every\nhorizon (M5 −4.05% … H4 −11.41%).\nThis is a DATA gap, not a code gap.",
    ),
]


def build() -> dict:
    els: list[dict] = []
    rows_geo: list[list[tuple[str, float, float]]] = []

    # Title
    els.append(text(X0, 40, "FX Trading Agent — current end-to-end flow", size=30, color="#1e1e1e"))
    els.append(
        text(
            X0,
            84,
            "Generated from the live tree by tools/make_flow_diagram.py.  Follow the numbers 1→15 for the live cycle; rows alternate direction.",
            size=13,
            color="#868e96",
        )
    )

    for r, (band, direction, items) in enumerate(ROWS):
        y = Y0 + r * ROW_PITCH_Y
        if band:
            els.append(text(X0, y - 34, band, size=17, color="#1971c2"))
        geo: list[tuple[str, float, float]] = []
        n = len(items)
        for i, (title, body, palette) in enumerate(items):
            slot = i if direction == "ltr" else (NCOLS - 1 - i)
            x = X0 + slot * PITCH_X
            rid, made = box(x, y, title, body, palette)
            els.extend(made)
            geo.append((rid, x, y))
        rows_geo.append(geo)

        # intra-row arrows
        for i in range(n - 1):
            _, x1, y1 = geo[i]
            rid1 = geo[i][0]
            rid2 = geo[i + 1][0]
            _, x2, _ = geo[i + 1]
            if x2 > x1:  # rightward
                els.extend(arrow([(x1 + BOX_W, y1 + BOX_H / 2), (x2, y1 + BOX_H / 2)], start=rid1, end=rid2))
            else:  # leftward
                els.extend(arrow([(x1, y1 + BOX_H / 2), (x2 + BOX_W, y1 + BOX_H / 2)], start=rid1, end=rid2))

        # band-to-band hop
        if r > 0:
            prev_last = rows_geo[r - 1][-1]
            cur_first = geo[0]
            pid, px, py = prev_last
            cid, cx, cy = cur_first
            els.extend(
                arrow(
                    [(px + BOX_W / 2, py + BOX_H), (cx + BOX_W / 2, cy)],
                    start=pid,
                    end=cid,
                    color="#1971c2",
                )
            )

    # Feedback: Positions Report -> Read Positions, routed through the left gutter.
    exec_last = rows_geo[5][-1]
    read_pos = rows_geo[2][1]
    _, ex, ey = exec_last
    _, rx, ry = read_pos
    els.extend(
        arrow(
            [
                (ex, ey + BOX_H / 2),
                (28, ey + BOX_H / 2),
                (28, ry + BOX_H / 2),
                (rx, ry + BOX_H / 2),
            ],
            start=exec_last[0],
            end=read_pos[0],
            color="#9c36b5",
            dashed=True,
            label="broker truth closes the loop",
        )
    )

    # Gaps band
    gy = Y0 + len(ROWS) * ROW_PITCH_Y + 30
    els.append(
        text(X0, gy - 40, "WHERE IT LEAKS — what to improve, ranked by how much it changes the P&L", size=19, color="#e03131")
    )
    for i, (title, body) in enumerate(GAPS):
        _, made = box(X0 + i * PITCH_X, gy, title, body, RED, h=150)
        els.extend(made)

    # Legend
    ly = gy + 190
    els.append(text(X0, ly, "Legend", size=17, color="#1e1e1e"))
    legend = [
        (BLUE, "offline research (build host)"),
        (YELLOW, "a gate — may say no"),
        (ORANGE, "actually decides"),
        (GREEN, "binding runtime check"),
        (GREY, "computes, binds nothing"),
        (VIOLET, "broker-facing execution"),
        (RED, "known gap"),
    ]
    for i, (palette, label) in enumerate(legend):
        lx = X0 + i * 215
        els.append(
            _base(
                "rectangle",
                lx,
                ly + 30,
                26,
                26,
                backgroundColor=palette[0],
                strokeColor=palette[1],
                roundness={"type": 3},
            )
        )
        els.append(text(lx + 36, ly + 36, label, size=12, color="#343a40"))

    # Excalidraw keeps bindings two-way: each bound shape must also list the arrow.
    by_id = {e["id"]: e for e in els}
    for el in els:
        if el["type"] != "arrow":
            continue
        for side in ("startBinding", "endBinding"):
            binding = el.get(side)
            if not binding:
                continue
            target = by_id.get(binding["elementId"])
            if target is not None:
                target["boundElements"].append({"id": el["id"], "type": "arrow"})

    return {
        "type": "excalidraw",
        "version": 2,
        "source": "tools/make_flow_diagram.py",
        "elements": els,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }


def main() -> None:
    out_dir = os.path.join("docs", "diagrams")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "current-flow.excalidraw")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(build(), fh, indent=2)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
