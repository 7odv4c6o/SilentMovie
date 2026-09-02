# -*- coding: utf-8 -*-
"""
webapp.py
----------
سرور Flask سبک برای Render Web Service (رایگان).
ربات تلگرام در یک thread جداگانه اجرا می‌شود و Flask فقط
یک health-check ساده روی پورت Render ارائه می‌دهد تا سرویس
زنده بماند.
"""
from __future__ import annotations

import logging
import os
import threading
import urllib.parse

from flask import Flask, Response

log = logging.getLogger(__name__)

# =================== Flask app ===================
flask_app = Flask(__name__)


@flask_app.route("/")
def index() -> Response:
    """Health-check ساده — Render این مسیر را چک می‌کند."""
    return Response("Bot is running", status=200, mimetype="text/plain")


@flask_app.route("/health")
def health() -> Response:
    """Health-check اضافی."""
    return Response("ok", status=200, mimetype="text/plain")


# =================== Player URL helper ===================
def build_player_url(video_url: str, title: str = "", episode: str = "") -> str:
    """لینک پخش را با سایت Vercel می‌پیچد."""
    base = os.environ.get("PLAYER_URL", "https://silentmoviebot.vercel.app").rstrip("/")
    params = {"url": video_url}
    if title:
        params["t"] = title
    if episode:
        params["e"] = episode
    qs = urllib.parse.urlencode(params, safe="/:@!$&'()*+,;=")
    return f"{base}/?{qs}"


# =================== Start bot in thread ===================
def _run_bot() -> None:
    """ربات را در thread جداگانه اجرا می‌کند."""
    try:
        from bot import main as bot_main
        bot_main()
    except Exception as e:
        log.critical("Bot thread crashed: %s", e, exc_info=True)


def start_player_server() -> None:
    """ربات را در یک thread و Flask را در thread اصلی اجرا می‌کند.

    این تابع از bot.py.main() فراخوانی می‌شود.
    """
    # ربات را در thread جداگانه شروع کن
    bot_thread = threading.Thread(target=_run_bot, daemon=True, name="telegram-bot")
    bot_thread.start()
    log.info("Thread ربات تلگرام شروع شد.")

    # Flask را در thread اصلی اجرا کن (Render منتظر پورت می‌ماند)
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)
