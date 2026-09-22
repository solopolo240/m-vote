import os
import json
import logging
import asyncio
import glob
import difflib
from datetime import datetime
from typing import List, Dict, Optional
import yt_dlp
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError
import time
import re
import socket
import random

from config import (
    BOT_TOKEN, CHAT_ID, SEARCH_QUERIES, DOWNLOAD_DIR,
    AUDIO_FORMAT, AUDIO_QUALITY, MAX_RESULTS_PER_QUERY,
    CHECK_INTERVAL_MINUTES, MIN_DURATION, MAX_DURATION
)

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler('bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

LOCAL_API_URL = 'http://localhost:8081/bot'


class MezmurSearchBot:
    def __init__(self):
        self.bot = Bot(token=BOT_TOKEN, base_url=LOCAL_API_URL)
        self.download_dir = DOWNLOAD_DIR
        self.processed_file = 'processed_videos.json'
        self.processed_videos = self.load_processed_videos()
        self.keywords_file = 'keywords.json'
        self.keywords = self.load_keywords()
        self.settings_file = 'settings.json'
        self.settings = self.load_settings()
        os.makedirs(self.download_dir, exist_ok=True)

        socket.setdefaulttimeout(30)

        self.user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0',
        ]

        if os.system('ffmpeg -version > /dev/null 2>&1') != 0:
            logger.warning("⚠️  ffmpeg not found in PATH — audio conversion WILL fail!")

    # ---------- persistence ----------

    def load_settings(self) -> Dict:
        defaults = {
            'min_duration': MIN_DURATION if MIN_DURATION else 60,
            'max_duration': MAX_DURATION if MAX_DURATION else 900,
            'duration_filter_enabled': True,
            'keywords_filter_enabled': True,
            'max_results': MAX_RESULTS_PER_QUERY,
        }
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    for k in defaults:
                        loaded.setdefault(k, defaults[k])
                    return loaded
            except Exception:
                return defaults
        return defaults

    def save_settings(self):
        with open(self.settings_file, 'w', encoding='utf-8') as f:
            json.dump(self.settings, f, ensure_ascii=False, indent=2)

    def load_keywords(self) -> List[str]:
        if os.path.exists(self.keywords_file):
            try:
                with open(self.keywords_file, 'r', encoding='utf-8') as f:
                    return json.load(f).get('keywords', [])
            except Exception:
                return []
        return []

    def save_keywords(self):
        with open(self.keywords_file, 'w', encoding='utf-8') as f:
            json.dump({'keywords': self.keywords}, f, ensure_ascii=False, indent=2)

    def load_processed_videos(self) -> Dict:
        if os.path.exists(self.processed_file):
            try:
                with open(self.processed_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                return {'videos': [], 'last_check': None}
        return {'videos': [], 'last_check': None}

    def save_processed_videos(self):
        with open(self.processed_file, 'w', encoding='utf-8') as f:
            json.dump(self.processed_videos, f, ensure_ascii=False, indent=2)

    def is_video_processed(self, video_id: str) -> bool:
        return any(v.get('id') == video_id for v in self.processed_videos.get('videos', []))

    def mark_video_processed(self, video_id: str, title: str, url: str, duration: int = 0):
        self.processed_videos.setdefault('videos', []).append({
            'id': video_id,
            'title': title,
            'url': url,
            'duration': duration,
            'processed_at': datetime.now().isoformat()
        })
        self.processed_videos['last_check'] = datetime.now().isoformat()
        self.save_processed_videos()

    # ---------- keywords ----------

    def get_all_keywords(self) -> List[str]:
        defaults = [
            'mezmur', 'ዝማሬ', 'ዘማሪ', 'ዲያቆን', 'diakon',
            'orthodox', 'ኦርቶዶክስ', 'tewahedo', 'ተዋሕዶ',
            'church', 'ቤተክርስቲያን', 'gospel', 'ወንጌል'
        ]
        return list(set(defaults + self.keywords))

    # ---------- dedup ----------

    def _normalize_title(self, title: str) -> str:
        if not title:
            return ""
        t = title.lower()
        t = re.sub(r'[^\w\s\u1200-\u137F]', ' ', t)
        for w in ['like', 'mezemran', 'mezmur', 'mezmurat', 'official', 'video',
                  'audio', 'new', 'ethiopia', 'ethiopian', 'orthodox', 'tewahedo',
                  'song', 'music', 'hymn', 'full', 'hd', 'live', 'remix']:
            t = re.sub(rf'\b{w}\b', ' ', t)
        return re.sub(r'\s+', ' ', t).strip()

    def _titles_similar(self, a: str, b: str, threshold: float = 0.80) -> bool:
        na, nb = self._normalize_title(a), self._normalize_title(b)
        if not na or not nb:
            return False
        if na in nb or nb in na:
            return True
        return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold

    def _is_duplicate(self, video: Dict, seen: List[Dict] = None) -> bool:
        title = video.get('title', '')
        duration = video.get('duration', 0) or 0
        vid = video.get('id')

        candidates = list(self.processed_videos.get('videos', []))
        if seen:
            candidates += [{'id': v.get('id'), 'title': v.get('title', ''),
                            'duration': v.get('duration', 0)} for v in seen]

        for c in candidates:
            if c.get('id') == vid:
                continue
            c_title = c.get('title', '')
            c_dur = c.get('duration', 0) or 0
            if duration and c_dur:
                if abs(duration - c_dur) > 15:
                    continue
                if self._titles_similar(title, c_title, 0.80):
                    logger.info(f"⏭  Duplicate: '{title[:60]}' ≈ '{c_title[:60]}'")
                    return True
            elif self._titles_similar(title, c_title, 0.92):
                logger.info(f"⏭  Duplicate: '{title[:60]}'")
                return True
        return False

    # ---------- yt-dlp ----------

    def _ydl_opts(self, for_download: bool = False) -> Dict:
        opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 60,
            'retries': 10,
            'fragment_retries': 10,
            'extractor_retries': 10,
            'sleep_interval': 5,
            'max_sleep_interval': 10,
            'sleep_interval_requests': 2,
            'user_agent': random.choice(self.user_agents),
            'headers': {
                'Accept-Language': 'en-US,en;q=0.5',
                'Connection': 'keep-alive',
            },
            'extractor_args': {
                'youtube': {
                    'player_client': ['android', 'web'],
                    'player_client_fallback': 'web',
                }
            },
            'ignoreerrors': False,
        }
        if for_download:
            opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': AUDIO_FORMAT,
                    'preferredquality': AUDIO_QUALITY,
                }],
                'noplaylist': True,
                'continuedl': True,
                'nocheckcertificate': True,
            })
        else:
            opts.update({'extract_flat': True})
        return opts

    # ---------- network ----------

    def check_internet(self) -> bool:
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=5)
            return True
        except OSError:
            return False

    # ---------- search ----------

    def search_youtube(self, query: str, target_count: int = 5) -> List[Dict]:
        videos = []
        if not self.check_internet():
            logger.error("No internet. Skipping search.")
            return videos

        fetch_count = min(max(target_count * 3, 10), 50)
        logger.info(f"Fetching up to {fetch_count} candidates (target: {target_count})")

        variations = [
            query, f"{query} mezmur", f"{query} orthodox",
            f"{query} ዝማሬ", f"{query} ኦርቶዶክስ",
        ]

        for sq in variations:
            try:
                logger.info(f"Search: ytsearch{fetch_count}:{sq}")
                with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
                    results = ydl.extract_info(f"ytsearch{fetch_count}:{sq}", download=False)

                if not (results and results.get('entries')):
                    continue

                for entry in results['entries']:
                    if not (entry and entry.get('id')):
                        continue
                    if any(v.get('id') == entry.get('id') for v in videos):
                        continue

                    duration = entry.get('duration', 0) or 0

                    if self.settings.get('duration_filter_enabled', True):
                        mn = self.settings.get('min_duration', 60)
                        mx = self.settings.get('max_duration', 900)
                        if duration < mn or duration > mx:
                            logger.info(f"Skip (duration {duration}s): {entry.get('title', '')[:50]}")
                            continue

                    if self.is_video_processed(entry.get('id')):
                        continue

                    videos.append({
                        'id': entry.get('id'),
                        'title': entry.get('title', 'Unknown'),
                        'url': f"https://www.youtube.com/watch?v={entry.get('id')}",
                        'duration': duration,
                        'uploader': entry.get('uploader', 'Unknown'),
                        'description': (entry.get('description') or '')[:200],
                    })

                if len(videos) >= target_count:
                    logger.info(f"Enough candidates ({len(videos)})")
                    break

            except Exception as e:
                msg = str(e)
                if "Sign in to confirm" in msg:
                    logger.warning(f"Auth required: {sq}")
                elif "rate limit" in msg.lower():
                    logger.warning("Rate limited, waiting...")
                    time.sleep(10)
                else:
                    logger.error(f"Search error: {msg[:200]}")
                continue

        logger.info(f"Total candidates: {len(videos)}")
        return videos

    def filter_relevant(self, videos: List[Dict]) -> List[Dict]:
        if not self.settings.get('keywords_filter_enabled', True):
            return videos
        kws = self.get_all_keywords()
        return [
            v for v in videos
            if any(k.lower() in (v.get('title') or '').lower()
                   or k.lower() in (v.get('description') or '').lower()
                   for k in kws)
        ]

    # ---------- download ----------

    def download_audio(self, url: str, title: str) -> tuple:
        safe = re.sub(r'[^\w\s-]', '', title)[:80].strip() or f"audio_{int(time.time())}"
        outtmpl = os.path.join(self.download_dir, f'{safe}.%(ext)s')

        ydl_opts = self._ydl_opts(for_download=True)
        ydl_opts['outtmpl'] = outtmpl
        ydl_opts['ignoreerrors'] = False

        captured = {'path': None}

        def pp_hook(d):
            if d.get('status') == 'finished':
                captured['path'] = d.get('filepath')

        ydl_opts['postprocessor_hooks'] = [pp_hook]

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            if captured['path'] and os.path.exists(captured['path']):
                return captured['path'], (info or {}).get('title', title)

            for d in (info or {}).get('requested_downloads', []) or []:
                fp = d.get('filepath')
                if fp and os.path.exists(fp):
                    return fp, (info or {}).get('title', title)

            matches = glob.glob(os.path.join(self.download_dir, f'{safe}.*'))
            for m in matches:
                if m.lower().endswith(f'.{AUDIO_FORMAT.lower()}'):
                    return m, (info or {}).get('title', title)
            if matches:
                return matches[0], (info or {}).get('title', title)

            logger.error(f"No file found for '{safe}'")
            return None, None
        except Exception as e:
            logger.error(f"Download error: {str(e)[:300]}")
            return None, None

    # ---------- upload ----------

    async def send_to_telegram(self, audio_path: str, title: str, video_info: Dict) -> bool:
        file_size = os.path.getsize(audio_path)
        logger.info(f"Uploading '{title}' ({file_size / 1024 / 1024:.2f} MB)")

        for attempt in range(1, 4):
            try:
                with open(audio_path, 'rb') as f:
                    await self.bot.send_audio(
                        chat_id=CHAT_ID,
                        audio=f,
                        title=title,
                        performer=video_info.get('uploader', 'Unknown'),
                        write_timeout=600,
                        read_timeout=600,
                        connect_timeout=30,
                    )
                logger.info(f"✅ Sent: {title}")
                return True
            except Exception as e:
                logger.warning(f"Attempt {attempt}/3: {type(e).__name__}: {str(e)[:150]}")
                if attempt < 3:
                    await asyncio.sleep(2 * attempt)

        logger.error(f"❌ Upload failed: {title}")
        return False

    # ---------- cleanup helper ----------

    async def _delete_messages(self, chat_id: int, *message_ids):
        """Delete messages, ignoring errors."""
        for mid in message_ids:
            if not mid:
                continue
            try:
                await self.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception:
                pass

    # ---------- main pipeline ----------

    async def process_search(self, search_query: Optional[str] = None,
                             target_count: int = None,
                             chat_id: int = None,
                             status_msg_id: int = None,
                             user_msg_id: int = None) -> int:
        if target_count is None:
            target_count = self.settings.get('max_results', MAX_RESULTS_PER_QUERY)

        logger.info(f"=== Search start (target {target_count}) ===")
        total = 0
        cleared = False
        queries = [search_query] if search_query else SEARCH_QUERIES

        for query in queries:
            if not self.check_internet():
                logger.error("No internet. Abort.")
                break

            logger.info(f"Query: '{query}'")
            videos = self.search_youtube(query, target_count)

            candidates = videos if search_query is not None else self.filter_relevant(videos)

            if not candidates:
                logger.info(f"No candidates for '{query}'")
                continue

            seen, delivered = [], 0

            for video in candidates:
                if delivered >= target_count:
                    logger.info(f"Target {target_count} reached")
                    break
                if self._is_duplicate(video, seen):
                    continue

                vid = video.get('id')
                title = video.get('title', 'Unknown')
                dur = video.get('duration', 0)
                dur_str = f"{dur // 60}:{dur % 60:02d}" if dur else "?"

                logger.info(f"({delivered+1}/{target_count}) {title} [{dur_str}]")

                loop = asyncio.get_running_loop()
                path, audio_title = await loop.run_in_executor(
                    None, self.download_audio, video['url'], title
                )

                if not (path and os.path.exists(path)):
                    logger.error(f"Download failed: {title}")
                    continue

                if await self.send_to_telegram(path, audio_title, video):
                    # FIX: clear status + user message on first successful send
                    if not cleared and chat_id:
                        await self._delete_messages(chat_id, status_msg_id, user_msg_id)
                        cleared = True

                    self.mark_video_processed(vid, title, video['url'], duration=dur)
                    total += 1
                    delivered += 1
                    seen.append(video)
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                else:
                    logger.error(f"Send failed: {title}")

                await asyncio.sleep(3)

            if delivered < target_count:
                logger.warning(f"Only {delivered}/{target_count} delivered for '{query}'")

        logger.info(f"=== Search done. Delivered {total} ===")
        return total


