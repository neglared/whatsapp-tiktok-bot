"""Lecture de la configuration depuis le fichier .env"""
import os

from dotenv import load_dotenv

load_dotenv()

# --- Identifiants Meta (à remplir à l'étape 3) ---
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN", "").strip()
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "").strip()
APP_SECRET = os.getenv("APP_SECRET", "").strip()
APP_ID = os.getenv("APP_ID", "").strip()

# --- Réglages ---
# Mot de passe inventé par nous, à recopier dans Meta lors de la configuration du webhook.
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "mon-secret-tiktok").strip()
API_VERSION = os.getenv("API_VERSION", "v23.0").strip()
PORT = int(os.getenv("PORT", "8000"))

# Taille maximale d'une vidéo acceptée par WhatsApp (16 Mo officiellement).
MAX_MB = int(os.getenv("MAX_MB", "16"))
# On garde une petite marge de sécurité sous la limite.
MAX_MB_SAFE = max(1, MAX_MB - 1)

# Dossier temporaire de travail.
WORK_DIR = os.getenv("WORK_DIR", "work")


def check_config() -> list[str]:
    """Retourne la liste des réglages manquants."""
    missing = []
    if not ACCESS_TOKEN:
        missing.append("ACCESS_TOKEN")
    if not PHONE_NUMBER_ID:
        missing.append("PHONE_NUMBER_ID")
    return missing
