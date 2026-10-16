"""Тариф парковки «Акжол».

- первые free_minutes минут — бесплатно;
- дальше hour_price сом за каждый начатый час (15:00 -> 15:16 = 50, 16:01 = 100);
- после first_period_hours часов: стоимость первых суток + day_price за каждые начатые сутки
  (24 ч = 1200, 24 ч 01 мин = 1500, 48 ч 01 мин = 1800).
"""
import math

DEFAULT_TARIFF = {
    "free_minutes": 15,
    "hour_price": 50,
    "first_period_hours": 24,
    "day_price": 300,
}


def calc_fee(seconds, tariff=None):
    t = dict(DEFAULT_TARIFF)
    t.update(tariff or {})
    # считаем по целым минутам, как показано клиенту: 2 ч 00 мин 40 сек = 2 ч 00 мин
    seconds = max(0, int(seconds)) // 60 * 60
    if seconds <= t["free_minutes"] * 60:
        return 0
    first = t["first_period_hours"] * 3600
    if seconds <= first:
        return math.ceil(seconds / 3600) * t["hour_price"]
    first_cost = t["first_period_hours"] * t["hour_price"]
    return first_cost + math.ceil((seconds - first) / 86400) * t["day_price"]


def format_duration(seconds):
    seconds = max(0, int(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return "%d д %d ч %02d мин" % (days, hours, minutes)
    if hours:
        return "%d ч %02d мин" % (hours, minutes)
    return "%d мин" % minutes
