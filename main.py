
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from decimal import Decimal, InvalidOperation

TOOBIT = "https://api.toobit.com"
PAPRIKA = "https://api.coinpaprika.com/v1"

STATE_FILE = Path("scan_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BALE_TOKEN = os.getenv("BALE_TOKEN")
BALE_CHAT_ID = os.getenv("BALE_CHAT_ID")

MIN_RANK = 10
MAX_RANK = 1000

REQUEST_GAP = 2.5
MAX_RETRIES = 2

PIVOT_INTERVALS = {
    "4h": 14400000,
    "1d": 86400000,
    "1w": 604800000,
}

HOUR_MS = 3600000
HOUR_LIMIT = 1000
WEEK_LIMIT = 160

last_toobit_request = 0.0


class RateLimitStop(Exception):
    pass


def wait_toobit():
    global last_toobit_request

    elapsed = time.time() - last_toobit_request

    if elapsed < REQUEST_GAP:
        time.sleep(REQUEST_GAP - elapsed)

    last_toobit_request = time.time()


def request_json(url, toobit=False):
    for attempt in range(MAX_RETRIES):
        if toobit:
            wait_toobit()

        request = urllib.request.Request(
            url,
            headers={"User-Agent": "CryptoScanner/2.0"}
        )

        try:
            with urllib.request.urlopen(
                request,
                timeout=40
            ) as response:
                return json.loads(
                    response.read().decode("utf-8")
                )

        except urllib.error.HTTPError as error:

            if error.code in (418, 403):
                raise RateLimitStop(
                    f"Toobit blocked request: HTTP {error.code}"
                )

            if error.code == 429:
                headers = error.headers

                retry_after = headers.get("Retry-After")
                reset_time = headers.get(
                    "X-Api-Limit-Reset-Timestamp"
                )

                delay = 0

                try:
                    if retry_after:
                        delay = float(retry_after)
                except (ValueError, TypeError):
                    pass

                try:
                    if reset_time:
                        reset_seconds = float(reset_time) / 1000
                        delay = max(
                            delay,
                            reset_seconds - time.time() + 2
                        )
                except (ValueError, TypeError):
                    pass

                if delay <= 0:
                    delay = 60

                if delay > 900:
                    raise RateLimitStop(
                        f"Rate limit reset is too far away: {delay:.0f}s"
                    )

                if attempt == MAX_RETRIES - 1:
                    raise RateLimitStop(
                        "Repeated HTTP 429. Scan stopped safely."
                    )

                print(
                    f"429 received. Waiting {delay:.0f} seconds."
                )

                time.sleep(max(5, delay))
                continue

            if 500 <= error.code < 600:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(15)
                    continue

            body = error.read().decode(
                "utf-8",
                errors="ignore"
            )

            raise RuntimeError(
                f"HTTP {error.code}: {body[:200]}"
            )

        except RateLimitStop:
            raise

        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(10)
                continue
            raise

    raise RuntimeError("Request failed")


def get_market_ranks():
    url = PAPRIKA + "/tickers?quotes=USD"
    rows = request_json(url)

    if not isinstance(rows, list) or not rows:
        raise RuntimeError("CoinPaprika returned no rankings")

    ranked = {}

    for coin in rows:
        try:
            rank = int(coin.get("rank") or 0)
            symbol = str(coin.get("symbol", "")).upper()

            if not symbol:
                continue

            if MIN_RANK <= rank <= MAX_RANK:
                if symbol not in ranked:
                    ranked[symbol] = rank
                else:
                    ranked[symbol] = min(
                        rank,
                        ranked[symbol]
                    )

        except (ValueError, TypeError):
            continue

    if not ranked:
        raise RuntimeError("No coins found in ranks 30-300")

    print(f"Ranked crypto symbols: {len(ranked)}")
    return ranked


def get_toobit_contracts(ranked):
    info = request_json(
        TOOBIT + "/api/v1/exchangeInfo",
        toobit=True
    )

    contracts = info.get("contracts", [])

    if not contracts:
        raise RuntimeError(
            "Toobit returned no futures contracts"
        )

    selected = []

    for item in contracts:
        symbol = str(item.get("symbol", ""))
        underlying = str(
            item.get("underlying", "")
        ).upper()

        categories = item.get("categories") or []

        if not symbol.endswith("-SWAP-USDT"):
            continue

        if item.get("status") != "TRADING":
            continue

        if item.get("quoteAsset") != "USDT":
            continue

        if item.get("inverse") is True:
            continue

        if "TRADFI" in [
            str(category).upper()
            for category in categories
        ]:
            continue

        if underlying not in ranked:
            continue

        selected.append({
            "symbol": symbol,
            "underlying": underlying,
            "rank": ranked[underlying],
        })

    selected.sort(key=lambda x: x["rank"])

    print(
        f"Matching Toobit futures contracts: {len(selected)}"
    )

    return selected


def get_candles(symbol, interval, limit):
    interval_ms = (
        HOUR_MS if interval == "1h"
        else PIVOT_INTERVALS[interval]
    )

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - interval_ms * limit

    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": now_ms,
        "limit": limit,
    })

    url = TOOBIT + "/quote/v1/klines?" + params

    rows = request_json(url, toobit=True)

    if not isinstance(rows, list):
        raise RuntimeError("Invalid kline response")

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


