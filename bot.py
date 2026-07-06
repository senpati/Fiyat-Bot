import os
import re
import json
import sqlite3
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
CHECK_INTERVAL_SECONDS = 60 * 60  # saatte bir

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
}

URL_REGEX = re.compile(r"https?://\S+")


# ------------------------- Veritabanı -------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            target_price REAL,
            last_price REAL,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def add_track(user_id, chat_id, url, title, target_price, current_price):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        """INSERT INTO tracks (user_id, chat_id, url, title, target_price, last_price, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (user_id, chat_id, url, title, target_price, current_price, datetime.utcnow().isoformat()),
    )
    conn.commit()
    track_id = cur.lastrowid
    conn.close()
    return track_id


def get_user_tracks(user_id):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, url, title, target_price, last_price FROM tracks WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    return rows


def delete_track(user_id, track_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "DELETE FROM tracks WHERE id = ? AND user_id = ?", (track_id, user_id)
    )
    conn.commit()
    deleted = cur.rowcount
    conn.close()
    return deleted > 0


def get_all_tracks():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, user_id, chat_id, url, title, target_price, last_price FROM tracks"
    ).fetchall()
    conn.close()
    return rows


def update_last_price(track_id, price):
    conn = sqlite3.connect(DB_PATH)
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
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    return resp.text


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

    return price, title


def get_price(url: str):
    html = fetch_page(url)
    return extract_price_and_title(html)


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
    rows = get_user_tracks(update.effective_user.id)
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
    await update.message.reply_text("\n\n".join(lines))


async def sil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Kullanım: /sil <id>  (id'yi /liste ile görebilirsin)")
        return
    try:
        track_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Geçerli bir id gir. Örnek: /sil 3")
        return
    ok = delete_track(update.effective_user.id, track_id)
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
        track_id = add_track(
            user_id,
            update.effective_chat.id,
            pending["url"],
            pending["title"],
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
    await update.message.reply_text("Fiyat kontrol ediliyor, bir saniye... 🔎")
    try:
        price, title = get_price(url)
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

    context.user_data["pending"] = {"url": url, "price": price, "title": title}
    await update.message.reply_text(
        f"Güncel fiyat: {price:.2f} TL\n({title or url})\n\n"
        "Hangi fiyatın altına düşünce haber vereyim? Bir sayı gönder (örn: 999.90) "
        "ya da her düşüşte haber almak istersen 'düşüşte' yaz."
    )


# ------------------------- Periyodik kontrol -------------------------

async def check_prices(context: ContextTypes.DEFAULT_TYPE):
    rows = get_all_tracks()
    logger.info("Fiyat kontrolü başladı: %d takip", len(rows))
    for track_id, user_id, chat_id, url, title, target_price, last_price in rows:
        try:
            price, _ = get_price(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kontrol hatası (#%s): %s", track_id, exc)
            continue
        if price is None:
            continue

        should_notify = False
        if target_price is not None:
            if price <= target_price and (last_price is None or last_price > target_price):
                should_notify = True
        else:
            if last_price is not None and price < last_price:
                should_notify = True

        if should_notify:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"🔔 Fiyat düştü!\n{title or url}\n"
                        f"Yeni fiyat: {price:.2f} TL (önceki: {last_price:.2f} TL)\n{url}"
                    ),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Mesaj gönderilemedi (user %s): %s", user_id, exc)

        if price != last_price:
            update_last_price(track_id, price)


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
