# -*- coding: utf-8 -*-
"""
serialirany_client.py
---------------------
کلاینت سایت serialirany.com برای فیلم‌ها و سریال‌های ایرانی.

امکانات:
  • دریافت لیست فیلم‌ها از آرشیو فیلم‌ها
  • دریافت لیست سریال‌ها از آرشیو سریال‌ها
  • جستجو در هر دو آرشیو (برای inline search)
  • استخراج لینک قسمت‌ها از صفحه فیلم/سریال
  • استخراج لینک مستقیم ویدیو:
      - سریال: cdn.serialirany.com لینک‌های .mp4
      - فیلم: playmovie → publicvm.com → Base64 decode → d1.flnd.buzz
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import requests
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

log = logging.getLogger("serialirany")

BASE_URL = "https://serialirany.com"
MOVIE_ARCHIVE = f"{BASE_URL}/%d8%a2%d8%b1%d8%b4%db%8c%d9%88-%d9%81%db%8c%d9%84%d9%85-%d9%87%d8%a7/"
SERIES_ARCHIVE = f"{BASE_URL}/%d8%a2%d8%b1%d8%b4%db%8c%d9%88-%d8%b3%d8%b1%db%8c%d8%a7%d9%84%d9%87%d8%a7/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# کلمات کلیدی تبلیغاتی — فقط لینک‌های ویدیو تبلیغاتی را فیلتر می‌کند
# نکته: publicvm.com لینک پلیر است، نه لینک ویدیو — نباید فیلتر شود
AD_KEYWORDS = ['test.mp4', 'ads.mp4', 'ad.mp4', 'yektanet']
# دامنه‌های مجاز
VALID_DOMAINS = ['cdn.serialirany.com', 'd1.flnd.buzz', 'flnd.buzz', 'urliran.com', 'urliran.net']

# حداکثر تعداد نتایج جستجو
MAX_SEARCH_RESULTS = 30


def _is_ad_link(url: str) -> bool:
    """چک کردن اینکه لینک تبلیغاتی است یا نه.
    هر لینکی که شامل کلمات ads, test, publicvm, yektanet باشد نادیده می‌شود.
    """
    url_lower = url.lower()
    for kw in AD_KEYWORDS:
        if kw in url_lower:
            return True
    return False


def _is_valid_video_url(url: str) -> bool:
    """چک کردن اینکه لینک مستقیم ویدیوی معتبر است.
    """
    if _is_ad_link(url):
        return False
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    for domain in VALID_DOMAINS:
        if domain in hostname:
            return True
    # هر لینک .mp4 از سایت خودش
    if ".mp4" in url and "serialirany.com" in hostname:
        return True
    return False


def _decode_base64_url(encoded: str) -> Optional[str]:
    """رمزگشایی Base64 لینک فیلم از URL پخش آنلاین.
    لینک فیلم در پارامتر q720, q1080 یا q480 کدگذاری شده است.
    """
    try:
        # اگر لینک خودش Base64 باشه (URL-safe)
        padded = encoded + '=' * (-len(encoded) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode('utf-8', errors='ignore')
        if decoded.startswith('http'):
            return decoded
    except Exception:
        pass
    return None


class SerialiranyClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self._driver = None
        self._driver_lock = threading.Lock()
        # کش جستجوی inline (هر دو آرشیو)
        self._search_cache: List[dict] = []
        self._search_cache_time: float = 0
        self._search_cache_lock = threading.Lock()
        # فلگ تشخیص هم‌زمانی: وقتی True باشد، یعنی Selenium در حال بارگذاری است
        # و درخواست‌های جدید باید از requests-only استفاده کنند (نه Selenium)
        # این کار از deadlock جلوگیری می‌کند
        self._is_loading_archive: Dict[str, bool] = {"movie": False, "series": False}
        self._loading_lock = threading.Lock()

    # ==================== آرشیو (requests) ====================

    # ==================== جستجوی مستقیم (به‌جای آرشیو) ====================

    def search_iranian(self, query: str) -> List[dict]:
        """جستجوی مستقیم فیلم/سریال ایرانی در سایت serialirany.com.

        از قابلیت جستجوی سایت استفاده می‌کند:
        URL: https://serialirany.com/site-search?s=<query>

        خروجی: لیستی از {title, url, thumb}
        """
        from urllib.parse import quote
        if not query or len(query.strip()) < 2:
            return []

        search_url = f"{BASE_URL}/site-search?s={quote(query.strip())}"
        log.info("جستجوی ایرانی: %s", search_url[:100])

        try:
            r = self.s.get(search_url, timeout=30,
                           headers={"Referer": BASE_URL + "/"})
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("خطا در جستجوی ایرانی: %s", e)
            return []

        return self._parse_search_results(r.text)

    @staticmethod
    def _parse_search_results(html: str) -> List[dict]:
        """پارس نتایج جستجو از HTML.
        ساختار سایت serialirany.com (صفحه site-search):
        <article class="right">
            <a href="https://serialirany.com/series/..." class="yc yc-relative" title="...">
                <img src="..." />
                <h2>عنوان</h2>
            </a>
        </article>
        """
        items: List[dict] = []
        if not HAS_BS4:
            # fallback با regex
            # پیدا کردن <a> با class yc-relative و href به series/movies
            pattern = re.compile(
                r'<a[^>]*href="(https://serialirany\.com/(?:series|movies)/[^"]+)"[^>]*class="[^"]*yc-relative[^"]*"[^>]*>(.*?)</a>',
                re.S | re.I
            )
            for m in pattern.finditer(html):
                url = m.group(1)
                content = m.group(2)
                # پیدا کردن عنوان از h2/h3/h4
                title_m = re.search(r'<h[234][^>]*>([^<]+)</h[234]>', content)
                if title_m:
                    title = re.sub(r'\s+', ' ', title_m.group(1)).strip()
                else:
                    # fallback: از attribute title
                    title_attr_m = re.search(r'title="([^"]+)"', m.group(0))
                    if not title_attr_m:
                        continue
                    title = title_attr_m.group(1)
                # پیدا کردن عکس
                img_m = re.search(r'<img[^>]+src="([^"]+)"', content)
                thumb = img_m.group(1) if img_m else ""
                if title and url:
                    items.append({"title": title, "url": url, "thumb": thumb})
            # حذف تکراری‌ها
            seen = set()
            unique = []
            for item in items:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    unique.append(item)
            log.info("جستجوی ایرانی: %d نتیجه پیدا شد", len(unique))
            return unique[:MAX_SEARCH_RESULTS]

        soup = BeautifulSoup(html, "html.parser")

        # پیدا کردن article با a.yc-relative
        for article in soup.select("article"):
            a_tag = article.select_one("a.yc-relative")
            if not a_tag or not a_tag.get("href"):
                continue
            href = a_tag["href"]
            # فقط لینک‌های series یا movies
            if "/series/" not in href and "/movies/" not in href:
                continue
            # پیدا کردن عنوان از h2/h3/h4
            h_tag = (a_tag.select_one("h2") or a_tag.select_one("h3")
                     or a_tag.select_one("h4"))
            if h_tag:
                title = h_tag.get_text(strip=True)
            else:
                # fallback: از attribute title
                title = a_tag.get("title", "") or a_tag.get_text(strip=True)
            if not title:
                continue
            # پیدا کردن عکس
            img_tag = a_tag.select_one("img")
            thumb = ""
            if img_tag:
                thumb = (img_tag.get("data-src") or img_tag.get("data-lazy-src")
                         or img_tag.get("src", ""))
            items.append({"title": title, "url": href, "thumb": thumb})

        # حذف تکراری‌ها
        seen = set()
        unique = []
        for item in items:
            if item["url"] not in seen:
                seen.add(item["url"])
                unique.append(item)
        log.info("جستجوی ایرانی: %d نتیجه پیدا شد", len(unique))
        return unique[:MAX_SEARCH_RESULTS]

    def detect_content_type(self, page_url: str) -> str:
        """تشخیص اینکه صفحه فیلم است یا سریال.
        خروجی: "movie" یا "series"
        """
        try:
            r = self.s.get(page_url, timeout=30,
                           headers={"Referer": BASE_URL + "/"})
            if r.status_code != 200:
                return "movie"
            html = r.text
            if HAS_BS4:
                soup = BeautifulSoup(html, "html.parser")
                if soup.select_one(".serie-item, .serie-episodes"):
                    return "series"
                if soup.select_one("a.playmovie"):
                    return "movie"
                onlineplay_links = soup.select('a[href*="onlineplay"]')
                if len(onlineplay_links) > 1:
                    return "series"
                return "movie"
            else:
                # regex fallback
                if re.search(r'class="[^"]*serie-item', html):
                    return "series"
                if re.search(r'class="[^"]*playmovie', html):
                    return "movie"
                onlineplay_count = len(re.findall(r'onlineplay', html))
                if onlineplay_count > 1:
                    return "series"
                return "movie"
        except Exception as e:
            log.warning("خطا در تشخیص نوع محتوا: %s", e)
        return "movie"

    # ==================== آرشیو (در صورت نیاز) ====================

    def get_movie_list(self, force_refresh: bool = False) -> List[dict]:
        """لیست فیلم‌های ایرانی.
        اولویت:
        ۱. دیتابیس (اگر توسط sync_archive.py پر شده)
        ۲. کش حافظه
        ۳. سایت (requests یا Selenium)
        """
        # اولویت ۱: دیتابیس
        if SerialiranyClient._db and not force_refresh:
            try:
                db_movies = SerialiranyClient._db.list_iranian_movies("movie")
                if db_movies:
                    log.info("بارگذاری %d فیلم از دیتابیس", len(db_movies))
                    return [{
                        "title": m["title"],
                        "url": m["page_url"],
                        "thumb": m.get("poster", "") or ""
                    } for m in db_movies]
            except Exception as e:
                log.warning("خطا در خواندن فیلم‌ها از دیتابیس: %s", e)

        # اولویت ۲ و ۳: کش حافظه / سایت
        return self._get_archive_list(MOVIE_ARCHIVE, force_refresh=force_refresh)

    def get_series_list(self, force_refresh: bool = False) -> List[dict]:
        """لیست سریال‌های ایرانی.
        اولویت:
        ۱. دیتابیس (اگر توسط sync_archive.py پر شده)
        ۲. کش حافظه
        ۳. سایت (requests یا Selenium)
        """
        # اولویت ۱: دیتابیس
        if SerialiranyClient._db and not force_refresh:
            try:
                db_series = SerialiranyClient._db.list_iranian_series()
                if db_series:
                    log.info("بارگذاری %d سریال از دیتابیس", len(db_series))
                    return [{
                        "title": s["title"],
                        "url": s["page_url"],
                        "thumb": s.get("poster", "") or ""
                    } for s in db_series]
            except Exception as e:
                log.warning("خطا در خواندن سریال‌ها از دیتابیس: %s", e)

        # اولویت ۲ و ۳: کش حافظه / سایت
        return self._get_archive_list(SERIES_ARCHIVE, force_refresh=force_refresh)

    # ==================== کش آرشیو کامل (سطح کلاس) ====================
    _full_archive_cache: Dict[str, dict] = {}
    _full_archive_lock = threading.Lock()
    # ارجاع به دیتابیس برای ذخیره آرشیو (توسط bot.py تنظیم می‌شود)
    _db = None

    @classmethod
    def set_database(cls, db) -> None:
        """تنظیم ارجاع به دیتابیس برای ذخیره/بازیابی آرشیو.
        توسط bot.py در استارت فراخوانی می‌شود.
        """
        cls._db = db

    def _save_archive_to_db(self, url: str, items: List[dict]) -> None:
        """ذخیره آرشیو در دیتابیس (PostgreSQL یا SQLite).
        این کار باعث می‌شود آرشیو حتی بعد از restart ربات هم در دسترس باشد.
        """
        if not SerialiranyClient._db:
            return
        try:
            import json
            payload = json.dumps(items, ensure_ascii=False)
            # استفاده از جدول settings با کلید خاص برای آرشیو
            archive_key = f"archive_{hashlib.md5(url.encode()).hexdigest()[:10]}"
            SerialiranyClient._db.set_setting(archive_key, payload)
            log.info("آرشیو در دیتابیس ذخیره شد: %d آیتم (key=%s)", len(items), archive_key)
        except Exception as e:
            log.warning("خطا در ذخیره آرشیو در دیتابیس: %s", e)

    def _load_archive_from_db(self, url: str, max_age: int = 86400) -> Optional[List[dict]]:
        """بازیابی آرشیو از دیتابیس.
        max_age: حداکثر سن آرشیو به ثانیه (پیش‌فرض ۲۴ ساعت).
        اگر آرشیو در دیتابیس بود و تازه بود، برمی‌گرداند. در غیر این‌صورت None.
        """
        if not SerialiranyClient._db:
            return None
        try:
            import json
            archive_key = f"archive_{hashlib.md5(url.encode()).hexdigest()[:10]}"
            payload = SerialiranyClient._db.get_setting(archive_key)
            if not payload:
                return None
            items = json.loads(payload)
            log.info("آرشیو از دیتابیس بارگذاری شد: %d آیتم (key=%s)", len(items), archive_key)
            return items
        except Exception as e:
            log.warning("خطا در بازیابی آرشیو از دیتابیس: %s", e)
            return None

    def _get_archive_list_requests_only(self, url: str) -> List[dict]:
        """دریافت لیست آرشیو با requests فقط (بدون Selenium).
        سریع ولی فقط صفحه اول را می‌گیرد. وقتی Selenium در حال بارگذاری است،
        از این متد استفاده می‌شود تا کاربر بدون صبر کردن چیزی ببیند.
        """
        # بررسی کش اول
        cache_key = url
        now = time.time()
        with self._full_archive_lock:
            cached = SerialiranyClient._full_archive_cache.get(cache_key)
            if cached and (now - cached["time"]) < 3600:
                return cached["items"]

        try:
            r = self.s.get(url, timeout=20)
            r.raise_for_status()
            items = self._parse_archive_list(r.text)
            log.info("بارگذاری سریع (requests-only): %d آیتم", len(items))
            # کش نکنیم چون Selenium ممکنه هنوز در حال بارگذاری باشه
            # و آیتم‌های بیشتری پیدا کنه
            return items
        except requests.RequestException as e:
            log.error("خطا در requests-only: %s", e)
            return []

    def _get_archive_list(self, url: str, force_refresh: bool = False) -> List[dict]:
        """دریافت لیست کامل از آرشیو.
        ترتیب بررسی:
        ۱. کش حافظه (۱ ساعت)
        ۲. دیتابیس (۲۴ ساعت) — اگر تنظیم شده باشد
        ۳. requests (سریع، صفحه اول)
        ۴. Selenium (اگر USE_SELENIUM_FOR_ARCHIVE=true)
        """
        # بررسی کش حافظه
        cache_key = url
        now = time.time()
        with self._full_archive_lock:
            cached = SerialiranyClient._full_archive_cache.get(cache_key)
            if cached and not force_refresh and (now - cached["time"]) < 3600:
                log.info("استفاده از کش حافظه: %d آیتم", len(cached["items"]))
                return cached["items"]

        # بررسی دیتابیس (اگر تنظیم شده باشد)
        if not force_refresh and SerialiranyClient._db:
            db_items = self._load_archive_from_db(url, max_age=86400)
            if db_items:
                # در کش حافظه هم ذخیره کن
                with self._full_archive_lock:
                    SerialiranyClient._full_archive_cache[cache_key] = {
                        "items": db_items,
                        "time": time.time(),
                    }
                return db_items

        # بررسی اینکه آیا Selenium فعال است
        use_selenium_flag = os.environ.get("USE_SELENIUM_FOR_ARCHIVE", "false").lower() in (
            "true", "1", "yes", "on"
        )

        items: List[dict] = []

        if use_selenium_flag:
            is_movie_archive = (MOVIE_ARCHIVE in url)
            archive_type = "movie" if is_movie_archive else "series"

            with self._loading_lock:
                if self._is_loading_archive.get(archive_type, False):
                    log.info("Selenium هم‌زمان در حال بارگذاری %s است، استفاده از requests",
                             archive_type)
                    use_selenium = False
                else:
                    self._is_loading_archive[archive_type] = True
                    use_selenium = True

            if use_selenium:
                try:
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                        future = ex.submit(self._load_full_archive_with_selenium, url)
                        try:
                            items = future.result(timeout=120)
                        except concurrent.futures.TimeoutError:
                            log.warning("Selenium timeout کرد، fallback به requests")
                            items = []
                        except Exception as e:
                            log.warning("Selenium کار نکرد (%s)، fallback به requests", e)
                            items = []
                except Exception as e:
                    log.warning("خطا در ThreadPoolExecutor: %s", e)
                    items = []
                finally:
                    with self._loading_lock:
                        self._is_loading_archive[archive_type] = False

        # اگر Selenium غیرفعال است یا کم آورد، از requests استفاده کن
        if not items:
            try:
                r = self.s.get(url, timeout=30)
                r.raise_for_status()
                items = self._parse_archive_list(r.text)
                log.info("بارگذاری با requests (صفحه اول): %d آیتم", len(items))
            except requests.RequestException as e:
                log.error("خطا در دریافت آرشیو با requests: %s", e)
                items = []

        if items:
            # ذخیره در کش حافظه
            with self._full_archive_lock:
                SerialiranyClient._full_archive_cache[cache_key] = {
                    "items": items,
                    "time": time.time(),
                }
            # ذخیره در دیتابیس (برای restart ربات)
            self._save_archive_to_db(url, items)
        return items

    def _load_full_archive_with_selenium(self, archive_url: str,
                                            max_clicks: int = 200) -> List[dict]:
        """بارگذاری تمام آیتم‌های آرشیو با کلیک روی «مشاهده بیشتر».
        این متد یک درایور Selenium می‌سازد، صفحه را باز می‌کند، اسکرول می‌کند
        و دکمه «مشاهده بیشتر» را تا جایی که دیگر وجود نداشته باشد کلیک می‌کند.
        سپس همه‌ی لینک‌های فیلم/سریال را از صفحه استخراج می‌کند.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import (
            TimeoutException, NoSuchElementException, ElementNotInteractableException,
            StaleElementReferenceException, WebDriverException
        )

        driver = self._get_driver()
        if not driver:
            raise RuntimeError("Selenium WebDriver در دسترس نیست")

        log.info("بارگذاری کامل آرشیو با Selenium: %s", archive_url)
        with self._driver_lock:
            try:
                driver.get(archive_url)
                # صبر برای بارگذاری اولیه
                time.sleep(3)

                clicks = 0
                no_progress_count = 0
                last_count = 0

                while clicks < max_clicks:
                    # اسکرول به پایین برای ظاهر شدن دکمه «مشاهده بیشتر»
                    try:
                        driver.execute_script(
                            "window.scrollTo(0, document.body.scrollHeight);"
                        )
                        time.sleep(1.5)
                    except WebDriverException:
                        break

                    # تلاش برای پیدا کردن دکمه «مشاهده بیشتر» با چندین سلکتور
                    load_more_btn = None
                    selectors = [
                        # سلکتورهای رایج در سایت‌های وردپرسی/المنتور
                        "a.elementor-button.elementor-size-sm",
                        "a.elementor-button",
                        "button.elementor-button",
                        "a.yc-load-more",
                        "a.load-more",
                        "button.load-more",
                        "a:contains('مشاهده بیشتر')",
                        "button:contains('مشاهده بیشتر')",
                    ]
                    for sel in selectors:
                        try:
                            # استفاده از XPath برای تطبیق متن فارسی
                            if ":contains(" in sel:
                                text_m = re.search(r":contains\('([^']+)'\)", sel)
                                if text_m:
                                    xpath = (
                                        f"//a[contains(.,'{text_m.group(1)}')]"
                                        f" | //button[contains(.,'{text_m.group(1)}')]"
                                    )
                                    try:
                                        els = driver.find_elements(By.XPATH, xpath)
                                        for el in els:
                                            if el.is_displayed() and el.is_enabled():
                                                load_more_btn = el
                                                break
                                    except Exception:
                                        pass
                            else:
                                try:
                                    els = driver.find_elements(By.CSS_SELECTOR, sel)
                                    for el in els:
                                        txt = (el.text or "").strip()
                                        if el.is_displayed() and el.is_enabled():
                                            # فقط دکمه‌هایی که «مشاهده بیشتر» دارند
                                            if "مشاهده بیشتر" in txt or "بیشتر" in txt or not txt:
                                                load_more_btn = el
                                                break
                                except Exception:
                                    pass
                            if load_more_btn:
                                break
                        except Exception:
                            continue

                    if not load_more_btn:
                        log.info("دکمه «مشاهده بیشتر» پیدا نشد (پایان لیست)")
                        break

                    # شمارش آیتم‌های فعلی برای تشخیص پیشرفت
                    current_items = driver.find_elements(By.CSS_SELECTOR,
                                                          "a.yc-relative, .elementor-post__card a")
                    current_count = len(current_items)

                    # کلیک روی دکمه (با JavaScript برای جلوگیری از خطاهای کلیک)
                    try:
                        # اسکرول به دکمه
                        driver.execute_script(
                            "arguments[0].scrollIntoView({block: 'center'});",
                            load_more_btn
                        )
                        time.sleep(0.5)
                        driver.execute_script("arguments[0].click();", load_more_btn)
                        clicks += 1
                        # صبر برای لود شدن آیتم‌های جدید (AJAX)
                        time.sleep(2.5)

                        # بررسی پیشرفت
                        new_items = driver.find_elements(
                            By.CSS_SELECTOR,
                            "a.yc-relative, .elementor-post__card a"
                        )
                        new_count = len(new_items)
                        if new_count == last_count:
                            no_progress_count += 1
                            # اگر ۳ بار پشت سر هم پیشرفت نبود، یعنی به انتهای لیست رسیده‌ایم
                            if no_progress_count >= 3:
                                log.info("پیشرفت متوقف شد (احتمالاً انتهای لیست). "
                                         "تعداد کلیک: %d، تعداد آیتم: %d",
                                         clicks, new_count)
                                break
                        else:
                            no_progress_count = 0
                        last_count = new_count

                        if clicks % 10 == 0:
                            log.info("پیشرفت آرشیو: %d کلیک، %d آیتم", clicks, new_count)
                    except (StaleElementReferenceException,
                            ElementNotInteractableException,
                            WebDriverException) as e:
                        log.warning("خطا هنگام کلیک روی «مشاهده بیشتر»: %s", e)
                        break

                log.info("بارگذاری کامل شد: %d کلیک، %d آیتم", clicks, last_count)

                # حالا که تمام آیتم‌ها بارگذاری شدند، HTML کامل را بگیر
                page_html = driver.page_source
            finally:
                # درایور را نگه نمی‌داریم، چون حافظه اشغال می‌کند
                # (درایور فقط برای بارگذاری آرشیو ساخته شد)
                pass

        # پارس HTML کامل
        items = self._parse_archive_list(page_html)

        # حذف آیتم‌های تکراری بر اساس URL
        seen_urls = set()
        unique_items = []
        for item in items:
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique_items.append(item)

        log.info("آیتم‌های نهایی آرشیو (یکتا): %d", len(unique_items))
        return unique_items

    @staticmethod
    def _parse_archive_list(html: str) -> List[dict]:
        """پارسر لیست از HTML صفحه آرشیو.
        خروجی: [{"title": "...", "url": "...", "thumb": "..."}, ...]
        """
        items = []
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")

            # روش ۱: Elementor (ساختار فعلی سایت)
            for card in soup.select(".elementor-post__card"):
                a_tag = card.select_one("a")
                if not a_tag or not a_tag.get("href"):
                    continue
                link = a_tag["href"]
                # عنوان
                title_el = (card.select_one(".elementor-post__title a")
                            or card.select_one(".elementor-post__title")
                            or card.select_one("h2") or card.select_one("h3")
                            or a_tag)
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    title = a_tag.get("title", "") or a_tag.get_text(strip=True)
                # عکس
                img = (card.select_one("img")
                       or a_tag.select_one("img"))
                thumb = ""
                if img:
                    thumb = (img.get("data-src")
                             or img.get("data-lazy-src")
                             or img.get("src", ""))
                if title and link:
                    items.append({"title": title, "url": link, "thumb": thumb})

            # روش ۲: تگ‌های a.yc-relative (ساختار قدیمی)
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
                        thumb = (img.get("data-src")
                                 or img.get("data-lazy-src")
                                 or img.get("src", ""))
                    if title and link:
                        items.append({"title": title, "url": link, "thumb": thumb})

            # روش ۳: article.yc
            if not items:
                articles = soup.select("article.yc")
                for article in articles:
                    link_tag = article.select_one("a")
                    title_tag = article.select_one("h2") or article.select_one("h3")
                    if not title_tag:
                        title_tag = article.select_one("a")
                    img_tag = article.select_one("img")
                    if link_tag and title_tag:
                        title = title_tag.get_text(strip=True)
                        url = link_tag.get("href", "")
                        thumb = ""
                        if img_tag:
                            thumb = (img_tag.get("data-src")
                                     or img_tag.get("data-lazy-src")
                                     or img_tag.get("src", ""))
                        if title and url:
                            items.append({"title": title, "url": url, "thumb": thumb})

            # روش ۴: هر a که h2/h3 دارد
            if not items:
                for a_tag in soup.select("a"):
                    h_tag = a_tag.select_one("h2, h3")
                    if h_tag:
                        title = h_tag.get_text(strip=True)
                        url = a_tag.get("href", "")
                        img_tag = a_tag.select_one("img")
                        thumb = ""
                        if img_tag:
                            thumb = (img_tag.get("data-src")
                                     or img_tag.get("data-lazy-src")
                                     or img_tag.get("src", ""))
                        if title and url:
                            items.append({"title": title, "url": url, "thumb": thumb})
        else:
            # بدون BeautifulSoup — regex
            # Elementor cards
            card_pattern = re.compile(
                r'<article[^>]*class="[^"]*elementor-post__card[^"]*"[^>]*>.*?'
                r'<a[^>]+href="([^"]+)"[^>]*>.*?'
                r'<(?:h[23]|[^>]*class="[^"]*elementor-post__title[^"]*")[^>]*>([^<]+)<',
                re.S)
            for m in card_pattern.finditer(html):
                url = m.group(1)
                title = re.sub(r'\s+', ' ', m.group(2)).strip()
                img_m = re.search(r'<img[^>]+(?:src|data-src)="([^"]+)"',
                                 html[html.index(m.group(0)):html.index(m.group(0))+2000] if m.group(0) in html else "")
                if title and url:
                    items.append({
                        "title": title,
                        "url": url,
                        "thumb": img_m.group(1) if img_m else ""
                    })

            if not items:
                # yc-relative
                yc_pattern = re.compile(
                    r'<a[^>]*class="[^"]*yc-relative[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                    re.S)
                for m in yc_pattern.finditer(html):
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
        return items

    # ==================== جستجو (برای inline) ====================

    def search(self, query: str) -> List[dict]:
        """جستجو در هر دو آرشیو (فیلم + سریال).
        خروجی: حداکثر MAX_SEARCH_RESULTS مورد منطبق.
        """
        query = query.strip().lower()
        if len(query) < 2:
            return []
        all_items = self._get_all_cached_items()
        results = []
        for item in all_items:
            if query in item.get("title", "").lower():
                results.append(item)
                if len(results) >= MAX_SEARCH_RESULTS:
                    break
        return results

    def _get_all_cached_items(self) -> List[dict]:
        """دریافت هر دو آرشیو با کش (TTL = ۱ ساعت).
        """
        now = time.time()
        with self._search_cache_lock:
            if self._search_cache and (now - self._search_cache_time) < 3600:
                return self._search_cache
        # خارج از قفل بگیر
        movies = self.get_movie_list()
        series = self.get_series_list()
        with self._search_cache_lock:
            self._search_cache = movies + series
            self._search_cache_time = time.time()
        log.info("آرشیو ایرانی بارگذاری شد: %d فیلم + %d سریال",
                 len(movies), len(series))
        return self._search_cache

    @staticmethod
    def make_cache_key(url: str) -> str:
        """ساخت کلید کوتاه برای کش inline.
        """
        return hashlib.md5(url.encode()).hexdigest()[:10]

    # ==================== استخراج اطلاعات صفحه ====================

    def get_page_info(self, page_url: str) -> dict:
        """استخراج اطلاعات اصلی از صفحه فیلم/سریال.
        
062eروجی: {
            "title": "...",       # از h1 یا .entry-title
            "thumb": "...",      # از .post-thumbnail img
            "images": [...],      # تمام عکس‌های .entry-content
            "is_series": bool,    # آیا سریال است؟
            "cdn_links": [...],    # لینک‌های مستقیم CDN (سریال)
            "playmovie_url": "..." # لینک دکمه پخش آنلاین (فیلم)
        }
        """
        page_url = self._normalize_url(page_url)
        result = {
            "title": "",
            "thumb": "",
            "images": [],
            "is_series": False,
            "cdn_links": [],
            "playmovie_url": "",
        }
        try:
            r = self.s.get(page_url, timeout=30,
                           headers={"Referer": BASE_URL + "/"})
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("خطا در دریافت صفحه: %s", e)
            return result

        html = r.text

        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")

            # عنوان از h1 یا .entry-title
            title_tag = (soup.select_one("h1")
                         or soup.select_one(".entry-title")
                         or soup.select_one("h2.entry-title"))
            if title_tag:
                result["title"] = title_tag.get_text(strip=True)

            # عکس اصلی از .post-thumbnail img
            thumb_tag = soup.select_one(".post-thumbnail img")
            if not thumb_tag:
                thumb_tag = soup.select_one("article img")
            if thumb_tag:
                result["thumb"] = (thumb_tag.get("data-src")
                                    or thumb_tag.get("data-lazy-src")
                                    or thumb_tag.get("src", ""))

            # تمام عکس‌های .entry-content
            for img in soup.select(".entry-content img"):
                src = (img.get("data-src")
                       or img.get("data-lazy-src")
                       or img.get("src", ""))
                if src:
                    result["images"].append(src)

            # بررسی لینک‌های مستقیم CDN (سریال)
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                if ("cdn.serialirany.com" in href
                        and href.endswith(".mp4")
                        and not _is_ad_link(href)):
                    text = a_tag.get_text(strip=True)
                    result["cdn_links"].append({
                        "title": text or href.split("/")[-1],
                        "url": href
                    })

            if result["cdn_links"]:
                result["is_series"] = True

            # بررسی دکمه پخش آنلاین (فیلم)
            play_btn = soup.select_one("a.playmovie")
            if play_btn:
                result["playmovie_url"] = play_btn.get("href", "")
        else:
            # بدون BeautifulSoup — regex
            title_m = re.search(r'<h1[^>]*>([^<]+)<', html)
            if not title_m:
                title_m = re.search(r'class="entry-title"[^>]*>([^<]+)<', html)
            if title_m:
                result["title"] = re.sub(r'\s+', ' ', title_m.group(1)).strip()

            thumb_m = re.search(
                r'class="post-thumbnail"[^>]*>.*?<img[^>]+(?:src|data-src)="([^"]+)"',
                html, re.S)
            if not thumb_m:
                thumb_m = re.search(r'<article[^>]*>.*?<img[^>]+src="([^"]+)"', html, re.S)
            if thumb_m:
                result["thumb"] = thumb_m.group(1)

            # CDN links
            cdn_pattern = re.compile(
                r'<a[^>]+href="(https?://cdn\.serialirany\.com/[^"]+\.mp4)"[^>]*>(.*?)</a>',
                re.S)
            for m in cdn_pattern.finditer(html):
                href = m.group(1)
                if not _is_ad_link(href):
                    text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
                    result["cdn_links"].append({
                        "title": text or href.split("/")[-1],
                        "url": href
                    })
            if result["cdn_links"]:
                result["is_series"] = True

            # playmovie button
            play_m = re.search(r'<a[^>]+class="playmovie"[^>]+href="([^"]+)"', html)
            if play_m:
                result["playmovie_url"] = play_m.group(1)

        return result

    # ==================== استخراج لینک قسمت‌ها (requests) ====================

    def get_episode_links(self, page_url: str) -> List[dict]:
        """لیست قسمت‌های یک صفحه فیلم/سریال.
        برای سریال: لینک‌های cdn.serialirany.com که .mp4 هستند.
        برای فیلم: لینک‌هایی که دارای کلمات کلیدی هستند.
        """
        page_url = self._normalize_url(page_url)
        try:
            r = self.s.get(page_url, timeout=30,
                           headers={"Referer": BASE_URL + "/"})
            r.raise_for_status()
            return self._parse_episode_links(r.text, page_url)
        except requests.RequestException as e:
            log.error("خطا در دریافت قسمت‌ها: %s", e)
            return []

    def get_series_structure(self, page_url: str) -> dict:
        """تشخیص ساختار فصل‌بندی سریال ایرانی.

        اولویت:
        ۱. دیتابیس (اگر توسط sync_archive.py پر شده — سریع‌ترین)
        ۲. سایت (requests)

        خروجی:
        {
            "has_seasons": True,
            "seasons": [
                {"title": "فصل 1", "value": "1", "episodes": [{"title": "...", "url": "..."}, ...]},
                {"title": "فصل 2", "value": "2", "episodes": [...]},
            ]
        }
        یا:
        {"has_seasons": False, "episodes": [{"title": "...", "url": "..."}, ...]}
        """
        page_url = self._normalize_url(page_url)

        # اولویت ۱: دیتابیس
        if SerialiranyClient._db:
            try:
                db_episodes = SerialiranyClient._db.list_iranian_episodes(page_url)
                if db_episodes:
                    # گروه‌بندی بر اساس فصل
                    seasons: Dict[str, List[dict]] = {}
                    for ep in db_episodes:
                        s_num = ep["season"] or "1"
                        if s_num not in seasons:
                            seasons[s_num] = []
                        seasons[s_num].append({
                            "episode": ep["episode"] or "",
                            "title": ep["title"] or "",
                            "url": ep["onlineplay_url"] or "",
                        })
                    if seasons:
                        result_seasons = []
                        for s_num in sorted(seasons.keys(),
                                            key=lambda x: int(x) if x.isdigit() else 0):
                            eps = seasons[s_num]
                            eps_sorted = sorted(eps, key=lambda x: int(x["episode"])
                                                 if x["episode"].isdigit() else 0)
                            result_seasons.append({
                                "title": f"فصل {s_num}",
                                "value": s_num,
                                "episodes": eps_sorted,
                            })
                        log.info("بارگذاری ساختار سریال از دیتابیس: %d فصل، %d قسمت",
                                 len(result_seasons), sum(len(s["episodes"]) for s in result_seasons))
                        return {"has_seasons": True, "seasons": result_seasons}
            except Exception as e:
                log.warning("خطا در خواندن قسمت‌ها از دیتابیس: %s", e)

        # اولویت ۲: سایت (requests)
        try:
            r = self.s.get(page_url, timeout=30,
                           headers={"Referer": BASE_URL + "/"})
            r.raise_for_status()
        except requests.RequestException as e:
            log.error("خطا در دریافت ساختار سریال: %s", e)
            return {"has_seasons": False, "episodes": []}

        html = r.text
        if HAS_BS4:
            return self._parse_series_structure_bs4(html)
        else:
            return self._parse_series_structure_regex(html)

    @staticmethod
    def _parse_series_structure_bs4(html: str) -> dict:
        """تشخیص فصل‌بندی با BeautifulSoup.
        رویکرد جدید: فقط لینک‌های واقعی پخش آنلاین (onlineplay یا cdn.serialirany)
        را از بخش محتوای اصلی استخراج می‌کند. این کار از گرفتن لینک‌های منو،
        فوتر و لینک‌های پیشنهادی جلوگیری می‌کند.
        نام قسمت از پارامتر ep در URL استخراج می‌شود (با URL-decode).
        اگر نام قسمت شامل «قسمت X» نباشد، یک نام تمیز «قسمت N» ساخته می‌شود.
        """
        from urllib.parse import unquote, urlparse, parse_qs

        soup = BeautifulSoup(html, "html.parser")

        # پیدا کردن بخش محتوای اصلی — اولویت با .serie-videos (ساختار اختصاصی سریال)
        # این فقط لینک‌های قسمت‌های واقعی را دارد (نه منو/فوتر)
        content_area = (soup.select_one(".serie-videos")
                        or soup.select_one(".serie-episodes")
                        or soup.select_one(".entry-content")
                        or soup.select_one(".single-main")
                        or soup.select_one(".elementor-widget-container")
                        or soup.select_one("main")
                        or soup.select_one("article")
                        or soup)
        if content_area is None:
            content_area = soup

        # حذف بخش‌های پیشنهادی و تبلیغاتی از content_area
        # این کار از گرفتن لینک‌های «همچنین تماشا کنید» جلوگیری می‌کند
        for selector in [".suggestions", ".suggested", ".recommend", ".related",
                          ".also-watch", ".sidebar", ".ads", ".advertisement",
                          ".elementor-element-populated:not(:has(.serie-item))"]:
            for el in content_area.select(selector):
                el.decompose()
        # حذف تگ‌های nav و header و footer از content_area
        for tag_name in ["nav", "header", "footer"]:
            for el in content_area.find_all(tag_name):
                el.decompose()

        # روش ۱ (اصلی): پیدا کردن li.yc-relative (ساختار اختصاصی سریال serialirany.com)
        # ساختار: <li class="right yc-relative"><a href="...onlineplay...">...</a><div class="serie-title"><h2>عنوان</h2></div></li>
        serie_items = content_area.select("li.yc-relative")
        if not serie_items:
            # fallback به .serie-item و کلاس‌های مشابه
            for cls in ["serie-item", "serie_item", "serie-title", "episode-item",
                        "serie-episodes-item"]:
                serie_items = content_area.select(f".{cls}")
                if serie_items:
                    break

        # روش ۲ (fallback): پیدا کردن لینک‌های onlineplay و cdn مستقیم
        onlineplay_links = []
        cdn_links = []
        for a in content_area.find_all("a", href=True):
            href = a["href"]
            if not href or href in ("#", "/"):
                continue
            if "onlineplay" in href.lower():
                onlineplay_links.append(a)
            elif ("cdn.serialirany.com" in href
                    and href.lower().endswith(".mp4")
                    and not _is_ad_link(href)):
                cdn_links.append(a)

        # استخراج اطلاعات از .serie-item (اگر وجود دارد)
        seasons: Dict[str, List[dict]] = {}
        ungrouped_eps: List[dict] = []
        has_episode_info = False
        # برای شماره‌گذاری قسمت‌ها در هر فصل (اگر نام تمیز نداشت)
        ep_counter: Dict[str, int] = {}

        def _clean_episode_title(text: str, season_num: str = "1",
                                   found_ep_num: str = "") -> str:
            """ساخت نام تمیز برای قسمت.
            اگر found_ep_num داده شود (از regex)، از آن استفاده می‌کند.
            در غیر این‌صورت، اگر متن شامل «قسمت X» است، همان را برمی‌گرداند.
            در غیر این‌صورت «قسمت N» می‌سازد (N = شماره بعدی در این فصل).
            """
            if found_ep_num:
                return f"قسمت {found_ep_num}"
            # تلاش برای پیدا کردن «قسمت X» در متن
            m = re.search(r"قسمت\s*(\d+)", text)
            if m:
                return f"قسمت {m.group(1)}"
            # اگر نبود، شماره‌گذاری خودکار
            ep_counter[season_num] = ep_counter.get(season_num, 0) + 1
            return f"قسمت {ep_counter[season_num]}"

        if serie_items:
            # روش ۱: استفاده از .serie-item
            for item in serie_items:
                # متن آیتم (با جداکننده فاصله)
                text = item.get_text(" ", strip=True)
                text = re.sub(r"\s+", " ", text).strip()
                # پیدا کردن لینک پخش آنلاین داخل آیتم
                a_tag = item.select_one('a[href*="onlineplay"]')
                if not a_tag:
                    a_tag = item.select_one("a[href]")
                if not a_tag or not a_tag.get("href"):
                    continue
                href = a_tag["href"]

                # تشخیص شماره فصل و قسمت از متن آیتم
                season_m = re.search(r"فصل\s*(\d+)", text)
                ep_m = re.search(r"قسمت\s*(\d+)", text)
                # اگر متن شامل فصل/قسمت نبود، تلاش کن از پارامتر ep استخراج کنی
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
                # مقداردهی اولیه ep_counter برای این فصل
                if s_num not in seasons:
                    seasons[s_num] = []
                    ep_counter[s_num] = 0

                # ساخت نام تمیز برای قسمت
                found_ep_num = ep_m.group(1) if ep_m else ""
                if found_ep_num:
                    clean_title = f"قسمت {found_ep_num}"
                    e_num = found_ep_num
                    # اطمینان از اینکه ep_counter حداقل به این شماره رسیده
                    if ep_counter.get(s_num, 0) < int(e_num):
                        ep_counter[s_num] = int(e_num)
                else:
                    # شماره‌گذاری خودکار
                    ep_counter[s_num] = ep_counter.get(s_num, 0) + 1
                    e_num = str(ep_counter[s_num])
                    clean_title = f"قسمت {e_num}"

                has_episode_info = True
                # جلوگیری از تکرار قسمت‌ها (بر اساس شماره قسمت)
                if not any(x["episode"] == e_num for x in seasons[s_num]):
                    seasons[s_num].append({
                        "episode": e_num,
                        "title": clean_title,
                        "url": href,
                    })

        elif onlineplay_links:
            # روش ۲: استفاده از لینک‌های onlineplay
            for a in onlineplay_links:
                href = a["href"]
                # استخراج پارامتر ep از URL
                try:
                    parsed = urlparse(href)
                    params = parse_qs(parsed.query)
                    ep_value = params.get("ep", [None])[0]
                except Exception:
                    ep_value = None

                if not ep_value:
                    m = re.search(r'[?&]ep=([^&]+)', href)
                    if m:
                        ep_value = m.group(1)

                if ep_value:
                    full_text = unquote(ep_value)
                    full_text = full_text.replace("+", " ").strip()
                    full_text = re.sub(r"\s+", " ", full_text)
                else:
                    full_text = a.get_text(" ", strip=True)
                    full_text = re.sub(r"\s+", " ", full_text).strip()
                    if not full_text:
                        continue

                season_m = re.search(r"فصل\s*(\d+)", full_text)
                ep_m = re.search(r"قسمت\s*(\d+)", full_text)

                s_num = season_m.group(1) if season_m else "1"
                if s_num not in seasons:
                    seasons[s_num] = []
                    ep_counter[s_num] = 0

                # ساخت نام تمیز
                found_ep_num = ep_m.group(1) if ep_m else ""
                if found_ep_num:
                    clean_title = f"قسمت {found_ep_num}"
                    e_num = found_ep_num
                    if ep_counter.get(s_num, 0) < int(e_num):
                        ep_counter[s_num] = int(e_num)
                else:
                    ep_counter[s_num] = ep_counter.get(s_num, 0) + 1
                    e_num = str(ep_counter[s_num])
                    clean_title = f"قسمت {e_num}"

                has_episode_info = True
                if not any(x["episode"] == e_num for x in seasons[s_num]):
                    seasons[s_num].append({
                        "episode": e_num,
                        "title": clean_title,
                        "url": href,
                    })

        # افزودن لینک‌های CDN مستقیم (اگر هیچ onlineplay پیدا نشد)
        if not serie_items and not onlineplay_links and cdn_links:
            for a in cdn_links:
                href = a["href"]
                text = a.get_text(" ", strip=True) or href.split("/")[-1]
                text = re.sub(r"\s+", " ", text).strip()
                ep_m = re.search(r"قسمت\s*(\d+)", text) or re.search(r"e(\d+)", href, re.I)
                season_m = re.search(r"فصل\s*(\d+)", text) or re.search(r"s(\d+)", href, re.I)
                s_num = season_m.group(1) if season_m else "1"
                if s_num not in seasons:
                    seasons[s_num] = []
                    ep_counter[s_num] = 0

                found_ep_num = ep_m.group(1) if ep_m else ""
                if found_ep_num:
                    clean_title = f"قسمت {found_ep_num}"
                    e_num = found_ep_num
                    if ep_counter.get(s_num, 0) < int(e_num):
                        ep_counter[s_num] = int(e_num)
                else:
                    ep_counter[s_num] = ep_counter.get(s_num, 0) + 1
                    e_num = str(ep_counter[s_num])
                    clean_title = f"قسمت {e_num}"

                has_episode_info = True
                if not any(x["episode"] == e_num for x in seasons[s_num]):
                    seasons[s_num].append({
                        "episode": e_num,
                        "title": clean_title,
                        "url": href,
                    })

        # اگر اطلاعات فصل‌بندی پیدا شد، خروجی ساختار فصل‌بندی را برمی‌گردانیم
        if has_episode_info and seasons:
            result_seasons = []
            for s_num in sorted(seasons.keys(),
                                key=lambda x: int(x) if x.isdigit() else 0):
                eps = seasons[s_num]
                eps_sorted = sorted(eps, key=lambda x: int(x["episode"])
                                     if x["episode"].isdigit() else 0)
                result_seasons.append({
                    "title": f"فصل {s_num}",
                    "value": s_num,
                    "episodes": eps_sorted,
                })
            log.info("ساختار سریال: %d فصل، مجموعاً %d قسمت",
                     len(result_seasons), sum(len(s["episodes"]) for s in result_seasons))
            return {"has_seasons": True, "seasons": result_seasons}

        # اگر هیچ فصل‌بندی پیدا نشد، لیست ساده برمی‌گردد
        all_eps = ungrouped_eps
        if not all_eps:
            all_eps = SerialiranyClient._parse_episode_links_static(html)

        return {"has_seasons": False, "episodes": all_eps}

    @staticmethod
    def _parse_series_structure_regex(html: str) -> dict:
        """تشخیص فصل‌بندی با regex (بدون BeautifulSoup).
        مشابه نسخه‌ی bs4 اما با regex.
        """
        from urllib.parse import unquote

        # پیدا کردن لینک‌های onlineplay
        onlineplay_pattern = re.compile(
            r'<a[^>]+href="([^"]*onlineplay[^"]*)"[^>]*>(.*?)</a>',
            re.S | re.I
        )
        seasons: Dict[str, List[dict]] = {}
        ungrouped_eps: List[dict] = []
        has_episode_info = False

        for m in onlineplay_pattern.finditer(html):
            href = m.group(1)
            inner_html = m.group(2)
            # استخراج پارامتر ep
            ep_m = re.search(r'[?&]ep=([^&]+)', href)
            if ep_m:
                full_text = unquote(ep_m.group(1)).replace("+", " ").strip()
                full_text = re.sub(r"\s+", " ", full_text)
            else:
                # پاکسازی HTML از inner
                full_text = re.sub(r'<[^>]+>', '', inner_html).strip()
                full_text = re.sub(r'\s+', ' ', full_text)
                if not full_text:
                    continue

            season_m = re.search(r"فصل\s*(\d+)", full_text)
            ep_m2 = re.search(r"قسمت\s*(\d+)", full_text)
            if season_m and ep_m2:
                has_episode_info = True
                s_num = season_m.group(1)
                e_num = ep_m2.group(1)
                if s_num not in seasons:
                    seasons[s_num] = []
                if not any(x["episode"] == e_num for x in seasons[s_num]):
                    seasons[s_num].append({
                        "episode": e_num,
                        "title": full_text,
                        "url": href,
                    })
            else:
                ungrouped_eps.append({"title": full_text, "url": href})

        if has_episode_info and seasons:
            result_seasons = []
            for s_num in sorted(seasons.keys(),
                                key=lambda x: int(x) if x.isdigit() else 0):
                eps = sorted(seasons[s_num],
                              key=lambda x: int(x["episode"])
                              if x["episode"].isdigit() else 0)
                result_seasons.append({
                    "title": f"فصل {s_num}",
                    "value": s_num,
                    "episodes": eps,
                })
            return {"has_seasons": True, "seasons": result_seasons}

        # fallback: لینک‌های CDN مستقیم
        all_eps = ungrouped_eps or SerialiranyClient._parse_episode_links_static(html)
        return {"has_seasons": False, "episodes": all_eps}

    @staticmethod
    def _parse_episode_links_static(html: str) -> List[dict]:
        """پارسر لینک قسمت‌ها (استاتیک - بدون نیاز به self)."""
        links = []
        pattern = re.compile(
            r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            re.S)
        for m in pattern.finditer(html):
            href = m.group(1)
            text = re.sub(r'<[^>]+>', '', m.group(2)).strip()
            text = re.sub(r'\s+', ' ', text).strip()
            if not text or not href or href in ("#", "/"):
                continue
            if href.startswith("javascript"):
                continue
            if ("cdn.serialirany.com" in href
                    and href.endswith(".mp4")
                    and not _is_ad_link(href)):
                links.append({"title": text or href.split("/")[-1], "url": href})
                continue
            if href.startswith("http") and "serialirany.com" not in href:
                continue
            if ".mp4" in href and not _is_ad_link(href):
                links.append({"title": text or href.split("/")[-1], "url": href})
                continue
            lower_text = text.lower()
            if any(kw in lower_text for kw in
                    ["قسمت", "فصل", "پخش", "دانلود", "سریال", "فیلم"]):
                links.append({"title": text, "url": href})
        return links

    @staticmethod
    def _parse_episode_links(html: str, page_url: str = "") -> List[dict]:
        """پارسر لینک قسمت‌ها از صفحه (Instance method - wrapper).
        سریال: لینک‌های cdn.serialirany.com مستقیم .mp4
        فیلم: لینک‌های دارای کلمات کلیدی (قسمت, فصل, پخش, دانلود)
        """
        if HAS_BS4:
            soup = BeautifulSoup(html, "html.parser")
            links = []
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"]
                text = a_tag.get_text(strip=True)
                if not text or not href or href in ("#", "/"):
                    continue
                if href.startswith("javascript"):
                    continue
                if ("cdn.serialirany.com" in href
                        and href.endswith(".mp4")
                        and not _is_ad_link(href)):
                    links.append({"title": text or href.split("/")[-1], "url": href})
                    continue
                if href.startswith("http") and "serialirany.com" not in href:
                    continue
                if ".mp4" in href and not _is_ad_link(href):
                    links.append({"title": text or href.split("/")[-1], "url": href})
                    continue
                lower_text = text.lower()
                if any(kw in lower_text for kw in
                       ["قسمت", "فصل", "پخش", "دانلود", "سریال", "فیلم"]):
                    links.append({"title": text, "url": href})
            return links
        else:
            return SerialiranyClient._parse_episode_links_static(html)

    # ==================== استخراج لینک ویدیو واقعی ====================

    def get_video_link(self, page_url: str, timeout: int = 25) -> Optional[str]:
        """استخراج لینک مستقیم ویدیو.

        اولویت:
        ۱. دیتابیس (اگر توسط sync_archive.py ذخیره شده — سریع‌ترین)
        ۲. اگر URL یک لینک onlineplay است → decode q720/q1080/q480
        ۳. اگر صفحه سریال است (لینک cdn.serialirany.com): مستقیم برمی‌گرداند
        ۴. اگر فیلم است (دکمه playmovie): decode Base64 از URL
        ۵. fallback — Selenium (اگر نصب باشد)
        """
        page_url = self._normalize_url(page_url)

        # اولویت ۱: دیتابیس (فقط برای فیلم‌ها)
        if SerialiranyClient._db:
            try:
                db_movie = SerialiranyClient._db.get_iranian_movie(page_url)
                if db_movie and db_movie.get("direct_link"):
                    log.info("لینک مستقیم از دیتابیس: %s...",
                             db_movie["direct_link"][:100])
                    return db_movie["direct_link"]
            except Exception as e:
                log.warning("خطا در خواندن لینک فیلم از دیتابیس: %s", e)

        # مرحله ۰: اگر URL خودش onlineplay است → مستقیم decode کن
        if "onlineplay" in page_url.lower():
            direct = self._decode_onlineplay_url(page_url)
            if direct:
                log.info("لینک مستقیم از onlineplay URL: %s...", direct[:100])
                return direct

        # مرحله ۱: اطلاعات صفحه را بگیر
        info = self.get_page_info(page_url)

        # مرحله ۲: اگر سریال است و لینک CDN دارد
        if info["cdn_links"]:
            link = info["cdn_links"][0]["url"]
            log.info("لینک مستقیم سریال: %s...", link[:100])
            return link

        # مرحله ۳: اگر فیلم است و دکمه playmovie دارد
        if info["playmovie_url"]:
            return self._extract_movie_link_from_player(info["playmovie_url"])

        # مرحله ۴: fallback — Selenium
        log.info("روش مستقیم کار نکرد، تلاش با Selenium...")
        return self._get_video_link_selenium(page_url, timeout)

    def _decode_onlineplay_url(self, online_url: str) -> Optional[str]:
        """رمزگشایی لینک مستقیم از URL پخش آنلاین سریال.
        URLها معمولا شامل پارامترهای q720, q1080, q480 هستند که base64-encoded هستند.
        مثال: ?q720=aHR0cHM6Ly9jZG4uc2VyaWFsaXJhbnkuY29tL2VwMS5tcDQ=

        فقط لینک‌های معتبر (نه test.mp4, ads.mp4, ad.mp4) برمی‌گرداند.
        """
        try:
            parsed = urlparse(online_url)
            params = parse_qs(parsed.query)
            # اولویت: q720 > q1080 > q480
            for q_key in ["q720", "q1080", "q480"]:
                encoded = params.get(q_key, [None])[0]
                if encoded:
                    decoded = _decode_base64_url(encoded)
                    if decoded:
                        # فیلتر لینک‌های تبلیغاتی
                        if _is_ad_link(decoded):
                            log.warning("لینک تبلیغاتی فیلتر شد: %s", decoded[:100])
                            continue
                        log.info("onlineplay decode شد (%s): %s", q_key, decoded[:100])
                        return decoded
            # fallback: هر پارامتری که با q شروع می‌شود
            for key, val in params.items():
                if key.startswith("q") and val and key not in ("q720", "q1080", "q480"):
                    decoded = _decode_base64_url(val[0])
                    if decoded and not _is_ad_link(decoded):
                        log.info("onlineplay decode شد (%s): %s", key, decoded[:100])
                        return decoded
        except Exception as e:
            log.warning("خطا در decode onlineplay URL: %s", e)
        return None

    def _extract_movie_link_from_player(self, play_url: str) -> Optional[str]:
        """
0627ستخراج لینک فیلم از صفحه پخش آنلاین (publicvm.com).
        بدون نیاز به Selenium! مستقیما از URL پارامتر q720/q1080 را
        Base64 decode میکند.
        """
        try:
            # محافظت از لینک‌های نامعتبر
            if "publicvm.com" not in play_url and "serialirany.com" not in play_url:
                log.warning("لینک playmovie نامعتبر: %s", play_url[:100])
                return None

            # تحلیل URL
            parsed = urlparse(play_url)
            params = parse_qs(parsed.query)

            # اولویت: q720 > q1080 > q480
            encoded = (params.get("q720", [None])[0]
                       or params.get("q1080", [None])[0]
                       or params.get("q480", [None])[0])

            if not encoded:
                # شبها: هر پارامتری که با q شروع میشود
                for key, val in params.items():
                    if key.startswith("q") and val:
                        encoded = val[0]
                        break

            if not encoded:
                log.warning("پارامتر کیفیت در URL پیدا نشد: %s", play_url[:100])
                return None

            # Base64 decode
            decoded = _decode_base64_url(encoded)
            if decoded:
                # فیلتر تبلیغاتی
                if _is_valid_video_url(decoded):
                    log.info("لینک فیلم از Base64: %s...", decoded[:100])
                    return decoded
                else:
                    log.warning("لینک دکود شده تبلیغاتی است: %s", decoded[:100])
                    # با لغو از فیلتر، لینک را برمیگرداند
                    return decoded

            return None

        except Exception as e:
            log.error("خطا در استخراج لینک فیلم: %s", e, exc_info=True)
            return None

    def _get_video_link_selenium(self, page_url: str, timeout: int = 25) -> Optional[str]:
        """
        استخراج لینک ویدیو با Selenium.
        برای فیلم‌ها: روی «پخش آنلاین» کلیک می‌کند، ۱۵ ثانیه صبر می‌کند،
        روی «Skip Ad» کلیک می‌کند، سپس لینک video.currentSrc را می‌گیرد.
        برای سریال‌ها: از URL onlineplay لینک مستقیم را decode می‌کند.
        """
        driver = self._get_driver()
        if not driver:
            log.error("Selenium WebDriver در دسترس نیست.")
            return None

        try:
            with self._driver_lock:
                driver.get(page_url)
                time.sleep(3)

                # اگر لینک onlineplay است (سریال) → decode از URL
                if "onlineplay" in page_url.lower():
                    link = driver.execute_script(
                        'var url = new URL(window.location.href);'
                        'var encoded = url.searchParams.get("q720") '
                        '|| url.searchParams.get("q1080") '
                        '|| url.searchParams.get("q480");'
                        'if (encoded) { try { return atob(encoded); } catch(e) {} }'
                        'return "";'
                    )
                    if link and not _is_ad_link(link):
                        log.info("لینک سریال از Selenium+Base64: %s...", link[:100])
                        return link

                # پیدا کردن دکمه «پخش آنلاین» فیلم
                play_btn = driver.execute_script(
                    'var btn = document.querySelector("a.playmovie, .post-btns a.playmovie, .post-btns.yc a.playmovie");'
                    'if (btn) return btn.href;'
                    'return "";'
                )
                if play_btn:
                    # تلاش برای decode مستقیم از URL (سریع، بدون انتظار)
                    direct = self._decode_onlineplay_url(play_btn)
                    if direct and not _is_ad_link(direct):
                        log.info("لینک فیلم از URL decode: %s...", direct[:100])
                        return direct

                    # اگر decode نشد، وارد صفحه پلیر شو و ۱۵ ثانیه صبر کن
                    log.info("ورود به صفحه پلیر و انتظار ۱۵ ثانیه برای تبلیغات...")
                    driver.get(play_btn)
                    time.sleep(3)

                    # صبر ۱۵ ثانیه برای تبلیغات
                    time.sleep(15)

                    # کلیک روی Skip Ad
                    skip_clicked = driver.execute_script(
                        'var skipBtns = document.querySelectorAll('
                        '  ".vjs-skip-ad, .vjs-skip-button, button.skip-ad, '
                        '  [class*=skip], a.skip, button[aria-label*=skip]");'
                        'for (var i = 0; i < skipBtns.length; i++) {'
                        '  if (skipBtns[i].offsetParent !== null) {'
                        '    skipBtns[i].click();'
                        '    return true;'
                        '  }'
                        '}'
                        'return false;'
                    )
                    if skip_clicked:
                        log.info("کلیک روی Skip Ad انجام شد")
                        time.sleep(2)
                    else:
                        log.info("دکمه Skip Ad پیدا نشد")

                    # استخراج لینک از video.currentSrc
                    link = driver.execute_script(
                        'var v = document.querySelector("video");'
                        'if (v) return v.currentSrc || v.src || "";'
                        'return "";'
                    )
                    if link and not _is_ad_link(link):
                        log.info("لینک فیلم از video.currentSrc: %s...", link[:100])
                        return link

                    # fallback: تلاش برای decode از URL فعلی
                    current_url = driver.current_url or ""
                    direct = self._decode_onlineplay_url(current_url)
                    if direct and not _is_ad_link(direct):
                        log.info("لینک فیلم از URL فعلی: %s...", direct[:100])
                        return direct

                # fallback: تلاش از video element مستقیم
                self._try_click_play(driver)
                self._try_skip_ad(driver)
                time.sleep(2)

                link = driver.execute_script(
                    'var v = document.querySelector("video");'
                    'if (v) return v.currentSrc || v.src || "";'
                    'return "";'
                )
                if link and not _is_ad_link(link):
                    log.info("لینک ویدیو از Selenium: %s...", link[:100])
                    return link

                log.warning("لینک ویدیویی پیدا نشد: %s", page_url)
                return None

        except Exception as e:
            log.error("خطا در Selenium: %s", e, exc_info=True)
            self._reset_driver()
            return None

    def _try_click_play(self, driver) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            play_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, ".vjs-big-play-button"))
            )
            play_btn.click()
            time.sleep(1.5)
        except Exception:
            pass

    def _try_skip_ad(self, driver) -> None:
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            skip_btn = WebDriverWait(driver, 5).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    '//*[contains(translate(text(), "ABCDEFGHIJKLMNOPQRSTUVWXYZ", '
                    '"abcdefghijklmnopqrstuvwxyz"), "skip")]'
                ))
            )
            skip_btn.click()
            time.sleep(1.5)
        except Exception:
            pass

    # ==================== مدیریت Selenium ====================

    def _get_driver(self):
        """ساخت یا بازگرداندن WebDriver.
        با timeout محافظت می‌شود تا اگر Chrome نتوانست شروع شود، ربات هنگ نکند.
        """
        # اگر درایور موجود و سالم است، سریع برگردان
        if self._driver is not None:
            try:
                _ = self._driver.current_url
                return self._driver
            except Exception:
                self._driver = None

        # تلاش برای ساخت درایور جدید با timeout
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
        except ImportError:
            log.error("Selenium نصب نیست. pip install selenium را اجرا کنید.")
            return None

        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1280,720")
        options.add_argument("--disable-extensions")
        options.add_argument("--disable-software-rasterizer")
        options.add_argument("--disable-dev-tools")
        options.add_argument("--disable-features=VizDisplayCompositor")
        options.add_argument("--user-agent=" + UA)
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        prefs = {"profile.default_content_setting_values.media_stream": 2}
        options.add_experimental_option("prefs", prefs)

        # ساخت درایور در یک thread جدا با timeout
        # این کار از هنگ کردن ربات جلوگیری می‌کند اگر Chrome نتوانست شروع شود
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(webdriver.Chrome, options=options)
                try:
                    self._driver = future.result(timeout=30)
                except concurrent.futures.TimeoutError:
                    log.error("Chrome نتوانست در ۳۰ ثانیه شروع شود (timeout)")
                    return None
        except Exception as e:
            log.error("خطا در ساخت WebDriver: %s", e)
            return None

        if self._driver:
            self._driver.set_page_load_timeout(30)
            log.info("Selenium Chrome WebDriver ساخته شد (headless)")
        return self._driver

    def _reset_driver(self) -> None:
        with self._driver_lock:
            if self._driver:
                try:
                    self._driver.quit()
                except Exception:
                    pass
                self._driver = None

    def close(self) -> None:
        self._reset_driver()
        self.s.close()

    @staticmethod
    def _normalize_url(url: str) -> str:
        if url.startswith("/"):
            return BASE_URL + url
        if not url.startswith("http"):
            return BASE_URL + "/" + url
        return url
