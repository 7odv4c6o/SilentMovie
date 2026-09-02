# -*- coding: utf-8 -*-
"""
database.py
-----------
لایه‌ی دیتابیس ربات با پشتیبانی همزمان از PostgreSQL و SQLite.

انتخاب نوع دیتابیس:
  • اگر متغیر محیطی DATABASE_URL تنظیم شده باشد (و با postgres:// شروع شود)،
    از PostgreSQL استفاده می‌شود.
  • در غیر این‌صورت، از SQLite (فایل محلی) استفاده می‌شود.

جدول‌ها:
  users            : کاربران ربات
  channels         : کانال‌های عضویت اجباری (ادمین اضافه/حذف می‌کند)
  favorites        : فیلم‌های نشان‌شده‌ی هر کاربر
  movie_cache      : کش اطلاعات فیلم برای کاهش درخواست به سایت
  admins           : ادمین‌های ربات
  settings         : تنظیمات کلید/مقدار
  error_log        : لاگ خطاها
  search_log       : لاگ جستجوهای کاربران
  watch_history    : تاریخچه‌ی تماشای کاربر

مثال PostgreSQL (Aiven):
  postgres://avnadmin:PASSWORD@HOST:PORT/defaultdb?sslmode=require
"""
from __future__ import annotations

import os
import threading
import time
import json
from typing import Any, List, Optional, Tuple

DEFAULT_DB = "data/bot.db"


def _detect_db_type(path: str) -> str:
    """تشخیص نوع دیتابیس بر اساس DATABASE_URL یا مسیر فایل."""
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url:
        if db_url.startswith("postgres://") or db_url.startswith("postgresql://"):
            return "postgres"
    return "sqlite"


