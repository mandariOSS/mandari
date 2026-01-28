# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Django management command to import known German OParl sources.

Adds sources with is_active=False so they don't sync automatically.
Activate individual sources manually as needed.
"""

from django.core.management.base import BaseCommand
from insight_core.models import OParlSource


# Complete list of known German OParl endpoints
# Source: https://github.com/OParl/resources/blob/main/endpoints.yml
OPARL_SOURCES = [
    # Major Cities (Priority 1)
    ("Stadt Köln", "https://buergerinfo.stadt-koeln.de/oparl/system"),
    ("Stadt Bonn", "https://www.bonn.sitzung-online.de/public/oparl/system"),
    ("Landeshauptstadt Düsseldorf", "https://ris-oparl.itk-rheinland.de/Oparl/system"),
    ("Stadt Dresden", "https://oparl.dresden.de/system"),
    ("Stadt Leipzig", "https://ratsinformation.leipzig.de/allris_leipzig_public/oparl/system"),
    ("Stadt Wuppertal", "https://oparl.wuppertal.de/oparl/system"),
    ("Stadt Münster", "https://oparl.stadt-muenster.de/system"),
    ("Stadt Aachen", "https://ratsinfo.aachen.de/bi/oparl/1.0/system.asp"),
    ("Stadt Braunschweig", "https://ratsinfo.braunschweig.de/bi/oparl/1.0/system.asp"),
    ("Stadt Krefeld", "https://ris.krefeld.de/webservice/oparl/v1.1/system"),
    ("Stadt Freiburg", "https://ris.freiburg.de/oparl"),
    ("Stadt Ulm", "https://buergerinfo.ulm.de/oparl/system"),
    ("München Transparent", "https://www.muenchen-transparent.de/oparl/v1.0"),

    # Medium Cities (Priority 2)
    ("Stadt Hagen", "https://www.hagen.de/buergerinfo/oparl/1.0/system.asp"),
    ("Klingenstadt Solingen", "https://sdnetrim.kdvz-frechen.de/rim4957/webservice/oparl/v1.1/system"),
    ("Stadt Castrop-Rauxel", "https://castroprauxel.gremien.info/oparl"),
    ("Stadt Herford", "https://herford.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Bergheim", "https://sdnetrim.kdvz-frechen.de/rim4800/webservice/oparl/v1.1/system"),
    ("Stadt Pulheim", "https://sdnetrim.kdvz-frechen.de/rim4350/webservice/oparl/v1.1/system"),
    ("Stadt Willich", "https://ris.stadt-willich.de/webservice/oparl/v1.1/system"),
    ("Stadt Erftstadt", "https://sdnetrim.kdvz-frechen.de/rim4490/webservice/oparl/v1.1/system"),
    ("Stadt Rheda-Wiedenbrück", "https://ratsinfo.rheda-wiedenbrueck.de/webservice/oparl/v1.1/system"),
    ("Stadt Gronau", "https://gronau.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Erkelenz", "https://ratsinfo.erkelenz.de/bi/oparl/1.0/system.asp"),
    ("Stadt Brühl", "https://ratsinfo.bruehl.de/webservice/oparl/v1.1/system"),
    ("Stadt Lahr/Schwarzwald", "https://lahr.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Bad Kreuznach", "https://bad-kreuznach-stadt.gremien.info/oparl/system"),
    ("Stadt Pirmasens", "https://oparl.stadt-pirmasens.de/oparl/system"),
    ("Stadt Wesseling", "https://ratsinfo.wesseling.de/webservice/oparl/v1.1/system"),
    ("Stadt Goch", "https://ris.goch.de/webservice/oparl/v1.1/system"),
    ("Stadt Jülich", "https://sdnetrim.kdvz-frechen.de/rim4240/webservice/oparl/v1.1/system"),
    ("Stadt Emsdetten", "https://emsdetten.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Kolpingstadt Kerpen", "https://ratsinfo.stadt-kerpen.de/webservice/oparl/v1.0/system"),

    # Berlin Districts
    ("Berlin Marzahn-Hellersdorf", "https://www.sitzungsdienst-marzahn-hellersdorf.de/oi/oparl/1.1/system.asp"),
    ("Berlin Steglitz-Zehlendorf", "https://www.sitzungsdienst-steglitz-zehlendorf.de/oi/oparl/1.0/system.asp"),
    ("Berlin Treptow-Köpenick", "https://www.sitzungsdienst-treptow-koepenick.de/oi/oparl/1.0/system.asp"),
    ("Berlin Reinickendorf", "https://www.sitzungsdienst-reinickendorf.de/oi/oparl/1.0/system.asp"),
    ("Berlin Pankow", "https://www.sitzungsdienst-pankow.de/oi/oparl/1.0/system.asp"),
    ("Berlin Lichtenberg", "https://www.sitzungsdienst-lichtenberg.de/oi/oparl/1.0/system.asp"),

    # Districts/Counties
    ("Landkreis Ludwigslust-Parchim", "https://www.lwl-pch.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Landkreis Märkisch-Oderland", "https://ratsinfo-online.net/landkreis-mol-bi/oparl/1.0/system.asp"),
    ("Kreis Gütersloh", "https://sdnetrim.kdvz-frechen.de/rim4890/webservice/oparl/v1.1/system"),
    ("Kreis Viersen", "https://kis.kreis-viersen.de/webservice/oparl/v1.0/system"),
    ("Kreisverwaltung Euskirchen", "https://sdnetrim.kdvz-frechen.de/rim4520/webservice/oparl/v1.1/system"),
    ("Regionalverband Ruhr", "https://rvr-online.gremien.info/oparl"),

    # Smaller Municipalities
    ("Eschwege", "https://rim.ekom21.de/eschwege/webservice/oparl/v1.1/system"),
    ("Stadt Enger", "https://enger.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Spenge", "https://spenge.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Vlotho", "https://vlotho.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Hiddenhausen", "https://hiddenhausen.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Kirchlengern", "https://kirchlengern.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Rödinghausen", "https://roedinghausen.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Schwalmtal", "https://ris.schwalmtal.de/webservice/oparl/v1.1/system"),
    ("Gemeinde Ladbergen", "https://ladbergen.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Stemwede", "https://stemwede.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Aldenhoven", "https://ratsinfo.aldenhoven.de/webservice/oparl/v1.1/system"),
    ("Gemeinde Nettersheim", "https://sdnetrim.kdvz-frechen.de/rim4580/webservice/oparl/v1.1/system"),
    ("Stadt Olpe", "https://sitzungsdienst.kdz-ws.net/gkz330/webservice/oparl/v1.1/system"),
    ("Gemeinde Steinhagen", "https://ratsinfo.steinhagen.de/webservice/oparl/v1.1/system"),
    ("Gemeinde Langenberg", "https://ratsinfo.langenberg.de/webservice/oparl/v1.0/system"),
    ("Gemeinde Weilerswist", "https://sdnetrim.kdvz-frechen.de/rim4510/webservice/oparl/v1.1/system"),
    ("Stadt Bad Münstereifel", "https://ratsinfo.bad-muenstereifel.de/webservice/oparl/v1.1/system"),
    ("Leopoldshohe", "https://leopoldshoehe.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Wachtendonk", "https://ris.wachtendonk.de/webservice/oparl/v1.1/system"),
    ("Stadt Rees", "https://sessionnet-oparl.krz.de/oparl/bodies/5205"),
    ("Stadt Bedburg", "https://sdnetrim.kdvz-frechen.de/rim4780/webservice/oparl/v1.1/system"),
    ("Aarbergen", "https://rim.ekom21.de/aarbergen/webservice/oparl/v1.1/system"),
    ("Westerburg", "https://westerburg.gremien.info/oparl/system"),
    ("Gemeinde Wallenhorst", "https://wallenhorst.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Stadt Bad Pyrmont", "https://badpyrmont.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Kronberg im Taunus", "https://kronberg.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Velen", "https://velen.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Schiffdorf", "https://schiffdorf.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Willingen", "https://rim.ekom21.de/willingen/webservice/oparl/v1.1/system"),
    ("Verbandsgemeinde Hagenbach", "https://www.hagenbach.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Stadt Boppard", "https://www.boppard.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Gemeinde Harsum", "https://www.harsum.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Stadt Großalmerode", "https://rim.ekom21.de/grossalmerode/webservice/oparl/v1.1/system"),
    ("Amt Itzstedt", "https://www.itzstedt.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Amt Trave-Land", "https://www.trave.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Verbandsgemeinde Weida-Land", "https://weida-land.gremien.info/oparl/system"),
    ("Hessisch Lichtenau", "https://rim.ekom21.de/hessisch-lichtenau/webservice/oparl/v1.1/system"),
    ("Kreisstadt Homberg (Efze)", "https://rim.ekom21.de/homberg-efze/webservice/oparl/v1.1/system"),
    ("Stadt Parchim", "https://www.parchim.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Uplengen", "https://uplengen.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Cölbe", "https://rim.ekom21.de/coelbe/webservice/oparl/v1.1/system"),
    ("Gemeinde Lohfelden", "https://lohfelden.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Herxheim", "https://herxheim.gremien.info/oparl/system"),
    ("Stadt Bleckede", "https://www.bleckede.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Gemeinde Waldbrunn im Westerwald", "https://rim.ekom21.de/waldbrunn/webservice/oparl/v1.1/system"),
    ("Stadt Rosbach", "https://www.rosbach.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Schmitten", "https://rim.ekom21.de/schmitten/webservice/oparl/v1.1/system"),
    ("Stadtverwaltung Ortenberg", "https://rim.ekom21.de/ortenberg/webservice/oparl/v1.1/system"),
    ("Homberg (Ohm)", "https://rim.ekom21.de/homberg-ohm/webservice/oparl/v1.1/system"),
    ("Schwarzenborn", "https://rim.ekom21.de/schwarzenborn/webservice/oparl/v1.1/system"),
    ("Gemeinde Fernwald", "https://rim.ekom21.de/fernwald/webservice/oparl/v1.1/system"),
    ("Guxhagen", "https://rim.ekom21.de/guxhagen/webservice/oparl/v1.1/system"),
    ("Gemeinde Ehringshausen", "https://rim.ekom21.de/ehringshausen/webservice/oparl/v1.1/system"),
    ("Samtgemeinde Sögel", "https://soegel.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Glashütten", "https://rim.ekom21.de/glashuetten/webservice/oparl/v1.1/system"),
    ("Emmelshausen", "https://emmelshausen.gremien.info/oparl/system"),
    ("Montabaur", "https://montabaur.gremien.info/oparl/system"),
    ("Edenkoben", "https://edenkoben.gremien.info/oparl/system"),
    ("Enkenbach-Alsenborn", "https://enkenbach-alsenborn.gremien.info/oparl/system"),
    ("Salzatal", "https://salzatal.gremien.info/oparl/system"),
    ("Gemeinde Glasau", "https://www.trave.sitzung-online.de/bi/oparl/1.0/system.asp"),
    ("Rahden", "https://rahden.ratsinfomanagement.net/webservice/oparl/v1.1/system"),
    ("Gemeinde Hürtgenwald", "https://sdnetrim.kdvz-frechen.de/rim4220/webservice/oparl/v1.1/system"),
    ("Gemeinde Inden", "https://sdnetrim.kdvz-frechen.de/rim4230/webservice/oparl/v1.1/system"),
    ("Gemeinde Kreuzau", "https://sdnetrim.kdvz-frechen.de/rim4250/webservice/oparl/v1.1/system"),
    ("Gemeinde Langerwehe", "https://sdnetrim.kdvz-frechen.de/rim4260/webservice/oparl/v1.1/system"),
    ("Gemeinde Merzenich", "https://sdnetrim.kdvz-frechen.de/rim4280/webservice/oparl/v1.1/system"),
    ("Gemeinde Nörvenich", "https://sdnetrim.kdvz-frechen.de/rim4160/webservice/oparl/v1.1/system"),
    ("Gemeinde Titz", "https://sdnetrim.kdvz-frechen.de/rim4170/webservice/oparl/v1.1/system"),
    ("Gemeinde Vettweiß", "https://sdnetrim.kdvz-frechen.de/rim4180/webservice/oparl/v1.1/system"),
    ("Stadt Linnich", "https://sdnetrim.kdvz-frechen.de/rim4270/webservice/oparl/v1.1/system"),
    ("Gemeinde Kall", "https://sdnetrim.kdvz-frechen.de/rim4550/webservice/oparl/v1.1/system"),

    # Aggregators
    ("Politik bei Uns", "https://oparl.politik-bei-uns.de/system"),
    ("OParl Mirror", "https://mirror.oparl.org/system"),
]


class Command(BaseCommand):
    help = "Import known German OParl sources (with is_active=False by default)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--activate",
            action="store_true",
            help="Set is_active=True for new sources (default: False)",
        )
        parser.add_argument(
            "--update",
            action="store_true",
            help="Update existing sources (name only, won't change is_active)",
        )

    def handle(self, *args, **options):
        activate = options["activate"]
        update = options["update"]

        self.stdout.write(self.style.NOTICE(
            f"\nImporting {len(OPARL_SOURCES)} OParl sources..."
        ))
        self.stdout.write(self.style.NOTICE(
            f"is_active will be set to: {activate}\n"
        ))

        created = 0
        updated = 0
        skipped = 0

        for name, url in OPARL_SOURCES:
            try:
                source, was_created = OParlSource.objects.get_or_create(
                    url=url,
                    defaults={
                        "name": name,
                        "is_active": activate,
                    }
                )

                if was_created:
                    created += 1
                    self.stdout.write(
                        self.style.SUCCESS(f"  + {name}")
                    )
                elif update:
                    source.name = name
                    source.save(update_fields=["name", "updated_at"])
                    updated += 1
                    self.stdout.write(
                        self.style.WARNING(f"  ~ {name} (updated)")
                    )
                else:
                    skipped += 1
                    self.stdout.write(
                        self.style.HTTP_INFO(f"  - {name} (exists)")
                    )

            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"  ! {name}: {e}")
                )

        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS(f"Created: {created}"))
        self.stdout.write(self.style.WARNING(f"Updated: {updated}"))
        self.stdout.write(self.style.HTTP_INFO(f"Skipped: {skipped}"))
        self.stdout.write("=" * 50)

        if created > 0 and not activate:
            self.stdout.write(self.style.NOTICE(
                "\nSources were added with is_active=False."
            ))
            self.stdout.write(self.style.NOTICE(
                "Activate them in Django Admin to start syncing."
            ))
