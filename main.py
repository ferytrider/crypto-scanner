
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://api.toobit.com"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = Path("scan_state.json")

# SHADOW settings
PIVOT_INTERVALS = ["4h", "1d", "1w"]
PIVOT_LOOKBACK = 150

SWEEP_INTERVAL = "1h"
SWEEP_LOOKBACK = 180
RECENT_SWEEP_CANDLES = 3

# Rate limit protection
MIN_REQUEST_INTERVAL = 0.35
MAX_RETRIES = 5

last_request_time = 0.0


def wait_before_request():
    global last_request_time

    now = time.time()
    wait = MIN_REQUEST_INTERVAL - (now - last_request_time)

    if wait > 0:
        time.sleep(wait)

    last_request_time = time.time()


def get_json(url):
    for attempt in range(MAX_RETRIES):
        wait_before_request()

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=30
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except urllib.error.HTTPError as error:
            if error.code == 429:
                wait_seconds = 10

                try:
                    retry_after = error.headers.get("Retry-After")
                    if retry_after:
                        wait_seconds = max(
                            wait_seconds,
                            int(float(retry_after))
                        )

                    reset = error.headers.get(
                        "X-Api-Limit-Reset-Timestamp"
                    )

                    if reset:
                        reset_seconds = (
                            int(reset) / 1000 - time.time() + 1
                        )

                        wait_seconds = max(
                            wait_seconds,
                            int(reset_seconds)
                        )
                except Exception:
                    pass

                wait_seconds = min(
                    max(wait_seconds, 5),
                    120
                )

                print(
                    f"429 rate limit. Waiting {wait_seconds}s "
                    f"({attempt + 1}/{MAX_RETRIES})"
                )

                time.sleep(wait_seconds)
                continue

            if error.code >= 500:
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue

            raise

        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise

            time.sleep(min(60, 2 ** (attempt + 1)))

    raise RuntimeError("Maximum retries reached")


def get_contracts():
    data = get_json(
        BASE + "/api/v1/exchangeInfo"
    )

    contracts = data.get("contracts", [])
    symbols = []

    for item in contracts:
        symbol = item.get("symbol", "")

        if not symbol.endswith("-SWAP-USDT"):
            continue

        if item.get("status") != "TRADING":
            continue

        symbols.append(symbol)

    return sorted(set(symbols))


def interval_ms(interval):
    durations = {
        "1h": 60 * 60 * 1000,
        "4h": 4 * 60 * 60 * 1000,
        "1d": 24 * 60 * 60 * 1000,
        "1w": 7 * 24 * 60 * 60 * 1000,
    }

    return durations[interval]


def get_candles(symbol, interval, limit):
    duration = interval_ms(interval)

    end_time = int(time.time() * 1000)
    start_time = end_time - (duration * (limit + 5))

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time,
        "limit": limit,
    })

    url = BASE + "/quote/v1/klines?" + params

    data = get_json(url)

    if not isinstance(data, list):
        raise ValueError("Unexpected candle response")

    now_ms = int(time.time() * 1000)
    candles = []

    for row in data:
        if not isinstance(row, list) or len(row) < 5:
            continue

        open_time = int(row[0])
        close_time = open_time + duration - 1

        # Only fully closed candles
        if close_time >= now_ms:
            continue

        candles.append({
            "time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "close_time": close_time,
        })

    candles.sort(key=lambda candle: candle["time"])

    return candles


def find_pivots(candles, interval):
    pivots = []

    # Minimum 3 candles on each side
    for i in range(3, len(candles) - 3):
        center = candles[i]

        left = candles[i - 3:i]
        right = candles[i + 1:i + 4]

        is_high = all(
            center["high"] > candle["high"]
            for candle in left + right
        )

        is_low = all(
            center["low"] < candle["low"]
            for candle in left + right
        )

        confirmed_at = right[-1]["close_time"]

        if is_high:
            pivots.append({
                "type": "HIGH",
                "price": center["high"],
                "time": center["time"],
                "confirmed_at": confirmed_at,
                "interval": interval,
            })

        if is_low:
            pivots.append({
                "type": "LOW",
                "price": center["low"],
                "time": center["time"],
                "confirmed_at": confirmed_at,
                "interval": interval,
            })

    return pivots


