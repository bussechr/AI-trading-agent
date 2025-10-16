from __future__ import annotations
import math, requests
from dataclasses import dataclass
from typing import List, Dict, Optional

@dataclass
class OptionRow:
    K: float; T: float; cp: str; bid: float; ask: float

@dataclass
class Chain:
    symbol_root: str; S0: float; rd: float; rf: float; rows: List[OptionRow]

class HTTPFXOptionProvider:
    """
    Pulls an option chain via REST and maps fields into a standard structure.
    The endpoint should return JSON: either a dict with 'meta' + 'rows', or a list of rows.
    Each row must have {K, T, cp, bid, ask}. Meta should contain S0, rd, rf (or F).
    """
    def __init__(self, url_template: str, headers: Optional[dict]=None,
                 field_map: Optional[dict]=None, timeout: float=10.0):
        self.url_template = url_template
        self.headers = headers or {}
        self.fm = field_map or {"K":"K","T":"T","cp":"cp","bid":"bid","ask":"ask","S0":"S0","rd":"rd","rf":"rf","F":"F"}
        self.timeout = timeout

    def get_chain(self, symbol_root: str) -> Chain:
        url = self.url_template.format(symbol=symbol_root)
        r = requests.get(url, headers=self.headers, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()

        if isinstance(data, dict) and "rows" in data:
            meta, rows_json = data, data["rows"]
        else:
            meta, rows_json = {}, data

        S0 = meta.get(self.fm["S0"], None)
        rd = float(meta.get(self.fm["rd"], 0.0))
        rf = float(meta.get(self.fm["rf"], 0.0))
        F  = meta.get(self.fm["F"], None)

        rows: List[OptionRow] = []
        for row in rows_json:
            rows.append(OptionRow(
                K=float(row[self.fm["K"]]),
                T=float(row[self.fm["T"]]),
                cp=str(row[self.fm["cp"]]).upper()[0],
                bid=float(row[self.fm["bid"]]),
                ask=float(row[self.fm["ask"]]),
            ))

        if S0 is None:
            if F is None:
                raise ValueError("Provider payload missing S0 (or F).")
            # back out S0 from F at the shortest T
            Tmin = min(rw.T for rw in rows) if rows else 0.0
            S0 = float(F) * math.exp(-(rd - rf)*Tmin)

        return Chain(symbol_root=symbol_root, S0=float(S0), rd=rd, rf=rf, rows=rows)
