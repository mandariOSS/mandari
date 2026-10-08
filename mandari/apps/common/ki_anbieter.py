# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Eine Konfiguration für jeden KI-Aufruf (Issue #950).

Schreibhilfe und Co-Editor in Work, Zusammenfassung, Bürger-Chat und KI-Verortung im Bürgerportal holen ihren
Endpunkt nur hier: ``endpunkt_fuer_work()`` bzw. ``endpunkt_fuer_insight()``. Anbieter, Basis-URL, Modell und
Schlüssel stehen im Admin (KI-Einstellungen, für Work auch je Organisation); die Positivliste erlaubter Hosts
kommt aus der Umgebung (``KI_ERLAUBTE_HOSTS``, ``mandari_dokumente.ki_hosts``). Es gibt keinen fest
eingebauten Anbieter und keinen stillen Rückfall: Fehlt etwas oder steht der Host nicht in der Liste, ist die
KI aus (``None``). Jede Adresse wird bei jedem Aufruf neu geprüft, auch wenn sie direkt in der Datenbank steht.

Ein Anbieterwechsel ist damit reine Konfiguration: Vorlage oder eigener Endpunkt, Modell und Schlüssel im
Admin, bei Bedarf der Host in ``KI_ERLAUBTE_HOSTS``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from mandari_dokumente.ki_hosts import (
    STANDARD_ERLAUBTE_HOSTS,
    gepruefter_host,
    ist_erlaubter_host,
    normalisiere_host,
)

if TYPE_CHECKING:
    from apps.tenants.models import Organization

logger = logging.getLogger(__name__)

#: Schlüssel der Vorlage für einen frei eingetragenen OpenAI-kompatiblen Endpunkt
EIGENER = "eigener"

#: Hinweis bei einer Adresse, deren Host nicht in der Positivliste steht
NICHT_FREIGEGEBEN = "nicht freigegebener Endpunkt (KI_ERLAUBTE_HOSTS)"

#: Hinweis bei einer Vorlage mit Basis-URL eines anderen Hosts: Anzeigename und Verarbeitungsort der Vorlage
#: gälten dann für einen anderen Anbieter (Einwilligung, Hinweise)
FREMDER_HOST = "Für einen anderen Host „Eigener Endpunkt“ wählen."

#: Hinweis im Protokoll, wenn der Anbieter eine Anfrage wegen ihrer Länge ablehnt (HTTP 400)
LAENGENLIMIT = (
    "Längenlimit des Modells überschritten: „Max. Output-Tokens“ in den KI-Einstellungen auf höchstens die "
    "dokumentierte maximale Antwortlänge des Modells beim Anbieter setzen"
)


@dataclass(frozen=True)
class AnbieterVorlage:
    """Bekannter OpenAI-kompatibler Anbieter: Anzeige, Vertragspartner, Verarbeitungsort, Standard-Basis-URL."""

    anzeigename: str
    vertragspartner: str
    verarbeitungsort: str
    basis_url: str


#: Vorlagen für die Auswahl im Admin. Nutzbar ist eine Vorlage nur, wenn ihr Host in der Positivliste steht.
ANBIETER_VORLAGEN: dict[str, AnbieterVorlage] = {
    "stackit": AnbieterVorlage(
        anzeigename="STACKIT AI Model Serving",
        vertragspartner="STACKIT GmbH & Co. KG",
        # „All models are operated in data centers in Germany and Austria“ (stackit.com, Wissen: LLM)
        verarbeitungsort="Rechenzentren in Deutschland und Österreich (EU)",
        basis_url="https://api.openai-compat.model-serving.eu01.onstackit.cloud/v1",
    ),
    "ionos": AnbieterVorlage(
        anzeigename="IONOS AI Model Hub",
        vertragspartner="IONOS SE",
        verarbeitungsort="Rechenzentren in Deutschland (EU)",
        basis_url="https://openai.inference.de-txl.ionos.com/v1",
    ),
    "scaleway": AnbieterVorlage(
        anzeigename="Scaleway Generative APIs",
        vertragspartner="Scaleway SAS",
        verarbeitungsort="Rechenzentren in Frankreich (EU)",
        basis_url="https://api.scaleway.ai/v1",
    ),
    # Basis-URL, Anzeigename und Verarbeitungsort sind hier Pflicht (Admin-Formular, Auflösung)
    EIGENER: AnbieterVorlage(
        anzeigename="Eigener OpenAI-kompatibler Endpunkt",
        vertragspartner="",
        verarbeitungsort="",
        basis_url="",
    ),
}

