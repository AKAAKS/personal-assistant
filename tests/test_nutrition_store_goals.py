from jarvisd.db import connect
from nutrition_core.models import Macros
from nutrition_core.store.goals import set_goals, active_goals, goals_history


class TestGoalsRepo:
    def test_set_then_active_roundtrips(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(2000, 150, 200, 60))
        assert active_goals(conn) == Macros(2000, 150, 200, 60)

    def test_new_goal_supersedes_old_single_active(self):
        conn = connect(":memory:")
        set_goals(conn, Macros(2000, 150, 200, 60))
        set_goals(conn, Macros(1800, 160, 150, 55))
        assert active_goals(conn) == Macros(1800, 160, 150, 55)
        n_active = conn.execute("SELECT COUNT(*) c FROM goals WHERE active=1").fetchone()["c"]
        assert n_active == 1 and len(goals_history(conn)) == 2

    def test_active_is_none_when_unset(self):
        assert active_goals(connect(":memory:")) is None