# ---------- Telegram command handlers ----------

async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data.get('bot_instance')
    if not bot:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /search <query> [count]\n"
            "Examples:\n"
            "  /search asasbi\n"
            "  /search asasbi 5\n"
            "  /search yilma hailu mekibib 3"
        )
        return

    count = None
    if len(args) > 1 and args[-1].isdigit():
        count = int(args[-1])
        query = " ".join(args[:-1])
    else:
        query = " ".join(args)

    if count is not None and not (1 <= count <= 50):
        await update.message.reply_text("Count must be 1–50.")
        return
    if not query.strip():
        await update.message.reply_text("Please provide a query.")
        return

    target = count if count else bot.settings.get('max_results', MAX_RESULTS_PER_QUERY)
    chat_id = update.effective_chat.id
    user_msg_id = update.message.message_id

    # Status message — will be deleted when first audio arrives
    status_msg = await update.message.reply_text(f"🔍 Searching '{query}' (max {target})...")
    status_msg_id = status_msg.message_id

    try:
        delivered = await bot.process_search(
            query, count,
            chat_id=chat_id,
            status_msg_id=status_msg_id,
            user_msg_id=user_msg_id,
        )

        if delivered == 0:
            # Nothing arrived — repurpose the status message as the result
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=f"❌ Nothing found for '{query}'"
                )
                await bot._delete_messages(chat_id, user_msg_id)
            except Exception:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"❌ Nothing found for '{query}'"
                )
        elif delivered < target:
            # Some audio already sent; status may already be deleted
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"⚠️ Delivered {delivered}/{target}"
            )
    except Exception as e:
        logger.error(f"Search error: {e}")
        try:
            await context.bot.send_message(chat_id=chat_id, text=f"Error: {str(e)[:150]}")
        except Exception:
            pass


