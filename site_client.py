# -*- coding: utf-8 -*-
"""
site_client.py
--------------
کلاینت سایت tdmmo.xyz (فیلمجو).

مسئولیت‌ها:
  • لاگین با اکانت کاربر + حل خودکار کپچا (با retry) و نگه‌داری سشن پایدار روی دیسک
  • تشخیص انقضای سشن و لاگین مجدد خودکار
  • جستجوی فیلم/سریال
  • خواندن اطلاعات صفحه‌ی فیلم (عنوان، ژانر، کشور، امتیاز، پوستر، خلاصه) و لیست قسمت‌ها
  • تولید لینک تازه‌ی پخش (vlc://) که امضا و انقضا دارد

فصل‌بندی سریال‌ها:
  داده‌های هر فصل داخل تابع جاوااسکریپت toggleListFill2 در یک تگ <script>
  ذخیره شده‌اند. هر فصل یک بلوک if (selectedValue == "X") دارد که HTML لینک‌ها
  را در result2.innerHTML قرار می‌دهد. ما این HTML را با regex استخراج و پارس می‌کنیم.
"""
from __future__ import annotations

import html as _html
import logging
import os
import pickle
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import requests

from captcha_solver import get_solver

log = logging.getLogger("site")

BASE = "https://tdmmo.xyz"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# اگر پاسخ صفحه‌ی لاگین‌شده باشد، حجمش خیلی بیشتر از صفحه‌ی «ورود» (~4KB) است.
_LOGGED_IN_MIN_LEN = 12000


@dataclass
class Episode:
    part: str          # مثلا «قسمت ۱»
    quality: str       # مثلا «480» (بهترین کیفیت موجود)
    size: str          # مثلا «533 مگابایت»
    play_url: str      # لینک /play?a=p&i=...&f=... (باید resolve شود)
    filename: str      # نام فایل
    # لیست همه‌ی کیفیت‌های موجود برای این قسمت (هر آیتم شامل quality و play_url)
    quality_links: list = field(default_factory=list)

    @property
    def label(self) -> str:
        q = f" | {self.quality}p" if self.quality else ""
        s = f" | {self.size}" if self.size else ""
        return f"{self.part}{q}{s}".strip()


@dataclass
class Movie:
    movie_id: str
    title: str = ""
    original_title: str = ""   # نام اصلی (لاتین)
    year: str = ""
    imdb: str = ""
    genre: str = ""
    country: str = ""
    age: str = ""              # رده سنی
    director: str = ""
    stars: str = ""
    poster: str = ""
    plot: str = ""             # فقط خلاصه‌ی داستان
    episodes: List[Episode] = field(default_factory=list)
    seasons: List[str] = field(default_factory=list)  # لیست مقادیر فصل‌ها
    # نگاشت value → عنوان فصل (مثلا «0» → «فصل 1 زیرنویس»)
    season_titles: Dict[str, str] = field(default_factory=dict)

    @property
    def url(self) -> str:
        return f"{BASE}/movie?m={self.movie_id}"

    @property
    def is_series(self) -> bool:
        # اگر بیش از یک «قسمت» متمایز داشته باشد سریال است
        parts = {e.part for e in self.episodes}
        return len([p for p in parts if "قسمت" in p]) > 1


@dataclass
class SearchResult:
    movie_id: str
    title: str
    year: str = ""
    imdb: str = ""
    poster: str = ""


class LoginError(Exception):
    pass


