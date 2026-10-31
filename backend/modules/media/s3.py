"""S3 multipart upload / presign / delete helpers (plan §3.8 / §5.13 /
Appendix A.5 / §C.2).

Owns: S3 multipart upload, presigned URL generation, raw/processed key
conventions, download.

Key naming convention (must be followed exactly, per Appendix C.2):
    raw videos:       teams/{team_number}/video/raw.{ext}
    processed videos:  teams/{team_number}/video/processed.webm

May import from: `backend.core` (settings for S3 credentials, env-var
fallback), `backend.models.settings` + `backend.core.db` (the `system_settings`
row the Settings page's "S3 Media Storage" section actually writes to — see
`_resolved_config()`). `boto3`/`aioboto3` only otherwise.
"""
from __future__ import annotations

import logging
import os

import aioboto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from backend.core.db import async_session_factory
from backend.core.exceptions import ProcessingError
from backend.core.settings import settings
from backend.models.settings import SystemSetting

logger = logging.getLogger(__name__)

# Multipart uploads use 8 MiB parts by default (S3's minimum part size,
# except for the final part, is 5 MiB). Per Appendix A.5 every upload must
# go through multipart — never a single PUT, regardless of file size (there
# is no size threshold to gate on: `upload_multipart` always uses the
# create/upload_part/complete multipart API).
_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024

_session = aioboto3.Session()


def raw_video_key(team_number: str, ext: str) -> str:
    """Return the canonical S3 key for a team's raw uploaded video."""
    ext = ext.lstrip(".").lower()
    return f"teams/{team_number}/video/raw.{ext}"


def processed_video_key(team_number: str) -> str:
    """Return the canonical S3 key for a team's processed (chroma-keyed) video."""
    return f"teams/{team_number}/video/processed.webm"


def team_logo_key(team_number: str, ext: str) -> str:
    return f"teams/{team_number}/logo.{ext.lstrip('.').lower()}"


def site_image_key(name: str) -> str:
    """Images uploaded for the public site (event map, sponsor logos)."""
    return f"site/images/{name}"


CLIP_KINDS = ("bot", "team")


def clip_raw_key(team_number: str, kind: str, ext: str) -> str:
    """S3 key for a team's raw recorded clip (`kind` is "bot" or "team")."""
    return f"teams/{team_number}/clips/{kind}/raw.{ext.lstrip('.').lower()}"


def clip_processed_key(team_number: str, kind: str) -> str:
    """S3 key for a team's processed (chroma-keyed, transparent) clip."""
    return f"teams/{team_number}/clips/{kind}/processed.webm"


class _ResolvedS3Config:
    __slots__ = ("endpoint_url", "public_endpoint_url", "bucket", "access_key", "secret_key", "region")

    def __init__(
        self,
        endpoint_url: str | None,
        public_endpoint_url: str | None,
        bucket: str | None,
        access_key: str | None,
        secret_key: str | None,
        region: str,
    ) -> None:
        self.endpoint_url = endpoint_url
        self.public_endpoint_url = public_endpoint_url
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region


async def _resolved_config() -> _ResolvedS3Config:
    """Resolves S3 config from the `system_settings` row the Settings page's
    "S3 Media Storage" section writes to (`PUT /api/v1/settings` with key
    `"s3"`), falling back field-by-field to the `S3_*` env vars.

    Before this, the DB row was write-only: `update_settings` persisted it
    but nothing ever read it back, so editing S3 config in the UI silently
    did nothing (the client only ever used env vars). A per-call DB read
    keeps this simple and always-current — uploads/presigns aren't
    frequent/hot-path enough to need caching, and a stale cache here would
    silently keep using old credentials after a rotation.
    """
    value: dict[str, object] = {}
    try:
        async with async_session_factory() as session:
            row = await session.get(SystemSetting, "s3")
            if row is not None:
                value = row.value or {}
    except Exception:
        logger.exception("Failed to read 's3' system_settings row; falling back to env vars only")

    def _str(field: str, env_value: str | None) -> str | None:
        raw = value.get(field)
        return str(raw) if raw else env_value

    return _ResolvedS3Config(
        endpoint_url=_str("endpoint_url", settings.S3_ENDPOINT_URL),
        public_endpoint_url=_str("public_endpoint_url", settings.S3_PUBLIC_ENDPOINT_URL),
        bucket=_str("bucket", settings.S3_BUCKET),
        access_key=_str("access_key", settings.S3_ACCESS_KEY),
        secret_key=_str("secret_key", settings.S3_SECRET_KEY),
        region=_str("region", settings.S3_REGION) or settings.S3_REGION,
    )


def _require_s3_settings(config: _ResolvedS3Config) -> None:
    if not config.bucket:
        raise ProcessingError(
            "S3 is not configured (bucket is unset) — set S3 credentials via "
            "env vars or the Settings page before uploading media."
        )


def _client_ctx(config: _ResolvedS3Config, endpoint_url: str | None):
    return _session.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        region_name=config.region,
        # path-style addressing is required for MinIO and most self-hosted
        # S3-compatible endpoints (virtual-hosted-style buckets don't resolve).
        config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
    )


