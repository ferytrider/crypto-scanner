
import os
import json
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.toobit.com"

TIMEFRAMES = {
    "1h": 3600000,
    "4h": 14400000,
    "1d": 86400000
}

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "CryptoScanner/1.0"}
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets missing")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    req = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(req, timeout=20) as r:
        result = json.loads(r.read().decode())

    if not result.get("ok"):
        raise RuntimeError("Telegram send failed")


def get_symbols():
    data = get_json(BASE + "/api/v1/exchangeInfo")

    contracts = data.get("contracts", [])

    return [
        c["symbol"] for c in contracts
        if c.get("quoteAsset") == "USDT"
        and c.get("status") == "TRADING"
        and c.get("inverse") is False
        and c.get("symbol", "").endswith("-SWAP-USDT")
    ]


def check_candle(c):
    o = float(c[1])
    h = float(c[2])
    l = float(c[3])
    close = float(c[4])

    body = abs(close - o)
    upper = h - max(o, close)
    lower = min(o, close) - l

    if body <= 0:
        return None

    if body >= 3 * (upper + lower):
        return "صعودی 🟢" if close > o else "نزولی 🔴"

    return None


def scan_one(symbol, interval, duration, now):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "startTime": now - duration * 5,
        "endTime": now,
        "limit": 5
    })

    candles = get_json(
        BASE + "/quote/v1/klines?" + params
    )

    if not isinstance(candles, list):
        return None

    closed = [
        c for c in candles
        if int(c[0]) + duration <= now
    ]

    if len(closed) < 2:
        return None

    first = check_candle(closed[-2])
    second = check_candle(closed[-1])

    if first and second:
        return {
            "symbol": symbol,
            "timeframe": interval,
            "first": first,
            "second": second
        }

    return None


def main():
    now = int(
        get_json(BASE + "/api/v1/time")["serverTime"]
    )

    symbols = get_symbols()

    print("Total USDT futures:", len(symbols))

    results = []
    errors = 0

    with ThreadPoolExecutor(max_workers=4) as pool:
        tasks = []

        for symbol in symbols:
            for interval, duration in TIMEFRAMES.items():
                tasks.append(
                    pool.submit(
                        scan_one,
                        symbol,
                        interval,
                        duration,
                        now
                    )
                )

        for task in as_completed(tasks):
            try:
                result = task.result()
                if result:
                    results.append(result)
            except Exception as e:
                errors += 1
                print("Error:", str(e))

    print("Scan finished:", len(results))
    print("Errors:", errors)

    if not results:
        print("No valid patterns found.")
        return

    message = "CRYPTO SCANNER\n"
    message += "Two consecutive strong candles\n\n"

    for r in results:
        message += (
            f"Coin: {r['symbol']}\n"
            f"Timeframe: {r['timeframe']}\n"
            f"Candle 1: {r['first']}\n"
            f"Candle 2: {r['second']}\n"
            "--------------------\n"
        )

    for i in range(0, len(message), 3500):
        send_telegram(message[i:i + 3500])


if __name__ == "__main__":
    main()
