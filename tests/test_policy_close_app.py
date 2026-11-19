"""Tiers and wiring for the two close paths.

close_app is now safe (it closes windows; the app prompts to save), so it
stays `write` and never interrupts the user. force_close_app throws away
unsaved work, so it must land in `destructive` — the only tier that asks.
"""

from types import SimpleNamespace

from jarvisd.tools import build_registry, system as system_tools
from security.policy import TOOL_PERMISSIONS


class FakeHA:
    def get_all_entities(self):
        return []

    def get_entities_by_domain(self, domain):
        return []

    def get_entity_state(self, entity_id):
        return {}

    def call_service(self, domain, action, entity_id, data=None):
        pass

    def send_notification(self, message, title):
        pass


class FakeSystem(SimpleNamespace):
    def close_app(self, name): return f"closed {name}"
    def force_close_app(self, name): return f"force-closed {name}"
    def launch_app(self, target): return f"launched {target}"
    def list_processes(self): return "procs"
    def open_url(self, url): return f"opened {url}"
    def system_info(self): return "info"


def test_force_close_app_requires_confirmation():
    assert TOOL_PERMISSIONS["force_close_app"] == "destructive"


def test_close_app_stays_write_so_routine_closes_do_not_prompt():
    assert TOOL_PERMISSIONS["close_app"] == "write"


def test_force_close_app_is_registered_with_a_handler():
    tools, handlers = build_registry(FakeHA(), FakeSystem())
    names = [t["name"] for t in tools]
    assert "force_close_app" in names
    assert handlers["force_close_app"]({"name": "spotify"}) == "force-closed spotify"


def test_close_app_description_steers_the_model_away_from_the_shell():
    spec = next(t for t in system_tools.TOOLS if t["name"] == "close_app")
    blob = (spec["description"] + str(spec["input_schema"])).lower()
    assert "explorer" in blob, "the model needs to know File Explorer is handled here"
    assert "kill" not in spec["description"].lower(), (
        "close_app no longer kills; the description must not invite it"
    )


def test_force_close_app_description_warns_about_unsaved_work():
    spec = next(t for t in system_tools.TOOLS if t["name"] == "force_close_app")
    assert "unsaved" in spec["description"].lower()
