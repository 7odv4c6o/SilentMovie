# -*- coding: utf-8 -*-
"""
bot.py
------
ربات تلگرامی فیلم/سریال «SilentMovie».

امکانات کاربر:
  • جستجوی فیلم با نوشتن نام آن (یا از طریق دکمه‌ی «🔍 جستجوی فیلم»)
  • نمایش کارت فیلم با پوستر و اطلاعات کامل
  • دکمه‌های شیشه‌ای برای هر قسمت → ساخت لینک تازه‌ی VLC
  • علاقه‌مندی‌ها (Favorites)
  • عضویت اجباری در کانال‌ها

پنل مدیریت (دکمه‌ی «🛠 پنل مدیریت» یا /admin):
  • آمار، مدیریت کانال‌های عضویت اجباری، پیام همگانی، مدیریت ادمین‌ها، لاگ خطا
  • ارسال دستی فایل دیتابیس و بازیابی آن از فایل آپلودی

زمان‌بندی:
  • هر N ساعت (پیش‌فرض ۲) فایل دیتابیس برای همه‌ی ادمین‌ها ارسال می‌شود
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import time
import urllib.parse
from dataclasses import asdict
from datetime import time as dt_time
from typing import Dict, List, Optional

import requests
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
                      Update, InlineQueryResultArticle, InlineQueryResultPhoto,
                      InputTextMessageContent)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, InlineQueryHandler, MessageHandler,
                          filters)

import config
import keyboards as kb
from database import Database
from formatting import esc, movie_caption, play_message, webapp_play_message
from site_client import BASE, Episode, Movie, SearchResult, SiteClient, LoginError
from serialirany_client import SerialiranyClient, MOVIE_ARCHIVE, SERIES_ARCHIVE
from categorize import (categorize_with_indices, get_available_types,
                      QUALITY_LABELS, TYPE_LABELS)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"

# ---------------- وضعیت سراسری ----------------
db: Database = None            # مقداردهی در main
site: SiteClient = None        # مقداردهی در main
ir_client: SerialiranyClient = None  # مقداردهی در main
# حالت گفتگوی ادمین (منتظر ورودی): user_id -> action
pending_admin: Dict[int, str] = {}
# کش لیست فیلم/سریال ایرانی: user_id -> {"type": "movie"|"series", "items": [...], "page": int}
iranian_cache: Dict[int, dict] = {}
# کش نتایج inline ایرانی: cache_key -> {"title": "...", "url": "...", "thumb": "..."}
ir_inline_cache: Dict[str, dict] = {}


# ---------------- کمک‌کننده‌ها ----------------
def super_admin() -> Optional[int]:
    admins = db.list_admins()
    return admins[0] if admins else (config.ADMIN_IDS[0] if config.ADMIN_IDS else None)


async def is_member_all_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> List[dict]:
    """کانال‌هایی که کاربر عضو آن‌ها نیست را برمی‌گرداند (لیست خالی = عضو همه)."""
    not_joined = []
    for ch in db.list_channels():
        chat_id = ch["chat_id"]
        try:
            member = await context.bot.get_chat_member(chat_id, user_id)
            if member.status in ("left", "kicked"):
                not_joined.append(dict(ch))
        except TelegramError as e:
            # اگر ربات ادمین کانال نباشد یا خطا بدهد، آن کانال را نادیده می‌گیریم
            log.warning("بررسی عضویت کانال %s ناموفق: %s", chat_id, e)
            db.log_error("membership_check", f"{chat_id}: {e}")
    return not_joined


async def require_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """اگر عضو نیست، پیام عضویت اجباری می‌فرستد و False برمی‌گرداند."""
    user_id = update.effective_user.id
    if db.is_admin(user_id):
        return True
    missing = await is_member_all_channels(user_id, context)
    if not missing:
        return True
    text = ("🔒 برای استفاده از ربات ابتدا در کانال‌های زیر عضو شوید، "
            "سپس روی «✅ عضو شدم» بزنید:")
    markup = kb.join_kb(missing)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)
    return False


def cache_movie(movie: Movie) -> None:
    try:
        payload = json.dumps(_movie_to_dict(movie), ensure_ascii=False)
        db.cache_put(movie.movie_id, payload)
    except Exception as e:
        log.warning("cache_put failed: %s", e)


def _movie_to_dict(movie: Movie) -> dict:
    d = asdict(movie)
    return d


def _movie_from_dict(d: dict) -> Movie:
    """بازسازی Movie از dict.
    این متد سختگیرانه است: اگر فیلد جدیدی (مثل quality_links یا season_titles)
    در داده‌ی کش‌شده نباشد، از مقدار پیش‌فرض استفاده می‌کند. همچنین
    فیلدهای ناشناخته را نادیده می‌گیرد (برای سازگاری با کش‌های قدیمی).
    """
    # فیلتر کردن کلیدهای نامعتبر Episode و افزودن پیش‌فرض
    valid_ep_keys = {"part", "quality", "size", "play_url", "filename", "quality_links"}
    eps = []
    for e in d.get("episodes", []):
        clean_e = {k: v for k, v in e.items() if k in valid_ep_keys}
        # اگر quality_links نبود، یک لیست خالی می‌سازیم
        if "quality_links" not in clean_e:
            clean_e["quality_links"] = []
        eps.append(Episode(**clean_e))
    
    # فیلتر کردن کلیدهای نامعتبر Movie
    valid_movie_keys = {"movie_id", "title", "original_title", "year", "imdb",
                         "genre", "country", "age", "director", "stars", "poster",
                         "plot", "episodes", "seasons", "season_titles"}
    clean_d = {k: v for k, v in d.items() if k in valid_movie_keys}
    clean_d["episodes"] = eps
    # اگر season_titles نبود، یک dict خالی می‌سازیم
    if "season_titles" not in clean_d:
        clean_d["season_titles"] = {}
    return Movie(**clean_d)


def get_movie_cached(movie_id: str) -> Movie:
    """اول از کش، بعد از سایت."""
    payload = db.cache_get(movie_id, config.MOVIE_CACHE_TTL)
    if payload:
        try:
            return _movie_from_dict(json.loads(payload))
        except Exception:
            pass
    movie = site.movie(movie_id)
    cache_movie(movie)
    return movie


async def download_bytes(url: str) -> Optional[bytes]:
    """دانلود پوستر در ترد جداگانه — با سشن سایت و پشتیبانی از فرمت‌های مختلف."""
    def _dl():
        try:
            # اول سشن سایت را امتحان کن (عکس ممکنه پشت لاگین باشه)
            if site and site.s.cookies:
                r = site.s.get(url, headers={"User-Agent": UA, "Referer": BASE},
                               timeout=25, verify=False)
                if r.status_code == 200 and len(r.content) > 2000:
                    return r.content
            # بعد بدون سشن (CDN عمومی)
            r = requests.get(url, headers={"User-Agent": UA, "Referer": BASE},
                             timeout=25, verify=False)
            if r.status_code == 200 and len(r.content) > 2000:
                return r.content
        except Exception:
            pass
        return None
    return await asyncio.to_thread(_dl)


# ---------------- دستورات کاربر ----------------
WELCOME = (
    "🎬 <b>به ربات SilentMovie خوش آمدید!</b>\n\n"
    "کافیست نام فیلم یا سریال مورد نظرتان را بنویسید تا برایتان جستجو کنم.\n"
    "مثال: <code>the lord of the rings</code>\n\n"
    "پس از انتخاب فیلم، اطلاعات کامل همراه پوستر نمایش داده می‌شود و می‌توانید "
    "روی هر قسمت بزنید تا لینک پخش در <b>VLC</b> ساخته شود.\n\n"
    "از دکمه‌های پایین صفحه استفاده کنید 👇"
)

SEARCH_PROMPT = "🔍 نام فیلم یا سریالی که می‌خواهید را بنویسید:"

# مسیر عکس خوش‌آمد؛ اگر این فایل موجود باشد همراه پیام استارت نمایش داده می‌شود.
WELCOME_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "assets", "welcome.jpg")


async def send_welcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام خوش‌آمد را با عکس (در صورت وجود) و دکمه‌های شیشه‌ای می‌فرستد."""
    inline_menu = kb.start_inline_kb(is_admin=db.is_admin(update.effective_user.id))
    if os.path.exists(WELCOME_IMAGE):
        try:
            with open(WELCOME_IMAGE, "rb") as f:
                await update.effective_message.reply_photo(
                    photo=InputFile(f, filename="welcome.jpg"),
                    caption=WELCOME, parse_mode=ParseMode.HTML,
                    reply_markup=inline_menu)
            return
        except Exception as e:
            log.warning("ارسال عکس خوش‌آمد ناموفق بود: %s", e)
    await update.effective_message.reply_html(WELCOME, reply_markup=inline_menu)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    # اگر ادمین اولیه هنوز ثبت نشده، ثبتش کن
    for aid in config.ADMIN_IDS:
        if not db.is_admin(aid):
            db.add_admin(aid)

    # پشتیبانی از دیپ‌لینک: /start movie_<id>  →  باز کردن مستقیم کارت فیلم
    # پشتیبانی از جستجوی inline ایرانی: /start ir_<cache_key>
    args = context.args or []
    if args:
        arg0 = args[0]
        if arg0.startswith("movie_"):
            movie_id = arg0[len("movie_"):].strip()
            if movie_id.isdigit():
                context.user_data["pending_movie_id"] = movie_id
                if not db.is_admin(u.id):
                    missing = await is_member_all_channels(u.id, context)
                    if missing:
                        text = ("🔒 برای دیدن این فیلم ابتدا در کانال‌های زیر عضو شوید، "
                                "سپس روی «✅ عضو شدم» بزنید:")
                        await update.effective_message.reply_text(
                            text, reply_markup=kb.join_kb(missing))
                        return
                context.user_data.pop("pending_movie_id", None)
                await _show_movie_card_from_message(update, context, movie_id)
                return

        # دیپ‌لینک inline ایرانی
        if arg0.startswith("ir_"):
            cache_key = arg0[3:]
            item = ir_inline_cache.get(cache_key)
            if not item:
                await update.effective_message.reply_text(
                    "⚠️ نتایج جستجو منقضی شده. دوباره جستجو کنید.")
                return
            # ذخیره در user_data برای استفاده‌ی بعدی
            context.user_data["ir_direct_url"] = item["url"]
            context.user_data["ir_title"] = item["title"]
            context.user_data["ir_search_results"] = [item]
            context.user_data["ir_search_query"] = item["title"]
            # نمایش نتیجه با تشخیص نوع محتوا (فیلم یا سریال)
            msg = update.effective_message
            await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
            try:
                content_type = await asyncio.to_thread(
                    ir_client.detect_content_type, item["url"])
            except Exception as e:
                log.warning("خطا در تشخیص نوع محتوا: %s", e)
                content_type = "movie"

            if content_type == "series":
                # سریال — بررسی ساختار (فصل‌بندی شده یا نه)
                try:
                    structure = await asyncio.to_thread(
                        ir_client.get_series_structure, item["url"])
                except Exception as e:
                    log.exception("iranian series structure error (deeplink)")
                    structure = {"has_seasons": False, "episodes": []}

                if structure.get("has_seasons") and structure.get("seasons"):
                    context.user_data["ir_structure"] = structure
                    rows = []
                    for s in structure["seasons"]:
                        ep_count = len(s.get("episodes", []))
                        label = f"📺 {s['title']} ({ep_count} قسمت)"
                        rows.append([InlineKeyboardButton(
                            label, callback_data=f"irseason:{s['value']}")])
                    rows.append([InlineKeyboardButton(
                        "⬅️ بازگشت به منوی اصلی", callback_data="menu:home")])
                    await msg.reply_text(
                        f"🎬 <b>{esc(item['title'])}</b>\n\n"
                        f"یکی از فصل‌ها رو انتخاب کنید:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=InlineKeyboardMarkup(rows))
                elif structure.get("episodes"):
                    ep_links = structure["episodes"]
                    context.user_data["ir_links"] = ep_links
                    markup = kb.iranian_links_kb(ep_links, title=item["title"])
                    await msg.reply_text(
                        f"🎬 <b>{esc(item['title'])}</b>\n"
                        f"📥 {len(ep_links)} قسمت:\n"
                        f"روی مورد مورد نظر بزنید:",
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup)
                else:
                    await msg.reply_text(
                        f"⚠️ قسمتی برای «{esc(item['title'])}» پیدا نشد.")
            else:
                # فیلم — دکمه پخش آنلاین
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton("▶️ پخش آنلاین", callback_data="irplay:0")],
                    [InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="menu:home")],
                ])
                await msg.reply_html(
                    f"🎬 <b>{esc(item['title'])}</b>\n\n"
                    f"روی «پخش آنلاین» بزنید تا لینک پخش ساخته شود.\n"
                    f"⏳ ممکن است ۱۵-۲۰ ثانیه طول بکشد (استخراج لینک از پلیر).",
                    reply_markup=markup)
            return

    if not await require_membership(update, context):
        return
    await send_welcome(update, context)


