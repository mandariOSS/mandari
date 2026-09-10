# SPDX-License-Identifier: AGPL-3.0-or-later
"""Planung der GPU-Knoten: Bedarf, Abrechnungsstunde, Sicherheitslöschungen."""

from datetime import UTC, datetime, timedelta

from apps.minutes.orchestrator import (
    ActionKind,
    Demand,
    Limits,
    NodeState,
    NodeView,
    desired_nodes,
    minutes_into_billing_hour,
    plan,
)

NOW = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
LIMITS = Limits(max_nodes=2, jobs_per_node=30, idle_minutes=10, provisioning_timeout_minutes=30)


def ready_node(node_id: str, *, created_minutes_ago: int, idle_minutes: int, running: bool = False) -> NodeView:
    return NodeView(
        id=node_id,
        state=NodeState.BUSY if running else NodeState.READY,
        created_at=NOW - timedelta(minutes=created_minutes_ago),
        ready_at=NOW - timedelta(minutes=created_minutes_ago - 5),
        last_activity_at=NOW - timedelta(minutes=idle_minutes),
        has_running_job=running,
    )


def kinds(actions: list) -> list[str]:  # type: ignore[type-arg]
    return [action.kind for action in actions]


def test_ohne_bedarf_und_ohne_knoten_passiert_nichts() -> None:
    assert plan([], Demand(), LIMITS, NOW) == []


def test_wartender_auftrag_fordert_knoten_an() -> None:
    assert kinds(plan([], Demand(queued_jobs=1), LIMITS, NOW)) == [ActionKind.CREATE]


def test_laufende_aufzeichnung_haelt_knoten_vor() -> None:
    assert kinds(plan([], Demand(active_recordings=1), LIMITS, NOW)) == [ActionKind.CREATE]


def test_hochfahrender_knoten_verhindert_doppelte_anforderung() -> None:
    starting = NodeView(id="n1", state=NodeState.BOOTING, created_at=NOW - timedelta(minutes=8))
    assert plan([starting], Demand(queued_jobs=5), LIMITS, NOW) == []


def test_ohne_erlaubte_knoten_wird_nichts_angefordert() -> None:
    assert plan([], Demand(queued_jobs=10), Limits(max_nodes=0), NOW) == []


def test_viele_auftraege_skalieren_bis_zur_obergrenze() -> None:
    assert desired_nodes(Demand(queued_jobs=31), LIMITS) == 2
    assert desired_nodes(Demand(queued_jobs=500), LIMITS) == 2
    assert kinds(plan([], Demand(queued_jobs=90), LIMITS, NOW)) == [ActionKind.CREATE, ActionKind.CREATE]


def test_leerlauf_mitten_in_der_bezahlten_stunde_wird_nicht_geloescht() -> None:
    node = ready_node("n1", created_minutes_ago=30, idle_minutes=20)
    assert plan([node], Demand(), LIMITS, NOW) == []


def test_leerlauf_am_ende_der_abrechnungsstunde_wird_geloescht() -> None:
    node = ready_node("n1", created_minutes_ago=56, idle_minutes=20)
    actions = plan([node], Demand(), LIMITS, NOW)
    assert kinds(actions) == [ActionKind.DELETE]
    assert actions[0].node_id == "n1"


def test_loeschfenster_gilt_auch_in_spaeteren_stunden() -> None:
    assert minutes_into_billing_hour(NOW - timedelta(minutes=116), NOW) == 56
    node = ready_node("n1", created_minutes_ago=116, idle_minutes=15)
    assert kinds(plan([node], Demand(), LIMITS, NOW)) == [ActionKind.DELETE]


def test_kurzer_leerlauf_wird_nicht_geloescht() -> None:
    node = ready_node("n1", created_minutes_ago=56, idle_minutes=3)
    assert plan([node], Demand(), LIMITS, NOW) == []


def test_waehrend_laufender_aufzeichnung_bleibt_der_knoten() -> None:
    node = ready_node("n1", created_minutes_ago=56, idle_minutes=20)
    assert plan([node], Demand(active_recordings=1), LIMITS, NOW) == []


def test_knoten_mit_laufendem_auftrag_wird_nie_wegen_leerlauf_geloescht() -> None:
    node = ready_node("n1", created_minutes_ago=56, idle_minutes=20, running=True)
    assert plan([node], Demand(), LIMITS, NOW) == []


def test_haengende_bereitstellung_wird_abgebrochen() -> None:
    stuck = NodeView(id="n1", state=NodeState.BOOTING, created_at=NOW - timedelta(minutes=31))
    actions = plan([stuck], Demand(queued_jobs=1), LIMITS, NOW)
    # Der hängende Knoten fliegt raus, und für den Bedarf wird ein neuer angefordert.
    assert kinds(actions) == [ActionKind.DELETE, ActionKind.CREATE]


def test_fehlgeschlagener_knoten_wird_geloescht() -> None:
    failed = NodeView(id="n1", state=NodeState.FAILED, created_at=NOW - timedelta(minutes=5))
    assert kinds(plan([failed], Demand(), LIMITS, NOW)) == [ActionKind.DELETE]


def test_hoechstlaufzeit_schuetzt_vor_vergessenen_knoten() -> None:
    runaway = ready_node("n1", created_minutes_ago=12 * 60 + 1, idle_minutes=0, running=True)
    assert kinds(plan([runaway], Demand(queued_jobs=1), LIMITS, NOW)) == [ActionKind.DELETE, ActionKind.CREATE]


def test_ueberzaehlige_knoten_werden_abgebaut_der_laengste_leerlauf_zuerst() -> None:
    kurz = ready_node("kurz", created_minutes_ago=57, idle_minutes=12)
    lang = ready_node("lang", created_minutes_ago=57, idle_minutes=40)
    actions = plan([kurz, lang], Demand(queued_jobs=1), LIMITS, NOW)
    assert [(action.kind, action.node_id) for action in actions] == [(ActionKind.DELETE, "lang")]