class Database:
    """دیتابیس با پشتیبانی همزمان از PostgreSQL و SQLite."""

    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        self._lock = threading.RLock()
        self.db_type = _detect_db_type(path)

        if self.db_type == "postgres":
            self._init_postgres()
        else:
            self._init_sqlite()

        self._init_schema()

    # ---------------- راه‌اندازی PostgreSQL ----------------
    def _init_postgres(self) -> None:
        """اتصال به PostgreSQL با استفاده از DATABASE_URL."""
        try:
            import psycopg2
            from psycopg2.extras import RealDictCursor
        except ImportError:
            raise RuntimeError(
                "psycopg2 نصب نیست. برای استفاده از PostgreSQL:\n"
                "  pip install psycopg2-binary\n"
                "یا در requirements.txt اضافه کنید: psycopg2-binary"
            )

        db_url = os.environ.get("DATABASE_URL")
        if not db_url:
            raise RuntimeError("DATABASE_URL تنظیم نشده است")

        # اطمینان از ssl-mode
        # Aiven نیاز به sslmode=require دارد
        if "sslmode" not in db_url:
            sep = "&" if "?" in db_url else "?"
            db_url = f"{db_url}{sep}sslmode=require"

        try:
            self.conn = psycopg2.connect(db_url)
            self.conn.autocommit = True
            self._RealDictCursor = RealDictCursor
            print(f"✅ اتصال به PostgreSQL برقرار شد")
        except Exception as e:
            raise RuntimeError(f"خطا در اتصال به PostgreSQL: {e}")

    # ---------------- راه‌اندازی SQLite ----------------
    def _init_sqlite(self) -> None:
        """اتصال به SQLite (فایل محلی)."""
        import sqlite3
        # ساخت پوشه اگر وجود ندارد
        db_dir = os.path.dirname(self.path) or "."
        os.makedirs(db_dir, exist_ok=True)

        # check_same_thread=False چون PTB ممکن است از تردهای مختلف صدا بزند؛
        # خودمان با قفل هماهنگ می‌کنیم.
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        print(f"✅ اتصال به SQLite برقرار شد: {self.path}")

    # ---------------- ساخت جدول‌ها ----------------
    def _init_schema(self) -> None:
        """ساخت جدول‌ها در دیتابیس (PostgreSQL یا SQLite)."""
        with self._lock:
            cur = self.conn.cursor()

            if self.db_type == "postgres":
                # PostgreSQL: استفاده از SERIAL و نوع‌های استاندارد
                statements = [
                    """CREATE TABLE IF NOT EXISTS users (
                        user_id     BIGINT PRIMARY KEY,
                        username    TEXT,
                        first_name  TEXT,
                        joined_at   BIGINT,
                        last_seen   BIGINT,
                        is_blocked  SMALLINT DEFAULT 0
                    )""",
                    """CREATE TABLE IF NOT EXISTS channels (
                        id          SERIAL PRIMARY KEY,
                        chat_id     TEXT UNIQUE,
                        title       TEXT,
                        invite_link TEXT,
                        added_at    BIGINT
                    )""",
                    """CREATE TABLE IF NOT EXISTS favorites (
                        user_id     BIGINT,
                        movie_id    TEXT,
                        title       TEXT,
                        added_at    BIGINT,
                        PRIMARY KEY (user_id, movie_id)
                    )""",
                    """CREATE TABLE IF NOT EXISTS movie_cache (
                        movie_id    TEXT PRIMARY KEY,
                        payload     TEXT,
                        cached_at   BIGINT
                    )""",
                    """CREATE TABLE IF NOT EXISTS admins (
                        user_id     BIGINT PRIMARY KEY,
                        added_at    BIGINT
                    )""",
                    """CREATE TABLE IF NOT EXISTS settings (
                        key         TEXT PRIMARY KEY,
                        value       TEXT
                    )""",
                    """CREATE TABLE IF NOT EXISTS error_log (
                        id          SERIAL PRIMARY KEY,
                        ts          BIGINT,
                        context     TEXT,
                        message     TEXT
                    )""",
                    """CREATE TABLE IF NOT EXISTS search_log (
                        id          SERIAL PRIMARY KEY,
                        user_id     BIGINT,
                        query       TEXT,
                        ts          BIGINT
                    )""",
                    """CREATE TABLE IF NOT EXISTS watch_history (
                        user_id     BIGINT,
                        movie_id    TEXT,
                        title       TEXT,
                        episode     TEXT,
                        ts          BIGINT,
                        PRIMARY KEY (user_id, movie_id)
                    )""",
                    # جداول آرشیو فیلم و سریال ایرانی (توسط sync_archive.py پر می‌شوند)
                    """CREATE TABLE IF NOT EXISTS iranian_movies (
                        id          SERIAL PRIMARY KEY,
                        title       TEXT,
                        page_url    TEXT UNIQUE,
                        poster      TEXT,
                        direct_link TEXT,
                        scanned_at  BIGINT,
                        type        TEXT DEFAULT 'movie'
                    )""",
                    """CREATE TABLE IF NOT EXISTS iranian_series (
                        id          SERIAL PRIMARY KEY,
                        title       TEXT,
                        page_url    TEXT UNIQUE,
                        poster      TEXT,
                        scanned_at  BIGINT,
                        type        TEXT DEFAULT 'series'
                    )""",
                    """CREATE TABLE IF NOT EXISTS iranian_episodes (
                        id          SERIAL PRIMARY KEY,
                        series_url  TEXT,
                        season      TEXT,
                        episode     TEXT,
                        title       TEXT,
                        onlineplay_url TEXT,
                        direct_link TEXT,
                        scanned_at  BIGINT
                    )""",
                    # ایندکس برای جستجوی سریع
                    """CREATE INDEX IF NOT EXISTS idx_iranian_movies_title ON iranian_movies(title)""",
                    """CREATE INDEX IF NOT EXISTS idx_iranian_series_title ON iranian_series(title)""",
                    """CREATE INDEX IF NOT EXISTS idx_iranian_episodes_series ON iranian_episodes(series_url)""",
                ]
                for stmt in statements:
                    cur.execute(stmt)
            else:
                # SQLite: استفاده از SQL قبلی
                cur.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id     INTEGER PRIMARY KEY,
                        username    TEXT,
                        first_name  TEXT,
                        joined_at   INTEGER,
                        last_seen   INTEGER,
                        is_blocked  INTEGER DEFAULT 0
                    );
                    CREATE TABLE IF NOT EXISTS channels (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id     TEXT UNIQUE,
                        title       TEXT,
                        invite_link TEXT,
                        added_at    INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS favorites (
                        user_id     INTEGER,
                        movie_id    TEXT,
                        title       TEXT,
                        added_at    INTEGER,
                        PRIMARY KEY (user_id, movie_id)
                    );
                    CREATE TABLE IF NOT EXISTS movie_cache (
                        movie_id    TEXT PRIMARY KEY,
                        payload     TEXT,
                        cached_at   INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS admins (
                        user_id     INTEGER PRIMARY KEY,
                        added_at    INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS settings (
                        key         TEXT PRIMARY KEY,
                        value       TEXT
                    );
                    CREATE TABLE IF NOT EXISTS error_log (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts          INTEGER,
                        context     TEXT,
                        message     TEXT
                    );
                    CREATE TABLE IF NOT EXISTS search_log (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id     INTEGER,
                        query       TEXT,
                        ts          INTEGER
                    );
                    CREATE TABLE IF NOT EXISTS watch_history (
                        user_id     INTEGER,
                        movie_id    TEXT,
                        title       TEXT,
                        episode     TEXT,
                        ts          INTEGER,
                        PRIMARY KEY (user_id, movie_id)
                    );
                    CREATE TABLE IF NOT EXISTS iranian_movies (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        title       TEXT,
                        page_url    TEXT UNIQUE,
                        poster      TEXT,
                        direct_link TEXT,
                        scanned_at  INTEGER,
                        type        TEXT DEFAULT 'movie'
                    );
                    CREATE TABLE IF NOT EXISTS iranian_series (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        title       TEXT,
                        page_url    TEXT UNIQUE,
                        poster      TEXT,
                        scanned_at  INTEGER,
                        type        TEXT DEFAULT 'series'
                    );
                    CREATE TABLE IF NOT EXISTS iranian_episodes (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        series_url  TEXT,
                        season      TEXT,
                        episode     TEXT,
                        title       TEXT,
                        onlineplay_url TEXT,
                        direct_link TEXT,
                        scanned_at  INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_iranian_movies_title ON iranian_movies(title);
                    CREATE INDEX IF NOT EXISTS idx_iranian_series_title ON iranian_series(title);
                    CREATE INDEX IF NOT EXISTS idx_iranian_episodes_series ON iranian_episodes(series_url);
                    """
                )
            if self.db_type == "sqlite":
                self.conn.commit()

    # ---------------- کمک‌کننده‌ها ----------------
    def _execute(self, query: str, params: Tuple = ()) -> Any:
        """اجرای کوئری با تطبیق پارامترها (? برای SQLite، %s برای PostgreSQL).
        برای PostgreSQL، یک cursor برمی‌گرداند (مثل conn.execute در SQLite).
        """
        if self.db_type == "postgres":
            # تبدیل ? به %s
            query = query.replace("?", "%s")
            cur = self.conn.cursor()
            cur.execute(query, params)
            return cur
        else:
            return self.conn.execute(query, params)

    def _fetchone(self, query: str, params: Tuple = ()):
        """اجرای کوئری و برگرداندن یک ردیف به‌صورت dict."""
        with self._lock:
            if self.db_type == "postgres":
                query = query.replace("?", "%s")
                cur = self.conn.cursor(cursor_factory=self._RealDictCursor)
                cur.execute(query, params)
                row = cur.fetchone()
                return dict(row) if row else None
            else:
                row = self.conn.execute(query, params).fetchone()
                return dict(row) if row else None

    def _fetchall(self, query: str, params: Tuple = ()) -> List[dict]:
        """اجرای کوئری و برگرداندن همه‌ی ردیف‌ها به‌صورت dict."""
        with self._lock:
            if self.db_type == "postgres":
                query = query.replace("?", "%s")
                cur = self.conn.cursor(cursor_factory=self._RealDictCursor)
                cur.execute(query, params)
                return [dict(r) for r in cur.fetchall()]
            else:
                rows = self.conn.execute(query, params).fetchall()
                return [dict(r) for r in rows]

    # ---------------- کاربران ----------------
    def upsert_user(self, user_id: int, username: str, first_name: str) -> None:
        now = int(time.time())
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
                       VALUES(%s,%s,%s,%s,%s)
                       ON CONFLICT(user_id) DO UPDATE SET
                         username=EXCLUDED.username,
                         first_name=EXCLUDED.first_name,
                         last_seen=EXCLUDED.last_seen""",
                    (user_id, username, first_name, now, now),
                )
            else:
                self._execute(
                    """INSERT INTO users(user_id, username, first_name, joined_at, last_seen)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(user_id) DO UPDATE SET
                         username=excluded.username,
                         first_name=excluded.first_name,
                         last_seen=excluded.last_seen""",
                    (user_id, username, first_name, now, now),
                )
                self.conn.commit()

    def count_users(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS c FROM users")
        return row["c"] if row else 0

    def all_user_ids(self) -> List[int]:
        rows = self._fetchall("SELECT user_id FROM users WHERE is_blocked=0")
        return [r["user_id"] for r in rows]

    def set_blocked(self, user_id: int, blocked: bool) -> None:
        with self._lock:
            self._execute("UPDATE users SET is_blocked=? WHERE user_id=?",
                          (1 if blocked else 0, user_id))
            if self.db_type == "sqlite":
                self.conn.commit()

    # ---------------- ادمین‌ها ----------------
    def add_admin(self, user_id: int) -> None:
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    "INSERT INTO admins(user_id, added_at) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, int(time.time())))
            else:
                self._execute(
                    "INSERT OR IGNORE INTO admins(user_id, added_at) VALUES(?,?)",
                    (user_id, int(time.time())))
                self.conn.commit()

    def remove_admin(self, user_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM admins WHERE user_id=?", (user_id,))
            if self.db_type == "sqlite":
                self.conn.commit()

    def is_admin(self, user_id: int) -> bool:
        return self._fetchone("SELECT 1 FROM admins WHERE user_id=?", (user_id,)) is not None

    def list_admins(self) -> List[int]:
        rows = self._fetchall("SELECT user_id FROM admins")
        return [r["user_id"] for r in rows]

    # ---------------- کانال‌های عضویت اجباری ----------------
    def add_channel(self, chat_id: str, title: str = "", invite_link: str = "") -> None:
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO channels(chat_id, title, invite_link, added_at) VALUES(%s,%s,%s,%s)
                       ON CONFLICT(chat_id) DO UPDATE SET title=EXCLUDED.title,
                         invite_link=EXCLUDED.invite_link""",
                    (chat_id, title, invite_link, int(time.time())))
            else:
                self._execute(
                    """INSERT INTO channels(chat_id, title, invite_link, added_at) VALUES(?,?,?,?)
                       ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,
                         invite_link=excluded.invite_link""",
                    (chat_id, title, invite_link, int(time.time())))
                self.conn.commit()

    def remove_channel(self, chat_id: str) -> bool:
        with self._lock:
            cur = self._execute("DELETE FROM channels WHERE chat_id=?", (chat_id,))
            if self.db_type == "sqlite":
                self.conn.commit()
                return cur.rowcount > 0
            else:
                return cur.rowcount > 0

    def list_channels(self) -> List[dict]:
        return self._fetchall("SELECT * FROM channels ORDER BY id")

    # ---------------- علاقه‌مندی‌ها ----------------
    def add_favorite(self, user_id: int, movie_id: str, title: str) -> None:
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    "INSERT INTO favorites(user_id, movie_id, title, added_at) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (user_id, movie_id, title, int(time.time())))
            else:
                self._execute(
                    "INSERT OR IGNORE INTO favorites(user_id, movie_id, title, added_at) VALUES(?,?,?,?)",
                    (user_id, movie_id, title, int(time.time())))
                self.conn.commit()

    def remove_favorite(self, user_id: int, movie_id: str) -> None:
        with self._lock:
            self._execute("DELETE FROM favorites WHERE user_id=? AND movie_id=?",
                          (user_id, movie_id))
            if self.db_type == "sqlite":
                self.conn.commit()

    def is_favorite(self, user_id: int, movie_id: str) -> bool:
        return self._fetchone(
            "SELECT 1 FROM favorites WHERE user_id=? AND movie_id=?",
            (user_id, movie_id)) is not None

    def list_favorites(self, user_id: int) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM favorites WHERE user_id=? ORDER BY added_at DESC",
            (user_id,))

    # ---------------- کش فیلم ----------------
    def cache_get(self, movie_id: str, max_age: int) -> Optional[str]:
        row = self._fetchone("SELECT payload, cached_at FROM movie_cache WHERE movie_id=?",
                             (movie_id,))
        if row and (int(time.time()) - row["cached_at"] <= max_age):
            return row["payload"]
        return None

    def cache_put(self, movie_id: str, payload: str) -> None:
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO movie_cache(movie_id, payload, cached_at) VALUES(%s,%s,%s)
                       ON CONFLICT(movie_id) DO UPDATE SET payload=EXCLUDED.payload,
                         cached_at=EXCLUDED.cached_at""",
                    (movie_id, payload, int(time.time())))
            else:
                self._execute(
                    """INSERT INTO movie_cache(movie_id, payload, cached_at) VALUES(?,?,?)
                       ON CONFLICT(movie_id) DO UPDATE SET payload=excluded.payload,
                         cached_at=excluded.cached_at""",
                    (movie_id, payload, int(time.time())))
                self.conn.commit()

    # ---------------- تنظیمات ----------------
    def get_setting(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self._fetchone("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO settings(key, value) VALUES(%s,%s)
                       ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value""",
                    (key, value))
            else:
                self._execute(
                    """INSERT INTO settings(key, value) VALUES(?,?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                    (key, value))
                self.conn.commit()

    # ---------------- لاگ ----------------
    def log_error(self, context: str, message: str) -> None:
        with self._lock:
            self._execute("INSERT INTO error_log(ts, context, message) VALUES(?,?,?)",
                          (int(time.time()), context, message[:2000]))
            if self.db_type == "sqlite":
                self.conn.commit()

    def recent_errors(self, limit: int = 15) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM error_log ORDER BY id DESC LIMIT ?", (limit,))

    def log_search(self, user_id: int, query: str) -> None:
        with self._lock:
            self._execute("INSERT INTO search_log(user_id, query, ts) VALUES(?,?,?)",
                          (user_id, query, int(time.time())))
            if self.db_type == "sqlite":
                self.conn.commit()

    def recent_searches(self, user_id: int, limit: int = 10) -> List[str]:
        """آخرین جستجوهای یکتای کاربر (جدیدترین اول)."""
        rows = self._fetchall(
            """SELECT query, MAX(ts) AS mts FROM search_log
               WHERE user_id=? AND query<>''
               GROUP BY query ORDER BY mts DESC LIMIT ?""",
            (user_id, limit))
        return [r["query"] for r in rows]

    def clear_searches(self, user_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM search_log WHERE user_id=?", (user_id,))
            if self.db_type == "sqlite":
                self.conn.commit()

    # ---------------- تاریخچه‌ی تماشا ----------------
    def add_watch(self, user_id: int, movie_id: str, title: str, episode: str = "") -> None:
        """ثبت/به‌روزرسانی آخرین تماشای کاربر از یک فیلم."""
        with self._lock:
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO watch_history(user_id, movie_id, title, episode, ts)
                       VALUES(%s,%s,%s,%s,%s)
                       ON CONFLICT(user_id, movie_id) DO UPDATE SET
                         title=EXCLUDED.title, episode=EXCLUDED.episode, ts=EXCLUDED.ts""",
                    (user_id, movie_id, title, episode, int(time.time())))
            else:
                self._execute(
                    """INSERT INTO watch_history(user_id, movie_id, title, episode, ts)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(user_id, movie_id) DO UPDATE SET
                         title=excluded.title, episode=excluded.episode, ts=excluded.ts""",
                    (user_id, movie_id, title, episode, int(time.time())))
                self.conn.commit()

    def list_watch(self, user_id: int, limit: int = 15) -> List[dict]:
        return self._fetchall(
            "SELECT * FROM watch_history WHERE user_id=? ORDER BY ts DESC LIMIT ?",
            (user_id, limit))

    def clear_watch(self, user_id: int) -> None:
        with self._lock:
            self._execute("DELETE FROM watch_history WHERE user_id=?", (user_id,))
            if self.db_type == "sqlite":
                self.conn.commit()

    def stats(self) -> dict:
        return {
            "users": self._fetchone("SELECT COUNT(*) AS x FROM users")["x"],
            "blocked": self._fetchone("SELECT COUNT(*) AS x FROM users WHERE is_blocked=1")["x"],
            "channels": self._fetchone("SELECT COUNT(*) AS x FROM channels")["x"],
            "favorites": self._fetchone("SELECT COUNT(*) AS x FROM favorites")["x"],
            "searches": self._fetchone("SELECT COUNT(*) AS x FROM search_log")["x"],
            "cached": self._fetchone("SELECT COUNT(*) AS x FROM movie_cache")["x"],
            "errors": self._fetchone("SELECT COUNT(*) AS x FROM error_log")["x"],
        }

    def checkpoint(self) -> None:
        """برای SQLite: WAL را در فایل اصلی ادغام می‌کند. برای PostgreSQL: no-op."""
        if self.db_type == "sqlite":
            with self._lock:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.conn.commit()

    def swap_file(self, new_path: str) -> None:
        """جایگزینی فایل دیتابیس SQLite با فایل جدید (بکاپ).
        برای PostgreSQL کار نمی‌کند — باید از pg_dump استفاده کنید.
        """
        if self.db_type == "postgres":
            raise NotImplementedError(
                "swap_file برای PostgreSQL پشتیبانی نمی‌شود. "
                "برای بازیابی، از pg_dump و psql استفاده کنید."
            )
        import shutil
        with self._lock:
            self.checkpoint()
            self.conn.close()
            # پاک‌سازی فایل‌های جانبی WAL/SHM
            for suffix in ("-wal", "-shm"):
                p = self.path + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            # بکاپ فایل فعلی
            if os.path.exists(self.path):
                shutil.copy2(self.path, self.path + ".bak")
            shutil.copy2(new_path, self.path)
            # بازگشایی
            self._init_sqlite()
            self._init_schema()

    @staticmethod
    def is_sqlite_file(path: str) -> bool:
        """بررسی می‌کند فایل واقعاً یک دیتابیس SQLite است."""
        try:
            with open(path, "rb") as f:
                return f.read(16) == b"SQLite format 3\x00"
        except OSError:
            return False

    # ---------------- آرشیو فیلم و سریال ایرانی ----------------

    def upsert_iranian_movie(self, title: str, page_url: str,
                              poster: str = "", direct_link: str = "",
                              content_type: str = "movie") -> None:
        """درج یا به‌روزرسانی یک فیلم/سریال ایرانی.
        اگر page_url از قبل وجود داشته باشد، فقط direct_link را به‌روزرسانی می‌کند.
        """
        now = int(time.time())
        with self._lock:
            if self.db_type == "postgres":
                if content_type == "movie":
                    self._execute(
                        """INSERT INTO iranian_movies (title, page_url, poster, direct_link, scanned_at, type)
                           VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (page_url) DO UPDATE SET
                             direct_link = COALESCE(NULLIF(EXCLUDED.direct_link, ''), iranian_movies.direct_link),
                             poster = COALESCE(NULLIF(EXCLUDED.poster, ''), iranian_movies.poster),
                             scanned_at = EXCLUDED.scanned_at""",
                        (title, page_url, poster, direct_link, now, content_type))
                else:
                    self._execute(
                        """INSERT INTO iranian_series (title, page_url, poster, scanned_at, type)
                           VALUES (%s,%s,%s,%s,%s)
                           ON CONFLICT (page_url) DO UPDATE SET
                             poster = COALESCE(NULLIF(EXCLUDED.poster, ''), iranian_series.poster),
                             scanned_at = EXCLUDED.scanned_at""",
                        (title, page_url, poster, now, content_type))
            else:
                if content_type == "movie":
                    self._execute(
                        """INSERT INTO iranian_movies (title, page_url, poster, direct_link, scanned_at, type)
                           VALUES (?,?,?,?,?,?)
                           ON CONFLICT(page_url) DO UPDATE SET
                             direct_link = CASE WHEN excluded.direct_link <> '' THEN excluded.direct_link
                                                ELSE iranian_movies.direct_link END,
                             poster = CASE WHEN excluded.poster <> '' THEN excluded.poster
                                          ELSE iranian_movies.poster END,
                             scanned_at = excluded.scanned_at""",
                        (title, page_url, poster, direct_link, now, content_type))
                else:
                    self._execute(
                        """INSERT INTO iranian_series (title, page_url, poster, scanned_at, type)
                           VALUES (?,?,?,?,?)
                           ON CONFLICT(page_url) DO UPDATE SET
                             poster = CASE WHEN excluded.poster <> '' THEN excluded.poster
                                          ELSE iranian_series.poster END,
                             scanned_at = excluded.scanned_at""",
                        (title, page_url, poster, now, content_type))
                self.conn.commit()

    def list_iranian_movies(self, content_type: str = "movie") -> List[dict]:
        """لیست تمام فیلم‌های ایرانی از دیتابیس."""
        return self._fetchall(
            "SELECT title, page_url, poster, direct_link FROM iranian_movies WHERE type = ? ORDER BY title",
            (content_type,))

    def list_iranian_series(self) -> List[dict]:
        """لیست تمام سریال‌های ایرانی از دیتابیس."""
        return self._fetchall(
            "SELECT title, page_url, poster FROM iranian_series ORDER BY title")

    def get_iranian_movie(self, page_url: str) -> Optional[dict]:
        """گرفتن اطلاعات یک فیلم ایرانی از دیتابیس."""
        return self._fetchone(
            "SELECT title, page_url, poster, direct_link FROM iranian_movies WHERE page_url = ?",
            (page_url,))

    def get_iranian_series(self, page_url: str) -> Optional[dict]:
        """گرفتن اطلاعات یک سریال ایرانی از دیتابیس."""
        return self._fetchone(
            "SELECT title, page_url, poster FROM iranian_series WHERE page_url = ?",
            (page_url,))

    def get_pending_movies(self) -> List[dict]:
        """لیست فیلم‌هایی که هنوز direct_link ندارند (نیاز به اسکن با Selenium)."""
        return self._fetchall(
            "SELECT title, page_url FROM iranian_movies WHERE direct_link IS NULL OR direct_link = ''")

    def get_pending_series(self) -> List[dict]:
        """لیست سریال‌هایی که هنوز قسمت‌هایشان اسکن نشده."""
        # سریال‌هایی که در جدول iranian_episodes هیچ رکوردی ندارند
        return self._fetchall(
            """SELECT s.title, s.page_url FROM iranian_series s
               WHERE NOT EXISTS (
                   SELECT 1 FROM iranian_episodes e WHERE e.series_url = s.page_url
               )""")

    def upsert_iranian_episode(self, series_url: str, season: str, episode: str,
                                title: str, onlineplay_url: str,
                                direct_link: str = "") -> None:
        """درج یا به‌روزرسانی یک قسمت سریال ایرانی."""
        now = int(time.time())
        with self._lock:
            # حذف قسمت‌های قبلی این سریال با همان onlineplay_url
            if self.db_type == "postgres":
                self._execute(
                    """INSERT INTO iranian_episodes
                       (series_url, season, episode, title, onlineplay_url, direct_link, scanned_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING""",
                    (series_url, season, episode, title, onlineplay_url, direct_link, now))
            else:
                # SQLite: چک کن وجود دارد یا نه
                existing = self._fetchone(
                    "SELECT id FROM iranian_episodes WHERE series_url = ? AND onlineplay_url = ?",
                    (series_url, onlineplay_url))
                if not existing:
                    self._execute(
                        """INSERT INTO iranian_episodes
                           (series_url, season, episode, title, onlineplay_url, direct_link, scanned_at)
                           VALUES (?,?,?,?,?,?,?)""",
                        (series_url, season, episode, title, onlineplay_url, direct_link, now))
                self.conn.commit()

    def list_iranian_episodes(self, series_url: str) -> List[dict]:
        """لیست تمام قسمت‌های یک سریال ایرانی از دیتابیس."""
        return self._fetchall(
            """SELECT season, episode, title, onlineplay_url, direct_link
               FROM iranian_episodes WHERE series_url = ?
               ORDER BY
                 CASE WHEN season ~ '^[0-9]+$' THEN CAST(season AS INTEGER) ELSE 999 END,
                 CASE WHEN episode ~ '^[0-9]+$' THEN CAST(episode AS INTEGER) ELSE 999 END""",
            (series_url,)) if self.db_type == "postgres" else self._fetchall(
            """SELECT season, episode, title, onlineplay_url, direct_link
               FROM iranian_episodes WHERE series_url = ?
               ORDER BY CAST(season AS INTEGER), CAST(episode AS INTEGER)""",
            (series_url,))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
