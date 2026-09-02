# -*- coding: utf-8 -*-
"""
sync_archive.py
---------------
اسکریپت همگام‌سازی آرشیو فیلم و سریال ایرانی از serialirany.com به دیتابیس.

این اسکریپت را روی کامپیوتر خودتان اجرا کنید (نه روی Render)، چون از Selenium
استفاده می‌کند که روی Render free tier باعث OOM می‌شود.

نحوه استفاده:
  ۱. این فایل را در همان پوشه‌ی ربات قرار دهید
  ۲. مطمئن شوید Chrome و ChromeDriver نصب هستند
  ۳. پکیج‌ها را نصب کنید: pip install -r requirements-sync.txt
  ۴. متغیر DATABASE_URL را تنظیم کنید (در فایل .env یا به‌صورت مستقیم):
       export DATABASE_URL="postgres://avnadmin:AVNS_wDfIwWbmz5XS2WgkvYh@pg-175a77e0-ehsanpoint-3c63.l.aivencloud.com:16853/defaultdb?sslmode=require"
  ۵. اجرا کنید: python sync_archive.py

این اسکریپت:
  • وارد صفحه آرشیو فیلم‌ها می‌شود و دکمه «مشاهده بیشتر» را تا انتها می‌زند
  • تمام فیلم‌ها را در دیتابیس ذخیره می‌کند (فقط فیلم‌های جدید)
  • برای هر فیلم: روی «پخش فیلم» کلیک می‌کند، ۱۵ ثانیه صبر می‌کند،
    لینک مستقیم ویدیو را از currentSrc می‌گیرد و در دیتابیس ذخیره می‌کند
  • برای سریال‌ها: وارد صفحه می‌شود، لیست قسمت‌ها را از .serie-item می‌گیرد،
    لینک مستقیم هر قسمت را از پارامتر q720/q1080 (Base64) استخراج می‌کند
  • فیلم‌ها و سریال‌های قبلی را دوباره اسکن نمی‌کند (به‌جز اگر --force بدهید)

پارامترها:
  • --movies-only : فقط فیلم‌ها را اسکن کن
  • --series-only : فقط سریال‌ها را اسکن کن
  • --force : همه را دوباره اسکن کن (حتی اگر لینک دارند)
  • --limit N : فقط N فیلم/سریال اول را اسکن کن (برای تست)
"""
from __future__ import annotations

import argparse
import base64
import os
import re
import sys
import time
from urllib.parse import urlparse, parse_qs, unquote

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

# ایمپورت BeautifulSoup (اختیاری)
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    print("⚠️ BeautifulSoup نصب نیست (اختیاری). اجرا کنید: pip install beautifulsoup4")

# ==================== تنظیمات ====================
BASE_URL = "https://serialirany.com"
MOVIE_ARCHIVE = f"{BASE_URL}/%d8%a2%d8%b1%d8%b4%db%8c%d9%88-%d9%81%db%8c%d9%84%d9%85-%d9%87%d8%a7/"
SERIES_ARCHIVE = f"{BASE_URL}/%d8%a2%d8%b1%d8%b4%db%8c%d9%88-%d8%b3%d8%b1%db%8c%d8%a7%d9%84%d9%87%d8%a7/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


