"""In-memory mailbox for tests and local demos."""

from __future__ import annotations

from ap_agent.mail.base import EmailMessage, MailboxConnector, PROCESSED_LABEL


class InMemoryMailbox(MailboxConnector):
    name = "memory"

    def __init__(self, messages: list[EmailMessage] | None = None, address: str = "ap@example.com"):
        self.messages = list(messages or [])
        self.address = address
        self.labels: dict[str, set[str]] = {}
        self.replies: list[dict] = []
        self.sent: list[dict] = []

    def list_new_messages(self, lookback_days: int = 7, limit: int = 50):
        return [m.id for m in self.messages if PROCESSED_LABEL not in self.labels.get(m.id, set())][:limit]

    def get_message(self, message_id):
        msg = next(m for m in self.messages if m.id == message_id)
        return msg.model_copy(update={"mailbox": self.address})

    def reply(self, message, body, *, send):
        self.replies.append({"message_id": message.id, "to": message.sender, "body": body, "sent": send})
        return f"reply-{len(self.replies)}"

    def send(self, to, subject, body):
        self.sent.append({"to": to, "subject": subject, "body": body})
        return f"sent-{len(self.sent)}"

    def label(self, message, label):
        mid = message if isinstance(message, str) else message.id
        self.labels.setdefault(mid, set()).add(label)
