
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

BASE = "https://api.toobit.com"
STATE_FILE = Path("scan_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

FIAT_AND_METALS = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
    "CNH", "CNY", "HKD", "SGD", "KRW", "INR", "TRY", "BRL",
    "MXN", "ZAR", "RUB", "PLN", "SEK", "NOK", "DKK",
    "XAU", "XAG", "XPT", "XPD", "GOLD", "SILVER"
}

PIVOT_INTERVALS = {
    "4h": 200,
    "1d": 200,
    "1w": 160,
}

SWEEP_INTERVAL = "1h"
HOUR_LIMIT = 500

MIN_REQUEST_GAP = 1.05
MAX_RETRIES = 5
last_request_at = 0.0

INTERVAL_MS = {
    "1h": 3600000,
    "4h": 14400000,
    "1d": 86400000,
    "1w": 604800000,
}


def wait_for_request():
    global last_request_at
    elapsed = time.time() - last_request_at
    if elapsed < MIN_REQUEST_GAP:
        time.sleep(MIN_REQUEST_GAP - elapsed)
    last_request_at = time.time()


def get_json(path, params=None):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + (("?" + query) if query else "")
    last_error = None

    for attempt in range(MAX_RETRIES):
        wait_for_request()

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 CryptoScanner/1.0"}
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as error:
            last_error = error

            if error.code == 429 or 500 <= error.code < 600:
                retry_after = error.headers.get("Retry-After")

                try:
                    delay = float(retry_after) if retry_after else 0
                except (TypeError, ValueError):
                    delay = 0

                reset_timestamp = error.headers.get(
                    "X-Api-Limit-Reset-Timestamp"
                )

                try:
                    if reset_timestamp:
                        reset_seconds = float(reset_timestamp) / 1000
                        delay = max(delay, reset_seconds - time.time())
                except (TypeError, ValueError):
                    pass

                if delay <= 0:
                    delay = min(15 * (2 ** attempt), 180)

                delay = min(max(delay, 5), 180)

                print(
                    f"Rate/server limit ({error.code}). "
                    f"Waiting {int(delay)} seconds..."
                )

                time.sleep(delay)
                continue

            body = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(
                f"HTTP {error.code}: {body[:250]}"
            ) from error

        except Exception as error:
            last_error = error

            if attempt < MAX_RETRIES - 1:
                delay = min(5 * (2 ** attempt), 60)
                print(f"Request error: {error}. Retry in {delay}s")
                time.sleep(delay)
                continue

            raise

    raise RuntimeError(f"Request failed after retries: {last_error}")


def get_contracts():
    info = get_json("/api/v1/exchangeInfo")

    spot_symbols = info.get("symbols", [])
    contracts = info.get("contracts", [])

    crypto_assets = {
        str(item.get("baseAsset", "")).upper()
        for item in spot_symbols
        if item.get("quoteAsset") == "USDT"
        and item.get("status") == "TRADING"
        and item.get("baseAsset")
    }

    result = []

    for item in contracts:
        symbol = str(item.get("symbol", ""))
        underlying = str(item.get("underlying", "")).upper()

        if not symbol.endswith("-SWAP-USDT"):
            continue

        if item.get("status") != "TRADING":
            continue

        if item.get("quoteAsset") != "USDT":
            continue

        if item.get("inverse") is True:
            continue

        if not underlying or underlying in FIAT_AND_METALS:
            continue

        if underlying not in crypto_assets:
            continue

        result.append(symbol)

    return sorted(set(result))


def get_candles(symbol, interval, limit):
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - INTERVAL_MS[interval] * limit

    rows = get_json(
        "/quote/v1/klines",
        {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": now_ms,
            "limit": limit,
        }
    )

    if not isinstance(rows, list):
        raise RuntimeError("Unexpected kline response format")

    now_ms = int(time.time() * 1000)
    closed = []

    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            continue

        try:
            if int(row[6]) < now_ms:
                closed.append(row)
        except (TypeError, ValueError):
            continue

    closed.sort(key=lambda candle: int(candle[0]))
    return closed


def find_pivots(candles):
    pivots = []

    for index in range(3, len(candles) - 3):
        candle = candles[index]

        high = float(candle[2])
        low = float(candle[3])

        left = candles[index - 3:index]
        right = candles[index + 1:index + 4]

        is_high = all(
            high > float(item[2])
            for item in left + right
        )

        is_low = all(
            low < float(item[3])
            for item in left + right
        )

        confirmed_at = int(right[-1][6])
        pivot_time = int(candle[0])

        if is_high:
            pivots.append({
                "type": "HIGH",
                "price": high,
                "time": pivot_time,
                "confirmed_at": confirmed_at,
            })

        if is_low:
            pivots.append({
                "type": "LOW",
                "price": low,
                "time": pivot_time,
                "confirmed_at": confirmed_at,
            })

    return pivots


def was_already_swept(pivot, hourly_candles, sweep_open_time):
    level = pivot["price"]
    confirmed_at = pivot["confirmed_at"]

    for candle in hourly_candles:
        candle_open = int(candle[0])

        if candle_open <= confirmed_at:
            continue

        if candle_open >= sweep_open_time:
            continue

        high = float(candle[2])
        low = float(candle[3])

        if pivot["type"] == "HIGH" and high > level:
            return True

        if pivot["type"] == "LOW" and low < level:
            return True

    return False


