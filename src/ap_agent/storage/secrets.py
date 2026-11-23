"""Where rotating credentials (OAuth refresh tokens) live.

* ``db``    - Fernet-encrypted rows in the agent database (key from TOKEN_ENCRYPTION_KEY)
* ``gcp``   - Google Secret Manager (new version per rotation)
* ``aws``   - AWS Secrets Manager
* ``azure`` - Azure Key Vault
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from ap_agent.config import Settings
from ap_agent.storage.db import Database, SecretRecord


class SecretStore(ABC):
    @abstractmethod
    def get(self, key: str) -> str | None: ...

    @abstractmethod
    def set(self, key: str, value: str) -> None: ...


class DBSecretStore(SecretStore):
    def __init__(self, db: Database, key: str, allow_plaintext: bool):
        self.db = db
        self.fernet = None
        if key:
            from cryptography.fernet import Fernet

            self.fernet = Fernet(key.encode())
        elif not allow_plaintext:
            raise RuntimeError("TOKEN_ENCRYPTION_KEY is required")

    def get(self, key: str) -> str | None:
        with self.db.session() as s:
            rec = s.get(SecretRecord, key)
        if rec is None:
            return None
        if self.fernet and rec.value.startswith("enc:"):
            return self.fernet.decrypt(rec.value[4:].encode()).decode()
        return rec.value

    def set(self, key: str, value: str) -> None:
        stored = "enc:" + self.fernet.encrypt(value.encode()).decode() if self.fernet else value
        with self.db.session() as s, s.begin():
            rec = s.get(SecretRecord, key)
            if rec:
                rec.value = stored
            else:
                s.add(SecretRecord(key=key, value=stored))


def _safe(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9-]", "-", f"ap-agent-{key}")


class GCPSecretStore(SecretStore):
    def __init__(self, project: str):
        from google.cloud import secretmanager

        self.client = secretmanager.SecretManagerServiceClient()
        self.project = project

    def get(self, key):
        from google.api_core.exceptions import NotFound

        try:
            return self.client.access_secret_version(name=f"projects/{self.project}/secrets/{_safe(key)}/versions/latest").payload.data.decode()
        except NotFound:
            return None

    def set(self, key, value):
        from google.api_core.exceptions import AlreadyExists

        parent = f"projects/{self.project}"
        try:
            self.client.create_secret(parent=parent, secret_id=_safe(key), secret={"replication": {"automatic": {}}})
        except AlreadyExists:
            pass
        self.client.add_secret_version(parent=f"{parent}/secrets/{_safe(key)}", payload={"data": value.encode()})


class AWSSecretStore(SecretStore):
    def __init__(self, region: str):
        import boto3

        self.sm = boto3.client("secretsmanager", region_name=region)

    def get(self, key):
        try:
            return self.sm.get_secret_value(SecretId=_safe(key))["SecretString"]
        except self.sm.exceptions.ResourceNotFoundException:
            return None

    def set(self, key, value):
        try:
            self.sm.put_secret_value(SecretId=_safe(key), SecretString=value)
        except self.sm.exceptions.ResourceNotFoundException:
            self.sm.create_secret(Name=_safe(key), SecretString=value)


class AzureSecretStore(SecretStore):
    def __init__(self, vault_url: str):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        self.client = SecretClient(vault_url=vault_url, credential=DefaultAzureCredential())

    def get(self, key):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return self.client.get_secret(_safe(key)).value
        except ResourceNotFoundError:
            return None

    def set(self, key, value):
        self.client.set_secret(_safe(key), value)


def build_secret_store(settings: Settings, db: Database) -> SecretStore:
    if settings.secret_store == "gcp":
        return GCPSecretStore(settings.gcp_project_id or settings.vertex_project_id)
    if settings.secret_store == "aws":
        return AWSSecretStore(settings.aws_region)
    if settings.secret_store == "azure":
        return AzureSecretStore(settings.azure_key_vault_url)
    return DBSecretStore(db, settings.token_encryption_key, allow_plaintext=settings.is_dev())