def latest_unraided_pivot(
    pivots,
    pivot_type,
    sweep_candle,
    hourly_candles
):
    candidates = [
        pivot for pivot in pivots
        if (
            pivot["type"] == pivot_type
            and pivot["confirmed_at"] < sweep_candle["time"]
        )
    ]

    candidates.sort(
        key=lambda pivot: pivot["time"],
        reverse=True
    )

    for pivot in candidates:
        level = pivot["price"]
        already_raided = False

        # Check all available hourly candles between
        # pivot confirmation and the current sweep candle.
        for candle in hourly_candles:
            if candle["time"] <= pivot["confirmed_at"]:
                continue

            if candle["time"] >= sweep_candle["time"]:
                continue

            if pivot_type == "HIGH":
                if candle["high"] > level:
                    already_raided = True
                    break

            else:
                if candle["low"] < level:
                    already_raided = True
                    break

        if not already_raided:
            return pivot

    return None


def check_sweep(pivot, candle):
    level = pivot["price"]

    # High pivot: take liquidity above,
    # then close back below the pivot.
    if pivot["type"] == "HIGH":
        if (
            candle["high"] > level
            and candle["close"] < level
        ):
            return "SELL"

    # Low pivot: take liquidity below,
    # then close back above the pivot.
    if pivot["type"] == "LOW":
        if (
            candle["low"] < level
            and candle["close"] > level
        ):
            return "BUY"

    return None


def load_state():
    if not STATE_FILE.exists():
        return {"sent_alerts": []}

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return {"sent_alerts": []}

        if not isinstance(
            state.get("sent_alerts"),
            list
        ):
            state["sent_alerts"] = []

        return state

    except Exception:
        return {"sent_alerts": []}