async def _show_movie_card_from_message(update: Update,
                                         context: ContextTypes.DEFAULT_TYPE,
                                         movie_id: str) -> None:
    """نمایش کارت فیلم از روی یک پیام مستقیم (نه callback query).
    برای دیپ‌لینک‌ها استفاده می‌شود تا دکمه‌ی نتایج inline به‌درستی کار کند."""
    msg = update.effective_message
    await context.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_PHOTO)
    try:
        movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except LoginError:
        await msg.reply_text("⚠️ ورود به سایت ممکن نشد. بعداً تلاش کنید.")
        return
    except Exception as e:
        log.exception("movie load error (deeplink)")
        db.log_error("movie_deeplink", f"{movie_id}: {e}")
        await msg.reply_text("⚠️ خطا در دریافت اطلاعات فیلم.")
        return

    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    caption = movie_caption(movie)
    # اگر سریال چند فصلی هست، کیبورد فصل‌ها را نشان بده (نه کیفیت)
    if movie.seasons:
        markup = kb.season_select_kb(
            movie.movie_id, movie.seasons, is_fav,
            season_titles=getattr(movie, "season_titles", None)
        )
    else:
        markup = kb.movie_card_kb(movie, is_fav)

    sent = False
    if movie.poster:
        try:
            await msg.reply_photo(
                photo=movie.poster,
                caption=caption[:1024], parse_mode=ParseMode.HTML,
                reply_markup=markup)
            sent = True
        except (BadRequest, TelegramError) as e:
            log.warning("ارسال پوستر با URL ناموفق (deeplink): %s", e)
    if not sent and movie.poster:
        poster_bytes = await download_bytes(movie.poster)
        if poster_bytes:
            try:
                ext = ".jpg"
                if poster_bytes[:4] == b"RIFF":
                    ext = ".webp"
                elif poster_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                await msg.reply_photo(
                    photo=InputFile(io.BytesIO(poster_bytes), filename=f"{movie_id}{ext}"),
                    caption=caption[:1024], parse_mode=ParseMode.HTML, reply_markup=markup)
                sent = True
            except (BadRequest, TelegramError) as e:
                log.warning("ارسال پوستر با bytes ناموفق (deeplink): %s", e)
    if not sent:
        await msg.reply_html(caption[:4096], reply_markup=markup)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(
        "📖 <b>راهنما</b>\n\n"
        "• دکمه‌ی «🔍 جستجوی فیلم» را بزنید یا مستقیم نام فیلم را بنویسید.\n"
        "• روی نتیجه بزنید تا کارت فیلم باز شود.\n"
        "• روی هر قسمت بزنید تا لینک VLC ساخته شود.\n"
        "• «❤️ علاقه‌مندی‌ها» فیلم‌هایی که ثبت کرده‌اید را نشان می‌دهد.\n"
        "• «🕒 تماشا شده‌ها» فیلم‌هایی که لینک پخش گرفته‌اید را نشان می‌دهد.\n"
        "• «📜 جستجوهای اخیر» جستجوهای قبلی شما را با یک کلیک تکرار می‌کند.",
        reply_markup=kb.start_inline_kb(is_admin=db.is_admin(update.effective_user.id)))


async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    if not await require_membership(update, context):
        return
    favs = db.list_favorites(u.id)
    if not favs:
        await update.effective_message.reply_text(
            "لیست علاقه‌مندی‌های شما خالی است. ❤️\n"
            "با زدن «❤️ افزودن به علاقه‌مندی‌ها» روی هر فیلم، آن را اینجا ذخیره کنید.",
            reply_markup=kb.start_inline_kb(is_admin=db.is_admin(u.id)))
        return
    await update.effective_message.reply_text(
        f"❤️ علاقه‌مندی‌های شما ({len(favs)} مورد):",
        reply_markup=kb.favorites_kb(favs))


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """فیلم‌هایی که کاربر تماشا کرده (لینک پخش گرفته)."""
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    if not await require_membership(update, context):
        return
    items = db.list_watch(u.id, 15)
    if not items:
        await update.effective_message.reply_text(
            "🕒 هنوز فیلمی تماشا نکرده‌اید.\n"
            "وقتی روی یک قسمت بزنید و لینک پخش بگیرید، اینجا ثبت می‌شود.",
            reply_markup=kb.start_inline_kb(is_admin=db.is_admin(u.id)))
        return
    await update.effective_message.reply_text(
        f"🕒 فیلم‌هایی که تماشا کرده‌اید ({len(items)} مورد):",
        reply_markup=kb.history_kb(items))


async def cmd_recent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """جستجوهای اخیر کاربر — با یک کلیک دوباره جستجو می‌شوند."""
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    if not await require_membership(update, context):
        return
    queries = db.recent_searches(u.id, 10)
    if not queries:
        await update.effective_message.reply_text(
            "📜 هنوز جستجویی انجام نداده‌اید.",
            reply_markup=kb.start_inline_kb(is_admin=db.is_admin(u.id)))
        return
    # لیست را برای نگاشت ایندکس در callback نگه می‌داریم
    context.user_data["recent"] = queries
    await update.effective_message.reply_text(
        "📜 جستجوهای اخیر شما — روی هرکدام بزنید تا دوباره جستجو شود:",
        reply_markup=kb.recent_searches_kb(queries))


