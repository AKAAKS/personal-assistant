"""Home Assistant REST API client."""

import requests


class HomeAssistantClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str) -> dict:
        resp = requests.get(f"{self.base_url}{path}", headers=self.headers, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, data: dict | None = None) -> dict:
        resp = requests.post(
            f"{self.base_url}{path}", headers=self.headers, json=data or {}, timeout=10
        )
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    # --- Query methods ---

    def get_all_entities(self) -> list[dict]:
        """Return a list of all entity states."""
        return self._get("/api/states")

    def get_entity_state(self, entity_id: str) -> dict:
        """Return the current state of a single entity."""
        return self._get(f"/api/states/{entity_id}")

    def get_entities_by_domain(self, domain: str) -> list[dict]:
        """Return all entities for a given domain (e.g. 'light', 'switch', 'media_player')."""
        return [e for e in self.get_all_entities() if e["entity_id"].startswith(f"{domain}.")]

    # --- Action methods ---

    def call_service(self, domain: str, service: str, entity_id: str, data: dict | None = None) -> dict:
        """Call a Home Assistant service on an entity.

        Examples:
            call_service("light", "turn_on", "light.living_room", {"brightness": 255})
            call_service("switch", "turn_off", "switch.fan")
            call_service("media_player", "media_play", "media_player.speaker")
        """
        payload = {"entity_id": entity_id}
        if data:
            payload.update(data)
        return self._post(f"/api/services/{domain}/{service}", payload)

    def send_notification(self, message: str, title: str = "Assistant", target: str = "notify.mobile_app") -> dict:
        """Send a push notification via the HA Companion App."""
        return self._post(
            f"/api/services/{target.split('.')[0]}/{target.split('.')[1] if '.' in target else 'notify'}",
            {"message": message, "title": title},
        )

    # --- Convenience helpers ---

    def turn_on(self, entity_id: str, **kwargs) -> dict:
        domain = entity_id.split(".")[0]
        return self.call_service(domain, "turn_on", entity_id, kwargs or None)

    def turn_off(self, entity_id: str) -> dict:
        domain = entity_id.split(".")[0]
        return self.call_service(domain, "turn_off", entity_id)

    def check_connection(self) -> bool:
        """Return True if Home Assistant API is reachable."""
        try:
            self._get("/api/")
            return True
        except requests.RequestException:
            return False
