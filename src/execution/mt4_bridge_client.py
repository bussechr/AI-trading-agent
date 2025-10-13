import requests

API = "http://127.0.0.1:5000"

def send(side: str, symbol: str, *, lots: float = 0.0,
         tp_cash: float | None = None, sl: float | None = None,
         magic: int = 246810) -> None:
    """
    lots=0.0 -> EA enforces *minimum* lot.
    tp_cash  -> EA converts cash TP (~1% of equity) to a price TP.
    """
    payload = {"cmd": side, "symbol": symbol, "lots": lots, "magic": magic}
    if tp_cash is not None: payload["tp_cash"] = float(tp_cash)
    if sl is not None: payload["sl"] = float(sl)
    r = requests.post(f"{API}/signal", json=payload, timeout=2)
    r.raise_for_status()

def close_all() -> None:
    requests.post(f"{API}/signal", json={"cmd": "CLOSE_ALL"}, timeout=2)