async def keyword_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data.get('bot_instance')
    if not bot:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "`/keyword list`\n"
            "`/keyword add <word>`\n"
            "`/keyword remove <word>`\n"
            "`/keyword toggle`",
            parse_mode='Markdown'
        )
        return

    action = args[0].lower()
    if action == "list":
        custom = bot.keywords
        status = "on" if bot.settings.get('keywords_filter_enabled', True) else "off"
        msg = f"📝 Filter: **{status}** ({len(bot.get_all_keywords())} total)\n"
        msg += "🔹 Defaults: mezmur, ዝማሬ, ዘማሪ, ዲያቆን, orthodox, ኦርቶዶክስ, tewahedo, ተዋሕዶ, church, ቤተክርስቲያን, gospel, ወንጌል\n"
        msg += f"🔸 Custom: {', '.join(custom) if custom else '_(none)_'}"
        await update.message.reply_text(msg, parse_mode='Markdown')
    elif action == "toggle":
        cur = bot.settings.get('keywords_filter_enabled', True)
        bot.settings['keywords_filter_enabled'] = not cur
        bot.save_settings()
        await update.message.reply_text(f"✅ Keyword filter {'on' if not cur else 'off'}")
    elif action == "add":
        if len(args) < 2:
            await update.message.reply_text("Usage: /keyword add <word>")
            return
        kw = args[1].lower()
        if kw in bot.keywords:
            await update.message.reply_text(f"Already exists: {kw}")
            return
        bot.keywords.append(kw)
        bot.save_keywords()
        await update.message.reply_text(f"✅ Added: {kw}")
    elif action == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /keyword remove <word>")
            return
        kw = args[1].lower()
        if kw not in bot.keywords:
            await update.message.reply_text(f"Not found: {kw}")
            return
        bot.keywords.remove(kw)
        bot.save_keywords()
        await update.message.reply_text(f"✅ Removed: {kw}")
    else:
        await update.message.reply_text(f"Unknown action: {action}")


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data.get('bot_instance')
    if not bot:
        return
    s = bot.settings
    dur = "on" if s.get('duration_filter_enabled', True) else "off"
    kw = "on" if s.get('keywords_filter_enabled', True) else "off"
    await update.message.reply_text(
        f"⚙️ **Settings**\n\n"
        f"Duration filter: **{dur}** ({s.get('min_duration', 60)}–{s.get('max_duration', 900)}s)\n"
        f"Keyword filter: **{kw}** ({len(bot.get_all_keywords())} words)\n"
        f"Default results: **{s.get('max_results', MAX_RESULTS_PER_QUERY)}**\n"
        f"Server: local Bot API",
        parse_mode='Markdown'
    )


