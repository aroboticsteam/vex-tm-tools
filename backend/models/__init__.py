"""SQLAlchemy ORM models.

Import every model module here so that `Base.metadata` (from
`backend.core.db`) is fully populated for Alembic autogenerate and for
`Base.metadata.create_all()` in tests.
"""
from backend.core.db import Base  # noqa: F401

from backend.models.audit import AuditLog  # noqa: F401
from backend.models.automation import Automation, AutomationFolder, AutomationRun, Script  # noqa: F401
from backend.models.integration import IntegrationInstance, ZerosPreset  # noqa: F401
from backend.models.notification import NotificationLog, PushSubscription, PushSubscriptionTeam  # noqa: F401
from backend.models.overlay import OverlayInstance  # noqa: F401
from backend.models.room import Room, RoomTeam  # noqa: F401
from backend.models.settings import ApiKey, RolePermission, SystemSetting  # noqa: F401
from backend.models.button_page import Button, ButtonPage  # noqa: F401
from backend.models.comms import AgendaItem, CommsPreset, Station, StationMessage  # noqa: F401
from backend.models.team import TeamProfile  # noqa: F401
from backend.models.team_clip import TeamClip  # noqa: F401
from backend.models.timer import PrompterCue, TimerInstance  # noqa: F401

__all__ = [
    "Base",
    "AuditLog",
    "Automation",
    "AutomationFolder",
    "AutomationRun",
    "Script",
    "IntegrationInstance",
    "ZerosPreset",
    "NotificationLog",
    "PushSubscription",
    "PushSubscriptionTeam",
    "OverlayInstance",
    "Room",
    "Station",
    "CommsPreset",
    "AgendaItem",
    "StationMessage",
    "Button",
    "ButtonPage",
    "RoomTeam",
    "ApiKey",
    "RolePermission",
    "SystemSetting",
    "TeamProfile",
    "TeamClip",
    "PrompterCue",
    "TimerInstance",
]