class SiteClient:
    def __init__(self, mobile: str, password: str,
                 session_path: str = "data/site_session.pkl"):
        self.mobile = mobile
        self.password = password
        self.session_path = session_path
        self._lock = threading.RLock()       # هم‌زمانی امن بین هندلرهای ربات
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA})
        self._solver = get_solver()
        self._load_session()

    # ---------------- مدیریت سشن ----------------
    def _load_session(self) -> None:
        try:
            if os.path.exists(self.session_path):
                with open(self.session_path, "rb") as f:
                    self.s.cookies = pickle.load(f)
                log.info("سشن قبلی سایت بارگذاری شد")
        except Exception as e:
            log.warning("بارگذاری سشن ناموفق بود: %s", e)

    def _save_session(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.session_path) or ".", exist_ok=True)
            with open(self.session_path, "wb") as f:
                pickle.dump(self.s.cookies, f)
        except Exception as e:
            log.warning("ذخیره‌ی سشن ناموفق بود: %s", e)

    def _get(self, path: str, **kw) -> requests.Response:
        kw.setdefault("timeout", 45)
        kw.setdefault("headers", {}).setdefault("Referer", BASE + "/")
        return self.s.get(BASE + path if path.startswith("/") else path, **kw)

    # ---------------- لاگین ----------------
    def is_logged_in(self) -> bool:
        """با گرفتن صفحه‌ی اصلی بررسی می‌کند که سشن معتبر است یا نه."""
        try:
            r = self._get("/", allow_redirects=True)
            if len(r.text) >= _LOGGED_IN_MIN_LEN and "form-login" not in r.text:
                return True
            return False
        except requests.RequestException as e:
            log.warning("بررسی وضعیت لاگین ناموفق: %s", e)
            return False

    def login(self, max_attempts: int = 6) -> bool:
        """لاگین با حل خودکار کپچا و چند بار تلاش (کپچای تازه در هر تلاش)."""
        with self._lock:
            for attempt in range(1, max_attempts + 1):
                try:
                    self.s.get(BASE + "/", timeout=45)
                    self.s.get(BASE + "/form-login", headers={"Referer": BASE + "/"}, timeout=45)
                    rc = self.s.get(BASE + "/captcha", headers={"Referer": BASE + "/"}, timeout=45)
                    expr, answer = self._solver.solve(rc.content)
                    if answer is None:
                        log.warning("تلاش %d: کپچا خوانده نشد، تلاش مجدد", attempt)
                        time.sleep(1.0)
                        continue
                    log.info("تلاش %d: کپچا = %s = %s", attempt, expr, answer)
                    r = self.s.post(
                        BASE + "/login",
                        data={"mobile": self.mobile, "password": self.password,
                              "captcha": str(answer), "submit": ""},
                        headers={"Referer": BASE + "/"}, timeout=45,
                    )
                    if len(r.text) >= _LOGGED_IN_MIN_LEN and "form-login" not in r.text:
                        self._save_session()
                        log.info("✅ لاگین موفق بود")
                        return True
                    if "کپچا" in r.text or "امنیتی" in r.text or "اشتباه" in r.text:
                        log.warning("تلاش %d: کپچا/اطلاعات رد شد", attempt)
                    else:
                        log.warning("تلاش %d: پاسخ نامعتبر (len=%d)", attempt, len(r.text))
                    time.sleep(1.2)
                except requests.RequestException as e:
                    log.warning("تلاش %d خطای شبکه: %s", attempt, e)
                    time.sleep(2.0 * attempt)
            return False

    def ensure_login(self) -> bool:
        """اگر لاگین نیست، لاگین می‌کند."""
        with self._lock:
            if self.is_logged_in():
                return True
            log.info("سشن منقضی شده؛ در حال لاگین مجدد…")
            return self.login()

    # ---------------- جستجو ----------------
    def search(self, query: str, page: int = 1) -> List[SearchResult]:
        if not self.ensure_login():
            raise LoginError("لاگین به سایت ممکن نشد")
        q = urllib.parse.quote(query.strip())
        r = self._get(f"/search?q={q}&p={page}")
        return self._parse_search(r.text)

    @staticmethod
    def _parse_search(text: str) -> List[SearchResult]:
        results: List[SearchResult] = []
        for block in re.findall(r'<div class="movie_item">(.*?)</a>\s*</div>', text, re.S):
            mid = re.search(r'movie\?m=(\d+)', block)
            if not mid:
                continue
            title = re.search(r'movie_item_title">([^<]*)<', block)
            year = re.search(r'movie_item_year">([^<]*)<', block)
            imdb = re.search(r'movie_item_imdb">([^<]*)<', block)
            poster = re.search(r'<img src="([^"]+)"', block)
            results.append(SearchResult(
                movie_id=mid.group(1),
                title=_clean(title.group(1)) if title else "",
                year=_clean(year.group(1)) if year else "",
                imdb=_clean(imdb.group(1)) if imdb else "",
                poster=poster.group(1) if poster else "",
            ))
        seen = set()
        uniq = []
        for r in results:
            if r.movie_id in seen:
                continue
            seen.add(r.movie_id)
            uniq.append(r)
        return uniq

    def search_total_pages(self, text: str) -> int:
        m = re.search(r'کل صفحات جستجوی[^:]*:\s*(\d+)', text)
        return int(m.group(1)) if m else 1

    # ---------------- صفحه‌ی فیلم ----------------
    def movie(self, movie_id: str) -> Movie:
        if not self.ensure_login():
            raise LoginError("لاگین به سایت ممکن نشد")
        r = self._get(f"/movie?m={movie_id}")
        return self._parse_movie(movie_id, r.text)

    @staticmethod
    def _parse_movie(movie_id: str, text: str) -> Movie:
        m = Movie(movie_id=movie_id)
        # عنوان و متادیتا داخل DetitlesTitleBox
        box = re.search(r'DetitlesTitleBox">(.*?)</div>\s*</div>', text, re.S)
        block = box.group(1) if box else text
        t = re.search(r'DetilesTitlesLarg">([^<]+)<', block)
        if t:
            m.title = _clean(t.group(1))
        for line in re.findall(r'DetilesTitlesSmall">([^<]+)<', block):
            line = _clean(line)
            if "imdb" in line.lower():
                m.imdb = line.replace(":", "").replace("imdb", "").strip()
            elif line.startswith("ژانر"):
                m.genre = line.split(":", 1)[-1].strip()
            elif line.startswith("کشور"):
                m.country = line.split(":", 1)[-1].strip()
            elif line.startswith("سال"):
                m.year = line.split(":", 1)[-1].strip()
        # پوستر
        pm = re.search(r'(https?://[^\s"\'<>]+/pic-list/[^\s"\'<>]+\.(?:jpe?g|png|webp))(?:\?[^\s"\'<>]*)?', text)
        if pm:
            m.poster = pm.group(1)
        if not m.poster:
            pm2 = re.search(r'<img[^>]+class=["\'][^"\'>]*(?:poster|cover|thumb)[^"\'>]*["\'][^>]+src=["\']([^"\'>]+)["\']', text, re.I)
            if pm2:
                m.poster = pm2.group(1)
        if not m.poster:
            pm3 = re.search(r'<img[^>]+src=["\']([^"\'>]+\.(?:jpe?g|png|webp))(?:\?[^\s"\'<>]*)?["\']', text, re.I)
            if pm3:
                m.poster = pm3.group(1)
        # بلاک توضیحات
        des = re.search(r'DetitlesDes[^"]*"[^>]*>(.*?)</div>', text, re.S)
        if des:
            raw = re.sub(r'<[^>]+>', "\n", des.group(1))
            lines = [_clean(x) for x in raw.split("\n")]
            lines = [x for x in lines if x]
            plot_lines: List[str] = []
            capture_plot = False
            for ln in lines:
                low = ln.replace(" ", "")
                if ln.startswith("نام اصلی"):
                    m.original_title = ln.split(":", 1)[-1].strip()
                elif ln.startswith("ژانر") and not m.genre:
                    m.genre = ln.split(":", 1)[-1].strip()
                elif ln.startswith("محصول"):
                    val = ln.split(":", 1)[-1].strip()
                    ym = re.search(r'(\d{4})', val)
                    if ym and not m.year:
                        m.year = ym.group(1)
                    country = re.sub(r'\d{4}', '', val).strip()
                    if country and not m.country:
                        m.country = country
                elif "ردهسنی" in low or ln.startswith("رده سنی"):
                    m.age = ln.split(":", 1)[-1].strip()
                elif ln.startswith("کارگردان"):
                    m.director = ln.split(":", 1)[-1].strip()
                elif ln.startswith("ستارگان"):
                    m.stars = ln.split(":", 1)[-1].strip()
                elif "خلاصه" in ln and ":" in ln and len(ln) < 25:
                    capture_plot = True
                elif capture_plot:
                    if re.match(r'^[A-Za-z0-9 /,\.\-]+$', ln):
                        continue
                    plot_lines.append(ln)
            m.plot = " ".join(plot_lines).strip()

        # ---- فصل‌ها و قسمت‌ها ----
        # ابتدا سعی کن از toggleListFill استخراج کن
        season_data = SiteClient._extract_seasons_from_script(text)

        if season_data:
            # فصل‌ها از اسکریپت استخراج شد
            m.seasons = [s["value"] for s in season_data]
            # ذخیره‌ی عناوین فصل‌ها برای نمایش به‌جای مقدار عددی
            m.season_titles = {s["value"]: s["title"] for s in season_data}
            # قسمت‌های فصل اول (یا فصلی که پیش‌فرض نمایش داده میشه)
            # صفحه HTML ممکنه فصل اول رو نمایش بده
            m.episodes = SiteClient._parse_episodes(text)
            # اگر از HTML قسمتی پیدا نشد، از فصل اول اسکریپت بگیر
            if not m.episodes and season_data:
                m.episodes = SiteClient._parse_episodes_from_html(season_data[0]["html"])
            log.info("فصل‌ها از toggleListFill: %d فصل/category", len(m.seasons))
        else:
            # بدون toggleListFill — از select#select و HTML عادی
            m.seasons = SiteClient._parse_seasons_select(text)
            # ذخیره‌ی عناوین فصل‌ها
            titles = SiteClient._parse_seasons_with_titles(text)
            if titles:
                m.season_titles = titles
            m.episodes = SiteClient._parse_episodes(text)
            if m.seasons:
                log.info("فصل‌ها از select#select: %s", m.seasons)

        return m

    def movie_season(self, movie_id: str, season_value: str) -> Movie:
        """صفحه فیلم رو میگیره و قسمت‌های فصل خاص رو استخراج می‌کنه.
        فصل‌ها داخل تابع toggleListFill در اسکریپت صفحه هستند.
        """
        if not self.ensure_login():
            raise LoginError("لاگین به سایت ممکن نشد")
        r = self._get(f"/movie?m={movie_id}")
        movie = self._parse_movie(movie_id, r.text)

        # قسمت‌های فصل درخواستی را از اسکریپت استخراج کن
        season_data = SiteClient._extract_seasons_from_script(r.text)
        if season_data:
            for sd in season_data:
                if sd["value"] == season_value:
                    movie.episodes = SiteClient._parse_episodes_from_html(sd["html"])
                    log.info("فصل %s: %d قسمت (دسته: %s)",
                             season_value, len(movie.episodes), sd.get("title", ""))
                    break
            else:
                # فصل پیدا نشد
                movie.episodes = []
                log.warning("فصل %s پیدا نشد در toggleListFill", season_value)
        else:
            # fallback: سعی کن با select انجام بده
            r2 = self._get(f"/movie?m={movie_id}&s={season_value}")
            movie2 = self._parse_movie(movie_id, r2.text)
            movie.episodes = movie2.episodes

        return movie

    # ---- استخراج فصل‌ها از toggleListFill ----

    @staticmethod
    def _extract_seasons_from_script(text: str) -> List[dict]:
        """فصل‌ها رو از تابع toggleListFill (یا toggleListFill2) در تگ <script> استخراج می‌کنه.

        ساختار expected (tdmmo.xyz):
          <select id="select">
            <option value="0">فصل 1 زیرنویس</option>
            <option value="1">فصل 1 دوبله</option>
            <option value="2">فصل 2 زیرنویس</option>
            <option value="3">فصل 2 دوبله</option>
            ...
          </select>
          <script>
            function toggleListFill() {
              var selectedValue = document.getElementById('select').value;
              if (selectedValue == '0') {
                result.innerHTML = '<a href="...">قسمت 1 - کیفیت : 480</a>';
              }
              ...
            }
          </script>

        نکته: هر option در select معمولاً یک ترکیب «فصل + نوع» است (مثلا «فصل 1 دوبله»)
        بنابراین خروجی به‌جای 4 فصل، 8 category می‌دهد.

        خروجی: [{"value": "0", "title": "فصل 1 زیرنویس", "html": "..."}, ...]
        """
        # 1. پیدا کردن select برای عنوان فصل‌ها
        # سایت واقعی از #select استفاده می‌کند (نه #select2)
        # اما برای پایداری هر دو را امتحان می‌کنیم
        season_map: Dict[str, str] = {}  # value -> title
        for select_id in ("select", "select2"):
            select_match = re.search(
                rf'<select[^>]*id=["\']{select_id}["\'][^>]*>(.*?)</select>',
                text, re.S
            )
            if not select_match:
                continue
            # پارس option ها: value → title
            for opt in re.finditer(
                r'<option[^>]*value=["\']([^"\']+)["\'][^>]*>([^<]*)<',
                select_match.group(1)
            ):
                val = opt.group(1).strip()
                title = _clean(opt.group(2))
                if val and title:
                    season_map[val] = title
            if season_map:
                log.info("select#%s با %d گزینه پیدا شد", select_id, len(season_map))
                break

        if not season_map:
            return []

        # اگر فقط یک گزینه داره، نیازی به انتخاب فصل نیست
        if len(season_map) <= 1:
            return []

        # 2. پیدا کردن تابع toggleListFill (یا toggleListFill2) در script
        # اول تابع اصلی (بدون 2) را امتحان کن، سپس fallback به نسخه با 2
        # نکته: به‌جای استخراج فقط بدنه‌ی تابع، کل محتوای script را می‌گیریم
        # و سپس الگوی if (selectedValue == 'X') { ... result.innerHTML = '...' } را در آن جستجو می‌کنیم.
        # این کار از مشکل توقف regex در اولین \n} (که مربوط به بسته شدن if است نه تابع) جلوگیری می‌کند.
        result_var = "result"  # متغیر تزریق HTML (result یا result2)
        fn_name = "toggleListFill"
        # پیدا کردن تمام بلوک‌های <script> در صفحه و یکی کردنشان
        all_scripts = re.findall(r'<script[^>]*>(.*?)</script>', text, re.S)
        script_body = "\n".join(all_scripts)

        if not script_body:
            log.warning("هیچ بلوک <script> در صفحه پیدا نشد")
            return []

        # تشخیص نام تابع و متغیر result
        # اول تابع اصلی (toggleListFill) را امتحان کن، سپس toggleListFill2
        if "toggleListFill2" in script_body and "toggleListFill" not in script_body.replace("toggleListFill2", ""):
            # فقط toggleList2 وجود دارد
            fn_name = "toggleListFill2"
            result_var = "result2"
        elif "toggleListFill2" in script_body:
            # هر دو وجود دارند — اولویت با toggleListFill (نسخه اصلی)
            fn_name = "toggleListFill"
            result_var = "result"
        elif "toggleListFill" in script_body:
            fn_name = "toggleListFill"
            result_var = "result"
        else:
            log.warning("هیچ تابع toggleListFill/toggleListFill2 پیدا نشد")
            return []

        log.info("استفاده از تابع %s با متغیر %s.innerHTML", fn_name, result_var)

        # 3. برای هر فصل، بلوک HTML را استخراج کنیم
        # الگو: if (selectedValue == 'X') { result.innerHTML = 'HTML'; }
        # نکته: HTML داخل آن ممکنه دارای کوتیت‌های escape شده (\\') باشه
        results = []
        for val, title in season_map.items():
            html_content = None
            # امتحان با هر دو نوع کوتیت
            for quote_char in ["'", '"']:
                # الگوی کلی: if (selectedValue == 'X') { ... result.innerHTML = 'HTML'; ... }
                # برای تطبیق HTML که ممکنه حاوی همان quote کاراکتر (به‌صورت escape شده) باشد
                # از الگوی (?:[^q\\]|\\.)* استفاده می‌کنیم که یعنی «هر کاراکتر به‌جز q یا \، یا یک escape sequence»
                pattern = re.compile(
                    rf"if\s*\(\s*selectedValue\s*==\s*[{quote_char}]" + re.escape(val) + rf"[{quote_char}]\s*\)\s*\{{"
                    rf"\s*{result_var}\.innerHTML\s*=\s*[{quote_char}]((?:[^{quote_char}\\\\]|\\\\.)*?)[{quote_char}]",
                    re.S
                )
                m = pattern.search(script_body)
                if m:
                    html_content = m.group(1)
                    # unescape: \' → '  و \\ → \
                    html_content = html_content.replace(r"\'", "'").replace(r'\\', '\\')
                    html_content = html_content.replace(r'\"', '"')
                    break

            if html_content:
                results.append({
                    "value": val,
                    "title": title,
                    "html": html_content,
                })

        log.info("استخراج فصل‌ها از اسکریپت: %d فصل/category", len(results))
        return results

    @staticmethod
    def _parse_episodes_from_html(html: str) -> List[Episode]:
        """پارسر لینک قسمت‌ها از یک بلوک HTML (که از toggleListFill استخراج شده).
        هر قسمت فقط یک بار در خروجی ظاهر می‌شود، با تمام کیفیت‌هایش در quality_links.
        این کار از تکرار یک قسمت به‌خاطر تفاوت کیفیت جلوگیری می‌کند.
        """
        # الگوی پیدا کردن لینک‌های /play?a=p
        pattern = re.compile(
            r'href=["\']?(https?://tdmmo\.xyz/play\?a=p[^"\'\s>]+)["\'\s]?[^>]*>(.*?)</a>',
            re.S
        )
        # نگاشت شماره قسمت → Episode
        # همه‌ی کیفیت‌ها برای یک قسمت در یک Episode جمع می‌شوند
        ep_map: Dict[str, Episode] = {}
        # ترتیب appearance قسمت‌ها را نگه می‌داریم
        ep_order: List[str] = []

        for m in pattern.finditer(html):
            href = m.group(1).replace("&amp;", "&")
            inner = m.group(2)
            fn = re.search(r'[?&]f=([^&"]+)', href)
            filename = urllib.parse.unquote(fn.group(1)) if fn else ""
            spans = re.findall(r'>([^<]+)<', inner)
            spans = [_clean(s) for s in spans if _clean(s)]
            size = ""
            part = ""
            quality = ""
            for s in spans:
                if "مگابایت" in s or "گیگابایت" in s:
                    size = s
                elif "قسمت" in s or "کیفیت" in s:
                    pm = re.search(r'(قسمت\s*\d+)', s)
                    qm = re.search(r'کیفیت\s*:\s*(\d+)', s)
                    if pm:
                        part = pm.group(1)
                    if qm:
                        quality = qm.group(1)
            if not part:
                # تلاش برای استخراج شماره قسمت از نام فایل
                fn_ep = re.search(r'[Ee](\d+)', filename)
                if fn_ep:
                    part = f"قسمت {fn_ep.group(1)}"
                else:
                    part = "پخش"

            # افزودن این لینک به عنوان یک گزینه کیفیت برای این قسمت
            quality_link = {
                "quality": quality,
                "url": href,
                "size": size,
            }
            if part not in ep_map:
                # اولین بار که این قسمت را می‌بینیم
                ep_map[part] = Episode(
                    part=part, quality=quality, size=size,
                    play_url=href, filename=filename,
                    quality_links=[quality_link]
                )
                ep_order.append(part)
            else:
                # این قسمت را قبلاً دیدیم — این کیفیت جدید را اضافه کن
                existing = ep_map[part]
                existing.quality_links.append(quality_link)
                # اگر این کیفیت بالاتر از کیفیت فعلی است، آن را به‌عنوان کیفیت اصلی قرار بده
                if quality and existing.quality:
                    try:
                        if int(quality) > int(existing.quality):
                            existing.quality = quality
                            existing.size = size
                            existing.play_url = href
                    except (ValueError, TypeError):
                        pass
                elif quality and not existing.quality:
                    existing.quality = quality
                    existing.size = size
                    existing.play_url = href

        # تبدیل به لیست با حفظ ترتیب ظاهر شدن
        eps = [ep_map[p] for p in ep_order]
        return eps

    @staticmethod
    def _parse_seasons_select(text: str) -> List[str]:
        """فالبک: فصل‌ها رو از select (یا select2) استخراج می‌کنه.
        فقط وقتی toggleListFill پیدا نشده.
        خروجی: لیست value های option.
        """
        # اول select#select را امتحان کن (ساختار اصلی سایت)
        for select_id in ("select", "select2"):
            select_match = re.search(
                rf'<select[^>]*id=["\']{select_id}["\'][^>]*>(.*?)</select>',
                text, re.S
            )
            if not select_match:
                continue
            options = re.findall(
                r'<option[^>]*value=["\']([^"\']+)["\'][^>]*>',
                select_match.group(1)
            )
            # فقط مقادیر عددی معتبر (>= 0) رو بگیر
            # (مقدار 0 هم معتبر است چون سایت از 0 شروع می‌کند)
            valid = [v for v in options if v.isdigit() and int(v) >= 0]
            if len(valid) > 1:
                log.info("select#%s: %d گزینه", select_id, len(valid))
                return valid
        return []

    @staticmethod
    def _parse_seasons_with_titles(text: str) -> Dict[str, str]:
        """فالبک: استخراج value → title برای فصل‌ها.
        وقتی toggleListFill پیدا نشده، این متد عنوان هر فصل را برمی‌گرداند.
        """
        for select_id in ("select", "select2"):
            select_match = re.search(
                rf'<select[^>]*id=["\']{select_id}["\'][^>]*>(.*?)</select>',
                text, re.S
            )
            if not select_match:
                continue
            result: Dict[str, str] = {}
            for opt in re.finditer(
                r'<option[^>]*value=["\']([^"\']+)["\'][^>]*>([^<]*)<',
                select_match.group(1)
            ):
                val = opt.group(1).strip()
                title = _clean(opt.group(2))
                if val and title:
                    result[val] = title
            if len(result) > 1:
                return result
        return {}

    @staticmethod
    def _parse_episodes(text: str) -> List[Episode]:
        """پارسر لینک قسمت‌ها از HTML صفحه (قسمت‌های فصل فعلی).
        هر قسمت فقط یک بار در خروجی ظاهر می‌شود، با تمام کیفیت‌هایش.
        """
        eps: List[Episode] = []
        pattern = re.compile(
            r'href="(https://tdmmo\.xyz/play\?a=p[^"]+)"\s*>(.*?)</a>', re.S)
        # نگاشت شماره قسمت → Episode برای تجمیع کیفیت‌ها
        ep_map: Dict[str, Episode] = {}
        ep_order: List[str] = []
        for href, inner in pattern.findall(text):
            href = href.replace("&amp;", "&")
            fn = re.search(r'[?&]f=([^&"]+)', href)
            filename = urllib.parse.unquote(fn.group(1)) if fn else ""
            spans = re.findall(r'>([^<]+)<', inner)
            spans = [_clean(s) for s in spans if _clean(s)]
            size = ""
            part = ""
            quality = ""
            for s in spans:
                if "مگابایت" in s or "گیگابایت" in s:
                    size = s
                elif "قسمت" in s or "کیفیت" in s:
                    pm = re.search(r'(قسمت\s*\d+)', s)
                    qm = re.search(r'کیفیت\s*:\s*(\d+)', s)
                    if pm:
                        part = pm.group(1)
                    if qm:
                        quality = qm.group(1)
            if not part:
                fn_ep = re.search(r'[Ee](\d+)', filename)
                if fn_ep:
                    part = f"قسمت {fn_ep.group(1)}"
                else:
                    part = "پخش"

            quality_link = {
                "quality": quality,
                "url": href,
                "size": size,
            }
            if part not in ep_map:
                ep_map[part] = Episode(
                    part=part, quality=quality, size=size,
                    play_url=href, filename=filename,
                    quality_links=[quality_link]
                )
                ep_order.append(part)
            else:
                existing = ep_map[part]
                existing.quality_links.append(quality_link)
                if quality and existing.quality:
                    try:
                        if int(quality) > int(existing.quality):
                            existing.quality = quality
                            existing.size = size
                            existing.play_url = href
                    except (ValueError, TypeError):
                        pass
                elif quality and not existing.quality:
                    existing.quality = quality
                    existing.size = size
                    existing.play_url = href

        eps = [ep_map[p] for p in ep_order]
        return eps

    # ---------------- تولید لینک پخش ----------------
    def resolve_play(self, play_url: str, movie_id: str = "") -> Optional[str]:
        """لینک /play?a=p را به لینک تازه‌ی vlc:// (امضاشده) تبدیل می‌کند."""
        if not self.ensure_login():
            raise LoginError("لاگین به سایت ممکن نشد")
        with self._lock:
            ref = f"{BASE}/movie?m={movie_id}" if movie_id else BASE + "/"
            r = self.s.get(play_url, headers={"Referer": ref}, timeout=45,
                           allow_redirects=False)
            loc = r.headers.get("Location", "")
            if loc.startswith("vlc://"):
                return loc
            if "login" in loc:
                if self.login():
                    r = self.s.get(play_url, headers={"Referer": ref}, timeout=45,
                                   allow_redirects=False)
                    loc = r.headers.get("Location", "")
                    if loc.startswith("vlc://"):
                        return loc
            return None

    @staticmethod
    def vlc_to_http(vlc_url: str) -> str:
        """لینک vlc:// را به لینک مستقیم http:// تبدیل می‌کند."""
        if not vlc_url:
            return vlc_url
        u = vlc_url
        if u.startswith("vlc://"):
            u = u[len("vlc://"):]
        if u.startswith("https://"):
            u = "http://" + u[len("https://"):]
        elif not u.startswith("http://"):
            u = "http://" + u
        return u

    @staticmethod
    def vlc_to_https(vlc_url: str) -> str:
        return SiteClient.vlc_to_http(vlc_url)


def _clean(s: str) -> str:
    return _html.unescape(re.sub(r"\s+", " ", s or "")).strip()
