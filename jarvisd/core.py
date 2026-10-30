"""Claude-powered agent core: routing, cost tracking, tool-use loop."""

import logging
from datetime import datetime

import anthropic

from jarvisd.tools import build_registry
from router.classifier import IntentClassifier, Tier
from router.cost_tracker import CostTracker
from router.ollama_backend import OllamaBackend
from security.audit import AuditLogger
from security.guard import SecurityGuard, Verdict
from system_client import SystemClient

log = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5"
SONNET_MODEL = "claude-sonnet-5"

WEB_SEARCH_MAX_USES_PER_REQUEST = 3
PAUSE_TURN_MAX_CONTINUATIONS = 5

SYSTEM_PROMPT = """You are JARVIS, the user's personal AI assistant. You control their smart home, Spotify, Android TV, and PC.

Persona and voice — this matters as much as being correct:
- You speak out loud through a voice assistant. Everything you say is READ ALOUD, so talk the way people talk, not the way documents are written.
- You are concise, dry, and quietly competent, with a touch of understated wit — a capable aide who's comfortable with the user. Warm, never gushing. Address the user directly; never use "sir" or their name as a title.
- Hard rules for every reply:
  - Plain spoken sentences only. NO markdown, NO bullet points, NO numbered lists, NO headings, NO bold or italics, NO emoji.
  - Default to one or two sentences. Only go longer when the user explicitly asks for detail.
  - Use contractions. Get to the point; skip preamble like "Sure, I can help with that!".
  - If you must convey several items, say them in a sentence ("You've got three: eggs, a run, and the dentist"), never as a list.
  - When you're unsure what the user meant, ask about the single most likely interpretation as a short question — don't enumerate every possibility.
  - If your previous reply was cut off (you'll see a note saying so), the user interrupted to correct or redirect you. Adjust to what they now want; don't repeat what you were saying.

Capabilities:
- Smart home (Home Assistant): lights, switches, climate, sensors, notifications
- Spotify: search, play on any device, control playback
- Android TV: launch/navigate apps, type in search fields, remote control
- PC control: launch/close apps, list processes, open URLs, system info
- Weather: get_weather gives current conditions and today's forecast for the user's location (known automatically) or any place they name. Use it for weather — do NOT web-search weather.
- Web search: current information from the web, with citations. Each search costs money — only search when the answer needs current information.
- Habit suggestions: when the user asks about suggestions, use list_proposals; accept_proposal/dismiss_proposal act on them.

Security policy (strictly enforced):
- NO access to bank accounts, cards, financial services, or sensitive IDs
- Banking/payment/trading URLs are blocked
- Destructive actions require user confirmation

How you work:
- Whether an app, device, or track is currently open, running, or playing is live state that changes on its own — you cannot know it from memory. To report or act on it, call the relevant tool to check or do it; never assume it from earlier in the conversation, because what you did before may no longer be true.
- Confirm actions briefly after doing them ("Done — lights are off.").
- Chain multiple actions when it makes sense (e.g. "movie night" = dim the lights and open Netflix).
- Spotify: search for the URI first, then play. Target a specific device only if asked.
- TV: launch the app first, then open its search, then type the query.
- "Give me my briefing" = weather (get_weather) plus top headlines (web search), today's reminders (list_reminders), and today's meals (meal_summary), delivered as a short spoken rundown.
"""

INTERRUPTION_NOTE = "(The user interrupted and cut off this reply before I finished.)"


def _find_last_assistant_turn(conversation: list[dict]) -> dict | None:
    """Return the most recent assistant-role turn in `conversation`, or None.

    Shared by Assistant.note_interruption()'s no-arg walk and by
    JarvisServer's barge-in capture-at-receipt logic (see
    JarvisServer._capture_interrupt_target), so "what counts as the current
    last assistant turn" is defined in exactly one place.
    """
    for turn in reversed(conversation):
        if turn["role"] == "assistant":
            return turn
    return None


def _mark_turn_interrupted(turn: dict) -> None:
    """Append the INTERRUPTION_NOTE marker onto a single assistant turn dict.

    Factored out of note_interruption() so its content-shape handling
    (str vs. SDK content-block list) and idempotency live in exactly one
    place, whether the turn was found by walking self.conversation or passed
    in explicitly by the caller.
    """
    content = turn["content"]
    if isinstance(content, str):
        if INTERRUPTION_NOTE in content:
            return  # already marked
        turn["content"] = content.rstrip() + " " + INTERRUPTION_NOTE
    elif isinstance(content, list):
        if any(
            isinstance(block, dict) and block.get("type") == "text" and block.get("text") == INTERRUPTION_NOTE
            for block in content
        ):
            return  # already marked
        content.append({"type": "text", "text": INTERRUPTION_NOTE})


