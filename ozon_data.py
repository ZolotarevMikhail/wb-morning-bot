# Данные Ozon для бота «Утренняя сводка».
# Официальный Seller API Ozon: выручка/заказы по дням (analytics) + остатки.
# Ключи из .env (OZON_CLIENT_ID, OZON_API_KEY), в код и git не попадают.
import os
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from wb_data import MSK, _fmt_money

BASE = "https://api-seller.ozon.ru"
LOW_STOCK = 5  # порог «заканчивается», штук на всех складах


# --- базовый запрос к Ozon ---------------------------------------------------

def _post(url, body, client_id, api_key):
    """POST с автоповтором: 429 (лимит API) и сетевые сбои. Всего 2 попытки."""
    last_exc = None
    for attempt in (1, 2):
        try:
            response = requests.post(
                url,
                headers={
                    "Client-Id": client_id,
                    "Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,
            )
            if response.status_code != 429:
                break
            if attempt == 2:
                raise RuntimeError(
                    "Ozon просит не спешить (лимит запросов). "
                    "Подожди минуту и повтори /summary."
                )
            print("429 от Ozon — жду 65 секунд и пробую ещё раз...",
                  flush=True)
            time.sleep(65)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt == 2:
                break
            print("Сетевая ошибка, повторяю через 5 секунд...", flush=True)
            time.sleep(5)
    if last_exc is not None:
        raise RuntimeError(
            f"Сеть не отвечает при запросе к Ozon: {last_exc}"
        )
    response.raise_for_status()
    return response.json()


def _day_iso(day: datetime):
    """Границы дня в ISO с московской таймзоной (так ждёт analytics Ozon)."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


# --- выручка и заказы через analytics ----------------------------------------

ANALYTICS_PAGE = 1000  # размер страницы в /v1/analytics/data


def _sales_metrics(days, client_id, api_key):
    """Выручка и заказанные единицы за `days` дней. Возвращает
    {день: {revenue, units}}. Нюансы API Ozon (проверено 21.09.2026): день
    лежит в dimensions[0].id (name пустой), а date_from/date_to могут
    игнорироваться — API отдаёт всю историю, фильтруем сами."""
    day_from = (datetime.now(MSK) - timedelta(days=days)).strftime("%Y-%m-%d")
    today_start = (
        datetime.now(MSK)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .isoformat()
    )
    by_day = {}
    offset = 0
    # страховка от бесконечной пагинации: максимум 5 страниц
    for _ in range(5):
        data = _post(
            f"{BASE}/v1/analytics/data",
            {
                "date_from": today_start,
                "date_to": today_start,
                "dimension": ["day"],
                "metrics": ["revenue", "ordered_units"],
                "limit": ANALYTICS_PAGE,
                "offset": offset,
            },
            client_id,
            api_key,
        )
        rows = (data.get("result") or {}).get("data") or []
        for row in rows:
            day = _row_day(row)
            if not day:
                continue
            metrics = row.get("metrics") or [0.0, 0]
            by_day[day] = {
                "revenue": float(metrics[0] or 0),
                "units": int(metrics[1] or 0),
            }
        if len(rows) < ANALYTICS_PAGE:
            break
        offset += ANALYTICS_PAGE
    return {d: v for d, v in by_day.items() if d >= day_from}


def _row_day(row):
    """День из строки ответа analytics: dimensions[0].id, резерв — name."""
    dimensions = (row.get("dimensions") or [{}])[0]
    return (dimensions.get("id") or dimensions.get("name") or "")[:10]


# --- остатки и карточки ------------------------------------------------------

def _stocks(client_id, api_key):
    """{product_id: [артикул offer_id, доступный остаток]} — пагинация по
    last_id, доступный остаток = present - reserved по всем складам."""
    by_product = {}
    last_id = ""
    while True:
        data = _post(
            f"{BASE}/v4/product/info/stocks",
            {"filter": {"visibility": "ALL"}, "limit": 1000,
             "last_id": last_id},
            client_id,
            api_key,
        )
        items = data.get("items") or []
        for item in items:
            product_id = item.get("product_id")
            if product_id is None:
                continue
            offer = item.get("offer_id") or f"товар {product_id}"
            available = sum(
                (stock.get("present") or 0) - (stock.get("reserved") or 0)
                for stock in item.get("stocks") or []
            )
            previous = by_product.get(product_id, [offer, 0])
            by_product[product_id] = [
                previous[0], previous[1] + max(available, 0)
            ]
        last_id = data.get("last_id")
        if not last_id or len(items) < 1000:
            break
    return by_product


def _product_names(client_id, api_key, product_ids):
    """{product_id: название} через /v3/product/info/list. Идёт батчами
    по 1000. Если метод недоступен — пустой словарь (в сводке останутся
    артикулы)."""
    names = {}
    ids = list(product_ids)
    for start in range(0, len(ids), 1000):
        try:
            data = _post(
                f"{BASE}/v3/product/info/list",
                {"product_id": ids[start:start + 1000]},
                client_id,
                api_key,
            )
        except RuntimeError as exc:
            print(f"Названия товаров Ozon недоступны: {exc}", flush=True)
            return names
        for item in data.get("items") or []:
            if item.get("id") and item.get("name"):
                names[item["id"]] = item["name"]
    return names


# --- текст сводки ------------------------------------------------------------

def build_ozon_summary(client_id, api_key, now=None):
    """Собирает текст сводки Ozon: вчера против позавчера + остатки."""
    now = now or datetime.now(MSK)
    sales = _sales_metrics(2, client_id, api_key)
    empty = {"revenue": 0.0, "units": 0}
    yesterday = sales.get(
        (now - timedelta(days=1)).strftime("%Y-%m-%d"), empty
    )
    day_before = sales.get(
        (now - timedelta(days=2)).strftime("%Y-%m-%d"), empty
    )

    yesterday_label = (now - timedelta(days=1)).strftime("%d.%m")
    lines = [f"🌐 Сводка Ozon за {yesterday_label} (вчера)"]
    revenue, revenue_prev = yesterday["revenue"], day_before["revenue"]
    lines.append(
        f"💰 Выручка: {_fmt_money(revenue)} ₽ "
        f"({yesterday['units']} заказанных шт.)"
    )
    if revenue_prev:
        diff = revenue - revenue_prev
        arrow = "📈" if diff >= 0 else "📉"
        percent = (diff / revenue_prev * 100) if revenue_prev else 0
        lines.append(
            f"{arrow} Позавчера: {_fmt_money(revenue_prev)} ₽ — разница "
            f"{diff:+,.0f} ₽ ({percent:+.0f}%)".replace(",", " ")
        )

    # Заканчивающиеся товары: названия из карточек, без названия — offer_id
    low, out = _stock_alerts(client_id, api_key)
    if out:
        lines.append(
            "\n🔴 Кончились совсем: " + ", ".join(subj for subj, _ in out[:7])
        )
    if low:
        lines.append(
            f"🟠 Заканчивается (<={LOW_STOCK} шт): "
            + ", ".join(f"{subj} ({qty})" for subj, qty in low[:7])
        )
    if not out and not low:
        lines.append("\n📦 Остатки в порядке — ничего не заканчивается.")

    return "\n".join(lines)


def _stock_alerts(client_id, api_key):
    """Списки (название, остаток) для сводки: закончились и заканчиваются.
    Только «живые» товары: карточки без названия пропускаем (мёртвые
    артикулы с нулевым остатком)."""
    stocks = _stocks(client_id, api_key)
    names = _product_names(client_id, api_key, stocks.keys())
    label = {pid: names.get(pid, stocks[pid][0]) for pid in stocks}
    live = {
        pid: qty for pid, (_, qty) in stocks.items()
        if not str(label[pid]).startswith("товар ")
    }
    by_name = sorted(live.items(), key=lambda item: label[item[0]])
    low = [
        (label[pid], qty) for pid, qty in by_name if 0 < qty <= LOW_STOCK
    ]
    out = [(label[pid], qty) for pid, qty in by_name if qty == 0]
    return low, out


if __name__ == "__main__":
    # Проверка без Telegram: python3 ozon_data.py
    load_dotenv()
    client_id = os.getenv("OZON_CLIENT_ID")
    api_key = os.getenv("OZON_API_KEY")
    if not client_id or not api_key:
        raise SystemExit(
            "Нет ключей Ozon. Добавь в .env две строки: "
            "OZON_CLIENT_ID=... и OZON_API_KEY=... "
            "(кабинет Ozon → Настройки → API-ключи)."
        )
    print(build_ozon_summary(client_id, api_key))
