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
        return '', 200  # No signals


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
            if 'magic' in data:
                parts.append(f"magic={data['magic']}")
            
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
                # Parse equity from HEARTBEAT eq=10000.00
                if "eq=" in msg:
                    try:
                        eq_str = msg.split("eq=")[1].split()[0]
                        trading_state["equity"] = float(eq_str)
                    except:
                        pass
            
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
        
        app.logger.info(f"[REPORT] {msg}")
        return '', 200
    except Exception as e:
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


if __name__ == '__main__':
    print("=" * 60)
    print("MT4 Bridge Server")
    print("=" * 60)
    print("Listening on: http://127.0.0.1:5000")
    print("\nEndpoints:")
    print("  GET  /poll    - MT4 EA polls for signals")
    print("  POST /signal  - Python agent posts trade commands")
    print("  POST /report  - MT4 EA posts status updates")
    print("  GET  /reports - View recent EA reports")
    print("  GET  /health  - Health check")
    print("\nMake sure to add http://127.0.0.1:5000 to MT4 WebRequest whitelist!")
    print("=" * 60)
    
    app.run(host='127.0.0.1', port=5000, debug=False)
