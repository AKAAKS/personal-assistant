# Unified App Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `launch_app`/`close_app`/is-running agree on one app record each, so UWP apps like Camera launch, verify, and close through the non-confirming `write` tier instead of being routed into destructive `run_command`.

**Architecture:** A new `app_registry.py` module owns one `AppEntry` per app (aliases, launch target, process image). `SystemClient` keeps its public method signatures and delegates all name resolution to the registry, deleting `app_map`, `process_map`, and `_RUNNING_NAMES`. `launch_app` gains post-launch verification; `list_processes` is corrected to sort by memory as its description already claims.

**Tech Stack:** Python 3.12, stdlib only (`os.startfile`, `subprocess`, `tasklist`/`taskkill`), pytest + monkeypatch.

**Spec:** `docs/superpowers/specs/2026-11-15-app-launch-and-alarms-design.md` (Part A)

## Global Constraints

- Launching any app in the registry must go through `launch_app`, which is tier `write` in `security/policy.py` — never `run_command` (`destructive`). Do not change any tier.
- `SystemClient.launch_app(target: str) -> str`, `close_app(name: str) -> str`, `list_processes() -> str` keep their exact names and signatures; `jarvisd/tools/system.py` handlers are not rewired.
- UWP apps launch by shell protocol URI (e.g. `microsoft.windows.camera:`) via `os.startfile`, never `explorer.exe shell:appsFolder\...` — the latter returned exit 1 in production.
- No new third-party dependency. `pywin32` stays optional, as it is today for `list_windows`.
- Every `subprocess.run` keeps an explicit `timeout=10` and tolerates `OSError`/`subprocess.SubprocessError`, matching `_is_running` today.
- Tests must never actually launch an app or kill a process: patch `os.startfile` and `subprocess.run`/`Popen`.

## Review Focus

Inputs the spec implies but no happy-path test exercises, most likely to bite first:

1. An alias the registry does not know (`"blender"`, a full `.exe` path) must still fall through to today's `os.startfile` + `.exe` retry behaviour, not raise — covered in Task 3.
2. A UWP app that is in the registry but not installed on this machine: `os.startfile` on its protocol raises `OSError`, and the user must get the honest "not installed / different name" message, not a false "Launched" — Task 3.
3. Verification races the app's startup: a GUI app is not in `tasklist` the instant `startfile` returns, so a verification that polls once will report a false negative on a slow-launching app — Task 3 pins the retry/grace behaviour.
4. `tasklist` memory strings are localised and formatted `"1,234 K"`, so a naive `int()` sort raises `ValueError` and would break `list_processes` entirely — Task 4.
5. Case and spacing in user phrasing (`"Camera"`, `"snipping  tool"`, `"VS Code"`) must resolve to the same entry — Task 2.

---

### Task 1: App registry data module

**Files:**
- Create: `app_registry.py`
- Test: `tests/test_app_registry.py`

**Interfaces:**
- Produces:
  - `AppEntry` dataclass with fields `name: str`, `aliases: tuple[str, ...]`, `launch: str`, `process: str | None`.
    `launch` is what `os.startfile` receives; `process` is the `tasklist` image name, `None` when the app has no stable single process.
  - `APPS: tuple[AppEntry, ...]` — the registry.

- [ ] **Step 1: Write the failing test**

```python
from app_registry import APPS, AppEntry


def test_every_entry_is_well_formed():
    for e in APPS:
        assert isinstance(e, AppEntry)
        assert e.name and e.launch
        assert e.name == e.name.lower(), f"{e.name} must be lowercase"
        for a in e.aliases:
            assert a == a.lower(), f"alias {a!r} must be lowercase"


def test_aliases_are_globally_unique():
    seen = {}
    for e in APPS:
        for key in (e.name, *e.aliases):
            assert key not in seen, f"{key!r} claimed by {seen.get(key)} and {e.name}"
            seen[key] = e.name


def test_uwp_apps_launch_by_protocol_not_appsfolder():
    # explorer.exe shell:appsFolder\... returned exit code 1 in production.
    for e in APPS:
        assert "appsfolder" not in e.launch.lower()


def test_camera_is_registered_with_its_real_process_image():
    cam = next(e for e in APPS if e.name == "camera")
    assert cam.launch == "microsoft.windows.camera:"
    assert cam.process == "WindowsCamera.exe"


def test_registry_covers_the_uwp_apps_the_spec_names():
    names = {e.name for e in APPS}
    for required in ("camera", "photos", "mail", "clock", "store", "maps",
                     "settings", "calculator", "phone link", "xbox",
                     "sound recorder", "snipping tool", "terminal"):
        assert required in names, f"missing {required}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_registry.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app_registry'`

