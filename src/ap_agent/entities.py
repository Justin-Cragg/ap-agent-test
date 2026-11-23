"""Legal entities (companies / subsidiaries).

Each entity can have its own ERP connection (NetSuite subsidiary, BC company,
QuickBooks realm, Xero organisation), functional currency, AP mailbox aliases
and policy overrides. Credentials for an entity are read from environment
variables carrying its prefix, e.g. ``US01_QUICKBOOKS_REALM_ID``.

Without ``config/entities.yaml`` there is one entity, ``default``, configured
from the unprefixed settings.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ap_agent.config import Settings

DEFAULT = "default"


class Entity(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    code: str
    name: str
    functional_currency: str = "USD"
    mailbox_aliases: list[str] = Field(default_factory=list)
    env_prefix: str = ""
    overrides: dict[str, Any] = Field(default_factory=dict)
    settings: Settings | None = Field(default=None, exclude=True)

    @property
    def has_own_mailbox(self) -> bool:
        keys = ("mail_provider", "gmail_user", "outlook_mailbox")
        if any(k in self.overrides for k in keys):
            return True
        return bool(self.env_prefix) and any(os.environ.get(f"{self.env_prefix}{k.upper()}") for k in keys)


def entity_settings(base: Settings, prefix: str, overrides: dict[str, Any]) -> Settings:
    data = base.model_dump()
    if prefix:
        for field in Settings.model_fields:
            value = os.environ.get(f"{prefix}{field.upper()}")
            if value is not None:
                data[field] = value
    data.update(overrides)
    return Settings.model_validate(data)


def load_entities(base: Settings) -> list[Entity]:
    path = base.entities_file
    if not path or not Path(path).exists():
        return [Entity(code=DEFAULT, name=base.company_name, mailbox_aliases=[base.ap_mailbox_address] if base.ap_mailbox_address else [], settings=base)]
    raw = yaml.safe_load(Path(path).read_text()) or {}
    out = []
    for item in raw.get("entities", []):
        ent = Entity.model_validate(item)
        ent.settings = entity_settings(base, ent.env_prefix, ent.overrides)
        out.append(ent)
    if not out:
        raise ValueError(f"{path} defines no entities")
    if len({e.code for e in out}) != len(out):
        raise ValueError("Duplicate entity codes")
    return out


def route(recipients: list[str], entities: list[Entity]) -> Entity | None:
    """Pick the entity whose AP alias the email was sent to; None if ambiguous."""
    rcpt = {r.lower() for r in recipients}
    hits = [e for e in entities if rcpt & {a.lower() for a in e.mailbox_aliases}]
    if len(hits) == 1:
        return hits[0]
    if not hits and len(entities) == 1:
        return entities[0]
    return None
