# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Demo-Organisationen mit Laufzeit (Issue #1406).

Eine Organisation mit ``demo_laufzeit_tage`` ist eine Demo. Ihre Laufzeit beginnt, wenn die erste Einladung
angenommen wird (:func:`laufzeit_starten`); daraus entsteht ``demo_endet_am``. Danach sperrt der stündliche
Zeitplan ``demo_ablauf`` die Organisation (:func:`abgelaufene_sperren`, nur ``is_active``; alle Daten bleiben).
Konten, die nur noch über eine abgelaufene Demo Zugang hätten, können sich nicht mehr anmelden und sehen den
Hinweis „Demozeit abgelaufen“ (:func:`abgelaufene_demo_fuer`) mit dem Weg zur kostenpflichtigen Verlängerung.

Ist eine Löschfrist eingestellt (``DEMO_LOESCHFRIST_TAGE``, Issue #1575; ohne Einstellung bleiben gesperrte Demos wie
bisher unbegrenzt), erinnert derselbe Zeitplan die Administratoren 7 Tage und 24 Stunden vorher und löscht eine nicht
verlängerte Demo nach der Frist endgültig (:func:`faellige_loeschen`): die Organisation mit allem, was an ihr hängt,
die Dateien dieser Einträge, was Fachmodule zusätzlich ablegen (:func:`beim_loeschen_melden`), und Konten, die danach
nirgends mehr gebunden sind.

Organisationen der Bereitstellung (``provisioning_ref``) sind nie Demos: Übernimmt die Provisioning-API eine Demo
bei einem Abschluss, entfallen die Demo-Angaben, und ihr Status gilt allein. Gelöscht wird nur, was gesperrt ist
(``is_active`` aus): Eine im Admin wieder aktivierte Demo bleibt, bis sie erneut abläuft.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import DEFAULT_DB_ALIAS, models, transaction
from django.db.models import Q
from django.db.models.deletion import Collector
from django.utils import timezone

from .models import Membership, Organization, UserInvitation

logger = logging.getLogger(__name__)

#: Hinweis bei der Anmeldung und auf der Hinweisseite
HINWEIS = "Die Demozeit ist abgelaufen."

#: Höchstens so viele Löschungen je Lauf (stündlich); der Rest folgt im nächsten Lauf. Begrenzt den Schaden, falls
#: Demo-Angaben einmal versehentlich an vielen Organisationen stehen.
LOESCHUNGEN_JE_LAUF = 5

#: Erinnerungen an die Administratoren vor der Löschung, in Stunden vorher (kleinste zuerst): 24 Stunden und 7 Tage
ERINNERUNG_STUNDEN = (24, 7 * 24)

#: Mailart der Erinnerung (Metrik, Postausgang)
MAILART_ERINNERUNG = "plattform.demo_erinnerung"

#: Zugänge außerhalb von Work (z. B. Session), mit denen ein Konto trotz abgelaufener Demo angemeldet bleiben darf.
#: Die Plattform kennt keine Fachmodule; diese melden ihre Prüfung in ``AppConfig.ready`` an.
_weitere_zugaenge: list[Callable[[Any], bool]] = []

#: Aufräumen der Fachmodule beim Löschen einer Demo (z. B. Verzeichnisse außerhalb der Dateifelder). Aufgerufen in
#: der Transaktion vor dem Löschen; Dateien erst nach erfolgreicher Transaktion entfernen (``transaction.on_commit``).
_beim_loeschen: list[Callable[[Organization], None]] = []


def weiteren_zugang_melden(pruefung: Callable[[Any], bool]) -> None:
    """Prüfung „Konto hat hier Zugang“ eines Fachmoduls anmelden (einmal je Prozess, doppelte zählen einmal)."""
    if pruefung not in _weitere_zugaenge:
        _weitere_zugaenge.append(pruefung)