#: Auswahl für Modellfelder (Schlüssel, Anzeigename)
ANBIETER_AUSWAHL: list[tuple[str, str]] = [(key, vorlage.anzeigename) for key, vorlage in ANBIETER_VORLAGEN.items()]


def erlaubte_hosts() -> tuple[str, ...]:
    """Positivliste aus den Einstellungen (``KI_ERLAUBTE_HOSTS``); leer gilt der Standard."""
    eingestellt: Any = getattr(settings, "KI_ERLAUBTE_HOSTS", None) or ()
    if isinstance(eingestellt, str):
        eingestellt = eingestellt.split(",")
    hosts = tuple(dict.fromkeys(normalisiere_host(host) for host in eingestellt if str(host).strip()))
    return hosts or STANDARD_ERLAUBTE_HOSTS


def vorlage_nutzbar(anbieter: str) -> bool:
    """Steht der Host der Vorlage in der Positivliste? ``eigener`` hat keine Vorlage-URL und gilt als nutzbar."""
    vorlage = ANBIETER_VORLAGEN.get(anbieter)
    if vorlage is None:
        return False
    return anbieter == EIGENER or ist_erlaubter_host(vorlage.basis_url, erlaubte_hosts())


def vorlage_host(anbieter: str) -> str | None:
    """Host der Basis-URL einer Vorlage; ``eigener`` und unbekannte Anbieter haben keinen."""
    vorlage = ANBIETER_VORLAGEN.get(anbieter or "")
    if vorlage is None or not vorlage.basis_url:
        return None
    return gepruefter_host(vorlage.basis_url)


def fremder_host_fuer_vorlage(anbieter: str, url: str) -> bool:
    """
    Gehört die eingetragene Basis-URL zu einem anderen Host als die Vorlage?

    Bei einer Vorlage (außer ``eigener``) darf nur der Pfad abweichen; leer gilt die URL der Vorlage. Sonst
    nennten Einwilligung und Hinweise Anbieter und Verarbeitungsort der Vorlage für einen anderen Anbieter.
    """
    roh = (url or "").strip()
    host = vorlage_host(anbieter)
    if not roh or host is None:
        return False
    return gepruefter_host(roh) != host


def wirksamer_host(anbieter: str, url: str) -> str | None:
    """Host, an den die Aufrufe gingen: eingetragene Basis-URL, sonst die der Vorlage; ohne Vorlage ``None``."""
    if (anbieter or "") not in ANBIETER_VORLAGEN:
        return None
    roh = (url or "").strip() or ANBIETER_VORLAGEN[anbieter].basis_url
    return gepruefter_host(roh) if roh else None


#: Merkmale einer Ablehnung wegen der Länge in der Fehlerantwort (klein geschrieben)
_LAENGENLIMIT_MERKMALE = (
    "max_tokens",
    "max_completion_tokens",
    "max_new_tokens",
    "maximum context length",
    "context length",
    "context_length",
    "context window",
    "too many tokens",
    "maximum generation",
)


def ist_laengenlimit(status: int, antworttext: str) -> bool:
    """
    Lehnt der Anbieter die Anfrage wegen ihrer Länge ab (Antwortlänge oder Kontext über dem Limit des Modells)?

    OpenAI-kompatible Server (etwa vLLM) antworten dann mit HTTP 400 und nennen ``max_tokens`` oder die
    Kontextlänge. Der Antworttext wird nur durchsucht, nie protokolliert (er kann Teile der Anfrage enthalten).
    """
    if status not in (400, 413, 422):
        return False
    text = (antworttext or "").lower()
    return any(merkmal in text for merkmal in _LAENGENLIMIT_MERKMALE)


