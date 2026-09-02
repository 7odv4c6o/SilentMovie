# -*- coding: utf-8 -*-
"""
keyboards.py
------------
سازنده‌ی کیبوردهای شیشه‌ای (inline) ربات.
"""
from __future__ import annotations

import math
from typing import List

from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo)

from site_client import Movie, SearchResult
from categorize import (categorize_episodes, categorize_with_indices, QUALITY_ORDER,
                      QUALITY_LABELS, TYPE_LABELS, get_available_qualities,
                      get_available_types, count_quality, count_type)


# ---------------- منوی اصلی (فقط شیشه‌ای) ----------------


def start_inline_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    """دکمه‌های شیشه‌ای برای پیام استارت/خوش‌آمد."""
    rows = [
        [InlineKeyboardButton("🔍 جستجوی فیلم خارجی", callback_data="menu:search"),
         InlineKeyboardButton("🎬 جستجوی فیلم/سریال ایرانی", callback_data="menu:iranian_search")],
        [InlineKeyboardButton("❤️ علاقه‌مندی‌ها", callback_data="menu:fav"),
         InlineKeyboardButton("🕒 تماشا شده‌ها", callback_data="menu:hist")],
        [InlineKeyboardButton("📜 جستجوهای اخیر", callback_data="menu:recent"),
         InlineKeyboardButton("📖 راهنما", callback_data="menu:help")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🛠 پنل مدیریت", callback_data="menu:admin")])
    return InlineKeyboardMarkup(rows)


def iranian_search_prompt_kb() -> InlineKeyboardMarkup:
    """کیبورد راهنمای جستجوی ایرانی — فقط دکمه بازگشت."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="menu:home")]
    ])


def iranian_search_results_kb(results: list) -> InlineKeyboardMarkup:
    """کیبورد نتایج جستجوی ایرانی.
    هر نتیجه یک دکمه است که با کلیک روی آن، اطلاعات فیلم/سریال نمایش داده می‌شود.
    """
    rows = []
    for i, r in enumerate(results):
        title = r.get("title", "")
        if len(title) > 50:
            title = title[:50] + "..."
        rows.append([InlineKeyboardButton(
            f"🎬 {title}",
            callback_data=f"irse:{i}")])
    rows.append([InlineKeyboardButton("⬅️ بازگشت به منوی اصلی", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def search_results_kb(results: List[SearchResult]) -> InlineKeyboardMarkup:
    """لیست نتایج جستجو در چت خصوصی — هر فیلم یک دکمه."""
    rows = []
    for r in results:
        year = f" ({r.year})" if r.year else ""
        imdb = f" ⭐{r.imdb}" if r.imdb else ""
        rows.append([InlineKeyboardButton(f"🎬 {r.title}{year}{imdb}",
                                          callback_data=f"mv:{r.movie_id}")])
    return InlineKeyboardMarkup(rows)


# ---------------- فصل‌بندی ----------------

def season_select_kb(movie_id: str, seasons: List[str],
                     is_fav: bool,
                     season_titles: dict = None) -> InlineKeyboardMarkup:
    """کیبورد مرحله‌ی ۰: انتخاب فصل (اگر سریال چند فصلی باشد).
    اگر season_titles داده شود (نگاشت value → title)، از عنوان فصل استفاده می‌کند
    (مثلا «فصل 1 دوبله»). در غیر این‌صورت از مقدار عددی استفاده می‌کند.
    """
    rows: List[List[InlineKeyboardButton]] = []
    season_titles = season_titles or {}
    for s_val in seasons:
        title = season_titles.get(s_val) or f"فصل {s_val}"
        rows.append([InlineKeyboardButton(
            f"📺 {title}",
            callback_data=f"season:{movie_id}:{s_val}")])

    if is_fav:
        rows.append([InlineKeyboardButton("💔 حذف از علاقه‌مندی‌ها",
                                          callback_data=f"unfav:{movie_id}")])
    else:
        rows.append([InlineKeyboardButton("❤️ افزودن به علاقه‌مندی‌ها",
                                          callback_data=f"fav:{movie_id}")])

    return InlineKeyboardMarkup(rows)


def movie_card_kb(movie: Movie, is_fav: bool, season_value: str = "") -> InlineKeyboardMarkup:
    """کیبورد مرحله‌ی ۱: انتخاب کیفیت."""
    rows: List[List[InlineKeyboardButton]] = []
    cats = categorize_episodes(movie.episodes)
    quals = get_available_qualities(cats)

    if not quals and "other" not in cats:
        rows.append([InlineKeyboardButton("\u26a0\ufe0f \u0644\u06cc\u0646\u06a9\u06cc \u0645\u0648\u062c\u0648\u062f \u0646\u06cc\u0633\u062a", callback_data="noop")])
    else:
        for q in quals:
            cnt = count_quality(cats, q)
            label = QUALITY_LABELS.get(q, q)
            rows.append([InlineKeyboardButton(
                f"\U0001f4cf {label} ({cnt} \u0644\u06cc\u0646\u06a9)",
                callback_data=f"q:{movie.movie_id}:{q}:{season_value}")])
        if "other" in cats:
            cnt = count_quality(cats, "other")
            rows.append([InlineKeyboardButton(
                f"\U0001f4cf \u0633\u0627\u06cc\u0631 ({cnt} \u0644\u06cc\u0646\u06a9)",
                callback_data=f"q:{movie.movie_id}:other:{season_value}")])

    if is_fav:
        rows.append([InlineKeyboardButton("💔 \u062d\u0630\u0641 \u0627\u0632 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"unfav:{movie.movie_id}")])
    else:
        rows.append([InlineKeyboardButton("❤️ \u0627\u0641\u0632\u0648\u062f\u0646 \u0628\u0647 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"fav:{movie.movie_id}")])

    return InlineKeyboardMarkup(rows)


def type_select_kb(movie_id: str, quality: str, groups: dict,
                    is_fav: bool, season_value: str = "") -> InlineKeyboardMarkup:
    """کیبورد مرحله‌ی ۲: انتخاب نوع (دوبله/زیرنویس)."""
    rows = []
    for t_key, t_label in TYPE_LABELS:
        cnt = count_type(groups, t_key)
        if cnt:
            rows.append([InlineKeyboardButton(
                f"{t_label} ({cnt} لینک)",
                callback_data=f"qt:{movie_id}:{quality}:{t_key}:{season_value}")])

    if not rows:
        rows.append([InlineKeyboardButton("\u26a0\ufe0f \u0644\u06cc\u0646\u06a9\u06cc \u06cc\u0627\u0641\u062a \u0646\u0634\u062f", callback_data="noop")])

    back_cb = f"bq:{movie_id}:{season_value}" if season_value else f"bq:{movie_id}"
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=back_cb)])

    if is_fav:
        rows.append([InlineKeyboardButton("💔 \u062d\u0630\u0641 \u0627\u0632 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"unfav:{movie_id}")])
    else:
        rows.append([InlineKeyboardButton("❤️ \u0627\u0641\u0632\u0648\u062f\u0646 \u0628\u0647 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"fav:{movie_id}")])

    return InlineKeyboardMarkup(rows)


def episode_list_kb(movie_id: str, quality: str, ep_type: str,
                      indexed_eps: list, page: int, page_size: int,
                      is_fav: bool, season_value: str = "") -> InlineKeyboardMarkup:
    """کیبورد مرحله‌ی ۳: لیست قسمت‌ها."""
    rows = []
    total = len(indexed_eps)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = indexed_eps[start:start + page_size]

    for orig_idx, ep in chunk:
        short = ep.label[:52]
        if len(ep.label) > 52:
            short += "..."
        rows.append([InlineKeyboardButton(
            f"\u25b6\ufe0f {short}",
            callback_data=f"ep:{movie_id}:{orig_idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ \u0642\u0628\u0644\u06cc",
                     callback_data=f"epl:{movie_id}:{quality}:{ep_type}:{page-1}:{season_value}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"\u0635\u0641\u062d\u0647 {page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("\u0628\u0639\u062f\u06cc \u27a1\ufe0f",
                     callback_data=f"epl:{movie_id}:{quality}:{ep_type}:{page+1}:{season_value}"))
    if nav:
        rows.append(nav)

    back_cb = f"bqt:{movie_id}:{quality}:{season_value}" if season_value else f"bqt:{movie_id}:{quality}"
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=back_cb)])

    if is_fav:
        rows.append([InlineKeyboardButton("💔 \u062d\u0630\u0641 \u0627\u0632 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"unfav:{movie_id}")])
    else:
        rows.append([InlineKeyboardButton("❤️ \u0627\u0641\u0632\u0648\u062f\u0646 \u0628\u0647 \u0639\u0644\u0627\u0642\u0647\u200c\u0645\u0646\u062f\u06cc\u200c\u0647\u0627",
                                          callback_data=f"fav:{movie_id}")])

    return InlineKeyboardMarkup(rows)


def episode_list_direct_kb(movie_id: str, episodes: list, page: int,
                            page_size: int, is_fav: bool,
                            season_value: str = "") -> InlineKeyboardMarkup:
    """کیبورد لیست قسمت‌ها بدون فیلتر کیفیت/نوع.
    هر قسمت فقط یک بار نمایش داده می‌شود، با همه‌ی کیفیت‌هایش در quality_links.
    وقتی کاربر روی یک قسمت کلیک می‌کند، یک کیبورد با کیفیت‌های مختلف نمایش داده می‌شود
    تا کاربر بتواند بر اساس سرعت اینترنتش انتخاب کند (480/720/1080).
    """
    rows: List[List[InlineKeyboardButton]] = []
    total = len(episodes)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = episodes[start:start + page_size]

    for idx, ep in enumerate(chunk):
        global_idx = start + idx
        # برچسب قسمت: «قسمت 1» یا «قسمت 1 | 720p» (بهترین کیفیت)
        # اگر چند کیفیت دارد، تعداد را نشان بده
        quality_links = getattr(ep, "quality_links", []) or []
        label = ep.part or f"قسمت {global_idx + 1}"
        if ep.quality:
            label += f" | {ep.quality}p"
        if len(quality_links) > 1:
            label += f" ({len(quality_links)} کیفیت)"
        short = label[:60]
        if len(label) > 60:
            short = label[:57] + "..."
        # callback شامل season_value برای بازیابی فصل در play_episode
        cb = f"ep:{movie_id}:{global_idx}:{season_value}" if season_value else f"ep:{movie_id}:{global_idx}"
        rows.append([InlineKeyboardButton(f"▶️ {short}", callback_data=cb)])

    # دکمه‌های ناوبری صفحه
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی",
                     callback_data=f"epld:{movie_id}:{page-1}:{season_value}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"صفحه {page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ➡️",
                     callback_data=f"epld:{movie_id}:{page+1}:{season_value}"))
    if nav:
        rows.append(nav)

    # دکمه بازگشت
    if season_value:
        back_cb = f"bs:{movie_id}:{season_value}"
    else:
        back_cb = f"back_movie:{movie_id}"
    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data=back_cb)])

    if is_fav:
        rows.append([InlineKeyboardButton("💔 حذف از علاقه‌مندی‌ها",
                                          callback_data=f"unfav:{movie_id}")])
    else:
        rows.append([InlineKeyboardButton("❤️ افزودن به علاقه‌مندی‌ها",
                                          callback_data=f"fav:{movie_id}")])

    return InlineKeyboardMarkup(rows)


def episode_quality_select_kb(movie_id: str, ep_idx: int, episode,
                                season_value: str = "") -> InlineKeyboardMarkup:
    """کیبورد انتخاب کیفیت برای یک قسمت.
    هر کیفیت یک دکمه است (480/720/1080) تا کاربر بتواند بر اساس
    سرعت اینترنتش انتخاب کند. اگر فقط یک کیفیت موجود باشد، این کیبورد
    نمایش داده نمی‌شود (به‌جای آن مستقیم پخش می‌شود).
    """
    rows: List[List[InlineKeyboardButton]] = []
    quality_links = getattr(episode, "quality_links", []) or []

    # مرتب‌سازی کیفیت‌ها از بالا به پایین (1080 → 720 → 480)
    def quality_sort_key(ql):
        try:
            q = ql.get("quality", "0") or "0"
            return -int(q)
        except (ValueError, TypeError):
            return 0
    sorted_qs = sorted(quality_links, key=quality_sort_key)

    for q_idx, q_link in enumerate(sorted_qs):
        quality = q_link.get("quality", "")
        size = q_link.get("size", "")
        # برچسب: «🎬 کیفیت 1080p | 1 گیگابایت»
        label = "🎬 کیفیت"
        if quality:
            label += f" {quality}p"
        else:
            label += " نامشخص"
        if size:
            label += f" | {size}"
        cb = f"epq:{movie_id}:{ep_idx}:{q_idx}"
        if season_value:
            cb += f":{season_value}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])

    # دکمه بازگشت به لیست قسمت‌ها
    back_cb = f"backeps:{movie_id}:{season_value}" if season_value else f"backeps:{movie_id}"
    rows.append([InlineKeyboardButton("⬅️ بازگشت به قسمت‌ها", callback_data=back_cb)])

    if not rows[:-1]:  # اگر هیچ کیفیت‌ای پیدا نشد
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("⚠️ کیفیتی یافت نشد", callback_data="noop")],
            [InlineKeyboardButton("⬅️ بازگشت به قسمت‌ها", callback_data=back_cb)]
        ])

    return InlineKeyboardMarkup(rows)


def webapp_play_kb(webapp_url: str) -> InlineKeyboardMarkup:
    """کیبورد پخش WebApp: دکمه‌ی تماشای آنلاین که صفحه‌ی پلیر را باز می‌کند."""
    rows = [[InlineKeyboardButton(
        "▶️ تماشای آنلاین",
        web_app=WebAppInfo(url=webapp_url)
    )]]
    return InlineKeyboardMarkup(rows)


def play_kb(http_link: str, title: str = "", episode: str = "",
            is_iranian: bool = False) -> InlineKeyboardMarkup:
    """دکمه‌ی تماشای آنلاین — لینک رو پشت سایت پلیر Vercel می‌پیچه.

    برای لینک‌های ایرانی (is_iranian=True یا دامنه‌های ایرانی):
      • از پارامتر urliran= استفاده می‌شود (نه url=)
      • title و episode ارسال نمی‌شوند (چون مشکل ایجاد می‌کنند)

    خروجی مثال (ایرانی):
      https://silentmoviebot.vercel.app/?urliran=<encoded>
    خروجی مثال (خارجی):
      https://silentmoviebot.vercel.app/?url=<encoded>&t=<title>&e=<ep>
    """
    import os
    import urllib.parse
    base = os.environ.get("PLAYER_URL", "https://silentmoviebot.vercel.app").rstrip("/")

    # تشخیص خودکار لینک‌های ایرانی
    iranian_domains = [
        "cdn.serialirany.com",
        "d1.flnd.buzz",
        "flnd.buzz",
        "urliran.com",
        "urliran.net",
        "serialirany.com",
    ]
    if not is_iranian:
        for domain in iranian_domains:
            if domain in http_link:
                is_iranian = True
                break

    if is_iranian:
        # لینک ایرانی: فقط urliran= بدون title/episode
        # چون title/episode مشکل ایجاد می‌کنند در نمایش پلیر
        params = {"urliran": http_link}
    else:
        # لینک خارجی: url= + title + episode
        params = {"url": http_link}
        if title:
            params["t"] = title
        if episode:
            params["e"] = episode

    qs = urllib.parse.urlencode(params, safe="/:@!$&'()*+,;=")
    player_url = f"{base}/?{qs}"
    rows = [[InlineKeyboardButton("▶️ تماشای آنلاین", url=player_url)]]
    return InlineKeyboardMarkup(rows)


# ---------------- کیبوردهای فیلم/سریال ایرانی (serialirany) ----------------

def iranian_category_kb() -> InlineKeyboardMarkup:
    """زیرمنوی انتخاب فیلم ایرانی یا سریال ایرانی."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎬 فیلم ایرانی", callback_data="ircat:movie")],
        [InlineKeyboardButton("📺 سریال ایرانی", callback_data="ircat:series")],
        [InlineKeyboardButton("⬅️ بازگشت", callback_data="menu:home")],
    ])


