"""API cost tracking with monthly budget enforcement."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_PRICING = {
    # Standard list prices per MTok. (Sonnet 5 has intro pricing of $2/$10
    # through 2026-08-31; we track at list price — conservative overestimate.)
    "claude-sonnet-5": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}

CACHE_READ_MULT = 0.1    # cache reads bill at ~0.1x input price
CACHE_WRITE_MULT = 1.25  # 5-minute-TTL cache writes bill at 1.25x input price


class CostTracker:

    def __init__(self, log_path: str = "logs/cost.jsonl", monthly_budget: float = 20.0):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.monthly_budget = monthly_budget
        self._month_cache: tuple[str, float] | None = None

    def record(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        source: str = "interactive",
    ) -> float:
        pricing = MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})
        cost = (
            input_tokens * pricing["input"]
            + cache_read_tokens * pricing["input"] * CACHE_READ_MULT
            + cache_creation_tokens * pricing["input"] * CACHE_WRITE_MULT
            + output_tokens * pricing["output"]
        ) / 1_000_000

        entry = {
            "ts": datetime.now().isoformat(),
            "model": model,
            "in": input_tokens,
            "out": output_tokens,
            "cache_read": cache_read_tokens,
            "cache_write": cache_creation_tokens,
            "source": source,
            "cost": round(cost, 6),
        }

        with open(self.log_path, "a") as f:
            f.write(json.dumps(entry) + "\n")

        self._month_cache = None

        over, spend, msg = self.check_budget()
        if msg:
            log.warning(msg)

        return cost

    def get_monthly_spend(self) -> float:
        month_key = datetime.now().strftime("%Y-%m")
        if self._month_cache and self._month_cache[0] == month_key:
            return self._month_cache[1]

        if not self.log_path.exists():
            return 0.0

        total = 0.0
        with open(self.log_path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry["ts"].startswith(month_key):
                        total += entry["cost"]
                except (json.JSONDecodeError, KeyError):
                    continue

        self._month_cache = (month_key, total)
        return total

    def check_budget(self) -> tuple[bool, float, str]:
        spend = self.get_monthly_spend()
        pct = (spend / self.monthly_budget * 100) if self.monthly_budget > 0 else 0
        if pct >= 95:
            return True, spend, f"Budget critical: ${spend:.2f}/${self.monthly_budget:.2f} ({pct:.0f}%)"
        if pct >= 75:
            return False, spend, f"Budget warning: ${spend:.2f}/${self.monthly_budget:.2f} ({pct:.0f}%)"
        return False, spend, ""

    def should_force_downgrade(self) -> bool:
        return self.get_monthly_spend() >= self.monthly_budget * 0.95

    def summary(self) -> str:
        spend = self.get_monthly_spend()
        return f"${spend:.2f} / ${self.monthly_budget:.2f} this month"
