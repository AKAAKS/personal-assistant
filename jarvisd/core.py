"""Claude-powered agent core: routing, cost tracking, tool-use loop."""

import logging

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

SYSTEM_PROMPT = """You are JARVIS, a personal AI assistant. You control the user's smart home, Spotify, Android TV, and PC.

Capabilities:
- **Smart home** (Home Assistant): lights, switches, climate, sensors, notifications
- **Spotify**: search, play on any device, control playback
- **Android TV**: launch/navigate apps, type in search fields, remote control
- **PC control**: launch/close apps, list processes, open URLs, system info
- **Web search**: current information from the web, with citations. Each search costs money — search only when the answer needs current information.

Security policy (strictly enforced):
- NO access to bank accounts, cards, financial services, or sensitive IDs
- Banking/payment/trading URLs are blocked
- Destructive actions require user confirmation

Guidelines:
- Be conversational but concise. Confirm actions after performing them.
- Chain multiple actions when appropriate (e.g. "movie night" = dim lights + open Netflix).
- Spotify: search first for URIs, then play. Target specific devices if asked.
- TV: launch app first, then navigate to search, then type query.
- "Give me my briefing" = weather + top headlines (web search) + today's reminders (list_reminders) + today's meals (meal_summary).
"""


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
                return "\n".join(text_parts)

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
