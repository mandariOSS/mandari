# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Adapter der Datendrehscheibe (``docs/adr/20260929-adapter-rahmen.md``, A9).

Ein Adapter übersetzt zwischen dem kanonischen RIS-Modell und dem Format eines Fremdsystems, ohne eigene
Geschäftsregeln: Was öffentlich ist, unter welcher Lizenz und seit wann, entscheidet der Eigentümer der
Daten; der Adapter liest es über die Drehscheibe und gibt es im fremden Format wieder.

- ``hub.adapters.dcat``: Katalog der offenen Daten je Kommune nach DCAT-AP.de 3.0 für Open-Data-Portale
  (GovData, Landesportale, CKAN mit ckanext-dcat). Die Portale holen den Katalog ab; es gibt keine
  Zustellung und deshalb kein Abonnement auf Ereignisse.
"""
