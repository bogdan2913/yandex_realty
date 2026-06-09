import csv
import json
import logging
import os
import random
import time
from urllib.parse import quote

import requests as plain_requests
from curl_cffi import requests as cffi_requests

from config import (
    CITY_CATALOG_URLS, MAX_PAGES, DELAY_MIN, DELAY_MAX, BLOCK_THRESHOLD, OUTPUT_DIR,
    PROXY_SERVER, PROXY_USERNAME, PROXY_PASSWORD, PROXY_ROTATE_URL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yandex_realty")

_IMPERSONATE = ["chrome120", "chrome124", "chrome131"]

_HEADERS = {
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
    "Sec-Fetch-Dest":  "document",
    "Sec-Fetch-Mode":  "navigate",
    "Sec-Fetch-Site":  "same-origin",
    "Sec-Fetch-User":  "?1",
    "Upgrade-Insecure-Requests": "1",
}

CSV_FIELDS = [
    "offer_id", "url", "city", "address", "lat", "lon",
    "rooms", "flat_type", "area_total", "area_living", "area_kitchen",
    "floor", "floors_total",
    "price", "price_per_sqm",
    "year_built", "building_type", "renovation",
    "bathroom", "balcony",
    "description", "created_at",
]

# ─── Прокси ──────────────────────────────────────────────────────────────────

_last_rotate = 0.0


def get_proxy_url():
    if not PROXY_SERVER:
        return None
    server = PROXY_SERVER
    for prefix in ("https://", "http://"):
        if server.startswith(prefix):
            server = server[len(prefix):]
    if PROXY_USERNAME:
        p = quote(PROXY_PASSWORD, safe="")
        return f"http://{PROXY_USERNAME}:{p}@{server}"
    return f"http://{server}"


def rotate_ip():
    global _last_rotate
    if not PROXY_ROTATE_URL:
        return False
    if time.time() - _last_rotate < 60:
        logger.info("Ротация пропущена — слишком рано")
        return False
    try:
        r = plain_requests.get(PROXY_ROTATE_URL, timeout=10)
        if r.status_code == 200:
            _last_rotate = time.time()
            time.sleep(3)
            logger.info("IP ротирован")
            return True
        logger.warning(f"Ротация не удалась: HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"Ротация ошибка: {e}")
    return False


# ─── Telegram ────────────────────────────────────────────────────────────────

def notify(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    proxy_url = get_proxy_url()
    proxies   = {"http": proxy_url, "https": proxy_url} if proxy_url else None
    try:
        plain_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            proxies=proxies,
            timeout=10,
        )
    except Exception:
        pass


# ─── Сессия ───────────────────────────────────────────────────────────────────

def make_session(proxy_url):
    session = cffi_requests.Session(impersonate=random.choice(_IMPERSONATE))
    session.headers.update(_HEADERS)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def warmup(session):
    try:
        session.get("https://realty.yandex.ru/", timeout=30)
        time.sleep(random.uniform(1.5, 3.0))
    except Exception:
        pass


# ─── Загрузка ─────────────────────────────────────────────────────────────────

_block_streak = 0


def fetch_page(url, session, referer=None):
    global _block_streak
    headers = {"Referer": referer} if referer else {}
    try:
        r = session.get(url, headers=headers, timeout=30)

        if r.status_code == 200:
            text = r.text
            # Яндекс иногда отдаёт капчу с кодом 200
            if "showcaptcha" in r.url or "Нам важно убедиться" in text:
                _block_streak += 1
                logger.warning(f"Капча (подряд: {_block_streak}): {url}")
                if _block_streak >= BLOCK_THRESHOLD:
                    logger.warning("Ротируем IP...")
                    rotate_ip()
                    _block_streak = 0
                    time.sleep(60)
                else:
                    time.sleep(random.uniform(20, 40))
                return None
            _block_streak = 0
            return text

        if r.status_code in (429, 403):
            _block_streak += 1
            logger.warning(f"HTTP {r.status_code} (подряд: {_block_streak}): {url}")
            if _block_streak >= BLOCK_THRESHOLD:
                logger.warning("Ротируем IP...")
                rotate_ip()
                _block_streak = 0
                time.sleep(60)
            else:
                time.sleep(random.uniform(20, 40))
        else:
            logger.warning(f"HTTP {r.status_code}: {url}")

    except Exception as e:
        logger.error(f"fetch {url}: {e}")

    return None


# ─── Парсинг ──────────────────────────────────────────────────────────────────

def extract_initial_state(html):
    idx = html.find("window.INITIAL_STATE = {")
    if idx == -1:
        return None
    chunk = html[idx:]
    end   = chunk.find("</script>")
    if end == -1:
        return None
    raw = chunk[:end].strip().rstrip(";")[len("window.INITIAL_STATE = "):]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error: {e}")
        return None


def parse_offer(o, city):
    loc   = o.get("location") or {}
    point = loc.get("point") or {}
    area  = o.get("area") or {}
    ls    = o.get("livingSpace") or {}
    ks    = o.get("kitchenSpace") or {}
    price = o.get("price") or {}
    bld   = o.get("building") or {}
    house = o.get("house") or {}
    apt   = o.get("apartment") or {}
    floors_offered = o.get("floorsOffered") or []

    return {
        "offer_id":      o.get("offerId"),
        "url":           o.get("shareUrl") or f"https://realty.yandex.ru/offer/{o.get('offerId', '')}",
        "city":          city,
        "address":       loc.get("address"),
        "lat":           point.get("latitude"),
        "lon":           point.get("longitude"),
        "rooms":         o.get("roomsTotal"),
        "flat_type":     o.get("flatType"),
        "area_total":    area.get("value"),
        "area_living":   ls.get("value"),
        "area_kitchen":  ks.get("value"),
        "floor":         floors_offered[0] if floors_offered else None,
        "floors_total":  o.get("floorsTotal"),
        "price":         price.get("value"),
        "price_per_sqm": price.get("valuePerPart"),
        "year_built":    bld.get("builtYear"),
        "building_type": bld.get("buildingType"),
        "renovation":    apt.get("renovation"),
        "bathroom":      house.get("bathroomUnit"),
        "balcony":       house.get("balconyType"),
        "description":   o.get("description"),
        "created_at":    o.get("creationDate"),
    }


# ─── Город ────────────────────────────────────────────────────────────────────

def scrape_city(city, base_url, proxy_url):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"{city}.csv")

    session  = make_session(proxy_url)
    warmup(session)

    saved    = 0
    prev_url = "https://realty.yandex.ru/"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for page_num in range(1, MAX_PAGES + 1):
            sep = "&" if "?" in base_url else "?"
            url = base_url if page_num == 1 else f"{base_url}{sep}page={page_num}"
            logger.info(f"стр. {page_num}: {url}")

            html = None
            for attempt in range(1, 4):
                html = fetch_page(url, session, referer=prev_url)
                if html:
                    break
                logger.warning(f"попытка {attempt}/3 не удалась, ждём...")
                time.sleep(10 * attempt)

            if not html:
                logger.error(f"стр. {page_num}: не удалось загрузить после 3 попыток — стоп")
                break

            data = extract_initial_state(html)
            if not data:
                logger.error(f"стр. {page_num}: INITIAL_STATE не найден — стоп")
                break

            offers_block = (data.get("search") or {}).get("offers") or {}
            entities     = offers_block.get("entities") or []
            pager        = offers_block.get("pager") or {}
            total_pages  = pager.get("totalPages", 1)

            if not entities:
                logger.info(f"стр. {page_num}: объявлений нет — стоп")
                break

            for o in entities:
                writer.writerow(parse_offer(o, city))
                saved += 1

            prev_url = url
            logger.info(f"стр. {page_num}/{total_pages}: +{len(entities)} (итого {saved})")

            if page_num >= total_pages:
                break

            time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    logger.info(f"{city}: сохранено {saved} → {out_path}")
    return saved


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    proxy_url   = get_proxy_url()
    total_saved = 0
    cities      = list(CITY_CATALOG_URLS.items())

    if proxy_url:
        logger.info(f"Прокси: {PROXY_SERVER}")
    else:
        logger.info("Прокси не настроен — работаем напрямую")

    for i, (city, url) in enumerate(cities, 1):
        logger.info(f"=== [{i}/{len(cities)}] {city} ===")
        n = scrape_city(city, url, proxy_url)
        total_saved += n

        remaining = len(cities) - i
        notify(
            f"Яндекс Недвижимость / {city} [{i}/{len(cities)}]\n"
            f"Объявлений: {n}\n"
            + (f"Осталось городов: {remaining}" if remaining else "Всё готово ✅")
        )

        if i < len(cities):
            pause = random.uniform(5, 15)
            logger.info(f"Пауза {pause:.1f} сек...")
            time.sleep(pause)

    logger.info(f"Готово. Всего объявлений: {total_saved}")
    notify(f"Яндекс Недвижимость — парсинг завершён\nВсего объявлений: {total_saved}")


if __name__ == "__main__":
    main()
