"""
Zensus-Werkzeug ``probe_ris`` (Issue #114).

Bevor ein weiterer Adapter entsteht, muss klar sein, welches Ratsinformationssystem
eine Kommune einsetzt, ob ihre robots.txt Crawling erlaubt, ob ein Bot-Gate davor
liegt und ob nicht längst eine OParl-Schnittstelle existiert. Diese Prüfung stellt
je Kommune **höchstens fünf Anfragen** (robots.txt, Startseite, bei 403 eine
Vergleichsanfrage mit neutralem Client, danach OParl-Kandidaten) und umgeht nichts:
Gates werden erkannt und gemeldet, nicht überwunden.

Die reinen Funktionen (Fingerprint, robots-Verdict, Gate-Erkennung, Kandidatenliste)
sind ohne Netz testbar; ``probe_url`` bindet sie mit einem ``httpx.AsyncClient``
zusammen.
"""

from __future__ import annotations

import json
import re
import urllib.robotparser
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from src.client.oparl_compat import detect_oparl_version, is_oparl_error

MAX_REQUESTS = 5
NEUTRAL_USER_AGENT = "Mozilla/5.0 (compatible; Vergleichsanfrage)"

VENDOR_LABELS = {
    "sessionnet": "Somacos SessionNet",
    "allris3": "ALLRIS 3 (klassische ASP-Oberfläche)",
    "allris4": "ALLRIS 4 (Wicket/Ajax, /public/)",
    "sternberg_rim": "Sternberg RIM (SD.NET RIM)",
    "rubin": "more! rubin (gremien.info)",
    "regisafe": "regisafe (Liferay-Portlet)",
    "komuna": "komuna (React-SPA)",
    "unbekannt": "unbekannt",
}

#: OParl-Pfadmuster je Hersteller (Reihenfolge = Probierreihenfolge), danach die allgemeinen.
OPARL_PATHS_BY_VENDOR: dict[str, list[str]] = {
    "sessionnet": ["/bi/oparl/1.0/system.asp", "/oparl/1.0/system.asp"],
    "allris4": ["/public/oparl/system", "/oparl/v1/system"],
    "allris3": ["/oparl/v1/system"],
    "sternberg_rim": ["/webservice/oparl/v1.1/system", "/webservice/oparl/v1/system"],
    "rubin": ["/oparl/system", "/oparl/v1.1/system"],
    "regisafe": ["/oparl/v1/system"],
    "komuna": ["/oparl/v1/system"],
}
OPARL_PATHS_GENERIC = ["/oparl/system", "/oparl/v1/system", "/oparl/v1.1/system"]

_STERNBERG = re.compile(r"sd\.?net(?:\s*rim)?|sdnetrim")
_GREMIEN_INFO = re.compile(r"gremien\.info")


def _hostname_matches(host: str, domain: str) -> bool:
    """Exakter Host oder Subdomain – kein Teilstring-Vergleich."""
    host = host.split(":")[0]
    return host == domain or host.endswith("." + domain)


_GATE_MARKERS = {
    "browser_verification": (
        "just a moment",
        "verifying your browser",
        "checking your browser",
        "browser-verifikation",
        "browser verification",
        "cf-chl",
        "challenge-platform",
        "cf_chl_opt",
    ),
    "proof_of_work": ("altcha", "proof-of-work", "proof of work", "pow-challenge", "anubis"),
    "waf_forbidden": ("access denied", "request blocked", "zugriff verweigert", "web application firewall"),
}


@dataclass
class ProbeResult:
    url: str
    host: str = ""
    vendor: str = "unbekannt"
    vendor_label: str = VENDOR_LABELS["unbekannt"]
    landing_status: int | None = None
    robots: str = "unbekannt"  # erlaubt | gesperrt | nicht_vorhanden | unbekannt
    robots_detail: str = ""
    gate: str | None = None  # browser_verification | proof_of_work | waf_forbidden
    ua_blocked: bool = False
    oparl_endpoint: str | None = None
    oparl_version: str | None = None
    oparl_checked: list[str] = field(default_factory=list)
    requests: int = 0
    sync_config: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Reine Funktionen
# ---------------------------------------------------------------------------


