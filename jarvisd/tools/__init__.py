"""Tool registry — assembles definitions + handlers for the available clients."""

from jarvisd.tools import ha, spotify, system, tv, memory, reminders, rules, meals, files, proposals, weather


def build_registry(
    ha_client,
    system_client,
    spotify_client=None,
    tv_client=None,
    profile_store=None,
    memory_store=None,
    reminder_store=None,
    rules_store=None,
    meal_store=None,
    file_tools: bool = False,
    file_summarizer=None,
    proposal_store=None,
    location_provider=None,
):
    """Return (tools, handlers) for the clients that exist.

    tools: list of Anthropic tool definitions (dicts)
    handlers: tool name -> callable(tool_input: dict) -> str

    Tools are assembled in order:
    1. ha + system (always)
    2. spotify (if spotify_client)
    3. tv (if tv_client)
    4. memory (if profile_store and memory_store)
    5. reminder (if reminder_store)
    6. rules (if rules_store)
    7. meals (if meal_store)
    8. files (if file_tools is True; file_summarizer may still be None)
    9. proposals (if proposal_store)
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
    if reminder_store is not None:
        tools += list(reminders.TOOLS)
        handler_map.update(reminders.handlers(reminder_store))
    if rules_store is not None:
        tools += list(rules.TOOLS)
        handler_map.update(rules.handlers(rules_store))
    if meal_store is not None:
        tools += list(meals.TOOLS)
        handler_map.update(meals.handlers(meal_store))
    if file_tools:
        tools += list(files.TOOLS)
        handler_map.update(files.handlers(summarizer=file_summarizer))
    if proposal_store is not None:
        tools += list(proposals.TOOLS)
        handler_map.update(proposals.handlers(
            proposal_store=proposal_store,
            rules_store=rules_store,
            profile_store=profile_store,
        ))
    if location_provider is not None:
        tools += list(weather.TOOLS)
        handler_map.update(weather.handlers(location_provider))
    return tools, handler_map