# ==================== کلاس اصلی ====================
class ArchiveSynchronizer:
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
        self.db = Database("sync.db")  # path فقط برای SQLite است، برای PG استفاده نمی‌شود
        print(f"✅ دیتابیس: {self.db.db_type}")

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
        try:
            self.driver = webdriver.Chrome(options=self.options)
            self.driver.set_page_load_timeout(60)
            print("✅ Chrome WebDriver ساخته شد")
        except Exception as e:
            print(f"❌ خطا در ساخت WebDriver: {e}")
            print("نصب کنید: pip install webdriver-manager")
            print("یا ChromeDriver را به PATH اضافه کنید")
            sys.exit(1)

    # ==================== بارگذاری آرشیو ====================
    def load_archive(self, archive_url: str, content_type: str) -> int:
        """بارگذاری تمام آیتم‌های آرشیو با کلیک روی «مشاهده بیشتر».
        خروجی: تعداد آیتم‌های پیدا شده.
        """
        if not self.driver:
            self._init_driver()

        print(f"\n{'='*60}")
        print(f"📚 بارگذاری آرشیو {content_type}ها")
        print(f"{'='*60}")
        print(f"URL: {archive_url}")

        self.driver.get(archive_url)
        time.sleep(3)

        clicks = 0
        no_progress_count = 0
        last_count = 0

        while clicks < 500:  # حد امن: ۵۰۰ کلیک
            # اسکرول به پایین
            try:
                self.driver.execute_script(
                    "window.scrollTo(0, document.body.scrollHeight);"
                )
                time.sleep(1.5)
            except WebDriverException:
                break

            # پیدا کردن دکمه «مشاهده بیشتر»
            load_more_btn = None
            selectors = [
                "a.elementor-button.elementor-size-sm",
                "a.elementor-button",
                "button.elementor-button",
                ".elementor-button-text",
                ".e-load-more-anchor",
                "a:contains('مشاهده بیشتر')",
            ]
            for sel in selectors:
                try:
                    if ":contains(" in sel:
                        # XPath برای متن فارسی
                        xpath = "//a[contains(.,'مشاهده بیشتر')] | //button[contains(.,'مشاهده بیشتر')] | //span[contains(.,'مشاهده بیشتر')]"
                        els = self.driver.find_elements(By.XPATH, xpath)
                        for el in els:
                            try:
                                if el.is_displayed() and el.is_enabled():
                                    # پیدا کردن parent که clickable باشد
                                    load_more_btn = el
                                    # اگر el تگ span است، parent آن را بگیر
                                    if el.tag_name.lower() in ("span", "div"):
                                        parent = el.find_element(By.XPATH, "..")
                                        if parent.is_displayed() and parent.is_enabled():
                                            load_more_btn = parent
                                    break
                            except StaleElementReferenceException:
                                continue
                    else:
                        els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        for el in els:
                            try:
                                txt = (el.text or "").strip()
                                if el.is_displayed() and el.is_enabled():
                                    if "مشاهده بیشتر" in txt or "بیشتر" in txt or not txt:
                                        load_more_btn = el
                                        break
                            except StaleElementReferenceException:
                                continue
                    if load_more_btn:
                        break
                except Exception:
                    continue

            if not load_more_btn:
                print(f"  ✅ دکمه «مشاهده بیشتر» پیدا نشد — پایان لیست")
                break

            # شمارش آیتم‌های فعلی
            current_items = self.driver.find_elements(
                By.CSS_SELECTOR,
                "a.yc-relative, .elementor-post__card a"
            )
            current_count = len(current_items)

            # کلیک روی دکمه
            try:
                self.driver.execute_script(
                    "arguments[0].scrollIntoView({block: 'center'});",
                    load_more_btn
                )
                time.sleep(0.5)
                self.driver.execute_script("arguments[0].click();", load_more_btn)
                clicks += 1
                time.sleep(2.5)

                # بررسی پیشرفت
                new_items = self.driver.find_elements(
                    By.CSS_SELECTOR,
                    "a.yc-relative, .elementor-post__card a"
                )
                new_count = len(new_items)
                if new_count == last_count:
                    no_progress_count += 1
                    if no_progress_count >= 3:
                        print(f"  ✅ پیشرفت متوقف شد — احتمالاً پایان لیست")
                        break
                else:
                    no_progress_count = 0
                last_count = new_count

                if clicks % 10 == 0:
                    print(f"  📊 کلیک {clicks} — {new_count} آیتم")
            except (StaleElementReferenceException,
                    ElementNotInteractableException,
                    WebDriverException) as e:
                print(f"  ⚠️ خطا هنگام کلیک: {e}")
                break

        print(f"  📊 بارگذاری کامل: {clicks} کلیک، {last_count} آیتم")

        # استخراج تمام آیتم‌ها از صفحه
        page_html = self.driver.page_source
        items = self._parse_archive_items(page_html)
        print(f"  📊 آیتم‌های پارس شده: {len(items)}")

        # ذخیره در دیتابیس
        saved_count = 0
        for item in items:
            title = item.get("title", "").strip()
            url = item.get("url", "").strip()
            thumb = item.get("thumb", "").strip()
            if title and url:
                try:
                    self.db.upsert_iranian_movie(
                        title=title,
                        page_url=url,
                        poster=thumb,
                        direct_link="",  # بعداً اسکن می‌شود
                        content_type=content_type,
                    )
                    saved_count += 1
                except Exception as e:
                    print(f"  ⚠️ خطا در ذخیره {title}: {e}")

        print(f"  ✅ {saved_count} {content_type} در دیتابیس ذخیره/به‌روزرسانی شد")
        return saved_count

    @staticmethod
    def _parse_archive_items(html: str) -> list:
        """پارس لیست آیتم‌ها از HTML آرشیو."""
        items = []
        if HAS_BS4:
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
        else:
            # regex fallback
            pattern = re.compile(
                r'<a[^>]*class="[^"]*yc-relative[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                re.S)
            for m in pattern.finditer(html):
                url = m.group(1)
                block = m.group(2)
                title_m = re.search(r'<h[23][^>]*>([^<]+)<', block)
                if not title_m:
                    title_m = re.search(r'title="([^"]+)"', block)
                img_m = re.search(r'<img[^>]+(?:src|data-src)="([^"]+)"', block)
                title = re.sub(r'\s+', ' ', title_m.group(1)).strip() if title_m else ""
                if title and url:
                    items.append({
                        "title": title,
                        "url": url,
                        "thumb": img_m.group(1) if img_m else ""
                    })

        # حذف تکراری‌ها
        seen = set()
        unique = []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        return unique

    # ==================== استخراج لینک فیلم ====================
    def extract_movie_link(self, page_url: str) -> str:
        """استخراج لینک مستقیم فیلم.
        ۱. وارد صفحه می‌شود
        ۲. روی دکمه «پخش فیلم» (.post-btns.yc a.playmovie) کلیک می‌کند
        ۳. ۱۵+ ثانیه صبر می‌کند (برای تبلیغات)
        ۴. اگر دکمه Skip Ad وجود دارد، کلیک می‌کند
        ۵. لینک video.currentSrc را برمی‌گرداند
        """
        if not self.driver:
            self._init_driver()

        try:
            self.driver.get(page_url)
            time.sleep(2)

            # روش ۱: تلاش برای decode از URL (سریع، بدون انتظار)
            # اگر صفحه دکمه playmovie دارد و URL آن شامل q720 است
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
                # شاید دکمه داخل iframe است یا صفحه فرق دارد
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

    # ==================== استخراج لینک سریال ====================
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
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")
            content_area = (soup.select_one(".serie-episodes")
                            or soup.select_one(".entry-content")
                            or soup.select_one(".single-main")
                            or soup.select_one(".elementor-widget-container")
                            or soup.select_one("main")
                            or soup.select_one("article")
                            or soup)
            # روش ۱: .serie-item
            serie_items = content_area.select(".serie-item")
            if not serie_items:
                for cls in ["serie-item", "serie_item", "serie-title",
                            "episode-item", "serie-episodes-item"]:
                    serie_items = content_area.select(f".{cls}")
                    if serie_items:
                        break

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
                    # fallback: از پارامتر ep
                    if not (season_m and ep_m):
                        ep_param_m = re.search(r'[?&]ep=([^&]+)', href)
                        if ep_param_m:
                            ep_text = unquote(ep_param_m.group(1)).replace("+", " ").strip()
                            ep_text = re.sub(r"\s+", " ", ep_text)
                            if not season_m:
                                season_m = re.search(r"فصل\s*(\d+)", ep_text)
                            if not ep_m:
                                ep_m = re.search(r"قسمت\s*(\d+)", ep_text)
                            if not text:
                                text = ep_text

                    season = season_m.group(1) if season_m else "1"
                    episode = ep_m.group(1) if ep_m else ""
                    episodes.append({
                        "season": season,
                        "episode": episode,
                        "title": text or f"فصل {season} قسمت {episode}",
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
                    season = season_m.group(1) if season_m else "1"
                    episode = ep_m.group(1) if ep_m else ""
                    episodes.append({
                        "season": season,
                        "episode": episode,
                        "title": full_text,
                        "onlineplay_url": href,
                    })
        else:
            # regex fallback
            pattern = re.compile(
                r'<a[^>]+href="([^"]*onlineplay[^"]*)"[^>]*>(.*?)</a>',
                re.S | re.I
            )
            for m in pattern.finditer(html):
                href = m.group(1)
                inner_html = m.group(2)
                ep_m = re.search(r'[?&]ep=([^&]+)', href)
                if ep_m:
                    full_text = unquote(ep_m.group(1)).replace("+", " ").strip()
                    full_text = re.sub(r"\s+", " ", full_text)
                else:
                    full_text = re.sub(r'<[^>]+>', '', inner_html).strip()
                    full_text = re.sub(r'\s+', ' ', full_text)
                    if not full_text:
                        continue
                season_m = re.search(r"فصل\s*(\d+)", full_text)
                ep_m2 = re.search(r"قسمت\s*(\d+)", full_text)
                season = season_m.group(1) if season_m else "1"
                episode = ep_m2.group(1) if ep_m2 else ""
                episodes.append({
                    "season": season,
                    "episode": episode,
                    "title": full_text,
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
            force: bool = False, limit: int = 0):
        """اجرای کامل همگام‌سازی."""
        print("\n" + "=" * 60)
        print("🚀 شروع همگام‌سازی آرشیو فیلم و سریال ایرانی")
        print("=" * 60)
        print(f"دیتابیس: {self.db.db_type}")
        print(f"Force: {force}")
        print(f"Limit: {limit if limit else 'نامحدود'}")

        # مرحله ۱: بارگذاری آرشیو (لیست فیلم‌ها/سریال‌ها)
        if not series_only:
            self.load_archive(MOVIE_ARCHIVE, "movie")
        if not movies_only:
            self.load_archive(SERIES_ARCHIVE, "series")

        # مرحله ۲: استخراج لینک فیلم‌ها
        if not series_only:
            print(f"\n{'='*60}")
            print("🎬 استخراج لینک فیلم‌ها")
            print("=" * 60)
            if force:
                pending = self.db.list_iranian_movies("movie")
            else:
                pending = self.db.get_pending_movies()
            if limit:
                pending = pending[:limit]
            print(f"📊 {len(pending)} فیلم برای استخراج لینک")

            for i, movie in enumerate(pending, 1):
                title = movie["title"]
                url = movie["page_url"]
                print(f"\n[{i}/{len(pending)}] {title}")
                print(f"  URL: {url}")
                direct_link = self.extract_movie_link(url)
                if direct_link:
                    self.db.upsert_iranian_movie(
                        title=title,
                        page_url=url,
                        poster=movie.get("poster", ""),
                        direct_link=direct_link,
                        content_type="movie",
                    )
                    print(f"  ✅ ذخیره شد")
                else:
                    print(f"  ⚠️ لینک پیدا نشد")
                time.sleep(1)  # مکث کوتاه بین فیلم‌ها

        # مرحله ۳: استخراج قسمت‌های سریال‌ها
        if not movies_only:
            print(f"\n{'='*60}")
            print("📺 استخراج قسمت‌های سریال‌ها")
            print("=" * 60)
            if force:
                pending_series = self.db.list_iranian_series()
            else:
                pending_series = self.db.get_pending_series()
            if limit:
                pending_series = pending_series[:limit]
            print(f"📊 {len(pending_series)} سریال برای استخراج قسمت‌ها")

            for i, series in enumerate(pending_series, 1):
                title = series["title"]
                url = series["page_url"]
                print(f"\n[{i}/{len(pending_series)}] {title}")
                print(f"  URL: {url}")
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
        description="همگام‌سازی آرشیو فیلم و سریال ایرانی با دیتابیس"
    )
    parser.add_argument("--movies-only", action="store_true",
                        help="فقط فیلم‌ها را اسکن کن")
    parser.add_argument("--series-only", action="store_true",
                        help="فقط سریال‌ها را اسکن کن")
    parser.add_argument("--force", action="store_true",
                        help="همه را دوباره اسکن کن (حتی اگر لینک دارند)")
    parser.add_argument("--limit", type=int, default=0,
                        help="فقط N آیتم اول را اسکن کن (برای تست)")
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
    sync = ArchiveSynchronizer(
        db_url=db_url,
        headless=not args.no_headless,
        wait_seconds=args.wait,
    )
    try:
        sync.run(
            movies_only=args.movies_only,
            series_only=args.series_only,
            force=args.force,
            limit=args.limit,
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
