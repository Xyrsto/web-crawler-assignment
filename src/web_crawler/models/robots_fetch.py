from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RobotsFetch:
    """What happened when robots.txt was requested."""

    state: Literal["ok", "missing", "unreachable"]
    text: str = ""
