# Данные Ozon для бота «Утренняя сводка»
# Официальный Seller API Ozon: выручка/заказы по дням (analytics) + остатки.
# Ключи читаются из .env (OZON_CLIENT_ID, OZON_API_KEY), в код и git не попадают.
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from wb_data import MSK, _fmt_money

BASE = "https://api-seller.ozon.ru"
LOW_STOCK = 5  # порог «заканчивается», штук на всех складах


def _post(url, body, client_id, api_key):
    """POST с автоповтором: 429 (лимит API) и сетевые сбои. Максимум 2 попытки."""
    last_exc = None
    for attempt in (1, 2):
        try:
            r = requests.post(
                url,
                headers={
                    "Client-Id": client_id,
                    "Api-Key": api_key,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=60,
            )
            if r.status_code != 429:
                break
            if attempt == 2:
                raise RuntimeError("Ozon просит не спешить (лимит запросов). Подожди минуту и повтори /summary.")
            print("429 от Ozon — жду 65 секунд и пробую ещё раз...", flush=True)
            time.sleep(65)
        except requests.RequestException as e:
            last_exc = e
            if attempt == 2:
                break
            print("Сетевая ошибка, повторяю через 5 секунд...", flush=True)
            time.sleep(5)
    if last_exc is not None:
        raise RuntimeError(f"Сеть не отвечает при запросе к Ozon: {last_exc}")
    r.raise_for_status()
    return r.json()


def _day_iso(day: datetime):
    """Границы дня в ISO с московской таймзоной (так ждёт analytics Ozon)."""
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def _sales_metrics(days, client_id, api_key):
    """Выручка и заказанные единицы за последние `days` дней ОДНИМ запросом
    (лимит analytics — 1 запрос/мин). Возвращает {день: {revenue, units}}."""
    day_from = datetime.now(MSK) - timedelta(days=days)
    date_from = day_from.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    date_to = datetime.now(MSK).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    data = _post(
        f"{BASE}/v1/analytics/data",
        {
            "date_from": date_from,
            "date_to": date_to,
            "dimension": ["day"],
            "metrics": ["revenue", "ordered_units"],
            "limit": 1000,
            "offset": 0,
        },
        client_id,
        api_key,
    )
    by_day = {}
    for row in (data.get("result") or {}).get("data") or []:
        day = (row.get("dimensions") or [{}])[0].get("name", "")[:10]
        if not day:
            continue
        metrics = row.get("metrics") or [0.0, 0]
        by_day[day] = {"revenue": float(metrics[0] or 0), "units": int(metrics[1] or 0)}
    return by_day


def _stocks(client_id, api_key):
    """{product_id: [артикул offer_id, доступный остаток]} — пагинация по last_id,
    доступный остаток = present - reserved по всем складам."""
    by_product = {}
    last_id = ""
    while True:
        data = _post(
            f"{BASE}/v4/product/info/stocks",
            {"filter": {"visibility": "ALL"}, "limit": 1000, "last_id": last_id},
            client_id,
            api_key,
        )
        items = data.get("items") or []
        for item in items:
            pid = item.get("product_id")
            if pid is None:
                continue
            offer = item.get("offer_id") or f"товар {pid}"
            available = sum((s.get("present") or 0) - (s.get("reserved") or 0) for s in item.get("stocks") or [])
            prev = by_product.get(pid, [offer, 0])
            by_product[pid] = [prev[0], prev[1] + max(available, 0)]
        last_id = data.get("last_id")
        if not last_id or len(items) < 1000:
            break
    return by_product


def _product_names(client_id, api_key, product_ids):
    """{product_id: название} через /v3/product/info/list. Идёт батчами по 1000.
    Если метод недоступен — пустой словарь (в сводке останутся артикулы)."""
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
        except RuntimeError as e:
            print(f"Названия товаров Ozon недоступны: {e}", flush=True)
            return names
        for item in data.get("items") or []:
            if item.get("id") and item.get("name"):
                names[item["id"]] = item["name"]
    return names


def build_ozon_summary(client_id, api_key, now=None):
    """Собирает текст сводки Ozon: вчера против позавчера + заканчивающиеся остатки."""
    now = now or datetime.now(MSK)
    sales = _sales_metrics(2, client_id, api_key)
    empty = {"revenue": 0.0, "units": 0}
    yesterday = sales.get((now - timedelta(days=1)).strftime("%Y-%m-%d"), empty)
    day_before = sales.get((now - timedelta(days=2)).strftime("%Y-%m-%d"), empty)

    lines = [f"🌐 Сводка Ozon за {(now - timedelta(days=1)).strftime('%d.%m')} (вчера)"]
    rev, rev_prev = yesterday["revenue"], day_before["revenue"]
    lines.append(f"💰 Выручка: {_fmt_money(rev)} ₽ ({yesterday['units']} заказанных шт.)")
    if rev_prev:
        diff = rev - rev_prev
        arrow = "📈" if diff >= 0 else "📉"
        percent = (diff / rev_prev * 100) if rev_prev else 0
        lines.append(f"{arrow} Позавчера: {_fmt_money(rev_prev)} ₽ — разница {diff:+,.0f} ₽ ({percent:+.0f}%)".replace(",", " "))

    # Заканчивающиеся товары: названия из карточек, без названия — артикул offer_id
    stocks = _stocks(client_id, api_key)
    names = _product_names(client_id, api_key, stocks.keys())
    label = {pid: names.get(pid, stocks[pid][0]) for pid in stocks}
    live = {pid: qty for pid, (_, qty) in stocks.items() if not str(label[pid]).startswith("товар ")}
    low = [(label[pid], qty) for pid, qty in sorted(live.items(), key=lambda kv: label[kv[0]]) if 0 < qty <= LOW_STOCK]
    out = [(label[pid], qty) for pid, qty in sorted(live.items(), key=lambda kv: label[kv[0]]) if qty == 0]
    if out:
        lines.append("\n🔴 Кончились совсем: " + ", ".join(subj for subj, _ in out[:7]))
    if low:
        lines.append("🟠 Заканчивается (<=%d шт): " % LOW_STOCK + ", ".join(f"{subj} ({qty})" for subj, qty in low[:7]))
    if not out and not low:
        lines.append("\n📦 Остатки в порядке — ничего не заканчивается.")

    return "\n".join(lines)


if __name__ == "__main__":
    # Проверка без Telegram: python3 ozon_data.py
    import os

    load_dotenv()
    client_id = os.getenv("OZON_CLIENT_ID")
    api_key = os.getenv("OZON_API_KEY")
    if not client_id or not api_key:
        raise SystemExit(
            "Нет ключей Ozon. Добавь в .env две строки: OZON_CLIENT_ID=... и OZON_API_KEY=... "
            "(кабинет Ozon → Настройки → API-ключи)."
        )
    print(build_ozon_summary(client_id, api_key))