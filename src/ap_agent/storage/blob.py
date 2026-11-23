"""Document storage for invoice files: local disk, S3, GCS or Azure Blob."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ap_agent.config import Settings


class BlobStore(ABC):
    @abstractmethod
    def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store bytes and return a URI."""

    @abstractmethod
    def get(self, uri: str) -> bytes: ...


class LocalBlobStore(BlobStore):
    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("invalid key")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return f"file://{path}"

    def get(self, uri: str) -> bytes:
        return Path(uri.removeprefix("file://")).read_bytes()


class S3BlobStore(BlobStore):
    def __init__(self, bucket: str, region: str):
        import boto3

        self.bucket = bucket
        self.s3 = boto3.client("s3", region_name=region)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256")
        return f"s3://{self.bucket}/{key}"

    def get(self, uri: str) -> bytes:
        bucket, key = uri.removeprefix("s3://").split("/", 1)
        return self.s3.get_object(Bucket=bucket, Key=key)["Body"].read()


class GCSBlobStore(BlobStore):
    def __init__(self, bucket: str):
        from google.cloud import storage

        self.client = storage.Client()
        self.bucket = self.client.bucket(bucket)

    def put(self, key: str, data: bytes, content_type: str) -> str:
        self.bucket.blob(key).upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket.name}/{key}"

    def get(self, uri: str) -> bytes:
        bucket, key = uri.removeprefix("gs://").split("/", 1)
        return self.client.bucket(bucket).blob(key).download_as_bytes()


class AzureBlobStore(BlobStore):
    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        self.service = BlobServiceClient(account_url=account_url, credential=DefaultAzureCredential())
        self.container = container

    def put(self, key: str, data: bytes, content_type: str) -> str:
        from azure.storage.blob import ContentSettings

        client = self.service.get_blob_client(self.container, key)
        client.upload_blob(data, overwrite=True, content_settings=ContentSettings(content_type=content_type))
        return f"azure://{self.container}/{key}"

    def get(self, uri: str) -> bytes:
        container, key = uri.removeprefix("azure://").split("/", 1)
        return self.service.get_blob_client(container, key).download_blob().readall()


def build_blob_store(settings: Settings) -> BlobStore:
    if settings.blob_backend == "local":
        return LocalBlobStore(settings.blob_bucket)
    if settings.blob_backend == "s3":
        return S3BlobStore(settings.blob_bucket, settings.aws_region)
    if settings.blob_backend == "gcs":
        return GCSBlobStore(settings.blob_bucket)
    if settings.blob_backend == "azure":
        return AzureBlobStore(settings.azure_storage_account_url, settings.blob_bucket)
    raise ValueError(settings.blob_backend)
