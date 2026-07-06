import os
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DB_PATH = os.path.join(os.path.dirname(__file__), "tracks.db")
CHECK_INTERVAL_SECONDS = 15 * 60  # 15 dakikada bir

# 500'e yakın kullanıcıyla aynı anda çalışırken hem hedef siteleri hem de
# botun kendi event loop'unu boğmamak için eşzamanlı tarama sayısını sınırlıyoruz.
MAX_CONCURRENT_CHECKS = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

URL_REGEX = re.compile(r"https?://\S+")

# Bilinen e-ticaret domain'leri: kısa link çözümlemesinde "hedefe ulaştık mı"
# kontrolü için kullanılır.
KNOWN_SHOP_DOMAINS = (
    "hepsiburada.com",
    "trendyol.com",
    "n11.com",
    "amazon.com.tr",
    "gittigidiyor.com",
    "pazarama.com",
    "ciceksepeti.com",
    "vatanbilgisayar.com",
    "mediamarkt.com.tr",
)

# Kısa link / paylaşım servisleri (bunlarda kalmışsak henüz çözülmemiş demektir)
SHORT_LINK_MARKERS = (
    "app.hb.biz",
    "ty.gl",
    "dyn.trendyol",
    "bit.ly",
    "t.co",
    "tinyurl.com",
)


def _is_still_short_link(url: str) -> bool:
    return any(marker in url for marker in SHORT_LINK_MARKERS)


# ------------------------- Veritabanı -------------------------

def _connect():
    return sqlite3.connect(DB_PATH, timeout=30)


def init_db():
    conn = _connect()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            image_url TEXT,
            target_price REAL,
            last_price REAL,
            created_at TEXT
        )
        """
    )
    # Daha önce oluşturulmuş (image_url'siz) veritabanlarını sorunsuz göçür
    try:
        conn.execute("ALTER TABLE tracks ADD COLUMN image_url TEXT")
    except sqlite3.OperationalError:
        pass  # kolon zaten var
    conn.commit()
    conn.close()


def add_track(user_id, chat_id, url, title, image_url, target_price, current_price):
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO tracks (user_id, chat_id, url, title, image_url, target_price, last_price, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, chat_id, url, title, image_url, target_price, current_price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    track_id = cur.lastrowid
    conn.close()
    return track_id


def get_user_tracks(user_id):
    conn = _connect()
    rows = conn.execute(
        "SELECT id, url, title, target_price, last_price FROM tracks WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def delete_track(user_id, track_id):
    conn = _connect()
    cur = conn.execute(
        "DELETE FROM tracks WHERE id = ? AND user_id = ?", (track_id, user_id)
    )
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted > 0


def get_all_tracks():
    conn = _connect()
    rows = conn.execute(
        "SELECT id, user_id, chat_id, url, title, image_url, target_price, last_price FROM tracks"
    ).fetchall()
    conn.close()
    return rows


def update_last_price(track_id, price):
    conn = _connect()
    conn.execute("UPDATE tracks SET last_price = ? WHERE id = ?", (price, track_id))
    conn.commit()
    conn.close()


# ------------------------- Fiyat çekme -------------------------

def _parse_number(raw: str):
    """'1.234,56' / '1,234.56' / '1234.56' gibi metinleri float'a çevirir."""
    raw = raw.strip()
    raw = re.sub(r"[^\d.,]", "", raw)
    if not raw:
        return None
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        # Türkçe format ihtimali: virgül ondalık ayraç
        parts = raw.split(",")
        if len(parts[-1]) == 2:
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return None


def fetch_page(url: str):
    resp = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, resp.url


def resolve_url(url: str) -> str:
    """Kısa/paylaşım linklerini (app.hb.biz, ty.gl vb.) gerçek ürün
    sayfasının linkine çözer. Çözemezse elindeki en iyi tahmini döndürür."""
    try:
        html, final_url = fetch_page(url)
    except Exception:  # noqa: BLE001
        return url

    if not _is_still_short_link(final_url):
        return final_url

    soup = BeautifulSoup(html, "lxml")
    candidates = []

    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        candidates.append(canonical["href"])

    og_url = soup.find("meta", property="og:url")
    if og_url and og_url.get("content"):
        candidates.append(og_url["content"])

    meta_refresh = soup.find("meta", attrs={"http-equiv": re.compile("refresh", re.I)})
    if meta_refresh and meta_refresh.get("content"):
        m = re.search(r"url=([^;]+)", meta_refresh["content"], re.I)
        if m:
            candidates.append(m.group(1).strip())

    js_match = re.search(
        r"(?:window\.location(?:\.href)?|location\.replace)\s*=?\(?\s*['\"]([^'\"]+)['\"]",
        html,
    )
    if js_match:
        candidates.append(js_match.group(1))

    # Sayfa metni içinde bilinen domainlere ait tam ürün linki ara (son çare)
    for domain in KNOWN_SHOP_DOMAINS:
        m = re.search(
            r"https?://(?:www\.)?" + re.escape(domain) + r"/[^\s\"'<>\\]+", html
        )
        if m:
            candidates.append(m.group(0))

    for cand in candidates:
        if cand.startswith("http") and not _is_still_short_link(cand):
            return cand

    return final_url


