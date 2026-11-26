# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anbindung je Mandant: Adresse und API-Schlüssel des Protokolldienstes, Rückgabeschlüssel, OParl-Kennungen.

**Adresse:** nur ``https`` und ein öffentlicher Name (kein ``localhost``, keine Adresse aus dem eigenen Netz, kein
Dienstname im Container-Netz). Wer den Dienst im eigenen Netz betreibt, nennt dessen Host in
``PROTOKOLLDIENST_ERLAUBTE_HOSTS``; ist die Liste gesetzt, sind nur diese Hosts zulässig. ``http`` (der API-Schlüssel
ginge im Klartext über das Netz) nur für diese Hosts und nur mit der ausdrücklichen Freigabe
``PROTOKOLLDIENST_HTTP_ERLAUBT``. Die Prüfung beim Speichern ersetzt nicht die beim Abruf (``client``): Dort wird der
Name aufgelöst und nur mit einer geprüften Adresse verbunden.

**Schlüssel:** Den API-Schlüssel des Dienstes verschlüsselt der Mandantenschlüssel (``api_key_encrypted``). Den
Rückgabeschlüssel, mit dem der Dienst Entwürfe übergibt, erzeugt Session; gespeichert ist nur sein SHA-256, angezeigt
wird er genau einmal.

**Zuordnung:** Der Dienst übernimmt Sitzungen über die OParl-Schnittstelle des Mandanten und kennt sie unter deren
Kennung (``id``). Dieselbe Kennung bildet ``sitzung_kennung``.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from typing import Any, Final, cast
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.common import tokens
from apps.session.models import SessionAgendaItem, SessionMeeting, SessionProtokolldienst, SessionTenant

#: Höchstlänge der Adresse (wie das Feld)
MAX_ADRESSE: Final = 300

UNGUELTIG: Final = "Bitte eine vollständige Adresse angeben, z. B. https://protokoll.example.org."
NUR_HTTPS: Final = "Der Protokolldienst muss über https erreichbar sein."
EIGENES_NETZ: Final = "Die Adresse zeigt ins eigene Netz. Solche Hosts gibt nur der Betrieb frei."
NICHT_FREIGEGEBEN: Final = "Diese Installation lässt nur freigegebene Protokolldienste zu."


def erlaubte_hosts() -> frozenset[str]:
    """Hosts aus ``PROTOKOLLDIENST_ERLAUBTE_HOSTS`` (klein geschrieben); leer heißt: jeder öffentliche Host."""
    eintraege = getattr(settings, "PROTOKOLLDIENST_ERLAUBTE_HOSTS", ()) or ()
    return frozenset(str(host).strip().lower().rstrip(".") for host in eintraege if str(host).strip())


def host_von(adresse: str) -> str:
    return (urlsplit(adresse).hostname or "").lower().rstrip(".")


def freigegebener_host(adresse: str) -> bool:
    """Steht der Host der Adresse in ``PROTOKOLLDIENST_ERLAUBTE_HOSTS``?"""
    return host_von(adresse) in erlaubte_hosts()


def http_erlaubt(adresse: str) -> bool:
    """Darf der Client ohne TLS verbinden? Nur zu freigegebenen Hosts und nur mit ``PROTOKOLLDIENST_HTTP_ERLAUBT``."""
    return bool(getattr(settings, "PROTOKOLLDIENST_HTTP_ERLAUBT", False)) and freigegebener_host(adresse)


def adresse_pruefen(roh: str) -> str:
    """Adresse des Protokolldienstes ohne abschließenden Schrägstrich; wirft ``ValidationError`` mit fester Meldung."""
    adresse = (roh or "").strip().rstrip("/")
    try:
        teile = urlsplit(adresse)
    except ValueError:
        raise ValidationError(UNGUELTIG) from None
    host = (teile.hostname or "").lower().rstrip(".")
    if not host or teile.scheme not in ("http", "https") or len(adresse) > MAX_ADRESSE or teile.query or teile.fragment:
        raise ValidationError(UNGUELTIG)
    if teile.username or teile.password:
        raise ValidationError(UNGUELTIG)
    freigegeben = erlaubte_hosts()
    if freigegeben and host not in freigegeben:
        raise ValidationError(NICHT_FREIGEGEBEN)
    if teile.scheme != "https" and not http_erlaubt(adresse):
        raise ValidationError(NUR_HTTPS)
    if freigegeben:
        return adresse
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        ip = None
    if ip is not None:
        if not ip.is_global:
            raise ValidationError(EIGENES_NETZ)
        return adresse
    endung = host.rsplit(".", 1)[-1]
    if "." not in host or host.endswith(".localhost") or not (endung.isalpha() or endung.startswith("xn--")):
        raise ValidationError(EIGENES_NETZ)
    return adresse


def fuer_mandant(tenant: SessionTenant) -> SessionProtokolldienst | None:
    """Anbindung des Mandanten, falls angelegt (eingeschaltet oder nicht)."""
    return SessionProtokolldienst.objects.filter(tenant=tenant).first()


