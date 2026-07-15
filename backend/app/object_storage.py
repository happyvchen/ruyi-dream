from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import BinaryIO
import mimetypes

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import get_settings

DIRECT_UPLOAD_CORS_RULE_ID = "ruyi-dream-direct-upload"
DIRECT_UPLOAD_CORS_METHODS = ["PUT", "GET", "HEAD"]
DIRECT_UPLOAD_CORS_HEADERS = ["*"]
DIRECT_UPLOAD_CORS_EXPOSE_HEADERS = ["ETag"]
DIRECT_UPLOAD_CORS_MAX_AGE_SECONDS = 3600


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
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": addressing_style, "payload_signing_enabled": False},
        ),
    )


def ensure_bucket() -> None:
    settings = get_settings()
    client = s3_client()
    try:
        client.head_bucket(Bucket=settings.s3_bucket)
    except ClientError as exc:
        if _is_missing_bucket_error(exc):
            try:
                client.create_bucket(Bucket=settings.s3_bucket)
            except ClientError as create_exc:
                raise RuntimeError(_bucket_error_message("create", create_exc)) from create_exc
            return
        raise RuntimeError(_bucket_error_message("check", exc)) from exc


def ensure_direct_upload_cors() -> None:
    settings = get_settings()
    allowed_origins = _normalized_cors_origins(settings.direct_upload_cors_origins)
    if not allowed_origins:
        return
    desired_rule = _direct_upload_cors_rule(allowed_origins)
    client = s3_client()
    try:
        response = client.get_bucket_cors(Bucket=settings.s3_bucket)
        rules = list(response.get("CORSRules") or [])
    except ClientError as exc:
        if _is_missing_cors_error(exc):
            rules = []
        else:
            raise RuntimeError(_cors_error_message("read", exc)) from exc

    exact_direct_rules = [
        rule for rule in rules
        if rule.get("ID") == DIRECT_UPLOAD_CORS_RULE_ID or _cors_rule_equivalent(rule, desired_rule)
    ]
    if not exact_direct_rules and any(_cors_rule_covers_direct_upload(rule, allowed_origins) for rule in rules):
        return

    next_rules = [
        rule for rule in rules
        if rule.get("ID") != DIRECT_UPLOAD_CORS_RULE_ID and not _cors_rule_equivalent(rule, desired_rule)
    ]
    next_rules.append(desired_rule)
    if _cors_rules_equal(rules, next_rules):
        return
    try:
        client.put_bucket_cors(Bucket=settings.s3_bucket, CORSConfiguration={"CORSRules": next_rules})
    except ClientError as exc:
        raise RuntimeError(_cors_error_message("update", exc)) from exc


def _normalized_cors_origins(origins: list[str]) -> list[str]:
    seen = set()
    normalized = []
    for origin in origins:
        value = origin.strip().rstrip("/")
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _direct_upload_cors_rule(allowed_origins: list[str]) -> dict[str, object]:
    return {
        "ID": DIRECT_UPLOAD_CORS_RULE_ID,
        "AllowedMethods": DIRECT_UPLOAD_CORS_METHODS,
        "AllowedOrigins": allowed_origins,
        "AllowedHeaders": DIRECT_UPLOAD_CORS_HEADERS,
        "ExposeHeaders": DIRECT_UPLOAD_CORS_EXPOSE_HEADERS,
        "MaxAgeSeconds": DIRECT_UPLOAD_CORS_MAX_AGE_SECONDS,
    }


def _cors_rules_equal(left: list[dict[str, object]], right: list[dict[str, object]]) -> bool:
    return [_normalized_cors_rule(rule) for rule in left] == [_normalized_cors_rule(rule) for rule in right]


def _cors_rule_equivalent(rule: dict[str, object], desired_rule: dict[str, object]) -> bool:
    rule_without_id = {key: value for key, value in rule.items() if key != "ID"}
    desired_without_id = {key: value for key, value in desired_rule.items() if key != "ID"}
    return _normalized_cors_rule(rule_without_id) == _normalized_cors_rule(desired_without_id)


def _cors_rule_covers_direct_upload(rule: dict[str, object], allowed_origins: list[str]) -> bool:
    methods = {str(value).upper() for value in (rule.get("AllowedMethods") or [])}
    origins = {str(value).rstrip("/") for value in (rule.get("AllowedOrigins") or [])}
    headers = {str(value).lower() for value in (rule.get("AllowedHeaders") or [])}
    exposes = {str(value).lower() for value in (rule.get("ExposeHeaders") or [])}
    origins_cover = "*" in origins or all(origin in origins for origin in allowed_origins)
    headers_cover = "*" in headers or "content-type" in headers
    exposes_cover = not DIRECT_UPLOAD_CORS_EXPOSE_HEADERS or "*" in exposes or "etag" in exposes
    return all(method in methods for method in DIRECT_UPLOAD_CORS_METHODS) and origins_cover and headers_cover and exposes_cover


