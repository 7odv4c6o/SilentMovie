# -*- coding: utf-8 -*-
"""
admin_panel.py
--------------
پنل مدیریت ربات + جاب زمان‌بندی‌شده‌ی بکاپ دیتابیس.

وابستگی‌ها (db, site, pending_admin) از bot.py با init() تزریق می‌شوند تا از
وابستگی حلقوی جلوگیری شود.
"""
from __future__ import annotations

import io
import os
import time
import logging
from datetime import datetime
from typing import Dict, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import ContextTypes

import keyboards as kb
from database import Database

log = logging.getLogger("admin")

db: Database = None
site = None
pending_admin: Dict[int, str] = {}


def init(_db, _site, _pending) -> None:
    global db, site, pending_admin
    db, site, pending_admin = _db, _site, _pending


def _is_admin(uid: int) -> bool:
    return db.is_admin(uid)


def _super_admin() -> Optional[int]:
    admins = db.list_admins()
    return admins[0] if admins else None


# ---------------- ورود به پنل ----------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.effective_message.reply_text("⛔️ شما دسترسی ادمین ندارید.")
        return
    await update.effective_message.reply_text(
        "🛠 <b>پنل مدیریت</b>\nیک گزینه را انتخاب کنید:",
        parse_mode=ParseMode.HTML, reply_markup=kb.admin_panel_kb())


async def _show_home(q) -> None:
    try:
        await q.edit_message_text("🛠 <b>پنل مدیریت</b>\nیک گزینه را انتخاب کنید:",
                                  parse_mode=ParseMode.HTML, reply_markup=kb.admin_panel_kb())
    except BadRequest:
        await q.message.reply_text("🛠 <b>پنل مدیریت</b>", parse_mode=ParseMode.HTML,
                                   reply_markup=kb.admin_panel_kb())