def save_state(state):
    temp_file = STATE_FILE.with_suffix(".tmp")

    with open(
        temp_file,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False
        )

    temp_file.replace(STATE_FILE)


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials are missing.")
        return False

    url = (
        f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    request = urllib.request.Request(
        url,
        data=payload
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=25
        ) as response:
            result = json.loads(
                response.read().decode("utf-8")
            )

        return result.get("ok", False)

    except Exception as error:
        print(f"Telegram error: {error}")
        return False


def make_alert(symbol, pivot, candle, side):
    direction = (
        "🔴 SELL"
        if side == "SELL"
        else "🟢 BUY"
    )

    return (
        f"🌑 SHADOW ALERT\n\n"
        f"Symbol: {symbol}\n"
        f"Signal: {direction}\n\n"
        f"Pivot timeframe: {pivot['interval']}\n"
        f"Pivot type: {pivot['type']}\n"
        f"Pivot price: {pivot['price']}\n"
        f"Pivot time: {pivot['time']}\n\n"
        f"Sweep candle timeframe: 1h\n"
        f"Sweep candle time: {candle['time']}\n"
        f"High: {candle['high']}\n"
        f"Low: {candle['low']}\n"
        f"Close: {candle['close']}\n\n"
        f"Liquidity swept and candle closed "
        f"back inside the pivot level."
    )


def scan_symbol(symbol, state):
    alerts = []
    errors = 0

    try:
        # Load historical hourly candles once.
        hourly = get_candles(
            symbol,
            SWEEP_INTERVAL,
            SWEEP_LOOKBACK
        )

        if len(hourly) < 10:
            return alerts, 1

        # Check the latest 3 closed hourly candles.
        sweep_candles = hourly[-RECENT_SWEEP_CANDLES:]

        # Load pivot histories.
        pivot_data = {}

        for interval in PIVOT_INTERVALS:
            candles = get_candles(
                symbol,
                interval,
                PIVOT_LOOKBACK
            )

            pivot_data[interval] = find_pivots(
                candles,
                interval
            )

        for sweep_candle in sweep_candles:
            for interval in PIVOT_INTERVALS:
                pivots = pivot_data[interval]

                # Latest valid previous pivot high
                high_pivot = latest_unraided_pivot(
                    pivots,
                    "HIGH",
                    sweep_candle,
                    hourly
                )

                # Latest valid previous pivot low
                low_pivot = latest_unraided_pivot(
                    pivots,
                    "LOW",
                    sweep_candle,
                    hourly
                )

                checks = [
                    (high_pivot, "SELL"),
                    (low_pivot, "BUY"),
                ]

                for pivot, expected_side in checks:
                    if pivot is None:
                        continue

                    side = check_sweep(
                        pivot,
                        sweep_candle
                    )

                    if side != expected_side:
                        continue

                    alert_id = (
                        f"{symbol}|{interval}|"
                        f"{pivot['type']}|"
                        f"{pivot['time']}|"
                        f"{sweep_candle['time']}"
                    )

                    if alert_id in state["sent_alerts"]:
                        continue

                    message = make_alert(
                        symbol,
                        pivot,
                        sweep_candle,
                        side
                    )

                    alerts.append(
                        (alert_id, message)
                    )

    except Exception as error:
        errors += 1
        print(f"Error on {symbol}: {error}")

    return alerts, errors


def main():
    print("Starting SHADOW scanner...")

    state = load_state()

    sent_ids = set(state["sent_alerts"])

    try:
        symbols = get_contracts()
    except Exception as error:
        print(f"Could not get contracts: {error}")
        return

    print(
        f"Found {len(symbols)} USDT futures contracts."
    )

    checked = 0
    errors = 0
    new_alerts = 0

    # Sequential scanning to reduce API pressure.
    for symbol in symbols:
        alerts, symbol_errors = scan_symbol(
            symbol,
            state
        )

        checked += 1
        errors += symbol_errors

        for alert_id, message in alerts:
            if alert_id in sent_ids:
                continue

            if send_telegram(message):
                sent_ids.add(alert_id)
                state["sent_alerts"].append(alert_id)

                # Keep state file from growing indefinitely.
                state["sent_alerts"] = (
                    state["sent_alerts"][-10000:]
                )

                save_state(state)
                new_alerts += 1

        if checked % 25 == 0:
            print(
                f"Progress: {checked}/{len(symbols)} "
                f"| Errors: {errors} "
                f"| New alerts: {new_alerts}"
            )

    save_state(state)

    print(
        f"Finished. Checked: {checked}, "
        f"Errors: {errors}, "
        f"New alerts: {new_alerts}"
    )


if __name__ == "__main__":
    main()

import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://api.toobit.com"
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

STATE_FILE = Path("scan_state.json")

PIVOT_INTERVALS = ["4h", "1d", "1w"]
PIVOT_LOOKBACK = 150

SWEEP_INTERVAL = "1h"
SWEEP_LOOKBACK = 180
RECENT_SWEEP_CANDLES = 3

MIN_REQUEST_INTERVAL = 0.35
MAX_RETRIES = 5

last_request_time = 0.0


def wait_before_request():
    global last_request_time
    wait = MIN_REQUEST_INTERVAL - (time.time() - last_request_time)
    if wait > 0:
        time.sleep(wait)
    last_request_time = time.time()


def get_json(url):
    for attempt in range(MAX_RETRIES):
        wait_before_request()
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = 10
                try:
                    retry = error.headers.get("Retry-After")
                    if retry:
                        delay = max(delay, int(float(retry)))

                    reset = error.headers.get(
                        "X-Api-Limit-Reset-Timestamp"
                    )
                    if reset:
                        delay = max(
                            delay,
                            int(int(reset) / 1000 - time.time() + 1)
                        )
                except Exception:
                    pass

                delay = min(max(delay, 5), 120)
                print(f"429. Waiting {delay}s ({attempt + 1}/{MAX_RETRIES})")
                time.sleep(delay)
                continue

            if error.code >= 500:
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue

            raise

        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(min(60, 2 ** (attempt + 1)))

    raise RuntimeError("Maximum retries reached")


def get_contracts():
    data = get_json(BASE + "/api/v1/exchangeInfo")
    symbols = []

    for item in data.get("contracts", []):
        symbol = item.get("symbol", "")
        if (
            symbol.endswith("-SWAP-USDT")
            and item.get("status") == "TRADING"
        ):
            symbols.append(symbol)

    return sorted(set(symbols))


def interval_ms(interval):
    return {
        "1h": 3600000,
        "4h": 14400000,
        "1d": 86400000,
        "1w": 604800000,
    }[interval]


def get_candles(symbol, interval, limit):
    duration = interval_ms(interval)
    end_time = int(time.time() * 1000)
    start_time = end_time - duration * (limit + 5)

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": start_time,
        "endTime": end_time,
        "limit": limit,
    })

    data = get_json(BASE + "/quote/v1/klines?" + params)

    if not isinstance(data, list):
        raise ValueError("Unexpected candle response")

    now = int(time.time() * 1000)
    candles = []

    for row in data:
        if not isinstance(row, list) or len(row) < 5:
            continue

        open_time = int(row[0])
        close_time = open_time + duration - 1

        if close_time >= now:
            continue

        candles.append({
            "time": open_time,
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
            "close_time": close_time,
        })

    candles.sort(key=lambda x: x["time"])
    return candles


