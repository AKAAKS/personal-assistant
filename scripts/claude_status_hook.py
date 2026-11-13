"""The Claude Code hook: read one event off stdin, record it, exit 0.

Claude Code runs this on SessionStart, UserPromptSubmit, PermissionRequest,
Notification, Stop and SessionEnd, as an `async` hook so the coding session
never waits on it. All the logic lives in `jarvisd.claude_sessions`; this file
exists only because a hook is a process, not a function call.

Three properties, and they are the whole point of keeping it this thin:

  - **Exits 0 always.** A non-zero hook can surface as an error in a Claude
    Code turn. The status feature is a nicety; the coding session is the work.
  - **Silent.** Nothing on stdout or stderr, ever -- Claude Code interprets
    hook stdout on some events, so even a traceback is a side effect.
  - **Path-independent.** The hook inherits the coding session's working
    directory, which is a different project every time, so the repo root comes
    from __file__ and the state directory from the module.

Install with `python scripts/install_claude_hooks.py`.
"""

import json
import sys
from pathlib import Path

# Claude Code invokes this by absolute path from an arbitrary cwd, so the repo
# root has to be put on the path by hand before jarvisd can be imported.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from jarvisd import claude_sessions  # noqa: E402  (after the sys.path fix)


def main(stdin, *, directory=None) -> int:
    """Record the event on `stdin`. Returns 0 whatever happens."""
    try:
        raw = stdin.read()
        if not raw or not raw.strip():
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        if directory is None:
            directory = claude_sessions.DIR
        claude_sessions.apply_event(payload, directory=directory)
    except Exception:
        # Deliberately bare: there is no failure mode here worth a non-zero
        # exit or a line of output in someone's coding session.
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.stdin))
