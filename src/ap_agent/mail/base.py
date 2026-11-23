"""The mailbox port: Gmail and Outlook (Microsoft Graph) implement this."""

from __future__ import annotations

import re
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


def dmarc_pass(auth_results: list[str], sender_domain: str, trusted_authserv: tuple[str, ...] = ()) -> bool:
    """True only if the receiving server (not the sender) recorded an aligned
    DMARC pass for the From domain.

    Only the topmost Authentication-Results header counts - it is the one our
    own mail provider added; anything below it may have been forged by the
    sender. For Gmail the header must also carry Google's authserv-id.
    """
    if not auth_results:
        return False
    top = auth_results[0]
    if trusted_authserv and not top.strip().lower().startswith(tuple(a.lower() for a in trusted_authserv)):
        return False
    dmarc = re.search(r"\bdmarc=(\w+)", top, re.I)
    header_from = re.search(r"header\.from=([A-Za-z0-9.\-]+)", top, re.I)
    return bool(dmarc and dmarc.group(1).lower() == "pass" and header_from and header_from.group(1).lower().rstrip(".") == sender_domain.lower())


class EmailMessage(BaseModel):
    id: str
    thread_id: str | None = None
    sender: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body_text: str = ""
    received_at: datetime | None = None
    attachments: list[Attachment] = Field(default_factory=list)
    # Set by the connector from the receiving server's Authentication-Results header.
    sender_authenticated: bool = False
    mailbox: str | None = None  # which AP mailbox it was read from

    @property
    def sender_domain(self) -> str:
        return self.sender.rsplit("@", 1)[-1].lower().strip(">")

    @property
    def recipients(self) -> list[str]:
        return [*self.to, *self.cc]


class MailboxConnector(ABC):
    name: str = "mail"
    address: str = ""

    @abstractmethod
    def list_new_messages(self, lookback_days: int = 7, limit: int = 50) -> list[str]:
        """Ids of inbox messages not yet labelled as processed."""

    @abstractmethod
    def get_message(self, message_id: str) -> EmailMessage:
        """Full message with attachments and sender authentication."""

    @abstractmethod
    def reply(self, message: EmailMessage, body: str, *, send: bool) -> str:
        """Reply in-thread to the sender. ``send=False`` creates a draft. Returns id."""

    @abstractmethod
    def send(self, to: list[str], subject: str, body: str) -> str:
        """Send a new internal notification (approvals etc.)."""

    @abstractmethod
    def label(self, message: EmailMessage | str, label: str) -> None: ...
