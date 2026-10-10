import logging
import os
import time
from urllib.parse import quote, urlencode

import requests
from aiohttp import web
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

import sonarr
import storage

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OVERSEERR_URL = os.environ["OVERSEERR_URL"].rstrip("/")
OVERSEERR_API_KEY = os.environ["OVERSEERR_API_KEY"]

# Permanent allowlist set via env (can't be removed without redeploying).
# Friends added later via /adduser go into storage.py's persisted list instead.
ALLOWED_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip()
}

# Admins can run /adduser, /removeuser, /listusers.
ADMIN_USER_IDS = {
    int(uid.strip())
    for uid in os.environ.get("ADMIN_USER_IDS", "").split(",")
    if uid.strip()
}

WEBHOOK_PORT = int(os.environ.get("WEBHOOK_PORT", "5091"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

SEARCH_CACHE_TTL = 15 * 60  # 15 minutes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

HEADERS = {"X-Api-Key": OVERSEERR_API_KEY, "Content-Type": "application/json"}

# chat_id -> {"ts": float, "items": {key: {"title", "media_type", "tmdb_id"}}}
SEARCH_CACHE: dict[int, dict] = {}

# (chat_id, key) -> set of selected season numbers, during the "choose seasons" flow
SEASON_SELECTION: dict[tuple, set] = {}

# key -> list[int] of real season numbers (specials excluded), cached per search session
TV_SEASON_CACHE: dict[str, list] = {}

MEDIA_STATUS_LABELS = {
    2: "📥 already requested",
    3: "⚙️ processing",
    4: "🔽 partially available",
    5: "✅ already available",
}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS and not ADMIN_USER_IDS:
        return True  # nothing configured yet - wide open until first redeploy
    return (
        user_id in ALLOWED_USER_IDS
        or user_id in ADMIN_USER_IDS
        or user_id in storage.load_allowed_users()
    )


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS


async def guard(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if is_allowed(user.id):
        return True
    if chat.type == "private":
        await update.effective_message.reply_text(
            f"Sorry, you're not authorized to use this bot.\n"
            f"Your Telegram user ID is {user.id} - ask the admin to run /adduser {user.id}."
        )
    else:
        logger.info("Ignoring unauthorized user %s in chat %s", user.id, chat.id)
    return False


# ---------------------------------------------------------------------------
# Basic commands
# ---------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Hi! Send me a movie or TV show title and I'll request it on Overseerr.\n\n"
            "Example: Dune Part Two\n\n"
            "Other commands: /requests (recent requests), /episode <title> s4e7 "
            "(fetch one missing episode), /whoami (your Telegram ID)"
        )
    else:
        await update.message.reply_text(
            "Hi! In this group, use /add <title> to request something.\n"
            "Example: /add Dune Part Two"
        )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your Telegram user ID is: {update.effective_user.id}")


# ---------------------------------------------------------------------------
# Admin: manage allowed users without redeploying
# ---------------------------------------------------------------------------

async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /adduser <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a numeric Telegram user ID.")
        return
    storage.add_allowed_user(uid)
    await update.message.reply_text(f"✅ Added user {uid}. They can use the bot now - no redeploy needed.")


async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeuser <telegram_user_id>")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a numeric Telegram user ID.")
        return
    storage.remove_allowed_user(uid)
    await update.message.reply_text(f"🗑 Removed user {uid} (if they were added via /adduser).")


async def cmd_listusers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admins only.")
        return
    admins = ", ".join(map(str, sorted(ADMIN_USER_IDS))) or "none"
    seeded = ", ".join(map(str, sorted(ALLOWED_USER_IDS))) or "none"
    added = ", ".join(map(str, sorted(storage.load_allowed_users()))) or "none"
    await update.message.reply_text(
        f"👑 Admins: {admins}\n"
        f"🔒 Seeded via env (permanent): {seeded}\n"
        f"👥 Added via /adduser: {added}"
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add <title> - works in both DMs and groups."""
    if not await guard(update):
        return
    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text("Usage: /add <title>, e.g. /add Dune Part Two")
        return
    await run_search(update, context, query)


async def search_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text search - private chats only, to avoid reacting to every group message."""
    if not await guard(update):
        return
    query = update.message.text.strip()
    if not query:
        return
    await run_search(update, context, query)


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    msg = await update.effective_message.reply_text(f"Searching for \"{query}\"...")

    try:
        # Overseerr's validation rejects "+" for spaces (requests' default
        # form-style encoding); force %20 via quote_via=quote instead.
        query_string = urlencode(
            {"query": query, "page": 1, "language": "en"},
            quote_via=quote,
        )
        resp = requests.get(
            f"{OVERSEERR_URL}/api/v1/search?{query_string}",
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as e:
        logger.exception("Overseerr search failed")
        await msg.edit_text(f"Couldn't reach Overseerr: {e}")
        return

    results = [r for r in results if r.get("mediaType") in ("movie", "tv")][:8]

    if not results:
        await msg.edit_text(f"No movies or TV shows found for \"{query}\".")
        return

    chat_id = update.effective_chat.id
    SEARCH_CACHE[chat_id] = {"ts": time.time(), "items": {}}

    buttons = []
    for r in results:
        media_type = r["mediaType"]
        tmdb_id = r["id"]
        title = r.get("title") or r.get("name") or "Unknown title"
        date = r.get("releaseDate") or r.get("firstAirDate") or ""
        year = date[:4] if date else "?"
        label_type = "Movie" if media_type == "movie" else "TV"

        status = (r.get("mediaInfo") or {}).get("status")
        key = f"{media_type}:{tmdb_id}"
        SEARCH_CACHE[chat_id]["items"][key] = {
            "title": title, "media_type": media_type, "tmdb_id": tmdb_id,
        }

        if media_type == "tv" and status == 4 and sonarr.is_configured():
            # Partially available: offer to fetch the missing episode(s) via Sonarr.
            label = f"{label_type}: {title} ({year}) - {MEDIA_STATUS_LABELS[status]} (tap: missing eps)"
            callback = f"missing:{key}"
        elif status in MEDIA_STATUS_LABELS:
            label = f"{label_type}: {title} ({year}) - {MEDIA_STATUS_LABELS[status]}"
            callback = f"info:{status}:{key}"
        else:
            label = f"{label_type}: {title} ({year})"
            callback = f"pick:{key}"

        buttons.append([InlineKeyboardButton(label, callback_data=callback)])

    await msg.edit_text(
        f"Results for \"{query}\" - tap the right one:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def get_cached_item(chat_id: int, key: str):
    cache = SEARCH_CACHE.get(chat_id)
    if not cache or time.time() - cache["ts"] > SEARCH_CACHE_TTL:
        return None
    return cache["items"].get(key)


# ---------------------------------------------------------------------------
# Callback query router
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = query.from_user
    if not is_allowed(user.id):
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data
    chat_id = update.effective_chat.id

    if data.startswith("info:"):
        await query.answer("This title is already tracked in Overseerr - no need to request again.", show_alert=True)
        return

    if data.startswith("pick:"):
        await query.answer()
        key = data.split(":", 1)[1]
        await start_request_flow(query, chat_id, key)
        return

    if data.startswith("missing:"):
        await query.answer()
        key = data.split(":", 1)[1]
        await show_missing_episodes(query, chat_id, key)
        return

    if data.startswith("epgo:"):
        await query.answer()
        _, series_id, season, episode = data.split(":")
        await request_single_episode(query, chat_id, int(series_id), int(season), int(episode))
        return

    if data.startswith("epall:"):
        await query.answer()
        series_id = int(data.split(":", 1)[1])
        await request_all_missing(query, chat_id, series_id)
        return

    if data.startswith("seasons:"):
        await query.answer()
        _, mode, key = data.split(":", 2)
        await handle_season_mode(query, chat_id, key, mode)
        return

    if data.startswith("toggle:"):
        await query.answer()
        rest = data[len("toggle:"):]
        key, season_str = rest.rsplit(":", 1)
        await toggle_season(query, chat_id, key, int(season_str))
        return

    if data.startswith("confirmseasons:"):
        await query.answer()
        key = data.split(":", 1)[1]
        await confirm_seasons(query, chat_id, key)
        return

    await query.answer()


async def start_request_flow(query, chat_id: int, key: str) -> None:
    cached = get_cached_item(chat_id, key)
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return

    media_type = cached["media_type"]
    title = cached["title"]

    if media_type == "movie":
        ok, message = submit_request(media_type, cached["tmdb_id"], title, chat_id, query.from_user.id)
        await query.edit_message_text(message)
        return

    # TV: offer season options first
    buttons = [
        [InlineKeyboardButton("All seasons", callback_data=f"seasons:all:{key}")],
        [InlineKeyboardButton("Latest season only", callback_data=f"seasons:latest:{key}")],
        [InlineKeyboardButton("Choose seasons...", callback_data=f"seasons:choose:{key}")],
    ]
    await query.edit_message_text(
        f"\"{title}\" - which seasons?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


def fetch_tv_seasons(tmdb_id: int) -> list:
    """Real season numbers for a show, excluding 'Specials' (season 0)."""
    try:
        resp = requests.get(f"{OVERSEERR_URL}/api/v1/tv/{tmdb_id}", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        return [
            s["seasonNumber"]
            for s in resp.json().get("seasons", [])
            if s["seasonNumber"] != 0
        ]
    except requests.RequestException:
        logger.exception("Failed to fetch season list for tv:%s", tmdb_id)
        return []


async def handle_season_mode(query, chat_id: int, key: str, mode: str) -> None:
    cached = get_cached_item(chat_id, key)
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return

    title = cached["title"]
    tmdb_id = cached["tmdb_id"]

    if mode == "all":
        ok, message = submit_request("tv", tmdb_id, title, chat_id, query.from_user.id, seasons="all")
        await query.edit_message_text(message)
        return

    if mode == "latest":
        seasons = fetch_tv_seasons(tmdb_id)
        if not seasons:
            await query.edit_message_text("Couldn't determine seasons for this show - try 'Choose seasons' instead.")
            return
        latest = max(seasons)
        ok, message = submit_request("tv", tmdb_id, title, chat_id, query.from_user.id, seasons=[latest])
        await query.edit_message_text(message)
        return

    if mode == "choose":
        seasons = fetch_tv_seasons(tmdb_id)
        if not seasons:
            await query.edit_message_text("Couldn't determine seasons for this show.")
            return
        TV_SEASON_CACHE[key] = seasons
        SEASON_SELECTION[(chat_id, key)] = set()
        await render_season_picker(query, chat_id, key, title)
        return


async def render_season_picker(query, chat_id: int, key: str, title: str) -> None:
    seasons = TV_SEASON_CACHE.get(key, [])
    selected = SEASON_SELECTION.get((chat_id, key), set())

    buttons = []
    row = []
    for num in seasons:
        mark = "✅ " if num in selected else ""
        row.append(InlineKeyboardButton(f"{mark}S{num}", callback_data=f"toggle:{key}:{num}"))
        if len(row) == 4:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("Confirm", callback_data=f"confirmseasons:{key}")])

    await query.edit_message_text(
        f"\"{title}\" - tap seasons to toggle, then Confirm:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def toggle_season(query, chat_id: int, key: str, season: int) -> None:
    cached = get_cached_item(chat_id, key)
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return
    selection = SEASON_SELECTION.setdefault((chat_id, key), set())
    if season in selection:
        selection.discard(season)
    else:
        selection.add(season)
    await render_season_picker(query, chat_id, key, cached["title"])


async def confirm_seasons(query, chat_id: int, key: str) -> None:
    cached = get_cached_item(chat_id, key)
    selection = SEASON_SELECTION.get((chat_id, key), set())
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return
    if not selection:
        await query.answer("Pick at least one season first.", show_alert=True)
        return

    ok, message = submit_request(
        "tv", cached["tmdb_id"], cached["title"], chat_id, query.from_user.id,
        seasons=sorted(selection),
    )
    await query.edit_message_text(message)
    SEASON_SELECTION.pop((chat_id, key), None)


def submit_request(media_type: str, tmdb_id: int, title: str, chat_id: int, user_id: int, seasons=None):
    payload = {"mediaType": media_type, "mediaId": tmdb_id}
    if media_type == "tv":
        payload["seasons"] = seasons or "all"

    try:
        resp = requests.post(f"{OVERSEERR_URL}/api/v1/request", headers=HEADERS, json=payload, timeout=15)
    except requests.RequestException as e:
        logger.exception("Overseerr request failed")
        return False, f"❌ Couldn't reach Overseerr: {e}"

    if resp.status_code in (200, 201):
        key = f"{media_type}:{tmdb_id}"
        storage.add_pending(key, chat_id, user_id, title)
        return True, f"✅ Requested: {title}\nI'll message you here when it's ready."
    if resp.status_code == 409:
        return True, f"ℹ️ Already requested: {title}"

    logger.error("Overseerr request failed: %s %s", resp.status_code, resp.text)
    return False, f"❌ Overseerr rejected the request for {title} (status {resp.status_code})."


# ---------------------------------------------------------------------------
# Single episodes via Sonarr (Overseerr cannot request individual episodes)
# ---------------------------------------------------------------------------

def ep_label(ep: dict) -> str:
    return f"S{ep['seasonNumber']:02d}E{ep['episodeNumber']:02d}"


def ep_pending_key(series_id: int, season: int, episode: int) -> str:
    return f"ep:{series_id}:{season}:{episode}"


async def cmd_episode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/episode <title> s4e7 - request one missing episode through Sonarr."""
    if not await guard(update):
        return
    if not sonarr.is_configured():
        await update.message.reply_text("Episode requests are not set up (SONARR_URL / SONARR_API_KEY missing).")
        return

    parsed = sonarr.parse_episode_arg(" ".join(context.args))
    if not parsed:
        await update.message.reply_text("Usage: /episode <title> s4e7\nExample: /episode Severance s4e7")
        return
    title, season, episode = parsed

    try:
        matches = sonarr.find_series(title)
    except sonarr.SonarrError as e:
        await update.message.reply_text(f"❌ {e}")
        return

    if not matches:
        await update.message.reply_text(
            f"\"{title}\" is not in Sonarr yet. Request the whole show first by searching for it normally, "
            f"then use /episode for missing episodes."
        )
        return

    if len(matches) > 1:
        buttons = [
            [InlineKeyboardButton(
                f"{s['title']} ({s.get('year', '?')})",
                callback_data=f"epgo:{s['id']}:{season}:{episode}",
            )]
            for s in matches[:8]
        ]
        await update.message.reply_text(
            f"Multiple shows match \"{title}\" - which one? (S{season:02d}E{episode:02d})",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    chat = update.effective_chat
    ok, message = do_episode_request(matches[0], season, episode, chat.id, update.effective_user.id)
    await update.message.reply_text(message)


def do_episode_request(series: dict, season: int, episode: int, chat_id: int, user_id: int):
    """Look up the episode, validate it, and trigger the Sonarr search."""
    name = series["title"]
    label = f"S{season:02d}E{episode:02d}"
    try:
        ep = sonarr.find_episode(series["id"], season, episode)
        if not ep:
            return False, f"❌ {name} {label} does not exist (Sonarr has no such episode)."
        if ep.get("hasFile"):
            return True, f"✅ {name} {label} is already available - nothing to request."
        air = ep.get("airDate")
        from datetime import date
        if air and air > date.today().isoformat():
            return False, f"⏳ {name} {label} has not aired yet (airs {air})."
        sonarr.search_episode(ep)
    except sonarr.SonarrError as e:
        return False, f"❌ {e}"

    storage.add_pending(ep_pending_key(series["id"], season, episode), chat_id, user_id, f"{name} {label}")
    return True, (
        f"🔎 Searching for {name} {label}.\n"
        f"I'll message you here when it's downloaded. If no release is found yet, "
        f"Sonarr keeps monitoring it and picks it up when one appears."
    )


async def request_single_episode(query, chat_id: int, series_id: int, season: int, episode: int) -> None:
    try:
        series = sonarr._get(f"/series/{series_id}")
    except sonarr.SonarrError as e:
        await query.edit_message_text(f"❌ {e}")
        return
    ok, message = do_episode_request(series, season, episode, chat_id, query.from_user.id)
    await query.edit_message_text(message)


async def show_missing_episodes(query, chat_id: int, key: str) -> None:
    cached = get_cached_item(chat_id, key)
    if not cached:
        await query.edit_message_text("This search expired, please send the title again.")
        return

    try:
        series = sonarr.find_series_by_tmdb(cached["tmdb_id"])
        if not series:
            await query.edit_message_text(
                f"\"{cached['title']}\" is not tracked in Sonarr, so I can't look up missing episodes."
            )
            return
        missing = sonarr.missing_episodes(series["id"])
    except sonarr.SonarrError as e:
        await query.edit_message_text(f"❌ {e}")
        return

    if not missing:
        await query.edit_message_text(f"No aired episodes are missing for \"{series['title']}\".")
        return

    buttons = [
        [InlineKeyboardButton(
            f"{ep_label(ep)} - {ep.get('title') or 'Untitled'}",
            callback_data=f"epgo:{series['id']}:{ep['seasonNumber']}:{ep['episodeNumber']}",
        )]
        for ep in missing[:12]
    ]
    if len(missing) > 1:
        buttons.append([InlineKeyboardButton(
            f"Search all {len(missing)} missing", callback_data=f"epall:{series['id']}"
        )])
    extra = f"\n(Showing 12 of {len(missing)}.)" if len(missing) > 12 else ""
    await query.edit_message_text(
        f"\"{series['title']}\" - missing episodes, tap one to fetch it:{extra}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def request_all_missing(query, chat_id: int, series_id: int) -> None:
    try:
        series = sonarr._get(f"/series/{series_id}")
        missing = sonarr.missing_episodes(series_id)
        if not missing:
            await query.edit_message_text(f"No aired episodes are missing for \"{series['title']}\".")
            return
        for ep in missing:
            sonarr.search_episode(ep)
    except sonarr.SonarrError as e:
        await query.edit_message_text(f"❌ {e}")
        return

    for ep in missing:
        storage.add_pending(
            ep_pending_key(series_id, ep["seasonNumber"], ep["episodeNumber"]),
            chat_id, query.from_user.id, f"{series['title']} {ep_label(ep)}",
        )
    await query.edit_message_text(
        f"🔎 Searching for {len(missing)} missing episode(s) of \"{series['title']}\".\n"
        f"I'll message you here as they are downloaded."
    )


# ---------------------------------------------------------------------------
# /requests - recent request status
# ---------------------------------------------------------------------------

def fetch_title(media_type: str, tmdb_id) -> str | None:
    if not tmdb_id or media_type not in ("movie", "tv"):
        return None
    try:
        resp = requests.get(f"{OVERSEERR_URL}/api/v1/{media_type}/{tmdb_id}", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("title") or data.get("name")
    except requests.RequestException:
        return None


async def cmd_requests(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        resp = requests.get(
            f"{OVERSEERR_URL}/api/v1/request",
            headers=HEADERS,
            params={"take": 8, "filter": "all", "sort": "added"},
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except requests.RequestException as e:
        await update.message.reply_text(f"Couldn't reach Overseerr: {e}")
        return

    if not results:
        await update.message.reply_text("No requests yet.")
        return

    lines = []
    for r in results:
        media = r.get("media", {})
        media_type = media.get("mediaType", "?")
        tmdb_id = media.get("tmdbId")
        status_icon = MEDIA_STATUS_LABELS.get(media.get("status"), "📥 requested")
        title = fetch_title(media_type, tmdb_id) or "Unknown title"
        lines.append(f"{status_icon} - {title} ({media_type})")

    await update.message.reply_text("Recent requests:\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Webhook receiver (internal LAN only - not exposed to the internet)
# ---------------------------------------------------------------------------

async def handle_webhook(request: web.Request) -> web.Response:
    if WEBHOOK_SECRET and request.headers.get("X-Webhook-Secret") != WEBHOOK_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if payload.get("notification_type") != "MEDIA_AVAILABLE":
        return web.json_response({"ok": True})

    media = payload.get("media") or {}
    media_type = media.get("media_type")
    tmdb_id = media.get("tmdbId")
    if not media_type or not tmdb_id:
        return web.json_response({"ok": True})

    key = f"{media_type}:{tmdb_id}"
    entries = storage.pop_pending(key)

    bot = request.app["bot"]
    for entry in entries:
        try:
            await bot.send_message(
                chat_id=entry["chat_id"],
                text=f"🎬 \"{entry['title']}\" is now available on Plex!",
            )
        except Exception:
            logger.exception("Failed to notify chat %s", entry["chat_id"])

    return web.json_response({"ok": True, "notified": len(entries)})


async def handle_sonarr_webhook(request: web.Request) -> web.Response:
    """Sonarr 'On Import/Download' webhook - notifies whoever asked for that episode.

    Sonarr (v3) cannot always send custom headers, so the shared secret is also
    accepted as a ?secret=... query parameter on the webhook URL.
    """
    supplied = request.headers.get("X-Webhook-Secret") or request.query.get("secret")
    if WEBHOOK_SECRET and supplied != WEBHOOK_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)

    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)

    if payload.get("eventType") not in ("Download", "Import"):
        return web.json_response({"ok": True})  # Test, Grab, Rename, etc.

    series_id = (payload.get("series") or {}).get("id")
    if series_id is None:
        return web.json_response({"ok": True})

    bot = request.app["bot"]
    notified = 0
    for ep in payload.get("episodes") or []:
        season, number = ep.get("seasonNumber"), ep.get("episodeNumber")
        if season is None or number is None:
            continue
        for entry in storage.pop_pending(ep_pending_key(series_id, season, number)):
            try:
                await bot.send_message(
                    chat_id=entry["chat_id"],
                    text=f"🎬 \"{entry['title']}\" is now available on Plex!",
                )
                notified += 1
            except Exception:
                logger.exception("Failed to notify chat %s", entry["chat_id"])

    return web.json_response({"ok": True, "notified": notified})


async def start_webhook_server(application: Application) -> web.AppRunner:
    app = web.Application()
    app["bot"] = application.bot
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_post("/sonarr-webhook", handle_sonarr_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logger.info("Webhook server listening on port %s", WEBHOOK_PORT)
    return runner


async def post_init(application: Application) -> None:
    application.bot_data["webhook_runner"] = await start_webhook_server(application)


async def post_shutdown(application: Application) -> None:
    runner = application.bot_data.get("webhook_runner")
    if runner:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("requests", cmd_requests))
    app.add_handler(CommandHandler("episode", cmd_episode))
    app.add_handler(CommandHandler("adduser", cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("listusers", cmd_listusers))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, search_media))

    logger.info("Bot starting with polling (no inbound internet ports needed)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
