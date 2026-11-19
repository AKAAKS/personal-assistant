"""PC control tool definitions and handlers."""

TOOLS = [
    {
        "name": "close_app",
        "description": (
            "Close a running application on the PC, the way clicking its X does — "
            "the app may prompt the user about unsaved work. Use this for every "
            "ordinary close request, including 'close File Explorer' (pass "
            "'file explorer'), which closes the folder window and leaves the "
            "Windows shell running. Common names: firefox, chrome, edge, notepad, "
            "file explorer, spotify, discord, steam, vscode, word, excel, teams, "
            "slack, obs, vlc, terminal."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "App name, e.g. 'chrome', 'file explorer', 'spotify'.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "force_close_app",
        "description": (
            "Force-kill an unresponsive app. DESTRUCTIVE — unsaved work is lost "
            "with no prompt, and it asks the user to confirm first. Only use it "
            "after close_app has already failed, or when the user explicitly says "
            "'force close'. Windows shell and system processes (explorer.exe, "
            "dwm.exe, lsass.exe …) are refused: killing them blanks the desktop "
            "and taskbar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "App name of the hung app, e.g. 'spotify'.",
                }
            },
            "required": ["name"],
        },
    },
    {
        "name": "launch_app",
        "description": "Launch an application on the PC. Common names: firefox, chrome, edge, notepad, calculator, settings, spotify, discord, steam, vscode, terminal, task manager, snipping tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": "App name or executable path to launch.",
                }
            },
            "required": ["target"],
        },
    },
    {
        "name": "list_processes",
        "description": "List the top 30 running processes on the PC with memory usage.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "open_url_browser",
        "description": "Open a URL in the default web browser on the PC.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to open.",
                }
            },
            "required": ["url"],
        },
    },
    {
        "name": "system_info",
        "description": "Get current PC system information: CPU usage, RAM usage, disk space.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


def handlers(system):
    """PC control tool handlers closing over a SystemClient."""
    return {
        "close_app": lambda ti: system.close_app(ti["name"]),
        "force_close_app": lambda ti: system.force_close_app(ti["name"]),
        "launch_app": lambda ti: system.launch_app(ti["target"]),
        "list_processes": lambda ti: system.list_processes(),
        "open_url_browser": lambda ti: system.open_url(ti["url"]),
        "system_info": lambda ti: system.system_info(),
    }
