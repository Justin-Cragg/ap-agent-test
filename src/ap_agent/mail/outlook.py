"""Outlook / Exchange Online adapter (Microsoft Graph).

Auth: Entra ID app registration with application permissions ``Mail.ReadWrite``
and ``Mail.Send``. Restrict it to the AP mailbox with an Exchange
application access policy (or RBAC for Applications) so the app can't read
other mailboxes.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import httpx

from ap_agent.mail.base import Attachment, EmailMessage, MailboxConnector, PROCESSED_LABEL, dmarc_pass

GRAPH = "https://graph.microsoft.com/v1.0"


def graph_token_provider(tenant_id: str, client_id: str, client_secret: str) -> Callable[[], str]:
    import msal

    app = msal.ConfidentialClientApplication(client_id, authority=f"https://login.microsoftonline.com/{tenant_id}", client_credential=client_secret)

    def token() -> str:
        result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise RuntimeError(f"Graph auth failed: {result.get('error_description')}")
        return result["access_token"]

    return token


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    return re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", "", text)).strip()


class OutlookMailbox(MailboxConnector):
    name = "outlook"

    def __init__(self, mailbox: str, token: Callable[[], str], transport: httpx.BaseTransport | None = None):
        self.mailbox = self.address = mailbox
        self._token = token
        self.http = httpx.Client(base_url=f"{GRAPH}/users/{mailbox}", timeout=60, transport=transport)

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        resp = self.http.request(method, url, headers={"Authorization": f"Bearer {self._token()}"}, **kw)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def list_new_messages(self, lookback_days: int = 7, limit: int = 50) -> list[str]:
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        url: str | None = "/mailFolders/inbox/messages"
        params: dict | None = {"$filter": f"receivedDateTime ge {since}", "$orderby": "receivedDateTime desc", "$top": min(limit, 100), "$select": "id,categories"}
        ids: list[str] = []
        while url and len(ids) < limit:
            data = self._req("GET", url, params=params)
            ids += [m["id"] for m in data.get("value", []) if PROCESSED_LABEL not in (m.get("categories") or [])]
            url, params = data.get("@odata.nextLink"), None
        return ids[:limit]

    def get_message(self, message_id: str) -> EmailMessage:
        m = self._req("GET", f"/messages/{message_id}", params={"$select": "id,conversationId,from,toRecipients,ccRecipients,subject,body,receivedDateTime,hasAttachments,internetMessageHeaders"})
        body = m.get("body", {})
        text = body.get("content", "") if body.get("contentType") == "text" else _strip_html(body.get("content", ""))
        atts = []
        if m.get("hasAttachments"):
            for a in self._req("GET", f"/messages/{m['id']}/attachments").get("value", []):
                if a.get("@odata.type") == "#microsoft.graph.fileAttachment" and not a.get("isInline"):
                    atts.append(Attachment(id=a["id"], filename=a["name"], content_type=a.get("contentType") or "application/octet-stream", data=base64.b64decode(a["contentBytes"])))
        sender = ((m.get("from") or {}).get("emailAddress") or {}).get("address", "")
        # Exchange Online stamps its own Authentication-Results at the top.
        auth = [h["value"] for h in m.get("internetMessageHeaders") or [] if h.get("name", "").lower() == "authentication-results"]
        return EmailMessage(
            id=m["id"], thread_id=m.get("conversationId"), sender=sender,
            sender_authenticated=dmarc_pass(auth, sender.rsplit("@", 1)[-1]),
            mailbox=self.mailbox,
            to=[r["emailAddress"]["address"] for r in m.get("toRecipients", [])],
            cc=[r["emailAddress"]["address"] for r in m.get("ccRecipients", [])],
            subject=m.get("subject") or "", body_text=text,
            received_at=datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")),
            attachments=atts,
        )

    def reply(self, message: EmailMessage, body: str, *, send: bool) -> str:
        if send:
            self._req("POST", f"/messages/{message.id}/reply", json={"comment": body})
            return message.id
        return self._req("POST", f"/messages/{message.id}/createReply", json={"comment": body})["id"]

    def send(self, to: list[str], subject: str, body: str) -> str:
        self._req("POST", "/sendMail", json={"message": {"subject": subject, "body": {"contentType": "Text", "content": body}, "toRecipients": [{"emailAddress": {"address": a}} for a in to]}, "saveToSentItems": True})
        return ""

    def label(self, message: EmailMessage | str, label: str) -> None:
        mid = message if isinstance(message, str) else message.id
        current = self._req("GET", f"/messages/{mid}", params={"$select": "categories"}).get("categories", [])
        self._req("PATCH", f"/messages/{mid}", json={"categories": sorted(set(current) | {label})})