def loeschfrist() -> timedelta | None:
    """
    Frist vom Ende einer Demo bis zu ihrer Löschung (``DEMO_LOESCHFRIST_TAGE``, Issue #1575); ``None`` = gesperrte
    Demos bleiben unbegrenzt erhalten (Verhalten bis 0.13).
    """
    tage = getattr(settings, "DEMO_LOESCHFRIST_TAGE", None)
    return timedelta(days=int(tage)) if tage else None


def beim_loeschen_melden(aufraeumen: Callable[[Organization], None]) -> None:
    """Aufräumen eines Fachmoduls für gelöschte Demos anmelden (einmal je Prozess, doppelte zählen einmal)."""
    if aufraeumen not in _beim_loeschen:
        _beim_loeschen.append(aufraeumen)


def abgelaufen_q(jetzt: datetime | None = None, *, prefix: str = "") -> Q:
    """
    Bedingung „Demo abgelaufen“ für Organisationen (``prefix`` z. B. ``"organization__"``). Ohne Laufzeit ist eine
    Organisation keine Demo, auch wenn noch ein Enddatum gespeichert ist (Laufzeit im Admin geleert).
    """
    jetzt = jetzt or timezone.now()
    return Q(
        **{
            f"{prefix}demo_endet_am__lte": jetzt,
            f"{prefix}demo_laufzeit_tage__isnull": False,
            f"{prefix}provisioning_ref__isnull": True,
        }
    )


def ist_abgelaufen(organization: Organization, jetzt: datetime | None = None) -> bool:
    """True, wenn die Demo-Zeit der Organisation vorbei ist."""
    ende = organization.demo_endet_am
    return (
        organization.provisioning_ref is None
        and organization.demo_laufzeit_tage is not None
        and ende is not None
        and ende <= (jetzt or timezone.now())
    )


def laufzeit_starten(organization: Organization, jetzt: datetime | None = None) -> bool:
    """
    Laufzeit einer Demo beginnen (bei der ersten angenommenen Einladung). True, wenn ``demo_endet_am`` dabei
    gesetzt wurde; eine laufende oder beendete Demo und Organisationen ohne Laufzeit bleiben unverändert.
    """
    tage = organization.demo_laufzeit_tage
    if not tage or organization.demo_endet_am is not None or organization.provisioning_ref is not None:
        return False
    ende = (jetzt or timezone.now()) + timedelta(days=tage)
    # Bedingungen im UPDATE: Eine zweite Annahme mit einer vorher geladenen Organisation überschreibt weder ein
    # schon gesetztes Ende noch eine Verlängerung
    gesetzt = Organization.objects.filter(
        pk=organization.pk,
        demo_endet_am__isnull=True,
        demo_laufzeit_tage__isnull=False,
        provisioning_ref__isnull=True,
    ).update(demo_endet_am=ende)
    if not gesetzt:
        return False
    organization.demo_endet_am = ende
    logger.info("Demo %s läuft bis %s", organization.slug, organization.demo_endet_am.isoformat())
    return True


def abgelaufene_demo_fuer(user: Any, jetzt: datetime | None = None) -> Organization | None:
    """
    Die abgelaufene Demo, wenn das Konto außer ihr keinen Zugang mehr hat, sonst None.

    Zugang behalten: Plattform-Administration, aktive Mitgliedschaften in einer aktiven Organisation, die keine
    abgelaufene Demo ist, eine Mitgliedschaft im Auslauf (Datenexport), offene Einladungen und Zugänge anderer
    Module (:func:`weiteren_zugang_melden`, z. B. Session).
    """
    if not getattr(user, "is_authenticated", False) or user.is_staff or user.is_superuser:
        return None
    jetzt = jetzt or timezone.now()
    mitgliedschaften = Membership.objects.filter(user=user, is_active=True)
    demo = (
        mitgliedschaften.filter(abgelaufen_q(jetzt, prefix="organization__"))
        .select_related("organization")
        .order_by("-organization__demo_endet_am")
        .first()
    )
    if demo is None:
        return None
    andere = mitgliedschaften.filter(organization__is_active=True).exclude(abgelaufen_q(jetzt, prefix="organization__"))
    # Mitgliedschaft im Auslauf „Nur Lesen“ (Issue #1237): Die Auslauf-Seite mit dem eigenen Datenexport bleibt
    auslauf = Membership.objects.filter(
        user=user, is_active=False, is_guest=False, deletion_scheduled_at__isnull=False, organization__is_active=True
    ).exclude(abgelaufen_q(jetzt, prefix="organization__"))
    if andere.exists() or auslauf.exists() or any(pruefung(user) for pruefung in _weitere_zugaenge):
        return None
    # Eine offene Einladung in eine andere Organisation braucht die Anmeldung, um sie anzunehmen
    einladungen = UserInvitation.objects.filter(
        email__iexact=user.email, accepted_at__isnull=True, expires_at__gt=jetzt, organization__is_active=True
    ).exclude(abgelaufen_q(jetzt, prefix="organization__"))
    if einladungen.exists():
        return None
    return demo.organization