def aktiv(tenant: SessionTenant) -> SessionProtokolldienst | None:
    """Anbindung, wenn sie eingeschaltet und vollständig ist; sonst ``None`` (dann geht nichts hinaus)."""
    konfiguration = fuer_mandant(tenant)
    return konfiguration if konfiguration is not None and konfiguration.configured else None


def speichern(
    tenant: SessionTenant, *, enabled: bool, base_url: str, api_key: str | None
) -> tuple[SessionProtokolldienst, dict[str, Any]]:
    """
    Anbindung anlegen oder ändern. ``api_key=None`` (leeres Feld) lässt den gespeicherten Schlüssel stehen.

    Eingeschaltet werden kann nur mit Adresse und Schlüssel. Liefert die Anbindung und die Änderungen für das
    Prüfprotokoll (ohne Schlüssel). Gespeichert werden nur diese Felder: Der Rückgabeschlüssel kommt nie aus einem
    älteren Stand zurück (``rueckgabe_schluessel_erzeugen`` kann gleichzeitig laufen).
    """
    konfiguration = fuer_mandant(tenant) or SessionProtokolldienst(tenant=tenant)
    adresse = adresse_pruefen(base_url) if (base_url or "").strip() else ""
    aenderungen: dict[str, Any] = {}
    if adresse != konfiguration.base_url:
        aenderungen["adresse"] = adresse or None
        konfiguration.base_url = adresse
    if api_key:
        cast(Any, konfiguration).set_api_key_encrypted(api_key.strip())
        aenderungen["api_schluessel"] = "neu hinterlegt"
    if enabled and not (konfiguration.base_url and konfiguration.api_key_encrypted):
        raise ValidationError("Zum Einschalten braucht es Adresse und API-Schlüssel des Protokolldienstes.")
    if enabled != konfiguration.enabled:
        aenderungen["eingeschaltet"] = enabled
        konfiguration.enabled = enabled
    if konfiguration._state.adding:
        konfiguration.save()
    else:
        konfiguration.save(update_fields=["base_url", "api_key_encrypted", "enabled", "updated_at"])
    return konfiguration, aenderungen


def api_schluessel(konfiguration: SessionProtokolldienst) -> str:
    """API-Schlüssel des Dienstes im Klartext (nur für den Aufruf, nie in Logs)."""
    return str(cast(Any, konfiguration).get_api_key_decrypted() or "")


def rueckgabe_schluessel_erzeugen(konfiguration: SessionProtokolldienst) -> str:
    """Neuen Rückgabeschlüssel erzeugen; der bisherige gilt ab sofort nicht mehr. Liefert ihn im Klartext (einmal)."""
    roh = tokens.new_token()
    konfiguration.rueckgabe_token_hash = tokens.hash_token(roh)
    konfiguration.rueckgabe_token_prefix = roh[:8]
    konfiguration.rueckgabe_token_created_at = timezone.now()
    konfiguration.save(
        update_fields=["rueckgabe_token_hash", "rueckgabe_token_prefix", "rueckgabe_token_created_at", "updated_at"]
    )
    return roh


def rueckgabe_schluessel_passt(konfiguration: SessionProtokolldienst, roh: object) -> bool:
    """Passt der Schlüssel zur Anbindung? Vergleich in konstanter Zeit, ohne gespeicherten Schlüssel nie."""
    return tokens.token_matches(roh, konfiguration.rueckgabe_token_hash)


# --- Kennungen ------------------------------------------------------------------------------------------------------


def _uris(tenant: SessionTenant) -> Any:
    from apps.session.services.insight_service import oparl_system_url
    from hub.ris.mapping.session import SessionUris

    return SessionUris(oparl_system_url(tenant))


def sitzung_kennung(meeting: SessionMeeting) -> str:
    """OParl-Kennung der Sitzung, unter der der Protokolldienst sie übernommen hat."""
    return str(_uris(meeting.tenant).obj("meeting", meeting.pk))


def top_kennung(item: SessionAgendaItem) -> str:
    """OParl-Kennung eines TOP (nur öffentliche TOPs öffentlicher Sitzungen erscheinen in der OParl-Ausgabe)."""
    return str(_uris(item.meeting.tenant).obj("agendaitem", item.pk))


_KENNUNG = re.compile(r"/(?P<art>meeting|agendaitem)/(?P<pk>[0-9a-fA-F-]{36})/?$")


def kennung_aufloesen(kennung: str, art: str) -> uuid.UUID | None:
    """
    Kennung (UUID) aus einer OParl-Kennung des Mandanten (``…/meeting/<uuid>/``) oder aus einer bloßen UUID; sonst
    ``None``. Ob das Objekt zum Mandanten gehört, prüft der Aufrufer.
    """
    wert = (kennung or "").strip()
    try:
        return uuid.UUID(wert)
    except ValueError:
        pass
    treffer = _KENNUNG.search(wert)
    if treffer is None or treffer.group("art") != art:
        return None
    try:
        return uuid.UUID(treffer.group("pk"))
    except ValueError:
        return None


def gleiche_kennung(a: str, b: str) -> bool:
    """Gleiche OParl-Kennung, unabhängig vom abschließenden Schrägstrich."""
    return bool(a) and bool(b) and a.rstrip("/") == b.rstrip("/")