async def on_text_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام متنی: ورودی پنل ادمین یا جستجو.
    ربات فقط وقتی جستجو می‌کند که کاربر قبلاً روی «🔍 جستجوی فیلم» کلیک کرده باشد.
    اگر کاربر بدون کلیک روی دکمه متنی بفرستد، پیام نادیده گرفته می‌شود.
    """
    u = update.effective_user
    text = (update.effective_message.text or "").strip()

    # اگر ادمین در حال وارد کردن چیزی است (اولویت با پنل)
    if u.id in pending_admin:
        await handle_admin_input(update, context, text)
        return

    db.upsert_user(u.id, u.username or "", u.first_name or "")

    # بررسی عضویت اجباری
    if not await require_membership(update, context):
        return
    if not text or text.startswith("/"):
        return

    # اگر کاربر در حالت جستجوی ایرانی است (از دکمه «جستجوی فیلم ایرانی» آمده)
    if context.user_data.get("awaiting_iranian_search"):
        context.user_data.pop("awaiting_iranian_search", None)
        await do_iranian_search(update, context, text)
        return

    # اگر کاربر در حالت جستجوی خارجی است (از دکمه «جستجوی فیلم خارجی» آمده)
    if context.user_data.get("awaiting_foreign_search"):
        context.user_data.pop("awaiting_foreign_search", None)
        await do_search(update, context, text)
        return

    # اگر کاربر بدون کلیک روی دکمه متنی بفرستد، نادیده بگیر
    # (ربات فقط با دکمه‌های شیشه‌ای کار می‌کند)


async def do_iranian_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                              query: str) -> None:
    """جستجوی فیلم/سریال ایرانی در سایت serialirany.com."""
    msg = update.effective_message
    uid = update.effective_user.id
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    await msg.reply_text(f"🔍 در حال جستجو برای «{esc(query)}»...")

    try:
        results = await asyncio.to_thread(ir_client.search_iranian, query)
    except Exception as e:
        log.exception("iranian search error")
        db.log_error("iranian_search", f"{query}: {e}")
        await msg.reply_text("⚠️ خطا در جستجوی ایرانی. دوباره تلاش کنید.")
        return

    if not results:
        await msg.reply_text(
            f"نتیجه‌ای برای «{esc(query)}» پیدا نشد. 🔍\n"
            f"نام دیگری امتحان کنید یا روی «جستجوی فیلم/سریال ایرانی» بزنید.")
        return

    # ذخیره نتایج در user_data برای استفاده‌ی بعدی
    context.user_data["ir_search_results"] = results
    context.user_data["ir_search_query"] = query

    markup = kb.iranian_search_results_kb(results)
    await msg.reply_text(
        f"🔍 نتایج جستجو برای «<b>{esc(query)}</b>» ({len(results)} مورد):",
        parse_mode=ParseMode.HTML,
        reply_markup=markup)


async def cmd_iranian_search_result(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                      idx: int) -> None:
    """نمایش اطلاعات یک نتیجه جستجوی ایرانی.
    تشخیص فیلم یا سریال، سپس نمایش دکمه پخش (برای فیلم) یا فصل‌ها (برای سریال).
    """
    q = update.callback_query
    uid = update.effective_user.id
    await q.answer("در حال بارگذاری...")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)

    results = context.user_data.get("ir_search_results", [])
    if idx < 0 or idx >= len(results):
        await q.answer("نتیجه نامعتبر است.", show_alert=True)
        return

    item = results[idx]
    item_url = item["url"]
    item_title = item["title"]
    item_thumb = item.get("thumb", "")

    # تشخیص نوع محتوا (فیلم یا سریال)
    try:
        content_type = await asyncio.to_thread(ir_client.detect_content_type, item_url)
    except Exception as e:
        log.warning("خطا در تشخیص نوع محتوا: %s", e)
        content_type = "movie"

    if content_type == "series":
        # سریال — بررسی ساختار (فصل‌بندی شده یا نه)
        try:
            structure = await asyncio.to_thread(ir_client.get_series_structure, item_url)
        except Exception as e:
            log.exception("iranian series structure error")
            structure = {"has_seasons": False, "episodes": []}

        if structure.get("has_seasons") and structure.get("seasons"):
            context.user_data["ir_structure"] = structure
            context.user_data["ir_title"] = item_title
            rows = []
            for s in structure["seasons"]:
                ep_count = len(s.get("episodes", []))
                label = f"📺 {s['title']} ({ep_count} قسمت)"
                rows.append([InlineKeyboardButton(
                    label, callback_data=f"irseason:{s['value']}")])
            rows.append([InlineKeyboardButton("⬅️ بازگشت به نتایج", callback_data="irback:0")])
            await q.message.reply_text(
                f"🎬 <b>{esc(item_title)}</b>\n\n"
                f"یکی از فصل‌ها رو انتخاب کنید:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(rows))
        elif structure.get("episodes"):
            ep_links = structure["episodes"]
            context.user_data["ir_links"] = ep_links
            context.user_data["ir_title"] = item_title
            markup = kb.iranian_links_kb(ep_links, title=item_title)
            await q.message.reply_text(
                f"🎬 <b>{esc(item_title)}</b>\n"
                f"📥 {len(ep_links)} قسمت:\n"
                f"روی مورد مورد نظر بزنید:",
                parse_mode=ParseMode.HTML,
                reply_markup=markup)
        else:
            await q.message.reply_text(
                f"⚠️ قسمتی برای «{esc(item_title)}» پیدا نشد.")
    else:
        # فیلم — دکمه پخش آنلاین
        context.user_data["ir_direct_url"] = item_url
        context.user_data["ir_title"] = item_title
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ پخش آنلاین", callback_data="irplay:0")],
            [InlineKeyboardButton("⬅️ بازگشت به نتایج", callback_data="irback:0")],
        ])
        await q.message.reply_text(
            f"🎬 <b>{esc(item_title)}</b>\n\n"
            f"روی «پخش آنلاین» بزنید تا لینک پخش ساخته شود.\n"
            f"⏳ ممکن است ۱۵-۲۰ ثانیه طول بکشد (استخراج لینک از پلیر).",
            parse_mode=ParseMode.HTML,
            reply_markup=markup)


async def do_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    msg = update.effective_message
    await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    db.log_search(update.effective_user.id, query)
    try:
        results = await asyncio.to_thread(site.search, query, 1)
    except LoginError as e:
        db.log_error("search_login", str(e))
        await msg.reply_text("⚠️ ورود به سایت ممکن نشد. کمی بعد دوباره تلاش کنید.")
        return
    except Exception as e:
        log.exception("search error")
        db.log_error("search", f"{query}: {e}")
        await msg.reply_text("⚠️ خطا در جستجو. لطفاً دوباره تلاش کنید.")
        return

    if not results:
        await msg.reply_text(f"نتیجه‌ای برای «{query}» پیدا نشد. 🔍")
        return

    results = results[:config.SEARCH_PAGE_SIZE]
    await msg.reply_text(
        f"🔍 نتایج جستجو برای «<b>{esc(query)}</b>»:",
        parse_mode=ParseMode.HTML,
        reply_markup=kb.search_results_kb(results))


# ---------------- نمایش کارت فیلم ----------------
async def show_movie_card(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          movie_id: str, page: int = 0) -> None:
    q = update.callback_query
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.UPLOAD_PHOTO)
    try:
        movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except LoginError:
        await q.message.reply_text("⚠️ ورود به سایت ممکن نشد. بعداً تلاش کنید.")
        return
    except Exception as e:
        log.exception("movie load error")
        db.log_error("movie", f"{movie_id}: {e}")
        await q.message.reply_text("⚠️ خطا در دریافت اطلاعات فیلم.")
        return

    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    caption = movie_caption(movie)

    # اگر سریال چند فصلی هست، اول فصل رو انتخاب کن
    if movie.seasons:
        markup = kb.season_select_kb(
            movie.movie_id, movie.seasons, is_fav,
            season_titles=getattr(movie, "season_titles", None)
        )
    else:
        markup = kb.movie_card_kb(movie, is_fav)

    sent = False
    # روش ۱: ارسال URL مستقیم به تلگرام (سرورهای تلگرام دانلود می‌کنند)
    if movie.poster:
        try:
            await q.message.reply_photo(
                photo=movie.poster,
                caption=caption[:1024], parse_mode=ParseMode.HTML,
                reply_markup=markup)
            sent = True
            log.info("پوستر با URL مستقیم ارسال شد: %s", movie.poster[:80])
        except (BadRequest, TelegramError) as e:
            log.warning("ارسال پوستر با URL ناموفق (%s)، تلاش با دانلود دستی: %s", e, movie.poster[:80])

    # روش ۲: دانلود دستی با سشن سایت و ارسال bytes
    if not sent and movie.poster:
        poster_bytes = await download_bytes(movie.poster)
        if poster_bytes:
            try:
                ext = ".jpg"
                if poster_bytes[:4] == b"RIFF":
                    ext = ".webp"
                elif poster_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                await q.message.reply_photo(
                    photo=InputFile(io.BytesIO(poster_bytes), filename=f"{movie_id}{ext}"),
                    caption=caption[:1024], parse_mode=ParseMode.HTML, reply_markup=markup)
                sent = True
                log.info("پوستر با bytes ارسال شد (%d bytes)", len(poster_bytes))
            except (BadRequest, TelegramError) as e:
                log.warning("ارسال پوستر با bytes هم ناموفق: %s", e)

    # روش ۳: بدون عکس (فقط متن)
    if not sent:
        log.warning("پوستر ارسال نشد برای فیلم %s — poster='%s'", movie_id, (movie.poster or "")[:100])
        await q.message.reply_html(caption[:4096], reply_markup=markup)


# مرحله 2
async def select_quality(update, context, movie_id, quality, season_value=""):
    q = update.callback_query
    await q.answer()
    try:
        if season_value:
            movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
        else:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception:
        await q.answer("Error", show_alert=True)
        return
    cats = categorize_with_indices(movie.episodes)
    groups = cats.get(quality, {})
    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    types = get_available_types(groups)
    if len(types) <= 1:
        ep_type = types[0] if types else "original"
        await show_episode_list(update, context, movie_id, quality, ep_type,
                                 season_value=season_value)
    else:
        markup = kb.type_select_kb(movie_id, quality, groups, is_fav,
                                     season_value=season_value)
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except BadRequest:
            pass


# مرحله 3
async def show_episode_list(update, context, movie_id, quality, ep_type, page=0, season_value=""):
    q = update.callback_query
    await q.answer()
    try:
        if season_value:
            movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
        else:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception:
        await q.answer("Error", show_alert=True)
        return
    cats = categorize_with_indices(movie.episodes)
    groups = cats.get(quality, {})
    indexed_eps = groups.get(ep_type, [])
    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    markup = kb.episode_list_kb(movie_id, quality, ep_type,
                                  indexed_eps, page, config.SEARCH_PAGE_SIZE,
                                  is_fav, season_value=season_value)
    try:
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        pass


# مرحله جدید: نمایش مستقیم لیست قسمت‌ها (بدون فیلتر کیفیت/نوع)
async def show_episode_list_direct(update, context, movie_id, season_value="",
                                     page: int = 0):
    """نمایش مستقیم لیست قسمت‌ها بدون فیلتر کیفیت/نوع.
    هر قسمت فقط یک بار نمایش داده می‌شود (با تمام کیفیت‌هایش تجمیع شده).
    وقتی کاربر روی یک قسمت کلیک کند، بهترین کیفیت موجود پخش می‌شود.
    """
    q = update.callback_query
    await q.answer()
    try:
        # اگر season_value داده شده، فصل خاص را می‌گیریم
        if season_value:
            movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
        else:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception:
        await q.answer("خطا در بارگذاری قسمت‌ها.", show_alert=True)
        return
    # کش کردن movie برای play_episode (در user_data)
    context.user_data["current_movie"] = movie
    context.user_data["current_season"] = season_value
    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    markup = kb.episode_list_direct_kb(
        movie_id, movie.episodes, page, config.SEARCH_PAGE_SIZE,
        is_fav, season_value=season_value
    )
    try:
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        pass


async def back_to_quality(update, context, movie_id, season_value=""):
    q = update.callback_query
    await q.answer()
    try:
        if season_value:
            movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
        else:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception:
        await q.answer("Error", show_alert=True)
        return
    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    markup = kb.movie_card_kb(movie, is_fav, season_value=season_value)
    try:
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        pass


async def back_to_type(update, context, movie_id, quality, season_value=""):
    q = update.callback_query
    await q.answer()
    try:
        if season_value:
            movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
        else:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception:
        await q.answer("Error", show_alert=True)
        return
    cats = categorize_with_indices(movie.episodes)
    groups = cats.get(quality, {})
    is_fav = db.is_favorite(update.effective_user.id, movie_id)
    types = get_available_types(groups)
    if len(types) <= 1:
        await back_to_quality(update, context, movie_id, season_value)
    else:
        markup = kb.type_select_kb(movie_id, quality, groups, is_fav,
                                     season_value=season_value)
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except BadRequest:
            pass


# پخش (WebApp یا لینک VLC) ----------------
async def play_episode(update: Update, context: ContextTypes.DEFAULT_TYPE,
                       movie_id: str, ep_index: int,
                       season_value: str = "") -> None:
    """پخش یک قسمت.
    اگر season_value داده شود، از movie ذخیره‌شده در user_data استفاده می‌کند
    (که شامل قسمت‌های فصل انتخاب‌شده است). در غیر این‌صورت از cache اصلی استفاده می‌کند.
    """
    q = update.callback_query
    await q.answer("در حال ساخت لینک پخش…")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
    try:
        # اولویت ۱: movie ذخیره‌شده در user_data (مربوط به فصل انتخاب‌شده)
        movie = context.user_data.get("current_movie")
        if not movie or (season_value and season_value != context.user_data.get("current_season")):
            # اولویت ۲: گرفتن movie از سایت
            if season_value:
                movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
            else:
                movie = await asyncio.to_thread(get_movie_cached, movie_id)
        if ep_index < 0 or ep_index >= len(movie.episodes):
            await q.message.reply_text("قسمت نامعتبر است.")
            return
        ep = movie.episodes[ep_index]
        vlc_link = await asyncio.to_thread(site.resolve_play, ep.play_url, movie_id)
    except LoginError:
        await q.message.reply_text("⚠️ ورود به سایت ممکن نشد. بعداً تلاش کنید.")
        return
    except Exception as e:
        log.exception("play error")
        db.log_error("play", f"{movie_id}/{ep_index}: {e}")
        await q.message.reply_text("⚠️ خطا در ساخت لینک پخش.")
        return

    if not vlc_link:
        await q.message.reply_text("⚠️ لینک پخش در دسترس نیست. دوباره تلاش کنید.")
        return

    http_link = SiteClient.vlc_to_http(vlc_link)
    # ثبت در تاریخچه‌ی تماشا
    try:
        db.add_watch(update.effective_user.id, movie_id, movie.title, ep.label)
    except Exception:
        pass

    # دکمه‌ی تماشا — لینک پیچ‌شده با سایت پلیر Vercel
    await q.message.reply_html(
        webapp_play_message(movie, ep),
        reply_markup=kb.play_kb(http_link, title=movie.title, episode=ep.label),
        disable_web_page_preview=True)


# مرحله جدید: نمایش کیفیت‌های موجود برای یک قسمت
async def show_episode_qualities(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                  movie_id: str, ep_index: int,
                                  season_value: str = "") -> None:
    """نمایش کیفیت‌های موجود برای یک قسمت.
    اگر قسمت فقط یک کیفیت دارد، مستقیم پخش می‌شود.
    اگر چند کیفیت دارد (مثلا 480/720/1080)، کیبورد انتخاب نمایش داده می‌شود
    تا کاربر بتواند بر اساس سرعت اینترنتش انتخاب کند.
    """
    q = update.callback_query
    await q.answer("در حال بارگذاری کیفیت‌ها...")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
    try:
        # اولویت ۱: movie ذخیره‌شده در user_data
        movie = context.user_data.get("current_movie")
        if not movie or (season_value and season_value != context.user_data.get("current_season")):
            if season_value:
                movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
            else:
                movie = await asyncio.to_thread(get_movie_cached, movie_id)
        if ep_index < 0 or ep_index >= len(movie.episodes):
            await q.message.reply_text("قسمت نامعتبر است.")
            return
        ep = movie.episodes[ep_index]
    except Exception as e:
        log.exception("show episode qualities error")
        await q.message.reply_text("⚠️ خطا در دریافت اطلاعات قسمت.")
        return

    quality_links = getattr(ep, "quality_links", []) or []

    # اگر فقط یک کیفیت دارد (یا هیچ کیفیت‌ای ندارد)، مستقیم پخش کن
    if len(quality_links) <= 1:
        await play_episode(update, context, movie_id, ep_index, season_value=season_value)
        return

    # وگرنه کیفیت‌ها را نشان بده
    markup = kb.episode_quality_select_kb(
        movie_id, ep_index, ep, season_value=season_value
    )

    quality_count = len(quality_links)
    # سعی کن reply_markup را آپدیت کن
    try:
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        # اگر پیام عکس یا کپشن طولانی است، پیام جدید بفرست
        await q.message.reply_text(
            f"🎬 <b>{esc(movie.title)}</b>\n"
            f"▶️ {esc(ep.part)}\n"
            f"📥 {quality_count} کیفیت موجود:\n"
            f"روی کیفیت مورد نظر بزنید (بر اساس سرعت اینترنت):",
            parse_mode=ParseMode.HTML,
            reply_markup=markup
        )


# پخش قسمت با کیفیت انتخاب‌شده
async def play_episode_with_quality(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     movie_id: str, ep_index: int, q_index: int,
                                     season_value: str = "") -> None:
    """پخش قسمت با کیفیت انتخاب‌شده توسط کاربر.
    این تابع به‌جای استفاده از ep.play_url (بهترین کیفیت)، از
    ep.quality_links[q_index] استفاده می‌کند.
    """
    q = update.callback_query
    await q.answer("در حال ساخت لینک پخش…")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
    try:
        # اولویت ۱: movie ذخیره‌شده در user_data
        movie = context.user_data.get("current_movie")
        if not movie or (season_value and season_value != context.user_data.get("current_season")):
            if season_value:
                movie = await asyncio.to_thread(site.movie_season, movie_id, season_value)
            else:
                movie = await asyncio.to_thread(get_movie_cached, movie_id)
        if ep_index < 0 or ep_index >= len(movie.episodes):
            await q.message.reply_text("قسمت نامعتبر است.")
            return
        ep = movie.episodes[ep_index]
        # انتخاب کیفیت درخواستی از quality_links
        quality_links = getattr(ep, "quality_links", []) or []
        if q_index < 0 or q_index >= len(quality_links):
            # fallback به play_url اصلی
            play_url = ep.play_url
        else:
            play_url = quality_links[q_index]["url"]
        vlc_link = await asyncio.to_thread(site.resolve_play, play_url, movie_id)
    except LoginError:
        await q.message.reply_text("⚠️ ورود به سایت ممکن نشد. بعداً تلاش کنید.")
        return
    except Exception as e:
        log.exception("play with quality error")
        db.log_error("play_q", f"{movie_id}/{ep_index}/{q_index}: {e}")
        await q.message.reply_text("⚠️ خطا در ساخت لینک پخش.")
        return

    if not vlc_link:
        await q.message.reply_text("⚠️ لینک پخش در دسترس نیست. دوباره تلاش کنید.")
        return

    http_link = SiteClient.vlc_to_http(vlc_link)
    # ثبت در تاریخچه‌ی تماشا
    try:
        db.add_watch(update.effective_user.id, movie_id, movie.title, ep.label)
    except Exception:
        pass

    # دکمه‌ی تماشا — لینک پیچ‌شده با سایت پلیر Vercel
    await q.message.reply_html(
        webapp_play_message(movie, ep),
        reply_markup=kb.play_kb(http_link, title=movie.title, episode=ep.label),
        disable_web_page_preview=True)


# ---------------- علاقه‌مندی ----------------
async def toggle_fav(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     movie_id: str, add: bool) -> None:
    q = update.callback_query
    uid = update.effective_user.id
    if add:
        try:
            movie = await asyncio.to_thread(get_movie_cached, movie_id)
            title = movie.title
        except Exception:
            title = movie_id
        db.add_favorite(uid, movie_id, title)
        await q.answer("به علاقه‌مندی‌ها اضافه شد ❤️")
    else:
        db.remove_favorite(uid, movie_id)
        await q.answer("از علاقه‌مندی‌ها حذف شد 💔")
    # به‌روزرسانی کیبورد
    try:
        movie = await asyncio.to_thread(get_movie_cached, movie_id)
        # اگر سریال چند فصلی هست، کیبورد فصل‌ها را نشان بده (نه کیفیت)
        if movie.seasons:
            markup = kb.season_select_kb(
                movie.movie_id, movie.seasons, db.is_favorite(uid, movie_id),
                season_titles=getattr(movie, "season_titles", None)
            )
        else:
            markup = kb.movie_card_kb(movie, db.is_favorite(uid, movie_id))
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        pass


# ---------------- روتر Callback ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")

    if data == "noop":
        await q.answer()
        return

    # دکمه‌های شیشه‌ای منوی استارت
    if data == "menu:search":
        await q.answer()
        context.user_data["awaiting_foreign_search"] = True
        await q.message.reply_text(
            SEARCH_PROMPT + "\n\n💡 برای لغو، روی «بازگشت» بزنید.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="menu:home")
            ]]))
        return
    if data == "menu:fav":
        await q.answer()
        await cmd_favorites(update, context)
        return
    if data == "menu:hist":
        await q.answer()
        await cmd_history(update, context)
        return
    if data == "menu:recent":
        await q.answer()
        await cmd_recent(update, context)
        return
    if data == "menu:help":
        await q.answer()
        await cmd_help(update, context)
        return
    if data == "menu:admin":
        await q.answer()
        await cmd_admin(update, context)
        return
    if data == "menu:home":
        await q.answer()
        # پاک‌کردن حالت‌های انتظار جستجو
        context.user_data.pop("awaiting_foreign_search", None)
        context.user_data.pop("awaiting_iranian_search", None)
        await send_welcome(update, context)
        return
    if data == "menu:iranian_search":
        # نمایش پیام راهنما برای جستجوی ایرانی
        await q.answer()
        context.user_data["awaiting_iranian_search"] = True
        await q.message.reply_text(
            "🎬 <b>جستجوی فیلم و سریال ایرانی</b>\n\n"
            "لطفاً نام فیلم یا سریال ایرانی را بنویسید تا در سایت جستجو کنم.\n"
            "مثال: <code>مست عشق</code>\n\n"
            "💡 برای فیلم‌ها ۱۵ ثانیه صبر می‌کنم تا لینک مستقیم را بگیرم.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.iranian_search_prompt_kb())
        return
    if data == "menu:iranian":
        # نمایش پیام راهنما برای جستجوی ایرانی (همان بالا)
        await q.answer()
        context.user_data["awaiting_iranian_search"] = True
        await q.message.reply_text(
            "🎬 <b>جستجوی فیلم و سریال ایرانی</b>\n\n"
            "لطفاً نام فیلم یا سریال ایرانی را بنویسید تا در سایت جستجو کنم.\n"
            "مثال: <code>مست عشق</code>\n\n"
            "💡 برای فیلم‌ها ۱۵ ثانیه صبر می‌کنم تا لینک مستقیم را بگیرم.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.iranian_search_prompt_kb())
        return

    if data == "checkjoin":
        missing = await is_member_all_channels(u.id, context)
        if missing:
            await q.answer("هنوز عضو همه‌ی کانال‌ها نیستید.", show_alert=True)
        else:
            await q.answer("عضویت تأیید شد ✅")
            try:
                await q.message.delete()
            except BadRequest:
                pass
            # اگر کاربر از طریق دیپ‌لینک «مشاهده‌ی فیلم» آمده بود، حالا که عضو شد
            # مستقیم کارت فیلم را نشان می‌دهیم.
            pending_movie_id = context.user_data.pop("pending_movie_id", None)
            if pending_movie_id and pending_movie_id.isdigit():
                try:
                    movie = await asyncio.to_thread(get_movie_cached, pending_movie_id)
                except Exception as e:
                    log.warning("پس از تأیید عضویت، دریافت فیلم ناموفق: %s", e)
                    movie = None
                if movie:
                    is_fav = db.is_favorite(u.id, pending_movie_id)
                    caption = movie_caption(movie)
                    markup = kb.movie_card_kb(movie, is_fav)
                    sent = False
                    if movie.poster:
                        try:
                            await context.bot.send_photo(
                                q.message.chat_id, photo=movie.poster,
                                caption=caption[:1024], parse_mode=ParseMode.HTML,
                                reply_markup=markup)
                            sent = True
                        except (BadRequest, TelegramError):
                            pass
                    if not sent:
                        await context.bot.send_message(
                            q.message.chat_id, caption[:4096],
                            parse_mode=ParseMode.HTML, reply_markup=markup,
                            disable_web_page_preview=True)
                    return
            # در غیر این صورت، پیام خوش‌آمد معمول
            if os.path.exists(WELCOME_IMAGE):
                try:
                    with open(WELCOME_IMAGE, "rb") as f:
                        await context.bot.send_photo(
                            q.message.chat_id, photo=InputFile(f, filename="welcome.jpg"),
                            caption=WELCOME, parse_mode=ParseMode.HTML,
                            reply_markup=kb.start_inline_kb(is_admin=db.is_admin(u.id)))
                    return
                except Exception:
                    pass
            await context.bot.send_message(
                q.message.chat_id, WELCOME, parse_mode=ParseMode.HTML,
                reply_markup=kb.start_inline_kb(is_admin=db.is_admin(u.id)))
        return

    # دستورات ادمین
    if data.startswith("adm:"):
        await on_admin_callback(update, context, data[4:])
        return

    # دستورات نیازمند عضویت
    if not db.is_admin(u.id):
        missing = await is_member_all_channels(u.id, context)
        if missing:
            await q.answer("ابتدا در کانال‌ها عضو شوید.", show_alert=True)
            await q.message.reply_text("🔒 عضویت اجباری:", reply_markup=kb.join_kb(missing))
            return

    # انتخاب فصل
    elif data.startswith("season:"):
        parts = data.split(":")
        movie_id = parts[1]
        season_val = parts[2]
        await q.answer()
        # به‌جای نمایش دکمه‌های کیفیت، مستقیم لیست قسمت‌ها را نشان بده
        # این کار طبق درخواست کاربر: «نیازی نیست دیگ کیفیت هارو جدا بکنی»
        await show_episode_list_direct(update, context, movie_id,
                                        season_value=season_val, page=0)

    elif data.startswith("mv:"):
        await show_movie_card(update, context, data[3:], 0)
        await q.answer()
    elif data.startswith("ep:"):
        # پارس callback: ep:{movie_id}:{idx} یا ep:{movie_id}:{idx}:{season_value}
        # به‌جای پخش مستقیم، کیفیت‌های موجود را نشان بده (اگر چند کیفیت دارد)
        parts = data.split(":")
        mid = parts[1]
        idx = int(parts[2])
        sv = parts[3] if len(parts) > 3 else ""
        await show_episode_qualities(update, context, mid, idx, season_value=sv)
    elif data.startswith("epq:"):
        # پخش قسمت با کیفیت انتخاب‌شده
        # epq:{movie_id}:{ep_idx}:{q_idx} یا epq:{movie_id}:{ep_idx}:{q_idx}:{season_value}
        parts = data.split(":")
        mid = parts[1]
        ep_idx = int(parts[2])
        q_idx = int(parts[3])
        sv = parts[4] if len(parts) > 4 else ""
        await play_episode_with_quality(update, context, mid, ep_idx, q_idx,
                                          season_value=sv)
    elif data.startswith("backeps:"):
        # بازگشت از انتخاب کیفیت به لیست قسمت‌ها
        # backeps:{movie_id}:{season_value} یا backeps:{movie_id}
        parts = data.split(":")
        mid = parts[1]
        sv = parts[2] if len(parts) > 2 else ""
        await show_episode_list_direct(update, context, mid,
                                        season_value=sv, page=0)
    elif data.startswith("q:"):
        parts = data.split(":")
        sv = parts[3] if len(parts) > 3 else ""
        await select_quality(update, context, parts[1], parts[2], season_value=sv)
    elif data.startswith("qt:"):
        parts = data.split(":")
        sv = parts[4] if len(parts) > 4 else ""
        await show_episode_list(update, context, parts[1], parts[2], parts[3], season_value=sv)
    elif data.startswith("epl:"):
        parts = data.split(":")
        sv = parts[5] if len(parts) > 5 else ""
        await show_episode_list(update, context, parts[1], parts[2], parts[3],
                                 int(parts[4]), season_value=sv)
    elif data.startswith("epld:"):
        # ناوبری صفحه در لیست مستقیم قسمت‌ها (بدون فیلتر کیفیت)
        # epld:{movie_id}:{page}:{season_value}
        parts = data.split(":")
        mid = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        sv = parts[3] if len(parts) > 3 else ""
        await show_episode_list_direct(update, context, mid,
                                        season_value=sv, page=page)
    elif data.startswith("bs:"):
        # بازگشت از لیست قسمت‌ها به انتخاب فصل
        # bs:{movie_id}:{season_value}
        parts = data.split(":")
        mid = parts[1]
        await q.answer()
        try:
            movie = await asyncio.to_thread(get_movie_cached, mid)
        except Exception:
            await q.answer("خطا.", show_alert=True)
            return
        is_fav = db.is_favorite(u.id, mid)
        markup = kb.season_select_kb(
            movie.movie_id, movie.seasons, is_fav,
            season_titles=getattr(movie, "season_titles", None)
        )
        try:
            await q.edit_message_reply_markup(reply_markup=markup)
        except BadRequest:
            pass
    elif data.startswith("back_movie:"):
        # بازگشت به کارت فیلم (بدون فصل)
        mid = data.split(":")[1]
        await show_movie_card(update, context, mid, 0)
        await q.answer()
    elif data.startswith("bq:"):
        parts = data.split(":")
        sv = parts[2] if len(parts) > 2 else ""
        await back_to_quality(update, context, parts[1], season_value=sv)
    elif data.startswith("bqt:"):
        parts = data.split(":")
        sv = parts[3] if len(parts) > 3 else ""
        await back_to_type(update, context, parts[1], parts[2], season_value=sv)

    # فیلم ایرانی - انتخاب دسته‌بندی
    elif data == "ircat:movie":
        await q.answer()
        await cmd_iranian_list(update, context, ir_type="movie")
    elif data == "ircat:series":
        await q.answer()
        await cmd_iranian_list(update, context, ir_type="series")
    elif data == "irback_cat":
        await q.answer()
        await q.message.reply_text(
            "🎬 <b>فیلم و سریال ایرانی</b>\n\n"
            "یکی رو انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=kb.iranian_category_kb())
    # فیلم ایرانی - انتخاب آیتم از لیست (آرشیو قدیمی)
    elif data.startswith("ir:"):
        idx = int(data[3:])
        await cmd_iranian_show(update, context, idx)
    elif data.startswith("irse:"):
        # نتیجه جستجوی ایرانی
        idx = int(data[5:])
        await cmd_iranian_search_result(update, context, idx)
    elif data.startswith("irp:"):
        page = int(data[4:])
        await cmd_iranian_page(update, context, page)
    elif data.startswith("irplay:"):
        idx = int(data[7:])
        await cmd_iranian_play(update, context, idx)
    elif data.startswith("irseason:"):
        # انتخاب فصل سریال ایرانی
        season_val = data[9:]
        await cmd_iranian_season_episodes(update, context, season_val)
    elif data.startswith("irback:"):
        # بازگشت به نتایج جستجوی ایرانی (اگر هست)
        if context.user_data.get("ir_search_results"):
            results = context.user_data["ir_search_results"]
            query = context.user_data.get("ir_search_query", "")
            markup = kb.iranian_search_results_kb(results)
            await q.answer()
            try:
                await q.edit_message_text(
                    f"🔍 نتایج جستجو برای «<b>{esc(query)}</b>» ({len(results)} مورد):",
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup)
            except BadRequest:
                await q.message.reply_text(
                    f"🔍 نتایج جستجو برای «<b>{esc(query)}</b>» ({len(results)} مورد):",
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup)
        else:
            # fallback به صفحه ۰ آرشیو قدیمی
            page = int(data[7:])
            await cmd_iranian_page(update, context, page)
    elif data.startswith("fav:"):
        await toggle_fav(update, context, data[4:], add=True)
    elif data.startswith("unfav:"):
        await toggle_fav(update, context, data[6:], add=False)
    elif data.startswith("rs:"):
        # جستجوی مجدد از تاریخچه
        idx = int(data[3:])
        queries = context.user_data.get("recent") or db.recent_searches(u.id, 10)
        if 0 <= idx < len(queries):
            await q.answer()
            await do_search(update, context, queries[idx])
        else:
            await q.answer("این مورد دیگر موجود نیست.", show_alert=True)
    elif data == "clearwatch":
        db.clear_watch(u.id)
        await q.answer("تاریخچه پاک شد")
        try:
            await q.edit_message_text("🕒 تاریخچه‌ی تماشای شما پاک شد.")
        except BadRequest:
            pass
    elif data == "clearsearch":
        db.clear_searches(u.id)
        context.user_data.pop("recent", None)
        await q.answer("تاریخچه‌ی جستجو پاک شد")
        try:
            await q.edit_message_text("📜 تاریخچه‌ی جستجوی شما پاک شد.")
        except BadRequest:
            pass
    else:
        await q.answer()


# ---------------- فیلم/سریال ایرانی (serialirany) ----------------

async def cmd_iranian_list(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          ir_type: str = "movie") -> None:
    """نمایش لیست فیلم یا سریال ایرانی.
    اگر آرشیو قبلاً بارگذاری شده (در استارت ربات)، فوراً نمایش داده می‌شود.
    در غیر این‌صورت، یک پیام «در حال بارگذاری» نمایش می‌دهد و سپس لود می‌کند.
    """
    msg = update.effective_message
    uid = update.effective_user.id
    label = "فیلم" if ir_type == "movie" else "سریال"

    # بررسی اینکه آیا آرشیو از قبل بارگذاری شده
    cache_key = MOVIE_ARCHIVE if ir_type == "movie" else SERIES_ARCHIVE
    cached = SerialiranyClient._full_archive_cache.get(cache_key)
    is_cached = cached and (time.time() - cached["time"]) < 3600

    # بررسی اینکه آیا Selenium هم‌زمان در حال بارگذاری است
    archive_type_key = ir_type  # "movie" یا "series"
    is_loading = ir_client._is_loading_archive.get(archive_type_key, False)

    if is_cached:
        # آرشیو از قبل بارگذاری شده — فوراً نمایش بده
        items = cached["items"]
        await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
    elif is_loading:
        # Selenium در حال بارگذاری است — به‌جای صبر کردن، از requests-only استفاده کن
        # این کار از deadlock جلوگیری می‌کند
        await msg.reply_text(
            f"⏳ در حال بارگذاری کامل لیست {label}های ایرانی...\n"
            f"برای نمایش سریع‌تر، روی «⚡ نمایش سریع» بزنید."
        )
        # درخواست با requests-only (بدون Selenium) — سریع ولی فقط صفحه اول
        try:
            items = await asyncio.to_thread(ir_client._get_archive_list_requests_only,
                                            cache_key)
        except Exception as e:
            log.exception("iranian list (requests-only) error")
            await msg.reply_text(f"⚠️ خطا در دریافت لیست {label}های ایرانی.")
            return
    else:
        # آرشیو هنوز بارگذاری نشده — پیام «در حال بارگذاری» بفرست
        loading_msg = await msg.reply_text(
            f"⏳ در حال بارگذاری لیست {label}های ایرانی...\n"
            f"این عمل ممکن است چند ثانیه طول بکشد. لطفاً صبر کنید."
        )
        await context.bot.send_chat_action(msg.chat_id, ChatAction.TYPING)
        try:
            if ir_type == "movie":
                items = await asyncio.to_thread(ir_client.get_movie_list)
            else:
                items = await asyncio.to_thread(ir_client.get_series_list)
        except Exception as e:
            log.exception("iranian list error")
            await loading_msg.edit_text(f"⚠️ خطا در دریافت لیست {label}های ایرانی.")
            return
        # پاک کردن پیام «در حال بارگذاری»
        try:
            await loading_msg.delete()
        except Exception:
            pass

    if not items:
        await msg.reply_text(f"{label} ایرانی‌ای در آرشیو یافت نشد.")
        return

    # کش کن
    iranian_cache[uid] = {"type": ir_type, "items": items, "page": 0}
    markup = kb.iranian_list_kb(items, page=0, page_size=config.SEARCH_PAGE_SIZE)
    await msg.reply_text(
        f"🎬 لیست {label}های ایرانی ({len(items)} مورد):",
        reply_markup=markup)


async def cmd_iranian_page(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           page: int) -> None:
    """تغییر صفحه لیست فیلم/سریال ایرانی."""
    q = update.callback_query
    uid = update.effective_user.id
    await q.answer()
    cached = iranian_cache.get(uid)
    if not cached:
        await q.answer("لیست منقضی شده. دوباره از منو وارد شوید.", show_alert=True)
        return
    items = cached["items"]
    markup = kb.iranian_list_kb(items, page=page, page_size=config.SEARCH_PAGE_SIZE)
    try:
        await q.edit_message_reply_markup(reply_markup=markup)
    except BadRequest:
        pass


async def cmd_iranian_show(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           item_idx: int) -> None:
    """نمایش قسمت‌های یک فیلم/سریال ایرانی.
    اول بررسی می‌کنه که آیتم فیلم است یا سریال (از دیتابیس یا ساختار صفحه).
    برای فیلم‌ها: فقط یک دکمه «پخش آنلاین» نمایش می‌دهد (نه ۲۳ لینک).
    برای سریال‌ها: ساختار فصل‌بندی را نشان می‌دهد.
    """
    q = update.callback_query
    uid = update.effective_user.id
    await q.answer("در حال بارگذاری صفحه…")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)
    cached = iranian_cache.get(uid)
    if not cached:
        await q.answer("لیست منقضی شده.", show_alert=True)
        return
    items = cached["items"]
    if item_idx < 0 or item_idx >= len(items):
        await q.answer("آیتم نامعتبر.", show_alert=True)
        return
    item = items[item_idx]
    item_url = item["url"]
    item_title = item["title"]
    item_type = cached.get("type", "movie")  # "movie" یا "series"

    # اگر آیتم فیلم است، مستقیم دکمه پخش را نشان بده (بدون بررسی ساختار سریال)
    # این کار مشکل «۲۳ لینک» را حل می‌کند چون فیلم‌ها فقط یک لینک دارند
    if item_type == "movie":
        # اول چک کن آیا لینک مستقیم در دیتابیس هست
        direct_link = None
        try:
            db_movie = db.get_iranian_movie(item_url)
            if db_movie and db_movie.get("direct_link"):
                direct_link = db_movie["direct_link"]
        except Exception as e:
            log.warning("خطا در خواندن فیلم از دیتابیس: %s", e)

        if direct_link:
            # لینک مستقیم از قبل در دیتابیس هست — فوراً پخش کن
            context.user_data["ir_direct_url"] = item_url
            context.user_data["ir_title"] = item_title
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ پخش آنلاین", callback_data="irplay:0")],
                [InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="irback:0")],
            ])
            await q.message.reply_text(
                f"🎬 <b>{esc(item_title)}</b>\n\n"
                f"روی «پخش آنلاین» بزنید تا پخش شروع شود.",
                parse_mode=ParseMode.HTML,
                reply_markup=markup)
        else:
            # لینک مستقیم در دیتابیس نیست — باید استخراج شود
            context.user_data["ir_direct_url"] = item_url
            context.user_data["ir_title"] = item_title
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("▶️ پخش آنلاین", callback_data="irplay:0")],
                [InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="irback:0")],
            ])
            await q.message.reply_text(
                f"🎬 <b>{esc(item_title)}</b>\n\n"
                f"روی «پخش آنلاین» بزنید تا لینک پخش ساخته شود.\n"
                f"⏳ ممکن است ۱۵-۲۰ ثانیه طول بکشد (استخراج لینک از پلیر).",
                parse_mode=ParseMode.HTML,
                reply_markup=markup)
        return

    # برای سریال‌ها: بررسی ساختار (فصل‌بندی شده یا نه)
    try:
        structure = await asyncio.to_thread(ir_client.get_series_structure, item_url)
    except Exception as e:
        log.exception("iranian series structure error")
        structure = {"has_seasons": False, "episodes": []}

    if structure.get("has_seasons") and structure.get("seasons"):
        # سریال چند فصلی — ابتدا فصل رو انتخاب کن
        context.user_data["ir_structure"] = structure
        context.user_data["ir_title"] = item_title
        rows = []
        for s in structure["seasons"]:
            ep_count = len(s.get("episodes", []))
            label = f"📺 {s['title']} ({ep_count} قسمت)"
            rows.append([InlineKeyboardButton(
                label, callback_data=f"irseason:{s['value']}")])
        rows.append([InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="irback:0")])
        await q.message.reply_text(
            f"🎬 <b>{esc(item_title)}</b>\n\n"
            f"یکی از فصل‌ها رو انتخاب کنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(rows))
    elif structure.get("episodes"):
        # بدون فصل‌بندی — لیست قسمت‌ها رو نشون بده
        ep_links = structure["episodes"]
        context.user_data["ir_links"] = ep_links
        context.user_data["ir_title"] = item_title
        markup = kb.iranian_links_kb(ep_links, title=item_title)
        await q.message.reply_text(
            f"🎬 <b>{esc(item_title)}</b>\n"
            f"📥 {len(ep_links)} قسمت:\n"
            f"روی مورد مورد نظر بزنید:",
            parse_mode=ParseMode.HTML,
            reply_markup=markup)
    else:
        # سریال بدون قسمت پیدا شده — مستقیم لینک پخش بساز
        context.user_data["ir_direct_url"] = item_url
        context.user_data["ir_title"] = item_title
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("▶️ پخش آنلاین", callback_data="irplay:0")],
            [InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="irback:0")],
        ])
        await q.message.reply_text(
            f"🎬 <b>{esc(item_title)}</b>\n\n"
            f"روی «پخش آنلاین» بزنید تا لینک پخش ساخته شود.",
            parse_mode=ParseMode.HTML,
            reply_markup=markup)


async def cmd_iranian_season_episodes(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                   season_val: str) -> None:
    """نمایش قسمت‌های یک فصل خاص سریال ایرانی."""
    q = update.callback_query
    await q.answer()
    structure = context.user_data.get("ir_structure")
    title = context.user_data.get("ir_title", "سریال ایرانی")
    if not structure or not structure.get("seasons"):
        await q.answer("ساختار منقضی شده.", show_alert=True)
        return
    # پیدا کردن فصل
    eps = []
    season_title = season_val
    for s in structure["seasons"]:
        if s["value"] == season_val:
            eps = s.get("episodes", [])
            season_title = s["title"]
            break
    if not eps:
        await q.message.reply_text(
            f"📺 {esc(season_title)}: قسمتی یافت نشد.")
        return
    context.user_data["ir_links"] = eps
    context.user_data["ir_title"] = title
    markup = kb.iranian_links_kb(eps, title=f"{title} - {season_title}")
    await q.message.reply_text(
        f"🎬 <b>{esc(title)}</b> — {esc(season_title)}\n"
        f"📥 {len(eps)} قسمت:\n"
        f"روی مورد مورد نظر بزنید:",
        parse_mode=ParseMode.HTML,
        reply_markup=markup)


async def cmd_iranian_play(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           link_idx: int) -> None:
    """پخش فیلم/قسمت ایرانی.
    اگه لینک مستقیم (.mp4) باشه → Vercel player
    اگه لینک صفحه باشه → Selenium → استخراج لینک واقعی → Vercel player
    برای فیلم‌ها: تایمر ۱۵ ثانیه نمایش داده می‌شه (در حال انتظار برای تبلیغات).
    """
    q = update.callback_query
    await q.answer("در حال ساخت لینک پخش…")
    await context.bot.send_chat_action(q.message.chat_id, ChatAction.TYPING)

    title = context.user_data.get("ir_title", "فیلم ایرانی")
    links = context.user_data.get("ir_links", [])
    direct_url = context.user_data.get("ir_direct_url", "")

    # تعیین URL هدف
    target_url = ""
    link_label = title

    if links and 0 <= link_idx < len(links):
        # حالت ۱: لیست لینک‌ها موجود هست (سریال)
        link_info = links[link_idx]
        link_label = link_info.get("title", title)
        target_url = link_info["url"]
    elif direct_url:
        # حالت ۲: فیلم تک‌قسمتی
        target_url = direct_url
        link_label = title
    else:
        await q.answer("لینک نامعتبر.", show_alert=True)
        return

    # اگه لینک مستقیم .mp4 هست (یا urliran) → مستقیم بفرست
    if ".mp4" in target_url or "urliran" in target_url:
        video_url = target_url
    else:
        # لینک صفحه‌ست — اول تلاش کن با decode Base64 لینک مستقیم بگیری (سریع)
        await q.message.reply_text("⏳ در حال استخراج لینک پخش…")
        try:
            video_url = await asyncio.to_thread(ir_client.get_video_link, target_url)
        except Exception as e:
            log.exception("iranian video link error")
            video_url = None

        if not video_url:
            # اگر decode Base64 نشد، با Selenium و تایمر ۱۵ ثانیه
            timer_msg = await q.message.reply_text(
                "⏳ در حال استخراج لینک پخش…\n"
                "⏱️ ۱ ثانیه")

            # اجرای تایمر ۱۵ ثانیه و استخراج لینک به‌صورت هم‌زمان
            import asyncio as _asyncio

            async def _update_timer():
                """به‌روزرسانی پیام تایمر هر ثانیه."""
                for i in range(2, 16):
                    await _asyncio.sleep(1)
                    try:
                        await timer_msg.edit_text(
                            f"⏳ در حال استخراج لینک پخش…\n"
                            f"⏱️ {i} ثانیه")
                    except Exception:
                        pass  # اگر edit ناموفق بود، ادامه بده

            async def _extract_with_selenium():
                """استخراج لینک با Selenium (۱۵ ثانیه صبر + Skip Ad)."""
                return await _asyncio.to_thread(
                    ir_client._get_video_link_selenium, target_url, 25)

            # اجرای هر دو به‌صورت هم‌زمان
            timer_task = _asyncio.create_task(_update_timer())
            try:
                video_url = await _extract_with_selenium()
            except Exception as e:
                log.exception("iranian selenium error")
                video_url = None
            finally:
                timer_task.cancel()
                try:
                    await timer_msg.delete()
                except Exception:
                    pass

            if not video_url:
                await q.message.reply_text(
                    "⚠️ لینک ویدیو استخراج نشد. ممکنه صفحه مشکل داشته باشه."
                    "\nلطفاً دوباره تلاش کنید.")
                return

    # لینک رو با سایت پلیر Vercel بپیچ
    # برای لینک‌های ایرانی، از urliran= استفاده می‌شود (بدون title/episode)
    # چون title/episode مشکل ایجاد می‌کنند در نمایش پلیر
    markup = kb.play_kb(video_url, is_iranian=True)
    await q.message.reply_html(
        f"🎬 <b>{esc(title)}</b>\n"
        f"▶️ {esc(link_label)}\n\n"
        f"روی دکمه‌ی «تماشای آنلاین» بزنید تا پخش شروع شود.",
        reply_markup=markup,
        disable_web_page_preview=True)


# ---------------- جستجوی درون‌خطی (inline) ----------------
async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.inline_query.query or "").strip()

    # بدون عبارت جستجو → راهنما نشان بده
    if not query:
        try:
            bot_username = (context.bot.username or "").lstrip("@")
        except Exception:
            bot_username = ""
        help_text = (
            "🎬 <b>راهنمای جستجوی ربات SilentMovie</b>\n\n"
            "🔍 <b>جستجوی فیلم خارجی:</b>\n"
            f"<code>@{bot_username} kh نام فیلم</code>\n"
            "مثال: <code>@{bot_username} kh inception</code>\n\n"
            "🎬 <b>جستجوی فیلم/سریال ایرانی:</b>\n"
            f"<code>@{bot_username} ir نام فیلم</code>\n"
            "مثال: <code>@{bot_username} ir مست عشق</code>\n\n"
            "💡 بدون پیشوند <code>kh</code> هم فیلم خارجی جستجو می‌شود."
        )
        items = [
            InlineQueryResultArticle(
                id="help",
                title="📖 راهنمای جستجو",
                description="چگونه فیلم ایرانی و خارجی جستجو کنید",
                input_message_content=InputTextMessageContent(
                    message_text=help_text,
                    parse_mode=ParseMode.HTML,
                ),
            )
        ]
        await update.inline_query.answer(items, cache_time=300)
        return

    if len(query) < 2:
        await update.inline_query.answer([], cache_time=5)
        return

    # تشخیص نوع جستجو بر اساس پیشوند
    if query.lower().startswith("ir "):
        # جستجوی فیلم/سریال ایرانی از serialirany.com
        search_query = query[3:].strip()
        await _inline_iranian_search(update, context, search_query)
    elif query.lower().startswith("kh "):
        # جستجوی فیلم خارجی از tdmmo.xyz
        search_query = query[3:].strip()
        await _inline_foreign_search(update, context, search_query)
    else:
        # بدون پیشوند → جستجوی خارجی (پیش‌فرض)
        await _inline_foreign_search(update, context, query)


async def _inline_iranian_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                query: str) -> None:
    """جستجوی inline در آرشیو فیلم و سریال ایرانی (serialirany.com).
    کاربر می‌نویسه: @bot ir <نام فیلم>
    """
    if len(query) < 2:
        await update.inline_query.answer([], cache_time=5)
        return

    try:
        results = await asyncio.to_thread(ir_client.search, query)
    except Exception as e:
        log.exception("inline iranian search error")
        await update.inline_query.answer([], cache_time=5)
        return

    if not results:
        await update.inline_query.answer([], cache_time=5)
        return

    # یوزرنیم ربات برای دیپ‌لینک
    try:
        bot_username = (context.bot.username or "").lstrip("@")
    except Exception:
        bot_username = ""

    items = []
    for r in results[:20]:
        title = r.get("title", "")
        thumb = r.get("thumb", "")
        url = r.get("url", "")

        # ساخت کلید کش و دیپ‌لینک
        cache_key = SerialiranyClient.make_cache_key(url)
        ir_inline_cache[cache_key] = r

        if bot_username and thumb:
            deep_url = f"https://t.me/{bot_username}?start=ir_{cache_key}"
            items.append(InlineQueryResultPhoto(
                id=cache_key,
                photo_url=thumb,
                thumbnail_url=thumb,
                title=title,
                description="برای پخش بزنید",
                caption=f"🎬 <b>{esc(title)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ پخش آنلاین", url=deep_url)
                ]]),
            ))
        elif bot_username:
            deep_url = f"https://t.me/{bot_username}?start=ir_{cache_key}"
            items.append(InlineQueryResultArticle(
                id=cache_key,
                title=title,
                description="برای پخش بزنید",
                input_message_content=InputTextMessageContent(
                    message_text=f"🎬 <b>{esc(title)}</b>",
                    parse_mode=ParseMode.HTML),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ پخش آنلاین", url=deep_url)
                ]]),
            ))
        else:
            # بدون یوزرنیم — fallback به callback
            items.append(InlineQueryResultArticle(
                id=cache_key,
                title=title,
                description="برای پخش بزنید",
                input_message_content=InputTextMessageContent(
                    message_text=f"🎬 <b>{esc(title)}</b>",
                    parse_mode=ParseMode.HTML),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("▶️ پخش آنلاین", callback_data=f"irplay:0")
                ]]),
            ))

    await update.inline_query.answer(items, cache_time=30)


async def _inline_foreign_search(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               query: str) -> None:
    """جستجوی inline در سایت tdmmo.xyz (فیلم خارجی).
    کاربر می‌نویسه: @bot kh <نام فیلم>  یا  @bot <نام فیلم>
    """
    try:
        results = await asyncio.to_thread(site.search, query, 1)
    except Exception as e:
        db.log_error("inline_search", f"{query}: {e}")
        await update.inline_query.answer([], cache_time=5)
        return

    try:
        bot_username = (context.bot.username or "").lstrip("@")
    except Exception:
        bot_username = ""

    items = []
    for r in results[:20]:
        year = f" ({r.year})" if r.year else ""
        imdb = f" ⭐{r.imdb}" if r.imdb else ""
        title_text = f"{r.title}{year}{imdb}"

        if bot_username:
            view_button = InlineKeyboardButton(
                "🎬 مشاهده قسمت‌ها و اطلاعات",
                url=kb.movie_deeplink_url(bot_username, r.movie_id))
        else:
            view_button = InlineKeyboardButton(
                "🎬 مشاهده قسمت‌ها و اطلاعات",
                callback_data=f"mv:{r.movie_id}")

        if r.poster:
            items.append(InlineQueryResultPhoto(
                id=r.movie_id,
                photo_url=r.poster,
                thumbnail_url=r.poster,
                title=title_text,
                description="برای دیدن اطلاعات و لینک پخش بزنید",
                caption=f"🎬 <b>{esc(r.title)}</b>{year}{imdb}",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[view_button]]),
            ))
        else:
            items.append(InlineQueryResultArticle(
                id=r.movie_id,
                title=title_text,
                description="برای دیدن اطلاعات و لینک پخش بزنید",
                input_message_content=InputTextMessageContent(
                    message_text=f"🎬 <b>{esc(r.title)}</b>{year}{imdb}\n"
                                 f"<i>برای دیدن قسمت‌ها روی دکمه‌ی زیر بزنید.</i>",
                    parse_mode=ParseMode.HTML),
                reply_markup=InlineKeyboardMarkup([[view_button]]),
            ))
    await update.inline_query.answer(items, cache_time=10)


async def cmd_movie_deeplink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """پیام /movie_<id> که از نتیجه‌ی inline می‌آید."""
    text = (update.effective_message.text or "").strip()
    if not text.startswith("/movie_"):
        return
    movie_id = text[len("/movie_"):].strip()
    if not movie_id.isdigit():
        return
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    if not await require_membership(update, context):
        return
    try:
        movie = await asyncio.to_thread(get_movie_cached, movie_id)
    except Exception as e:
        db.log_error("deeplink", f"{movie_id}: {e}")
        await update.effective_message.reply_text("⚠️ خطا در دریافت فیلم.")
        return
    is_fav = db.is_favorite(u.id, movie_id)
    caption = movie_caption(movie)
    markup = kb.movie_card_kb(movie, is_fav)

    sent = False
    if movie.poster:
        try:
            await update.effective_message.reply_photo(
                photo=movie.poster,
                caption=caption[:1024], parse_mode=ParseMode.HTML, reply_markup=markup)
            sent = True
        except (BadRequest, TelegramError):
            pass
    if not sent and movie.poster:
        poster_bytes = await download_bytes(movie.poster)
        if poster_bytes:
            try:
                ext = ".jpg"
                if poster_bytes[:4] == b"RIFF":
                    ext = ".webp"
                elif poster_bytes[:8] == b"\x89PNG\r\n\x1a\n":
                    ext = ".png"
                await update.effective_message.reply_photo(
                    photo=InputFile(io.BytesIO(poster_bytes), filename=f"{movie_id}{ext}"),
                    caption=caption[:1024], parse_mode=ParseMode.HTML, reply_markup=markup)
                sent = True
            except (BadRequest, TelegramError):
                pass
    if not sent:
        await update.effective_message.reply_html(caption[:4096], reply_markup=markup)


# ================= پنل مدیریت =================
# (در ماژول admin_panel.py پیاده‌سازی و اینجا وارد می‌شود)
from admin_panel import (cmd_admin, on_admin_callback, handle_admin_input,
                         handle_admin_document, job_send_db_backup)


# ---------------- لغو عملیات ادمین ----------------
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """لغو هر حالت در انتظار ادمین (مثلاً افزودن کانال یا ادمین)."""
    u = update.effective_user
    if u.id in pending_admin:
        pending_admin.pop(u.id, None)
        await update.effective_message.reply_text("✅ عملیات لغو شد.",
                                                  reply_markup=kb.admin_panel_kb()
                                                  if db.is_admin(u.id) else None)
    else:
        await update.effective_message.reply_text("هیچ عملیات فعالی برای لغو کردن وجود ندارد.")


# ---------------- هندلر عکس ----------------
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """عکس‌های ارسال‌شده توسط کاربر را نادیده می‌گیرد.
    ربات فقط با دکمه‌های شیشه‌ای کار می‌کند.
    """
    # فقط کاربر را ثبت می‌کنیم، بدون هیچ پیامی
    u = update.effective_user
    db.upsert_user(u.id, u.username or "", u.first_name or "")
    # هیچ پاسخی ارسال نمی‌کنیم — ربات فقط با دکمه‌ها کار می‌کند


# ---------------- خطاها ----------------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Exception:", exc_info=context.error)
    try:
        db.log_error("handler", repr(context.error))
    except Exception:
        pass


# ---------------- راه‌اندازی ----------------

async def preload_iranian_archives() -> None:
    """پیش‌بارگذاری آرشیو فیلم و سریال ایرانی در پس‌زمینه.
    این کار باعث می‌شود وقتی کاربر روی «فیلم ایرانی» یا «سریال ایرانی» می‌زند،
    فهرست فوراً نمایش داده شود و ربات هنگ نکند.
    """
    log.info("🚀 شروع پیش‌بارگذاری آرشیو فیلم و سریال ایرانی...")
    try:
        # بارگذاری همزمان فیلم و سریال
        movies_task = asyncio.to_thread(ir_client.get_movie_list)
        series_task = asyncio.to_thread(ir_client.get_series_list)
        movies, series = await asyncio.gather(movies_task, series_task, return_exceptions=True)

        # بررسی خطاها
        if isinstance(movies, Exception):
            log.error("خطا در پیش‌بارگذاری آرشیو فیلم‌های ایرانی: %s", movies)
        else:
            log.info("✅ پیش‌بارگذاری فیلم‌های ایرانی کامل شد: %d مورد", len(movies))

        if isinstance(series, Exception):
            log.error("خطا در پیش‌بارگذاری آرشیو سریال‌های ایرانی: %s", series)
        else:
            log.info("✅ پیش‌بارگذاری سریال‌های ایرانی کامل شد: %d مورد", len(series))

        # پر کردن کش جستجو (inline search)
        try:
            await asyncio.to_thread(ir_client._get_all_cached_items)
            log.info("✅ کش جستجوی inline هم پر شد")
        except Exception as e:
            log.warning("خطا در پر کردن کش جستجو: %s", e)

    except Exception as e:
        log.error("خطای کلی در پیش‌بارگذاری آرشیو ایرانی: %s", e)


async def post_init(app) -> None:
    """هک پس از ساخت Application.
    پیش‌بارگذاری غیرفعال است چون حالا از جستجوی مستقیم استفاده می‌کنیم.
    """
    log.info("ℹ️ جستجوی مستقیم ایرانی فعال است — نیازی به پیش‌بارگذاری نیست.")


def build_application() -> Application:
    global db, site, ir_client
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    db = Database(config.DB_PATH)
    for aid in config.ADMIN_IDS:
        db.add_admin(aid)
    site = SiteClient(config.SITE_MOBILE, config.SITE_PASSWORD, config.SESSION_PATH)
    ir_client = SerialiranyClient()
    # تزریق دیتابیس به SerialiranyClient برای ذخیره/بازیابی آرشیو
    SerialiranyClient.set_database(db)
    log.info("دیتابیس به SerialiranyClient تزریق شد (نوع: %s)", db.db_type)

    # وابستگی‌ها را به admin_panel تزریق می‌کنیم
    import admin_panel
    admin_panel.init(db, site, pending_admin)

    builder = Application.builder().token(config.BOT_TOKEN)
    # پروکسی اختیاری (برای اجرا روی سیستمی که تلگرام مسدود است).
    # روی VPS خارج از ایران این خالی است و نادیده گرفته می‌شود.
    if config.TELEGRAM_PROXY:
        builder = builder.proxy(config.TELEGRAM_PROXY).get_updates_proxy(config.TELEGRAM_PROXY)
        log.info("استفاده از پروکسی برای تلگرام: %s", config.TELEGRAM_PROXY)
    # ثبت post_init برای پیش‌بارگذاری آرشیو ایرانی هنگام استارت
    builder = builder.post_init(post_init)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(filters.Regex(r"^/movie_\d+"), cmd_movie_deeplink))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    # هندلر عکس باید قبل از هندلر document باشد تا عکس‌ها به این هندلر برسند.
    # توجه: filters.PHOTO فقط پیام‌های عکس را می‌گیرد، نه فایل‌های آپلود‌شده را.
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_admin_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text_search))
    app.add_error_handler(on_error)

    # زمان‌بندی بکاپ دیتابیس
    interval = max(0.1, config.BACKUP_INTERVAL_HOURS) * 3600
    app.job_queue.run_repeating(job_send_db_backup, interval=interval,
                                first=interval, name="db_backup")
    log.info("بکاپ دیتابیس هر %.1f ساعت ارسال می‌شود", config.BACKUP_INTERVAL_HOURS)
    return app


def main() -> None:
    if config.token_is_placeholder():
        raise SystemExit("❌ BOT_TOKEN تنظیم نشده است. فایل .env را ویرایش کنید.")

    app = build_application()
    try:
        ok = site.ensure_login()
        log.info("وضعیت لاگین اولیه به سایت: %s", "موفق" if ok else "ناموفق")
    except Exception as e:
        log.warning("لاگین اولیه ناموفق: %s", e)

    # اگر روی Render Web Service هستیم، Flask را در thread جداگانه شروع کن
    # تا health-check پاسخ دهد (ربات باید در main thread باشه چون signal handler نیاز داره)
    port = os.environ.get("PORT")
    if port:
        import threading
        import webapp
        flask_thread = threading.Thread(
            target=webapp.flask_app.run,
            kwargs={"host": "0.0.0.0", "port": int(port)},
            daemon=True, name="flask-health")
        flask_thread.start()
        log.info("حالت Web Service — Flask health-check در thread جداگانه روی پورت %s", port)

    log.info("🤖 ربات در حال اجراست…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
