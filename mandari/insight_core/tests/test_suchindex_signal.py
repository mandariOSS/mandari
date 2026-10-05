# SPDX-License-Identifier: AGPL-3.0-or-later
"""Signal ``index_file``: Der Suchindex folgt erst nach dem Commit (Issue #821)."""

from __future__ import annotations

from typing import Any

import pytest
from django.db import transaction

from insight_core import signals
from insight_core.models import OParlBody, OParlFile, OParlSource


@pytest.mark.django_db
def test_datei_wird_erst_nach_dem_commit_indexiert(
    monkeypatch: pytest.MonkeyPatch, django_capture_on_commit_callbacks: Any
) -> None:
    """
    Speichert die Texterkennung den Text in einer Transaktion (mit ihrem Ereignis), darf der Index ihn erst
    nach dem Commit sehen; rollt sie zurück, bleibt der Index unberührt.
    """
    indexiert: list[str] = []
    monkeypatch.setattr(signals, "_index_document", lambda index, doc_id, doc: indexiert.append(f"{index}/{doc_id}"))
    quelle = OParlSource.objects.create(name="Quelle", url="https://ris.example.org/oparl/system")
    body = OParlBody.objects.create(external_id="https://ris.example.org/oparl/body/1", source=quelle, name="Stadt")
    datei = OParlFile.objects.create(external_id="https://ris.example.org/oparl/file/1", body=body, name="Anlage")
    datei.text_content, datei.text_extraction_status = "Beschluss zum Radweg", "completed"

    with pytest.raises(RuntimeError), django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        datei.save(update_fields=["text_content", "text_extraction_status"])
        assert indexiert == [], "nicht vor dem Commit"
        raise RuntimeError("Rückrollen")
    assert indexiert == []

    with django_capture_on_commit_callbacks(execute=True), transaction.atomic():
        datei.save(update_fields=["text_content", "text_extraction_status"])
        assert indexiert == [], "nicht vor dem Commit"
    assert indexiert == [f"files/{datei.pk}"]
