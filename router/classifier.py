"""Intent classifier — routes requests to the cheapest capable model tier."""

import re
from enum import Enum


class Tier(str, Enum):
    LOCAL = "local"
    HAIKU = "haiku"
    SONNET = "sonnet"


class IntentClassifier:

    GREETINGS = re.compile(
        r"^(hey|hi|hello|howdy|yo|sup"
        r"|good\s*(morning|evening|night|afternoon)"
        r"|what'?s?\s*up"
        r"|thanks?(\s*(you|a lot|so much))?"
        r"|bye|goodbye|goodnight|see\s*ya|later|cheers"
        r"|ok|okay|cool|got\s*it|alright|sure|nice|np|no\s*problem"
        r")\s*[.!?]*$",
        re.IGNORECASE,
    )

    ACTION_VERBS = re.compile(
        r"^(turn|switch|open|close|launch|start|kill|stop|play|pause|resume|"
        r"skip|set|dim|brighten|mute|unmute|search|find|list|show|check|send)\b",
        re.IGNORECASE,
    )

    SONNET_PATTERNS = [
        re.compile(r"\b(and\s+then|and\s+also|then\s+also)\b", re.IGNORECASE),
        re.compile(r"\bwhile\s+(also|you('re|\s+are))\b", re.IGNORECASE),
        re.compile(r"\bif\b.{5,}\bthen\b", re.IGNORECASE),
        re.compile(
            r"\b(explain|analyze|compare|summarize|write\s+me|create|design|plan|debug)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(movie\s*night|bedtime|morning\s*routine|set\s*(it\s+)?up)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(why|how\s+does|how\s+do\s+you|what\s+would)\b.*\?",
            re.IGNORECASE,
        ),
    ]

    def classify(self, text: str) -> Tier:
        stripped = text.strip()

        if self.GREETINGS.match(stripped):
            return Tier.LOCAL

        words = stripped.split()

        if len(words) <= 3 and "?" not in stripped and not self.ACTION_VERBS.search(stripped):
            return Tier.LOCAL

        sonnet_score = sum(1 for p in self.SONNET_PATTERNS if p.search(stripped))
        if sonnet_score >= 2 or len(words) > 40:
            return Tier.SONNET

        return Tier.HAIKU
