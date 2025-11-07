import os
import sqlite3
import asyncio
import logging
from datetime import datetime
from functools import partial

import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By

# -------------------------
# Load env
# -------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
DATABASE = os.getenv("TOTO_DB_PATH", "toto_subscribers.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g., https://my-app.up.railway.app
PORT = int(os.getenv("PORT", 8443))
WEBHOOK_PATH = "telegram-webhook"

# Scheduler timezone
SCHEDULER_TZ = pytz.timezone("Asia/Singapore")
NOTIFY_HOUR = 10
NOTIFY_MINUTE = 0

# Selenium scraping interval in seconds
SCRAPE_INTERVAL = 60 * 60  # 1 hour

# -------------------------
# Logging
# -------------------------
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------
# DB functions
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
    cur.execute("INSERT OR REPLACE INTO nextDraw (next_draw, jackpot) VALUES (?, ?)", (next_draw, jackpot))
    conn.commit()
    conn.close()

def get_next_draw():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()
    cur.execute("SELECT next_draw, jackpot FROM nextDraw ORDER BY rowid DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row

# -------------------------
# Selenium scraping
# -------------------------
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
        driver.implicitly_wait(3)

        jackpot_elem = driver.find_element(By.XPATH, "//div[text()[contains(.,'Next Jackpot')]]/following-sibling::span")
        draw_elem = driver.find_element(By.XPATH, "//div[text()[contains(.,'Next Draw')]]/following-sibling::div[@class='toto-draw-date']")

        jackpot = jackpot_elem.text.strip() if jackpot_elem else None
        next_draw = draw_elem.text.strip() if draw_elem else None
        return jackpot, next_draw
    except Exception as e:
        logger.error(f"Selenium scraping error: {e}")
        return None, None
    finally:
        driver.quit()

async def background_scraper():
    while True:
        jackpot, next_draw = fetch_toto_info_selenium()
        if jackpot and next_draw:
            store_next_draw(next_draw, jackpot)
            logger.info(f"TOTO data updated: {next_draw} / {jackpot}")
        else:
            logger.warning("Failed to fetch TOTO data")
        await asyncio.sleep(SCRAPE_INTERVAL)

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
    record = get_next_draw()
    if record:
        next_draw, jackpot = record
    else:
        next_draw, jackpot = None, None
    await update.message.reply_text(f"🏆 Prize: {jackpot or 'N/A'}\n📅 Next Draw: {next_draw or 'N/A'}")

def _is_admin(update: Update):
    uname = update.effective_user.username
    return uname and uname.lower() == ADMIN_USERNAME.lower()

async def listsubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("Unauthorized.")
    subs = list_subscribers()
    await update.message.reply_text(f"Subscribers ({len(subs)}):\n" + "\n".join(str(s) for s in subs[:200]))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update): return await update.message.reply_text("Unauthorized.")
    record = get_next_draw()
    jackpot, next_draw = record[1], record[0] if record else (None, None)
    message = f"🏆 <b>TOTO Update</b>\n💰 Prize: {jackpot or '(not available)'}\n📅 Next Draw: {next_draw or '(not available)'}\n"
    for cid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=cid, text=message, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to send message to {cid}: {e}")
    await update.message.reply_text(f"Broadcast sent to {len(list_subscribers())} subscribers.")

# -------------------------
# Scheduled notifications
# -------------------------
async def send_toto_update(context: ContextTypes.DEFAULT_TYPE):
    record = get_next_draw()
    jackpot, next_draw = record[1], record[0] if record else (None, None)
    message = f"🏆 <b>TOTO Update</b>\n💰 Prize: {jackpot or '(not available)'}\n📅 Next Draw: {next_draw or '(not available)'}\n"
    for cid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=cid, text=message, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to send message to {cid}: {e}")

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

    # Scheduler for cron notifications
    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TZ)
    cron_trigger = CronTrigger(day_of_week="sun,thu", hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE)
    scheduler.add_job(partial(send_toto_update, context=app), cron_trigger)
    scheduler.start()
    logger.info(f"Cron notifications scheduled Sun & Thu at {NOTIFY_HOUR:02d}:{NOTIFY_MINUTE:02d} SGT")

    # Start background scraper
    asyncio.create_task(background_scraper())

    # Start webhook
    await app.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_path=WEBHOOK_PATH
    )
    await app.bot.set_webhook(f"{WEBHOOK_URL}/{WEBHOOK_PATH}")
    logger.info(f"Webhook running at {WEBHOOK_URL}/{WEBHOOK_PATH}")

    await app.updater.idle()

if __name__ == "__main__":
    asyncio.run(main())
