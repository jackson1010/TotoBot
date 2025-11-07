"""
TotoBot.py using Playwright

Requirements:
pip install python-telegram-bot==20.7 apscheduler requests beautifulsoup4 python-dotenv playwright pytz nest_asyncio

Run once:
playwright install

.env file should have:
TELEGRAM_TOKEN=123456789:ABCDefGhIjKlmNoPQrsTUvWXyZ
ADMIN_USERNAME=YourTelegramUsernameWithout@
TOTO_DB_PATH=toto_subscribers.db
WEBHOOK_URL=https://your-app.up.railway.app
PORT=8000
"""

import os
import sqlite3
import logging
import asyncio
from functools import partial
from datetime import datetime
import pytz
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from playwright.async_api import async_playwright

# -------------------------
# Load env
# -------------------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
DATABASE = os.getenv("TOTO_DB_PATH")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))
WEBHOOK_PATH = "telegram-webhook"

# Scheduler timezone
SCHEDULER_TZ = pytz.timezone("Asia/Singapore")
NOTIFY_HOUR = 10
NOTIFY_MINUTE = 0

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
    return row

# -------------------------
# Playwright scraper
# -------------------------
async def fetch_toto_info_playwright():
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto("https://www.singaporepools.com.sg/en/product/pages/toto_results.aspx")
            await page.wait_for_selector("div.toto-draw-date", timeout=5000)

            jackpot_elem = await page.query_selector("//div[text()[contains(.,'Next Jackpot')]]/following-sibling::span")
            next_draw_elem = await page.query_selector("//div[text()[contains(.,'Next Draw')]]/following-sibling::div[@class='toto-draw-date']")

            jackpot = await jackpot_elem.inner_text() if jackpot_elem else None
            next_draw = await next_draw_elem.inner_text() if next_draw_elem else None

            await browser.close()
            return jackpot.strip() if jackpot else None, next_draw.strip() if next_draw else None
    except Exception as e:
        print(f"Error fetching TOTO info via Playwright: {e}")
        return None, None


async def get_data():
    record = get_next_draw()
    jackpot, next_draw = (None, None)
    if record:
        next_draw, jackpot = record

    # Fetch fresh data if needed
    if not jackpot or not next_draw:
        jackpot, next_draw = await fetch_toto_info_playwright()
        if jackpot and next_draw:
            store_next_draw(next_draw, jackpot)
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
    jackpot, next_draw = await get_data()
    text = f"🏆 Prize: {jackpot or 'N/A'}\n📅 Next Draw: {next_draw or 'N/A'}"
    await update.message.reply_text(text)

def _is_admin(update: Update):
    uname = update.effective_user.username
    return uname and uname.lower() == ADMIN_USERNAME.lower()

async def listsubs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Unauthorized.")
    subs = list_subscribers()
    await update.message.reply_text(f"Subscribers ({len(subs)}):\n" + "\n".join(str(s) for s in subs[:200]))

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return await update.message.reply_text("Unauthorized.")
    jackpot, next_draw = await get_data()
    message = f"🏆 <b>TOTO Update</b>\n💰 Prize: {jackpot or '(not available)'}\n📅 Next Draw: {next_draw or '(not available)'}\n"
    for cid in list_subscribers():
        try:
            await context.bot.send_message(chat_id=cid, text=message, parse_mode="HTML")
        except: pass
    await update.message.reply_text(f"Broadcast sent to {len(list_subscribers())} subscribers.")

# -------------------------
# Scheduler
# -------------------------
async def send_toto_update(context: ContextTypes.DEFAULT_TYPE):
    jackpot, next_draw = await get_data()
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

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("listsubs", listsubs_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Scheduler
    scheduler = AsyncIOScheduler(timezone=SCHEDULER_TZ)
    cron_trigger = CronTrigger(day_of_week="sun,thu", hour=NOTIFY_HOUR, minute=NOTIFY_MINUTE)
    scheduler.add_job(partial(send_toto_update, context=app), cron_trigger)
    scheduler.start()
    logger.info(f"Cron notifications scheduled Sun & Thu at {NOTIFY_HOUR:02d}:{NOTIFY_MINUTE:02d} SGT")

    # Run webhook
    await app.start_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=TELEGRAM_TOKEN
    )
    await app.bot.set_webhook(f"{WEBHOOK_URL}/{TELEGRAM_TOKEN}")

    print(f"Webhook running at {WEBHOOK_URL}/{TELEGRAM_TOKEN}")
    await app.updater.idle()