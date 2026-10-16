"""Нормализация и сравнение госномеров."""
import re

# Кириллица, похожая на латиницу (камеры и операторы иногда путают раскладку).
_CYR = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")
_BAD = {"", "UNKNOWN", "NOPLATE", "NONE", "NULL"}


def normalize(plate):
    p = (plate or "").upper().translate(_CYR)
    return re.sub(r"[^0-9A-Z]", "", p)


def key(plate):
    """Ключ для поиска: без пробелов и без ведущих нулей (6978ATH == 06978ATH)."""
    n = normalize(plate)
    return n.lstrip("0") or n


def is_valid(plate):
    n = normalize(plate)
    return n not in _BAD and len(n) >= 4 and any(c.isdigit() for c in n)


def close_match(a, b):
    """Номера отличаются не более чем одним символом (замена, вставка или удаление)."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(1 for x, y in zip(a, b) if x != y) == 1
    if la > lb:
        a, b = b, a
    # b длиннее на один символ
    for i in range(len(b)):
        if b[:i] + b[i + 1:] == a:
            return True
    return False