def kontakt() -> str:
    """
    Adresse für die Verlängerung: ``DEMO_KONTAKT_EMAIL``, sonst der Absender der Plattform (leer, wenn keiner
    eingerichtet ist).
    """
    from email.utils import parseaddr

    from apps.common.email import get_from_email

    eingestellt = getattr(settings, "DEMO_KONTAKT_EMAIL", "")
    if eingestellt:
        return str(eingestellt)
    try:
        return parseaddr(get_from_email())[1]
    except Exception:  # Hinweis ohne Adresse statt Fehler bei der Anmeldung
        logger.warning("Demo-Hinweis: Absender der Plattform nicht lesbar")
        return ""


def verlaengern_url() -> str:
    """Link zur Buchung der Verlängerung (``DEMO_VERLAENGERN_URL``, leer = keiner)."""
    return str(getattr(settings, "DEMO_VERLAENGERN_URL", "") or "")


def loeschung_am(organization: Organization) -> datetime | None:
    """
    Ab wann eine abgelaufene Demo gelöscht wird; ``None`` ohne Löschfrist, ohne Laufzeit, ohne Ende oder bei einer
    Bereitstellung.
    """
    frist = loeschfrist()
    ende = organization.demo_endet_am
    if frist is None or organization.provisioning_ref is not None or organization.demo_laufzeit_tage is None:
        return None
    return None if ende is None else ende + frist


def _datum(zeitpunkt: datetime) -> str:
    return timezone.localtime(zeitpunkt).strftime("%d.%m.%Y")


def hinweis(organization: Organization) -> str:
    """Text für Anmeldung und Hinweisseite: Name, Enddatum, wie lange die Daten bleiben, Weg zur Verlängerung."""
    text = HINWEIS
    if organization.demo_endet_am is not None:
        text = f"Die Demozeit für „{organization.name}“ ist am {_datum(organization.demo_endet_am)} abgelaufen."
    loeschung = loeschung_am(organization)
    if loeschung is not None:
        text += (
            f" Eure Daten bleiben bis zum {_datum(loeschung)} erhalten, danach löschen wir die Organisation mit allen "
            "Daten."
        )
    else:
        text += " Eure Daten bleiben erhalten."
    adresse, url = kontakt(), verlaengern_url()
    if url and adresse:
        text += (
            f" Zum Weitermachen bucht ihr die kostenpflichtige Verlängerung unter {url} oder schreibt uns an {adresse}."
        )
    elif url:
        text += f" Zum Weitermachen bucht ihr die kostenpflichtige Verlängerung unter {url}."
    elif adresse:
        text += f" Zum Weitermachen schreibt uns für die kostenpflichtige Verlängerung an {adresse}."
    else:
        text += " Zum Weitermachen braucht es die kostenpflichtige Verlängerung."
    return text