def _normalized_cors_rule(rule: dict[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = dict(rule)
    for key in ("AllowedMethods", "AllowedOrigins", "AllowedHeaders", "ExposeHeaders"):
        if key in normalized:
            normalized[key] = sorted(str(value) for value in (normalized.get(key) or []))
    return normalized


def _is_missing_cors_error(error: ClientError) -> bool:
    response = error.response or {}
    error_code = str(response.get("Error", {}).get("Code", ""))
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status_code == 404 or error_code in {"NoSuchCORSConfiguration", "NoSuchCORS", "404", "NotFound"}


def _is_missing_bucket_error(error: ClientError) -> bool:
    response = error.response or {}
    error_code = str(response.get("Error", {}).get("Code", ""))
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status_code == 404 or error_code in {"404", "NoSuchBucket", "NotFound"}


def _bucket_error_message(action: str, error: ClientError) -> str:
    settings = get_settings()
    response = error.response or {}
    error_info = response.get("Error", {})
    error_code = error_info.get("Code") or "unknown"
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode") or "unknown"
    return (
        f"Object storage bucket {action} failed for bucket '{settings.s3_bucket}' "
        f"at '{settings.s3_endpoint_url}' (status={status_code}, code={error_code}). "
        "Verify APP_S3_ENDPOINT_URL, APP_S3_BUCKET/APP_STORAGE_BUCKET, "
        "APP_S3_REGION, APP_S3_FORCE_PATH_STYLE, APP_S3_ACCESS_KEY_ID, and "
        "APP_S3_SECRET_ACCESS_KEY match the configured S3-compatible object storage service."
    )


def _cors_error_message(action: str, error: ClientError) -> str:
    settings = get_settings()
    response = error.response or {}
    error_info = response.get("Error", {})
    error_code = error_info.get("Code") or "unknown"
    status_code = response.get("ResponseMetadata", {}).get("HTTPStatusCode") or "unknown"
    return (
        f"Object storage CORS {action} failed for bucket '{settings.s3_bucket}' "
        f"at '{settings.s3_endpoint_url}' (status={status_code}, code={error_code}). "
        "Browser direct uploads require bucket CORS for APP_DIRECT_UPLOAD_CORS_ORIGINS "
        "or BACKEND_CORS_ORIGINS with PUT, GET, HEAD, Content-Type, and ETag access."
    )


def put_fileobj(key: str | Path, fileobj: BinaryIO, content_type: str | None = None) -> str:
    settings = get_settings()
    key = clean_key(key)
    if hasattr(fileobj, "seek"):
        fileobj.seek(0)
    data = fileobj.read()
    if isinstance(data, str):
        data = data.encode("utf-8")
    extra_args: dict[str, object] = {"Body": data}
    if content_type:
        extra_args["ContentType"] = content_type
    s3_client().put_object(Bucket=settings.s3_bucket, Key=key, **extra_args)
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


def object_head(key: str | Path | None) -> dict[str, object] | None:
    if not key:
        return None
    settings = get_settings()
    try:
        response = s3_client().head_object(Bucket=settings.s3_bucket, Key=clean_key(key))
    except ClientError:
        return None
    return {
        "contentLength": int(response.get("ContentLength") or 0),
        "contentType": response.get("ContentType"),
        "eTag": response.get("ETag"),
    }


def presigned_get_url(
    key: str | Path | None,
    *,
    response_content_disposition: str | None = None,
    response_content_type: str | None = None,
    response_cache_control: str | None = None,
    expires_seconds: int | None = None,
) -> str | None:
    if not key:
        return None
    settings = get_settings()
    params = {"Bucket": settings.s3_bucket, "Key": clean_key(key)}
    if response_content_disposition:
        params["ResponseContentDisposition"] = response_content_disposition
    if response_content_type:
        params["ResponseContentType"] = response_content_type
    if response_cache_control:
        params["ResponseCacheControl"] = response_cache_control
    return presign_s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=expires_seconds or settings.s3_presign_expires_seconds,
    )


def presigned_put_url(key: str | Path | None, content_type: str | None = None) -> str | None:
    if not key:
        return None
    settings = get_settings()
    params = {"Bucket": settings.s3_bucket, "Key": clean_key(key)}
    if content_type:
        params["ContentType"] = content_type
    return presign_s3_client().generate_presigned_url(
        "put_object",
        Params=params,
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