def fingerprint(html: str, url: str = "", headers: dict[str, str] | None = None) -> str:
    """Hersteller aus Startseite, URL und Antwort-Headern erkennen (Kennung aus VENDOR_LABELS)."""
    text = (html or "").lower()
    host = urlparse(url).netloc.lower() if url else ""
    kopf = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    generator = ""
    m = re.search(r'<meta[^>]+name=["\']generator["\'][^>]+content=["\']([^"\']+)', text)
    if m:
        generator = m.group(1)

    # Regulaere Ausdruecke statt Teilstring-Vergleichen auf domain-artige Zeichenketten:
    # Das ist Fingerprinting von HTML, keine Sicherheitsentscheidung ueber eine URL.
    if _STERNBERG.search(generator) or _STERNBERG.search(text):
        return "sternberg_rim"
    if (
        _hostname_matches(host, "gremien.info")
        or _GREMIEN_INFO.search(text[:5000])
        or "rubin" in generator
        or "more! rubin" in text
    ):
        return "rubin"
    if "regisafe" in text and ("liferay" in text or "portlet" in text or "liferay" in kopf.get("set-cookie", "")):
        return "regisafe"
    if "komuna" in text and ('id="root"' in text or "komuna" in host):
        return "komuna"
    if "allris" in text or "allris" in host:
        # ALLRIS 4 liefert Wicket-Seiten unter /public/, ALLRIS 3 klassische *.asp mit dreistelligen Nummern
        if "/public/" in text or "wicket" in text or "allris-net" in text:
            return "allris4"
        if re.search(r"\b(si|to|vo|au)\d{3}\.asp", text):
            return "allris3"
        return "allris4" if "/public/" in url else "allris3"
    # SessionNet: vierstellige Seitenkennungen (si0040, si0057, to0040, vo0050) und Bürgerinfo-Layout
    if "sessionnet" in text or "session net" in text or re.search(r"\b(si|to|vo|au)\d{4}\.(asp|php)", text):
        return "sessionnet"
    if "/bi/" in url and re.search(r"\b(si|to|vo)\d{4}\.(asp|php)", text):
        return "sessionnet"
    return "unbekannt"


def detect_gate(status: int | None, html: str, headers: dict[str, str] | None = None) -> str | None:
    """Bot-Gate oder WAF-Seite erkennen; ``None`` = keine Sperre erkennbar."""
    text = (html or "").lower()
    kopf = {k.lower(): v.lower() for k, v in (headers or {}).items()}
    if kopf.get("cf-mitigated") == "challenge":
        return "browser_verification"
    for art, marker in _GATE_MARKERS.items():
        if any(m in text for m in marker):
            return art
    if status in (403, 429, 503) and "cloudflare" in kopf.get("server", ""):
        return "waf_forbidden"
    return None


def robots_verdict(robots_txt: str | None, user_agent: str, paths: list[str]) -> tuple[str, str]:
    """
    (Verdict, Detail) nach RFC 9309 für unseren User-Agent.

    Verdict: ``erlaubt``, ``gesperrt`` (mindestens einer der Pfade), ``nicht_vorhanden``.
    Geprüft wird das Produkt-Token (Text vor ``/`` oder Leerzeichen) und der volle UA-String,
    wie es der Fetcher des Ingestors auch tut.
    """
    if robots_txt is None:
        return "nicht_vorhanden", "keine gültige robots.txt (RFC 9309: unavailable = erlaubt)"
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    token = user_agent.split("/")[0].split(" ")[0]
    gesperrt = [p for p in paths if not (parser.can_fetch(token, p) and parser.can_fetch(user_agent, p))]
    if gesperrt:
        alles = re.search(r"^\s*disallow\s*:\s*/\s*$", robots_txt, re.IGNORECASE | re.MULTILINE) is not None
        detail = (
            "Disallow: / (alles gesperrt)"
            if alles and len(gesperrt) == len(paths)
            else f"gesperrt: {', '.join(gesperrt)}"
        )
        return "gesperrt", detail
    return "erlaubt", f"{len(paths)} Pfad(e) erlaubt"


def oparl_candidates(url: str, vendor: str) -> list[str]:
    """Kandidaten-URLs für den OParl-System-Endpunkt, herstellerspezifische zuerst."""
    parsed = urlparse(url)
    basis = urlunparse((parsed.scheme or "https", parsed.netloc, "", "", "", ""))
    pfade = list(OPARL_PATHS_BY_VENDOR.get(vendor, [])) + OPARL_PATHS_GENERIC
    gesehen: list[str] = []
    for pfad in pfade:
        kandidat = basis + pfad
        if kandidat not in gesehen:
            gesehen.append(kandidat)
    return gesehen


def suggest_sync_config(result: ProbeResult) -> dict[str, Any] | None:
    """Vorschlag für ``sync_config`` der Quelle; ``None`` mit Begründung in ``notes``."""
    if result.oparl_endpoint:
        cfg: dict[str, Any] = {"type": "oparl", "system_url": result.oparl_endpoint}
        if result.oparl_version:
            cfg["oparl_version"] = result.oparl_version
        return cfg
    if result.gate or result.ua_blocked:
        result.notes.append("kein Vorschlag: Bot-Gate oder Sperre – Kooperationspfad (docs/adr, kein Scraping)")
        return None
    if result.robots == "gesperrt":
        result.notes.append("kein Vorschlag: robots.txt untersagt den Abruf (§ 44b UrhG)")
        return None
    if result.vendor == "sessionnet":
        return {"scraper": "sessionnet", "base_url": result.url.rstrip("/")}
    if result.vendor in ("allris3", "allris4"):
        result.notes.append("ALLRIS ohne OParl: Bridge bzw. App-API prüfen (Issues #119/#120), kein direkter Scraper")
        return None
    if result.vendor in ("sternberg_rim", "regisafe", "komuna"):
        result.notes.append("kein Scraping für diesen Hersteller (ADR 2026-09-17), OParl-Aktivierung anfragen")
        return None
    result.notes.append("Hersteller unbekannt – von Hand prüfen")
    return None


