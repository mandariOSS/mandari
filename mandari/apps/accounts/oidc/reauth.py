# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Erneute Anmeldung auf Wunsch einer Anwendung durchsetzen (``prompt=login`` und ``max_age``, Issue #1206).

Verlangt eine Anwendung eine erneute Anmeldung, leitet der Autorisierungs-Endpunkt zur Anmeldeseite weiter. Ziel
danach ist dieselbe Anfrage ohne ``prompt`` und ``max_age``. Damit dieses Ziel nicht ohne Anmeldung zum Code
führt (etwa an einem unbeaufsichtigten, noch angemeldeten Browser), merkt sich die Sitzung den Wunsch, gebunden
an Client-ID und ``state``. Erfüllt ist er erst mit der nächsten Anmeldung in dieser Sitzung (Signal
``user_logged_in``); bis dahin führt die Anfrage wieder zur Anmeldung, mit ``prompt=none`` zu ``login_required``.

Gemerkt werden höchstens :data:`MAX_PENDING` Wünsche. Kommen mehr zusammen, verdrängt keiner einen anderen:
Dann gilt bis zur nächsten Anmeldung jede Anfrage als offen.

Ob eine Anmeldung jung genug ist (``max_age``) und welcher Zeitpunkt als ``auth_time`` ins ID-Token kommt, richtet
sich nach der Anmeldung in dieser Sitzung (:func:`login_time`), nicht nach ``User.last_login``: Den setzt jede
Anmeldung auf irgendeinem Gerät neu, eine alte, unbeaufsichtigte Sitzung würde damit wieder „frisch“ (#1215).
Eine Sitzung ohne gemerkten Zeitpunkt (angemeldet vor dieser Regel) gilt für ``max_age`` als zu alt.

Ebenso merkt sich die Sitzung, ob die letzte Anmeldung mit zweitem Faktor war (:func:`mark_second_factor`, aus
``apps.accounts.views.complete_login``). Geschützte Scopes (#565) verlangen das von der Anmeldung selbst: Ein Konto,
das den Faktor erst nach einer Anmeldung nur mit Passwort einrichtet, meldet sich dafür erneut an.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from django.contrib.auth.signals import user_logged_in
from django.utils import timezone

SESSION_KEY = "oidc_erneute_anmeldung"
#: Zeitpunkt der letzten Anmeldung in dieser Sitzung (Unix-Sekunden)
LOGIN_TIME_KEY = "oidc_anmeldezeit"
#: Die letzte Anmeldung in dieser Sitzung war mit zweitem Faktor (Code, Backup-Code oder Sicherheitsschlüssel)
SECOND_FACTOR_KEY = "oidc_anmeldung_zweiter_faktor"
#: Höchstzahl gemerkter Wünsche je Sitzung
MAX_PENDING = 20
#: Steht für „jede Anfrage“, sobald mehr als :data:`MAX_PENDING` Wünsche offen sind
ALL = "*"


def _key(client_id: str, state: str) -> str:
    """Kurzer, eindeutiger Schlüssel; ``state`` kann lang sein und gehört nicht im Klartext in die Sitzung."""
    return hashlib.sha256(json.dumps([client_id, state]).encode()).hexdigest()[:32]


def _pending(request: Any) -> list[str]:
    session = getattr(request, "session", None)
    value = session.get(SESSION_KEY) if session is not None else None
    return [str(item) for item in value] if isinstance(value, list) else []


def require(request: Any, client_id: str, state: str) -> None:
    """Für diese Anfrage gibt es erst nach der nächsten Anmeldung in der Sitzung einen Code."""
    pending = _pending(request)
    key = _key(client_id, state)
    if ALL in pending or key in pending:
        return
    request.session[SESSION_KEY] = [ALL] if len(pending) >= MAX_PENDING else [*pending, key]


def pending(request: Any, client_id: str, state: str) -> bool:
    """Wartet diese Anfrage noch auf die erneute Anmeldung?"""
    waiting = _pending(request)
    return ALL in waiting or _key(client_id, state) in waiting


def login_time(request: Any) -> datetime | None:
    """Wann hat sich die Person in dieser Sitzung zuletzt angemeldet? ``None``: unbekannt."""
    session = getattr(request, "session", None)
    value = session.get(LOGIN_TIME_KEY) if session is not None else None
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def login_with_second_factor(request: Any) -> bool:
    """War die letzte Anmeldung in dieser Sitzung mit zweitem Faktor?"""
    session = getattr(request, "session", None)
    return bool(session is not None and session.get(SECOND_FACTOR_KEY) is True)


def mark_second_factor(request: Any) -> None:
    """Die eben abgeschlossene Anmeldung war mit zweitem Faktor (nach ``login``, das den Vermerk zurücksetzt)."""
    session = getattr(request, "session", None)
    if session is not None:
        session[SECOND_FACTOR_KEY] = True


def on_user_logged_in(sender: Any, request: Any = None, user: Any = None, **kwargs: Any) -> None:
    """
    Signal ``user_logged_in``: Die Anmeldung erfüllt alle offenen Wünsche dieser Sitzung und gilt ab jetzt. Ob sie mit
    zweitem Faktor war, vermerkt erst der Anmeldeschritt selbst (:func:`mark_second_factor`).
    """
    session = getattr(request, "session", None)
    if session is not None:
        session.pop(SESSION_KEY, None)
        session.pop(SECOND_FACTOR_KEY, None)
        session[LOGIN_TIME_KEY] = int(timezone.now().timestamp())


def connect() -> None:
    """Signal-Empfänger verbinden (idempotent über ``dispatch_uid``)."""
    user_logged_in.connect(on_user_logged_in, dispatch_uid="oidc_reauth_logged_in")
