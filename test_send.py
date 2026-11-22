"""Test manuel : télécharge une vidéo TikTok et l'envoie à un numéro."""
import os
import sys

import requests

import config
from downloader import download_tiktok, prepare_for_whatsapp

URL = sys.argv[1] if len(sys.argv) > 1 else "https://vt.tiktok.com/ZSsdwp5tG/"
TO = sys.argv[2] if len(sys.argv) > 2 else "50949097506"

work = os.path.abspath("work/test")
os.makedirs(work, exist_ok=True)

print("1) Téléchargement...")
path = download_tiktok(URL, work)
print("   ->", path, os.path.getsize(path), "octets")

print("2) Conversion H.264 si besoin...")
path = prepare_for_whatsapp(path, config.MAX_MB_SAFE, work)
print("   ->", path, round(os.path.getsize(path) / 1024 / 1024, 2), "Mo")

print("3) Upload du média...")
with open(path, "rb") as fh:
    r = requests.post(
        f"https://graph.facebook.com/{config.API_VERSION}/{config.PHONE_NUMBER_ID}/media",
        headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
        data={"messaging_product": "whatsapp", "type": "video/mp4"},
        files={"file": (os.path.basename(path), fh, "video/mp4")},
        timeout=300,
    )
print("   HTTP", r.status_code)
print("   ", r.text[:800])
if r.status_code >= 400:
    sys.exit(1)

media_id = r.json()["id"]
print("   media_id =", media_id)

print("4) Envoi de la vidéo...")
r2 = requests.post(
    f"https://graph.facebook.com/{config.API_VERSION}/{config.PHONE_NUMBER_ID}/messages",
    headers={"Authorization": f"Bearer {config.ACCESS_TOKEN}"},
    json={
        "messaging_product": "whatsapp",
        "to": TO,
        "type": "video",
        "video": {"id": media_id},
    },
    timeout=120,
)
print("   HTTP", r2.status_code)
print("   ", r2.text[:800])
