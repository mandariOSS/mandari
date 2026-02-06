# SPDX-License-Identifier: AGPL-3.0-or-later
"""
Work module models - imports all submodule models.

This allows accessing all models via `from apps.work.models import ...`
"""

# Meetings
# Faction meetings
from apps.work.faction.models import (
    FactionAgendaItem,
    FactionAttendance,
    FactionMeeting,
    FactionMeetingException,
    FactionMeetingSchedule,
)
from apps.work.meetings.models import (
    AgendaItemNote,
    AgendaItemPosition,
    MeetingPreparation,
)

# Motions
from apps.work.motions.models import (
    Motion,
    MotionComment,
    MotionDocument,
    MotionRevision,
    MotionShare,
    MotionTemplate,
)

# Notifications
from apps.work.notifications.models import (
    Notification,
    NotificationPreference,
    NotificationType,
)

# Support
from apps.work.support.models import (
    SupportTicket,
    SupportTicketAttachment,
    SupportTicketMessage,
)

# Tasks
from apps.work.tasks.models import Task, TaskComment

__all__ = [
    # Meetings
    "MeetingPreparation",
    "AgendaItemPosition",
    "AgendaItemNote",
    # Motions
    "Motion",
    "MotionShare",
    "MotionDocument",
    "MotionRevision",
    "MotionComment",
    "MotionTemplate",
    # Faction
    "FactionMeeting",
    "FactionMeetingSchedule",
    "FactionMeetingException",
    "FactionAgendaItem",
    "FactionAttendance",
    # Tasks
    "Task",
    "TaskComment",
    # Support
    "SupportTicket",
    "SupportTicketMessage",
    "SupportTicketAttachment",
    # Notifications
    "Notification",
    "NotificationPreference",
    "NotificationType",
]
