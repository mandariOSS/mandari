# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Anwendungen (Clients) des OpenID-Connect-Anbieters auf der Kommandozeile (Issue #1215).

    python manage.py oidc_anwendung anlegen --name "Rats-App" --typ öffentlich --grant authorization-code \\
        --redirect-uri https://app.example.org/rueckweg --scope openid --scope ratsdaten:lesen
    python manage.py oidc_anwendung anlegen --name "Datendienst" --typ vertraulich --grant client-credentials \\
        --scope ratsdaten:lesen
    python manage.py oidc_anwendung anlegen --name "Fachverfahren" --typ vertraulich --grant client-credentials \\
        --mandant musterstadt --jwks dienst-jwks.json --scope sitzung:lesen
    python manage.py oidc_anwendung liste
    python manage.py oidc_anwendung schluessel <client-id> --jwks neue-jwks.json
    python manage.py oidc_anwendung sperren <client-id> [--grund "…"]
    python manage.py oidc_anwendung entsperren <client-id>

Anlegen geht durch das Formular des Admins (``OAuthApplicationForm``) und damit durch dieselben Prüfungen wie dort
und im Modell: nur Code-Ablauf (PKCE S256 ist Pflicht) und Client Credentials, Redirect-URIs nur mit https und exakt,
Scopes nur aus dem Katalog und passend zum Ablauf, Signatur RS256 für den Code-Ablauf. Das Client-Secret einer
vertraulichen Anwendung erscheint genau einmal in der Ausgabe; gespeichert ist nur sein Hash. Öffentliche
Anwendungen bekommen kein nutzbares Secret.

