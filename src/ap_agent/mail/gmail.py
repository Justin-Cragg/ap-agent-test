"""Gmail adapter (Gmail REST API).

Auth, either:
* Google Workspace service account with domain-wide delegation for the
  ``gmail.modify`` scope (GMAIL_SERVICE_ACCOUNT_JSON + GMAIL_USER), or
* an OAuth client + refresh token for the AP mailbox
  (GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET / GMAIL_REFRESH_TOKEN).
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from email.message import EmailMessage as MIMEMessage
from email.utils import getaddresses, parseaddr
from typing import Any, Callable

import httpx

from ap_agent.mail.base import Attachment, EmailMessage, MailboxConnector, PROCESSED_LABEL, dmarc_pass

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
API = "https://gmail.googleapis.com/gmail/v1/users"


def google_token_provider(service_account_json: str, user: str, client_id: str, client_secret: str, refresh_token: str) -> Callable[[], str]:
    from google.auth.transport.requests import Request

    if service_account_json:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(json.loads(service_account_json), scopes=SCOPES, subject=user)
    else:
        from google.oauth2.credentials import Credentials

        creds = Credentials(None, refresh_token=refresh_token, client_id=client_id, client_secret=client_secret, token_uri="https://oauth2.googleapis.com/token", scopes=SCOPES)

    def token() -> str:
        if not creds.valid:
            creds.refresh(Request())
        return creds.token

    return token


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


class GmailMailbox(MailboxConnector):
    name = "gmail"

    def __init__(self, user: str, token: Callable[[], str], transport: httpx.BaseTransport | None = None):
        self.user = self.address = user
        self._token = token
        self.http = httpx.Client(base_url=f"{API}/{user}", timeout=60, transport=transport)
        self._labels: dict[str, str] = {}

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        resp = self.http.request(method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _label_id(self, name: str) -> str:
        if name not in self._labels:
            for lab in self._req("GET", "/labels").get("labels", []):
                self._labels[lab["name"]] = lab["id"]
        if name not in self._labels:
            self._labels[name] = self._req("POST", "/labels", json={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"})["id"]
        return self._labels[name]

    def list_new_messages(self, lookback_days: int = 7, limit: int = 50) -> list[str]:
        q = f"in:inbox -label:{PROCESSED_LABEL} newer_than:{lookback_days}d"
        ids: list[str] = []
        page_token = None
        while len(ids) < limit:
            params = {"q": q, "maxResults": min(100, limit - len(ids))}
            if page_token:
                params["pageToken"] = page_token
            data = self._req("GET", "/messages", params=params)
            ids += [ref["id"] for ref in data.get("messages", [])]
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_message(self, message_id: str) -> EmailMessage:
        return self._load(message_id)

    def _load(self, msg_id: str) -> EmailMessage:
        msg = self._req("GET", f"/messages/{msg_id}", params={"format": "full"})
        raw_headers = msg["payload"].get("headers", [])
        headers = {h["name"].lower(): h["value"] for h in raw_headers}
        auth_results = [h["value"] for h in raw_headers if h["name"].lower() == "authentication-results"]
        text_parts: list[str] = []
        attachments: list[Attachment] = []

        def walk(part: dict) -> None:
            mime = part.get("mimeType", "")
            body = part.get("body", {})
            if part.get("filename") and (body.get("attachmentId") or body.get("data")):
                raw = body.get("data") or self._req("GET", f"/messages/{msg_id}/attachments/{body['attachmentId']}")["data"]
                attachments.append(Attachment(id=body.get("attachmentId", part["partId"]), filename=part["filename"], content_type=mime, data=_b64url_decode(raw)))
            elif mime == "text/plain" and body.get("data"):
                text_parts.append(_b64url_decode(body["data"]).decode("utf-8", "replace"))
            for child in part.get("parts", []) or []:
                walk(child)

        walk(msg["payload"])
        sender = parseaddr(headers.get("from", ""))[1]
        return EmailMessage(
            id=msg["id"],
            thread_id=msg.get("threadId"),
            sender=sender,
            sender_authenticated=dmarc_pass(auth_results, sender.rsplit("@", 1)[-1], ("mx.google.com",)),
            mailbox=self.user,
            to=[a for _, a in getaddresses([headers.get("to", "")]) if a],
            cc=[a for _, a in getaddresses([headers.get("cc", "")]) if a],
            subject=headers.get("subject", ""),
            body_text="\n".join(text_parts) or msg.get("snippet", ""),
            received_at=datetime.fromtimestamp(int(msg.get("internalDate", 0)) / 1000, tz=timezone.utc),
            attachments=attachments,
        )

    def _raw(self, to: list[str], subject: str, body: str, in_reply_to: str | None = None) -> str:
        mime = MIMEMessage()
        mime["To"] = ", ".join(to)
        mime["From"] = self.user
        mime["Subject"] = subject
        if in_reply_to:
            mime["In-Reply-To"] = in_reply_to
            mime["References"] = in_reply_to
        mime.set_content(body)
        return base64.urlsafe_b64encode(mime.as_bytes()).decode()

    def reply(self, message: EmailMessage, body: str, *, send: bool) -> str:
        meta = self._req("GET", f"/messages/{message.id}", params={"format": "metadata", "metadataHeaders": "Message-ID"})
        msg_header = next((h["value"] for h in meta["payload"].get("headers", []) if h["name"].lower() == "message-id"), None)
        subject = message.subject if message.subject.lower().startswith("re:") else f"Re: {message.subject}"
        payload = {"raw": self._raw([message.sender], subject, body, msg_header), "threadId": message.thread_id}
        if send:
            return self._req("POST", "/messages/send", json=payload)["id"]
        return self._req("POST", "/drafts", json={"message": payload})["id"]

    def send(self, to: list[str], subject: str, body: str) -> str:
        return self._req("POST", "/messages/send", json={"raw": self._raw(to, subject, body)})["id"]

    def label(self, message: EmailMessage | str, label: str) -> None:
        mid = message if isinstance(message, str) else message.id
        self._req("POST", f"/messages/{mid}/modify", json={"addLabelIds": [self._label_id(label)]})
