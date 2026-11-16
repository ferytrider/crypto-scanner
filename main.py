
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

BASE = "https://api.toobit.com"
STATE_FILE = Path("scan_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

PIVOT_LEFT = 3
PIVOT_RIGHT = 3

# Higher-timeframe pivots, tested by closed 1h candles
PIVOT_INTERVALS = {
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}
SCAN_INTERVAL = "1h"
SCAN_MS = 60 * 60 * 1000

REQUEST_DELAY = 0.18
MAX_RETRIES = 5


def api_get(path, params=None):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?" + query if query else "")

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "ShadowScanner/1.0"}
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                return json.loads(response.read().decode())

        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(min(2 ** attempt, 30))
                continue
            raise

        except Exception:
            time.sleep(min(2 ** attempt, 30))

    raise RuntimeError("API request failed: " + path)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"sent": []}


def save_state(state):
    # Keep recent alert IDs only
    state["sent"] = state["sent"][-10000:]
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False)
    )


def get_contracts():
    data = api_get("/api/v1/exchangeInfo")

    contracts = data.get("contracts", [])

    return [
        item["symbol"]
        for item in contracts
        if item.get("status") == "TRADING"
        and item.get("symbol", "").endswith("-SWAP-USDT")
    ]


def get_candles(symbol, interval, limit=1000):
    data = api_get(
        "/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
    )

    if not isinstance(data, list):
        return []

    candles = []

    for row in data:
        if not isinstance(row, list) or len(row) < 6:
            continue

        candles.append({
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        })

    return candles


def closed_candles(candles, interval_ms):
    now = int(time.time() * 1000)

    return [
        c for c in candles
        if c["time"] + interval_ms <= now
    ]


def find_pivots(candles, interval, interval_ms):
    pivots = []

    # A pivot needs 3 candles on both sides.
    for i in range(
        PIVOT_LEFT,
        len(candles) - PIVOT_RIGHT
    ):
        current = candles[i]

        before = candles[i - PIVOT_LEFT:i]
        after = candles[i + 1:i + PIVOT_RIGHT + 1]

        left_highs = [c["high"] for c in before]
        right_highs = [c["high"] for c in after]

        left_lows = [c["low"] for c in before]
        right_lows = [c["low"] for c in after]

        # Confirmed pivot high
        if (
            current["high"] > max(left_highs)
            and current["high"] > max(right_highs)
        ):
            pivots.append({
                "side": "HIGH",
                "price": current["high"],
                "time": current["time"],
                "confirmed_at": (
                    after[-1]["time"] + interval_ms
                ),
                "interval": interval,
            })

        # Confirmed pivot low
        if (
            current["low"] < min(left_lows)
            and current["low"] < min(right_lows)
        ):
            pivots.append({
                "side": "LOW",
                "price": current["low"],
                "time": current["time"],
                "confirmed_at": (
                    after[-1]["time"] + interval_ms
                ),
                "interval": interval,
            })

    return pivots


def check_sweep(pivot, candle):
    level = pivot["price"]

    # Price takes liquidity above pivot high,
    # then the SAME candle closes below it.
    if pivot["side"] == "HIGH":
        if (
            candle["high"] > level
            and candle["close"] < level
        ):
            return "SELL"

    # Price takes liquidity below pivot low,
    # then the SAME candle closes above it.
    if pivot["side"] == "LOW":
        if (
            candle["low"] < level
            and candle["close"] > level
        ):
            return "BUY"

    return None


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
        return False

    url = (
        "https://api.telegram.org/bot"
        + TOKEN
        + "/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    req = urllib.request.Request(url, data=payload)

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())
            return result.get("ok", False)
    except Exception as e:
        print("Telegram error:", e)
        return False


def make_alert(symbol, direction, pivot, candle):
    emoji = "🔴" if direction == "SELL" else "🟢"

    pivot_time = datetime.fromtimestamp(
        pivot["time"] / 1000,
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    candle_time = datetime.fromtimestamp(
        candle["time"] / 1000,
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")

    return (
        f"{emoji} SHADOW {direction}\n\n"
        f"Symbol: {symbol}\n"
        f"Pivot timeframe: {pivot['interval']}\n"
        f"Pivot type: {pivot['side']}\n"
        f"Pivot price: {pivot['price']}\n"
        f"Pivot time: {pivot_time}\n\n"
        f"1H sweep candle:\n"
        f"High: {candle['high']}\n"
        f"Low: {candle['low']}\n"
        f"Close: {candle['close']}\n"
        f"Close time: {candle_time}\n\n"
        f"Liquidity sweep confirmed."
    )


def scan_symbol(symbol, state, latest_hour):
    new_alerts = 0

    for interval, interval_ms in PIVOT_INTERVALS.items():
        try:
            candles = get_candles(symbol, interval, 1000)
            candles = closed_candles(candles, interval_ms)

            pivots = find_pivots(
                candles, interval, interval_ms
            )

            time.sleep(REQUEST_DELAY)

            for pivot in pivots:
                # The pivot must be confirmed BEFORE
                # the 1h sweep candle begins.
                if pivot["confirmed_at"] > latest_hour["time"]:
                    continue

                alert_id = (
                    symbol + "|" +
                    interval + "|" +
                    str(pivot["time"]) + "|" +
                    pivot["side"] + "|" +
                    str(latest_hour["time"])
                )

                if alert_id in state["sent"]:
                    continue

                direction = check_sweep(
                    pivot, latest_hour
                )

                if not direction:
                    continue

                message = make_alert(
                    symbol,
                    direction,
                    pivot,
                    latest_hour
                )

                print(message)

                if send_telegram(message):
                    state["sent"].append(alert_id)
                    new_alerts += 1

        except Exception as e:
            print("Error:", symbol, interval, str(e))

    return new_alerts


def main():
    print("SHADOW SCANNER STARTED")

    state = load_state()

    contracts = get_contracts()
    print("USDT futures contracts:", len(contracts))

    checked = 0
    errors = 0
    alerts = 0

    for symbol in contracts:
        try:
            candles = get_candles(
                symbol, SCAN_INTERVAL, 10
            )
            candles = closed_candles(candles, SCAN_MS)

            if not candles:
                continue

            latest_hour = candles[-1]

            alerts += scan_symbol(
                symbol, state, latest_hour
            )

            checked += 1

        except Exception as e:
            errors += 1
            print("Symbol error:", symbol, str(e))

        time.sleep(REQUEST_DELAY)
        save_state(state)

    save_state(state)

    print("\nSHADOW SCAN FINISHED")
    print("Checked:", checked)
    print("Errors:", errors)
    print("New alerts:", alerts)


if __name__ == "__main__":
    main()
