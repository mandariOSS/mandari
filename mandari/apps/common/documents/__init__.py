# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Fachfreie Dokumentbausteine der Plattform (Schicht 4).

Hier liegt, was Dokumente als Dateien betrifft und keine Fachlogik kennt – derzeit die Seitenanalyse
von PDFs (``page_analysis``). Die Texterkennung zieht mit #530 an denselben Ort bzw. nach
``shared/``; die Module importieren deshalb nichts aus Django-Apps.
"""
