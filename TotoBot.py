"""
TotoBot.py

Requirements:
pip install python-telegram-bot==20.7 apscheduler requests beautifulsoup4 python-dotenv selenium pytz nest_asyncio

.env file should have:
TELEGRAM_TOKEN=123456789:ABCDefGhIjKlmNoPQrsTUvWXyZ
ADMIN_USERNAME=YourTelegramUsernameWithout@
TOTO_DB_PATH=toto_subscribers.db
"""
import os
import sqlite3
import logging
import asyncio
from functools import partial
from datetime import datetime, timedelta
import time
import pytz
from dotenv import load_dotenv
import nest_asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# -------------------------
# Load env
# -------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
DATABASE = os.getenv("TOTO_DB_PATH")
SCRAPING_ENABLED = True
USER_AGENT = "TotoNotifierBot/1.0 (+https://example.com)"

# Scheduler timezone
SCHEDULER_TZ = pytz.timezone("Asia/Singapore")
NOTIFY_HOUR = 10
NOTIFY_MINUTE = 0
SCRAPE_DELAY_SECONDS = 1.5

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------
# SQLite DB functions
# -------------------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            added_at TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nextDraw (
            next_draw TEXT PRIMARY KEY,
            jackpot TEXT
        );
    """)
    conn.commit()
    conn.close()


def add_subscriber(chat_id: int):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO subscribers (chat_id, added_at) VALUES (?, datetime('now'))", (chat_id,))
    conn.commit()
    conn.close()


def remove_subscriber(chat_id: int):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def list_subscribers() -> list:
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM subscribers")
    rows = cur.fetchall()
    conn.close()
    return [r[0] for r in rows]


def store_next_draw(next_draw: str, jackpot: str):
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    print(f"Storing data → next_draw={next_draw}, jackpot={jackpot}")
    cur.execute("""
        INSERT OR REPLACE INTO nextDraw (next_draw, jackpot)
        VALUES (?, ?)
    """, (next_draw, jackpot))
    conn.commit()
    conn.close()


def get_next_draw():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT next_draw, jackpot FROM nextDraw ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row  # (next_draw, jackpot) or None

# -------------------------
# Scraping functions
# -------------------------
HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

def fetch_toto_info_selenium():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver = webdriver.Chrome(options=options)

    try:
        driver.get("https://www.singaporepools.com.sg/en/product/pages/toto_results.aspx")
        time.sleep(3)

        jackpot = None
        jackpot_elem = driver.find_element(By.XPATH, "//div[text()[contains(.,'Next Jackpot')]]/following-sibling::span")
        if jackpot_elem:
            jackpot = jackpot_elem.text.strip()

        next_draw = None
        draw_elem = driver.find_element(By.XPATH, "//div[text()[contains(.,'Next Draw')]]/following-sibling::div[@class='toto-draw-date']")
        if draw_elem:
            next_draw = draw_elem.text.strip()

        return jackpot, next_draw
    except Exception as e:
        print(f"Error fetching Toto info: {e}")
        return None, None
    finally:
        driver.quit()


def fetch_toto_info():
    if not SCRAPING_ENABLED:
        return None, None
    return fetch_toto_info_selenium()


def is_past_draw(next_draw_str: str) -> bool:
    try:
        clean = next_draw_str.replace(" ,", ",").replace(" .", ".").strip()
        clean = clean.replace(".", ":")
        dt = datetime.strptime(clean, "%a, %d %b %Y, %I:%M%p")
        dt = SCHEDULER_TZ.localize(dt)
        return dt < datetime.now(SCHEDULER_TZ)
    except Exception as e:
        print("Could not parse next_draw:", e)
        return True


def get_data():
    record = get_next_draw()
    jackpot, next_draw = (None, None)
    if record:
        next_draw, jackpot = record
        print("Fetched TOTO data from DB")

    if not jackpot or not next_draw or is_past_draw(next_draw):
        print("Fetching fresh TOTO data...")
        jackpot, next_draw = fetch_toto_info()
        if jackpot and next_draw:
            store_next_draw(next_draw, jackpot)
        else:
            print("Warning: could not fetch valid TOTO data")
    return jackpot, next_draw

# -------------------------
# Telegram handlers
# -------------------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text("✅ Subscribed to TOTO prize updates!")


async def unsubscribe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("✅ You have been unsubscribed.")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jackpot, next_draw = get_data()
    text = f"🏆 Prize: {jackpot or 'N/A'}\n📅 Next Draw: {next_draw or 'N/A'}"
    await update.message.reply_text(text)


def _is_admin(update: Update):
    uname = update.effective_user.username
    return uname and uname.lower() == ADMIN_USERNAME.lower()


async def listsubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("Unauthorized.")
    subs = list_subscribers()
    await update.message.reply_text(f"Subscribers ({len(subs)}):\n" + "\n".join(str(s) for s in subs[:200]))


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("Unauthorized.")
    jackpot, next_draw = get_data()
    message = f"🏆 <b>TOTO Update</b>\n💰 Prize: {jackpot or '(not available)'}\n📅 Next Draw: {next_draw or '(not available)'}\n"
    for cid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=cid, text=message, parse_mode="HTML")
        except: pass
    await update.message.reply_text(f"Broadcast sent to {len(list_subscribers())} subscribers.")

# -------------------------
# Scheduled notifications
# -------------------------
async def send_toto_update(context: ContextTypes.DEFAULT_TYPE):
    jackpot, next_draw = get_data()
    message = f"🏆 <b>TOTO Update</b>\n💰 Prize: {jackpot or '(not available)'}\n📅 Next Draw: {next_draw or '(not available)'}\n"
    for cid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=cid, text=message, parse_mode="HTML")
        except Exception as e:
            print(f"Failed to send message to {cid}: {e}")


# -------------------------
# Main
# -------------------------
async def main():
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("listsubs", listsubs_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TZ)
    # Cron notifications Sun & Thu
    cron_trigger = CronTrigger(day_of_week="sun,thu", hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE, timezone=SCHEDULER_TZ)
    scheduler.add_job(partial(send_toto_update, context=app), cron_trigger)
    print(f"Cron notifications scheduled Sun & Thu at {NOTIFY_HOUR:02d}:{NOTIFY_MINUTE:02d} SGT")
    logger.info(f"Cron notifications scheduled Sun & Thu at {NOTIFY_HOUR:02d}:{NOTIFY_MINUTE:02d} (SGT)")

    scheduler.start()
    webhook_url = os.getenv("WEBHOOK_URL")
    port = int(os.getenv("port", 8000))
    await app.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=TELEGRAM_TOKEN
    )

    await app.bot.set_webhook(f"{webhook_url}/{TELEGRAM_TOKEN}")
    print (f"Webhook running at {webhook_url}/{TELEGRAM_TOKEN}")
    await app.updater.idle()


# -------------------------
# Run done
# -------------------------
# nest_asyncio.apply()
# loop = asyncio.get_event_loop()
# loop.create_task(main())
# loop.run_forever()
