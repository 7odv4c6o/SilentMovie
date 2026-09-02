# -*- coding: utf-8 -*-
"""
formatting.py
-------------
ساخت متن‌های نمایشی ربات (کارت فیلم و…).
"""
from __future__ import annotations

from site_client import Movie


def esc(s: str) -> str:
    """فرار کاراکترهای HTML برای parse_mode=HTML."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def movie_caption(movie: Movie) -> str:
    lines = [f"🎬 <b>{esc(movie.title)}</b>"]
    if movie.original_title:
        lines.append(f"🔤 <i>{esc(movie.original_title)}</i>")
    meta = []
    if movie.year:
        meta.append(f"📅 {esc(movie.year)}")
    if movie.imdb:
        meta.append(f"⭐ IMDb {esc(movie.imdb)}")
    if movie.age:
        meta.append(f"🔞 {esc(movie.age)}")
    if meta:
        lines.append(" | ".join(meta))
    if movie.genre:
        lines.append(f"🎭 ژانر: {esc(movie.genre)}")
    if movie.country:
        lines.append(f"🌍 کشور: {esc(movie.country)}")
    if movie.director:
        lines.append(f"🎥 کارگردان: {esc(movie.director)}")
    if movie.stars:
        stars = movie.stars
        if len(stars) > 160:
            stars = stars[:160] + "…"
        lines.append(f"👥 بازیگران: {esc(stars)}")
    if movie.plot:
        plot = movie.plot
        if len(plot) > 600:
            plot = plot[:600] + "…"
        lines.append(f"\n📖 <b>خلاصه:</b>\n{esc(plot)}")

    kind = "سریال" if movie.is_series else "فیلم"
    count = len(movie.episodes)
    lines.append(f"\n📥 <b>لینک‌های پخش ({kind} — {count} مورد):</b>")
    lines.append("روی هر قسمت بزنید تا پخش آنلاین شروع شود.")
    return "\n".join(lines)


def play_message(movie: Movie, ep, vlc_link: str, https_link: str) -> str:
    return (
        f"🎬 <b>{esc(movie.title)}</b>\n"
        f"▶️ {esc(ep.label)}\n\n"
        f"📱 <b>روش ۱ — باز کردن مستقیم در VLC:</b>\n"
        f"روی لینک زیر بزنید (اگر VLC نصب باشد خودکار باز می‌شود):\n"
        f"<code>{esc(vlc_link)}</code>\n\n"
        f"💻 <b>روش ۲ — باز کردن جریان شبکه (Open Network Stream):</b>\n"
        f"لینک زیر را در VLC وارد کنید:\n"
        f"<code>{esc(https_link)}</code>\n\n"
        f"⏳ توجه: این لینک موقتی است و بعد از مدتی منقضی می‌شود؛ "
        f"در صورت خطا دوباره روی قسمت بزنید."
    )


def webapp_play_message(movie: Movie, ep) -> str:
    """پیام پخش WebApp — بدون نمایش هیچ لینکی."""
    return (
        f"🎬 <b>{esc(movie.title)}</b>\n"
        f"▶️ {esc(ep.label)}\n\n"
        f"روی دکمه‌ی «تماشای آنلاین» بزنید تا پخش شروع شود."
    )
