# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Organization Ranking Utilities.

Provides ranking/sorting for organizations (Gremien) based on their importance.
The ranking is determined by analyzing the organization name and type.

Priority levels (lower = more important):
10: Rat (Council)
20: Hauptausschuss (Main Committee)
30: Regular Ausschüsse (Committees)
40: Betriebsausschüsse (Operating Committees)
50: Specialized Ausschüsse (Kultur, Sport, Rechnungsprüfung)
60: Unterausschüsse (Subcommittees)
70: Wahlausschüsse (Election Committees)
80: Kommissionen (Commissions)
90: Bezirksvertretungen (District Representations)
95: Integrationsrat
100: Beiräte (Advisory Boards)
110: Jugendrat, Seniorenvertretung
200: Company boards (GmbH, Aufsichtsrat, Gesellschafterversammlung)
210: Zweckverbände (Special Purpose Associations)
300: Other/Unknown

Inactive penalty:
+500: Organizations without a meeting in the last 12 months are considered
      inactive and get a penalty added to their priority.
"""

import re
from datetime import timedelta

from django.db.models import Case, IntegerField, Max, Value, When
from django.utils import timezone

# Penalty for organizations without recent activity (no meeting in 12 months)
INACTIVITY_PENALTY = 500
INACTIVITY_MONTHS = 12


# Ranking rules: (pattern, priority, is_exact_match)
# Patterns are matched case-insensitively against the organization name
# Lower priority = more important
RANKING_RULES = [
    # Exact matches (highest priority)
    (r"^Rat$", 10, True),
    (r"^Hauptausschuss$", 20, True),
    (r"^Haupt- und Finanzausschuss$", 20, True),
    # Regular committees (pattern matches)
    (r"^Ausschuss für ", 30, False),
    (r"^Betriebsausschuss ", 40, False),
    (r"^Kulturausschuss$", 50, True),
    (r"^Sportausschuss$", 50, True),
    (r"^Rechnungsprüfungsausschuss$", 50, True),
    (r"^Unterausschuss ", 60, False),
    (r"^Wahlausschuss$", 70, True),
    (r"^Wahlprüfungsausschuss$", 70, True),
    (r"^Wahlausschuss ", 70, False),
    # Commissions
    (r"^Kommission ", 80, False),
    (r"Kommission$", 80, False),
    # District representations
    (r"^Bezirksvertretung ", 90, False),
    # Integration council
    (r"^Integrationsrat$", 95, True),
    # Advisory boards
    (r"^Beirat ", 100, False),
    (r" Beirat$", 100, False),
    # Youth/Senior councils
    (r"^Jugendrat$", 110, True),
    (r"^Seniorenvertretung$", 110, True),
    (r"^Kommunale Seniorenvertretung$", 110, True),
    # Company boards (GmbH, etc.) - lowest priority for active committees
    (r"GmbH", 200, False),
    (r"Aufsichtsrat$", 200, False),
    (r"Gesellschafterversammlung$", 200, False),
    (r"eG,", 200, False),
    (r"e\.V\.", 200, False),
    # Special purpose associations
    (r"^Zweckverband ", 210, False),
]


def get_organization_priority(name: str) -> int:
    """
    Calculate the ranking priority for an organization based on its name.

    Args:
        name: The organization name

    Returns:
        Integer priority (lower = more important)
    """
    if not name:
        return 300

    for pattern, priority, is_exact in RANKING_RULES:
        if is_exact:
            # Exact match (case-insensitive)
            if re.match(pattern, name, re.IGNORECASE):
                return priority
        else:
            # Pattern match
            if re.search(pattern, name, re.IGNORECASE):
                return priority

    return 300  # Default priority for unknown types


def get_ranking_annotation():
    """
    Create a Django Case/When annotation for database-level sorting.

    This creates a 'ranking_priority' annotation that can be used in order_by().

    Usage:
        organizations.annotate(
            ranking_priority=get_ranking_annotation()
        ).order_by('ranking_priority', 'name')

    Returns:
        Case expression for use with annotate()
    """
    whens = [
        # Exact matches - use __iexact for case-insensitive exact match
        When(name__iexact="Rat", then=Value(10)),
        When(name__iexact="Hauptausschuss", then=Value(20)),
        When(name__iexact="Haupt- und Finanzausschuss", then=Value(20)),
        # Pattern matches - use __istartswith and __icontains
        When(name__istartswith="Ausschuss für ", then=Value(30)),
        When(name__istartswith="Betriebsausschuss ", then=Value(40)),
        When(name__iexact="Kulturausschuss", then=Value(50)),
        When(name__iexact="Sportausschuss", then=Value(50)),
        When(name__iexact="Rechnungsprüfungsausschuss", then=Value(50)),
        When(name__istartswith="Unterausschuss ", then=Value(60)),
        When(name__iexact="Wahlausschuss", then=Value(70)),
        When(name__iexact="Wahlprüfungsausschuss", then=Value(70)),
        When(name__istartswith="Wahlausschuss ", then=Value(70)),
        When(name__istartswith="Kommission ", then=Value(80)),
        When(name__iendswith="kommission", then=Value(80)),
        When(name__istartswith="Bezirksvertretung ", then=Value(90)),
        When(name__iexact="Integrationsrat", then=Value(95)),
        When(name__istartswith="Beirat ", then=Value(100)),
        When(name__iendswith=" Beirat", then=Value(100)),
        When(name__iexact="Jugendrat", then=Value(110)),
        When(name__icontains="Seniorenvertretung", then=Value(110)),
        # Company boards (lower priority)
        When(name__icontains="GmbH", then=Value(200)),
        When(name__iendswith="Aufsichtsrat", then=Value(200)),
        When(name__iendswith="Gesellschafterversammlung", then=Value(200)),
        When(name__icontains="eG,", then=Value(200)),
        When(name__icontains="e.V.", then=Value(200)),
        When(name__istartswith="Zweckverband ", then=Value(210)),
    ]

    return Case(*whens, default=Value(300), output_field=IntegerField())


def get_inactivity_penalty_annotation():
    """
    Create a Django Case/When annotation for inactivity penalty.

    Organizations without a meeting in the last 12 months get a penalty
    of 500 added to their ranking priority.

    Usage:
        organizations.annotate(
            last_meeting_date=Max('meetings__start'),
            inactivity_penalty=get_inactivity_penalty_annotation()
        )

    Returns:
        Case expression for use with annotate()
    """
    cutoff_date = timezone.now() - timedelta(days=INACTIVITY_MONTHS * 30)

    return Case(
        # No meetings at all -> inactive
        When(last_meeting_date__isnull=True, then=Value(INACTIVITY_PENALTY)),
        # Last meeting older than 12 months -> inactive
        When(last_meeting_date__lt=cutoff_date, then=Value(INACTIVITY_PENALTY)),
        # Active -> no penalty
        default=Value(0),
        output_field=IntegerField(),
    )


def sort_organizations_by_ranking(queryset, include_activity=True):
    """
    Sort a queryset of organizations by ranking priority, then by name.

    Organizations are sorted by:
    1. Activity status (active organizations first, if include_activity=True)
    2. Type-based ranking priority (Rat, Hauptausschuss, etc.)
    3. Name (alphabetically)

    Args:
        queryset: Django QuerySet of OParlOrganization objects
        include_activity: If True, inactive organizations (no meeting in 12 months)
                         are sorted to the bottom. Default: True

    Returns:
        Sorted QuerySet with annotations:
        - ranking_priority: The type-based priority (10-300)
        - last_meeting_date: Date of the most recent meeting (if include_activity)
        - inactivity_penalty: 0 or 500 (if include_activity)
        - final_priority: ranking_priority + inactivity_penalty (if include_activity)
    """
    if include_activity:
        # Annotate with last meeting date and calculate final priority
        from django.db.models import F

        cutoff_date = timezone.now() - timedelta(days=INACTIVITY_MONTHS * 30)

        return (
            queryset.annotate(
                ranking_priority=get_ranking_annotation(),
                last_meeting_date=Max("meetings__start"),
            )
            .annotate(
                inactivity_penalty=Case(
                    When(last_meeting_date__isnull=True, then=Value(INACTIVITY_PENALTY)),
                    When(last_meeting_date__lt=cutoff_date, then=Value(INACTIVITY_PENALTY)),
                    default=Value(0),
                    output_field=IntegerField(),
                ),
            )
            .annotate(final_priority=F("ranking_priority") + F("inactivity_penalty"))
            .order_by("final_priority", "name")
        )
    else:
        # Simple ranking without activity check
        return queryset.annotate(ranking_priority=get_ranking_annotation()).order_by("ranking_priority", "name")
