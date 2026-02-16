#!/usr/bin/env python3
"""
Simple HTTP bridge server for MT4 EA communication.
Receives trade signals from Python agent and forwards to MT4 EA via polling.
"""

from flask import Flask, request, jsonify
from flask_cors import CORS
from collections import deque
import threading
import logging
from datetime import datetime
import json
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for frontend
logging.basicConfig(level=logging.INFO)

# Signal queue (FIFO) - MT4 EA polls this
signal_queue = deque()
signal_lock = threading.Lock()

# Report storage (from EA back to Python)
reports = deque(maxlen=1000)
report_lock = threading.Lock()

# Trading state tracking
trading_state = {
    "last_heartbeat": None,
    "equity": 0.0,
    "positions": [],
    "cycle_active": False,
    "cycle_start_equity": 0.0,
    "cycle_target": 0.0,
    "signals_sent": 0,
    "trades_executed": 0,
    "last_signal": None,
    "agent_decisions": [],
    "system_status": "starting"
}
state_lock = threading.Lock()

DASHBOARD_URL = os.getenv("DASHBOARD_URL", None)

@app.route('/')
def home():
    return "MT4 Bridge Server is RUNNING. (Do not close this window)", 200

@app.route('/poll', methods=['GET'])
def poll():
    """
    MT4 EA polls this endpoint to get pending signals.
    Returns oldest signal from queue in format: cmd=BUY;symbol=EURUSD;lots=0.1;tp_cash=100
    """
    with signal_lock:
        if signal_queue:
            signal = signal_queue.popleft()
            app.logger.info(f"[POLL] Sending to EA: {signal}")
            return signal, 200
        
        # Return current thought if no signal
        with state_lock:
            thought = trading_state.get("current_thought", "")
            if thought:
                # Send INFO command to update dashboard
                return f"cmd=INFO;thought={thought}", 200
                
        # Periodic Log (every 10s approximately) to show connection is alive even if no signals
        # We use a simple counter/timer
        # Or just checking time.
        now = datetime.now()
        if now.second % 10 == 0 and now.microsecond < 100000: # Rough check
             print(f"[{now.strftime('%H:%M:%S')}] POLL OK (MT4 Connected)", flush=True)
                
        return '', 200  # No signals

