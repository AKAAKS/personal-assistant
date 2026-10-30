"""Gmail tool handlers — read + draft only (never sends)."""

from jarvisd.tools.gmail import handlers


class FakeGoogle:
    """Minimal GoogleClient stand-in for the Gmail surface."""

    def __init__(self, messages=None, bodies=None):
        self._messages = messages or []
        self._bodies = bodies or {}
        self.drafts = []
        self.sent = []  # must stay empty — read+draft only

    def search_messages(self, query, max_results=10):
        return list(self._messages)

    def get_message(self, message_id):
        return self._bodies.get(message_id)

    def create_draft(self, to, subject, body):
        self.drafts.append({"to": to, "subject": subject, "body": body})
        return "draft_1"


def test_gmail_search_formats_results():
    g = FakeGoogle(messages=[
        {"id": "m1", "from": "Alice <a@x.com>", "subject": "Lunch?", "snippet": "want to grab lunch", "date": "Mon"},
        {"id": "m2", "from": "Bob <b@x.com>", "subject": "Invoice", "snippet": "attached", "date": "Tue"},
    ])
    out = handlers(g)["gmail_search"]({"query": "is:unread"})
    assert "Lunch?" in out and "Invoice" in out
    assert "m1" in out and "m2" in out  # ids present so the model can read one


def test_gmail_search_handles_no_results():
    out = handlers(FakeGoogle())["gmail_search"]({"query": "nothing"})
    assert "no" in out.lower()


def test_gmail_read_returns_full_message():
    g = FakeGoogle(bodies={"m1": {
        "from": "Alice <a@x.com>", "to": "me@x.com", "subject": "Lunch?",
        "date": "Mon", "body": "Do you want to grab lunch tomorrow?",
    }})
    out = handlers(g)["gmail_read"]({"message_id": "m1"})
    assert "grab lunch tomorrow" in out
    assert "Alice" in out


def test_gmail_read_unknown_id_is_clear():
    out = handlers(FakeGoogle())["gmail_read"]({"message_id": "nope"})
    assert "not" in out.lower() or "error" in out.lower()


def test_gmail_draft_creates_draft_and_never_sends():
    g = FakeGoogle()
    out = handlers(g)["gmail_draft"]({
        "to": "alice@x.com", "subject": "Re: Lunch?", "body": "Sure, noon works.",
    })
    assert g.drafts == [{"to": "alice@x.com", "subject": "Re: Lunch?", "body": "Sure, noon works."}]
    assert g.sent == []                       # the whole point: nothing is sent
    assert "draft" in out.lower()
    assert "not" in out.lower() and "sent" in out.lower()  # tells the user it wasn't sent
