"""Téléchargement TikTok via yt-dlp + compression via ffmpeg."""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import time

import imageio_ffmpeg
import yt_dlp

# curl_cffi (optionnel) permet à yt-dlp d'imiter un vrai navigateur :
# TikTok bloque beaucoup moins les serveurs quand on ressemble à Chrome.
_IMPERSONATE = None
try:  # pragma: no cover
    import curl_cffi  # noqa: F401

    from yt_dlp.networking.impersonate import ImpersonateTarget

    _IMPERSONATE = ImpersonateTarget("chrome")
except Exception:  # pragma: no cover
    _IMPERSONATE = None

# Reconnaît les liens TikTok (tiktok.com, vm.tiktok.com, vt.tiktok.com, m.tiktok.com...)
_TIKTOK_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)?tiktok\.com/\S+",
    re.IGNORECASE,
)

_TRAILING = ".,;:!?)]}'\"»"


def extract_tiktok_url(text: str) -> str | None:
    """Extrait le premier lien TikTok présent dans un texte."""
    if not text:
        return None
    match = _TIKTOK_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(_TRAILING)


def _ffmpeg_exe() -> str:
    """Utilise ffmpeg du système s'il existe, sinon celui fourni par imageio."""
    return shutil.which("ffmpeg") or imageio_ffmpeg.get_ffmpeg_exe()


def _ffmpeg_dir() -> str:
    return os.path.dirname(_ffmpeg_exe())


def _ydl_options(workdir: str) -> dict:
    options = {
        "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
        "format": (
            "best[ext=mp4][vcodec^=h264]/"
            "bestvideo[vcodec^=h264]+bestaudio/"
            "best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "ffmpeg_location": _ffmpeg_dir(),
        # Beaucoup plus patient : TikTok peut être lent depuis un serveur.
        "socket_timeout": 60,
        "retries": 10,
        "fragment_retries": 10,
        "extractor_retries": 5,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
    }
    if _IMPERSONATE:
        options["impersonate"] = _IMPERSONATE
    return options


# Erreurs qui méritent une nouvelle tentative (réseau, lenteur, blocage passager).
_RETRY_HINTS = (
    "timed out",
    "timeout",
    "transport",
    "connection",
    "temporarily",
    "429",
    "500",
    "502",
    "503",
    "504",
    "unable to download webpage",
    "unable to download video data",
)


def _should_retry(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(hint in text for hint in _RETRY_HINTS)


def _find_video(workdir: str, video_id: str) -> str:
    """Retourne le fichier vidéo téléchargé."""
    extensions = {".mp4", ".mkv", ".webm", ".mov"}
    candidates = [
        p
        for p in glob.glob(os.path.join(workdir, f"{video_id}.*"))
        if os.path.splitext(p)[1].lower() in extensions
    ]
    if not candidates:
        candidates = [
            p
            for p in glob.glob(os.path.join(workdir, "*"))
            if os.path.splitext(p)[1].lower() in extensions
        ]
    if not candidates:
        raise RuntimeError("Fichier vidéo introuvable après téléchargement.")

    candidates.sort(key=os.path.getsize, reverse=True)
    return candidates[0]


def download_tiktok(url: str, workdir: str, attempts: int = 4) -> str:
    """Télécharge une vidéo TikTok et retourne le chemin du fichier.

    Réessaie plusieurs fois en cas de lenteur ou de blocage réseau passager.
    """
    os.makedirs(workdir, exist_ok=True)

    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with yt_dlp.YoutubeDL(_ydl_options(workdir)) as ydl:
                info = ydl.extract_info(url, download=True)
                video_id = (info or {}).get("id") or "video"
            return _find_video(workdir, video_id)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= attempts or not _should_retry(exc):
                raise
            # On attend un peu plus longtemps à chaque essai (2 s, 4 s, 6 s).
            time.sleep(2 * attempt)

    raise last_error  # pragma: no cover


def _probe(path: str) -> str:
    """Retourne les informations techniques affichées par ffmpeg."""
    result = subprocess.run(
        [_ffmpeg_exe(), "-i", path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    return result.stderr.decode("utf-8", "ignore")


def _transcode(src: str, dst: str, height: int | None, crf: int) -> None:
    cmd = [_ffmpeg_exe(), "-y", "-i", src]
    if height:
        cmd += ["-vf", f"scale=-2:{height}"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        str(crf),
        "-profile:v",
        "main",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        dst,
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg a échoué : " + result.stderr.decode("utf-8", "ignore")[-400:]
        )


def prepare_for_whatsapp(path: str, max_mb: int, workdir: str) -> str:
    """Garantit une vidéo compatible WhatsApp : H.264 + AAC, sous max_mb Mo."""
    limit = max_mb * 1024 * 1024
    info = _probe(path)

    video_ok = "Video: h264" in info
    audio_ok = ("Audio: aac" in info) or ("Audio:" not in info)
    size_ok = os.path.getsize(path) <= limit

    if video_ok and audio_ok and size_ok:
        return path

    # On convertit. D'abord en gardant la résolution, puis en réduisant si besoin.
    presets: list[tuple[int | None, int]] = [(None, 23), (720, 28), (540, 30), (480, 32), (360, 34)]
    best = path
    for index, (height, crf) in enumerate(presets):
        out = os.path.join(workdir, f"conv_{index}.mp4")
        try:
            _transcode(path, out, height, crf)
        except Exception:
            continue
        if os.path.getsize(out) <= limit:
            return out
        best = out
    return best


# Ancien nom conservé pour compatibilité.
compress_if_needed = prepare_for_whatsapp
