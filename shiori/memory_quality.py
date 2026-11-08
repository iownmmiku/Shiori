"""Model hints that make an extracted memory useful later: a title, how much it
matters, how long it stays true, and its topics. Every hint is optional and
bounded locally; an unusable hint is dropped instead of failing the batch."""
from __future__ import annotations

import math
import re
import unicodedata
from datetime import datetime, timedelta, timezone

# How long a memory of each kind stays worth recalling, before importance.
DURABILITY_DAYS = {'days': 7, 'weeks': 30, 'months': 180, 'lasting': None}
DURABILITY_LABELS = {'days': '几天内', 'weeks': '几周内', 'months': '几个月内', 'lasting': '长期'}
# Recency half-life used when ranking recall results.
DURABILITY_HALF_LIFE = {'days': 7, 'weeks': 21, 'months': 60, 'lasting': 180}
DEFAULT_HALF_LIFE = 60
# The model may say what matters, but never claims the extremes reserved for
# what an administrator pins or rejects.
IMPORTANCE_RANGE = (0.1, 0.9)
NEAR_DUPLICATE = 0.82


def memory_hints(candidate):
    """Optional title/importance/durability/topics, each validated on its own."""
    hints = {}
    title = candidate.get('title')
    if isinstance(title, str) and 1 <= len(title.strip()) <= 40:
        hints['title'] = title.strip()
    importance = candidate.get('importance')
    if type(importance) in (int, float) and math.isfinite(importance) and 0 <= importance <= 1:
        low, high = IMPORTANCE_RANGE
        hints['importance'] = round(min(high, max(low, float(importance))), 2)
    if candidate.get('durability') in DURABILITY_DAYS:
        hints['durability'] = candidate['durability']
    topics = candidate.get('topics')
    if isinstance(topics, list):
        clean = [item.strip() for item in topics if isinstance(item, str) and 1 <= len(item.strip()) <= 20]
        if clean:
            hints['topics'] = list(dict.fromkeys(clean))[:4]
    return hints


def valid_until(durability, importance, anchor):
    """The end of a memory's useful life: longer when it matters more.

    `anchor` is when it was said (ISO text or datetime); lasting memories and
    unknown durations never expire on their own.
    """
    days = DURABILITY_DAYS.get(durability)
    if not days:
        return None
    if isinstance(anchor, str):
        try:
            anchor = datetime.fromisoformat(anchor.replace('Z', '+00:00'))
        except ValueError:
            anchor = None
    if not isinstance(anchor, datetime):
        anchor = datetime.now(timezone.utc)
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    weight = 0.5 + (importance if type(importance) in (int, float) else 0.5)
    end = anchor.astimezone(timezone.utc) + timedelta(days=days * weight)
    return end.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def half_life_days(record):
    return DURABILITY_HALF_LIFE.get(record.get('durability'), DEFAULT_HALF_LIFE)


def _grams(text):
    text = unicodedata.normalize('NFKC', str(text)).casefold()
    text = re.sub(r'[\W_]+', '', text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index:index + 2] for index in range(len(text) - 1)}


def similarity(left, right):
    """Character-pair overlap (Dice) of two texts, 0—1."""
    a, b = _grams(left), _grams(right)
    if not a or not b:
        return 0.0
    return 2 * len(a & b) / (len(a) + len(b))