def aggregate_candles(hourly, target_ms, required_count):
    buckets = {}

    for candle in hourly:
        open_time = int(candle[0])
        bucket_time = (
            open_time // target_ms
        ) * target_ms

        buckets.setdefault(bucket_time, []).append(candle)

    result = []

    for bucket_time in sorted(buckets):
        group = sorted(
            buckets[bucket_time],
            key=lambda x: int(x[0])
        )

        if len(group) != required_count:
            continue

        expected_times = [
            bucket_time + i * HOUR_MS
            for i in range(required_count)
        ]

        actual_times = [
            int(item[0]) for item in group
        ]

        if actual_times != expected_times:
            continue

        first = group[0]
        last = group[-1]

        high = max(float(x[2]) for x in group)
        low = min(float(x[3]) for x in group)
        volume = sum(float(x[5]) for x in group)

        result.append([
            bucket_time,
            first[1],
            str(high),
            str(low),
            last[4],
            str(volume),
            0,
        ])

    return result


# ==================================================
# NEW SHADOW PIVOT DETECTION
# ==================================================

def find_pivots(candles, interval_ms):
    pivots = []

    if len(candles) < 7:
        return pivots

    # PIVOT LOW:
    # First: 3 lower lows.
    # Then: 3 higher highs.
    #
    # PIVOT HIGH:
    # First: 3 higher highs.
    # Then: 3 lower lows.
    #
    # Events do not need to be consecutive.
    # Wick and body breaks are both counted.
    # Each new event must break the last counted level.

    low_count = 0
    low_reference = float(candles[0][3])
    low_reference_time = int(candles[0][0])

    low_phase = False
    low_high_count = 0
    low_high_reference = None
    low_pivot_confirmed = False

    high_count = 0
    high_reference = float(candles[0][2])
    high_reference_time = int(candles[0][0])

    high_phase = False
    high_low_count = 0
    high_low_reference = None
    high_pivot_confirmed = False

    for candle in candles[1:]:
        candle_time = int(candle[0])
        candle_high = float(candle[2])
        candle_low = float(candle[3])

        # ------------------------------------------
        # PIVOT LOW DETECTION
        # ------------------------------------------

        if not low_phase:

            if candle_low < low_reference:
                low_count += 1
                low_reference = candle_low
                low_reference_time = candle_time

                low_high_reference = candle_high

                if low_count >= 3:
                    low_phase = True
                    low_high_count = 0
                    low_pivot_confirmed = False

        else:

            # A new lower low restarts the first phase.
            if candle_low < low_reference:

                low_count = 1
                low_reference = candle_low
                low_reference_time = candle_time

                low_high_reference = candle_high
                low_high_count = 0
                low_pivot_confirmed = False

            elif candle_high > low_high_reference:

                low_high_count += 1
                low_high_reference = candle_high

                if (
                    low_high_count >= 3
                    and not low_pivot_confirmed
                ):
                    pivots.append({
                        "type": "LOW",
                        "price": low_reference,
                        "time": low_reference_time,
                        "confirmed_at": (
                            candle_time + interval_ms
                        ),
                    })

                    low_pivot_confirmed = True

        # ------------------------------------------
        # PIVOT HIGH DETECTION
        # ------------------------------------------

        if not high_phase:

            if candle_high > high_reference:
                high_count += 1
                high_reference = candle_high
                high_reference_time = candle_time

                high_low_reference = candle_low

                if high_count >= 3:
                    high_phase = True
                    high_low_count = 0
                    high_pivot_confirmed = False

        else:

            # A new higher high restarts the first phase.
            if candle_high > high_reference:

                high_count = 1
                high_reference = candle_high
                high_reference_time = candle_time

                high_low_reference = candle_low
                high_low_count = 0
                high_pivot_confirmed = False

            elif candle_low < high_low_reference:

                high_low_count += 1
                high_low_reference = candle_low

                if (
                    high_low_count >= 3
                    and not high_pivot_confirmed
                ):
                    pivots.append({
                        "type": "HIGH",
                        "price": high_reference,
                        "time": high_reference_time,
                        "confirmed_at": (
                            candle_time + interval_ms
                        ),
                    })

                    high_pivot_confirmed = True

    pivots.sort(
        key=lambda p: p["confirmed_at"]
    )

    return pivots


