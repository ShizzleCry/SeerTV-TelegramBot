import logging
import os

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OVERSEERR_URL = os.environ["OVERSEERR_URL"].rstrip("/")
OVERSEERR_API_KEY = os.environ["OVERSEERR_API_KEY"]

# Comma-separated list of Telegram numeric user IDs allowed to use the bot.
# Leave empty to allow anyone (not recommended).
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HEADERS = {"X-Api-Key": OVERSEERR_API_KEY, "Content-Type": "application/json"}

# In-memory cache of search results per chat, so callback buttons can look
# up what the user picked without re-querying Overseerr.
SEARCH_CACHE: dict[int, dict[str, dict]] = {}


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not is_allowed(user.id):
        await update.effective_message.reply_text(
            f"Sorry, you're not authorized to use this bot.\n"
            f"Your Telegram user ID is {user.id} - ask the admin to add it."
        )
        logger.warning("Blocked unauthorized user %s (%s)", user.id, user.username)
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.message.reply_text(
        "Hi! Send me a movie or TV show title and I'll request it on Overseerr.\n\n"
        "Example: Dune Part Two"
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.message.reply_text(f"Your Telegram user ID is: {user.id}")


async def search_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return

    query = update.message.text.strip()
    if not query:
        return

    msg = await update.message.reply_text(f"Searching for \"{query}\"...")

    try:
        resp = requests.get(
            f"{OVERSEERR_URL}/api/v1/search",
            headers=HEADERS,
            params={"query": query, "page": 1, "language": "en"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as e:
        logger.exception("Overseerr search failed")
        await msg.edit_text(f"Couldn't reach Overseerr: {e}")
        return

    # Only keep movies and TV shows (drop people, collections, etc.)
    results = [r for r in results if r.get("mediaType") in ("movie", "tv")][:8]

    if not results:
        await msg.edit_text(f"No movies or TV shows found for \"{query}\".")
        return

    chat_id = update.effective_chat.id
    SEARCH_CACHE[chat_id] = {}

    buttons = []
    for r in results:
        media_type = r["mediaType"]
        tmdb_id = r["id"]
        title = r.get("title") or r.get("name") or "Unknown title"
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        year = date[:4] if date else "?"
        label_type = "Movie" if media_type == "movie" else "TV"

        status = (r.get("mediaInfo") or {}).get("status")
        # Overseerr status codes: 3/4/5 roughly mean already available/processing
        tag = " ✅ already added" if status in (3, 4, 5) else ""

        key = f"{media_type}:{tmdb_id}"
        SEARCH_CACHE[chat_id][key] = {"title": title, "media_type": media_type, "tmdb_id": tmdb_id}

        buttons.append([
            InlineKeyboardButton(
                f"{label_type}: {title} ({year}){tag}",
                callback_data=f"pick:{key}",
            )
        ])

    await msg.edit_text(
        f"Results for \"{query}\" - tap the right one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not is_allowed(user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    await query.answer()
    chat_id = update.effective_chat.id
    _, key = query.data.split(":", 1)

    cached = SEARCH_CACHE.get(chat_id, {}).get(key)
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return

    media_type = cached["media_type"]
    tmdb_id = cached["tmdb_id"]
    title = cached["title"]

    payload = {"mediaType": media_type, "mediaId": tmdb_id}

    if media_type == "tv":
        # Request every season. Fetch season list from Overseerr's TMDB proxy.
        try:
            tv_resp = requests.get(
                f"{OVERSEERR_URL}/api/v1/tv/{tmdb_id}", headers=HEADERS, timeout=15
            )
            tv_resp.raise_for_status()
            seasons = [
                s["seasonNumber"]
                for s in tv_resp.json().get("seasons", [])
                if s["seasonNumber"] != 0  # skip "Specials"
            ]
            payload["seasons"] = seasons or "all"
        except requests.RequestException:
            payload["seasons"] = "all"

    try:
        resp = requests.post(
            f"{OVERSEERR_URL}/api/v1/request", headers=HEADERS, json=payload, timeout=15
        )
        if resp.status_code in (200, 201):
            await query.edit_message_text(f"✅ Requested: {title}")
        elif resp.status_code == 409:
            await query.edit_message_text(f"ℹ️ Already requested: {title}")
        else:
            logger.error("Overseerr request failed: %s %s", resp.status_code, resp.text)
            await query.edit_message_text(
                f"❌ Overseerr rejected the request for {title} "
                f"(status {resp.status_code}). Check the bot logs."
            )
    except requests.RequestException as e:
        logger.exception("Overseerr request failed")
        await query.edit_message_text(f"❌ Couldn't reach Overseerr: {e}")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CallbackQueryHandler(handle_pick, pattern=r"^pick:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_media))

    logger.info("Bot starting with polling (no inbound ports needed)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
