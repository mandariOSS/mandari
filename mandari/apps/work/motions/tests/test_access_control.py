# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Zugriffsschutz für Anträge (Work): Freigabe-Leak auf private Dokumente und Yjs-Persistenz.

Migriert aus scripts/smoke_motions_access.py. Abgesichert werden zwei behobene Fehler:
- Ein Mitglied mit Bearbeitungsrecht, aber ohne Zugriff auf ein PRIVATES Dokument, kann sich über
  eine Freigabeanfrage keinen Zugriff verschaffen; die Sichtbarkeit bleibt privat.
- Ein Kollaborations-Client mit Lese- oder Kommentarrecht überschreibt den gemeinsamen
  Yjs-Zustand nicht.
"""

import base64

import pytest
from asgiref.sync import async_to_sync

from apps.work.motions.consumers import DocumentCollaborationConsumer
from apps.work.motions.models import Motion, MotionShare

EDIT_PERMISSIONS = ["motions.view", "motions.edit", "motions.comment"]


@pytest.fixture
def setup(org, make_member):
    author = make_member(org, EDIT_PERMISSIONS, email="author@example.org")
    attacker = make_member(org, EDIT_PERMISSIONS, email="attacker@example.org")
    motion = Motion.objects.create(organization=org, author=author, title="Geheimer Antrag", visibility="private")
    return {"org": org, "author": author, "attacker": attacker, "motion": motion}


def approval_url(org, motion):
    return f"/work/{org.slug}/documents/{motion.id}/approvals/request/"


@pytest.mark.django_db
def test_approval_request_without_access_is_rejected(setup, client_for):
    org, motion, attacker = setup["org"], setup["motion"], setup["attacker"]
    client = client_for(attacker.user)

    response = client.post(
        approval_url(org, motion),
        {"approver": str(attacker.id), "approval_type": "chair"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )
    motion.refresh_from_db()

    assert response.status_code == 403
    assert motion.visibility == "private"
    assert not MotionShare.objects.filter(motion=motion, user=attacker.user).exists()
    assert not motion.can_access(attacker)


@pytest.mark.django_db
def test_author_can_request_approval(setup, make_member, client_for):
    org, motion, author = setup["org"], setup["motion"], setup["author"]
    approver = make_member(org, EDIT_PERMISSIONS, email="approver@example.org")
    client = client_for(author.user)

    response = client.post(
        approval_url(org, motion),
        {"approver": str(approver.id), "approval_type": "chair"},
        HTTP_X_REQUESTED_WITH="XMLHttpRequest",
    )

    assert response.status_code == 200


def _persist(motion, access_level):
    consumer = DocumentCollaborationConsumer()
    consumer.document_id = str(motion.id)
    consumer.user_info = {"access_level": access_level}
    sample = base64.b64encode(b"\x01\x02\x03yjs-state").decode()
    async_to_sync(consumer._persist_yjs_state)(sample, None)
    motion.refresh_from_db()


@pytest.mark.django_db
@pytest.mark.parametrize("access_level", ["view", "comment"])
def test_readonly_collaboration_client_does_not_persist_state(setup, access_level):
    motion = setup["motion"]
    _persist(motion, access_level)
    assert not motion.yjs_document


@pytest.mark.django_db
def test_edit_collaboration_client_persists_state(setup):
    motion = setup["motion"]
    _persist(motion, "edit")
    assert motion.yjs_document
