# -*- coding: utf-8 -*-
"""
categorize.py
--------------
dسته‌بندی خودکار اپیزودها بر اساس کیفیت و نوع (دوبله/زیرنویس/اصلی).
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from site_client import Episode


# ترتیب کیفیت‌ها (از بالا به پایین)
QUALITY_ORDER = ["2160p", "1080p", "720p", "480p", "360p"]
QUALITY_LABELS = {
    "2160p": "4K",
    "1080p": "1080p",
    "720p": "720p",
    "480p": "480p",
    "360p": "360p",
}

# ترتیب و لیبل نوع‌ها
TYPE_ORDER = ["dub", "sub", "original"]
TYPE_LABELS = [
    ("dub", "\U0001f508 دوبله"),
    ("sub", "\U0001f4ac زیرنویس"),
    ("original", "\U0001f3ac اصلی"),
]


def _detect_quality(ep: Episode) -> str:
    q = str(ep.quality or "").strip()
    if q.isdigit() and q in ("2160", "1080", "720", "480", "360"):
        return q + "p"
    label_lower = ep.label.lower()
    for q_name in QUALITY_ORDER:
        if q_name in label_lower:
            return q_name
    if q.isdigit():
        if int(q) >= 2160: return "2160p"
        if int(q) >= 1080: return "1080p"
        if int(q) >= 720: return "720p"
        if int(q) >= 480: return "480p"
        return "360p"
    return "other"


def _detect_type(ep: Episode) -> str:
    text = (ep.label + " " + ep.filename).lower()
    dub_kw = ["دوبله", "دوپله", "دوبله‌", "dubbed", "dub", "فارسی", "fa"]
    sub_kw = ["زیرنویس", "زیر نویس", "sub", "subtitle", "چسبیده"]
    for kw in dub_kw:
        if kw in text:
            return "dub"
    for kw in sub_kw:
        if kw in text:
            return "sub"
    return "original"


def categorize_episodes(episodes: List[Episode]) -> Dict[str, Dict[str, List[Episode]]]:
    """خروجی: {quality: {type: [ep, ...]}}"""
    cats: Dict[str, Dict[str, List[Episode]]] = {}
    for ep in episodes:
        quality = _detect_quality(ep)
        ep_type = _detect_type(ep)
        if quality not in cats:
            cats[quality] = {}
        if ep_type not in cats[quality]:
            cats[quality][ep_type] = []
        cats[quality][ep_type].append(ep)
    return cats


def categorize_with_indices(episodes: List[Episode]) -> Dict[str, Dict[str, List[Tuple[int, Episode]]]]:
    """خروجی: {quality: {type: [(original_index, ep), ...]}}
    ایندکس اصلی رو هم نگه می‌داره تا play_episode درست کار کنه."""
    cats: Dict[str, Dict[str, List[Tuple[int, Episode]]]] = {}
    for i, ep in enumerate(episodes):
        quality = _detect_quality(ep)
        ep_type = _detect_type(ep)
        if quality not in cats:
            cats[quality] = {}
        if ep_type not in cats[quality]:
            cats[quality][ep_type] = []
        cats[quality][ep_type].append((i, ep))
    return cats


def get_available_qualities(cats) -> List[str]:
    """لیست کیفیت‌های موجود (مرتب)."""
    return [q for q in QUALITY_ORDER if q in cats]


def get_available_types(groups: dict) -> List[str]:
    """لیست نوع‌های موجود برای یه کیفیت (مرتب)."""
    return [t for t, _ in TYPE_LABELS if t in groups and groups[t]]


def count_quality(cats, quality: str) -> int:
    """تعداد کل لینک‌های یه کیفیت."""
    g = cats.get(quality, {})
    return sum(len(v) for v in g.values())


def count_type(groups: dict, ep_type: str) -> int:
    """تعداد لینک‌های یه نوع."""
    return len(groups.get(ep_type, []))