def web_search_tool(model: str) -> dict:
    """Newest server-side web_search variant the model supports (spec §4).

    claude-sonnet-5 supports web_search_20260209 (dynamic filtering);
    claude-haiku-4-5 only supports the basic web_search_20250305.
    """
    variant = "web_search_20260209" if model == SONNET_MODEL else "web_search_20250305"
    return {"type": variant, "name": "web_search", "max_uses": WEB_SEARCH_MAX_USES_PER_REQUEST}


class Assistant:

    MAX_HISTORY = 40

    def __init__(
        self,
        ha_client,
        anthropic_api_key: str,
        spotify_client=None,
        tv_client=None,
        system_client=None,
        monthly_budget: float = 20.0,
        ollama_model: str = "qwen2.5:3b-instruct-q4_K_M",
        profile_store=None,
        memory_store=None,
        reminder_store=None,
        rules_store=None,
        enable_web_search: bool = False,
        search_monthly_cap: int = 100,
        meal_store=None,
        file_tools: bool = False,
        proposal_store=None,
        location_provider=None,
    ):
        spotify = None
        if spotify_client is not None and getattr(spotify_client, "is_authenticated", lambda: True)():
            spotify = spotify_client

        self.client = anthropic.Anthropic(api_key=anthropic_api_key)
        self.conversation: list[dict] = []
        self.guard = SecurityGuard()
        self.audit = AuditLogger()
        self.classifier = IntentClassifier()
        self.cost_tracker = CostTracker(monthly_budget=monthly_budget, search_monthly_cap=search_monthly_cap)
        self.enable_web_search = enable_web_search
        self.ollama = OllamaBackend(model=ollama_model)
        self.profile_store = profile_store
        self.memory_store = memory_store
        self.location_provider = location_provider

        self.tools, self._handlers = build_registry(
            ha_client,
            system_client if system_client is not None else SystemClient(),
            spotify_client=spotify,
            tv_client=tv_client,
            profile_store=profile_store,
            memory_store=memory_store,
            reminder_store=reminder_store,
            rules_store=rules_store,
            meal_store=meal_store,
            file_tools=file_tools,
            file_summarizer=self._summarize if file_tools else None,
            proposal_store=proposal_store,
            location_provider=location_provider,
        )
        if self.tools:
            self.tools[-1] = {**self.tools[-1], "cache_control": {"type": "ephemeral"}}

        if self.memory_store is not None:
            self.conversation = self.memory_store.load_history(max_turns=10)
            if self.conversation:
                log.info("Restored %d turns of conversation history", len(self.conversation))

    def _summarize(self, text: str) -> str:
        """Condense a long document: free local Ollama when up, else one Haiku call.

        Used as the file tools' summarizer (jarvisd/tools/files.py, via
        build_registry's file_summarizer param) to shrink documents too long to
        return in full. Implemented as an Assistant METHOD rather than a
        bootstrap closure over a fully-built `ai` object: build_registry runs
        here inside __init__, before an `ai` instance exists to close over, so
        a closure captured in bootstrap.create_assistant() would either be
        unusable at build_registry time or reference a not-yet-existing name.
        A bound method has no such ordering problem -- self.ollama/self.client/
        self.cost_tracker only need to exist by the time this is *called*, not
        by the time it is *referenced* when passed into build_registry.
        """
        prompt = "Summarize the key points of this document in <=300 words:\n\n" + text
        if self.ollama.is_available():
            try:
                return self.ollama.generate(prompt)
            except Exception:
                pass
        resp = self.client.messages.create(
            model=HAIKU_MODEL, max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        u = resp.usage
        self.cost_tracker.record(HAIKU_MODEL, u.input_tokens, u.output_tokens, source="summarize")
        return "".join(b.text for b in resp.content if b.type == "text")

    def _build_system(self) -> list[dict]:
        """Assemble the cached system-prompt blocks: SYSTEM_PROMPT, then profile (if any)."""
        blocks = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
        if self.profile_store is not None:
            profile = self.profile_store.load()
            if profile.strip():
                blocks.append({
                    "type": "text",
                    "text": f"# What you know about the user\n\n{profile}",
                    "cache_control": {"type": "ephemeral"},
                })
        # Current local time — appended AFTER the last cached block and WITHOUT
        # cache_control, so the model can resolve relative times ("in 2 minutes",
        # "tomorrow at 8am") while the cached SYSTEM_PROMPT+profile prefix stays a
        # cache hit (a value that changes every call must never carry cache_control).
        now = datetime.now().astimezone()
        blocks.append({
            "type": "text",
            "text": (
                f"The current local date and time is {now:%A, %Y-%m-%d %H:%M %Z}. "
                "Use this to resolve relative times (e.g. 'in 2 minutes', 'in an hour', "
                "'tomorrow at 8am') into absolute times when setting reminders."
            ),
        })
        # Current location (uncached, like the time block — it can change on a
        # laptop). Lets the model answer weather / time-zone / "near me" questions
        # without asking the user where they are.
        if self.location_provider is not None:
            loc = self.location_provider.current()
            if loc is not None:
                where = loc.place or f"{loc.latitude:.3f}, {loc.longitude:.3f}"
                blocks.append({
                    "type": "text",
                    "text": (
                        f"The user is currently in {where} "
                        f"(latitude {loc.latitude:.4f}, longitude {loc.longitude:.4f}). "
                        "Use this for weather, time zones, travel, and 'near me' questions "
                        "instead of asking where they are."
                    ),
                })
        return blocks

    def _record_turn(self, role: str, text: str) -> None:
        """Persist a turn to episodic memory; never let a persistence failure break the reply."""
        if self.memory_store is None:
            return
        try:
            self.memory_store.record(role, text)
        except Exception as e:
            log.warning("memory_store.record failed: %s", e)

    def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        handler = self._handlers.get(tool_name)
        if handler is None:
            return f"Unknown tool: {tool_name}"
        try:
            return handler(tool_input)
        except Exception as e:
            return f"Error executing {tool_name}: {e}"

    def execute_guarded(self, tool_name: str, tool_input: dict, autonomous: bool = False) -> str:
        """Run one tool call through the security guard + audit choke point.

        This is the ONLY path any caller should use to execute a tool call —
        the Claude tool-use loop (`_claude_loop`) and the HA event listener's
        direct rules both funnel through here so guard enforcement and audit
        logging can never be bypassed or duplicated.

        autonomous=True means no user initiated this call (HA trigger rules):
        the guard then treats destructive tools as terminally unconfirmable
        instead of playing the retry-confirm dance (see SecurityGuard.check).
        """
        check = self.guard.check(tool_name, tool_input, autonomous=autonomous)

        if check.verdict == Verdict.BLOCK:
            result = f"SECURITY BLOCK: {check.reason}"
            self.audit.log(tool_name, tool_input, "blocked", check.reason)
        elif check.verdict == Verdict.CONFIRM_REQUIRED:
            result = f"CONFIRMATION REQUIRED: {check.reason}"
            self.audit.log(tool_name, tool_input, "confirm_required", check.reason)
        else:
            result = self._execute_tool(tool_name, tool_input)
            self.audit.log(tool_name, tool_input, "allowed", check.reason, result)

        return result

    def process(self, user_message: str, autonomous: bool = False) -> str:
        """Classify intent, route to cheapest capable tier, return response.

        autonomous=True flags a machine-initiated turn (smart trigger rules):
        it flows down to execute_guarded so destructive tools can never be
        confirmed-by-retry without a user present.
        """
        # Mark a fresh user-turn boundary so a destructive-tool confirmation
        # armed on a previous turn becomes consumable, while the model retrying
        # within THIS turn's agentic loop cannot self-confirm (see guard.check).
        self.guard.begin_turn()

        tier = self.classifier.classify(user_message)
        self._record_turn("user", user_message)

        if self.cost_tracker.should_force_downgrade() and tier == Tier.SONNET:
            log.warning("Budget near limit — downgrading Sonnet to Haiku")
            tier = Tier.HAIKU

        if tier == Tier.LOCAL:
            resp = self._handle_local(user_message)
            if resp is not None:
                self.conversation.append({"role": "user", "content": user_message})
                self.conversation.append({"role": "assistant", "content": resp})
                self._record_turn("assistant", resp)
                return resp
            tier = Tier.HAIKU

        model = HAIKU_MODEL if tier == Tier.HAIKU else SONNET_MODEL
        log.info("Routing to %s", model)
        reply = self._claude_loop(user_message, model, autonomous=autonomous)
        self._record_turn("assistant", reply)
        return reply

    def _handle_local(self, text: str) -> str | None:
        """Handle simple messages without an API call."""
        t = text.lower().strip().rstrip("!?.")

        if t in ("hey", "hi", "hello", "howdy", "yo", "sup"):
            return "Hey! What can I do for you?"
        if t in ("thanks", "thank you", "cheers", "thanks a lot", "thanks so much"):
            return "You're welcome!"
        if t in ("bye", "goodbye", "see ya", "later"):
            return "Goodbye! Let me know if you need anything."
        if t.startswith("good morning"):
            return "Good morning! How can I help today?"
        if t.startswith("good night") or t == "goodnight":
            return "Goodnight! Sleep well."
        if t in ("ok", "ok cool", "cool", "got it", "alright", "sure", "nice", "np", "no problem"):
            return "Let me know if you need anything else!"

        if self.ollama.is_available():
            try:
                return self.ollama.generate(text)
            except Exception as e:
                log.warning("Ollama failed, escalating: %s", e)

        return None

    def _claude_loop(self, user_message: str, model: str, autonomous: bool = False) -> str:
        """Agentic tool-use loop with Claude."""
        self.conversation.append({"role": "user", "content": user_message})
        self._compact_history()

        pause_continuations = 0

        while True:
            tools = self.tools
            if self.enable_web_search and self.cost_tracker.web_search_allowed():
                # Appended AFTER self.tools[-1] (the cached breakpoint) and never
                # carries cache_control itself, so dropping it at the cap never
                # invalidates the cached tools prefix.
                tools = self.tools + [web_search_tool(model)]

            response = self.client.messages.create(
                model=model,
                max_tokens=1024,
                system=self._build_system(),
                tools=tools,
                messages=self.conversation,
            )

            u = response.usage
            srv = getattr(u, "server_tool_use", None)
            searches = (getattr(srv, "web_search_requests", 0) or 0) if srv else 0
            self.cost_tracker.record(
                model, u.input_tokens, u.output_tokens,
                cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_creation_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                web_search_requests=searches,
            )

            assistant_content = response.content
            self.conversation.append({"role": "assistant", "content": assistant_content})

            tool_use_blocks = [b for b in assistant_content if b.type == "tool_use"]

            if response.stop_reason == "pause_turn" and not tool_use_blocks:
                # Server-side tool (web_search) loop paused mid-turn; re-request
                # WITHOUT adding a user message and the API resumes automatically.
                # If local tool_use blocks are present we must NOT take this
                # shortcut: skipping them would bypass execute_guarded and leave
                # dangling tool_use in the conversation (API 400) — fall through
                # to the normal execute-and-append path (the loop re-requests anyway).
                pause_continuations += 1
                if pause_continuations > PAUSE_TURN_MAX_CONTINUATIONS:
                    text_parts = [b.text for b in assistant_content if b.type == "text"]
                    return "\n".join(text_parts) if text_parts else "(web search did not complete)"
                continue

            if not tool_use_blocks:
                text_parts = [b.text for b in assistant_content if b.type == "text"]
                reply = "\n".join(p for p in text_parts if p).strip()
                return reply or "Done."

            tool_results = []
            for tool_block in tool_use_blocks:
                result = self.execute_guarded(tool_block.name, tool_block.input, autonomous=autonomous)

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block.id,
                    "content": result,
                })

            self.conversation.append({"role": "user", "content": tool_results})

    def _compact_history(self):
        if len(self.conversation) > self.MAX_HISTORY:
            self.conversation = self.conversation[-self.MAX_HISTORY:]

    def reset_conversation(self):
        """Clear conversation history."""
        self.conversation = []

    def note_interruption(self, turn: dict | None = None) -> None:
        """Mark an assistant reply as cut off by the user.

        Called when the user barged in (said the wake word) mid-reply. The
        marker lives in conversation history so the model's next completion
        knows its last reply wasn't fully heard and the user's new message is
        likely a correction or redirect.

        Two calling conventions:
        - No `turn` (or `turn=None`, indistinguishable in Python): walk
          self.conversation from the end and mark the most recent
          assistant-role turn -- the original behavior. No-op if there's no
          assistant turn.
        - Explicit `turn` (a dict): mark that exact turn instead of searching.
          Used by the server, which captures the target turn at interrupt
          FRAME RECEIPT rather than at consume time (assistant.conversation is
          shared/global -- an HA-rule action or another connected client can
          append new assistant turns in the window before the barge-in audio
          arrives, and marking whatever is then-newest would mark the wrong
          reply). The passed turn may no longer be self.conversation's most
          recent assistant turn, or may even have been compacted out of
          self.conversation entirely by _compact_history -- marking it is
          still safe, just a harmless no-op on the live history in that case,
          since it's a plain dict the caller already holds a reference to.

        Note: the case where the server captured NO assistant turn at
        interrupt receipt (nothing to mark) is handled by the caller skipping
        the call to this method entirely, not by passing turn=None -- since
        turn=None here just triggers the walk above, same as calling with no
        argument at all. See JarvisServer._process.

        Handles both content shapes an assistant turn can have:
        - fast/local path: `content` is a plain str -> append the note as text.
        - Claude path: `content` is a list of SDK content blocks -> APPEND a new
          text block rather than mutating an existing block in place (SDK
          blocks are pydantic models; mutating one in place is not the same as
          replacing it, and this also means a turn with only tool_use blocks
          and no text block is handled safely -- the note is just added as its
          own block, nothing existing needs to be found or touched).

        Idempotent: calling it twice on the same turn leaves the turn unchanged
        after the first call.
        """
        if turn is None:
            turn = _find_last_assistant_turn(self.conversation)
        if turn is None:
            return
        _mark_turn_interrupted(turn)