@app.route('/thought', methods=['POST'])
def thought():
    """
    Agent posts its current thought process/status.
    """
    try:
        data = request.get_json()
        text = data.get("thought", "")
        with state_lock:
            trading_state["current_thought"] = text
        
        # Force print for visibility
        print(f"[{datetime.now().strftime('%H:%M:%S')}] THOUGHT: {text}", flush=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/signal', methods=['POST'])
def signal():
    """
    Python agent posts trade signals here.
    Format: {"cmd": "BUY", "symbol": "EURUSD", "lots": 0.1, "tp_cash": 100, "sl": 1.0950}
    """
    try:
        data = request.get_json()
        
        if data.get('cmd') == 'CLOSE_ALL':
            signal_str = "cmd=CLOSE_ALL"
        else:
            # Build signal string for MT4
            parts = [f"cmd={data['cmd']}", f"symbol={data['symbol']}"]
            
            if 'lots' in data:
                parts.append(f"lots={data['lots']}")
            if 'tp_cash' in data:
                parts.append(f"tp_cash={data['tp_cash']}")
            if 'sl' in data:
                parts.append(f"sl={data['sl']}")
            if 'sl_price' in data:
                parts.append(f"sl={data['sl_price']}")
            if 'tp_price' in data:
                parts.append(f"tp_price={data['tp_price']}")
            if 'magic' in data:
                parts.append(f"magic={data['magic']}")
            if 'thought' in data:
                parts.append(f"thought={data['thought']}")
            
            signal_str = ';'.join(parts)
        
        with signal_lock:
            signal_queue.append(signal_str)
        
        # Update state
        with state_lock:
            trading_state["signals_sent"] += 1
            trading_state["last_signal"] = {
                "time": datetime.now().isoformat(),
                "data": data
            }
        
        app.logger.info(f"[SIGNAL] Queued: {signal_str}")
        return jsonify({"status": "queued", "signal": signal_str}), 200
        
    except Exception as e:
        app.logger.error(f"[SIGNAL] Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


@app.route('/report', methods=['POST'])
def report():
    """
    MT4 EA posts status reports here.
    Python agent can query /reports to see EA activity.
    """
    try:
        msg = request.get_data(as_text=True)
        with report_lock:
            reports.append({
                "time": datetime.now().isoformat(),
                "message": msg
            })
        
        # Parse and update state
        with state_lock:
            if "HEARTBEAT" in msg:
                trading_state["last_heartbeat"] = datetime.now().isoformat()
                trading_state["system_status"] = "connected"
                # Parse equity from HEARTBEAT eq=10000.00 margin=100.00 freemargin=9900.00
                try:
                    parts = msg.split()
                    for p in parts:
                        if p.startswith("eq="):
                            trading_state["equity"] = float(p.split("=")[1])
                        elif p.startswith("margin="):
                            trading_state["margin"] = float(p.split("=")[1])
                        elif p.startswith("freemargin="):
                            trading_state["freemargin"] = float(p.split("=")[1])
                except Exception as e:
                    app.logger.warning(f"Failed to parse heartbeat: {e}")
            
            elif "CYCLE_START" in msg:
                trading_state["cycle_active"] = True
                if "eq=" in msg:
                    try:
                        eq_str = msg.split("eq=")[1].split()[0]
                        trading_state["cycle_start_equity"] = float(eq_str)
                        trading_state["cycle_target"] = float(eq_str) * 0.01
                    except:
                        pass
            
            elif "CYCLE_TARGET_HIT" in msg:
                trading_state["cycle_active"] = False
            
            elif msg.startswith("OK BUY") or msg.startswith("OK SELL"):
                trading_state["trades_executed"] += 1
            
            elif msg.startswith("POSITIONS"):
                # Format: POSITIONS symbol=EURUSD,lots=0.1,profit=10.5 symbol=...
                # or "POSITIONS NONE"
                parts = msg.split(" ")[1:] # Skip "POSITIONS"
                current_positions = []
                for p in parts:
                    if p == "NONE": continue
                    # Parse symbol=EURUSD,lots=0.1...
                    pos_data = {}
                    kv_pairs = p.split(",")
                    for kv in kv_pairs:
                        if "=" in kv:
                            k, v = kv.split("=")
                            if k == "lots": pos_data[k] = float(v)
                            elif k == "profit": pos_data[k] = float(v)
                            else: pos_data[k] = v
                    if pos_data:
                        current_positions.append(pos_data)
                
                trading_state["positions"] = current_positions
                trading_state["last_pos_update"] = datetime.now().isoformat()
        
        if DASHBOARD_URL:
            try:
                import requests
                requests.post(
                    f"{DASHBOARD_URL}/api/trading/update",
                    json=trading_state,
                    timeout=2
                )
            except Exception as e:
                app.logger.warning(f"Failed to push to dashboard: {e}")
        
        # FORCE PRINT FOR USER VISIBILITY
        print(f"[{datetime.now().strftime('%H:%M:%S')}] REPORT: {msg}", flush=True)
        app.logger.info(f"[REPORT] {msg}")
        return '', 200
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        app.logger.error(f"[REPORT] Error: {e}")
        return '', 400


@app.route('/reports', methods=['GET'])
def get_reports():
    """
    Get recent reports from EA (for monitoring/debugging).
    """
    with report_lock:
        return jsonify({"reports": list(reports)}), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    with signal_lock:
        signal_count = len(signal_queue)
    with report_lock:
        report_count = len(reports)
    
    return jsonify({
        "status": "healthy",
        "pending_signals": signal_count,
        "reports_stored": report_count
    }), 200


@app.route('/state', methods=['GET'])
def get_state():
    """Get current trading state for dashboard."""
    with state_lock:
        return jsonify(trading_state), 200


@app.route('/state/decisions', methods=['POST'])
def post_decisions():
    """Agent posts its latest decisions."""
    try:
        data = request.get_json()
        with state_lock:
            trading_state["agent_decisions"] = data.get("decisions", [])
            trading_state["last_update"] = datetime.now().isoformat()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 400


# Visuals Store (for Indicator)
visuals = {}  # { "EURUSD": [ {cmd}, ... ] }
visual_lock = threading.Lock()

@app.route('/visuals', methods=['POST'])
def post_visuals():
    """
    Agent posts drawing commands for specific charts.
    Format: {"symbol": "EURUSD", "type": "arrow", "side": "BUY", "price": 1.0500, "time": "..."}
    """
    try:
        data = request.get_json()
        symbol = data.get("symbol")
        if not symbol:
            return jsonify({"error": "Missing symbol"}), 400
            
        with visual_lock:
            if symbol not in visuals:
                visuals[symbol] = []
            visuals[symbol].append(data)
            
            # Limit buffer size per symbol to avoid memory leaks if indicator is offline
            if len(visuals[symbol]) > 50:
                visuals[symbol].pop(0)
                
        return jsonify({"status": "queued"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/visuals', methods=['GET'])
def get_visuals():
    """
    Indicator polls for drawing commands for its symbol.
    Query: /visuals?symbol=EURUSD
    Returns: [ {cmd}, ... ] and CLEARS them.
    """
    symbol = request.args.get("symbol")
    if not symbol:
        return jsonify({"error": "Missing symbol param"}), 400
        
    with visual_lock:
        cmds = visuals.get(symbol, [])
        if cmds:
            # Clear after reading (consume)
            visuals[symbol] = []
            return jsonify(cmds), 200
        else:
            return jsonify([]), 200


# Market Data Store (Last known tick)
market_data = {}
md_lock = threading.Lock()

@app.route('/tick', methods=['POST'])
def tick():
    """
    MT4 EA posts market data here.
    Format: {"symbol": "EURUSD", "bid": 1.0500, "ask": 1.0501, "time": "..."}
    """
    try:
        import json
        # Strip null bytes from MQL4 StringToCharArray before any parsing
        raw = request.data.replace(b'\x00', b'')
        raw_str = raw.decode('utf-8', errors='ignore').strip()
        data = json.loads(raw_str) if raw_str else None
        if not data:
            return jsonify({"error": "Empty tick data"}), 400
        
        symbol = data.get("symbol")
        if symbol:
            with md_lock:
                market_data[symbol] = {
                    "bid": float(data.get("bid", 0)),
                    "ask": float(data.get("ask", 0)),
                    "spread": float(data.get("spread", 0)),
                    "time": datetime.now().isoformat()
                }
            
            # Explicitly log every tick for user visibility ("detailed log")
            now_str = datetime.now().strftime('%H:%M:%S')
            # Use print to bypass Flask log suppression
            print(f"[{now_str}] TICK {symbol} {data.get('bid')}", flush=True)

        return '', 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/ticks', methods=['GET'])
def get_ticks():
    """Agent gets latest market data."""
    with md_lock:
        return jsonify(market_data), 200

if __name__ == '__main__':
    print("\n" + "="*60)
    print("MT4 Bridge Server")
    print("="*60)
    print("STATUS:  [RUNNING] (Green)")
    print(f"ADDRESS: http://127.0.0.1:58710")
    print("\nEndpoints:")
    print("  GET  /poll    - MT4 EA polls for signs")
    print("  POST /signal  - Python agent posts trade commands")
    print("  POST /report  - MT4 EA posts status updates")
    print("  POST /tick    - MT4 EA posts price updates")
    print("  GET  /ticks   - Python agent gets latest prices")
    print("  GET  /reports - View recent EA reports")
    print("  GET  /health  - Health check")
    
    # SUPPRESS FLASK/WERKZEUG LOGS
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    print("\nLogs will appear below...")
    print("="*60 + "\n")
    
    # Background heartbeat so the user knows bridge is alive
    import time as _time
    def _heartbeat():
        while True:
            _time.sleep(30)
            now_str = datetime.now().strftime('%H:%M:%S')
            with md_lock:
                syms = list(market_data.keys())
            if syms:
                print(f"[{now_str}] HEARTBEAT: Bridge alive | Tracking: {', '.join(syms)}", flush=True)
            else:
                print(f"[{now_str}] HEARTBEAT: Bridge alive | Waiting for MT4 ticks...", flush=True)
    
    hb_thread = threading.Thread(target=_heartbeat, daemon=True)
    hb_thread.start()
    
    app.run(host='127.0.0.1', port=58710, debug=False)
