
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

# =========================
# SURPRISE PRO CONFIG
# =========================

TOOBIT = "https://api.toobit.com"
PAPRIKA = "https://api.coinpaprika.com/v1"

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_RANK = 10
MAX_RANK = 1000

TIMEFRAMES = {
    "1h": 3600000,
    "4h": 14400000,
    "1d": 86400000,
}

STATE_FILE = Path("surprise_pro_state.json")

REQUEST_GAP = 2.5
MAX_RETRIES = 2
last_request = 0.0


class RateLimitStop(Exception):
    pass


# =========================
# HTTP
# =========================

def request_json(url, toobit=False):
    global last_request

    for attempt in range(MAX_RETRIES):
        if toobit:
            elapsed = time.time() - last_request
            if elapsed < REQUEST_GAP:
                time.sleep(REQUEST_GAP - elapsed)
            last_request = time.time()

        req = urllib.request.Request(
            url,
            headers={"User-Agent": "SurpriseProScanner/1.0"}
        )

        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                return json.loads(response.read().decode("utf-8"))

        except urllib.error.HTTPError as error:
            if error.code in (403, 418):
                raise RateLimitStop(
                    f"Toobit blocked request: HTTP {error.code}"
                )

            if error.code == 429:
                if attempt == MAX_RETRIES - 1:
                    raise RateLimitStop("Repeated HTTP 429")

                retry = error.headers.get("Retry-After")
                try:
                    delay = max(10, float(retry or 60))
                except (ValueError, TypeError):
                    delay = 60

                if delay > 900:
                    raise RateLimitStop(
                        f"Rate limit delay too long: {delay}"
                    )

                print(f"429: waiting {delay:.0f}s")
                time.sleep(delay)
                continue

            if 500 <= error.code < 600 and attempt < MAX_RETRIES - 1:
                time.sleep(15)
                continue

            raise

        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(10)
                continue
            raise

    raise RuntimeError("Request failed")


# =========================
# MARKET RANKS
# =========================

def get_market_ranks():
    rows = request_json(PAPRIKA + "/tickers?quotes=USD")

    if not isinstance(rows, list):
        raise RuntimeError("Invalid CoinPaprika response")

    ranked = {}

    for coin in rows:
        try:
            rank = int(coin.get("rank") or 0)
            symbol = str(coin.get("symbol", "")).upper()

            if symbol and MIN_RANK <= rank <= MAX_RANK:
                if symbol not in ranked:
                    ranked[symbol] = rank
                else:
                    ranked[symbol] = min(ranked[symbol], rank)

        except (ValueError, TypeError):
            continue

    print(f"Ranked symbols: {len(ranked)}")
    return ranked


# =========================
# TOOBIT CONTRACTS
# =========================

def get_contracts(ranked):
    info = request_json(
        TOOBIT + "/api/v1/exchangeInfo",
        toobit=True
    )

    selected = []

    for item in info.get("contracts", []):
        symbol = str(item.get("symbol", ""))
        underlying = str(item.get("underlying", "")).upper()
        categories = item.get("categories") or []

        if not symbol.endswith("-SWAP-USDT"):
            continue

        if item.get("status") != "TRADING":
            continue

        if item.get("quoteAsset") != "USDT":
            continue

        if item.get("inverse") is True:
            continue

        if "TRADFI" in [str(x).upper() for x in categories]:
            continue

        if underlying not in ranked:
            continue

        selected.append({
            "symbol": symbol,
            "underlying": underlying,
            "rank": ranked[underlying],
        })

    selected.sort(key=lambda x: x["rank"])

    print(f"Matching Toobit contracts: {len(selected)}")
    return selected


# =========================
# CANDLES
# =========================

def get_candles(symbol, interval, limit=300):
    interval_ms = TIMEFRAMES[interval]
    now_ms = int(time.time() * 1000)

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": now_ms - interval_ms * limit,
        "endTime": now_ms,
        "limit": limit,
    })

    url = TOOBIT + "/quote/v1/klines?" + params
    rows = request_json(url, toobit=True)

    if not isinstance(rows, list):
        raise RuntimeError("Invalid candle response")

    now_ms = int(time.time() * 1000)
    closed = []

    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue

        try:
            open_time = int(row[0])

            if open_time + interval_ms <= now_ms:
                closed.append(row)

        except (ValueError, TypeError):
            continue

    closed.sort(key=lambda x: int(x[0]))
    return closed


# =========================
# CANDLE HELPERS
# =========================

def candle_values(candle):
    o = float(candle[1])
    h = float(candle[2])
    l = float(candle[3])
    c = float(candle[4])

    return o, h, l, c


def candle_direction(candle):
    o, h, l, c = candle_values(candle)

    if c > o:
        return "BUY"

    if c < o:
        return "SELL"

    return None


def is_strong(candle):
    o, h, l, c = candle_values(candle)

    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    total_wick = upper_wick + lower_wick

    return body > 0 and body >= 4 * total_wick


# =========================
# SURPRISE PRO PATTERN
# =========================

