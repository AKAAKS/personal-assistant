from types import SimpleNamespace
from jarvisd.tools import build_registry


class FakeHA:
    def get_all_entities(self):
        return [{"entity_id": "light.desk", "state": "on", "attributes": {"friendly_name": "Desk"}}]

    def get_entities_by_domain(self, domain):
        return self.get_all_entities()

    def get_entity_state(self, entity_id):
        return {"entity_id": entity_id, "state": "on", "attributes": {}}

    def call_service(self, domain, action, entity_id, data=None):
        self.last = (domain, action, entity_id, data)

    def send_notification(self, message, title):
        pass


class FakeSystem:
    def close_app(self, name): return f"closed {name}"
    def launch_app(self, target): return f"launched {target}"
    def list_processes(self): return "procs"
    def open_url(self, url): return f"opened {url}"
    def system_info(self): return "info"


def test_registry_without_optional_clients():
    tools, handlers = build_registry(FakeHA(), FakeSystem())
    names = [t["name"] for t in tools]
    assert "list_devices" in names and "launch_app" in names
    assert "spotify_play" not in names and "tv_remote" not in names
    assert set(names) == set(handlers.keys())          # every tool has a handler
    assert len(names) == len(set(names))               # no duplicate names


def test_registry_with_optional_clients():
    spotify = SimpleNamespace()  # handlers close over it; not called here
    tv = SimpleNamespace()
    tools, handlers = build_registry(FakeHA(), FakeSystem(), spotify_client=spotify, tv_client=tv)
    names = [t["name"] for t in tools]
    assert "spotify_play" in names and "tv_remote" in names
    assert set(names) == set(handlers.keys())


def test_handler_dispatch_and_client_call():
    ha = FakeHA()
    tools, handlers = build_registry(ha, FakeSystem())
    out = handlers["list_devices"]({})
    assert "Desk" in out and "light.desk" in out
    handlers["control_light"]({"entity_id": "light.desk", "action": "turn_on", "brightness": 128})
    assert ha.last == ("light", "turn_on", "light.desk", {"brightness": 128})


def test_every_tool_def_is_well_formed():
    tools, _ = build_registry(FakeHA(), FakeSystem(), spotify_client=object(), tv_client=object())
    for t in tools:
        assert t["name"] and t["description"], t
        assert t["input_schema"]["type"] == "object", t["name"]
