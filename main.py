
import os
import json
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://api.toobit.com"
TIMEFRAMES = {
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000
}

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "CryptoScanner/1.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError("Telegram secrets are missing")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


def get_symbols():
    data = get_json(BASE + "/api/v1/exchangeInfo")
    return [
        c["symbol"] for c in data["contracts"]
        if c.get("quoteAsset") == "USDT"
        and c.get("status") == "TRADING"
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
        return "BULLISH" if close > o else "BEARISH"

    return None


def scan_one(symbol, interval, duration):
    params = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "limit": 10
    })

    candles = get_json(
        BASE + "/quote/v1/klines?" + params
    )

    if not isinstance(candles, list):
        return None

    now = int(time.time() * 1000)

    closed = [
        c for c in candles
        if int(c[0]) + duration <= now
    ]

    if len(closed) < 2:
        return None

    first = check_candle(closed[-2])
    second = check_candle(closed[-1])

    if first and first == second:
        return {
            "symbol": symbol,
            "timeframe": interval,
            "direction": first
        }

    return None


def main():
    symbols = get_symbols()
    results = []

    print("Symbols found:", len(symbols))

    with ThreadPoolExecutor(max_workers=5) as pool:
        tasks = []

        for symbol in symbols:
            for interval, duration in TIMEFRAMES.items():
                tasks.append(
                    pool.submit(
                        scan_one, symbol, interval, duration
                    )
                )

        for task in as_completed(tasks):
            try:
                result = task.result()
                if result:
                    results.append(result)
            except Exception as e:
                print("Scan error:", str(e))

    if not results:
        send_telegram("Crypto Scanner\n\nNo valid patterns found.")
        return

    message = "CRYPTO SCANNER\n\n"

    for r in results:
        direction = (
            "صعودی 🟢"
            if r["direction"] == "BULLISH"
            else "نزولی 🔴"
        )
        message += (
            f"{r['symbol']}\n"
            f"Timeframe: {r['timeframe']}\n"
            f"Direction: {direction}\n\n"
        )

    for i in range(0, len(message), 3500):
        send_telegram(message[i:i + 3500])

    print("Scan completed:", len(results))


if __name__ == "__main__":
    main()