def abgelaufene_sperren(jetzt: datetime | None = None) -> list[Organization]:
    """Aktive Demos nach ihrem Ende sperren (nur ``is_active``); liefert die gesperrten Organisationen."""
    jetzt = jetzt or timezone.now()
    # Bedingung unter Zeilensperre prüfen: Eine gleichzeitige Verlängerung im Admin oder eine Übernahme durch die
    # Bereitstellung wartet und wird danach nicht mehr gesperrt
    with transaction.atomic():
        faellig = list(
            Organization.objects.select_for_update().filter(abgelaufen_q(jetzt), is_active=True).order_by("slug")
        )
        if faellig:
            # Jede neue Sperre beginnt ohne verschickte Erinnerung (nach einer Verlängerung wieder beide Stufen)
            Organization.objects.filter(pk__in=[org.pk for org in faellig]).update(
                is_active=False, updated_at=jetzt, demo_erinnert_stunden=None
            )
    for org in faellig:
        org.is_active = False
        logger.info("Demo %s abgelaufen (Ende %s), Organisation gesperrt", org.slug, org.demo_endet_am)
    return faellig


# ---------------------------------------------------------------------------
# Löschung nach der Frist (Issue #1575)
# ---------------------------------------------------------------------------


def loeschfaellig_q(jetzt: datetime | None = None) -> Q:
    """Gesperrte Demos, deren Ende mindestens die Löschfrist zurückliegt (ohne Löschfrist: keine)."""
    frist = loeschfrist()
    if frist is None:
        return Q(pk__in=[])
    return abgelaufen_q((jetzt or timezone.now()) - frist) & Q(is_active=False)


@dataclass(frozen=True)
class Erinnerung:
    """Eine verschickte Erinnerung an die Löschung (Ausgabe des Zeitplans, ohne Adressen)."""

    slug: str
    stunden: int
    empfaenger: int
    fehlgeschlagen: int = 0
    wiederholen: bool = False


def faellige_stufe(organization: Organization, jetzt: datetime | None = None) -> int | None:
    """
    Fällige Erinnerungsstufe (Stunden vor der Löschung) einer gesperrten Demo, sonst ``None``. Jede Stufe geht
    höchstens einmal raus; ein verspäteter Lauf schickt nur die jüngste fällige (wie beim Auslauf, Issue #1237).
    """
    loeschung = loeschung_am(organization)
    if loeschung is None or organization.is_active:
        return None
    rest = loeschung - (jetzt or timezone.now())
    if rest <= timedelta(0):
        return None  # fällig zum Löschen
    for stunden in ERINNERUNG_STUNDEN:
        if rest <= timedelta(hours=stunden):
            gesendet = organization.demo_erinnert_stunden
            return None if gesendet is not None and gesendet <= stunden else stunden
    return None


def _empfaenger(organization: Organization) -> list[str]:
    """Adressen der aktiven Administratoren (Rolle mit Administratorrecht) und der Eigentümerin, ohne Gäste."""
    adressen = set(
        Membership.objects.filter(
            organization=organization, is_active=True, is_guest=False, roles__is_admin=True
        ).values_list("user__email", flat=True)
    )
    if organization.owner is not None:
        adressen.add(organization.owner.email)
    return sorted(adresse for adresse in adressen if adresse)