def _host_fuer_protokoll(url: str) -> str:
    """Hostname einer (auch unzulässigen) Adresse für Warnungen; nie Pfad, Abfrage oder Zugangsdaten."""
    try:
        return urlsplit((url or "").strip()).hostname or "(keiner)"
    except ValueError:
        return "(unlesbar)"


def pruefe_basis_url(url: str) -> str:
    """
    Normalisierte Basis-URL (``https://host/pfad`` ohne Schrägstrich am Ende) oder ``ValidationError``.

    Verlangt wird ``https`` ohne Zugangsdaten und ohne Port außer 443, ein Host aus der Positivliste und weder
    Abfrage noch Fragment.
    """
    roh = (url or "").strip()
    if not roh:
        raise ValidationError("Basis-URL fehlt.", code="leer")
    if not ist_erlaubter_host(roh, erlaubte_hosts()):
        raise ValidationError(f"Basis-URL: {NICHT_FREIGEGEBEN}.", code="nicht_freigegeben")
    teile = urlsplit(roh)
    if teile.query or teile.fragment:
        raise ValidationError("Basis-URL ohne Abfrage und ohne Fragment angeben.", code="abfrage")
    return f"https://{gepruefter_host(roh)}{teile.path.rstrip('/')}"


@dataclass(frozen=True)
class KiHinweis:
    """Was die Oberfläche über den Anbieter zeigen darf (Einwilligung, Hinweise), ohne Zugangsdaten."""

    anbieter: str
    anzeigename: str
    verarbeitungsort: str
    host: str

    @property
    def einwilligungskennung(self) -> str:
        """
        Eine Einwilligung gilt nur für diesen Anbieter an diesem Host mit genau diesem Anzeigenamen und
        Verarbeitungsort; ändert sich eins davon, wird erneut gefragt.
        """
        return json.dumps([self.anbieter, self.host, self.anzeigename, self.verarbeitungsort], ensure_ascii=False)


@dataclass(frozen=True)
class KiEndpunkt:
    """Aufgelöster, geprüfter Endpunkt eines KI-Aufrufs. ``repr`` und ``str`` nennen den Schlüssel nie."""

    anbieter: str
    anzeigename: str
    verarbeitungsort: str
    base_url: str
    api_key: str = field(repr=False)
    modell: str
    ausweichmodell: str = ""
    #: Obergrenze der Antwortlänge je Aufruf; 0 = keine eigene Grenze
    max_output_tokens: int = 0

    def __repr__(self) -> str:
        return (
            f"KiEndpunkt(anbieter={self.anbieter!r}, host={self.host!r}, modell={self.modell!r}, "
            f"ausweichmodell={self.ausweichmodell!r}, max_output_tokens={self.max_output_tokens})"
        )

    __str__ = __repr__

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def chat_url(self) -> str:
        """Adresse der OpenAI-kompatiblen Chat-Schnittstelle."""
        return self.base_url.rstrip("/") + "/chat/completions"

    def hinweis(self) -> KiHinweis:
        return KiHinweis(
            anbieter=self.anbieter, anzeigename=self.anzeigename, verarbeitungsort=self.verarbeitungsort, host=self.host
        )


def _baue_endpunkt(
    *,
    quelle: str,
    anbieter: str,
    base_url: str,
    anzeigename: str,
    verarbeitungsort: str,
    api_key: str,
    modell: str,
    ausweichmodell: str = "",
    max_output_tokens: int = 0,
) -> KiEndpunkt | None:
    """Endpunkt aus Einstellungen; unvollständig oder nicht freigegeben ergibt ``None`` mit Warnung (ohne Schlüssel)."""
    vorlage = ANBIETER_VORLAGEN.get(anbieter or "")
    if vorlage is None:
        logger.warning("KI aus (%s): kein freigegebener Anbieter gewählt", quelle)
        return None
    if fremder_host_fuer_vorlage(anbieter, base_url):
        logger.warning(
            "KI aus (%s): Basis-URL mit Host %s passt nicht zur Vorlage %s. %s",
            quelle,
            _host_fuer_protokoll(base_url),
            anbieter,
            FREMDER_HOST,
        )
        return None
    try:
        geprueft = pruefe_basis_url(base_url or vorlage.basis_url)
    except ValidationError:
        logger.warning(
            "KI aus (%s): Endpunkt gesperrt, Host %s steht nicht in KI_ERLAUBTE_HOSTS",
            quelle,
            _host_fuer_protokoll(base_url or vorlage.basis_url),
        )
        return None
    anzeigename = (anzeigename or "").strip() or (vorlage.anzeigename if anbieter != EIGENER else "")
    verarbeitungsort = (verarbeitungsort or "").strip() or vorlage.verarbeitungsort
    if not anzeigename or not verarbeitungsort:
        logger.warning("KI aus (%s): Anzeigename und Verarbeitungsort des Endpunkts fehlen", quelle)
        return None
    if not (modell or "").strip():
        logger.warning("KI aus (%s): kein Modell eingetragen", quelle)
        return None
    return KiEndpunkt(
        anbieter=anbieter,
        anzeigename=anzeigename,
        verarbeitungsort=verarbeitungsort,
        base_url=geprueft,
        api_key=api_key,
        modell=modell.strip(),
        ausweichmodell=(ausweichmodell or "").strip(),
        max_output_tokens=max(0, int(max_output_tokens or 0)),
    )


