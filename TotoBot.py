import asyncio
from datetime import datetime
import os
import sqlite3
import logging
import pytz
from pytz import timezone
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

Memory ={
    "jackpot": None,
    "draw": None,
}

from playwright.async_api import async_playwright

# -------------------------
# ENV
# -------------------------
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
DATABASE = os.getenv("TOTO_DB_PATH", "toto.db")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 10000))
WEBHOOK_PATH = "telegram"

# -------------------------
# Logging
# -------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------
# DB
# -------------------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    cur = conn.cursor()

    # Create subscribers table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY
        )
    """)
    # Create result table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS result (
            jackpot INTEGER,
            draw TEXT
        )
    """)

    conn.commit()
    conn.close()


def add_subscriber(cid):
    with sqlite3.connect(DATABASE) as c:
        c.execute("INSERT OR IGNORE INTO subscribers VALUES (?)", (cid,))

def remove_subscriber(cid):
    with sqlite3.connect(DATABASE) as c:
        c.execute("DELETE FROM subscribers WHERE chat_id=?", (cid,))

def list_subscribers():
    with sqlite3.connect(DATABASE) as c:
        return [r[0] for r in c.execute("SELECT chat_id FROM subscribers")]

def store_result(jackpot, draw):
    with sqlite3.connect(DATABASE) as c:
        c.execute("DELETE FROM result")
        c.execute("INSERT INTO result (jackpot, draw) VALUES (?, ?)", (jackpot, draw))

def get_latest_result():
    with sqlite3.connect(DATABASE) as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT jackpot, draw
            FROM result
            ORDER BY rowid DESC
            LIMIT 1
        """)
        return cur.fetchone()

# -------------------------
# Playwright
# -------------------------
def cache_result(jackpot, draw):
    Memory["jackpot"] = jackpot
    Memory["draw"] = draw

async def fetch_toto():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://www.singaporepools.com.sg/en/product/pages/toto_results.aspx")
        await page.wait_for_selector("div.toto-draw-date", timeout=10000)
        jackpot = await page.locator("text=Next Jackpot").locator("xpath=following-sibling::span").inner_text()
        draw = await page.locator("text=Next Draw").locator("xpath=following-sibling::div").inner_text()

        await browser.close()
        return jackpot.strip(), draw.strip()

def is_draw_past(draw_str: str) -> bool:
    try:
        # Replace periods if needed and normalize spacing
        draw_str = draw_str.replace(" ,", ",").strip()
        draw_str = draw_str.replace('.', ':')
        clean = draw_str[:-2] + draw_str[-2:].upper()
        dt = datetime.strptime(clean, "%a, %d %b %Y, %I:%M%p")
        return dt < datetime.now()
    except Exception as e:
        print("Could not parse draw date:", e)
        return True  # if parsing fails, assume we need to fetch new data

async def get_toto_data(fetch_func):
    jackpot, draw = Memory.get("jackpot"), Memory.get("draw")
    if jackpot is not None and draw is not None:
        if not is_draw_past(draw):
            logger.info("Jackpot from cache")
            return jackpot, draw

    #from DB
    result = get_latest_result()
    if result:
        jackpot, draw = result
        if jackpot is not None and draw is not None:
            if not is_draw_past(draw):
                logger.info("Jackpot from DB")
                cache_result(jackpot, draw)
                return jackpot, draw

    #from website
    jackpot, draw = await fetch_func()
    logger.info("Jackpot from website")
    if jackpot is not None and draw is not None:
            cache_result(jackpot, draw)
            store_result(jackpot, draw)
    return jackpot, draw



# -------------------------
# Handlers
# -------------------------
async def start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_chat.id)
    await update.message.reply_text("✅ Subscribed to TOTO updates")

async def unsubscribe(update: Update, _: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_chat.id)
    await update.message.reply_text("❌ Unsubscribed")

async def status(update: Update, _: ContextTypes.DEFAULT_TYPE):
    jackpot, draw = await get_toto_data(fetch_toto)
    await update.message.reply_text(f"🏆 {jackpot}\n📅 {draw}")

# -------------------------
# Scheduler job
# -------------------------
async def send_update(app: Application):
    jackpot, draw = await get_toto_data(fetch_toto)
    msg = f"🏆 <b>TOTO Update</b>\n💰 {jackpot}\n📅 {draw}"

    for cid in list_subscribers():
        try:
            await app.bot.send_message(cid, msg, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Failed to send to {cid}: {e}")

# -------------------------
# Post-init hook
# -------------------------
async def post_init(app: Application):
    scheduler = AsyncIOScheduler(timezone=timezone("Asia/Singapore"))
    trigger = CronTrigger(day_of_week="mon,thu", hour=11, minute=0)
#for render
    scheduler.add_job(send_update, trigger, args=[app])
# for local
#    loop = asyncio.get_event_loop()
#   scheduler.add_job(
#        lambda: asyncio.run_coroutine_threadsafe(send_update(app), loop),
#        trigger
#    )
    scheduler.start()
    for job in scheduler.get_jobs():
        logger.info(f"Next run: {job.next_run_time}")
    logger.info("Scheduler started~")

# -------------------------
# Main
# -------------------------
def main():
    init_db()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("unsubscribe", unsubscribe))
    app.add_handler(CommandHandler("status", status))
    # for local
    # app.run_polling()

    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=WEBHOOK_PATH,
        webhook_url=f"{WEBHOOK_URL}/{WEBHOOK_PATH}",
    )

if __name__ == "__main__":
    main()