Profil Nichtöffentliches (#565): ``--jwks`` trägt öffentliche Schlüssel (JWK Set als Datei) ein; die Anwendung weist
sich dann mit ``private_key_jwt`` (RFC 7523) aus und hat kein nutzbares Client-Secret. ``--mandant`` bindet einen
Dienst an genau einen Session-Mandanten (Kurzname); die Plattform speichert nur die fachfreie Kennung
(``apps.accounts.oidc.bindings``), Session wertet sie aus. Gebundene Dienste brauchen ``--jwks``. ``schluessel``
ersetzt die Schlüssel einer Anwendung (Schlüsselwechsel).

Sperren schaltet die Anwendung aus und widerruft alle ihre Tokens; Entsperren schaltet sie wieder ein, alte Tokens
leben dabei nicht auf. Anlegen, Sperren und Entsperren stehen im Sicherheitsprotokoll (``oidc_anwendung``), nie mit
Secret. Anleitung: ``docs/schnittstelle/anmeldung-betrieb.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone
from oauth2_provider.generators import generate_client_id, generate_client_secret
from oauth2_provider.settings import oauth2_settings

from apps.accounts.oidc import audit, bindings, catalog, provider_enabled, services
from apps.accounts.oidc.admin import OAuthApplicationForm
from apps.accounts.oidc.models import OAuthApplication

#: Typ auf der Kommandozeile → ``client_type``
TYPES: dict[str, str] = {
    "vertraulich": OAuthApplication.CLIENT_CONFIDENTIAL,
    "öffentlich": OAuthApplication.CLIENT_PUBLIC,
    "oeffentlich": OAuthApplication.CLIENT_PUBLIC,
}
GRANTS = (OAuthApplication.GRANT_AUTHORIZATION_CODE, OAuthApplication.GRANT_CLIENT_CREDENTIALS)
TYPE_LABELS: dict[str, str] = {
    OAuthApplication.CLIENT_CONFIDENTIAL: "vertraulich",
    OAuthApplication.CLIENT_PUBLIC: "öffentlich",
}
GRANT_LABELS: dict[str, str] = {
    OAuthApplication.GRANT_AUTHORIZATION_CODE: "Code-Ablauf mit PKCE (authorization-code)",
    OAuthApplication.GRANT_CLIENT_CREDENTIALS: "Client Credentials für einen Dienst (client-credentials)",
}


def _words(values: list[str] | None) -> list[str]:
    """Wiederholte und durch Leerzeichen getrennte Angaben, ohne Doppelte, in der Reihenfolge der Eingabe."""
    seen: dict[str, None] = {}
    for value in values or []:
        for word in value.split():
            seen.setdefault(word, None)
    return list(seen)


def _jwks(path: str) -> dict[str, Any]:
    """JWK Set aus einer Datei; ein einzelner Schlüssel wird zum Set mit einem Schlüssel."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise CommandError(f"--jwks: Datei nicht lesbar ({exc.strerror or exc}).") from None
    except ValueError:
        raise CommandError("--jwks: Die Datei enthält kein gültiges JSON.") from None
    if isinstance(data, dict) and "keys" not in data and "kty" in data:
        data = {"keys": [data]}
    if not isinstance(data, dict):
        raise CommandError('--jwks: Erwartet wird ein JWK Set ({"keys": [ … ]}) oder ein einzelner Schlüssel.')
    return data


def _type(value: str) -> str:
    key = value.strip().lower()
    if key not in TYPES:
        raise CommandError("--typ: vertraulich oder öffentlich.")
    return TYPES[key]


class Command(BaseCommand):
    help = "OIDC-Anwendungen anlegen, auflisten, sperren und entsperren (dieselben Regeln wie im Admin)."

    def add_arguments(self, parser: CommandParser) -> None:
        aktionen = parser.add_subparsers(
            dest="aktion", metavar="{anlegen,liste,schluessel,sperren,entsperren}", required=True
        )

        anlegen = aktionen.add_parser("anlegen", help="Anwendung anlegen; das Client-Secret erscheint nur jetzt.")
        anlegen.add_argument("--name", required=True, help="Name auf der Zustimmungsseite und in der Kontoseite.")
        anlegen.add_argument(
            "--typ",
            required=True,
            help="vertraulich (mit Client-Secret, Server) oder öffentlich (ohne Secret, etwa eine App).",
        )
        anlegen.add_argument(
            "--grant",
            required=True,
            choices=GRANTS,
            help="authorization-code (im Namen einer Person, PKCE) oder client-credentials (Dienst, vertraulich).",
        )
        anlegen.add_argument(
            "--redirect-uri",
            action="append",
            metavar="URI",
            help="Exakte Redirect-URI mit https, wiederholbar (nur Code-Ablauf).",
        )
        anlegen.add_argument(
            "--scope",
            action="append",
            metavar="SCOPE",
            help=f"Erlaubter Scope aus dem Katalog, wiederholbar: {', '.join(s.name for s in catalog.CATALOG)}.",
        )
        anlegen.add_argument(
            "--skip-authorization",
            action="store_true",
            help="Ohne Zustimmungsseite (nur für eigene Anwendungen von mandari, nur Code-Ablauf).",
        )
        anlegen.add_argument(
            "--jwks",
            metavar="DATEI",
            help="Öffentliche Schlüssel (JWK Set) für private_key_jwt; dann gibt es kein Client-Secret.",
        )
        anlegen.add_argument(
            "--mandant",
            metavar="KURZNAME",
            help="Dienst an genau diesen Session-Mandanten binden (nur client-credentials, braucht --jwks).",
        )

        aktionen.add_parser("liste", help="Alle Anwendungen mit Ablauf, Scopes und Redirect-URIs (ohne Secret).")

        schluessel = aktionen.add_parser("schluessel", help="Öffentliche Schlüssel (JWKS) einer Anwendung ersetzen.")
        schluessel.add_argument("client_id", help="Client-ID der Anwendung")
        schluessel.add_argument("--jwks", metavar="DATEI", required=True, help="Neues JWK Set (nur öffentliche Teile).")

        sperren = aktionen.add_parser("sperren", help="Anwendung ausschalten und alle ihre Tokens widerrufen.")
        sperren.add_argument("client_id", help="Client-ID der Anwendung")
        sperren.add_argument("--grund", default="", help="Anlass (steht im Sicherheitsprotokoll).")

        entsperren = aktionen.add_parser("entsperren", help="Gesperrte Anwendung wieder einschalten.")
        entsperren.add_argument("client_id", help="Client-ID der Anwendung")
        entsperren.add_argument("--grund", default="", help="Anlass (steht im Sicherheitsprotokoll).")

    def handle(self, *args: Any, **options: Any) -> None:
        aktion = options["aktion"]
        if aktion == "anlegen":
            self._anlegen(options)
        elif aktion == "liste":
            self._liste()
        elif aktion == "schluessel":
            self._schluessel(options["client_id"], str(options["jwks"]))
        else:
            self._sperren(options["client_id"], gesperrt=aktion == "sperren", grund=str(options["grund"]).strip())

    # ------------------------------------------------------------------ anlegen

    def _anlegen(self, options: dict[str, Any]) -> None:
        name = str(options["name"]).strip()
        client_type = _type(str(options["typ"]))
        grant = str(options["grant"])
        person = grant == OAuthApplication.GRANT_AUTHORIZATION_CODE
        redirect_uris = _words(options["redirect_uri"])
        scopes = _words(options["scope"])

        if not name:
            raise CommandError("--name darf nicht leer sein.")
        if person and not redirect_uris:
            raise CommandError("Der Code-Ablauf braucht mindestens eine Redirect-URI (--redirect-uri https://…).")
        if not person and redirect_uris:
            raise CommandError("Dienste (client-credentials) haben keine Redirect-URIs.")
        if not person and options["skip_authorization"]:
            raise CommandError(
                "--skip-authorization gilt nur für den Code-Ablauf; Dienste haben keine Zustimmungsseite."
            )
        if not scopes:
            raise CommandError("Mindestens einen erlaubten Scope angeben (--scope).")
        unknown = [scope for scope in scopes if catalog.get(scope) is None]
        if unknown:
            raise CommandError(
                f"Unbekannte Scopes: {', '.join(unknown)}. Der Katalog: {', '.join(s.name for s in catalog.CATALOG)}."
            )
        jwks = _jwks(str(options["jwks"])) if options.get("jwks") else None
        binding = ""
        if options.get("mandant"):
            if person:
                raise CommandError("--mandant gilt nur für Dienste (client-credentials).")
            binding = bindings.resolve(bindings.TENANT, str(options["mandant"])) or ""
            if not binding:
                raise CommandError(
                    f"--mandant: Keinen aktiven Session-Mandanten mit dem Kurznamen {options['mandant']}."
                )
            if jwks is None:
                raise CommandError("Gebundene Dienste weisen sich mit private_key_jwt aus; bitte --jwks angeben.")
        if jwks is not None and client_type != OAuthApplication.CLIENT_CONFIDENTIAL:
            raise CommandError("--jwks gilt nur für vertrauliche Anwendungen (--typ vertraulich).")
        if person and not oauth2_settings.OIDC_RSA_PRIVATE_KEY:
            raise CommandError(
                "Für den Code-Ablauf braucht mandari einen Signaturschlüssel (OIDC_RSA_PRIVATE_KEY_FILE oder "
                "OIDC_RSA_PRIVATE_KEY). „manage.py oidc_pruefen“ zeigt, was fehlt."
            )

        secret = generate_client_secret()
        form = OAuthApplicationForm(
            data={
                "name": name,
                "client_id": generate_client_id(),
                "client_secret": secret,
                "client_type": client_type,
                "authorization_grant_type": grant,
                "redirect_uris": " ".join(redirect_uris),
                "allowed_scopes": scopes,
                "skip_authorization": bool(options["skip_authorization"]),
                "algorithm": OAuthApplication.RS256_ALGORITHM if person else OAuthApplication.NO_ALGORITHM,
                "is_active": True,
                "binding": binding,
                "jwks": json.dumps(jwks) if jwks is not None else "",
            }
        )
        if not form.is_valid():
            meldungen = []
            for field, errors in form.errors.items():
                label = form.fields[field].label if field in form.fields else ""
                for error in errors:
                    meldungen.append(f"{label}: {error}" if label else str(error))
            raise CommandError("Anwendung nicht angelegt:\n" + "\n".join(f"- {meldung}" for meldung in meldungen))

        with transaction.atomic():
            application: OAuthApplication = form.save()
            audit.write(
                audit.EVENT_APPLICATION,
                None,
                aktion="angelegt",
                anwendung=application.client_id,
                name=application.name,
                durch=audit.BY_COMMAND,
                bindung=application.binding or None,
                anmeldung="private_key_jwt" if application.uses_private_key_jwt else None,
            )

        self.stdout.write(self.style.SUCCESS(f"Anwendung „{application.name}“ angelegt."))
        self._details(application)
        if application.uses_private_key_jwt:
            self.stdout.write(
                "  Client-Secret: keins (Anmeldung mit private_key_jwt und dem eigenen privaten Schlüssel)"
            )
        elif client_type == OAuthApplication.CLIENT_CONFIDENTIAL:
            self.stdout.write(f"  Client-Secret: {secret}")
            self.stdout.write(
                self.style.WARNING(
                    "Das Client-Secret erscheint nur jetzt. Sicher an die Anwendung übergeben; gespeichert ist nur "
                    "sein Hash. Geht es verloren, im Admin unter „OIDC-Anwendungen“ ein neues eintragen."
                )
            )
        else:
            self.stdout.write("  Client-Secret: keins (öffentliche Anwendung, nur mit PKCE)")
        if not provider_enabled():
            self.stdout.write(
                self.style.WARNING("Der Anbieter ist ausgeschaltet; die Anwendung wirkt erst nach dem Einschalten.")
            )

    # ------------------------------------------------------------------ liste

    def _liste(self) -> None:
        applications = list(OAuthApplication.objects.order_by("name", "client_id"))
        if not applications:
            self.stdout.write("Keine Anwendungen. Anlegen mit „manage.py oidc_anwendung anlegen …“.")
            return
        for application in applications:
            stand = "aktiv" if application.is_active else "gesperrt"
            self.stdout.write(f"{application.name or application.client_id} [{stand}]")
            self._details(application)
            self.stdout.write("")
        self.stdout.write(f"{len(applications)} Anwendung(en).")

    def _details(self, application: OAuthApplication) -> None:
        self.stdout.write(f"  Client-ID:     {application.client_id}")
        self.stdout.write(f"  Ablauf:        {GRANT_LABELS.get(application.authorization_grant_type, '?')}")
        self.stdout.write(f"  Typ:           {TYPE_LABELS.get(application.client_type, '?')}")
        self.stdout.write(f"  Scopes:        {' '.join(application.scope_list) or '–'}")
        if application.binding:
            ziel = bindings.describe(application.binding) or "Bereich nicht gefunden"
            self.stdout.write(f"  Bindung:       {application.binding} ({ziel})")
        if application.uses_private_key_jwt:
            kids = [str(key.get("kid") or "ohne kid") for key in (application.jwks or {}).get("keys", [])]
            self.stdout.write(f"  Anmeldung:     private_key_jwt, Schlüssel: {', '.join(kids)}")
        if application.acts_for_person:
            self.stdout.write(f"  Redirect-URIs: {' '.join(application.redirect_uris.split()) or '–'}")
            zustimmung = "nein (vertrauenswürdig)" if application.skip_authorization else "ja, jedes Mal"
            self.stdout.write(f"  Zustimmung:    {zustimmung}")
        created = timezone.localtime(application.created).strftime("%d.%m.%Y %H:%M")
        self.stdout.write(f"  Angelegt:      {created}")

    # ------------------------------------------------------------------ schluessel

    def _schluessel(self, client_id: str, path: str) -> None:
        application = OAuthApplication.objects.filter(client_id=client_id).first()
        if application is None:
            raise CommandError(f"Keine Anwendung mit der Client-ID {client_id}. „oidc_anwendung liste“ zeigt alle.")
        application.jwks = _jwks(path)
        try:
            application.full_clean(exclude=["client_secret"])
        except ValidationError as exc:
            meldungen = [f"- {meldung}" for meldungen in exc.message_dict.values() for meldung in meldungen]
            raise CommandError("Schlüssel nicht übernommen:\n" + "\n".join(meldungen)) from None
        with transaction.atomic():
            application.save(update_fields=["jwks", "updated"])
            audit.write(
                audit.EVENT_APPLICATION,
                None,
                aktion="Schlüssel ersetzt",
                anwendung=application.client_id,
                name=application.name,
                durch=audit.BY_COMMAND,
            )
        self.stdout.write(self.style.SUCCESS(f"Schlüssel der Anwendung „{application.name}“ ersetzt."))
        self._details(application)

    # ------------------------------------------------------------------ sperren / entsperren

    def _sperren(self, client_id: str, *, gesperrt: bool, grund: str) -> None:
        application = OAuthApplication.objects.filter(client_id=client_id).first()
        if application is None:
            raise CommandError(f"Keine Anwendung mit der Client-ID {client_id}. „oidc_anwendung liste“ zeigt alle.")
        aktion = "gesperrt" if gesperrt else "entsperrt"
        geaendert = bool(application.is_active) == gesperrt
        with transaction.atomic():
            if geaendert:
                application.is_active = not gesperrt
                application.save(update_fields=["is_active", "updated"])
                audit.write(
                    audit.EVENT_APPLICATION,
                    None,
                    aktion=aktion,
                    anwendung=application.client_id,
                    name=application.name,
                    durch=audit.BY_COMMAND,
                    grund=grund,
                )
            # Auch eine schon gesperrte Anwendung behält keine Tokens (etwa nach Ausschalten ohne Widerruf)
            widerrufen = (
                services.revoke_application(None, application, befehl=audit.BY_COMMAND, grund=grund) if gesperrt else 0
            )

        name = application.name or application.client_id
        if not geaendert:
            self.stdout.write(f"Anwendung „{name}“ ist bereits {aktion}.")
        else:
            self.stdout.write(self.style.SUCCESS(f"Anwendung „{name}“ {aktion}."))
        if gesperrt:
            self.stdout.write(f"{widerrufen} Token(s) widerrufen.")
            if application.acts_for_person:
                self.stdout.write("Nach dem Entsperren müssen Personen der Anwendung erneut zustimmen.")
