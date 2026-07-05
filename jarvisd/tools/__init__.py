"""Tool registry — assembles definitions + handlers for the available clients."""

from jarvisd.tools import ha, spotify, system, tv, memory


def build_registry(ha_client, system_client, spotify_client=None, tv_client=None, profile_store=None, memory_store=None):
    """Return (tools, handlers) for the clients that exist.

    tools: list of Anthropic tool definitions (dicts)
    handlers: tool name -> callable(tool_input: dict) -> str
    """
    tools = list(ha.TOOLS) + list(system.TOOLS)
    handler_map = {}
    handler_map.update(ha.handlers(ha_client))
    handler_map.update(system.handlers(system_client))
    if spotify_client is not None:
        tools += list(spotify.TOOLS)
        handler_map.update(spotify.handlers(spotify_client))
    if tv_client is not None:
        tools += list(tv.TOOLS)
        handler_map.update(tv.handlers(tv_client))
    if profile_store is not None and memory_store is not None:
        tools += list(memory.TOOLS)
        handler_map.update(memory.handlers(profile_store, memory_store))
    return tools, handler_map
