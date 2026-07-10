"""create_assistant() wiring: return tuple arity/contents + Assistant capability wiring.

create_assistant()'s return tuple grew by one element in Task 9 (meal_store appended
last), so this asserts the new arity/contents directly rather than relying on the
per-module tests (core/tools/meal_store) to catch a caller-side unpacking break.
"""

import sqlite3


class FakeHA:
    def __init__(self, *a, **k):
        pass

    def check_connection(self):
        return True

    def get_all_entities(self):
        return []

    def get_entities_by_domain(self, domain):
        return []

    def get_entity_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    def call_service(self, domain, action, entity_id, data=None):
        pass

    def send_notification(self, message, title):
        pass


def test_create_assistant_returns_meal_store(monkeypatch, tmp_path):
    import jarvisd.bootstrap as bootstrap

    monkeypatch.setattr(bootstrap, "HomeAssistantClient", FakeHA)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("HA_TOKEN", "test-token")
    monkeypatch.setenv("HA_URL", "http://localhost:8123")
    # Force optional integrations off so bootstrap never attempts real auth/network.
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "")
    monkeypatch.setenv("ANDROID_TV_IP", "")
    monkeypatch.setenv("JARVIS_DB", str(tmp_path / "jarvis.db"))
    monkeypatch.setenv("SEARCH_MONTHLY_CAP", "42")

    result = bootstrap.create_assistant()

    assert len(result) == 7
    ai, features, conn, reminder_store, rules_store, ha, meal_store = result

    assert conn is not None
    assert meal_store is not None
    assert "Meals" in features
    assert "Web Search" in features
    assert "File Tools" in features
    assert ai.cost_tracker.search_monthly_cap == 42

    conn.close()


def test_assistant_with_file_tools_exposes_file_and_meal_handlers():
    from jarvisd.core import Assistant
    from jarvisd.meal_store import MealStore
    from jarvisd import db

    conn = db.connect(":memory:")
    meal_store = MealStore(conn)

    ai = Assistant(
        FakeHA(), "sk-test-not-a-real-key",
        meal_store=meal_store,
        file_tools=True,
    )

    assert "run_script" in ai._handlers
    assert "search_files" in ai._handlers
    assert "read_document" in ai._handlers
    assert "log_meal" in ai._handlers

    conn.close()
