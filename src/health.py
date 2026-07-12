"""Health reporting models.

The active health monitor will be implemented in a later stage.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(slots=True)
class HealthSnapshot:
    status: str
    version: str
    timestamp: str

    @classmethod
    def starting(cls, version: str) -> "HealthSnapshot":
        return cls(
            status="starting",
            version=version,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