async def upload_multipart(local_path: str, s3_key: str) -> None:
    """Upload `local_path` to `s3_key` using S3's multipart upload API.

    Always uses multipart (create/upload_part/complete), never a single PUT,
    per Appendix A.5 — regardless of file size. Aborts the multipart upload
    on any failure so no orphaned parts are left billing storage.
    """
    config = await _resolved_config()
    _require_s3_settings(config)
    file_size = os.path.getsize(local_path)

    async with _client_ctx(config, config.endpoint_url) as s3:
        create_resp = await s3.create_multipart_upload(Bucket=config.bucket, Key=s3_key)
        upload_id = create_resp["UploadId"]
        parts: list[dict] = []
        try:
            with open(local_path, "rb") as fh:
                part_number = 1
                while True:
                    chunk = fh.read(_MULTIPART_CHUNK_SIZE)
                    if not chunk:
                        break
                    part_resp = await s3.upload_part(
                        Bucket=config.bucket,
                        Key=s3_key,
                        PartNumber=part_number,
                        UploadId=upload_id,
                        Body=chunk,
                    )
                    parts.append({"PartNumber": part_number, "ETag": part_resp["ETag"]})
                    part_number += 1

            if not parts:
                # Zero-byte file: S3 requires at least one part.
                part_resp = await s3.upload_part(
                    Bucket=config.bucket,
                    Key=s3_key,
                    PartNumber=1,
                    UploadId=upload_id,
                    Body=b"",
                )
                parts.append({"PartNumber": 1, "ETag": part_resp["ETag"]})

            await s3.complete_multipart_upload(
                Bucket=config.bucket,
                Key=s3_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception as exc:
            try:
                await s3.abort_multipart_upload(
                    Bucket=config.bucket, Key=s3_key, UploadId=upload_id
                )
            except Exception:
                logger.exception("Failed to abort multipart upload %s for key %s", upload_id, s3_key)
            raise ProcessingError(
                f"Multipart upload of {local_path!r} to {s3_key!r} failed: {exc}"
            ) from exc

    logger.info("Uploaded %s (%d bytes) to s3://%s/%s", local_path, file_size, config.bucket, s3_key)


async def generate_presigned_url(s3_key: str, expires_in: int = 900) -> str:
    """Generate a pre-signed GET URL for `s3_key`, valid for `expires_in`
    seconds (default 15 minutes, per plan §3.8).

    Signed against `public_endpoint_url`/`S3_PUBLIC_ENDPOINT_URL` when set,
    not the internal `endpoint_url` the backend itself uses to talk to S3 —
    a self-hosted endpoint's in-cluster hostname (e.g. MinIO's `minio:9000`)
    is typically unresolvable from the browser this URL is ultimately handed
    to. Falls back to `endpoint_url` (correct for public AWS S3, where both
    are the same reachable host).
    """
    config = await _resolved_config()
    _require_s3_settings(config)
    async with _client_ctx(config, config.public_endpoint_url or config.endpoint_url) as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": config.bucket, "Key": s3_key},
            ExpiresIn=expires_in,
        )


async def generate_presigned_put_url(s3_key: str, expires_in: int = 3600, content_type: str | None = None) -> str:
    """Pre-signed single-PUT upload URL for `s3_key`, signed against the public
    endpoint like `generate_presigned_url` (the capture agent is outside the
    server's network). When `content_type` is given the uploader must send the
    same `Content-Type` header."""
    config = await _resolved_config()
    _require_s3_settings(config)
    params: dict[str, str] = {"Bucket": config.bucket, "Key": s3_key}
    if content_type:
        params["ContentType"] = content_type
    async with _client_ctx(config, config.public_endpoint_url or config.endpoint_url) as s3:
        return await s3.generate_presigned_url("put_object", Params=params, ExpiresIn=expires_in)


async def object_size(s3_key: str) -> int | None:
    """Size in bytes of `s3_key`, or None if it does not exist."""
    config = await _resolved_config()
    _require_s3_settings(config)
    async with _client_ctx(config, config.endpoint_url) as s3:
        try:
            head = await s3.head_object(Bucket=config.bucket, Key=s3_key)
            return int(head.get("ContentLength", 0))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code", "") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise


async def delete_object(s3_key: str) -> None:
    """Delete `s3_key` from the configured bucket. Idempotent — deleting a
    key that doesn't exist is not an error (matches S3 semantics)."""
    config = await _resolved_config()
    _require_s3_settings(config)
    async with _client_ctx(config, config.endpoint_url) as s3:
        await s3.delete_object(Bucket=config.bucket, Key=s3_key)


async def object_exists(s3_key: str) -> bool:
    """Return True if `s3_key` exists in the configured bucket."""
    config = await _resolved_config()
    _require_s3_settings(config)
    async with _client_ctx(config, config.endpoint_url) as s3:
        try:
            await s3.head_object(Bucket=config.bucket, Key=s3_key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise
