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
from downloader import download_tiktok, extract_tiktok_url, prepare_for_whatsapp

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

# Nombre de téléchargements simultanés maximum.
_slots = threading.Semaphore(3)

HELP_TEXT = (
    "👋 Envoie-moi un lien TikTok et je te renvoie la vidéo ici.\n\n"
    "Exemple :\nhttps://www.tiktok.com/@compte/video/1234567890"
)


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


def send_text(to: str, body: str) -> None:
    try:
        requests.post(
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
    except Exception:
        log.exception("Envoi du texte impossible")


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
    return True


# --------------------------------------------------------------------------
#  Traitement d'une demande
# --------------------------------------------------------------------------
def process(sender: str, url: str) -> None:
    if not _slots.acquire(timeout=120):
        send_text(sender, "⏳ Trop de demandes en même temps. Réessaie dans un instant.")
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
            send_document(sender, media_id, os.path.basename(path))

    except Exception as exc:
        log.exception("Échec du traitement")
        message = str(exc).lower()
        if "private" in message or "login" in message:
            reason = "Cette vidéo est privée ou supprimée."
        elif "unsupported url" in message:
            reason = "Ce lien n'est pas reconnu comme un lien TikTok valide."
        else:
            reason = "Je n'ai pas réussi à télécharger cette vidéo."
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
    return "bot ok"


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
    print("=" * 60)
    print("  Laisse cette fenêtre ouverte. Ctrl+C pour arrêter.\n")

    app.run(host="0.0.0.0", port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