def classify_batch(
    results: list[ProbeResult], registered_urls: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    """
    Zwei Listen für die Zielplanung: „ohne OParl, robots-frei“ (Scraper-Kandidaten) und
    „OParl vorhanden, nicht registriert“ (Aktivierung/Registrierung anstoßen).
    """
    registriert = {u.rstrip("/") for u in (registered_urls or set())}
    ohne_oparl: list[dict[str, Any]] = []
    oparl_unregistriert: list[dict[str, Any]] = []
    for r in results:
        if r.oparl_endpoint:
            if r.oparl_endpoint.rstrip("/") not in registriert:
                oparl_unregistriert.append({"url": r.url, "oparl_endpoint": r.oparl_endpoint, "vendor": r.vendor})
        elif r.robots != "gesperrt" and not r.gate and not r.ua_blocked:
            ohne_oparl.append({"url": r.url, "vendor": r.vendor, "robots": r.robots})
    return {"ohne_oparl_robots_frei": ohne_oparl, "oparl_vorhanden_nicht_registriert": oparl_unregistriert}


# ---------------------------------------------------------------------------
# Netz
# ---------------------------------------------------------------------------


class _Budget:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def take(self) -> bool:
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


async def _get(client: httpx.AsyncClient, url: str, budget: _Budget, **kw: Any) -> httpx.Response | None:
    if not budget.take():
        return None
    try:
        return await client.get(url, follow_redirects=True, timeout=15.0, **kw)
    except httpx.HTTPError:
        return None


async def probe_url(
    url: str,
    client: httpx.AsyncClient,
    *,
    user_agent: str,
    max_requests: int = MAX_REQUESTS,
) -> ProbeResult:
    """Eine Kommune prüfen; stellt höchstens ``max_requests`` Anfragen."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    result = ProbeResult(url=url, host=parsed.netloc.lower())
    budget = _Budget(max_requests)
    kopf = {"User-Agent": user_agent}

    # 1) robots.txt
    robots_url = urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    robots_txt: str | None = None
    antwort = await _get(client, robots_url, budget, headers=kopf)
    if antwort is not None and antwort.status_code == 200 and "<html" not in antwort.text[:200].lower():
        robots_txt = antwort.text

    # 2) Startseite
    landing = await _get(client, url, budget, headers=kopf)
    html = landing.text if landing is not None else ""
    header = dict(landing.headers) if landing is not None else {}
    result.landing_status = landing.status_code if landing is not None else None
    result.vendor = fingerprint(html, url, header)
    result.vendor_label = VENDOR_LABELS[result.vendor]
    result.gate = detect_gate(result.landing_status, html, header)

    # 3) 403 auf unseren User-Agent? Eine Vergleichsanfrage, nur zur Diagnose.
    if result.landing_status == 403:
        vergleich = await _get(client, url, budget, headers={"User-Agent": NEUTRAL_USER_AGENT})
        if vergleich is not None and vergleich.status_code == 200:
            result.ua_blocked = True
            result.notes.append("403 nur für unseren User-Agent (neutraler Client: 200)")
            if result.vendor == "unbekannt":
                result.vendor = fingerprint(vergleich.text, url, dict(vergleich.headers))
                result.vendor_label = VENDOR_LABELS[result.vendor]

    # 4) OParl-Autodiscovery mit dem Restbudget
    kandidaten = oparl_candidates(url, result.vendor)
    result.robots, result.robots_detail = robots_verdict(
        robots_txt, user_agent, [parsed.path or "/"] + [urlparse(k).path for k in kandidaten[:3]]
    )
    for kandidat in kandidaten:
        if budget.used >= budget.limit:
            break
        antwort = await _get(client, kandidat, budget, headers={**kopf, "Accept": "application/json"})
        result.oparl_checked.append(kandidat)
        if antwort is None or antwort.status_code != 200:
            continue
        try:
            daten = antwort.json()
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(daten, dict) or is_oparl_error(daten):
            continue
        typ = str(daten.get("type", ""))
        if "System" in typ or "body" in daten or detect_oparl_version(daten):
            result.oparl_endpoint = str(antwort.url)
            result.oparl_version = detect_oparl_version(daten)
            break

    result.requests = budget.used
    result.sync_config = suggest_sync_config(result)
    return result


def to_csv_rows(results: list[ProbeResult]) -> list[dict[str, str]]:
    """Flache Zeilen für die CSV-Ausgabe."""
    rows = []
    for r in results:
        rows.append(
            {
                "url": r.url,
                "hersteller": r.vendor,
                "status": str(r.landing_status or ""),
                "robots": r.robots,
                "gate": r.gate or "",
                "ua_gesperrt": "ja" if r.ua_blocked else "nein",
                "oparl": r.oparl_endpoint or "",
                "oparl_version": r.oparl_version or "",
                "anfragen": str(r.requests),
                "vorschlag": json.dumps(r.sync_config, ensure_ascii=False) if r.sync_config else "",
                "hinweise": "; ".join(r.notes),
            }
        )
    return rows
