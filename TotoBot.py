from telegram.ext import Application, CommandHandler
import os

TOKEN = os.getenv("TELEGRAM_TOKEN")
PORT = int(os.environ.get("PORT", 8443))  # required for Railway

app = Application.builder().token(TOKEN).build()

@app.get("/")
def home():
    return "Bot is running!"

if __name__ == "__main__":
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        webhook_url=f"https://totobot-production.up.railway.app/telegram-webhook"
    )
