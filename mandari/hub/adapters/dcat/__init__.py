# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Katalog der offenen Ratsinformationen nach DCAT-AP.de 3.0 (Issue #104, ``docs/DCAT_KATALOG.md``).

Erster Adapter der Datendrehscheibe (``docs/adr/20260929-adapter-rahmen.md``): Er beschreibt, was die
OParl-Schnittstelle je Kommune anbietet, in der Sprache der Open-Data-Portale. Inhalte übersetzt er nicht;
Abnehmer holen sie über die Schnittstelle selbst.

- ``vokabular``: die kontrollierten Vokabulare (Lizenzen, Formate, Themen, Raumbezug …)
- ``katalog``: das Katalogmodell und die drei Datensätze einer Kommune – ohne Django und ohne RDF
- ``rdf``: die Serialisierung als Turtle, RDF/XML und JSON-LD (rdflib, erst bei Bedarf geladen)
- ``http``: Adressen mit Endung, Inhaltsaushandlung, Zwischenspeicher, Absagen
- ``aggregator``: die Kataloge des Aggregators (gelistete Kommunen), Routen in ``urls``
"""
