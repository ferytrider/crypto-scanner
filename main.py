
import os
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = "https://api.toobit.com"
STATE_FILE = Path("scan_state.json")

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

INTERVAL_MS = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}

def api_get(path, params=None):
    query = urllib.parse.urlencode(params or {})
    url = BASE + path + ("?" + query if query else "")
    req = urllib.request.Request(
        url, headers={"User-Agent": "crypto-scanner/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

def get_server_time():
    data = api_get("/api/v1/time")
    return int(data["serverTime"])

def get_symbols():
    data = api_get("/api/v1/exchangeInfo")
    contracts = data.get("contracts", [])

    symbols = []
    for item in contracts:
        symbol = item.get("symbol", "")
        inverse = item.get("inverse", False)

        if (
            item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and not inverse
            and symbol.endswith("-SWAP-USDT")
        ):
            symbols.append(symbol)

    return sorted(set(symbols))

def get_closed_candles(symbol, interval, server_time, limit=40):
    data = api_get(
        "/quote/v1/klines",
        {"symbol": symbol, "interval": interval, "limit": limit}
    )

    if isinstance(data, dict):
        data = data.get("data", [])

    duration = INTERVAL_MS[interval]
    candles = []

    for row in data:
        if len(row) < 5:
            continue

        candle = {
            "time": int(row[0]),
            "open": float(row[1]),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }

        # فقط کندل‌هایی که کاملاً بسته شده‌اند
        if candle["time"] + duration <= server_time:
            candles.append(candle)

    return sorted(candles, key=lambda x: x["time"])

def candle_direction(c):
    if c["close"] > c["open"]:
        return "bull"
    if c["close"] < c["open"]:
        return "bear"
    return "doji"

def body_size(c):
    return abs(c["close"] - c["open"])

def is_powerful(candles, index):
    c = candles[index]
    body = body_size(c)

    upper_wick = c["high"] - max(c["open"], c["close"])
    lower_wick = min(c["open"], c["close"]) - c["low"]
    wick_sum = max(0, upper_wick) + max(0, lower_wick)

    # حداقل 10 کندل قبل از کندل مورد بررسی لازم است
    previous = candles[max(0, index - 10):index]
    if len(previous) < 10:
        return False

    average_body = sum(body_size(x) for x in previous) / 10

    return (
        body > 0
        and body >= 3 * wick_sum
        and body >= 2 * average_body
    )

def scan_surprise(candles):
    if len(candles) < 13:
        return None

    last_index = len(candles) - 1
    direction = candle_direction(candles[last_index])

    if direction not in ("bull", "bear"):
        return None

    # تعداد کندل‌های قدرتمند هم‌رنگ در انتهای داده
    start = last_index
    while start >= 0:
        if (
            candle_direction(candles[start]) == direction
            and is_powerful(candles, start)
        ):
            start -= 1
        else:
            break

    first_impulse = start + 1
    impulse_count = last_index - first_impulse + 1

    # حداقل دو کندل قدرتمند متوالی
    if impulse_count < 2 or start < 0:
        return None

    opposite = candles[start]
    expected_opposite = "bear" if direction == "bull" else "bull"

    if candle_direction(opposite) != expected_opposite:
        return None

    return {
        "time": candles[last_index]["time"],
        "direction": direction,
        "count": impulse_count,
        "zone_high": opposite["high"],
        "zone_low": opposite["low"],
    }

def scan_shadow(candles_1h, references):
    if not candles_1h:
        return None

    c = candles_1h[-1]
    results = []

    for timeframe, levels in references.items():
        high_level = levels["high"]
        low_level = levels["low"]

        # سایه از سقف عبور کرده و کندل زیر سقف بسته شده
        if c["high"] > high_level and c["close"] < high_level:
            results.append(f"فروش: سقف {timeframe} = {high_level}")

        # سایه از کف عبور کرده و کندل بالای کف بسته شده
        if c["low"] < low_level and c["close"] > low_level:
            results.append(f"خرید: کف {timeframe} = {low_level}")

    if not results:
        return None

    return {
        "time": c["time"],
        "details": results,
    }

def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("TELEGRAM_TOKEN یا TELEGRAM_CHAT_ID تنظیم نشده است.")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")

def load_state():
    if not STATE_FILE.exists():
        return {}, False

    try:
        return json.loads(STATE_FILE.read_text()), True
    except Exception:
        return {}, False

def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2)
    )

def scan_symbol(symbol, server_time):
    candles_1h = get_closed_candles(
        symbol, "1h", server_time, 40
    )

    surprise = scan_surprise(candles_1h)

    references = {}
    for timeframe in ("4h", "1d", "1w"):
        candles = get_closed_candles(
            symbol, timeframe, server_time, 5
        )
        if candles:
            last = candles[-1]
            references[timeframe] = {
                "high": last["high"],
                "low": last["low"],
            }

    shadow = None
    if len(references) == 3:
        shadow = scan_shadow(candles_1h, references)

    return symbol, surprise, shadow

def main():
    server_time = get_server_time()
    symbols = get_symbols()

    print(f"Found {len(symbols)} USDT futures contracts.")

    state, state_exists = load_state()
    new_messages = []
    checked = 0
    errors = 0

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(scan_symbol, symbol, server_time): symbol
            for symbol in symbols
        }

        for future in as_completed(futures):
            symbol = futures[future]

            try:
                symbol, surprise, shadow = future.result()
                checked += 1

                surprise_key = f"{symbol}|SURPRISE"
                shadow_key = f"{symbol}|SHADOW"

                if surprise:
                    event_time = surprise["time"]
                    old_time = int(state.get(surprise_key, 0))

                    if state_exists and event_time > old_time:
                        direction = (
                            "صعودی" if surprise["direction"] == "bull"
                            else "نزولی"
                        )
                        message = (
                            f"🚨 SURPRISE | {symbol}\n"
                            f"جهت: {direction}\n"
                            f"تعداد کندل قدرتمند: {surprise['count']}\n"
                            f"محدوده کندل مخالف:\n"
                            f"کف: {surprise['zone_low']}\n"
                            f"سقف: {surprise['zone_high']}\n"
                            f"تایم‌فریم: 1H\n"
                            f"⏳ بررسی برگشت قیمت با خودت"
                        )
                        new_messages.append(message)

                    state[surprise_key] = max(
                        old_time, event_time
                    )

                if shadow:
                    event_time = shadow["time"]
                    old_time = int(state.get(shadow_key, 0))

                    if state_exists and event_time > old_time:
                        message = (
                            f"🎯 SHADOW | {symbol}\n"
                            f"تأیید کندل بسته‌شده 1H:\n"
                            + "\n".join(shadow["details"])
                        )
                        new_messages.append(message)

                    state[shadow_key] = max(
                        old_time, event_time
                    )

            except Exception as error:
                errors += 1
                print(f"Error on {symbol}: {error}")

    # اولین اجرا فقط وضعیت فعلی را ذخیره می‌کند و پیام قدیمی نمی‌فرستد
    for message in new_messages:
        send_telegram(message)
        time.sleep(0.2)

    save_state(state)

    print(
        f"Finished. Checked: {checked}, "
        f"Errors: {errors}, New alerts: {len(new_messages)}"
    )

if __name__ == "__main__":
    main()