def _erinnerung_senden(organization: Organization, stunden: int, empfaenger: list[str]) -> int:
    """
    Erinnerung einzeln an jede Adresse (keine fremden Adressen im Kopf) und eine Kopie an ``DEMO_KONTAKT_EMAIL``;
    liefert die Zahl der versandten bzw. dauerhaft eingereihten Mails ohne die Kopie.
    """
    from apps.common import mail

    loeschung = loeschung_am(organization)
    ende = organization.demo_endet_am
    if loeschung is None or ende is None:
        return 0
    letzte = stunden == min(ERINNERUNG_STUNDEN)
    kontext = {
        "organization": organization,
        "ende": _datum(ende),
        "loeschung": _datum(loeschung),
        "loeschung_uhrzeit": timezone.localtime(loeschung).strftime("%H:%M"),
        "letzte": letzte,
        "verlaengern_url": verlaengern_url(),
        "kontakt": kontakt(),
    }
    betreff = f"Eure Demo „{organization.name}“ wird am {kontext['loeschung']} gelöscht"
    if letzte:
        betreff = f"Letzte Erinnerung: {betreff}"
    schluessel = f"demo-erinnerung:{organization.pk}:{stunden}:{loeschung.isoformat()}"
    gesendet = 0
    for adresse in empfaenger:
        if mail.send_template(
            kind=MAILART_ERINNERUNG,
            subject=betreff,
            template_name="emails/demo/loeschung_erinnerung",
            context=kontext,
            to=[adresse],
            fail_silently=True,
            idempotency_key=f"{schluessel}:{adresse}",
        ):
            gesendet += 1
    kopie = str(getattr(settings, "DEMO_KONTAKT_EMAIL", "") or "")
    if kopie:
        mail.send_template(
            kind=MAILART_ERINNERUNG,
            subject=f"Kopie ({gesendet} Empfänger): {betreff}",
            template_name="emails/demo/loeschung_erinnerung",
            context=kontext,
            to=[kopie],
            fail_silently=True,
            idempotency_key=f"{schluessel}:kopie",
        )
    return gesendet


def erinnerungen_senden(jetzt: datetime | None = None) -> list[Erinnerung]:
    """
    Administratoren gesperrter Demos an die Löschung erinnern: 7 Tage und 24 Stunden vorher (``ERINNERUNG_STUNDEN``).

    Die Stufe wird vor dem Versand bedingt gesetzt: Ein gleichzeitiger Lauf, eine Verlängerung oder eine Übernahme
    durch die Bereitstellung gewinnt, und keine Erinnerung geht doppelt raus. Scheitert der Versand an alle
    Empfänger (etwa SMTP nicht erreichbar), gilt die Stufe wieder als offen und der nächste Lauf versucht es erneut;
    ein Teilfehler steht in ``fehlgeschlagen`` und lässt den Befehl mit Fehler enden.
    """
    frist = loeschfrist()
    if frist is None:
        return []
    jetzt = jetzt or timezone.now()
    fenster = jetzt - frist + timedelta(hours=max(ERINNERUNG_STUNDEN))
    kandidaten = (
        Organization.objects.filter(abgelaufen_q(fenster), is_active=False)
        .exclude(loeschfaellig_q(jetzt))
        .select_related("owner")
        .order_by("demo_endet_am", "slug")
    )
    verschickt: list[Erinnerung] = []
    for org in kandidaten:
        stufe = faellige_stufe(org, jetzt)
        if stufe is None:
            continue
        gesetzt = (
            Organization.objects.filter(
                pk=org.pk,
                is_active=False,
                demo_endet_am=org.demo_endet_am,
                demo_laufzeit_tage__isnull=False,
                provisioning_ref__isnull=True,
            )
            .filter(Q(demo_erinnert_stunden__isnull=True) | Q(demo_erinnert_stunden__gt=stufe))
            .update(demo_erinnert_stunden=stufe)
        )
        if not gesetzt:
            continue
        empfaenger = _empfaenger(org)
        if not empfaenger:
            logger.warning("Demo %s: niemand mit Administratorrecht für die Erinnerung an die Löschung", org.slug)
        anzahl = _erinnerung_senden(org, stufe, empfaenger)
        fehlgeschlagen = len(empfaenger) - anzahl
        wiederholen = bool(empfaenger) and anzahl == 0
        if wiederholen:
            # Niemand hat die Erinnerung bekommen: Stufe wieder freigeben (nur, wenn kein anderer Lauf sie änderte)
            Organization.objects.filter(pk=org.pk, demo_erinnert_stunden=stufe).update(
                demo_erinnert_stunden=org.demo_erinnert_stunden
            )
            logger.error(
                "Demo %s: Erinnerung %d Stunden vor der Löschung nicht versendet, nächster Lauf", org.slug, stufe
            )
        elif fehlgeschlagen:
            logger.error(
                "Demo %s: Erinnerung %d Stunden vor der Löschung an %d von %d Empfängern nicht versendet",
                org.slug,
                stufe,
                fehlgeschlagen,
                len(empfaenger),
            )
        else:
            logger.info("Demo %s: Erinnerung %d Stunden vor der Löschung an %d Empfänger", org.slug, stufe, anzahl)
        verschickt.append(
            Erinnerung(
                slug=org.slug,
                stunden=stufe,
                empfaenger=anzahl,
                fehlgeschlagen=fehlgeschlagen,
                wiederholen=wiederholen,
            )
        )
    return verschickt


