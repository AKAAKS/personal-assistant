"""Security policy definitions — blocked patterns, keywords, and permission tiers."""

# Permission tiers for all tools.
# read_only:    information queries, always allowed
# write:        modifies state but reversible / low-risk
# destructive:  hard to undo, requires user confirmation before executing
# forbidden:    never allowed
TOOL_PERMISSIONS = {
    # Home Assistant
    "list_devices": "read_only",
    "get_device_status": "read_only",
    "control_light": "write",
    "control_switch": "write",
    "control_media": "write",
    "set_thermostat": "write",
    "send_notification": "write",
    # Spotify
    "spotify_search": "read_only",
    "spotify_play": "write",
    "spotify_playback_control": "write",
    "spotify_queue": "write",
    "spotify_now_playing": "read_only",
    "spotify_devices": "read_only",
    # Android TV
    "tv_launch_app": "write",
    "tv_close_app": "write",
    "tv_remote": "write",
    "tv_type_text": "write",
    "tv_open_url": "write",
    "tv_search_youtube": "write",
    "tv_status": "read_only",
    "tv_power": "write",
    # PC / System
    "close_app": "write",
    "launch_app": "write",
    "list_processes": "read_only",
    "open_url_browser": "write",
    "system_info": "read_only",
    # Memory
    "remember": "write",
    "recall": "read_only",
    # Proactivity
    "set_reminder": "write",
    "list_reminders": "read_only",
    "cancel_reminder": "write",
    "add_trigger_rule": "write",
    "list_trigger_rules": "read_only",
    "delete_trigger_rule": "write",
    # Meals
    "log_meal": "write",
    "meal_summary": "read_only",
    "export_meals": "write",
    # Files
    "search_files": "read_only",
    "read_document": "read_only",
    "run_script": "destructive",
}

DEFAULT_PERMISSION = "write"

# Tools whose tool_input holds user-supplied filesystem paths that must be
# validated against the allowlist. Maps tool name -> list of input keys to
# check. run_script's path is whitelist-constrained by the tool itself (not
# user-supplied), so it is listed with no keys to check here.
PATH_CHECKED_TOOLS = {
    "search_files": ["directory"],
    "read_document": ["path"],
    "run_script": [],
}

# URL patterns that are always blocked (case-insensitive substring match on URLs)
BLOCKED_URL_PATTERNS = [
    "bank", "banking",
    "paypal.com", "venmo.com", "zelle", "cashapp", "cash.app",
    "chase.com", "wellsfargo", "bofa.com", "bankofamerica",
    "citibank", "capitalone",
    "discover.com/bank", "ally.com/bank",
    "creditcard", "credit-card",
    "irs.gov", "ssa.gov",
    "mint.com", "creditkarma", "nerdwallet.com/banking",
    "coinbase.com", "binance.com", "kraken.com",
    "robinhood.com",
    "etrade.com", "schwab.com/trading", "fidelity.com/trading",
]

# Keywords that must never appear in tool inputs (case-insensitive)
BLOCKED_KEYWORDS = [
    "social security number", "ssn",
    "credit card number", "card number", "cvv", "cvc",
    "bank account number", "routing number",
    "debit card number",
    "pin number", "atm pin",
    "tax id", "ein number",
    "passport number",
    "driver license number", "drivers license number",
]