- [ ] **Step 3: Implement `AppEntry` and `APPS` in `app_registry.py`**

Carry over every app currently in `app_map`, `process_map`, and `_RUNNING_NAMES` in `system_client.py` (firefox, chrome, edge, notepad, explorer, spotify, discord, steam, epic, vscode, terminal, cmd, powershell, task manager, word, excel, powerpoint, teams, slack, obs, vlc), then add the UWP entries. Use these launch protocols, which are the documented Windows shell URIs:

| name | launch | process |
|---|---|---|
| camera | `microsoft.windows.camera:` | `WindowsCamera.exe` |
| photos | `ms-photos:` | `Microsoft.Photos.exe` |
| mail | `outlookmail:` | `olk.exe` |
| calendar app | `outlookcal:` | `olk.exe` |
| clock | `ms-clock:` | `Time.exe` |
| store | `ms-windows-store:` | `WinStore.App.exe` |
| maps | `bingmaps:` | `Maps.exe` |
| settings | `ms-settings:` | `SystemSettings.exe` |
| phone link | `ms-phone:` | `PhoneExperienceHost.exe` |
| xbox | `ms-xbox:` | `XboxPcApp.exe` |
| sound recorder | `soundrecorder:` | `SoundRecorder.exe` |
| snipping tool | `ms-screensketch:` | `SnippingTool.exe` |
| calculator | `calculator:` | `CalculatorApp.exe` |

Give each a natural-language alias set (`"windows camera"`, `"webcam"` for camera; `"photo"`, `"gallery"` for photos; `"alarms"`, `"timer app"` for clock; `"microsoft store"` for store). Keep `cmd`/`powershell`/`terminal` launching as they do today.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app_registry.py tests/test_app_registry.py
git commit -m "feat: one app registry record per app, covering UWP apps"
```

---

### Task 2: Alias resolution

**Files:**
- Modify: `app_registry.py`
- Test: `tests/test_app_registry.py`

**Interfaces:**
- Consumes: `APPS`, `AppEntry` from Task 1.
- Produces: `resolve(target: str) -> AppEntry | None` — case-insensitive, whitespace-collapsing lookup by name or alias; `None` when unknown.

- [ ] **Step 1: Write the failing test**

```python
import pytest
from app_registry import resolve


@pytest.mark.parametrize("phrasing", ["camera", "Camera", "  CAMERA ", "windows camera"])
def test_resolve_is_case_and_space_insensitive(phrasing):
    assert resolve(phrasing).name == "camera"


def test_resolve_collapses_internal_whitespace():
    assert resolve("snipping  tool").name == "snipping tool"


def test_resolve_returns_none_for_an_unknown_app():
    assert resolve("blender") is None
    assert resolve("") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_app_registry.py -k resolve -v`
Expected: FAIL with `ImportError: cannot import name 'resolve'`

- [ ] **Step 3: Implement `resolve(target: str) -> AppEntry | None` in `app_registry.py`**

Build a single `dict[str, AppEntry]` index at import time from each entry's name and aliases. Normalise the key with `" ".join(target.lower().split())`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_app_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app_registry.py tests/test_app_registry.py
git commit -m "feat: case- and whitespace-insensitive app alias resolution"
```

---

### Task 3: `launch_app` and `close_app` use the registry, and launch verifies

**Files:**
- Modify: `system_client.py` — delete `app_map` (inside `launch_app`), `process_map` (inside `close_app`), and the `_RUNNING_NAMES` class attribute; rewrite `launch_app` and `close_app` to call `resolve`.
- Test: `tests/test_system_client.py`

**Interfaces:**
- Consumes: `resolve` from Task 2.
- Produces: `SystemClient.launch_app`, `.close_app` unchanged in signature. New private `SystemClient._verify_started(process: str, attempts: int = 6, delay: float = 0.5) -> bool`, polling `_is_running`.

