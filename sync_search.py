# -*- coding: utf-8 -*-
"""
sync_search.py
--------------
اسکریپت همگام‌سازی آرشیو فیلم و سریال ایرانی از serialirany.com به دیتابیس.

این نسخه به‌جای استفاده از صفحات آرشیو، از قابلیت جستجوی سایت استفاده می‌کند
تا تمام فیلم‌ها و سریال‌ها را پیدا کند.

روش کار:
  • جستجوی تک‌حرفی (ا، ب، پ، ت، ث، ج، چ، ح، خ، د، ذ، ر، ز، ژ، س، ش، ص، ض، ط، ظ، ع، غ، ف، ق، ک، گ، ل، م، ن، و، ه، ی)
  • برای هر حرف، تمام نتایج جستجو را در دیتابیس ذخواند
  • سپس برای هر فیلم/سریال، لینک مستقیم را استخراج می‌کند

نحوه استفاده:
  ۱. این فایل را در همان پوشه‌ی ربات قرار دهید
  ۲. مطمئن شوید Chrome و ChromeDriver نصب هستند
  ۳. پکیج‌ها را نصب کنید: pip install -r requirements-sync.txt
  ۴. متغیر DATABASE_URL را تنظیم کنید:
       export DATABASE_URL="postgres://avnadmin:AVNS_wDfIwWbmz5XS2WgkvYh@pg-175a77e0-ehsanpoint-3c63.l.aivencloud.com:16853/defaultdb?sslmode=require"
  ۵. اجرا کنید: python sync_search.py

پارامترها:
  • --movies-only : فقط فیلم‌ها را اسکن کن
  • --series-only : فقط سریال‌ها را اسکن کن
  • --extract-links : لینک‌های مستقیم را هم استخراج کن (برای فیلم‌ها ۱۵ ثانیه صبر می‌کند)
  • --limit N : فقط N مورد اول را اسکن کن (برای تست)
  • --letter X : فقط یک حرف خاص را جستجو کن (مثلا --letter ا)
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, unquote, quote
import json

# اضافه‌کردن مسیر فعلی برای ایمپورت‌های محلی
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ایمپورت دیتابیس
from database import Database

# ایمپورت Selenium
try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from selenium.common.exceptions import (
        TimeoutException, NoSuchElementException,
        ElementNotInteractableException, StaleElementReferenceException,
        WebDriverException
    )
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False
    print("❌ Selenium نصب نیست. اجرا کنید: pip install selenium")
    sys.exit(1)

# ایمپورت BeautifulSoup
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print("⚠️ BeautifulSoup نصب نیست. اجرا کنید: pip install beautifulsoup4")

# ایمپورت requests
import requests

# ==================== تنظیمات ====================
BASE_URL = "https://serialirany.com"
SEARCH_URL_TEMPLATE = f"{BASE_URL}/?s={{query}}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# حروف الفبای فارسی برای جستجوی تک‌حرفی
PERSIAN_LETTERS = [
    "ا", "آ", "ب", "پ", "ت", "ث", "ج", "چ", "ح", "خ",
    "د", "ذ", "ر", "ز", "ژ", "س", "ش", "ص", "ض", "ط",
    "ظ", "ع", "غ", "ف", "ق", "ک", "گ", "ل", "م", "ن",
    "و", "ه", "ی"
]

# کلمات کلیدی پرکاربرد برای جستجو (در صورت نیاز)
COMMON_KEYWORDS = [
    "فیلم", "سریال", "دوبله", "زیرنویس", "فصل", "قسمت",
    "ایرانی", "خارجی", "جدید", "قدیمی",
]


# ==================== کلاس اصلی ====================
class SearchSynchronizer:
    def __init__(self, db_url: str = None, headless: bool = True,
                 wait_seconds: int = 17):
        """راه‌اندازی همگام‌ساز.
        db_url: اتصال به دیتابیس (اگر None، از DATABASE_URL محیط می‌گیرد)
        headless: اگر True، پنجره Chrome نمایش داده نمی‌شود
        wait_seconds: ثانیه‌های انتظار برای تبلیغات فیلم‌ها
        """
        # اتصال به دیتابیس
        if db_url:
            os.environ["DATABASE_URL"] = db_url
        self.db = Database("sync.db")
        print(f"✅ دیتابیس: {self.db.db_type}")

        # تنظیمات requests (برای جستجو)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})

        # تنظیمات Selenium
        self.options = Options()
        if headless:
            self.options.add_argument("--headless=new")
        self.options.add_argument("--no-sandbox")
        self.options.add_argument("--disable-dev-shm-usage")
        self.options.add_argument("--disable-gpu")
        self.options.add_argument("--window-size=1280,720")
        self.options.add_argument("--disable-extensions")
        self.options.add_argument("--user-agent=" + UA)
        self.options.add_experimental_option("excludeSwitches", ["enable-logging"])

        self.wait_seconds = wait_seconds
        self.driver = None

    def _init_driver(self):
        """ساخت WebDriver."""
        if self.driver is not None:
            return self.driver
        try:
            self.driver = webdriver.Chrome(options=self.options)
            self.driver.set_page_load_timeout(60)
            print("✅ Chrome WebDriver ساخته شد")
        except Exception as e:
            print(f"❌ خطا در ساخت WebDriver: {e}")
            print("نصب کنید: pip install webdriver-manager")
            print("یا ChromeDriver را به PATH اضافه کنید")
            sys.exit(1)
        return self.driver

    # ==================== جستجوی تک‌حرفی ====================
    def search_letter(self, letter: str) -> list:
        """جستجوی یک حرف در سایت و استخراج نتایج.
        خروجی: لیستی از {title, url, thumb}
        """
        # استفاده از جستجوی سایت
        search_url = f"{BASE_URL}/?s={quote(letter)}"
        print(f"  جستجوی حرف «{letter}»...")

        items = []
        try:
            r = self.session.get(search_url, timeout=30)
            if r.status_code != 200:
                print(f"    ⚠️ خطای HTTP: {r.status_code}")
                return []
            items = self._parse_search_results(r.text)
            print(f"    📊 {len(items)} نتیجه")
        except Exception as e:
            print(f"    ⚠️ خطا: {e}")

        return items

    @staticmethod
    def _parse_search_results(html: str) -> list:
        """پارس نتایج جستجو از HTML."""
        items = []
        if not HAS_BS4:
            return items

        soup = BeautifulSoup(html, "html.parser")

        # روش ۱: Elementor cards
        for card in soup.select(".elementor-post__card"):
            a_tag = card.select_one("a")
            if not a_tag or not a_tag.get("href"):
                continue
            link = a_tag["href"]
            title_el = (card.select_one(".elementor-post__title a")
                        or card.select_one(".elementor-post__title")
                        or card.select_one("h2") or card.select_one("h3")
                        or a_tag)
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                title = a_tag.get("title", "") or a_tag.get_text(strip=True)
            img = card.select_one("img")
            thumb = ""
            if img:
                thumb = (img.get("data-src") or img.get("data-lazy-src")
                         or img.get("src", ""))
            if title and link:
                items.append({"title": title, "url": link, "thumb": thumb})

        # روش ۲: a.yc-relative
        if not items:
            for a_tag in soup.select("a.yc-relative"):
                link = a_tag.get("href", "")
                title = ""
                h_tag = a_tag.select_one("h2") or a_tag.select_one("h3")
                if h_tag:
                    title = h_tag.get_text(strip=True)
                if not title:
                    title = a_tag.get("title", "") or a_tag.get_text(strip=True)
                img = a_tag.select_one("img")
                thumb = ""
                if img:
                    thumb = (img.get("data-src") or img.get("data-lazy-src")
                             or img.get("src", ""))
                if title and link:
                    items.append({"title": title, "url": link, "thumb": thumb})

        # روش ۳: هر article با لینک
        if not items:
            for article in soup.select("article"):
                a_tag = article.select_one("a")
                if not a_tag or not a_tag.get("href"):
                    continue
                link = a_tag["href"]
                title = article.select_one("h2")
                if title:
                    title = title.get_text(strip=True)
                else:
                    title = a_tag.get("title", "") or a_tag.get_text(strip=True)
                img = article.select_one("img")
                thumb = ""
                if img:
                    thumb = (img.get("data-src") or img.get("data-lazy-src")
                             or img.get("src", ""))
                if title and link:
                    items.append({"title": title, "url": link, "thumb": thumb})

        # حذف تکراری‌ها
        seen = set()
        unique = []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        return unique

    # ==================== تشخیص فیلم یا سریال ====================
    def detect_content_type(self, page_url: str) -> str:
        """تشخیص اینکه صفحه فیلم است یا سریال.
        خروجی: "movie" یا "series"
        """
        try:
            r = self.session.get(page_url, timeout=30)
            if r.status_code != 200:
                return "movie"  # پیش‌فرض
            html = r.text
            if HAS_BS4:
                soup = BeautifulSoup(html, "html.parser")
                # اگر .serie-item داشت → سریال
                if soup.select_one(".serie-item, .serie-episodes"):
                    return "series"
                # اگر .playmovie داشت → فیلم
                if soup.select_one("a.playmovie"):
                    return "movie"
                # اگر لینک onlineplay زیادی داشت → سریال
                onlineplay_links = soup.select('a[href*="onlineplay"]')
                if len(onlineplay_links) > 1:
                    return "series"
                return "movie"
        except Exception as e:
            print(f"    ⚠️ خطا در تشخیص نوع: {e}")
        return "movie"

    # ==================== استخراج لینک مستقیم فیلم ====================
    def extract_movie_link(self, page_url: str) -> str:
        """استخراج لینک مستقیم فیلم.
        ۱. وارد صفحه می‌شود
        ۲. روی دکمه «پخش فیلم» (.post-btns.yc a.playmovie) کلیک می‌کند
        ۳. ۱۷ ثانیه صبر می‌کند (برای تبلیغات)
        ۴. اگر دکمه Skip Ad وجود دارد، کلیک می‌کند
        ۵. لینک video.currentSrc را برمی‌گرداند
        """
        if not self.driver:
            self._init_driver()

        try:
            self.driver.get(page_url)
            time.sleep(2)

            # روش ۱: تلاش برای decode از URL (سریع، بدون انتظار)
            play_btn = self.driver.find_elements(
                By.CSS_SELECTOR, ".post-btns.yc a.playmovie, a.playmovie"
            )
            if play_btn:
                play_url = play_btn[0].get_attribute("href") or ""
                # تلاش برای decode q720 از URL
                direct = self._decode_quality_from_url(play_url)
                if direct:
                    print(f"    ✅ لینک از URL decode شد (سریع): {direct[:80]}...")
                    return direct

            # روش ۲: کلیک و انتظار (کند ولی مطمئن)
            print(f"    ⏳ کلیک روی پخش و انتظار {self.wait_seconds} ثانیه...")
            try:
                btn = WebDriverWait(self.driver, 10).until(
                    EC.element_to_be_clickable(
                        (By.CSS_SELECTOR, ".post-btns.yc a.playmovie, a.playmovie")
                    )
                )
                self.driver.execute_script("arguments[0].click();", btn)
            except TimeoutException:
                print(f"    ⚠️ دکمه playmovie پیدا نشد")
                return ""

            # انتظار برای تبلیغات
            time.sleep(self.wait_seconds)

            # کلیک روی Skip Ad اگر وجود دارد
            try:
                skip_selectors = [
                    ".vjs-skip-ad", ".vjs-skip-button", "button.skip-ad",
                    "[class*='skip']", "a.skip", "button[aria-label*='skip']"
                ]
                for sel in skip_selectors:
                    skip_btns = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    for skip_btn in skip_btns:
                        try:
                            if skip_btn.is_displayed() and skip_btn.is_enabled():
                                skip_btn.click()
                                print(f"    ✅ کلیک روی Skip Ad")
                                time.sleep(2)
                                break
                        except Exception:
                            continue
            except Exception:
                pass

            # استخراج لینک از video.currentSrc
            video_link = self.driver.execute_script(
                'var v = document.querySelector("video");'
                'if (v) return v.currentSrc || v.src || "";'
                'return "";'
            )
            if video_link:
                print(f"    ✅ لینک از video.currentSrc: {video_link[:80]}...")
                return video_link

            # روش ۳: تلاش برای پیدا کردن لینک در URL فعلی (بعد از کلیک)
            current_url = self.driver.current_url or ""
            direct = self._decode_quality_from_url(current_url)
            if direct:
                print(f"    ✅ لینک از URL فعلی: {direct[:80]}...")
                return direct

            print(f"    ⚠️ لینک پیدا نشد")
            return ""

        except Exception as e:
            print(f"    ❌ خطا: {e}")
            return ""

    @staticmethod
    def _decode_quality_from_url(url: str) -> str:
        """استخراج لینک مستقیم از URL با decode Base64.
        URL شامل پارامترهای q720, q1080, q480 است.
        """
        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            # اولویت: q720 > q1080 > q480
            encoded = (params.get("q720", [None])[0]
                       or params.get("q1080", [None])[0]
                       or params.get("q480", [None])[0])
            if not encoded:
                # fallback: هر پارامتری که با q شروع می‌شود
                for key, val in params.items():
                    if key.startswith("q") and val:
                        encoded = val[0]
                        break
            if not encoded:
                return ""
            # Base64 decode (URL-safe + padding)
            padded = encoded + "=" * (-len(encoded) % 4)
            try:
                decoded = base64.urlsafe_b64decode(padded).decode("utf-8", errors="ignore")
            except Exception:
                decoded = base64.b64decode(padded).decode("utf-8", errors="ignore")
            if decoded.startswith("http"):
                return decoded
        except Exception:
            pass
        return ""

    # ==================== استخراج قسمت‌های سریال ====================
    def extract_series_episodes(self, page_url: str) -> int:
        """استخراج تمام قسمت‌های یک سریال.
        ۱. وارد صفحه سریال می‌شود
        ۲. تمام .serie-item را پیدا می‌کند
        ۳. برای هر آیتم: لینک onlineplay را می‌گیرد، decode می‌کند
        ۴. در دیتابیس ذخیره می‌کند
        خروجی: تعداد قسمت‌های ذخیره شده.
        """
        if not self.driver:
            self._init_driver()

        try:
            self.driver.get(page_url)
            time.sleep(2)
        except Exception as e:
            print(f"    ❌ خطا در بارگذاری صفحه: {e}")
            return 0

        page_html = self.driver.page_source
        episodes = self._parse_series_episodes(page_html)
        print(f"    📊 {len(episodes)} قسمت پیدا شد")

        saved_count = 0
        for ep in episodes:
            season = ep.get("season", "1")
            episode = ep.get("episode", "")
            title = ep.get("title", "")
            onlineplay_url = ep.get("onlineplay_url", "")

            # استخراج لینک مستقیم از onlineplay URL
            direct_link = self._decode_quality_from_url(onlineplay_url)

            try:
                self.db.upsert_iranian_episode(
                    series_url=page_url,
                    season=season,
                    episode=episode,
                    title=title,
                    onlineplay_url=onlineplay_url,
                    direct_link=direct_link,
                )
                saved_count += 1
                if direct_link:
                    print(f"      ✅ فصل {season} قسمت {episode}: {direct_link[:60]}...")
                else:
                    print(f"      ⚠️ فصل {season} قسمت {episode}: لینک decode نشد")
            except Exception as e:
                print(f"      ❌ خطا در ذخیره: {e}")

        return saved_count

    @staticmethod
    def _parse_series_episodes(html: str) -> list:
        """پارس قسمت‌های سریال از HTML."""
        episodes = []
        if not HAS_BS4:
            return episodes

        soup = BeautifulSoup(html, "html.parser")

        # پیدا کردن بخش محتوای اصلی
        content_area = (soup.select_one(".serie-episodes")
                        or soup.select_one(".entry-content")
                        or soup.select_one(".single-main")
                        or soup.select_one(".elementor-widget-container")
                        or soup.select_one("main")
                        or soup.select_one("article")
                        or soup)

        # حذف بخش‌های پیشنهادی و تبلیغاتی
        for selector in [".suggestions", ".suggested", ".recommend", ".related",
                          ".also-watch", ".sidebar", ".ads", ".advertisement"]:
            for el in content_area.select(selector):
                el.decompose()
        for tag_name in ["nav", "header", "footer"]:
            for el in content_area.find_all(tag_name):
                el.decompose()

        # روش ۱: .serie-item
        serie_items = content_area.select(".serie-item")
        if not serie_items:
            for cls in ["serie-item", "serie_item", "serie-title",
                        "episode-item", "serie-episodes-item"]:
                serie_items = content_area.select(f".{cls}")
                if serie_items:
                    break

        ep_counter: dict = {}

        def _clean_title(text: str, season: str, found_ep: str = "") -> str:
            if found_ep:
                return f"قسمت {found_ep}"
            m = re.search(r"قسمت\s*(\d+)", text)
            if m:
                return f"قسمت {m.group(1)}"
            ep_counter[season] = ep_counter.get(season, 0) + 1
            return f"قسمت {ep_counter[season]}"

        if serie_items:
            for item in serie_items:
                text = item.get_text(" ", strip=True)
                text = re.sub(r"\s+", " ", text).strip()
                a_tag = item.select_one('a[href*="onlineplay"]')
                if not a_tag:
                    a_tag = item.select_one("a[href]")
                if not a_tag or not a_tag.get("href"):
                    continue
                href = a_tag["href"]

                season_m = re.search(r"فصل\s*(\d+)", text)
                ep_m = re.search(r"قسمت\s*(\d+)", text)
                if not (season_m and ep_m):
                    ep_param_m = re.search(r'[?&]ep=([^&]+)', href)
                    if ep_param_m:
                        ep_text = unquote(ep_param_m.group(1)).replace("+", " ").strip()
                        ep_text = re.sub(r"\s+", " ", ep_text)
                        if not season_m:
                            season_m = re.search(r"فصل\s*(\d+)", ep_text)
                        if not ep_m:
                            ep_m = re.search(r"قسمت\s*(\d+)", ep_text)

                s_num = season_m.group(1) if season_m else "1"
                if s_num not in ep_counter:
                    ep_counter[s_num] = 0

                found_ep_num = ep_m.group(1) if ep_m else ""
                clean_title = _clean_title(text, s_num, found_ep_num)

                if found_ep_num:
                    e_num = found_ep_num
                    if ep_counter.get(s_num, 0) < int(e_num):
                        ep_counter[s_num] = int(e_num)
                else:
                    ep_counter[s_num] = ep_counter.get(s_num, 0) + 1
                    e_num = str(ep_counter[s_num])

                episodes.append({
                    "season": s_num,
                    "episode": e_num,
                    "title": clean_title,
                    "onlineplay_url": href,
                })
        else:
            # روش ۲: لینک‌های onlineplay
            for a in content_area.find_all("a", href=True):
                href = a["href"]
                if "onlineplay" not in href.lower():
                    continue
                ep_param_m = re.search(r'[?&]ep=([^&]+)', href)
                if ep_param_m:
                    full_text = unquote(ep_param_m.group(1)).replace("+", " ").strip()
                    full_text = re.sub(r"\s+", " ", full_text)
                else:
                    full_text = a.get_text(" ", strip=True)
                    full_text = re.sub(r"\s+", " ", full_text).strip()
                    if not full_text:
                        continue
                season_m = re.search(r"فصل\s*(\d+)", full_text)
                ep_m = re.search(r"قسمت\s*(\d+)", full_text)
                s_num = season_m.group(1) if season_m else "1"
                if s_num not in ep_counter:
                    ep_counter[s_num] = 0

                found_ep_num = ep_m.group(1) if ep_m else ""
                clean_title = _clean_title(full_text, s_num, found_ep_num)

                if found_ep_num:
                    e_num = found_ep_num
                    if ep_counter.get(s_num, 0) < int(e_num):
                        ep_counter[s_num] = int(e_num)
                else:
                    ep_counter[s_num] = ep_counter.get(s_num, 0) + 1
                    e_num = str(ep_counter[s_num])

                episodes.append({
                    "season": s_num,
                    "episode": e_num,
                    "title": clean_title,
                    "onlineplay_url": href,
                })

        # حذف تکراری‌ها
        seen = set()
        unique = []
        for ep in episodes:
            key = (ep["season"], ep["episode"], ep["onlineplay_url"])
            if key not in seen:
                seen.add(key)
                unique.append(ep)
        return unique

    # ==================== فرآیند اصلی ====================
    def run(self, movies_only: bool = False, series_only: bool = False,
            extract_links: bool = False, limit: int = 0,
            letter: str = ""):
        """اجرای کامل همگام‌سازی با جستجوی تک‌حرفی."""
        print("\n" + "=" * 60)
        print("🚀 شروع همگام‌سازی با جستجوی تک‌حرفی")
        print("=" * 60)
        print(f"دیتابیس: {self.db.db_type}")
        print(f"Extract links: {extract_links}")
        print(f"Limit: {limit if limit else 'نامحدود'}")

        # مرحله ۱: جستجوی تک‌حرفی
        print(f"\n{'='*60}")
        print("📚 مرحله ۱: جستجوی تک‌حرفی")
        print("=" * 60)

        all_items = []
        if letter:
            # فقط یک حرف خاص
            letters = [letter]
        else:
            letters = PERSIAN_LETTERS

        for lt in letters:
            items = self.search_letter(lt)
            for item in items:
                # بررسی تکراری نبودن
                if not any(i["url"] == item["url"] for i in all_items):
                    all_items.append(item)

        print(f"\n📊 مجموعاً {len(all_items)} مورد یکتا پیدا شد")

        # مرحله ۲: تشخیص نوع و ذخیره در دیتابیس
        print(f"\n{'='*60}")
        print("💾 مرحله ۲: تشخیص نوع و ذخیره در دیتابیس")
        print("=" * 60)

        if limit:
            all_items = all_items[:limit]

        movies_count = 0
        series_count = 0
        for i, item in enumerate(all_items, 1):
            title = item["title"]
            url = item["url"]
            thumb = item.get("thumb", "")

            # بررسی اینکه آیا از قبل در دیتابیس هست
            existing_movie = self.db.get_iranian_movie(url)
            existing_series = self.db.get_iranian_series(url)

            if existing_movie or existing_series:
                print(f"[{i}/{len(all_items)}] {title} — از قبل در دیتابیس هست")
                continue

            # تشخیص نوع (فیلم یا سریال)
            content_type = self.detect_content_type(url)
            print(f"[{i}/{len(all_items)}] {title} — {content_type}")

            try:
                self.db.upsert_iranian_movie(
                    title=title,
                    page_url=url,
                    poster=thumb,
                    direct_link="",  # بعداً استخراج می‌شود
                    content_type=content_type,
                )
                if content_type == "movie":
                    movies_count += 1
                else:
                    series_count += 1
            except Exception as e:
                print(f"    ⚠️ خطا در ذخیره: {e}")

        print(f"\n📊 فیلم‌های جدید: {movies_count}")
        print(f"📊 سریال‌های جدید: {series_count}")

        # مرحله ۳: استخراج لینک‌های مستقیم (اگر درخواست شده)
        if extract_links:
            print(f"\n{'='*60}")
            print("🎬 مرحله ۳: استخراج لینک‌های مستقیم فیلم‌ها")
            print("=" * 60)
            pending_movies = self.db.get_pending_movies()
            if limit:
                pending_movies = pending_movies[:limit]
            print(f"📊 {len(pending_movies)} فیلم برای استخراج لینک")

            for i, movie in enumerate(pending_movies, 1):
                title = movie["title"]
                url = movie["page_url"]
                print(f"\n[{i}/{len(pending_movies)}] {title}")
                direct_link = self.extract_movie_link(url)
                if direct_link:
                    self.db.upsert_iranian_movie(
                        title=title,
                        page_url=url,
                        direct_link=direct_link,
                        content_type="movie",
                    )
                    print(f"  ✅ ذخیره شد")
                else:
                    print(f"  ⚠️ لینک پیدا نشد")
                time.sleep(1)

            print(f"\n{'='*60}")
            print("📺 مرحله ۴: استخراج قسمت‌های سریال‌ها")
            print("=" * 60)
            pending_series = self.db.get_pending_series()
            if limit:
                pending_series = pending_series[:limit]
            print(f"📊 {len(pending_series)} سریال برای استخراج قسمت‌ها")

            for i, series in enumerate(pending_series, 1):
                title = series["title"]
                url = series["page_url"]
                print(f"\n[{i}/{len(pending_series)}] {title}")
                count = self.extract_series_episodes(url)
                print(f"  ✅ {count} قسمت ذخیره شد")
                time.sleep(1)

        print("\n" + "=" * 60)
        print("✅ همگام‌سازی کامل شد!")
        print("=" * 60)
        # آمار
        stats = {
            "movies": len(self.db.list_iranian_movies("movie")),
            "series": len(self.db.list_iranian_series()),
        }
        print(f"📊 فیلم‌ها: {stats['movies']}")
        print(f"📊 سریال‌ها: {stats['series']}")

    def close(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        self.db.close()


# ==================== اجرای اصلی ====================
def main():
    parser = argparse.ArgumentParser(
        description="همگام‌سازی آرشیو فیلم و سریال ایرانی با جستجوی تک‌حرفی"
    )
    parser.add_argument("--movies-only", action="store_true",
                        help="فقط فیلم‌ها را اسکن کن")
    parser.add_argument("--series-only", action="store_true",
                        help="فقط سریال‌ها را اسکن کن")
    parser.add_argument("--extract-links", action="store_true",
                        help="لینک‌های مستقیم را هم استخراج کن (برای فیلم‌ها ۱۷ ثانیه صبر می‌کند)")
    parser.add_argument("--limit", type=int, default=0,
                        help="فقط N مورد اول را اسکن کن (برای تست)")
    parser.add_argument("--letter", type=str, default="",
                        help="فقط یک حرف خاص را جستجو کن (مثلا --letter ا)")
    parser.add_argument("--no-headless", action="store_true",
                        help="پنجره Chrome نمایش داده شود (برای دیباگ)")
    parser.add_argument("--wait", type=int, default=17,
                        help="ثانیه‌های انتظار برای تبلیغات فیلم‌ها (پیش‌فرض: ۱۷)")
    args = parser.parse_args()

    # بررسی DATABASE_URL
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if not db_url:
        print("❌ DATABASE_URL تنظیم نشده!")
        print("مثال:")
        print('  export DATABASE_URL="postgres://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require"')
        print("یا در فایل .env قرار دهید")
        sys.exit(1)

    # اجرای همگام‌سازی
    sync = SearchSynchronizer(
        db_url=db_url,
        headless=not args.no_headless,
        wait_seconds=args.wait,
    )
    try:
        sync.run(
            movies_only=args.movies_only,
            series_only=args.series_only,
            extract_links=args.extract_links,
            limit=args.limit,
            letter=args.letter,
        )
    except KeyboardInterrupt:
        print("\n\n⚠️ متوقف شد توسط کاربر")
    except Exception as e:
        print(f"\n❌ خطا: {e}")
        import traceback
        traceback.print_exc()
    finally:
        sync.close()


if __name__ == "__main__":
    main()
