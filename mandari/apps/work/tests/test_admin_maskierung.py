# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Support-Admin maskiert Nachrichten, Beschreibungen und Namen über ``format_html``.

Inhalte stammen von Mitgliedern der Organisationen; im Admin lesen sie Superuser.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from django.contrib import admin
from django.utils import timezone

from apps.work.admin import SupportTicketAdmin, SupportTicketMessageInline
from apps.work.support.models import SupportTicket, SupportTicketMessage

BOESE = '<script>alert("x")</script>'


def _nachricht(**werte: object) -> SimpleNamespace:
    basis = {
        "get_content_decrypted": lambda: BOESE,
        "author_staff": None,
        "author_membership": SimpleNamespace(user=SimpleNamespace(get_full_name=lambda: BOESE, email="a@example.org")),
        "is_internal": True,
        "created_at": timezone.make_aware(datetime(2026, 9, 26, 12, 30)),
    }
    basis.update(werte)
    return SimpleNamespace(**basis)


def test_nachricht_maskiert_inhalt_und_autor() -> None:
    inline = SupportTicketMessageInline(SupportTicketMessage, admin.site)

    html = str(inline.message_display(_nachricht()))

    assert "<script>" not in html
    assert html.count("&lt;script&gt;") == 2
    assert "<span class='ticket-msg-internal-badge'>Interne Notiz</span>" in html
    assert "(a@example.org)" in html
    assert "26.09.2026 12:30" in html


def test_nachricht_vom_support_maskiert_namen() -> None:
    inline = SupportTicketMessageInline(SupportTicketMessage, admin.site)
    staff = SimpleNamespace(get_full_name=lambda: BOESE, email="s@example.org")

    html = str(inline.message_display(_nachricht(author_staff=staff, is_internal=False)))

    assert "<script>" not in html
    assert "<strong class='ticket-msg-staff'>⚡ &lt;script&gt;" in html
    assert "ticket-msg-internal-badge" not in html


def test_beschreibung_maskiert() -> None:
    ticket_admin = SupportTicketAdmin(SupportTicket, admin.site)

    html = str(ticket_admin.description_display(SimpleNamespace(get_description_decrypted=lambda: BOESE)))

    assert html == '<div class="ticket-description">&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;</div>'
