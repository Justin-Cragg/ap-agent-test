"""Test doubles: a scripted fake Anthropic client (no network, no spend)."""

from __future__ import annotations

import itertools
from collections import deque
from typing import Any

import pytest
from anthropic.types.beta import BetaMessage

from ap_agent.ap.policy import Policy
from ap_agent.config import Settings
from ap_agent.erp.memory import InMemoryERP
from ap_agent.llm.client import LLM
from ap_agent.mail.memory import InMemoryMailbox
from ap_agent.pipeline import APService
from ap_agent.storage.blob import LocalBlobStore
from ap_agent.storage.db import Database

_ids = itertools.count(1)


def tool_use(tool_name: str, /, **inp: Any) -> dict:
    return {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": tool_name, "input": inp}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def message(*blocks: dict, stop_reason: str | None = None) -> BetaMessage:
    if stop_reason is None:
        stop_reason = "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn"
    return BetaMessage.model_validate({
        "id": f"msg_{next(_ids)}", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, *blocks],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 80},
    })


class _Parsed:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None


class _Stream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **params):
        params = {**params, "messages": list(params["messages"])}  # snapshot; the loop appends in place
        self.owner.calls.append(params)
        step = self.owner.agent_script.popleft()
        return _Stream(step(params) if callable(step) else step)

    def parse(self, output_format, **params):
        self.owner.calls.append({"output_format": output_format, **params})
        return _Parsed(self.owner.extraction_script.popleft())


class FakeAnthropic:
    def __init__(self):
        self.calls: list[dict] = []
        self.agent_script: deque = deque()
        self.extraction_script: deque = deque()

        class _Beta:
            pass

        self.beta = _Beta()
        self.beta.messages = FakeMessages(self)


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/t.db", blob_bucket=str(tmp_path / "blobs"), api_token="test-token", policy_file=None, llm_provider="anthropic", erp_provider="memory", mail_provider="memory")


@pytest.fixture
def fake_client():
    return FakeAnthropic()


@pytest.fixture
def service(settings, fake_client, tmp_path):
    db = Database(settings.database_url)
    return APService(
        settings,
        llm=LLM(settings, client=fake_client),
        erp=InMemoryERP.with_demo_data(),
        mailbox=InMemoryMailbox(),
        db=db,
        blobs=LocalBlobStore(str(tmp_path / "blobs")),
        policy=Policy(),
    )
