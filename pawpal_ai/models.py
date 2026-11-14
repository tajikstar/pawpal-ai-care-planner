"""Plain data shared by the planner backends, guardrails and agent."""

from dataclasses import asdict, dataclass, field
from typing import List, Optional


def life_stage_for(species: str, age_years: Optional[float]) -> str:
    """Maps species + age to the life stages the knowledge base uses."""
    if age_years is None:
        return "adult"
    if age_years < 1:
        return "puppy" if species == "dog" else "kitten"
    senior_age = 7 if species == "dog" else 11
    return "senior" if age_years >= senior_age else "adult"


@dataclass
class PetProfile:
    name: str
    species: str  # "dog", "cat", or anything else (unsupported)
    age_years: Optional[float] = None
    conditions: List[str] = field(default_factory=list)

    @property
    def life_stage(self) -> str:
        return life_stage_for(self.species, self.age_years)


@dataclass
class CareRequest:
    """What the parse step understood from the owner's message."""
    pets: List[PetProfile] = field(default_factory=list)
    busy_windows: List[List[str]] = field(default_factory=list)  # [["09:00", "17:00"], ...]
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProposedTask:
    pet: str
    description: str
    time: str  # "HH:MM"
    frequency: str  # "once", "daily", "weekly"
    source: str  # citation id, e.g. "dog-puppy#feeding" or "dog-puppy"
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ProposedTask":
        return cls(
            pet=str(data.get("pet", "")).strip(),
            description=str(data.get("description", "")).strip(),
            time=str(data.get("time", "")).strip(),
            frequency=str(data.get("frequency", "")).strip().lower(),
            source=str(data.get("source", "")).strip(),
            reason=str(data.get("reason", "")).strip(),
        )


@dataclass
class Issue:
    level: str  # "error" (task is rejected) or "warning" (shown to the owner)
    code: str
    message: str
    task_index: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)
