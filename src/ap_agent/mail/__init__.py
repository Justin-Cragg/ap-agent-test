from __future__ import annotations

from ap_agent.config import Settings
from ap_agent.mail.base import EmailMessage, MailboxConnector


def build_mailbox(settings: Settings) -> MailboxConnector:
    if settings.mail_provider == "memory":
        from ap_agent.mail.memory import InMemoryMailbox

        return InMemoryMailbox()
    if settings.mail_provider == "gmail":
        from ap_agent.mail.gmail import GmailMailbox, google_token_provider

        token = google_token_provider(settings.gmail_service_account_json, settings.gmail_user, settings.gmail_client_id, settings.gmail_client_secret, settings.gmail_refresh_token)
        return GmailMailbox(settings.gmail_user, token)
    if settings.mail_provider == "outlook":
        from ap_agent.mail.outlook import OutlookMailbox, graph_token_provider

        token = graph_token_provider(settings.outlook_tenant_id, settings.outlook_client_id, settings.outlook_client_secret)
        return OutlookMailbox(settings.outlook_mailbox, token)
    raise ValueError(settings.mail_provider)


__all__ = ["EmailMessage", "MailboxConnector", "build_mailbox"]
