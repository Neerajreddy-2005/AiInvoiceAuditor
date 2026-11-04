"""Private Supabase Storage bucket for uploaded invoices, through its S3 API.

S3 keys bypass the bucket's row-level security, so they stay on the server. They
were chosen over a service-role key because they reach Storage and nothing else.
"""
from functools import lru_cache
from pathlib import Path

from .settings import settings

SCHEME = "storage://"


def configured() -> bool:
    s = settings()
    return all((s.storage_endpoint, s.storage_region, s.storage_access_key, s.storage_secret_key))


@lru_cache
def _client():
    import boto3
    from botocore.config import Config

    s = settings()
    return boto3.client("s3", endpoint_url=s.storage_endpoint, region_name=s.storage_region,
                        aws_access_key_id=s.storage_access_key, aws_secret_access_key=s.storage_secret_key,
                        config=Config(s3={"addressing_style": "path"}, connect_timeout=5, read_timeout=60,
                                      retries={"max_attempts": 2}))


def put(key: str, data: bytes) -> None:
    _client().put_object(Bucket=settings().storage_bucket, Key=key, Body=data,
                         ContentType="application/octet-stream")


def download(key: str, path: Path) -> None:
    _client().download_file(settings().storage_bucket, key, str(path))
