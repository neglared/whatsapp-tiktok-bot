"""Compteur de messages pour rester sous les 1 000 messages gratuits de WhatsApp.

WhatsApp offre 1 000 messages de service gratuits par mois et par numéro.
Au-delà, chaque message coûte ~0,0113 $ (tarif Haïti).

Ce module compte :
  - les messages envoyés aujourd'hui (plafond quotidien),
  - les messages envoyés ce mois-ci (plafond mensuel).

Le compteur est écrit dans un petit fichier JSON pour survivre à un redémarrage.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger("quota")

# Haïti = UTC-5 toute l'année (pas d'heure d'été).
HAITI = timezone(timedelta(hours=-5))

_lock = threading.Lock()


def _today() -> str:
    return datetime.now(HAITI).strftime("%Y-%m-%d")


def _month() -> str:
    return datetime.now(HAITI).strftime("%Y-%m")


class Quota:
    """Compte les messages sortants et décide si on a le droit d'en envoyer un."""

    OK = "ok"
    DAILY = "daily"        # plafond du jour atteint
    MONTHLY = "monthly"    # plafond du mois atteint

    def __init__(self, path: str, daily_limit: int, monthly_limit: int) -> None:
        self.path = path
        self.daily_limit = daily_limit
        self.monthly_limit = monthly_limit
        self._state = {"day": "", "day_count": 0, "month": "", "month_count": 0}
        self._load()

    # ------------------------------------------------------------------ disque
    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._state.update(data)
        except FileNotFoundError:
            pass
        except Exception:
            log.warning("Compteur illisible, on repart de zéro", exc_info=True)
        self._rollover()

    def _save(self) -> None:
        try:
            folder = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(folder, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(self._state, handle)
            os.replace(tmp, self.path)
        except Exception:
            log.warning("Impossible d'écrire le compteur", exc_info=True)

    def _rollover(self) -> None:
        """Remet les compteurs à zéro quand le jour / le mois change."""
        today, month = _today(), _month()
        if self._state.get("day") != today:
            self._state["day"] = today
            self._state["day_count"] = 0
        if self._state.get("month") != month:
            self._state["month"] = month
            self._state["month_count"] = 0

    # ------------------------------------------------------------------ lecture
    @property
    def day_count(self) -> int:
        with _lock:
            self._rollover()
            return int(self._state.get("day_count", 0))

    @property
    def month_count(self) -> int:
        with _lock:
            self._rollover()
            return int(self._state.get("month_count", 0))

    def check(self) -> str:
        """Réponse : OK, DAILY ou MONTHLY."""
        with _lock:
            self._rollover()
            if int(self._state.get("month_count", 0)) >= self.monthly_limit:
                return self.MONTHLY
            if int(self._state.get("day_count", 0)) >= self.daily_limit:
                return self.DAILY
            return self.OK

    # ------------------------------------------------------------------ écriture
    def spend(self, n: int = 1) -> None:
        """À appeler après chaque message réellement accepté par WhatsApp."""
        with _lock:
            self._rollover()
            self._state["day_count"] = int(self._state.get("day_count", 0)) + n
            self._state["month_count"] = int(self._state.get("month_count", 0)) + n
            self._save()

    def summary(self) -> str:
        with _lock:
            self._rollover()
            return (
                f"{self._state['day_count']}/{self.daily_limit} aujourd'hui, "
                f"{self._state['month_count']}/{self.monthly_limit} ce mois-ci"
            )