def extract_price_and_title(html: str):
    soup = BeautifulSoup(html, "lxml")
    price = None

    # 1) Meta etiketleri
    meta_candidates = [
        ("meta", {"property": "og:price:amount"}),
        ("meta", {"property": "product:price:amount"}),
        ("meta", {"itemprop": "price"}),
        ("meta", {"name": "twitter:data1"}),
    ]
    for tag, attrs in meta_candidates:
        el = soup.find(tag, attrs=attrs)
        if el and el.get("content"):
            price = _parse_number(el["content"])
            if price:
                break

    # 2) JSON-LD (schema.org Product/Offer)
    if not price:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (ValueError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                offers = item.get("offers")
                if isinstance(offers, dict):
                    p = offers.get("price") or offers.get("lowPrice")
                    if p:
                        price = _parse_number(str(p))
                if price:
                    break
            if price:
                break

    # 3) itemprop="price" element içeriği
    if not price:
        el = soup.find(attrs={"itemprop": "price"})
        if el:
            price = _parse_number(el.get("content") or el.get_text())

    # 4) Son çare: sayfa metninde ₺ / TL / $ geçen ilk makul sayı
    if not price:
        text = soup.get_text(" ", strip=True)
        match = re.search(r"([\d.,]+)\s*(₺|TL|TRY|\$|USD)", text)
        if match:
            price = _parse_number(match.group(1))

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()[:100]
    og_title = soup.find("meta", property="og:title")
    if og_title and og_title.get("content"):
        title = og_title["content"].strip()[:100]

    image_url = None
    og_image = soup.find("meta", property="og:image")
    if og_image and og_image.get("content"):
        image_url = og_image["content"].strip()
    if not image_url:
        twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
        if twitter_image and twitter_image.get("content"):
            image_url = twitter_image["content"].strip()
    if not image_url:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "{}")
            except (ValueError, TypeError):
                continue
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                img = item.get("image")
                if isinstance(img, list) and img:
                    image_url = str(img[0])
                elif isinstance(img, str):
                    image_url = img
                if image_url:
                    break
            if image_url:
                break

    return price, title, image_url


def get_price(url: str):
    resolved_url = resolve_url(url)
    html, _ = fetch_page(resolved_url)
    price, title, image_url = extract_price_and_title(html)
    return price, title, image_url, resolved_url


# ------------------------- Telegram komutları -------------------------

WELCOME = (
    "Merhaba! 👋\n\n"
    "Bana bir ürün linki gönder, güncel fiyatını sana söyleyeyim.\n"
    "Ardından bir hedef fiyat belirtebilir ya da fiyat her düştüğünde haber "
    "vermemi isteyebilirsin.\n\n"
    "Komutlar:\n"
    "/liste - Takip ettiğin ürünleri gösterir\n"
    "/sil <id> - Bir takibi siler\n"
    "/yardim - Bu mesajı tekrar gösterir"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def yardim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME)


