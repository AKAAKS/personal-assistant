"""API cost tracking with monthly budget enforcement."""

import json
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_PRICING = {
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
}


class CostTracker:

    def __init__(self, log_path: str = "logs/cost.jsonl", monthly_budget: float = 20.0):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.monthly_budget = monthly_budget
        self._month_cache: tuple[str, float] | None = None

    def record(self, model: str, input_tokens: int, output_tokens: int) -> float:
        pricing = MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})
        cost = (input_tokens * pricing["input"] + output_tokens * pricing["output"]) / 1_000_000

        entry = {
            "ts": datetime.now().isoformat(),
            "model": model,
            "in": input_tokens,
            "out": output_tokens,
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