@dataclass(frozen=True)
class GeloeschteDemo:
    """Was beim Löschen einer Demo entfallen ist (Ausgabe des Zeitplans und Log, ohne Personendaten)."""

    slug: str
    demo_endet_am: datetime | None
    eintraege: int
    dateien: int
    konten_geloescht: int
    konten_behalten: int
    dateien_nicht_entfernt: int = 0


@dataclass
class Loeschlauf:
    """Ergebnis von :func:`faellige_loeschen`: gelöschte Demos und Slugs der Demos, deren Löschung scheiterte."""

    geloescht: list[GeloeschteDemo] = field(default_factory=list)
    fehlgeschlagen: list[str] = field(default_factory=list)


def _dateifelder(model: type[models.Model]) -> list[models.FileField]:
    return [feld for feld in model._meta.concrete_fields if isinstance(feld, models.FileField)]


def _dateien(collector: Collector) -> list[tuple[Any, str]]:
    """Gespeicherte Dateien (Speicher, Name) aller Einträge, die der Collector löschen wird."""
    dateien: list[tuple[Any, str]] = []
    for model, objekte in collector.data.items():
        for feld in _dateifelder(model):
            for objekt in objekte:
                name = getattr(objekt, feld.attname).name
                if name:
                    dateien.append((feld.storage, name))
    # Einträge ohne Signale und eigene Abhängigkeiten löscht Django direkt per Abfrage, ohne sie zu laden
    for abfrage in collector.fast_deletes:
        for feld in _dateifelder(abfrage.model):
            for name in abfrage.values_list(feld.attname, flat=True):
                if name:
                    dateien.append((feld.storage, name))
    return dateien


def _nach_commit_entfernen(dateien: list[tuple[Any, str]], nicht_entfernt: list[str]) -> None:
    """
    Dateien erst nach erfolgreicher Transaktion löschen; fehlt eine schon, ist das kein Fehler. Was sich nicht
    entfernen lässt, landet in ``nicht_entfernt`` und im Fehlerlog: Die Einträge sind dann schon gelöscht, der Befehl
    meldet die Reste als Fehler, damit sie jemand von Hand entfernt.
    """
    if not dateien:
        return

    def entfernen() -> None:
        for speicher, name in dateien:
            try:
                if speicher.exists(name):
                    speicher.delete(name)
            except OSError:
                nicht_entfernt.append(name)
                logger.error("Datei einer gelöschten Demo konnte nicht entfernt werden: %s", name)

    transaction.on_commit(entfernen)


def _konten_aufraeumen(user_ids: list[Any]) -> tuple[int, int]:
    """
    Konten der früheren Mitglieder löschen, an denen nichts mehr hängt (Regeln wie nach dem Auslauf einer
    Mitgliedschaft, Issue #1237); liefert (gelöscht, behalten).
    """
    from apps.accounts.models import User
    from apps.accounts.orphaned_accounts import loesche_konto_nach_austritt

    geloescht = behalten = 0
    for user in User.objects.filter(pk__in=user_ids).order_by("pk"):
        try:
            bindungen = loesche_konto_nach_austritt(user)
        except Exception:  # noqa: BLE001 – die Organisation ist gelöscht; verwaiste Konten fängt der Zeitplan
            logger.exception("Konto nach Löschung einer Demo nicht gelöscht")
            bindungen = ["fehler"]
        if bindungen:
            behalten += 1
        else:
            geloescht += 1
    return geloescht, behalten


