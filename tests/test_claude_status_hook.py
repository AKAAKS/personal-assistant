"""The hook adapter: silent, total, and exits 0 whatever happens.

Claude Code runs this script on every lifecycle event of every session. The
coding session is the work and the status feature is a nicety, so the only
behaviour these tests really guard is that nothing the hook can be fed makes it
raise, print, or return anything other than 0.
"""

import io
import json

from jarvisd import claude_sessions as cs
from scripts import claude_status_hook as hook


def _raiser(*_args, **_kwargs):
    raise RuntimeError("the store is broken")


def test_a_valid_event_reaches_the_store(tmp_path):
    payload = json.dumps({"hook_event_name": "SessionStart",
                          "session_id": "s1", "cwd": "/x/proj"})
    assert hook.main(io.StringIO(payload), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path)[0]["state"] == cs.IDLE


def test_the_project_survives_a_posix_cwd(tmp_path):
    payload = json.dumps({"hook_event_name": "SessionStart",
                          "session_id": "s1", "cwd": "/x/proj"})
    hook.main(io.StringIO(payload), directory=tmp_path)
    assert cs.read_raw(directory=tmp_path)[0]["project"] == "proj"


def test_empty_stdin_exits_zero_and_writes_nothing(tmp_path):
    assert hook.main(io.StringIO(""), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path) == []


def test_whitespace_only_stdin_exits_zero(tmp_path):
    assert hook.main(io.StringIO("   \n"), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path) == []


def test_non_json_stdin_exits_zero(tmp_path):
    assert hook.main(io.StringIO("not json at all {{{"), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path) == []


def test_json_that_is_not_an_object_exits_zero(tmp_path):
    assert hook.main(io.StringIO("[1,2,3]"), directory=tmp_path) == 0
    assert cs.read_raw(directory=tmp_path) == []


def test_a_stdin_that_cannot_be_read_exits_zero(tmp_path):
    class Broken:
        def read(self):
            raise OSError("pipe closed")

    assert hook.main(Broken(), directory=tmp_path) == 0


def test_a_store_failure_still_exits_zero(monkeypatch, tmp_path):
    """A broken status write must never fail a Claude Code turn."""
    monkeypatch.setattr(cs, "apply_event", _raiser)
    assert hook.main(io.StringIO('{"hook_event_name":"Stop"}'),
                     directory=tmp_path) == 0


def test_it_writes_nothing_to_stdout_or_stderr(capsys, tmp_path):
    hook.main(io.StringIO('{"hook_event_name":"SessionStart","session_id":"s1"}'),
              directory=tmp_path)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_it_stays_silent_even_when_the_store_fails(capsys, monkeypatch, tmp_path):
    """Claude Code interprets hook stdout on some events, so a failure must
    not announce itself there."""
    monkeypatch.setattr(cs, "apply_event", _raiser)
    hook.main(io.StringIO('{"hook_event_name":"Stop","session_id":"s1"}'),
              directory=tmp_path)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_a_hook_whose_repo_is_gone_still_exits_zero_and_says_nothing(tmp_path):
    """The predicted failure: the hook is registered by absolute path, so if
    the checkout it points into is moved or deleted, `import jarvisd` fails at
    module scope -- a traceback on stderr and a non-zero exit inside someone's
    coding session. The spec allows neither."""
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    source = Path(hook.__file__)
    stranded = tmp_path / "scripts" / "claude_status_hook.py"
    stranded.parent.mkdir(parents=True)
    shutil.copyfile(source, stranded)

    result = subprocess.run(
        [sys.executable, "-I", str(stranded)],
        input='{"hook_event_name":"Stop","session_id":"s1"}',
        capture_output=True, text=True, cwd=str(tmp_path), timeout=60,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_with_no_directory_it_uses_the_shared_state_directory(monkeypatch, tmp_path):
    seen = {}

    def spy(payload, *, directory=None, **kw):
        seen["directory"] = directory

    monkeypatch.setattr(cs, "apply_event", spy)
    hook.main(io.StringIO('{"hook_event_name":"Stop","session_id":"s1"}'))
    assert seen["directory"] == cs.DIR
