# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Deutsche Kommunal-Synonyme für Elasticsearch.

Diese Synonyme verbessern die Sucherfahrung für kommunalpolitische Begriffe.
"""

# Kommunalpolitische Synonyme
# Format: Begriff -> Liste von Synonymen (bidirektional)
GERMAN_MUNICIPAL_SYNONYMS = {
    # Gremien & Organe
    "Stadtrat": ["Rat", "Gemeinderat", "Stadtparlament"],
    "Rat": ["Stadtrat", "Gemeinderat"],
    "Gemeinderat": ["Stadtrat", "Rat"],
    "Kreistag": ["Kreis"],
    "Bezirksvertretung": ["BV", "Bezirksausschuss"],
    "Ausschuss": ["Gremium", "Fachausschuss", "Ausschuß"],
    "Fachausschuss": ["Ausschuss", "Gremium"],
    "Hauptausschuss": ["HA", "Haupt- und Finanzausschuss"],
    # Personen & Rollen
    "Bürgermeister": ["BM", "Oberbürgermeister", "OB"],
    "Oberbürgermeister": ["OB", "Bürgermeister"],
    "OB": ["Oberbürgermeister", "Bürgermeister"],
    "Bürgermeisterin": ["OBin", "Oberbürgermeisterin"],
    "Landrat": ["LR"],
    "Stadtdirektor": ["Stadtdirektorin"],
    "Fraktionsvorsitzender": ["Fraktionsvorsitzende", "FV"],
    "Sachkundiger Bürger": ["SKB", "sachkundige Bürgerin"],
    "Ratsmitglied": ["Stadtverordnete", "Stadtverordneter", "Ratsherr", "Ratsfrau"],
    # Fraktionen & Parteien
    "Fraktion": ["Ratsfraktion", "Fraktionsgemeinschaft"],
    "Ratsfraktion": ["Fraktion"],
    "Koalition": ["Bündnis", "Zusammenschluss"],
    "Opposition": ["Oppositionsfraktion"],
    "Gruppe": ["Wählergruppe", "Wählergemeinschaft"],
    # Dokumente
    "Vorlage": ["Drucksache", "Beschlussvorlage", "Ratsvorlage"],
    "Drucksache": ["Vorlage", "DS"],
    "Antrag": ["Ratsantrag", "Beschlussantrag"],
    "Ratsantrag": ["Antrag", "Beschlussantrag"],
    "Anfrage": ["Ratsanfrage", "Kleine Anfrage", "Große Anfrage"],
    "Ratsanfrage": ["Anfrage"],
    "Beschluss": ["Ratsbeschluss", "Bescheid"],
    "Ratsbeschluss": ["Beschluss"],
    "Satzung": ["Ordnung", "Verordnung"],
    "Haushalt": ["Haushaltsplan", "Etat", "Budget"],
    "Haushaltsplan": ["Haushalt", "Etat"],
    "Stellungnahme": ["Statement", "Positionierung"],
    "Protokoll": ["Niederschrift", "Sitzungsprotokoll"],
    "Niederschrift": ["Protokoll"],
    # Sitzungen & Termine
    "Sitzung": ["Versammlung", "Zusammenkunft", "Termin"],
    "Ratssitzung": ["Stadtratssitzung", "Gemeinderatssitzung"],
    "Ausschusssitzung": ["Fachausschusssitzung"],
    "Tagesordnung": ["TO", "Agenda", "TOP"],
    "TOP": ["Tagesordnungspunkt", "Tagesordnung"],
    "Tagesordnungspunkt": ["TOP"],
    # Abstimmungen
    "Abstimmung": ["Votum", "Beschlussfassung"],
    "Mehrheit": ["Stimmmehrheit"],
    "einstimmig": ["einmütig", "unanim"],
    "Enthaltung": ["Stimmenthaltung"],
    "abgelehnt": ["zurückgewiesen", "nicht angenommen"],
    "angenommen": ["beschlossen", "zugestimmt"],
    # Verwaltung
    "Verwaltung": ["Stadtverwaltung", "Gemeindeverwaltung", "Rathaus"],
    "Stadtverwaltung": ["Verwaltung", "Rathaus"],
    "Dezernat": ["Abteilung", "Ressort"],
    "Amt": ["Fachamt", "Behörde", "Dienststelle"],
    "Fachamt": ["Amt"],
    "Fachbereich": ["FB", "Abteilung"],
    # Themen
    "Stadtentwicklung": ["Stadterneuerung", "Stadtplanung"],
    "Stadtplanung": ["Bauleitplanung", "Bebauungsplan"],
    "Bebauungsplan": ["B-Plan", "Bauleitplan"],
    "Flächennutzungsplan": ["FNP", "F-Plan"],
    "Verkehr": ["Mobilität", "ÖPNV", "Nahverkehr"],
    "ÖPNV": ["Nahverkehr", "Öffentlicher Nahverkehr", "Bus", "Bahn"],
    "Umwelt": ["Klimaschutz", "Nachhaltigkeit", "Ökologie"],
    "Klimaschutz": ["Klima", "Umweltschutz"],
    "Bildung": ["Schule", "Kita", "Kindergarten"],
    "Schule": ["Schulen", "Bildungseinrichtung"],
    "Kita": ["Kindertagesstätte", "Kindergarten", "KiTa"],
    "Kindergarten": ["Kita", "Kindertagesstätte"],
    "Soziales": ["Sozialhilfe", "Sozialamt"],
    "Kultur": ["Kulturförderung", "Kulturamt"],
    "Sport": ["Sportförderung", "Sportamt", "Sportstätten"],
    "Jugend": ["Jugendamt", "Jugendhilfe", "Jugendarbeit"],
    "Senioren": ["Seniorenarbeit", "Altenhilfe", "Pflegeheim"],
    "Integration": ["Migration", "Zuwanderung", "Flüchtlinge"],
    "Digitalisierung": ["IT", "E-Government", "Smart City"],
    "Finanzen": ["Haushalt", "Kämmerei", "Finanzamt"],
    "Personal": ["Personalamt", "Mitarbeiter"],
    "Bauen": ["Bauamt", "Bauwesen", "Hochbau", "Tiefbau"],
    "Ordnung": ["Ordnungsamt", "Ordnungsdienst"],
    "Feuerwehr": ["Brandschutz", "Rettungsdienst"],
    # Rechtliche Begriffe
    "Genehmigung": ["Erlaubnis", "Bewilligung", "Zustimmung"],
    "Einwand": ["Einspruch", "Widerspruch"],
    "Bürgerantrag": ["Bürgerbegehren", "Einwohnerantrag"],
    "Bürgerbegehren": ["Bürgerantrag", "Volksbegehren"],
    "Öffentlichkeitsbeteiligung": ["Bürgerbeteiligung", "Partizipation"],
    "Bürgerbeteiligung": ["Beteiligung", "Partizipation"],
    # Abkürzungen
    "BV": ["Bezirksvertretung"],
    "HA": ["Hauptausschuss"],
    "DS": ["Drucksache"],
    "FB": ["Fachbereich"],
    "B-Plan": ["Bebauungsplan"],
    "FNP": ["Flächennutzungsplan"],
}


def get_elasticsearch_synonyms() -> list[str]:
    """
    Gibt die Synonyme im Elasticsearch-Format zurück.

    Elasticsearch erwartet Solr-Format: "Begriff, Synonym1, Synonym2"
    """
    synonym_lines: list[str] = []
    seen: set[frozenset] = set()

    for key, values in GERMAN_MUNICIPAL_SYNONYMS.items():
        group = frozenset([key] + values)
        if group not in seen:
            seen.add(group)
            synonym_lines.append(", ".join(sorted(group)))

    return synonym_lines