async def duration_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot = context.bot_data.get('bot_instance')
    if not bot:
        return
    args = context.args
    if not args:
        await update.message.reply_text(
            "`/duration enable|disable`\n"
            "`/duration set <min_s> <max_s>`\n"
            "`/duration show`",
            parse_mode='Markdown'
        )
        return

    action = args[0].lower()
    if action == "enable":
        bot.settings['duration_filter_enabled'] = True
        bot.save_settings()
        await update.message.reply_text("✅ Duration filter on")
    elif action == "disable":
        bot.settings['duration_filter_enabled'] = False
        bot.save_settings()
        await update.message.reply_text("✅ Duration filter off")
    elif action == "set":
        if len(args) < 3:
            await update.message.reply_text("Usage: /duration set <min_s> <max_s>")
            return
        try:
            mn, mx = int(args[1]), int(args[2])
            if mn < 0 or mx < 0 or mn > mx:
                await update.message.reply_text("Invalid range.")
                return
            bot.settings['min_duration'] = mn
            bot.settings['max_duration'] = mx
            bot.settings['duration_filter_enabled'] = True
            bot.save_settings()
            await update.message.reply_text(f"✅ Set: {mn}s – {mx}s")
        except ValueError:
            await update.message.reply_text("Numbers only.")
    elif action == "show":
        s = bot.settings
        st = "on" if s.get('duration_filter_enabled', True) else "off"
        await update.message.reply_text(
            f"Duration filter: **{st}**\nMin: {s.get('min_duration', 60)}s\nMax: {s.get('max_duration', 900)}s",
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(f"Unknown action: {action}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 **Mezmur Search Bot**\n\n"
        "**Search**\n"
        "`/search <query> [count]`\n"
        "  `/search asasbi`\n"
        "  `/search asasbi 5`\n"
        "  `/search yilma hailu mekibib 3`\n\n"
        "**Filters**\n"
        "`/keyword add|remove|list|toggle`\n"
        "`/duration enable|disable|set|show`\n\n"
        "**Info**\n"
        "`/settings` — current settings\n"
        "`/help` — this message\n\n"
        "_Count = how many to deliver; the bot fetches extras to compensate for filters._",
        parse_mode='Markdown'
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await help_command(update, context)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update error: {context.error}", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                f"⚠️ Error: {str(context.error)[:200]}"
            )
        except Exception:
            pass


# ---------- setup ----------

def setup_handlers(app: Application, bot_instance: MezmurSearchBot):
    app.bot_data['bot_instance'] = bot_instance
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("keyword", keyword_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("duration", duration_command))
    app.add_error_handler(error_handler)
    logger.info("Handlers registered")


async def run_telegram_bot():
    bot_instance = MezmurSearchBot()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(LOCAL_API_URL)
        .local_mode(True)
        .build()
    )

    setup_handlers(app, bot_instance)

    logger.info("Starting bot with local Bot API server...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logger.info("Bot running. Press Ctrl+C to stop.")

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Mezmur Search Bot')
    parser.add_argument('--telegram', action='store_true', help='Run Telegram bot (default)')
    parser.parse_args()
    asyncio.run(run_telegram_bot())


if __name__ == '__main__':
    main()
