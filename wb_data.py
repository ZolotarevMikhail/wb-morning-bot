# Данные Wildberries для бота «Утренняя сводка» (шаг 2)
# Официальный API статистики WB: продажи, заказы, остатки.
# Токен читается из .env, в код и git не попадает.
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

MSK = timezone(timedelta(hours=3))  # WB живёт по московскому времени
BASE = "https://statistics-api.wildberries.ru"
LOW_STOCK = 5  # порог «заканчивается», штук на всех складах


def _get(url, params, token):
    """GET с автоповтором: 429 (лимит WB — 1 запрос/мин на метод) и сетевые сбои."""
    last_exc = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, headers={"Authorization": token}, params=params, timeout=30)
            if r.status_code != 429:
                break
            if attempt == 2:
                raise RuntimeError("WB просит не спешить (лимит запросов). Подожди минуту и повтори /summary.")
            print("429 от WB — жду 65 секунд и пробую ещё раз...", flush=True)
            time.sleep(65)
        except requests.RequestException as e:
            last_exc = e
            if attempt == 2:
                break
            print("Сетевая ошибка, повторяю через 5 секунд...", flush=True)
            time.sleep(5)
    if last_exc is not None:
        raise RuntimeError(f"Сеть не отвечает при запросе к WB: {last_exc}")
    r.raise_for_status()
    return r.json()


def _day_bounds(day: datetime):
    # WB хочет дату без суффикса часового пояса: 2026-09-14T00:00:00
    fmt = "%Y-%m-%dT%H:%M:%S"
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.strftime(fmt), (start + timedelta(days=1)).strftime(fmt)


def _sales_for_range(days, token):
    """Продажи за последние `days` дней ОДНИМ запросом (лимит WB — 1 запрос/мин).
    Возвращает {день (YYYY-MM-DD): {revenue, count, by_product}}."""
    date_from, _ = _day_bounds(datetime.now(MSK) - timedelta(days=days))
    rows = _get(f"{BASE}/api/v1/supplier/sales", {"dateFrom": date_from, "flag": 0}, token)
    by_day = {}
    for row in rows:
        day = (row.get("date") or "")[:10]  # 2026-09-14 из даты продажи
        if not day:
            continue
        d = by_day.setdefault(day, {"revenue": 0.0, "count": 0, "by_product": {}})
        pay = float(row.get("forPay") or 0)  # к получению с учётом комиссии
        d["revenue"] += pay
        d["count"] += 1
        name = row.get("subject") or row.get("nmId")
        d["by_product"][name] = d["by_product"].get(name, 0.0) + pay
    return by_day


def _orders_count_range(days, token):
    """Новые заказы за последние `days` дней ОДНИМ запросом.
    Возвращает {день (YYYY-MM-DD): число заказов}."""
    date_from, _ = _day_bounds(datetime.now(MSK) - timedelta(days=days))
    rows = _get(f"{BASE}/api/v1/supplier/orders", {"dateFrom": date_from, "flag": 0}, token)
    by_day = {}
    for row in rows:
        day = (row.get("date") or "")[:10]
        if day:
            by_day[day] = by_day.get(day, 0) + 1
    return by_day


ANALYTICS = "https://seller-analytics-api.wildberries.ru"
CONTENT = "https://content-api.wildberries.ru"


def _product_names(token):
    """{nmId: название} из Content API. Если метод недоступен — пустой словарь
    (тогда в сводке будут артикулы «товар 7758367»)."""
    names = {}
    cursor = {"limit": 100}
    tries_429 = 0
    while True:
        r = requests.post(
            f"{CONTENT}/content/v2/get/cards/list",
            headers={"Authorization": token, "Content-Type": "application/json"},
            json={"settings": {"filter": {"withPhoto": -1}, "cursor": cursor}},
            timeout=60,
        )
        if r.status_code == 429:
            tries_429 += 1
            if tries_429 > 3:
                print("Content API всё время отвечает 429 — покажу артикулы.", flush=True)
                return names
            time.sleep(10)
            continue
        if not r.ok:
            print(f"Названия товаров недоступны (ответ {r.status_code}) — покажу артикулы.", flush=True)
            return names
        data = r.json()  # WB кладёт карточки прямо в корень: {"cards": [...], "cursor": {...}}
        cards = data.get("cards") or (data.get("data") or {}).get("cards") or []
        for card in cards:
            if card.get("nmID"):
                names[card["nmID"]] = card.get("title") or f"товар {card['nmID']}"
        nxt = data.get("cursor") or (data.get("data") or {}).get("cursor") or {}
        if not nxt.get("nmID") or not nxt.get("updatedAt"):
            break
        cursor = {"limit": 100, "nmID": nxt["nmID"], "updatedAt": nxt["updatedAt"]}
    return names


def _find_rows(obj):
    """Ищет в ответе API список словарей — на любой глубине вложенности."""
    if isinstance(obj, list):
        return obj if obj and isinstance(obj[0], dict) else None
    if isinstance(obj, dict):
        for v in obj.values():
            found = _find_rows(v)
            if found is not None:
                return found
    return None