async def liste(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(get_user_tracks, update.effective_user.id)
    if not rows:
        await update.message.reply_text("Henüz takip ettiğin bir ürün yok.")
        return
    lines = []
    for track_id, url, title, target_price, last_price in rows:
        hedef = f"{target_price:.2f} TL" if target_price else "her düşüş"
        lines.append(
            f"#{track_id} - {title or url}\n"
            f"   Son fiyat: {last_price:.2f} TL | Hedef: {hedef}\n   {url}"
        )

    # Telegram tek mesajda ~4096 karaktere izin veriyor; uzun listeleri parçala
    chunk = ""
    for line in lines:
        if len(chunk) + len(line) + 2 > 3500:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += (line + "\n\n")
    if chunk:
        await update.message.reply_text(chunk)


async def sil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /sil <id>  (id'yi /liste ile görebilirsin)")
        return
    try:
        track_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçerli bir id gir. Örnek: /sil 3")
        return
    ok = await asyncio.to_thread(delete_track, update.effective_user.id, track_id)
    await update.message.reply_text("Silindi ✅" if ok else "Böyle bir takip bulunamadı.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    pending = context.user_data.get("pending")

    # Kullanıcı hedef fiyat / "düşüşte" cevabı veriyorsa
    if pending:
        if text.lower() in ("düşüşte", "dususte", "her düşüş", "d"):
            target_price = None
        else:
            target_price = _parse_number(text)
            if target_price is None:
                await update.message.reply_text(
                    "Anlayamadım. Bir sayı gönder (örn: 999.90) ya da "
                    "her düşüşte haber almak için 'düşüşte' yaz."
                )
                return
        track_id = await asyncio.to_thread(
            add_track,
            user_id,
            update.effective_chat.id,
            pending["url"],
            pending["title"],
            pending.get("image_url"),
            target_price,
            pending["price"],
        )
        context.user_data.pop("pending", None)
        hedef_msg = f"{target_price:.2f} TL altına düşünce" if target_price else "her fiyat düşüşünde"
        await update.message.reply_text(
            f"Takibe alındı ✅ (#{track_id})\n{hedef_msg} sana haber vereceğim."
        )
        return

    # Kullanıcı bir link gönderdiyse
    match = URL_REGEX.search(text)
    if not match:
        await update.message.reply_text(
            "Bana bir ürün linki gönder, fiyatını kontrol edeyim. /yardim yazarak "
            "nasıl kullanılacağını görebilirsin."
        )
        return

    url = match.group(0)
    await update.message.reply_text("Link çözülüyor ve fiyat kontrol ediliyor, bir saniye... 🔎")
    try:
        price, title, image_url, resolved_url = await asyncio.to_thread(get_price, url)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Fiyat çekilemedi: %s - %s", url, exc)
        await update.message.reply_text(
            "Bu linkten fiyatı okuyamadım 😕 Site bot korumalı olabilir ya da "
            "farklı bir yapı kullanıyor olabilir. Başka bir link deneyebilirsin."
        )
        return

    if price is None:
        await update.message.reply_text(
            "Sayfayı açtım ama fiyatı bulamadım. Linkin ürün sayfası olduğundan emin ol."
        )
        return

    context.user_data["pending"] = {
        "url": resolved_url,
        "price": price,
        "title": title,
        "image_url": image_url,
    }
    caption = (
        f"Güncel fiyat: {price:.2f} TL\n({title or resolved_url})\n\n"
        "Hangi fiyatın altına düşünce haber vereyim? Bir sayı gönder (örn: 999.90) "
        "ya da her düşüşte haber almak istersen 'düşüşte' yaz."
    )
    if image_url:
        try:
            await update.message.reply_photo(photo=image_url, caption=caption)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Görsel gönderilemedi, metne düşülüyor: %s", exc)
    await update.message.reply_text(caption)


# ------------------------- Periyodik kontrol -------------------------

async def _check_single_track(context, semaphore, track):
    track_id, user_id, chat_id, url, title, image_url, target_price, last_price = track
    async with semaphore:
        try:
            price, _, fresh_image_url, _ = await asyncio.to_thread(get_price, url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kontrol hatası (#%s): %s", track_id, exc)
            return
    if price is None:
        return

    should_notify = False
    if target_price is not None:
        if price <= target_price and (last_price is None or last_price > target_price):
            should_notify = True
    else:
        if last_price is not None and price < last_price:
            should_notify = True

    if should_notify:
        caption = (
            f"🔔 Fiyat düştü!\n{title or url}\n"
            f"Yeni fiyat: {price:.2f} TL (önceki: {last_price:.2f} TL)\n{url}"
        )
        img = fresh_image_url or image_url
        try:
            if img:
                await context.bot.send_photo(chat_id=chat_id, photo=img, caption=caption)
            else:
                await context.bot.send_message(chat_id=chat_id, text=caption)
            # Telegram'ın chat başına ~1 msg/sn limitine takılmamak için küçük bir es
            await asyncio.sleep(0.05)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Mesaj gönderilemedi (user %s): %s", user_id, exc)

    if price != last_price:
        await asyncio.to_thread(update_last_price, track_id, price)


async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    rows = await asyncio.to_thread(get_all_tracks)
    logger.info("Fiyat kontrolü başladı: %d takip", len(rows))
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)
    tasks = [_check_single_track(context, semaphore, row) for row in rows]
    # Bir görevde hata olsa bile diğerleri devam etsin diye return_exceptions=True
    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Fiyat kontrolü bitti.")


# ------------------------- Başlangıç -------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN ortam değişkeni bulunamadı. BotFather'dan aldığın token'ı "
            "BOT_TOKEN olarak ayarla."
        )
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("yardim", yardim))
    app.add_handler(CommandHandler("liste", liste))
    app.add_handler(CommandHandler("sil", sil))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.job_queue.run_repeating(check_prices, interval=CHECK_INTERVAL_SECONDS, first=CHECK_INTERVAL_SECONDS)

    logger.info("Bot başlatıldı.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
        
