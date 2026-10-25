"""Google client — shared OAuth for Gmail (read + draft) and Calendar (read/write).

One authorized credential backs both services. Scopes are deliberately scoped
to what JARVIS needs and no more:
  - gmail.readonly   : search + read messages
  - gmail.compose    : create drafts (CANNOT send — no gmail.send scope)
  - calendar         : read + create/delete events

The OAuth token is cached at data/google_token.json (gitignored, like the
Spotify token). First run opens a browser once for consent; afterwards the
cached refresh token is reused and silently refreshed.

google_* packages are imported lazily so importing this module never hard-fails
on a machine that hasn't installed them (bootstrap only constructs GoogleClient
when GOOGLE_CREDENTIALS_FILE is set).
"""

import base64
import os
from datetime import datetime
from email.mime.text import MIMEText

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar",
]

DEFAULT_TOKEN_PATH = os.path.join("data", "google_token.json")


class GoogleClient:
    def __init__(self, credentials_file: str, token_file: str = DEFAULT_TOKEN_PATH):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._creds = None
        self._gmail = None
        self._calendar = None

    # -- auth / service construction --

    def _authorize(self):
        if self._creds is not None:
            return self._creds
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if os.path.exists(self.token_file):
            creds = Credentials.from_authorized_user_file(self.token_file, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.credentials_file, SCOPES)
                creds = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(self.token_file) or ".", exist_ok=True)
            with open(self.token_file, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        self._creds = creds
        return creds

    def gmail_service(self):
        if self._gmail is None:
            from googleapiclient.discovery import build
            self._gmail = build("gmail", "v1", credentials=self._authorize(), cache_discovery=False)
        return self._gmail

    def calendar_service(self):
        if self._calendar is None:
            from googleapiclient.discovery import build
            self._calendar = build("calendar", "v3", credentials=self._authorize(), cache_discovery=False)
        return self._calendar

    def ensure_authorized(self):
        """Force the OAuth flow now (used at startup so the browser prompt
        happens once, not mid-conversation)."""
        self._authorize()

    # -- Gmail --

    def search_messages(self, query: str, max_results: int = 10):
        svc = self.gmail_service()
        resp = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        out = []
        for ref in resp.get("messages", []):
            msg = svc.users().messages().get(
                userId="me", id=ref["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            out.append({
                "id": ref["id"],
                "from": headers.get("From", "?"),
                "subject": headers.get("Subject", "(no subject)"),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })
        return out

    def get_message(self, message_id: str):
        svc = self.gmail_service()
        msg = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        return {
            "from": headers.get("From", "?"),
            "to": headers.get("To", "?"),
            "date": headers.get("Date", "?"),
            "subject": headers.get("Subject", "(no subject)"),
            "body": _extract_body(payload),
        }

    def create_draft(self, to: str, subject: str, body: str):
        svc = self.gmail_service()
        mime = MIMEText(body)
        mime["to"] = to
        mime["subject"] = subject
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        draft = svc.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        return draft.get("id")

    # -- Calendar --

    def list_events(self, time_min: str, time_max: str, max_results: int = 10):
        svc = self.calendar_service()
        resp = svc.events().list(
            calendarId="primary",
            timeMin=_rfc3339(time_min),
            timeMax=_rfc3339(time_max),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        ).execute()
        out = []
        for e in resp.get("items", []):
            start = e.get("start", {})
            end = e.get("end", {})
            out.append({
                "id": e.get("id"),
                "summary": e.get("summary", "(untitled)"),
                "start": start.get("dateTime") or start.get("date", "?"),
                "end": end.get("dateTime") or end.get("date", "?"),
                "location": e.get("location", ""),
            })
        return out

    def create_event(self, summary, start, end, description=None, location=None):
        svc = self.calendar_service()
        body = {
            "summary": summary,
            "start": {"dateTime": _rfc3339(start)},
            "end": {"dateTime": _rfc3339(end)},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        ev = svc.events().insert(calendarId="primary", body=body).execute()
        return {"id": ev.get("id"), "link": ev.get("htmlLink")}

    def delete_event(self, event_id: str):
        svc = self.calendar_service()
        svc.events().delete(calendarId="primary", eventId=event_id).execute()


def _rfc3339(iso: str) -> str:
    """Attach the local timezone offset to a naive ISO datetime so Google's
    APIs accept it (they require RFC3339 with a timezone). Already-qualified
    strings (with 'Z' or a +hh:mm offset) are passed through."""
    if iso.endswith("Z") or "+" in iso[10:] or iso[10:].count("-") > 0:
        return iso
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach local tz
    return dt.isoformat()


def _extract_body(payload) -> str:
    """Pull text/plain (falling back to text/html stripped-ish) from a Gmail
    message payload, walking multipart trees."""
    def walk(part):
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime == "text/plain" and data:
            return _b64(data)
        for sub in part.get("parts", []) or []:
            found = walk(sub)
            if found:
                return found
        if mime == "text/html" and data:
            return _b64(data)
        return ""
    return walk(payload).strip()


def _b64(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""
