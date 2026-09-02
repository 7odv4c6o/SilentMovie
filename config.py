# -*- coding: utf-8 -*-
"""
config.py
---------
خواندن تنظیمات از فایل .env (یا متغیرهای محیطی).
"""
from __future__ import annotations

import os
from typing import List


def _load_dotenv(path: str = ".env") -> None:
    """یک .env ساده را بدون وابستگی خارجی می‌خواند."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # فقط اگر از قبل در محیط نباشد
            os.environ.setdefault(key, val)


_load_dotenv()


def _int_list(raw: str) -> List[int]:
    out = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out


# ---- تلگرام ----
BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "8346392606:AAEPQ2mMMHDxOXGMJGDN1u_A6TCbYDbtSnY")
# ادمین‌های اولیه (با کاما جدا شوند). اولین نفر «سوپر‌ادمین» است.
ADMIN_IDS: List[int] = _int_list(os.environ.get("ADMIN_IDS", "7232719340"))

# ---- اکانت سایت ----
SITE_MOBILE: str = os.environ.get("SITE_MOBILE", "09334897017")
SITE_PASSWORD: str = os.environ.get("SITE_PASSWORD", "Ehsan138813")

# ---- رفتار ----
# هر چند ساعت فایل دیتابیس برای ادمین‌ها ارسال شود
BACKUP_INTERVAL_HOURS: float = float(os.environ.get("BACKUP_INTERVAL_HOURS", "2"))
# مدت اعتبار کش فیلم (ثانیه) — پیش‌فرض ۶ ساعت
MOVIE_CACHE_TTL: int = int(os.environ.get("MOVIE_CACHE_TTL", str(6 * 3600)))
# تعداد نتایج در هر صفحه‌ی جستجوی درون‌خطی
SEARCH_PAGE_SIZE: int = int(os.environ.get("SEARCH_PAGE_SIZE", "8"))

# ---- مسیرها ----
# اگر DATABASE_URL تنظیم شده باشد (مثلا postgres://...), از PostgreSQL استفاده می‌شود.
# در غیر این‌صورت، از SQLite (فایل محلی) استفاده می‌شود.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "").strip()
# مسیر فایل SQLite (فقط وقتی DATABASE_URL تنظیم نشده باشد استفاده می‌شود)
DB_PATH: str = os.environ.get("DB_PATH", "data/bot.db")
SESSION_PATH: str = os.environ.get("SESSION_PATH", "data/site_session.pkl")

# ---- پیش‌بارگذاری آرشیو ایرانی ----
# اگر True باشد، ربات هنگام استارت آرشیو فیلم/سریال ایرانی را در پس‌زمینه بارگذاری می‌کند.
# نکته: این کار از Selenium + Chrome استفاده می‌کند که روی Render free tier (512MB RAM)
# ممکنه باعث هنگ کردن ربات بشه. اگر روی Render free tier هستید، این را False بگذارید.
# در غیر این‌صورت (VPS با RAM زیاد) True بگذارید تا تجربه‌ی کاربر بهتر بشه.
ENABLE_PRELOAD: bool = os.environ.get("ENABLE_PRELOAD", "false").lower() in ("true", "1", "yes", "on")

# ---- استفاده از Selenium برای آرشیو ایرانی ----
# اگر True باشد، ربات هنگام نمایش آرشیو فیلم/سریال ایرانی از Selenium برای کلیک روی
# دکمه‌ی «مشاهده بیشتر» استفاده می‌کند تا تمام آیتم‌ها را بگیرد.
# نکته مهم: Selenium + Chrome حدود ۲۰۰-۴۰۰MB RAM مصرف می‌کند.
# روی Render free tier (512MB) این باعث OOM (Ran out of memory) می‌شود.
# پیش‌فرض: False — فقط از requests استفاده می‌شود (سریع ولی فقط صفحه اول).
# روی VPS با RAM زیاد می‌توانید True بگذارید.
USE_SELENIUM_FOR_ARCHIVE: bool = os.environ.get("USE_SELENIUM_FOR_ARCHIVE", "false").lower() in ("true", "1", "yes", "on")

# ---- پروکسی (اختیاری) ----
# اگر روی سرور/کامپیوتری هستید که تلگرام مسدود است، آدرس پروکسی را اینجا بگذارید.
# نمونه‌ها:
#   socks5://127.0.0.1:1080
#   http://127.0.0.1:8080
# روی VPS خارج از ایران این را خالی بگذارید.
TELEGRAM_PROXY: str = os.environ.get("TELEGRAM_PROXY", "").strip()

# ---- سایت پلیر Vercel ----
# لینک مستقیم فیلم‌ها پشت این آدرس پیچیده میشه تا توی مرورگر پخش بشه
# بجای دانلود. مثال: https://silentmoviebot.vercel.app
PLAYER_URL: str = os.environ.get("PLAYER_URL", "https://silentmoviebot.vercel.app").rstrip("/")


def token_is_placeholder() -> bool:
    return (not BOT_TOKEN) or BOT_TOKEN.startswith("PUT-YOUR")