def was_already_swept(
    pivot,
    hourly,
    sweep_open_time
):
    level = pivot["price"]

    for candle in hourly:
        candle_time = int(candle[0])

        if candle_time <= pivot["confirmed_at"]:
            continue

        if candle_time >= sweep_open_time:
            continue

        high = float(candle[2])
        low = float(candle[3])

        if pivot["type"] == "HIGH" and high > level:
            return True

        if pivot["type"] == "LOW" and low < level:
            return True

    return False


def latest_valid_pivot(
    pivots,
    hourly,
    sweep_open_time
):
    if not hourly:
        return None

    oldest_time = int(hourly[0][0])

    for pivot in reversed(pivots):

        if pivot["confirmed_at"] >= sweep_open_time:
            continue

        if pivot["confirmed_at"] < oldest_time:
            continue

        if was_already_swept(
            pivot,
            hourly,
            sweep_open_time
        ):
            continue

        return pivot

    return None


# ==================================================
# SHADOW SAME-CANDLE SWEEP
# ==================================================

def check_sweep(pivot, candle):
    try:
        high = Decimal(str(candle[2]))
        low = Decimal(str(candle[3]))
        close = Decimal(str(candle[4]))
        level = Decimal(str(pivot["price"]))

    except (InvalidOperation, ValueError, TypeError, IndexError):
        return None

    # BUY:
    # Wick goes below pivot low.
    # Same candle closes above pivot low.

    if pivot["type"] == "LOW":
        if low < level and close > level:
            return "BUY"

    # SELL:
    # Wick goes above pivot high.
    # Same candle closes below pivot high.

    if pivot["type"] == "HIGH":
        if high > level and close < level:
            return "SELL"

    return None


def make_alert_id(
    symbol,
    interval,
    pivot,
    sweep_interval
):
    return "|".join([
        symbol,
        interval,
        pivot["type"],
        str(pivot["time"]),
        sweep_interval,
    ])


def was_sent_in_old_format(
    sent_ids,
    symbol,
    direction,
    interval,
    sweep_interval,
    pivot
):
    old_prefix = "|".join([
        symbol,
        direction,
        interval,
        sweep_interval,
        str(pivot["time"]),
    ]) + "|"

    return any(
        str(old_id).startswith(old_prefix)
        for old_id in sent_ids
    )


def scan_symbol(item):
    symbol = item["symbol"]

    hourly = get_candles(
        symbol,
        "1h",
        HOUR_LIMIT
    )

    if len(hourly) < 100:
        return []

    four_hour = aggregate_candles(
        hourly,
        4 * HOUR_MS,
        4
    )

    daily = aggregate_candles(
        hourly,
        24 * HOUR_MS,
        24
    )

    weekly = get_candles(
        symbol,
        "1w",
        WEEK_LIMIT
    )

    pivot_sets = {
        "4h": four_hour,
        "1d": daily,
        "1w": weekly,
    }

    sweep_sets = {
        "1h": hourly[-3:],
        "4h": four_hour[-2:],
    }

    alerts = []
    found_ids = set()

    for sweep_interval, sweep_candles in sweep_sets.items():

        for interval, candles in pivot_sets.items():

            if len(candles) < 10:
                continue

            pivots = find_pivots(
                candles,
                PIVOT_INTERVALS[interval]
            )

            if not pivots:
                continue

            for sweep_candle in sweep_candles:
                sweep_time = int(sweep_candle[0])

                for pivot_type in ("HIGH", "LOW"):

                    candidates = [
                        p for p in pivots
                        if p["type"] == pivot_type
                    ]

                    pivot = latest_valid_pivot(
                        candidates,
                        hourly,
                        sweep_time
                    )

                    if not pivot:
                        continue

                    direction = check_sweep(
                        pivot,
                        sweep_candle
                    )

                    if not direction:
                        continue

                    alert_id = make_alert_id(
                        symbol,
                        interval,
                        pivot,
                        sweep_interval
                    )

                    if alert_id in found_ids:
                        continue

                    found_ids.add(alert_id)

                    alerts.append({
                        "id": alert_id,
                        "symbol": symbol,
                        "rank": item["rank"],
                        "direction": direction,
                        "interval": interval,
                        "sweep_interval": sweep_interval,
                        "level": pivot["price"],
                        "pivot_type": pivot["type"],
                        "pivot_time": pivot["time"],
                        "sweep_time": sweep_time,
                        "sweep_high": float(sweep_candle[2]),
                        "sweep_low": float(sweep_candle[3]),
                        "sweep_close": float(sweep_candle[4]),
                    })

    return alerts


