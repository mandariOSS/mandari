"""
Work module - API for political organizations (Säule 2).

Implements flexible document workflow with coalition voting.
"""

from src.work.models import (
    ApprovalStatus,
    CoalitionConsultation,
    CoalitionResult,
    CoAuthorInviteStatus,
    CouncilParty,
    Membership,
    MembershipRole,
    Motion,
    MotionApproval,
    MotionCoAuthorInvite,
    MotionShareLog,
    MotionStatus,
    MotionType,
    MotionVisibility,
    Organization,
    ShareMethod,
    User,
    WorkGroup,
    WorkGroupMembership,
    WorkGroupRole,
)

__all__ = [
    # Enums
    "ApprovalStatus",
    "CoalitionResult",
    "CoAuthorInviteStatus",
    "MembershipRole",
    "MotionStatus",
    "MotionVisibility",
    "ShareMethod",
    "WorkGroupRole",
    # Models
    "CoalitionConsultation",
    "CouncilParty",
    "Membership",
    "Motion",
    "MotionApproval",
    "MotionCoAuthorInvite",
    "MotionShareLog",
    "MotionType",
    "Organization",
    "User",
    "WorkGroup",
    "WorkGroupMembership",
]