def iranian_list_kb(items: list, page: int = 0, page_size: int = 8) -> InlineKeyboardMarkup:
    """لیست فیلم‌ها یا سریال‌های ایرانی از serialirany.com."""
    rows = []
    total = len(items)
    pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, pages - 1))
    start = page * page_size
    chunk = items[start:start + page_size]

    for i, m in enumerate(chunk):
        idx = start + i
        title = m.get("title", "")
        if len(title) > 50:
            title = title[:50] + "..."
        rows.append([InlineKeyboardButton(
            f"🎬 {title}",
            callback_data=f"ir:{idx}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ قبلی",
                     callback_data=f"irp:{page-1}"))
    if pages > 1:
        nav.append(InlineKeyboardButton(f"صفحه {page+1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton("بعدی ➡️",
                     callback_data=f"irp:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("⬅️ بازگشت", callback_data="irback_cat")])

    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def iranian_links_kb(links: list, title: str = "") -> InlineKeyboardMarkup:
    """لیست لینک‌های دانلود/پخش فیلم ایرانی."""
    rows = []
    for i, link in enumerate(links):
        link_title = link.get("title", f"لینک {i+1}")
        if len(link_title) > 48:
            link_title = link_title[:48] + "..."
        rows.append([InlineKeyboardButton(
            f"▶️ {link_title}",
            callback_data=f"irplay:{i}")])

    if rows:
        rows.append([InlineKeyboardButton("⬅️ بازگشت به لیست", callback_data="irback:0")])

    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def join_kb(channels, check_cb: str = "checkjoin") -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        link = ch["invite_link"] or (f"https://t.me/{ch['chat_id'].lstrip('@')}"
                                     if str(ch["chat_id"]).startswith("@") else None)
        title = ch["title"] or ch["chat_id"]
        if link:
            rows.append([InlineKeyboardButton(f"📢 {title}", url=link)])
    rows.append([InlineKeyboardButton("✅ عضو شدم", callback_data=check_cb)])
    return InlineKeyboardMarkup(rows)


def favorites_kb(favs) -> InlineKeyboardMarkup:
    rows = []
    for f in favs:
        rows.append([InlineKeyboardButton(f"🎬 {f['title']}", callback_data=f"mv:{f['movie_id']}")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def history_kb(items) -> InlineKeyboardMarkup:
    """تاریخچه‌ی تماشا: هر مورد دکمه‌ای برای باز کردن دوباره‌ی فیلم."""
    rows = []
    for it in items:
        ep = f" — {it['episode']}" if it["episode"] else ""
        rows.append([InlineKeyboardButton(f"🎬 {it['title']}{ep}",
                                          callback_data=f"mv:{it['movie_id']}")])
    if rows:
        rows.append([InlineKeyboardButton("🗑 پاک کردن تاریخچه", callback_data="clearwatch")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def recent_searches_kb(queries) -> InlineKeyboardMarkup:
    """جستجوهای اخیر: هر مورد دکمه‌ای برای جستجوی دوباره."""
    rows = []
    for i, q in enumerate(queries):
        rows.append([InlineKeyboardButton(f"🔍 {q}", callback_data=f"rs:{i}")])
    if rows:
        rows.append([InlineKeyboardButton("🗑 پاک کردن تاریخچه جستجو", callback_data="clearsearch")])
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def movie_deeplink_url(bot_username: str, movie_id: str) -> str:
    """ساخت لینک دیپ‌لینک به چت خصوصی ربات برای باز کردن کارت فیلم."""
    if not bot_username:
        return ""
    bu = bot_username.lstrip("@")
    return f"https://t.me/{bu}?start=movie_{movie_id}"


# ---------------- پنل مدیریت ----------------
def admin_panel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 آمار ربات", callback_data="adm:stats")],
        [InlineKeyboardButton("📢 کانال‌های عضویت اجباری", callback_data="adm:channels")],
        [InlineKeyboardButton("📣 پیام همگانی", callback_data="adm:broadcast")],
        [InlineKeyboardButton("📤 ارسال فایل دیتابیس", callback_data="adm:senddb"),
         InlineKeyboardButton("♻️ بازیابی دیتابیس", callback_data="adm:restoredb")],
        [InlineKeyboardButton("👤 مدیریت ادمین‌ها", callback_data="adm:admins")],
        [InlineKeyboardButton("🐞 لاگ خطاها", callback_data="adm:errors")],
        [InlineKeyboardButton("❌ بستن", callback_data="adm:close")],
    ])


def channels_kb(channels) -> InlineKeyboardMarkup:
    rows = []
    for ch in channels:
        title = ch["title"] or ch["chat_id"]
        rows.append([
            InlineKeyboardButton(f"📢 {title}", callback_data="noop"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"adm:delch:{ch['chat_id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ افزودن کانال", callback_data="adm:addch")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def admins_kb(admin_ids, super_admin: int) -> InlineKeyboardMarkup:
    rows = []
    for aid in admin_ids:
        label = f"👑 {aid}" + (" (سوپر‌ادمین)" if aid == super_admin else "")
        btns = [InlineKeyboardButton(label, callback_data="noop")]
        if aid != super_admin:
            btns.append(InlineKeyboardButton("🗑", callback_data=f"adm:deladmin:{aid}"))
        rows.append(btns)
    rows.append([InlineKeyboardButton("➕ افزودن ادمین", callback_data="adm:addadmin")])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="adm:home")])
    return InlineKeyboardMarkup(rows)


def back_home_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به پنل", callback_data="adm:home")]])