def load_state():
    if not STATE_FILE.exists():
        return {"sent": []}

    try:
        with STATE_FILE.open(
            "r",
            encoding="utf-8"
        ) as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return {"sent": []}

        state.setdefault("sent", [])
        return state

    except Exception:
        return {"sent": []}


def save_state(state):
    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )


def send_telegram(message):
    # Telegram
    if TOKEN and CHAT_ID:
        url = (
            "https://api.telegram.org/bot"
            + TOKEN
            + "/sendMessage"
        )

        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": message,
        }).encode("utf-8")

        request = urllib.request.Request(url, data=data)

        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            raise RuntimeError("Telegram rejected message")

    # Bale
    if BALE_TOKEN and BALE_CHAT_ID:
        url = (
            "https://tapi.bale.ai/bot"
            + BALE_TOKEN
            + "/sendMessage"
        )

        data = urllib.parse.urlencode({
            "chat_id": BALE_CHAT_ID,
            "text": message,
        }).encode("utf-8")

        request = urllib.request.Request(url, data=data)

        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))

        if not result.get("ok"):
            raise RuntimeError("Bale rejected message")



def format_alert(alert):
    direction = (
        "خرید BUY"
        if alert["direction"] == "BUY"
        else "فروش SELL"
    )

    if alert["direction"] == "BUY":
        sweep_price = alert["sweep_low"]
    else:
        sweep_price = alert["sweep_high"]

    return (
        "🔔 SHADOW ALERT\n"
        f"ارز: {alert['symbol']}\n"
        f"رتبه بازار: {alert['rank']}\n"
        f"جهت: {direction}\n"
        f"تایم‌فریم کندل شکار: {alert['sweep_interval']}\n"
        f"تایم‌فریم پیوت: {alert['interval']}\n"
        f"نوع پیوت: {alert['pivot_type']}\n"
        f"قیمت پیوت: {alert['level']}\n"
        f"قیمت شدو: {sweep_price}\n"
        f"کلوز همان کندل: {alert['sweep_close']}\n"
        "شرط: شدو از پیوت عبور کرده و همان کندل "
        "در سمت مخالف پیوت بسته شده است."
    )


def main():
    print("Starting SHADOW rank 30-300 scanner")
    print("Sweep timeframes: 1h and 4h")
    print("Pivot timeframes: 4h, 1d and 1w")
    print("Pivot rule: 3 breaks then 3 reverse breaks")
    print("Break rule: wick or body")
    print("Candles do not need to be consecutive")
    print("Close rule: same sweep candle must close beyond pivot")

    state = load_state()
    sent_ids = set(state.get("sent", []))

    checked = 0
    errors = 0
    new_alerts = 0
    stopped_by_limit = False

    try:
        ranked = get_market_ranks()
        contracts = get_toobit_contracts(ranked)

    except RateLimitStop as error:
        print(f"STOPPED: {error}")
        return

    except Exception as error:
        print(f"STARTUP ERROR: {error}")
        return

    if not contracts:
        print("No matching Toobit contracts.")
        return

    for index, item in enumerate(contracts, start=1):
        symbol = item["symbol"]

        try:
            alerts = scan_symbol(item)
            checked += 1

            for alert in alerts:
                alert_id = alert["id"]

                if alert_id in sent_ids:
                    continue

                if was_sent_in_old_format(
                    sent_ids,
                    alert["symbol"],
                    alert["direction"],
                    alert["interval"],
                    alert["sweep_interval"],
                    {
                        "time": alert["pivot_time"]
                    }
                ):
                    sent_ids.add(alert_id)
                    continue

                message = format_alert(alert)

                print(message)

                send_telegram(message)

                sent_ids.add(alert_id)
                new_alerts += 1

                state["sent"] = list(sent_ids)[-10000:]
                save_state(state)

        except RateLimitStop as error:
            print(f"SCAN STOPPED: {error}")
            stopped_by_limit = True
            break

        except Exception as error:
            errors += 1
            print(f"Error on {symbol}: {error}")

        if index % 10 == 0:
            print(
                f"Progress: {index}/{len(contracts)} | "
                f"Checked: {checked} | "
                f"Errors: {errors} | "
                f"Alerts: {new_alerts}"
            )

    state["sent"] = list(sent_ids)[-10000:]
    save_state(state)

    status = (
        "PARTIAL - RATE LIMIT"
        if stopped_by_limit
        else "COMPLETE"
    )

    print(
        f"Finished: {status} | "
        f"Selected: {len(contracts)} | "
        f"Checked: {checked} | "
        f"Errors: {errors} | "
        f"New alerts: {new_alerts}"
    )


if __name__ == "__main__":
    main()
