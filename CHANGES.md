# تغییرات نسخه‌ی v17 (اسکریپت همگام‌سازی + حل مشکل ۲۳ لینک فیلم)

## معماری جدید

### مشکل
- فیلم‌های ایرانی «۲۳ لینک/قسمت» نشان می‌دادند (چون ربات تمام لینک‌های صفحه شامل «همچنین تماشا کنید» را می‌گرفت)
- نیاز به Selenium برای کلیک روی «مشاهده بیشتر» و انتظار ۱۵ ثانیه برای تبلیغات
- اما Selenium روی Render free tier باعث OOM می‌شد

### راه‌حل
معماری دو بخشی:
1. **`sync_archive.py`** — اسکریپتی که روی کامپیوتر خودتان اجرا می‌کنید (با Selenium)
2. **ربات روی Render** — فقط از دیتابیس می‌خواند (بدون Selenium)

## ۱. اسکریپت sync_archive.py

این اسکریپت را روی کامپیوتر خودتان اجرا کنید:

```bash
# نصب پکیج‌ها
pip install -r requirements-sync.txt

# تنظیم DATABASE_URL (در فایل .env یا به‌صورت مستقیم)
export DATABASE_URL="postgres://avnadmin:AVNS_wDfIwWbmz5XS2WgkvYh@pg-175a77e0-ehsanpoint-3c63.l.aivencloud.com:16853/defaultdb?sslmode=require"

# اجرای کامل
python sync_archive.py

# یا فقط فیلم‌ها
python sync_archive.py --movies-only

# یا فقط ۵ فیلم اول (برای تست)
python sync_archive.py --movies-only --limit 5

# یا با نمایش پنجره Chrome (برای دیباگ)
python sync_archive.py --no-headless
```

### کارهایی که انجام می‌دهد:
1. وارد صفحه‌ی آرشیو فیلم‌ها می‌شود
2. دکمه «مشاهده بیشتر» را تا انتها کلیک می‌کند (تا تمام فیلم‌ها لود شوند)
3. تمام فیلم‌ها را در دیتابیس ذخیره می‌کند (فقط فیلم‌های جدید)
4. برای هر فیلم:
   - روی دکمه «پخش فیلم» (`.post-btns.yc a.playmovie`) کلیک می‌کند
   - ۱۷ ثانیه صبر می‌کند (برای تبلیغات)
   - اگر دکمه Skip Ad وجود دارد، کلیک می‌کند
   - لینک مستقیم را از `video.currentSrc` می‌گیرد
   - در دیتابیس ذخیره می‌کند
5. برای سریال‌ها:
   - وارد صفحه می‌شود
   - تمام `.serie-item` را پیدا می‌کند
   - برای هر قسمت، لینک onlineplay را می‌گیرد
   - با decode Base64 (q720/q1080/q480) لینک مستقیم را استخراج می‌کند
   - در دیتابیس ذخیره می‌کند
6. فیلم‌ها و سریال‌های قبلی را دوباره اسکن نمی‌کند (مگر با `--force`)

## ۲. جداول دیتابیس جدید

سه جدول جدید به `database.py` اضافه شد:

### `iranian_movies`
- `title` — عنوان فیلم
- `page_url` — لینک صفحه فیلم (UNIQUE)
- `poster` — لینک پوستر
- `direct_link` — لینک مستقیم ویدیو (توسط sync_archive.py پر می‌شود)
- `scanned_at` — زمان آخرین اسکن
- `type` — "movie" یا "series"

### `iranian_series`
- `title` — عنوان سریال
- `page_url` — لینک صفحه سریال (UNIQUE)
- `poster` — لینک پوستر
- `scanned_at` — زمان آخرین اسکن

### `iranian_episodes`
- `series_url` — لینک سریال (FK)
- `season` — شماره فصل
- `episode` — شماره قسمت
- `title` — عنوان قسمت
- `onlineplay_url` — لینک onlineplay
- `direct_link` — لینک مستقیم ویدیو (decode شده)
- `scanned_at` — زمان اسکن

## ۳. اصلاح ربات (bot.py)

### حل مشکل «۲۳ لینک فیلم»
- وقتی کاربر روی یک فیلم ایرانی کلیک می‌کند، ربات **دیگر** `get_series_structure` را صدا نمی‌زند
- به‌جای آن، فقط دکمه «پخش آنلاین» را نمایش می‌دهد
- وقتی کاربر روی «پخش آنلاین» کلیک می‌کند:
  ۱. اول دیتابیس را چک می‌کند (اگر لینک مستقیم ذخیره شده → فوراً پخش)
  ۲. اگر نبود، از سایت استخراج می‌کند (decode Base64 یا Selenium)

### اولویت بارگذاری
1. **دیتابیس** (سریع‌ترین — اگر توسط sync_archive.py پر شده)
2. **کش حافظه** (۱ ساعت)
3. **سایت** (requests یا Selenium)