def find_signals(candles, item, interval):
    signals = []

    if len(candles) < 5:
        return signals

    # Only evaluate setups whose confirmation
    # candle is among the latest three closed candles.
    start_i = max(1, len(candles) - 5)
    end_i = len(candles) - 2

    for i in range(start_i, end_i + 1):
        first = candles[i]
        direction = candle_direction(first)

        if direction is None or not is_strong(first):
            continue

        # Find the opposite candle immediately before
        # the range and include its full wick range.
        opposite_direction = (
            "SELL" if direction == "BUY" else "BUY"
        )

        k = i - 1

        while k >= 0:
            if candle_direction(candles[k]) == opposite_direction:
                break
            k -= 1

        if k < 0:
            continue

        # Every candle from the opposite candle through
        # the candle before the first impulse is included.
        range_candles = candles[k:i]

        range_high = max(float(x[2]) for x in range_candles)
        range_low = min(float(x[3]) for x in range_candles)

        _, first_high, first_low, first_close = candle_values(first)

        # First impulse must cover the complete range,
        # including all wicks, and close beyond it.
        if direction == "BUY":
            if first_high < range_high:
                continue
            if first_low > range_low:
                continue
            if first_close <= range_high:
                continue

        else:
            if first_high < range_high:
                continue
            if first_low > range_low:
                continue
            if first_close >= range_low:
                continue

        # Second candle must continue in same direction.
        second_index = i + 1

        if second_index >= len(candles):
            continue

        second = candles[second_index]

        if candle_direction(second) != direction:
            continue

        confirmation = None
        confirmation_index = None

        # Strong second candle: alert immediately.
        if is_strong(second):
            confirmation = second
            confirmation_index = second_index

        else:
            # Weak second candle is allowed only once.
            third_index = i + 2

            if third_index >= len(candles):
                continue

            third = candles[third_index]

            if candle_direction(third) != direction:
                continue

            if not is_strong(third):
                continue

            confirmation = third
            confirmation_index = third_index

        confirmation_time = int(confirmation[0])

        alert_id = "|".join([
            item["symbol"],
            interval,
            direction,
            str(confirmation_time),
            str(int(first[0])),
        ])

        signals.append({
            "id": alert_id,
            "symbol": item["symbol"],
            "rank": item["rank"],
            "direction": direction,
            "interval": interval,
            "opposite_high": float(candles[k][2]),
            "opposite_low": float(candles[k][3]),
            "range_high": range_high,
            "range_low": range_low,
            "first_time": int(first[0]),
            "confirmation_time": confirmation_time,
            "confirmation_index": confirmation_index,
        })

    return signals


# =========================
# STATE
# =========================

def load_state():
    if not STATE_FILE.exists():
        return {"sent": []}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return {"sent": []}

        state.setdefault("sent", [])
        return state

    except Exception:
        return {"sent": []}


def save_state(state):
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(
            state,
            f,
            ensure_ascii=False,
            indent=2
        )


# =========================
# TELEGRAM
# =========================

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram credentials missing")

    url = (
        "https://api.telegram.org/bot"
        + TOKEN
        + "/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode("utf-8")

    req = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(req, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")


def format_alert(alert):
    direction = (
        "خرید BUY"
        if alert["direction"] == "BUY"
        else "فروش SELL"
    )

    return (
        "🚨 SURPRISE PRO ALERT\n\n"
        f"ارز: {alert['symbol']}\n"
        f"رتبه بازار: {alert['rank']}\n"
        f"جهت: {direction}\n"
        f"تایم‌فریم: {alert['interval']}\n\n"
        "محدوده کندل مخالف اولیه:\n"
        f"High: {alert['opposite_high']}\n"
        f"Low: {alert['opposite_low']}\n\n"
        "محدوده کامل رنج پوشش داده شده:\n"
        f"High: {alert['range_high']}\n"
        f"Low: {alert['range_low']}\n\n"
        f"زمان کندل اول شارپ: {alert['first_time']}\n"
        f"زمان تأیید نهایی: {alert['confirmation_time']}\n\n"
        "شرط: بدنه کندل اول حداقل ۴ برابر مجموع شدوها، "
        "پوشش کامل رنج و تأیید کندل دوم یا سوم."
    )


# =========================
# SCAN
# =========================

def main():
    print("Starting SURPRISE PRO scanner")
    print("Rank range: 10-1000")
    print("Timeframes: 1h, 4h, 1d")
    print("Impulse ratio: body >= 4x total wicks")

    state = load_state()
    sent_ids = set(state["sent"])

    checked = 0
    errors = 0
    new_alerts = 0
    stopped = False

    try:
        ranked = get_market_ranks()
        contracts = get_contracts(ranked)

    except Exception as error:
        print(f"STARTUP ERROR: {error}")
        return

    for index, item in enumerate(contracts, start=1):
        try:
            for interval in TIMEFRAMES:
                candles = get_candles(
                    item["symbol"],
                    interval,
                    300
                )

                alerts = find_signals(
                    candles,
                    item,
                    interval
                )

                for alert in alerts:
                    if alert["id"] in sent_ids:
                        continue

                    message = format_alert(alert)

                    print(message)
                    send_telegram(message)

                    sent_ids.add(alert["id"])
                    new_alerts += 1

                    state["sent"] = list(sent_ids)[-10000:]
                    save_state(state)

            checked += 1

        except RateLimitStop as error:
            print(f"SCAN STOPPED: {error}")
            stopped = True
            break

        except Exception as error:
            errors += 1
            print(f"Error on {item['symbol']}: {error}")

        if index % 10 == 0:
            print(
                f"Progress: {index}/{len(contracts)} | "
                f"Checked: {checked} | "
                f"Errors: {errors} | "
                f"Alerts: {new_alerts}"
            )

    state["sent"] = list(sent_ids)[-10000:]
    save_state(state)

    status = "PARTIAL - RATE LIMIT" if stopped else "COMPLETE"

    print(
        f"Finished: {status} | "
        f"Selected: {len(contracts)} | "
        f"Checked: {checked} | "
        f"Errors: {errors} | "
        f"New alerts: {new_alerts}"
    )


if __name__ == "__main__":
    main()