# ---------------- روتر callback پنل ----------------
async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str) -> None:
    q = update.callback_query
    uid = update.effective_user.id
    if not _is_admin(uid):
        await q.answer("⛔️ دسترسی ندارید.", show_alert=True)
        return

    if action == "home":
        pending_admin.pop(uid, None)
        await _show_home(q); await q.answer(); return

    if action == "close":
        pending_admin.pop(uid, None)
        try:
            await q.message.delete()
        except BadRequest:
            pass
        await q.answer("بسته شد"); return

    if action == "stats":
        s = db.stats()
        text = (
            "📊 <b>آمار ربات</b>\n\n"
            f"👥 کاربران: <b>{s['users']}</b>\n"
            f"🚫 مسدود: {s['blocked']}\n"
            f"📢 کانال‌های عضویت اجباری: {s['channels']}\n"
            f"❤️ کل علاقه‌مندی‌ها: {s['favorites']}\n"
            f"🔍 کل جستجوها: {s['searches']}\n"
            f"🗄 فیلم‌های کش‌شده: {s['cached']}\n"
            f"🐞 خطاهای ثبت‌شده: {s['errors']}\n"
        )
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=kb.back_home_kb())
        except BadRequest:
            await q.message.reply_html(text, reply_markup=kb.back_home_kb())
        await q.answer(); return

    if action == "channels":
        chans = db.list_channels()
        await q.edit_message_text(
            "📢 <b>کانال‌های عضویت اجباری</b>\n\n"
            + ("برای حذف روی 🗑 بزنید، یا کانال جدید اضافه کنید."
               if chans else "هیچ کانالی ثبت نشده. یک کانال اضافه کنید."),
            parse_mode=ParseMode.HTML, reply_markup=kb.channels_kb(chans))
        await q.answer(); return

    if action == "addch":
        pending_admin[uid] = "add_channel"
        await q.edit_message_text(
            "➕ <b>افزودن کانال</b>\n\n"
            "شناسه‌ی کانال را بفرستید. یکی از این دو فرم:\n"
            "• یوزرنیم عمومی: <code>@mychannel</code>\n"
            "• آیدی عددی خصوصی: <code>-1001234567890</code>\n\n"
            "می‌توانید بعد از شناسه و یک فاصله، لینک دعوت را هم بگذارید:\n"
            "<code>-1001234567890 https://t.me/+abcd...</code>\n\n"
            "⚠️ حتماً ربات را در آن کانال <b>ادمین</b> کنید تا بتواند عضویت را بررسی کند.\n\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML, reply_markup=kb.back_home_kb())
        await q.answer(); return

    if action.startswith("delch:"):
        chat_id = action[len("delch:"):]
        db.remove_channel(chat_id)
        chans = db.list_channels()
        await q.edit_message_text("✅ کانال حذف شد.\n\n📢 <b>کانال‌های عضویت اجباری</b>",
                                  parse_mode=ParseMode.HTML, reply_markup=kb.channels_kb(chans))
        await q.answer("حذف شد"); return

    if action == "broadcast":
        pending_admin[uid] = "broadcast"
        await q.edit_message_text(
            "📣 <b>پیام همگانی</b>\n\n"
            "متن پیامی که می‌خواهید برای همه‌ی کاربران ارسال شود را بفرستید.\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML, reply_markup=kb.back_home_kb())
        await q.answer(); return

    if action == "senddb":
        await q.answer("در حال ارسال…")
        await _send_db_to(context, uid, caption="📤 فایل دیتابیس (ارسال دستی)")
        return

    if action == "restoredb":
        pending_admin[uid] = "restore_db"
        await q.edit_message_text(
            "♻️ <b>بازیابی دیتابیس</b>\n\n"
            "فایل دیتابیس (<code>bot.db</code>) را که قبلاً از ربات گرفته‌اید، "
            "همین‌جا <b>ارسال (آپلود)</b> کنید تا جایگزین شود.\n\n"
            "⚠️ داده‌های فعلی با فایل جدید جایگزین می‌شوند (یک نسخه‌ی .bak نگه داشته می‌شود).\n\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML, reply_markup=kb.back_home_kb())
        await q.answer(); return

    if action == "admins":
        admins = db.list_admins()
        await q.edit_message_text(
            "👤 <b>مدیریت ادمین‌ها</b>", parse_mode=ParseMode.HTML,
            reply_markup=kb.admins_kb(admins, _super_admin()))
        await q.answer(); return

    if action == "addadmin":
        if uid != _super_admin():
            await q.answer("فقط سوپر‌ادمین می‌تواند ادمین اضافه کند.", show_alert=True)
            return
        pending_admin[uid] = "add_admin"
        await q.edit_message_text(
            "➕ <b>افزودن ادمین</b>\n\nآیدی عددی کاربر را بفرستید.\n"
            "(کاربر می‌تواند آیدی خود را از رباتی مثل @userinfobot بگیرد.)\n\n"
            "برای لغو: /cancel",
            parse_mode=ParseMode.HTML, reply_markup=kb.back_home_kb())
        await q.answer(); return

    if action.startswith("deladmin:"):
        if uid != _super_admin():
            await q.answer("فقط سوپر‌ادمین مجاز است.", show_alert=True)
            return
        target = int(action[len("deladmin:"):])
        if target == _super_admin():
            await q.answer("سوپر‌ادمین قابل حذف نیست.", show_alert=True)
            return
        db.remove_admin(target)
        admins = db.list_admins()
        await q.edit_message_text("✅ ادمین حذف شد.\n\n👤 <b>مدیریت ادمین‌ها</b>",
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=kb.admins_kb(admins, _super_admin()))
        await q.answer("حذف شد"); return

    if action == "errors":
        errs = db.recent_errors(15)
        if not errs:
            body = "هیچ خطایی ثبت نشده. ✅"
        else:
            lines = []
            for e in errs:
                ts = datetime.fromtimestamp(e["ts"]).strftime("%m-%d %H:%M")
                lines.append(f"• <code>{ts}</code> [{e['context']}]\n  {e['message'][:120]}")
            body = "🐞 <b>آخرین خطاها</b>\n\n" + "\n".join(lines)
        try:
            await q.edit_message_text(body[:4000], parse_mode=ParseMode.HTML,
                                      reply_markup=kb.back_home_kb())
        except BadRequest:
            await q.message.reply_html(body[:4000], reply_markup=kb.back_home_kb())
        await q.answer(); return

    await q.answer()


# ---------------- ورودی متنی ادمین ----------------
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    uid = update.effective_user.id
    action = pending_admin.get(uid)
    if not action:
        return
    if text.strip() == "/cancel":
        pending_admin.pop(uid, None)
        await update.effective_message.reply_text("لغو شد.", reply_markup=kb.admin_panel_kb())
        return

    if action == "add_channel":
        parts = text.split()
        chat_id = parts[0].strip()
        invite = parts[1].strip() if len(parts) > 1 else ""
        # اعتبارسنجی ساده
        if not (chat_id.startswith("@") or chat_id.lstrip("-").isdigit()):
            await update.effective_message.reply_text(
                "شناسه نامعتبر است. مثال: @mychannel یا -1001234567890")
            return
        title = chat_id
        # تلاش برای گرفتن عنوان و لینک دعوت واقعی
        try:
            chat = await context.bot.get_chat(chat_id)
            title = chat.title or chat_id
            if not invite:
                if chat.username:
                    invite = f"https://t.me/{chat.username}"
                else:
                    try:
                        invite = await context.bot.export_chat_invite_link(chat_id)
                    except TelegramError:
                        invite = ""
        except TelegramError as e:
            await update.effective_message.reply_text(
                f"⚠️ نتوانستم به کانال دسترسی پیدا کنم: {e}\n"
                "مطمئن شوید ربات در کانال ادمین است. با این حال کانال ثبت شد.")
        db.add_channel(chat_id, title, invite)
        pending_admin.pop(uid, None)
        await update.effective_message.reply_text(
            f"✅ کانال «{title}» ثبت شد.", reply_markup=kb.admin_panel_kb())
        return

    if action == "add_admin":
        if not text.strip().lstrip("-").isdigit():
            await update.effective_message.reply_text("آیدی باید عددی باشد.")
            return
        db.add_admin(int(text.strip()))
        pending_admin.pop(uid, None)
        await update.effective_message.reply_text("✅ ادمین اضافه شد.",
                                                  reply_markup=kb.admin_panel_kb())
        return

    if action == "broadcast":
        pending_admin.pop(uid, None)
        await _do_broadcast(update, context, text)
        return


async def _do_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    ids = db.all_user_ids()
    await update.effective_message.reply_text(f"📣 در حال ارسال به {len(ids)} کاربر…")
    sent = failed = 0
    for i, cid in enumerate(ids):
        try:
            await context.bot.send_message(cid, text)
            sent += 1
        except Forbidden:
            db.set_blocked(cid, True); failed += 1
        except TelegramError:
            failed += 1
        if (i + 1) % 25 == 0:
            import asyncio
            await asyncio.sleep(1)  # رعایت محدودیت نرخ تلگرام
    await update.effective_message.reply_text(
        f"✅ ارسال تمام شد.\nموفق: {sent} | ناموفق: {failed}",
        reply_markup=kb.admin_panel_kb())


# ---------------- بکاپ و بازیابی دیتابیس ----------------
async def _send_db_to(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str) -> None:
    """ارسال بکاپ دیتابیس به ادمین.
    برای SQLite: فایل .db ارسال می‌شود.
    برای PostgreSQL: یک فایل SQL (dump) ارسال می‌شود.
    """
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M")

        if getattr(db, "db_type", "sqlite") == "postgres":
            # PostgreSQL: ساخت dump از دیتابیس
            try:
                # گرفتن dump از دیتابیس
                import psycopg2
                from psycopg2.extras import RealDictCursor
                import os as _os
                import json as _json

                db_url = _os.environ.get("DATABASE_URL", "")
                if "sslmode" not in db_url:
                    sep = "&" if "?" in db_url else "?"
                    db_url = f"{db_url}{sep}sslmode=require"

                tmp_conn = psycopg2.connect(db_url)
                tmp_cur = tmp_conn.cursor()

                # گرفتن لیست جدول‌ها
                tmp_cur.execute("""
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema='public' AND table_type='BASE TABLE'
                """)
                tables = [r[0] for r in tmp_cur.fetchall()]

                dump_lines = [
                    f"-- SilentMovie Bot PostgreSQL Backup",
                    f"-- Generated: {datetime.now().isoformat()}",
                    f"-- Database URL host: {_os.environ.get('DATABASE_URL', 'unknown').split('@')[-1] if db_url else 'unknown'}",
                    f"",
                ]

                for table in tables:
                    dump_lines.append(f"-- Table: {table}")
                    dump_lines.append(f"DELETE FROM {table};")

                    tmp_cur.execute(f"SELECT * FROM {table}")
                    cols = [d[0] for d in tmp_cur.description]
                    rows = tmp_cur.fetchall()

                    if rows:
                        col_list = ", ".join(cols)
                        for row in rows:
                            values = []
                            for v in row:
                                if v is None:
                                    values.append("NULL")
                                elif isinstance(v, (int, float)):
                                    values.append(str(v))
                                else:
                                    # escape برای SQL
                                    s = str(v).replace("'", "''")
                                    values.append(f"'{s}'")
                            dump_lines.append(
                                f"INSERT INTO {table} ({col_list}) VALUES ({', '.join(values)});"
                            )
                    dump_lines.append("")

                tmp_cur.close()
                tmp_conn.close()

                dump_text = "\n".join(dump_lines)
                data = dump_text.encode("utf-8")
                fname = f"bot_backup_{stamp}.sql"
            except Exception as dump_err:
                log.exception("postgres dump failed")
                # fallback: ارسال آمار به‌جای dump
                stats = db.stats()
                data = (
                    f"SilentMovie Bot - PostgreSQL Stats Backup\n"
                    f"Generated: {datetime.now().isoformat()}\n\n"
                    f"Stats: {stats}\n"
                ).encode("utf-8")
                fname = f"bot_backup_{stamp}.txt"
        else:
            # SQLite: ارسال فایل
            db.checkpoint()  # WAL را ادغام کن تا فایل کامل باشد
            with open(db.path, "rb") as f:
                data = f.read()
            fname = f"bot_backup_{stamp}.db"

        await context.bot.send_document(
            chat_id, document=InputFile(io.BytesIO(data), filename=fname),
            caption=caption + f"\n🕒 {stamp}\n📦 {len(data)//1024} KB")
    except Exception as e:
        log.exception("send db failed")
        db.log_error("send_db", str(e))
        try:
            await context.bot.send_message(chat_id, f"⚠️ ارسال دیتابیس ناموفق: {e}")
        except TelegramError:
            pass


async def handle_admin_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """اگر ادمین در حالت restore باشد و فایلی بفرستد، دیتابیس را جایگزین می‌کند."""
    uid = update.effective_user.id
    if not _is_admin(uid) or pending_admin.get(uid) != "restore_db":
        return
    doc = update.effective_message.document
    if not doc:
        return
    await update.effective_message.reply_text("⏳ در حال دریافت و بررسی فایل…")
    try:
        # بررسی نوع دیتابیس — اگر PostgreSQL است، swap_file کار نمی‌کند
        if getattr(db, "db_type", "sqlite") == "postgres":
            await update.effective_message.reply_text(
                "❌ بازیابی از فایل SQLite در حالت PostgreSQL پشتیبانی نمی‌شود.\n"
                "برای بازیابی، از pg_dump و psql استفاده کنید.")
            pending_admin.pop(uid, None)
            return

        tg_file = await doc.get_file()
        tmp_path = os.path.join(os.path.dirname(db.path) or ".", "restore_upload.db")
        await tg_file.download_to_drive(tmp_path)
        if not Database.is_sqlite_file(tmp_path):
            os.remove(tmp_path)
            await update.effective_message.reply_text(
                "❌ این فایل یک دیتابیس SQLite معتبر نیست.")
            return
        db.swap_file(tmp_path)
        os.remove(tmp_path)
        pending_admin.pop(uid, None)
        s = db.stats()
        await update.effective_message.reply_text(
            f"✅ دیتابیس با موفقیت بازیابی شد.\n"
            f"👥 کاربران: {s['users']} | 📢 کانال‌ها: {s['channels']} | "
            f"❤️ علاقه‌مندی‌ها: {s['favorites']}",
            reply_markup=kb.admin_panel_kb())
    except Exception as e:
        log.exception("restore failed")
        db.log_error("restore_db", str(e))
        await update.effective_message.reply_text(f"⚠️ بازیابی ناموفق: {e}")


async def job_send_db_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """جاب زمان‌بندی‌شده: هر N ساعت فایل دیتابیس را برای همه‌ی ادمین‌ها می‌فرستد."""
    admins = db.list_admins()
    if not admins:
        return
    for aid in admins:
        await _send_db_to(context, aid, caption="🔄 بکاپ خودکار دیتابیس")
    log.info("بکاپ خودکار برای %d ادمین ارسال شد", len(admins))
