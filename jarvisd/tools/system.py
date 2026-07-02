"""PC control tool definitions and handlers."""

TOOLS = [
    {
        "name": "close_app",
        "description": "Close/kill a running application on the PC. Common names: firefox, chrome, edge, notepad, spotify, discord, steam, vscode, word, excel, teams, slack, obs, vlc, terminal.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "App name, e.g. 'chrome', 'spotify', 'discord'.",
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
        "launch_app": lambda ti: system.launch_app(ti["target"]),
        "list_processes": lambda ti: system.list_processes(),
        "open_url_browser": lambda ti: system.open_url(ti["url"]),
        "system_info": lambda ti: system.system_info(),
    }