- [ ] **Step 1: Write the failing test**

```python
import os
import subprocess
import pytest
from system_client import SystemClient


def _fake_startfile(monkeypatch, ok=()):
    """Record startfile targets; raise OSError for anything not in `ok`."""
    called = []

    def startfile(target):
        called.append(target)
        if ok and target not in ok:
            raise OSError("not found")

    monkeypatch.setattr(os, "startfile", startfile)
    return called


def test_launch_camera_uses_the_protocol_uri(monkeypatch):
    called = _fake_startfile(monkeypatch, ok=("microsoft.windows.camera:",))
    monkeypatch.setattr(SystemClient, "_verify_started", lambda self, p, **k: True)
    out = SystemClient().launch_app("camera")
    assert called == ["microsoft.windows.camera:"]
    assert "launch" in out.lower() and "could not" not in out.lower()


def test_launch_verifies_and_reports_truthfully_when_nothing_started(monkeypatch):
    # The regression: "Launched camera." while the app never came up.
    _fake_startfile(monkeypatch, ok=("microsoft.windows.camera:",))
    monkeypatch.setattr(SystemClient, "_verify_started", lambda self, p, **k: False)
    out = SystemClient().launch_app("camera")
    assert "could not" in out.lower() or "didn't" in out.lower()
    assert out.lower() != "launched camera."


def test_launch_reports_honestly_when_uwp_app_is_not_installed(monkeypatch):
    _fake_startfile(monkeypatch, ok=())          # every startfile raises
    out = SystemClient().launch_app("camera")
    assert "could not find" in out.lower()


def test_unknown_app_still_falls_through_to_startfile_and_exe_retry(monkeypatch):
    called = _fake_startfile(monkeypatch, ok=("blender.exe",))
    out = SystemClient().launch_app("blender")
    assert called == ["blender", "blender.exe"]
    assert "launch" in out.lower()


def test_verify_started_polls_until_the_app_appears(monkeypatch):
    # A GUI app is not in tasklist the instant startfile returns.
    seen = {"n": 0}

    def running(self, proc):
        seen["n"] += 1
        return seen["n"] >= 3

    monkeypatch.setattr(SystemClient, "_is_running", running)
    monkeypatch.setattr("time.sleep", lambda s: None)
    assert SystemClient()._verify_started("WindowsCamera.exe") is True
    assert seen["n"] == 3


def test_already_running_app_is_not_relaunched(monkeypatch):
    called = _fake_startfile(monkeypatch, ok=("microsoft.windows.camera:",))
    monkeypatch.setattr(SystemClient, "_is_running", lambda self, p: True)
    out = SystemClient().launch_app("camera")
    assert called == []
    assert "already running" in out.lower()


def test_close_camera_resolves_its_real_process_image(monkeypatch):
    # The regression: close_app("camera") tried camera.exe -> "not running".
    runs = []

    def fake_run(args, **kwargs):
        runs.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = SystemClient().close_app("camera")
    assert "WindowsCamera.exe" in runs[0]
    assert "closed" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_system_client.py -v`
Expected: FAIL — `_verify_started` does not exist; camera resolves to `camera`/`camera.exe`

- [ ] **Step 3: Rewrite `launch_app`, `close_app`, and add `_verify_started` in `system_client.py`**

`launch_app`: resolve the target. If resolved and `entry.process` is running, return "already running" (today's behaviour, now for every registry app). Keep the existing `_launches_claude` branch ahead of the startfile attempt. Launch `entry.launch` if resolved, else keep today's `[cmd, cmd + ".exe"]` candidate list for unknown targets. After a successful `startfile`, when `entry.process` is set, call `_verify_started` and return the honest message on failure. Preserve the existing "Could not find '{target}'…" wording when every candidate raises `OSError`.

`close_app`: resolve the name and use `entry.process`; fall back to today's `f"{name_lower}.exe"` when unresolved. Keep the three existing return strings.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_system_client.py tests/test_app_registry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add system_client.py tests/test_system_client.py
git commit -m "fix: launch/close resolve one registry record and verify startup"
```

---

### Task 4: `list_processes` sorts by memory as advertised

**Files:**
- Modify: `system_client.py:list_processes`
- Modify: `jarvisd/tools/system.py` — `list_processes` description, if its wording no longer matches
- Test: `tests/test_system_client.py`

**Interfaces:**
- Produces: `list_processes() -> str`, unchanged signature; output ordered by descending memory.

- [ ] **Step 1: Write the failing test**

```python
import subprocess
from system_client import SystemClient

_TASKLIST = (
    '"WindowsCamera.exe","1234","Console","1","52,000 K"\n'
    '"aaa.exe","1","Console","1","4 K"\n'
    '"bbb.exe","2","Console","1","8,192 K"\n'
)


def _fake_tasklist(monkeypatch, stdout=_TASKLIST):
    monkeypatch.setattr(
        subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout, ""),
    )