def latest_valid_pivot(pivots, hourly_candles, sweep_open_time):
    oldest_hourly_time = (
        int(hourly_candles[0][0]) if hourly_candles else 0
    )

    for pivot in reversed(pivots):
        if pivot["confirmed_at"] >= sweep_open_time:
            continue

        # اگر تاریخچه کافی برای بررسی پیوت نداریم، آن را رد کن
        if pivot["confirmed_at"] < oldest_hourly_time:
            continue

        if was_already_swept(
            pivot,
            hourly_candles,
            sweep_open_time
        ):
            continue

        return pivot

    return None


def check_sweep(pivot, candle):
    high = float(candle[2])
    low = float(candle[3])
    close = float(candle[4])
    level = pivot["price"]

    # SELL: شدو بالای سقف و بسته‌شدن همان کندل زیر سقف
    if pivot["type"] == "HIGH" and high > level and close < level:
        return "SELL"

    # BUY: شدو زیر کف و بسته‌شدن همان کندل بالای کف
    if pivot["type"] == "LOW" and low < level and close > level:
        return "BUY"

    return None


def make_alert_id(symbol, direction, interval, pivot, sweep_candle):
    return "|".join([
        symbol,
        direction,
        interval,
        str(pivot["time"]),
        str(int(sweep_candle[0])),
    ])


def scan_symbol(symbol):
    hourly = get_candles(
        symbol,
        SWEEP_INTERVAL,
        HOUR_LIMIT
    )

    if len(hourly) < 10:
        return []

    recent_sweeps = hourly[-3:]
    alerts = []

    for interval, limit in PIVOT_INTERVALS.items():
        pivot_candles = get_candles(
            symbol,
            interval,
            limit
        )

        if len(pivot_candles) < 10:
            continue

        pivots = find_pivots(pivot_candles)

        if not pivots:
            continue

        for sweep_candle in recent_sweeps:
            sweep_open_time = int(sweep_candle[0])

            high_pivot = latest_valid_pivot(
                [p for p in pivots if p["type"] == "HIGH"],
                hourly,
                sweep_open_time
            )

            low_pivot = latest_valid_pivot(
                [p for p in pivots if p["type"] == "LOW"],
                hourly,
                sweep_open_time
            )

            for pivot in (high_pivot, low_pivot):
                if not pivot:
                    continue

                direction = check_sweep(pivot, sweep_candle)

                if not direction:
                    continue

                alerts.append({
                    "symbol": symbol,
                    "direction": direction,
                    "interval": interval,
                    "level": pivot["price"],
                    "pivot_time": pivot["time"],
                    "sweep_time": sweep_open_time,
                    "alert_id": make_alert_id(
                        symbol,
                        direction,
                        interval,
                        pivot,
                        sweep_candle
                    ),
                })

    return alerts


def load_state():
    if not STATE_FILE.exists():
        return {"sent": []}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        if not isinstance(data, dict):
            return {"sent": []}

        data.setdefault("sent", [])
        return data

    except Exception:
        return {"sent": []}


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials are missing.")
        return

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(url, data=payload)

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read()

    except Exception as error:
        print(f"Telegram error: {error}")


def format_alert(alert):
    direction_fa = (
        "خرید (BUY)"
        if alert["direction"] == "BUY"
        else "فروش (SELL)"
    )

    return (
        "🔔 SHADOW ALERT\n"
        f"ارز: {alert['symbol']}\n"
        f"جهت: {direction_fa}\n"
        f"تایم‌فریم پیوت: {alert['interval']}\n"
        f"سطح پیوت: {alert['level']}\n"
        "شرط: شدو از سطح عبور کرده و همان کندل "
        "در سمت مخالف سطح بسته شده است."
    )


def main():
    print("Starting SHADOW crypto scanner...")

    symbols = get_contracts()

    print(
        f"Crypto USDT-M contracts after filter: {len(symbols)}"
    )

    state = load_state()
    sent_ids = set(state.get("sent", []))

    checked = 0
    errors = 0
    new_alerts = 0

    for index, symbol in enumerate(symbols, start=1):
        try:
            found = scan_symbol(symbol)
            checked += 1

            for alert in found:
                alert_id = alert["alert_id"]

                if alert_id in sent_ids:
                    continue

                message = format_alert(alert)

                print(message)
                send_telegram(message)

                sent_ids.add(alert_id)
                new_alerts += 1

        except Exception as error:
            errors += 1
            print(f"Error on {symbol}: {error}")

        if index % 25 == 0:
            print(
                f"Progress: {index}/{len(symbols)} | "
                f"Checked: {checked} | Errors: {errors} | "
                f"New alerts: {new_alerts}"
            )

    state["sent"] = list(sent_ids)[-10000:]
    save_state(state)

    print(
        f"Finished. Checked: {checked}, "
        f"Errors: {errors}, New alerts: {new_alerts}"
    )


if __name__ == "__main__":
    main()
