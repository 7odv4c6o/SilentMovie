# 🎬 ربات تلگرامی SilentMovie

ربات فیلم/سریال که از سایت tdmmo.xyz اطلاعات می‌گیرد، با اکانت شما لاگین می‌کند
(کپچا را خودکار حل می‌کند)، و برای هر قسمت لینک پخش در **VLC** می‌سازد.

## امکانات
- 🔍 جستجوی فیلم/سریال (با دکمه یا نوشتن نام + جستجوی inline در چت‌ها)
- 🎬 کارت فیلم با پوستر و اطلاعات کامل (نام اصلی، سال، IMDb، ژانر، کشور، کارگردان، بازیگران، خلاصه)
- ▶️ دکمه‌ی هر قسمت → ساخت لینک تازه‌ی `vlc://` + لینک مستقیم `https://`
- ❤️ علاقه‌مندی‌ها | 🕒 تماشا شده‌ها | 📜 جستجوهای اخیر
- 🔒 عضویت اجباری در کانال‌ها (ادمین از پنل اضافه/حذف می‌کند)
- 🛠 پنل مدیریت: آمار، کانال‌ها، پیام همگانی، مدیریت ادمین‌ها، لاگ خطا
- 🗄 دیتابیس SQLite که خودکار ساخته می‌شود
- 🔄 هر ۲ ساعت فایل دیتابیس برای ادمین ارسال می‌شود؛ با آپلود همان فایل در پنل، داده‌ها بازیابی می‌شوند

---

## 📁 چه فایل‌هایی را روی VPS بگذارم؟
**فقط محتوای همین پوشه‌ی `bot/`.** یعنی این فایل‌ها:

```
bot.py              site_client.py      captcha_solver.py
admin_panel.py      database.py         keyboards.py
formatting.py       config.py           captcha_templates.json
requirements.txt    .env                render.yaml
Procfile            .python-version    runtime.txt
assets/welcome.jpg   index.html          silentmovie.service
```

- پوشه‌ی `data/` خودکار ساخته می‌شود (نیازی به کپی نیست).
- پوشه‌ی `__pycache__/` را کپی نکنید (کش پایتون است).
- فایل‌های `_*.py`، `caps/` که **بیرون** پوشه‌ی bot بودند فایل‌های تست‌اند و لازم نیستند.

---

## 🚀 نصب و اجرا روی VPS (اوبونتو/دبیان)

### ۱) آپلود فایل‌ها
با WinSCP یا دستور `scp` پوشه‌ی bot را در `/root/bot` بگذارید. مثال از ویندوز (PowerShell):
```powershell
scp -r C:\Users\msi\Desktop\film\bot root@IP-SERVER:/root/
```

### ۱.۵) اجرا روی Render.com (به‌جای VPS)
اگر می‌خواهی روی Render دیپلوی کنی:

1. همه‌ی فایل‌ها را در یک ریپوی GitHub بگذار (پوشه‌ی `data/` و `.env` را **نگیر** — متغیرها را از داشبورد Render تنظیم کن).
2. در داشبورد Render: **New → Web Service** → ریپوی خودت را انتخاب کن.
3. این مقادیر را تنظیم کن:
   - **Runtime:** Python 3
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`  ← **مهم!** اگر این فیلد خالی باشد، ارور `Exited with status 127` می‌گیری.
   - **Plan:** Free (یا Starter برای همیشه‌روشن بودن)
4. در بخش **Environment** این متغیرها را اضافه کن:
   - `BOT_TOKEN` = توکن ربات
   - `ADMIN_IDS` = آیدی عددی ادمین (مثلاً `7232719340`)
   - `SITE_MOBILE` = موبایل سایت
   - `SITE_PASSWORD` = رمز سایت
5. **Create Web Service** را بزن.
6. بعد از اولین دیپلوی موفق، آدرس `https://<your-service>.onrender.com` را بردار و در متغیر `WEBAPP_URL` قرار بده (برای پلیر آنلاین).

