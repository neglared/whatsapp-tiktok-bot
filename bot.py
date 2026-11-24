"""Bot WhatsApp : reçoit un lien TikTok et renvoie la vidéo.

Lancement : python bot.py
Le serveur écoute sur http://localhost:8000
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import shutil
import tempfile
import threading
import time
from collections import OrderedDict

import requests
from flask import Flask, abort, request

import config
import quota
from downloader import (
    STRATEGIES,
    download_tiktok,
    extract_tiktok_url,
    prepare_for_whatsapp,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

app = Flask(__name__)

GRAPH = f"https://graph.facebook.com/{config.API_VERSION}"

# Anti-doublon : Meta peut renvoyer plusieurs fois le même message.
_seen: "OrderedDict[str, float]" = OrderedDict()
_seen_lock = threading.Lock()

# Vidéos déjà envoyées (URL -> media_id) pour ne pas les retélécharger.
_media_cache: "OrderedDict[str, tuple[str, float]]" = OrderedDict()
_media_lock = threading.Lock()

# (destinataire, url) -> date d'envoi : évite de facturer un 2e envoi
# quand l'utilisateur renvoie le même lien par impatience.
_sent: "OrderedDict[tuple[str, str], float]" = OrderedDict()
_sent_lock = threading.Lock()

# Durée pendant laquelle on ne renvoie pas deux fois la même vidéo au même client.
DEDUP_SECONDS = 300

# Compteur de messages pour rester dans le gratuit.
QUOTA = quota.Quota(config.QUOTA_FILE, config.DAILY_LIMIT, config.MONTHLY_LIMIT)

# Nombre de téléchargements simultanés maximum.
_slots = threading.Semaphore(3)

HELP_TEXT = (
    "👋 Envoie-moi un lien TikTok et je te renvoie la vidéo ici.\n\n"
    "Exemple :\nhttps://www.tiktok.com/@compte/video/1234567890"
)

DAILY_TEXT = (
    "🌙 Le bot a atteint sa limite gratuite du jour.\n"
    "Reviens demain : ça se remet à zéro à minuit (heure d'Haïti)."
)


def cached_media(url: str) -> str | None:
    with _media_lock:
        item = _media_cache.get(url)
        if not item:
            return None
        media_id, stamp = item
        if time.time() - stamp > config.MEDIA_CACHE_MINUTES * 60:
            _media_cache.pop(url, None)
            return None
        _media_cache.move_to_end(url)
        return media_id


def cache_media(url: str, media_id: str) -> None:
    with _media_lock:
        _media_cache[url] = (media_id, time.time())
        _media_cache.move_to_end(url)
        while len(_media_cache) > 200:
            _media_cache.popitem(last=False)


def recently_sent(sender: str, url: str) -> bool:
    """Vrai si on a déjà envoyé cette vidéo à ce client il y a peu."""
    with _sent_lock:
        stamp = _sent.get((sender, url))
        if not stamp:
            return False
        if time.time() - stamp > DEDUP_SECONDS:
            _sent.pop((sender, url), None)
            return False
        return True


def mark_sent(sender: str, url: str) -> None:
    with _sent_lock:
        _sent[(sender, url)] = time.time()
        _sent.move_to_end((sender, url))
        while len(_sent) > 1000:
            _sent.popitem(last=False)


def already_seen(message_id: str) -> bool:
    with _seen_lock:
        if message_id in _seen:
            return True
        _seen[message_id] = time.time()
        while len(_seen) > 500:
            _seen.popitem(last=False)
        return False


# --------------------------------------------------------------------------
#  Envoi vers l'API WhatsApp
# --------------------------------------------------------------------------
def _headers() -> dict:
    return {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}


def send_text(to: str, body: str) -> bool:
    try:
        response = requests.post(
            f"{GRAPH}/{config.PHONE_NUMBER_ID}/messages",
            headers=_headers(),
            json={
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": body},
            },
            timeout=30,
        )
        if response.status_code >= 400:
            log.warning("Texte refusé : %s", response.text[:300])
            return False
        QUOTA.spend()
        return True
    except Exception:
        log.exception("Envoi du texte impossible")
        return False


def upload_media(path: str, mime: str = "video/mp4") -> str:
    with open(path, "rb") as handle:
        response = requests.post(
            f"{GRAPH}/{config.PHONE_NUMBER_ID}/media",
            headers=_headers(),
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (os.path.basename(path), handle, mime)},
            timeout=300,
        )
    if response.status_code >= 400:
        raise RuntimeError(f"upload {response.status_code}: {response.text[:300]}")
    return response.json()["id"]


def send_video(to: str, media_id: str) -> bool:
    response = requests.post(
        f"{GRAPH}/{config.PHONE_NUMBER_ID}/messages",
        headers=_headers(),
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "video",
            "video": {"id": media_id},
        },
        timeout=120,
    )
    if response.status_code >= 400:
        log.warning("Envoi vidéo refusé : %s", response.text[:300])
        return False
    QUOTA.spend()
    return True


def send_document(to: str, media_id: str, filename: str) -> bool:
    response = requests.post(
        f"{GRAPH}/{config.PHONE_NUMBER_ID}/messages",
        headers=_headers(),
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": {"id": media_id, "filename": filename},
        },
        timeout=120,
    )
    if response.status_code >= 400:
        log.warning("Envoi document refusé : %s", response.text[:300])
        return False
    QUOTA.spend()
    return True


# --------------------------------------------------------------------------
#  Traitement d'une demande
# --------------------------------------------------------------------------
def process(sender: str, url: str) -> None:
    if not _slots.acquire(timeout=120):
        send_text(sender, "⏳ Trop de demandes en même temps. Réessaie dans un instant.")
        return

    # Vidéo déjà envoyée récemment : on renvoie la même sans la retélécharger.
    known = cached_media(url)
    if known:
        log.info("Vidéo déjà en cache, renvoi direct")
        if send_video(sender, known):
            mark_sent(sender, url)
            _slots.release()
            return

    workdir = tempfile.mkdtemp(prefix="tt_", dir=os.path.abspath(config.WORK_DIR) if os.path.isdir(config.WORK_DIR) else None)
    try:
        log.info("Téléchargement : %s", url)
        path = download_tiktok(url, workdir)
        log.info("Téléchargé : %s (%d Ko)", path, os.path.getsize(path) // 1024)

        path = prepare_for_whatsapp(path, config.MAX_MB_SAFE, workdir)
        size_mb = os.path.getsize(path) / (1024 * 1024)
        log.info("Taille finale : %.1f Mo", size_mb)

        if size_mb > config.MAX_MB:
            send_text(
                sender,
                "⚠️ Cette vidéo est trop lourde pour WhatsApp, même compressée.",
            )
            return

        media_id = upload_media(path, "video/mp4")
        if not send_video(sender, media_id):
            # Deuxième chance : envoyer en fichier joint.
            media_id = upload_media(path, "video/mp4")
            if send_document(sender, media_id, os.path.basename(path)):
                mark_sent(sender, url)
        else:
            cache_media(url, media_id)
            mark_sent(sender, url)

    except Exception as exc:
        log.exception("Échec du traitement")
        message = str(exc).lower()
        if "private" in message or "login" in message or "not available" in message:
            reason = "Cette vidéo est privée, supprimée ou interdite dans cette région."
        elif "unsupported url" in message:
            reason = "Ce lien n'est pas reconnu comme un lien TikTok valide."
        elif "timed out" in message or "timeout" in message or "transport" in message:
            reason = (
                "TikTok n'a pas répondu à temps.\n"
                "Renvoie le même lien : ça marche presque toujours au 2e essai."
            )
        elif "404" in message or "not found" in message:
            reason = "Cette vidéo n'existe plus."
        else:
            reason = "Je n'ai pas réussi à télécharger cette vidéo. Réessaie dans un instant."
        send_text(sender, f"❌ {reason}")
    finally:
        _slots.release()
        shutil.rmtree(workdir, ignore_errors=True)


def handle_message(message: dict) -> None:
    message_id = message.get("id", "")
    if message_id and already_seen(message_id):
        return

    sender = message.get("from")
    if not sender:
        return

    msg_type = message.get("type")
    if msg_type == "text":
        text = message.get("text", {}).get("body", "")
    elif msg_type == "button":
        text = message.get("button", {}).get("text", "")
    elif msg_type == "interactive":
        text = (
            message.get("interactive", {})
            .get("button_reply", {})
            .get("title", "")
        )
    else:
        text = ""

    url = extract_tiktok_url(text)
    if not url:
        send_text(sender, HELP_TEXT)
        return

    state = QUOTA.check()
    if state == quota.Quota.MONTHLY:
        log.warning("Plafond mensuel atteint (%s) : demande ignorée", QUOTA.summary())
        return
    if state == quota.Quota.DAILY:
        log.info("Plafond du jour atteint (%s) : on prévient l'utilisateur", QUOTA.summary())
        send_text(sender, DAILY_TEXT)
        return

    # Mode économie : pas de message d'attente, la vidéo arrive directement.
    if recently_sent(sender, url):
        log.info("Même lien déjà envoyé à %s il y a moins de 5 min : on ignore", sender)
        return

    if not config.SINGLE_MESSAGE:
        send_text(sender, "⏳ Je prépare ta vidéo, patiente quelques secondes…")
    threading.Thread(target=process, args=(sender, url), daemon=True).start()


# --------------------------------------------------------------------------
#  Webhook
# --------------------------------------------------------------------------
def signature_ok() -> bool:
    if not config.APP_SECRET:
        return True
    received = request.headers.get("X-Hub-Signature-256", "")
    if not received.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        config.APP_SECRET.encode(), request.get_data(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, received)


@app.get("/")
def health() -> str:
    return f"bot ok | {QUOTA.summary()}"


@app.get("/probe")
def probe():
    """Teste une URL depuis le serveur : /probe?token=...&url=...

    Sert uniquement au diagnostic (joignabilité des domaines TikTok).
    """
    if request.args.get("token") != config.VERIFY_TOKEN:
        abort(403)

    url = request.args.get("url", "")
    if not url.lower().startswith(("http://", "https://")):
        abort(400)

    try:
        timeout = min(int(request.args.get("timeout", "20")), 60)
    except ValueError:
        timeout = 20

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }

    result: dict = {"url": url}
    started = time.time()
    follow = request.args.get("noredirect") != "1"
    try:
        with requests.get(
            url,
            headers=headers,
            timeout=timeout,
            stream=True,
            allow_redirects=follow,
        ) as response:
            result["status"] = response.status_code
            result["url_finale"] = response.url
            result["redirection"] = response.headers.get("Location", "")
            result["type"] = response.headers.get("Content-Type", "")
            result["taille_annoncee"] = response.headers.get("Content-Length", "")
            chunk = b""
            for piece in response.iter_content(65536):
                chunk += piece
                if len(chunk) >= 3000:
                    break
            result["octets_lus"] = len(chunk)
            result["extrait"] = chunk[:250].decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        result["erreur"] = str(exc)[:220]

    result["secondes"] = round(time.time() - started, 1)
    return result


@app.get("/diag")
def diag():
    """Teste chaque méthode de téléchargement depuis le serveur.

    Protégé par le mot de vérification : /diag?token=...&url=...
    """
    if request.args.get("token") != config.VERIFY_TOKEN:
        abort(403)

    url = request.args.get("url", "https://vt.tiktok.com/ZSsdwp5tG/")
    only = request.args.get("only")
    stop_first = request.args.get("stop") == "1"

    results = {}
    workdir = tempfile.mkdtemp(prefix="diag_")
    try:
        for name, strategy in STRATEGIES:
            if only and only != name:
                continue
            started = time.time()
            try:
                path = strategy(url, workdir)
                results[name] = {
                    "ok": True,
                    "secondes": round(time.time() - started, 1),
                    "mo": round(os.path.getsize(path) / 1048576, 2),
                }
                if stop_first:
                    break
            except Exception as exc:  # noqa: BLE001
                results[name] = {
                    "ok": False,
                    "secondes": round(time.time() - started, 1),
                    "erreur": str(exc)[:200],
                }
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    return {"url": url, "resultats": results}


@app.get("/webhook")
def verify() -> str:
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge", "")
    if mode == "subscribe" and token == config.VERIFY_TOKEN:
        log.info("Webhook vérifié par Meta ✅")
        return challenge
    log.warning("Vérification webhook refusée (mauvais VERIFY_TOKEN)")
    abort(403)


@app.post("/webhook")
def webhook():
    if not signature_ok():
        abort(403)

    data = request.get_json(silent=True) or {}
    try:
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for status in value.get("statuses", []):
                    log.info(
                        "Statut %s : %s %s",
                        status.get("id"),
                        status.get("status"),
                        status.get("errors", ""),
                    )
                for message in value.get("messages", []):
                    handle_message(message)
    except Exception:
        log.exception("Erreur webhook")

    # Meta exige une réponse 200 rapide.
    return "OK", 200


def main() -> None:
    os.makedirs(config.WORK_DIR, exist_ok=True)

    missing = config.check_config()
    if missing:
        print("\n⚠️  Réglages manquants dans le fichier .env :", ", ".join(missing))
        print("   Ouvre .env et complète-les, puis relance.\n")

    print("=" * 60)
    print("  Bot WhatsApp TikTok")
    print(f"  Serveur : http://localhost:{config.PORT}")
    print(f"  Webhook : http://localhost:{config.PORT}/webhook")
    print(f"  Mot de vérification : {config.VERIFY_TOKEN}")
    print(f"  Limite : {config.DAILY_LIMIT}/jour, {config.MONTHLY_LIMIT}/mois")
    print("=" * 60)
    print("  Laisse cette fenêtre ouverte. Ctrl+C pour arrêter.\n")

    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