def find_pivots(candles, interval):
    pivots = []

    for i in range(3, len(candles) - 3):
        center = candles[i]
        left = candles[i - 3:i]
        right = candles[i + 1:i + 4]

        is_high = all(
            center["high"] > c["high"]
            for c in left + right
        )

        is_low = all(
            center["low"] < c["low"]
            for c in left + right
        )

        confirmed_at = right[-1]["close_time"]

        if is_high:
            pivots.append({
                "type": "HIGH",
                "price": center["high"],
                "time": center["time"],
                "confirmed_at": confirmed_at,
                "interval": interval,
            })

        if is_low:
            pivots.append({
                "type": "LOW",
                "price": center["low"],
                "time": center["time"],
                "confirmed_at": confirmed_at,
                "interval": interval,
            })

    return pivots


def was_raided(pivot, sweep_candle, pivot_candles, hourly_candles):
    level = pivot["price"]
    pivot_type = pivot["type"]

    # Check closed candles of the pivot timeframe.
    # This detects old liquidity sweeps.
    for candle in pivot_candles:
        if candle["time"] <= pivot["confirmed_at"]:
            continue
        if candle["time"] >= sweep_candle["time"]:
            continue

        if pivot_type == "HIGH" and candle["high"] > level:
            return True

        if pivot_type == "LOW" and candle["low"] < level:
            return True

    # Also check hourly candles, including candles inside
    # the currently forming 4h / daily / weekly candle.
    for candle in hourly_candles:
        if candle["time"] <= pivot["confirmed_at"]:
            continue
        if candle["time"] >= sweep_candle["time"]:
            continue

        if pivot_type == "HIGH" and candle["high"] > level:
            return True

        if pivot_type == "LOW" and candle["low"] < level:
            return True

    return False


def latest_unraided_pivot(
    pivots,
    pivot_type,
    sweep_candle,
    pivot_candles,
    hourly_candles
):
    candidates = [
        p for p in pivots
        if (
            p["type"] == pivot_type
            and p["confirmed_at"] < sweep_candle["time"]
        )
    ]

    candidates.sort(
        key=lambda p: p["time"],
        reverse=True
    )

    for pivot in candidates:
        if not was_raided(
            pivot,
            sweep_candle,
            pivot_candles,
            hourly_candles
        ):
            return pivot

    return None


def check_sweep(pivot, candle):
    level = pivot["price"]

    if pivot["type"] == "HIGH":
        if candle["high"] > level and candle["close"] < level:
            return "SELL"

    if pivot["type"] == "LOW":
        if candle["low"] < level and candle["close"] > level:
            return "BUY"

    return None


def load_state():
    if not STATE_FILE.exists():
        return {"sent_alerts": []}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return {"sent_alerts": []}

        if not isinstance(state.get("sent_alerts"), list):
            state["sent_alerts"] = []

        return state

    except Exception:
        return {"sent_alerts": []}


