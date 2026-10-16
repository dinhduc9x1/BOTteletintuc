# BOTteletintuc
import os
import time
import html
import sqlite3
import hashlib
import logging
from datetime import datetime, timezone, timedelta

import requests
import feedparser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

BOT_TOKEN = "8919509225:AAHbEbZGA_dM3RCFF4CyYasg4DKk2xv3jH0"
CHAT_ID = "1493544088"
DB_FILE = "seen.db"
CHECK_EVERY = 300          # quét mỗi 5 phút
PRICE_EVERY = 4 * 3600     # báo giá mỗi 4 giờ
PRE_ALERT_MIN = 30         # cảnh báo trước 30 phút
VN_TZ = timezone(timedelta(hours=7))

# Lịch kinh tế (JSON không chính thức của Forex Factory, có thể thay đổi)
CAL_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CAL_KEYWORDS = ["cpi", "non-farm", "nfp", "gdp", "fed", "fomc", "interest rate",
                "powell", "unemployment", "pce", "payroll"]

RSS_FEEDS = [
    ("🏛 Fed", "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("🪙 CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("🥇 Gold", "https://news.google.com/rss/search?q=gold+price+fed&hl=en-US&gl=US&ceid=US:en"),
    ("₿ BTC", "https://news.google.com/rss/search?q=bitcoin+fed+rate&hl=en-US&gl=US&ceid=US:en"),
]
NEWS_KEYWORDS = ["fed", "fomc", "powell", "rate", "cpi", "inflation", "nfp", "payroll",
                 "gdp", "gold", "bitcoin", "btc", "dollar", "dxy", "treasury", "jobs"]


# ---------- DB chống trùng ----------
def get_db():
    c = sqlite3.connect(DB_FILE)
    c.execute("CREATE TABLE IF NOT EXISTS seen (k TEXT PRIMARY KEY, t TEXT)")
    c.commit()
    return c


def notify(c, key, msg, silent):
    """Gửi tin nếu chưa gửi bao giờ. silent=True thì chỉ ghi nhận."""
    exists = c.execute("SELECT 1 FROM seen WHERE k=?", (key,)).fetchone()
    if exists:
        return
    c.execute("INSERT INTO seen VALUES (?, ?)", (key, datetime.now().isoformat()))
    c.commit()
    if not silent:
        tg_send(msg)


# ---------- Telegram ----------
def tg_send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    for attempt in range(3):
        try:
            r = requests.post(url, data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }, timeout=20)
            if r.status_code == 200:
                return
            logging.warning("Telegram lỗi %s: %s", r.status_code, r.text[:200])
        except requests.RequestException as e:
            logging.warning("Lỗi mạng Telegram: %s", e)
        time.sleep(3)


# ---------- Lịch kinh tế: 🔴 High Impact + USD ----------
def check_calendar(c, silent):
    try:
        events = requests.get(CAL_URL, timeout=20).json()
    except Exception as e:
        logging.warning("Không tải được lịch kinh tế: %s", e)
        return

    now = datetime.now(timezone.utc)
    for ev in events:
        if ev.get("country") != "USD" or ev.get("impact") != "High":
            continue
        title = ev.get("title", "")
        if not any(k in title.lower() for k in CAL_KEYWORDS):
            continue

        when = datetime.fromisoformat(ev["date"]).astimezone(timezone.utc)
        base = f"{title}|{ev['date']}"
        mins_to = (when - now).total_seconds() / 60
        local_time = when.astimezone(VN_TZ).strftime("%H:%M %d/%m")

        # 1) Cảnh báo trước giờ công bố
        if 0 < mins_to <= PRE_ALERT_MIN:
            msg = (f"🔴 <b>SẮP CÔNG BỐ · USD · HIGH IMPACT</b>\n"
                   f"<b>{html.escape(title)}</b>\n"
                   f"🕒 {local_time} (giờ VN)\n"
                   f"📊 Dự báo: {ev.get('forecast') or '-'} | Kỳ trước: {ev.get('previous') or '-'}\n"
                   f"⚠️ Biến động mạnh USD → ảnh hưởng Vàng & BTC")
            notify(c, f"pre|{base}", msg, silent)

        # 2) Kết quả thực tế (chỉ báo trong 24h gần nhất để tránh spam)
        actual = ev.get("actual")
        if actual and -1440 <= mins_to <= 0:
            msg = (f"🔴 <b>KẾT QUẢ · {html.escape(title)}</b>\n"
                   f"✅ Thực tế: <b>{actual}</b>\n"
                   f"📊 Dự báo: {ev.get('forecast') or '-'} | Kỳ trước: {ev.get('previous') or '-'}\n"
                   f"💡 Thường: số cao hơn kỳ vọng → USD mạnh → Vàng/BTC áp lực giảm (không đảm bảo)")
            notify(c, f"act|{base}", msg, silent)


# ---------- Tin tức RSS ----------
def check_news(c, silent):
    for name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logging.warning("Lỗi RSS %s: %s", name, e)
            continue
        for e in feed.entries[:15]:
            title = e.get("title", "").strip()
            link = e.get("link", "")
            if not title:
                continue
            if not any(k in title.lower() for k in NEWS_KEYWORDS):
                continue
            key = "news|" + hashlib.md5((link or title).encode()).hexdigest()
            msg = f"{name}\n{html.escape(title)}\n{link}"
            notify(c, key, msg, silent)


# ---------- Giá BTC / Vàng / DXY ----------
def price_snapshot():
    lines = ["💰 <b>Cập nhật giá</b>"]
    try:
        r = requests.get("https://api.coingecko.com/api/v3/simple/price", params={
            "ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"
        }, timeout=15).json()["bitcoin"]
        lines.append(f"₿ BTC: <b>${r['usd']:,.0f}</b> ({r['usd_24h_change']:+.2f}% 24h)")
    except Exception as e:
        logging.warning("Lỗi giá BTC: %s", e)

    try:
        import yfinance as yf
        for symbol, label in [("GC=F", "🥇 Vàng (GC=F)"), ("DX-Y.NYB", "💵 DXY")]:
            h = yf.Ticker(symbol).history(period="5d")["Close"].dropna()
            last, prev = h.iloc[-1], h.iloc[-2]
            lines.append(f"{label}: <b>{last:,.2f}</b> ({(last / prev - 1) * 100:+.2f}%)")
    except Exception as e:
        logging.warning("Lỗi giá vàng/DXY: %s", e)

    if len(lines) > 1:
        tg_send("\n".join(lines))


# ---------- Main loop ----------
def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Thiếu TG_BOT_TOKEN hoặc TG_CHAT_ID")

    c = get_db()
    first_run = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    last_price = 0.0
    logging.info("Bot khởi động (first_run=%s)", first_run)

    while True:
        try:
            check_calendar(c, silent=first_run)
            check_news(c, silent=first_run)
            if not first_run and time.time() - last_price > PRICE_EVERY:
                price_snapshot()
                last_price = time.time()
        except Exception:
            logging.exception("Lỗi vòng lặp")
        first_run = False
        time.sleep(CHECK_EVERY)


if __name__ == "__main__":
    main()