def demo_loeschen(organization_id: Any, jetzt: datetime | None = None) -> GeloeschteDemo | None:
    """
    Eine fällige Demo endgültig löschen; ``None``, wenn sie (inzwischen) nicht mehr fällig ist: verlängert und wieder
    aktiv, von der Bereitstellung übernommen, Laufzeit im Admin geleert oder schon gelöscht.

    Gelöscht wird die Organisation mit allem, was per Kaskade an ihr hängt, nach erfolgreicher Transaktion auch die
    Dateien dieser Einträge (Anhänge, Briefköpfe, Logo) und was Fachmodule angemeldet haben
    (:func:`beim_loeschen_melden`). Danach verschwinden Konten früherer Mitglieder, die nirgends mehr gebunden sind.
    """
    jetzt = jetzt or timezone.now()
    nicht_entfernt: list[str] = []
    with transaction.atomic():
        # Unter Zeilensperre neu prüfen: Eine gleichzeitige Verlängerung oder Übernahme wartet und gewinnt danach
        organization = (
            Organization.objects.select_for_update().filter(loeschfaellig_q(jetzt), pk=organization_id).first()
        )
        if organization is None:
            return None
        slug, ende = organization.slug, organization.demo_endet_am
        user_ids = list(Membership.objects.filter(organization=organization).values_list("user_id", flat=True))
        for aufraeumen in list(_beim_loeschen):
            aufraeumen(organization)
        collector = Collector(using=DEFAULT_DB_ALIAS, origin=organization)
        collector.collect([organization])
        dateien = _dateien(collector)
        eintraege, _ = collector.delete()
        _nach_commit_entfernen(dateien, nicht_entfernt)
    konten_geloescht, konten_behalten = _konten_aufraeumen(user_ids)
    ergebnis = GeloeschteDemo(
        slug=slug,
        demo_endet_am=ende,
        eintraege=eintraege,
        dateien=len(dateien),
        konten_geloescht=konten_geloescht,
        konten_behalten=konten_behalten,
        dateien_nicht_entfernt=len(nicht_entfernt),
    )
    frist = loeschfrist()
    logger.warning(
        "Demo %s gelöscht (Ende %s, Frist %s Tage): %d Einträge, %d Dateien, Konten gelöscht %d, behalten %d",
        slug,
        ende.isoformat() if ende else "-",
        frist.days if frist else "-",
        eintraege,
        len(dateien),
        konten_geloescht,
        konten_behalten,
    )
    return ergebnis


def faellige_loeschen(jetzt: datetime | None = None, *, hoechstens: int = LOESCHUNGEN_JE_LAUF) -> Loeschlauf:
    """
    Fällige Demos löschen, älteste zuerst und höchstens ``hoechstens`` je Lauf. Eine gescheiterte Löschung hält die
    übrigen nicht auf; ihr Slug steht in ``fehlgeschlagen``.
    """
    jetzt = jetzt or timezone.now()
    lauf = Loeschlauf()
    faellig = list(
        Organization.objects.filter(loeschfaellig_q(jetzt))
        .order_by("demo_endet_am", "slug")
        .values_list("pk", "slug")[:hoechstens]
    )
    for pk, slug in faellig:
        try:
            ergebnis = demo_loeschen(pk, jetzt)
        except Exception:  # noqa: BLE001 – im Log und als Fehler des Befehls; die nächste Demo versuchen
            logger.exception("Demo %s konnte nicht gelöscht werden", slug)
            lauf.fehlgeschlagen.append(slug)
            continue
        if ergebnis is not None:
            lauf.geloescht.append(ergebnis)
            if ergebnis.dateien_nicht_entfernt:
                lauf.fehlgeschlagen.append(f"{slug} ({ergebnis.dateien_nicht_entfernt} Dateien nicht entfernt)")
    return lauf
