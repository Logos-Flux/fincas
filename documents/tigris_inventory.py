"""Tigris inventory helpers — S3 ListObjectsV2 with pagination and optional text extract.

Kept isolated so the management command and tests can swap the client/fetcher.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TigrisObject:
    key: str
    size: int | None = None
    etag: str | None = None


def list_bucket_keys(
    client,
    bucket: str,
    prefix: str = "",
    max_keys: int | None = None,
) -> list[TigrisObject]:
    """List all keys in *bucket* (paginated). Returns [] on list failure.

    *client* is a boto3 S3 client (or any object with list_objects_v2 + get_paginator).
    *max_keys* caps the total returned (for --limit / testing).
    """
    objs: list[TigrisObject] = []
    try:
        paginator = client.get_paginator("list_objects_v2")
        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
    except Exception:
        # Fallback for stub clients in tests that only implement list_objects_v2
        return _list_via_manual_pagination(client, bucket, prefix, max_keys)

    try:
        for page in pages:
            for item in page.get("Contents") or []:
                objs.append(TigrisObject(
                    key=item.get("Key", ""),
                    size=item.get("Size"),
                    etag=item.get("ETag"),
                ))
                if max_keys is not None and len(objs) >= max_keys:
                    return objs[:max_keys]
    except Exception:
        logger.exception("tigris_inventory: listing failed for bucket %r", bucket)
        return objs

    if max_keys is not None:
        return objs[:max_keys]
    return objs


def _list_via_manual_pagination(client, bucket: str, prefix: str, max_keys: int | None) -> list[TigrisObject]:
    objs: list[TigrisObject] = []
    token: str | None = None
    while True:
        kwargs: dict = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            resp = client.list_objects_v2(**kwargs)  # type: ignore[attr-defined]
        except Exception:
            logger.exception("tigris_inventory: list_objects_v2 failed")
            break
        for item in resp.get("Contents") or []:
            objs.append(TigrisObject(key=item.get("Key", ""), size=item.get("Size"), etag=item.get("ETag")))
            if max_keys is not None and len(objs) >= max_keys:
                return objs[:max_keys]
        if not resp.get("IsTruncated"):
            break
        token = resp.get("NextContinuationToken")
        if not token:
            break
    return objs


def fetch_first_bytes(client, bucket: str, key: str, max_bytes: int = 1_200_000) -> bytes:
    """Fetch up to *max_bytes* from *key* for first-page text extract.

    Returns b"" on any failure. Caps via Range header so we don't pull
    a 50 MB manual for 800 chars of title-page text.
    """
    try:
        resp = client.get_object(Bucket=bucket, Key=key, Range=f"bytes=0-{max_bytes - 1}")  # type: ignore[attr-defined]
        body = resp.get("Body")
        if body is None:
            return b""
        # StreamingBody vs BytesIO in stubs
        if hasattr(body, "read"):
            return body.read()
        return bytes(body)
    except Exception:
        return b""