**راه جایگزین:** فایل `render.yaml` در ریپو هست. می‌توانی به‌جای تنظیم دستی، در Render **New → Blueprint** را بزنی و ریپو را انتخاب کنی — همه‌ی تنظیمات خودکار انجام می‌شود.

### رفع ارور `Exited with status 127` روی Render
این ارور یعنی Render نمی‌تواند دستور Start را پیدا کند. علت‌های رایج:
- **فیلد Start Command خالی است** → مقدار `python bot.py` را در آن بنویس.
- **فایل `requirements.txt` در ریشه‌ی ریپو نیست** → مطمئن شو در پوشه‌ی اصلی ریپو است (نه در زیرپوشه).
- **نسخه‌ی پایتون نادرست** → فایل `.python-version` در ریپو هست (Python 3.11.9)؛ در تنظیمات Render هم Runtime را «Python 3» انتخاب کن.
- **اجرای دستور اشتباه** → اگر در Start Command چیزی مثل `npm start` یا `gunicorn` گذاشته‌ای، پاکش کن. این یک برنامه‌ی پایتون است.

### ۲) نصب پیش‌نیازها
```bash
apt update && apt install -y python3 python3-venv python3-pip
cd /root/bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### ۳) بررسی فایل `.env`
```bash
nano .env
```
مقادیر توکن، آیدی ادمین، موبایل و رمز سایت را چک کنید (از قبل پر شده‌اند).
روی VPS خارج از ایران `TELEGRAM_PROXY` را خالی بگذارید.

### ۴) اجرای آزمایشی (برای دیدن لاگ)
```bash
source venv/bin/activate
python bot.py
```
اگر پیام `🤖 ربات در حال اجراست…` را دیدید، در تلگرام برای ربات `/start` بزنید.
برای توقف: `Ctrl + C`.

### ۵) اجرای دائمی با systemd (پیشنهادی)
```bash
# اگر مسیر یا کاربر فرق دارد، فایل سرویس را ویرایش کنید:
nano /root/bot/silentmovie.service

cp /root/bot/silentmovie.service /etc/systemd/system/silentmovie.service
systemctl daemon-reload
systemctl enable silentmovie
systemctl start silentmovie
```

مدیریت سرویس:
```bash
systemctl status silentmovie      # وضعیت
journalctl -u silentmovie -f      # دیدن لاگ زنده
systemctl restart silentmovie     # ری‌استارت
systemctl stop silentmovie        # توقف
```

---

## 🔧 بعد از اجرا (کارهای ادمین)
1. در تلگرام `/start` بزنید (چون آیدی شما ادمین است، دکمه‌ی «🛠 پنل مدیریت» را می‌بینید).
2. از پنل → «📢 کانال‌های عضویت اجباری» → «➕ افزودن کانال»:
   - **ربات را حتماً در کانال ادمین کنید** تا بتواند عضویت را بررسی کند.
   - شناسه را بفرستید: `@channelusername` یا آیدی عددی `-100...`.
3. آمار، پیام همگانی و لاگ خطا هم از همان پنل در دسترس‌اند.

## ♻️ انتقال داده‌ها هنگام تعویض سرور
- ربات هر ۲ ساعت فایل `bot_backup_*.db` را برای شما می‌فرستد (یا از پنل «📤 ارسال فایل دیتابیس»).
- روی سرور جدید بعد از اجرای ربات: پنل → «♻️ بازیابی دیتابیس» → همان فایل را **آپلود** کنید.
  داده‌های قبلی (کاربران، کانال‌ها، علاقه‌مندی‌ها، تاریخچه) برمی‌گردند.

## ⚠️ نکات
- اگر رمز سایت را عوض کردید، در `.env` مقدار `SITE_PASSWORD` را به‌روز کنید و ربات را ری‌استارت کنید.
- لینک‌های پخش موقتی‌اند (چند دقیقه)؛ اگر منقضی شد، دوباره روی همان قسمت بزنید تا لینک تازه ساخته شود.
- فایل `.env` را جایی به اشتراک نگذارید (حاوی توکن و رمز است).
