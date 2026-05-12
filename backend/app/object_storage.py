from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
import mimetypes

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import get_settings


def clean_key(value: str | Path) -> str:
    return str(value).replace("\\", "/").strip("/")


def join_key(*parts: str | Path | None) -> str:
    return "/".join(clean_key(part) for part in parts if part is not None and clean_key(part))


@lru_cache
def s3_client():
    settings = get_settings()
    return create_s3_client(settings.s3_endpoint_url)


@lru_cache
def presign_s3_client():
    settings = get_settings()
    return create_s3_client(settings.s3_public_endpoint_url)


def create_s3_client(endpoint_url: str):
    settings = get_settings()
    addressing_style = "path" if settings.s3_force_path_style else "virtual"
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}),
    )


def ensure_bucket() -> None:
    settings = get_settings()
    client = s3_client()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError:
        client.create_bucket(Bucket=settings.s3_bucket)


def put_fileobj(key: str | Path, fileobj: BinaryIO, content_type: str | None = None) -> str:
    settings = get_settings()
    key = clean_key(key)
    extra_args = {}
    if content_type:
        extra_args["ContentType"] = content_type
    if extra_args:
        s3_client().upload_fileobj(fileobj, settings.s3_bucket, key, ExtraArgs=extra_args)
    else:
        s3_client().upload_fileobj(fileobj, settings.s3_bucket, key)
    return key


def put_file(path: str | Path, key: str | Path | None = None, content_type: str | None = None) -> str:
    path = Path(path)
    target_key = clean_key(key or path.as_posix())
    guessed_type = content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with path.open("rb") as handle:
        return put_fileobj(target_key, handle, guessed_type)


def put_bytes(key: str | Path, data: bytes, content_type: str = "application/octet-stream") -> str:
    settings = get_settings()
    key = clean_key(key)
    s3_client().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    return key


def get_bytes(key: str | Path) -> bytes:
    settings = get_settings()
    response = s3_client().get_object(Bucket=settings.s3_bucket, Key=clean_key(key))
    return response["Body"].read()


def delete_object(key: str | Path | None) -> None:
    if not key:
        return
    settings = get_settings()
    s3_client().delete_object(Bucket=settings.s3_bucket, Key=clean_key(key))


def delete_prefix(prefix: str | Path | None) -> None:
    if not prefix:
        return
    settings = get_settings()
    client = s3_client()
    prefix = clean_key(prefix).rstrip("/") + "/"
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket, Prefix=prefix):
        objects = [{"Key": item["Key"]} for item in page.get("Contents", [])]
        if objects:
            client.delete_objects(Bucket=settings.s3_bucket, Delete={"Objects": objects})


def object_exists(key: str | Path | None) -> bool:
    if not key:
        return False
    settings = get_settings()
    try:
        s3_client().head_object(Bucket=settings.s3_bucket, Key=clean_key(key))
        return True
    except ClientError:
        return False


def object_size(key: str | Path | None) -> int | None:
    if not key:
        return None
    settings = get_settings()
    try:
        response = s3_client().head_object(Bucket=settings.s3_bucket, Key=clean_key(key))
        return int(response.get("ContentLength") or 0)
    except ClientError:
        return None


def presigned_get_url(key: str | Path | None) -> str | None:
    if not key:
        return None
    settings = get_settings()
    return presign_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": clean_key(key)},
        ExpiresIn=settings.s3_presign_expires_seconds,
    )


def unique_key(key: str | Path) -> str:
    key = clean_key(key)
    if not object_exists(key):
        return key
    path = Path(key)
    stem = path.stem
    suffix = path.suffix
    parent = clean_key(path.parent.as_posix())
    for index in range(1, 1000):
        candidate = join_key(parent, f"{stem}-{index}{suffix}")
        if not object_exists(candidate):
            return candidate
    from uuid import uuid4

    return join_key(parent, f"{stem}-{uuid4().hex[:8]}{suffix}")
