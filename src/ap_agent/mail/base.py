"""The mailbox port: Gmail and Outlook (Microsoft Graph) implement this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field

PROCESSED_LABEL = "AP-Processed"
REVIEW_LABEL = "AP-Needs-Review"


class Attachment(BaseModel):
    id: str
    filename: str
    content_type: str
    data: bytes = Field(repr=False)


class EmailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    sender: str
    to: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    received_at: datetime | None = None
    attachments: list[Attachment] = Field(default_factory=list)

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower().strip(">")


class MailboxConnector(ABC):
    name: str = "mail"

    @abstractmethod
    def list_new_messages(self, lookback_days: int = 7, limit: int = 50) -> list[EmailMessage]:
        """Inbox messages not yet labelled as processed, with attachments loaded."""

    @abstractmethod
    def reply(self, message: EmailMessage, body: str, *, send: bool) -> str:
        """Reply in-thread to the sender. ``send=False`` creates a draft. Returns id."""

    @abstractmethod
    def send(self, to: list[str], subject: str, body: str) -> str:
        """Send a new internal notification (approvals etc.)."""

    @abstractmethod
    def label(self, message: EmailMessage, label: str) -> None: ...