def test_list_processes_orders_by_memory_not_alphabetically(monkeypatch):
    # The regression: sorted(...)[:30] alphabetically meant a ~200-process
    # machine only ever returned A-C names, so WindowsCamera.exe was invisible.
    _fake_tasklist(monkeypatch)
    lines = SystemClient().list_processes().strip().split("\n")
    assert "WindowsCamera.exe" in lines[0]
    assert "aaa.exe" in lines[-1]


def test_list_processes_survives_unparseable_memory_values(monkeypatch):
    # tasklist is localised: "N/A", non-breaking spaces, other separators.
    _fake_tasklist(monkeypatch, '"x.exe","1","Console","1","N/A"\n' + _TASKLIST)
    out = SystemClient().list_processes()
    assert "x.exe" in out and "WindowsCamera.exe" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_system_client.py -k list_processes -v`
Expected: FAIL — `aaa.exe` sorts first alphabetically

- [ ] **Step 3: Rewrite `list_processes` in `system_client.py`**

Parse each memory string by stripping non-digits (`"".join(c for c in mem if c.isdigit())`) and treat an empty result as `0` so `"N/A"` sorts last without raising. Sort by that integer descending, take 30, keep the existing `f"- {name} ({mem})"` line format with the original display string.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_system_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add system_client.py jarvisd/tools/system.py tests/test_system_client.py
git commit -m "fix: list_processes returns the real top 30 by memory"
```

---

### Task 5: Tell the model the registry exists, and retire the stale memory

**Files:**
- Modify: `jarvisd/tools/system.py` — `launch_app` and `close_app` descriptions
- Modify: `jarvisd/core.py:73` — the PC-control `CAPABILITIES` line
- Test: `tests/test_tools_registry.py`

**Interfaces:**
- Consumes: `APPS` from Task 1.

- [ ] **Step 1: Write the failing test**

```python
from jarvisd.tools import system as system_tools


def _schema(name):
    return next(t for t in system_tools.TOOLS if t["name"] == name)


def test_launch_app_description_advertises_uwp_apps():
    desc = _schema("launch_app")["description"].lower()
    for app in ("camera", "photos", "settings", "store"):
        assert app in desc, f"{app} not advertised to the model"


def test_launch_app_description_does_not_send_the_model_to_run_command():
    desc = _schema("launch_app")["description"].lower()
    assert "run_command" not in desc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_tools_registry.py -k launch_app -v`
Expected: FAIL — `camera` is absent from the description

- [ ] **Step 3: Update the descriptions**

Add the UWP app names to `launch_app`'s and `close_app`'s `description` strings so the model knows to use them. In `jarvisd/core.py:73`, state that `launch_app` handles built-in Windows apps including Camera and that `run_command` must not be used to launch an app.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: PASS, no regressions

- [ ] **Step 5: Delete the stale remembered fact**

The memory row `"The Windows camera app can be launched using the command: explorer.exe shell:appsFolder\Microsoft.WindowsCamera_8wekyb3d8bbwe!App"` now pins Camera to a destructive, exit-1 path. Find it and remove it:

```bash
python -c "import sqlite3; c=sqlite3.connect('data/jarvis.db'); print([dict(r) for r in c.execute('select rowid,* from memories where content like \"%WindowsCamera%\"')])"
```

Delete the matching row (confirm the table name against `jarvisd/memory_store.py` first), then verify it is gone.

- [ ] **Step 6: Commit**

```bash
git add jarvisd/tools/system.py jarvisd/core.py tests/test_tools_registry.py
git commit -m "feat: advertise built-in Windows apps on launch_app"
```
