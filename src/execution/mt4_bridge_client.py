import requests
import os
import time
import logging

logger = logging.getLogger(__name__)

# C1 FIX: Use environment variable instead of hardcoded URL
API = os.environ.get("MT4_BRIDGE_URL", "http://127.0.0.1:58710")

def send(side: str, symbol: str, *, lots: float = 0.0,
         tp_cash: float | None = None, sl_price: float | None = None, tp_price: float | None = None,
         magic: int = 246810, max_retries: int = 3) -> None:
    """
    Send trade signal to MT4 bridge with retry logic.
    
    lots=0.0 -> EA enforces *minimum* lot.
    tp_cash  -> EA converts cash TP (~1% of equity) to a price TP.
    sl_price -> Absolute price for Stop Loss.
    tp_price -> Absolute price for Take Profit.
    max_retries -> Number of retry attempts (default: 3)
    """
    payload = {"cmd": side, "symbol": symbol, "lots": lots, "magic": magic}
    if tp_cash is not None: payload["tp_cash"] = float(tp_cash)
    if sl_price is not None: payload["sl_price"] = float(sl_price)
    if tp_price is not None: payload["tp_price"] = float(tp_price)
    
    # C2 FIX: Add exponential backoff retry logic
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{API}/signal", json=payload, timeout=2)
            r.raise_for_status()
            logger.debug(f"Signal sent successfully: {side} {symbol} @ {lots} lots")
            return  # Success
        except requests.exceptions.Timeout as e:
            # C3 FIX: Use logging instead of print
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(f"Bridge timeout (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s: {e}")
                time.sleep(wait_time)
            else:
                logger.error(f"Bridge timeout after {max_retries} attempts: {e}")
        except requests.exceptions.ConnectionError as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"Bridge connection error (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s: {e}")
                time.sleep(wait_time)
            else:
                logger.error(f"Bridge connection failed after {max_retries} attempts: {e}")
        except Exception as e:
            # Log but don't crash main loop
            logger.error(f"Bridge error on {side} {symbol}: {e}")
            break  # Don't retry on unexpected errors

def close_all(max_retries: int = 3) -> None:
    """Close all positions with retry logic."""
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{API}/signal", json={"cmd": "CLOSE_ALL"}, timeout=2)
            r.raise_for_status()
            logger.info("CLOSE_ALL signal sent successfully")
            return
        except Exception as e:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.warning(f"CLOSE_ALL failed (attempt {attempt+1}/{max_retries}), retrying in {wait_time}s: {e}")
                time.sleep(wait_time)
            else:
                logger.error(f"CLOSE_ALL failed after {max_retries} attempts: {e}")

def update_thought(thought: str, max_retries: int = 3) -> None:
    """Send thought process to on-chart dashboard."""
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{API}/thought", json={"thought": thought}, timeout=1)
            r.raise_for_status()
            return
        except Exception as e:
            # Don't retry heavily for non-critical dashboard updates
            pass

def get_ticks(max_retries: int = 1) -> dict:
    """Fetch latest market data from bridge."""
    for attempt in range(max_retries):
        try:
            r = requests.get(f"{API}/ticks", timeout=1)
            r.raise_for_status()
            return r.json()
        except Exception:
            pass
    return {}

def get_positions(max_retries: int = 1) -> list:
    """Fetch current open positions from bridge."""
    for attempt in range(max_retries):
        try:
            r = requests.get(f"{API}/state", timeout=1)
            r.raise_for_status()
            data = r.json()
            return data.get("positions", [])
        except Exception:
            pass
    return []

def get_account_info(max_retries: int = 1) -> dict:
    """Fetch current account equity and margin from bridge."""
    for attempt in range(max_retries):
        try:
            r = requests.get(f"{API}/state", timeout=1)
            r.raise_for_status()
            return r.json()
        except Exception:
            pass
    return {}

def post_visuals(visual_data: dict, max_retries: int = 1) -> None:
    """
    Send visual command to bridge (fire-and-forget).
    visual_data: {"symbol": "EURUSD", "type": "arrow", "side": "BUY", ...}
    """
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{API}/visuals", json=visual_data, timeout=1)
            r.raise_for_status()
            return
        except Exception:
            pass

def check_connection(retries: int = 3) -> bool:
    """Verify bridge server is running and reachable."""
    import time
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}/health", timeout=3)
            if r.status_code == 200:
                return True
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)  # Wait 1s between retries
    return False