## ۴. متدهای جدید database.py

- `upsert_iranian_movie(title, page_url, poster, direct_link, content_type)` — درج/به‌روزرسانی فیلم
- `list_iranian_movies(content_type)` — لیست فیلم‌ها
- `list_iranian_series()` — لیست سریال‌ها
- `get_iranian_movie(page_url)` — گرفتن یک فیلم
- `get_pending_movies()` — فیلم‌های بدون لینک مستقیم
- `get_pending_series()` — سریال‌های بدون قسمت
- `upsert_iranian_episode(...)` — درج قسمت
- `list_iranian_episodes(series_url)` — لیست قسمت‌های یک سریال

## ۵. متدهای جدید serialirany_client.py

- `get_movie_list()` — اول دیتابیس، بعد سایت
- `get_series_list()` — اول دیتابیس، بعد سایت
- `get_series_structure()` — اول دیتابیس، بعد سایت
- `get_video_link()` — اول دیتابیس، بعد decode Base64، بعد Selenium

## نحوه استفاده

### مرحله ۱: آماده‌سازی
1. فایل ZIP را دانلود و استخراج کنید
2. مطمئن شوید Chrome نصب است
3. پکیج‌ها را نصب کنید:
   ```bash
   pip install -r requirements-sync.txt
   ```

### مرحله ۲: اجرای sync_archive.py (روی کامپیوتر خودتان)
```bash
# تنظیم DATABASE_URL
export DATABASE_URL="postgres://avnadmin:AVNS_wDfIwWbmz5XS2WgkvYh@pg-175a77e0-ehsanpoint-3c63.l.aivencloud.com:16853/defaultdb?sslmode=require"

# تست با ۵ فیلم اول
python sync_archive.py --movies-only --limit 5

# اگر کار کرد، اجرای کامل
python sync_archive.py
```

### مرحله ۳: Deploy ربات روی Render
1. فایل‌های ربات را روی Render آپلود کنید (بدون sync_archive.py و requirements-sync.txt)
2. متغیرهای محیطی را تنظیم کنید:
   ```
   DATABASE_URL=postgres://avnadmin:AVNS_wDfIwWbmz5XS2WgkvYh@pg-175a77e0-ehsanpoint-3c63.l.aivencloud.com:16853/defaultdb?sslmode=require
   USE_SELENIUM_FOR_ARCHIVE=false
   ENABLE_PRELOAD=false
   ```
3. ربات به‌طور خودکار از دیتابیس می‌خواند

### مرحله ۴: افزودن فیلم‌های جدید
وقتی فیلم‌های جدیدی به سایت اضافه می‌شود:
```bash
# فقط فیلم‌های جدید را اسکن کن (فیلم‌های قبلی را دوباره اسکن نمی‌کند)
python sync_archive.py --movies-only

# یا برای به‌روزرسانی کامل (همه را دوباره اسکن کن)
python sync_archive.py --force
```

## مزایا

1. **ربات سبک** — بدون Selenium، بدون OOM
2. **همه فیلم‌ها نمایش داده می‌شوند** — چون sync_archive.py با کلیک روی «مشاهده بیشتر» تمام فیلم‌ها را می‌گیرد
3. **لینک‌های مستقیم ذخیره می‌شوند** — وقتی کاربر روی پخش کلیک می‌کند، فوراً پخش می‌شود
4. **فقط فیلم‌های جدید اسکن می‌شوند** — صرفه‌جویی در زمان
5. **دیتابیس مرکزی** — چند ربات می‌توانند از همان دیتابیس استفاده کنند

## فایل‌های جدید/تغییر کرده

| فایل | نوع | توضیح |
|------|-----|-------|
| `sync_archive.py` | جدید | اسکریپت همگام‌سازی (روی کامپیوتر خودتان) |
| `requirements-sync.txt` | جدید | پکیج‌های sync_archive.py |
| `database.py` | تغییر | جداول جدید + متدهای آرشیو |
| `serialirany_client.py` | تغییر | اولویت دیتابیس در خواندن |
| `bot.py` | تغییر | حل مشکل ۲۳ لینک فیلم |

## نکات مهم

۱. **sync_archive.py را روی Render اجرا نکنید** — باعث OOM می‌شود. فقط روی کامپیوتر خودتان.

۲. **اولین بار طول می‌کشد** — اسکن کامل همه فیلم‌ها ممکن است ۱-۲ ساعت طول بکشد (بسته به سرعت اینترنت).

۳. **می‌توانید متوقف و ادامه دهید** — اگر وسط کار متوقف کنید، دفعه بعد فقط فیلم‌های جدید را اسکن می‌کند.

۴. **برای تست** — از `--limit 5` استفاده کنید تا فقط ۵ فیلم اول اسکن شوند.

۵. **اگر لینک بعضی فیلم‌ها پیدا نشد** — می‌توانید با `--force` دوباره همه را اسکن کنید.
