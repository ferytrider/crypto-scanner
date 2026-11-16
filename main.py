
import os
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

TOOBIT = "https://api.toobit.com"
PAPRIKA = "https://api.coinpaprika.com/v1"

STATE_FILE = Path("scan_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_RANK = 30
MAX_RANK = 300

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
    global last_toobit_request

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
        raise RuntimeError("No coins found in ranks 50-250")

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

            # A candle is closed only after its interval ends.
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

        # Only use complete higher-timeframe candles.
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


def find_pivots(candles, interval_ms):
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

        confirmed_at = (
            int(right[-1][0]) + interval_ms
        )

        if is_high:
            pivots.append({
                "type": "HIGH",
                "price": high,
                "time": int(candle[0]),
                "confirmed_at": confirmed_at,
            })

        if is_low:
            pivots.append({
                "type": "LOW",
                "price": low,
                "time": int(candle[0]),
                "confirmed_at": confirmed_at,
            })

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

        # Do not use pivots whose full sweep history
        # is outside the available hourly data.
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


def check_sweep(pivot, candle):
    high = float(candle[2])
    low = float(candle[3])
    close = float(candle[4])

    level = pivot["price"]

    if (
        pivot["type"] == "HIGH"
        and high > level
        and close < level
    ):
        return "SELL"

    if (
        pivot["type"] == "LOW"
        and low < level
        and close > level
    ):
        return "BUY"

    return None


def make_alert_id(
    symbol,
    direction,
    interval,
    pivot,
    candle
):
    return "|".join([
        symbol,
        direction,
        interval,
        str(pivot["time"]),
        str(int(candle[0])),
    ])


def scan_symbol(item):
    symbol = item["symbol"]

    # One hourly request is used for 1h, 4h and 1d.
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

    # Weekly candles are fetched separately.
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

    recent_sweeps = hourly[-3:]
    alerts = []

    for interval, candles in pivot_sets.items():

        if len(candles) < 10:
            continue

        pivots = find_pivots(
            candles,
            PIVOT_INTERVALS[interval]
        )

        if not pivots:
            continue

        for sweep_candle in recent_sweeps:
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
                    direction,
                    interval,
                    pivot,
                    sweep_candle
                )

                alerts.append({
                    "id": alert_id,
                    "symbol": symbol,
                    "rank": item["rank"],
                    "direction": direction,
                    "interval": interval,
                    "level": pivot["price"],
                    "pivot_time": pivot["time"],
                    "sweep_time": sweep_time,
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
    if not TOKEN or not CHAT_ID:
        print("Telegram credentials missing.")
        return

    url = (
        "https://api.telegram.org/bot"
        + TOKEN
        + "/sendMessage"
    )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=data
    )

    with urllib.request.urlopen(
        request,
        timeout=25
    ) as response:
        result = json.loads(
            response.read().decode("utf-8")
        )

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected message")


def format_alert(alert):
    direction = (
        "خرید BUY"
        if alert["direction"] == "BUY"
        else "فروش SELL"
    )

    return (
        "🔔 SHADOW ALERT\n"
        f"ارز: {alert['symbol']}\n"
        f"رتبه بازار: {alert['rank']}\n"
        f"جهت: {direction}\n"
        f"تایم‌فریم پیوت: {alert['interval']}\n"
        f"قیمت پیوت: {alert['level']}\n"
        "شرط: شدو از سطح عبور کرده و همان کندل "
        "در سمت مخالف سطح بسته شده است."
    )


def main():
    print("Starting SHADOW rank 50-250 scanner")

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