def save_state(state):
    temp = STATE_FILE.with_suffix(".tmp")

    with open(temp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)

    temp.replace(STATE_FILE)


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials are missing.")
        return False

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    request = urllib.request.Request(url, data=payload)

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
        return result.get("ok", False)

    except Exception as error:
        print(f"Telegram error: {error}")
        return False


def make_alert(symbol, pivot, candle, side):
    direction = "🔴 SELL" if side == "SELL" else "🟢 BUY"

    return (
        f"🌑 SHADOW ALERT\n\n"
        f"Symbol: {symbol}\n"
        f"Signal: {direction}\n\n"
        f"Pivot timeframe: {pivot['interval']}\n"
        f"Pivot type: {pivot['type']}\n"
        f"Pivot price: {pivot['price']}\n"
        f"Pivot time: {pivot['time']}\n\n"
        f"Sweep timeframe: 1h\n"
        f"Sweep candle time: {candle['time']}\n"
        f"High: {candle['high']}\n"
        f"Low: {candle['low']}\n"
        f"Close: {candle['close']}\n\n"
        f"Liquidity swept and candle closed back inside."
    )


def scan_symbol(symbol, state):
    alerts = []

    try:
        hourly = get_candles(
            symbol,
            SWEEP_INTERVAL,
            SWEEP_LOOKBACK
        )

        if len(hourly) < 10:
            return alerts, 1

        pivot_data = {}

        for interval in PIVOT_INTERVALS:
            candles = get_candles(
                symbol,
                interval,
                PIVOT_LOOKBACK
            )

            pivots = find_pivots(candles, interval)
            pivot_data[interval] = (candles, pivots)

        sweep_candles = hourly[-RECENT_SWEEP_CANDLES:]

        for sweep_candle in sweep_candles:
            for interval in PIVOT_INTERVALS:
                pivot_candles, pivots = pivot_data[interval]

                for pivot_type, expected_side in [
                    ("HIGH", "SELL"),
                    ("LOW", "BUY"),
                ]:
                    pivot = latest_unraided_pivot(
                        pivots,
                        pivot_type,
                        sweep_candle,
                        pivot_candles,
                        hourly
                    )

                    if pivot is None:
                        continue

                    side = check_sweep(pivot, sweep_candle)

                    if side != expected_side:
                        continue

                    alert_id = (
                        f"{symbol}|{interval}|{pivot_type}|"
                        f"{pivot['time']}|{sweep_candle['time']}"
                    )

                    if alert_id in state["sent_alerts"]:
                        continue

                    alerts.append((
                        alert_id,
                        make_alert(
                            symbol,
                            pivot,
                            sweep_candle,
                            side
                        )
                    ))

        return alerts, 0

    except Exception as error:
        print(f"Error on {symbol}: {error}")
        return alerts, 1


def main():
    print("Starting SHADOW scanner...")

    state = load_state()
    sent_ids = set(state["sent_alerts"])

    try:
        symbols = get_contracts()
    except Exception as error:
        print(f"Could not get contracts: {error}")
        return

    print(f"Found {len(symbols)} USDT futures contracts.")

    checked = 0
    errors = 0
    new_alerts = 0

    # Sequential scanning reduces API pressure.
    for symbol in symbols:
        alerts, symbol_errors = scan_symbol(symbol, state)

        checked += 1
        errors += symbol_errors

        for alert_id, message in alerts:
            if alert_id in sent_ids:
                continue

            if send_telegram(message):
                sent_ids.add(alert_id)
                state["sent_alerts"].append(alert_id)

                state["sent_alerts"] = (
                    state["sent_alerts"][-10000:]
                )

                save_state(state)
                new_alerts += 1

        if checked % 25 == 0:
            print(
                f"Progress: {checked}/{len(symbols)} "
                f"| Errors: {errors} "
                f"| New alerts: {new_alerts}"
            )

    save_state(state)

    print(
        f"Finished. Checked: {checked}, "
        f"Errors: {errors}, "
        f"New alerts: {new_alerts}"
    )


if __name__ == "__main__":
    main()