def endpunkt_fuer_work(organization: Organization | None = None) -> KiEndpunkt | None:
    """
    Endpunkt für Schreibhilfe und Co-Editor in Work.

    1. Organisation mit eigenem Schlüssel: ihr Anbieter, ihre Basis-URL, ihr Modell (kein Rückfall auf die
       Plattform, wenn ihre Angaben nicht taugen).
    2. Sonst die KI-Einstellungen, nur wenn „KI in Work aktiviert“ und ein Schlüssel gesetzt ist.
    3. Sonst ``None``.

    ``organization.ai_enabled`` aus und die öffentliche Demo ergeben immer ``None``.
    """
    if getattr(settings, "DEMO_INSTANCE", False):
        return None
    if organization is not None and not organization.ai_enabled:
        return None

    org_key = organization.get_ai_api_key() if organization is not None else ""
    if organization is not None and org_key:
        return _baue_endpunkt(
            quelle=f"Organisation {organization.pk}",
            anbieter=organization.ai_provider or "",
            base_url=organization.ai_base_url or "",
            anzeigename=organization.ai_anzeigename or "",
            verarbeitungsort=organization.ai_verarbeitungsort or "",
            api_key=org_key,
            modell=organization.ai_model or "",
        )

    from .models import AISettings

    ki = AISettings.get_settings()
    if not ki.enabled:
        return None
    key = ki.get_api_key()
    if not key:
        return None
    return _baue_endpunkt(
        quelle="KI-Einstellungen, Work",
        anbieter=ki.provider or "",
        base_url=ki.base_url or "",
        anzeigename=ki.anzeigename or "",
        verarbeitungsort=ki.verarbeitungsort or "",
        api_key=key,
        modell=ki.model_name or "",
        ausweichmodell=ki.fallback_model or "",
        max_output_tokens=ki.max_output_tokens,
    )


def endpunkt_fuer_insight() -> KiEndpunkt | None:
    """
    Endpunkt für Zusammenfassung, Bürger-Chat und KI-Verortung im Bürgerportal.

    Nur aus den KI-Einstellungen, nur mit „KI im Bürgerportal aktiviert“ und gesetztem Schlüssel. Modell ist das
    Bürgerportal-Modell, sonst das allgemeine; die Antwortlänge begrenzt ``insight_max_output_tokens``.
    """
    if getattr(settings, "DEMO_INSTANCE", False):
        return None

    from .models import AISettings

    ki = AISettings.get_settings()
    if not ki.insight_enabled:
        return None
    key = ki.get_api_key()
    if not key:
        return None
    return _baue_endpunkt(
        quelle="KI-Einstellungen, Bürgerportal",
        anbieter=ki.provider or "",
        base_url=ki.base_url or "",
        anzeigename=ki.anzeigename or "",
        verarbeitungsort=ki.verarbeitungsort or "",
        api_key=key,
        modell=(ki.insight_model or "").strip() or ki.model_name or "",
        ausweichmodell=ki.fallback_model or "",
        max_output_tokens=ki.insight_max_output_tokens,
    )
