from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nutrition_core.models import Macros


def remaining(goal: Macros, eaten: Macros) -> Macros:
    return goal - eaten


def after_meal(remaining_now: Macros, meal_total: Macros) -> Macros:
    return remaining_now - meal_total


def min_kcal_for(macros: Macros) -> float:
    # Atwater floor: the calories needed to supply the remaining macros.
    # Negative components are an overshoot, not a calorie requirement.
    return (
        4 * max(macros.protein_g, 0.0)
        + 4 * max(macros.carbs_g, 0.0)
        + 9 * max(macros.fat_g, 0.0)
    )


@dataclass(frozen=True)
class FitAssessment:
    status: Literal["ok", "over", "cornered"]
    messages: tuple[str, ...]


def assess_day(after: Macros, *, kcal_tolerance: float = 50.0) -> FitAssessment:
    overs: list[str] = []
    if after.kcal < -kcal_tolerance:
        overs.append(f"calories over budget by {-after.kcal:.0f} kcal")
    for label, value in (
        ("protein", after.protein_g),
        ("carbs", after.carbs_g),
        ("fat", after.fat_g),
    ):
        if value < 0:
            overs.append(f"{label} over budget by {-value:.1f} g")
    if overs:
        return FitAssessment("over", tuple(overs))

    floor = min_kcal_for(after)
    if after.kcal + kcal_tolerance < floor:
        return FitAssessment(
            "cornered",
            (
                f"remaining macros need at least {floor:.0f} kcal "
                f"but only {max(after.kcal, 0.0):.0f} kcal left",
            ),
        )

    return FitAssessment("ok", ())