def _stocks(token):
    """Остатки на складах WB — новый метод (старый v1 отключён 23.06.2026).
    С автоповтором: лимит этого метода — 1 запрос / 20 сек."""
    names = _product_names(token)  # названия подтягиваем заранее, один раз
    by_product = {}
    limit, offset = 100000, 0
    while True:
        r = None
        for attempt in (1, 2):
            try:
                r = requests.post(
                    f"{ANALYTICS}/api/analytics/v1/stocks-report/wb-warehouses",
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    json={"limit": limit, "offset": offset},
                    timeout=60,
                )
            except requests.RequestException as e:
                if attempt == 2:
                    raise RuntimeError(f"Сеть не отвечает при запросе остатков: {e}")
                print("Сетевая ошибка на остатках, повторяю через 5 секунд...", flush=True)
                time.sleep(5)
                continue
            if r.status_code == 429 and attempt == 1:
                print("429 на остатках — жду 25 секунд и пробую ещё раз...", flush=True)
                time.sleep(25)
                continue
            break
        if r is None or r.status_code == 429:
            raise RuntimeError("WB просит не спешить (лимит запросов). Подожди минуту и повтори /summary.")
        r.raise_for_status()
        data = r.json()
        rows = _find_rows(data) or []
        if not rows:
            break
        for row in rows:
            available = (row.get("quantity") or 0) - (row.get("inWayToClient") or 0) - (row.get("inWayFromClient") or 0)
            nm = row.get("nmId")
            key = (nm, names.get(nm, f"товар {nm}"))
            by_product[key] = by_product.get(key, 0) + max(available, 0)
        if len(rows) < limit:
            break
        offset += limit
    return by_product


def _fmt_money(x):
    return f"{x:,.0f}".replace(",", " ")


def build_summary(token, chat_tz=None):
    """Собирает текст сводки: вчера против позавчера + остатки."""
    now = datetime.now(MSK)
    sales = _sales_for_range(2, token)
    empty = {"revenue": 0.0, "count": 0, "by_product": {}}
    yesterday = sales.get((now - timedelta(days=1)).strftime("%Y-%m-%d"), empty)
    day_before = sales.get((now - timedelta(days=2)).strftime("%Y-%m-%d"), empty)
    orders = _orders_count_range(2, token)
    orders_yesterday = orders.get((now - timedelta(days=1)).strftime("%Y-%m-%d"), 0)

    lines = [f"🦅 Сводка WB за {(now - timedelta(days=1)).strftime('%d.%m')} (вчера)"]

    rev, rev_prev = yesterday["revenue"], day_before["revenue"]
    lines.append(f"💰 Выручка: {_fmt_money(rev)} ₽ ({yesterday['count']} продаж)")

    if rev_prev:
        diff = rev - rev_prev
        arrow = "📈" if diff >= 0 else "📉"
        percent = (diff / rev_prev * 100) if rev_prev else 0
        lines.append(f"{arrow} Позавчера: {_fmt_money(rev_prev)} ₽ — разница {diff:+,.0f} ₽ ({percent:+.0f}%)".replace(",", " "))

    lines.append(f"🛒 Новых заказов: {orders_yesterday}")

    # Топ-3 товара по выручке за вчера
    top = sorted(yesterday["by_product"].items(), key=lambda kv: -kv[1])[:3]
    if top:
        lines.append("\nТоп вчера:")
        for name, pay in top:
            lines.append(f"  • {name} — {_fmt_money(pay)} ₽")

    # Заканчивающиеся товары. Позиции без названия (карточка снята с продажи,
    # остаток 0) в сводку не попадают — это мёртвые артикулы.
    stocks = _stocks(token)
    live = {k: v for k, v in stocks.items() if not str(k[1]).startswith("товар ")}
    low = [(subj, qty) for (_, subj), qty in sorted(live.items()) if 0 < qty <= LOW_STOCK]
    out = [(subj, qty) for (_, subj), qty in sorted(live.items()) if qty == 0]
    if out:
        lines.append("\n🔴 Кончились совсем: " + ", ".join(subj for subj, _ in out[:7]))
    if low:
        lines.append("🟠 Заканчивается (<=%d шт): " % LOW_STOCK + ", ".join(f"{subj} ({qty})" for subj, qty in low[:7]))
    if not out and not low:
        lines.append("\n📦 Остатки в порядке — ничего не заканчивается.")

    return "\n".join(lines)


if __name__ == "__main__":
    # Проверка без Telegram: python3 wb_data.py
    token = os.getenv("WB_TOKEN")
    if not token:
        load_dotenv()
        token = os.getenv("WB_TOKEN")
    if not token:
        raise SystemExit("Нет WB_TOKEN в .env — запусти check_wb_token.py, он впишет его.")
    print(build_summary(token))