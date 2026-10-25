"""Publish immutable static releases through the standard S3 API."""

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from pipeline.runtime import check, read_json, sha256

IMMUTABLE = "public, max-age=31536000, immutable"
REVALIDATE = "no-cache"


def client(endpoint=None):
    endpoint = (
        endpoint or os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1")),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path" if endpoint else "auto"},
            retries={"mode": "standard", "max_attempts": 5},
        ),
    )


def bucket_name(args):
    name = args.get("--bucket", os.environ.get("TILE_BUCKET", ""))
    if not name:
        raise RuntimeError("Set TILE_BUCKET or --bucket")
    return name


def head(s3, bucket, key):
    try:
        return s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def init_s3(args):
    """Initialize only a local emulator; production buckets are managed by Terraform."""
    args.validate({"--bucket", "--endpoint-url", "--origin"})
    endpoint = (
        args.get("--endpoint-url")
        or os.environ.get("AWS_ENDPOINT_URL_S3")
        or os.environ.get("AWS_ENDPOINT_URL")
    )
    if not endpoint:
        raise RuntimeError("init-s3 requires an explicit emulator endpoint; use Terraform for AWS")
    host = urlsplit(endpoint).hostname or ""
    if host not in {
        "localhost",
        "127.0.0.1",
        "objectstore",
        "localstack",
        "host.docker.internal",
    } and not host.endswith(".localhost.localstack.cloud"):
        raise RuntimeError(
            "init-s3 is restricted to local emulator endpoints; use Terraform for AWS"
        )
    s3, bucket = client(endpoint), bucket_name(args)
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("404", "NoSuchBucket", "NotFound"):
            raise
        options = {"Bucket": bucket}
        if s3.meta.region_name != "us-east-1":
            options["CreateBucketConfiguration"] = {"LocationConstraint": s3.meta.region_name}
        s3.create_bucket(**options)
    s3.put_bucket_cors(
        Bucket=bucket,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": [args.get("--origin", "*")],
                    "AllowedMethods": ["GET", "HEAD"],
                    "AllowedHeaders": ["Range", "If-Match", "If-None-Match"],
                    "ExposeHeaders": ["ETag", "Accept-Ranges", "Content-Length", "Content-Range"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
    # Local reads are anonymous; AWS uses a private bucket and CloudFront OAC.
    actions = ["s3:GetObject"]
    if os.environ.get("TILE_S3_EMULATOR") == "moto":
        # Moto's anonymous HEAD policy check uses this non-AWS action name.
        # Keep the workaround in local initialization, outside the AWS policy.
        actions.append("s3:HeadObject")
    s3.put_bucket_policy(
        Bucket=bucket,
        Policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": actions,
                        "Resource": f"arn:aws:s3:::{bucket}/releases/*",
                    }
                ],
            }
        ),
    )
    print(f"Initialized local S3 bucket {bucket}")


def release_files(root):
    """Read the exported release, reject escapes, and validate its archive/page hashes."""
    release = read_json(root / "release.json")
    paths = {}

    def add(relative, expected=None):
        path = root / relative
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise RuntimeError(f"Missing or escaping release asset: {relative}")
        digest = sha256(path)
        if expected and digest != expected:
            raise RuntimeError(f"Release asset checksum mismatch: {relative}")
        paths[relative] = (path, digest)

    def storage(directory, descriptor):
        if descriptor.get("type") != "pmtiles" or descriptor.get("profile") != "normalized-cube-v1":
            raise RuntimeError("Unsupported release storage")
        for name in descriptor["archives"].values():
            path = directory / name
            expected = path.stem.rsplit("-", 1)[-1]
            if not re.fullmatch(r"[a-f0-9]{64}", expected):
                raise RuntimeError(f"Archive lacks content hash: {path}")
            add(path.as_posix(), expected)

    for name in release["documents"]:
        check()
        relative = Path("data") / name
        add(relative.as_posix())
        document = read_json(root / relative)
        if "sites" in document:
            for site in document["sites"]:
                storage(relative.parent / site["id"], site["storage"])
        else:
            storage(relative.parent, document["storage"])
            for page in document.get("pages", {}).values():
                add((relative.parent / page["path"]).as_posix(), page["sha256"])
    if len(paths) != release["assetCount"]:
        raise RuntimeError("Release asset count mismatch")
    add("release.json")
    return paths


def publish_s3(args):
    args.validate({"--source", "--bucket", "--release", "--endpoint-url", "--part-size-mib"})
    root = Path(args.get("--source", "published")).resolve()
    bucket = bucket_name(args)
    release_id = args.get("--release", os.environ.get("TILE_RELEASE", ""))
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", release_id):
        raise RuntimeError("Set a safe, unique TILE_RELEASE or --release")
    part_size = args.integer("--part-size-mib", 64)
    if not 5 <= part_size <= 512:
        raise RuntimeError("Multipart size must be 5..512 MiB")
    files = release_files(root)
    identity = hashlib.sha256()
    for name, (_, digest) in sorted(files.items()):
        identity.update(f"{name}:{digest}\n".encode())
    release_hash = identity.hexdigest()
    prefix = f"releases/{release_id}/"
    marker_key = prefix + "_complete.json"
    s3 = client(args.get("--endpoint-url"))
    marker = head(s3, bucket, marker_key)
    if marker and marker.get("Metadata", {}).get("sha256") != release_hash:
        raise RuntimeError(
            "S3 release already exists with different content; choose a new release ID"
        )
    config = TransferConfig(
        multipart_threshold=part_size * 1024**2,
        multipart_chunksize=part_size * 1024**2,
        max_concurrency=4,
    )
    uploaded = reused = 0
    # Payloads first, then metadata, then the completion marker. A partial
    # upload is resumable and never reports a completed release.
    ordered = sorted(
        files, key=lambda name: (not name.endswith(".pmtiles"), name == "release.json", name)
    )
    for name in ordered:
        check()
        path, digest = files[name]
        key = prefix + name
        existing = head(s3, bucket, key)
        if existing:
            if (
                existing.get("Metadata", {}).get("sha256") != digest
                or existing["ContentLength"] != path.stat().st_size
            ):
                raise RuntimeError(
                    f"Existing S3 release object differs: {key}; choose a new release ID"
                )
            reused += 1
            continue
        if marker:
            raise RuntimeError(f"Completed S3 release is missing an object: {key}")
        is_archive = name.endswith(".pmtiles")
        s3.upload_file(
            str(path),
            bucket,
            key,
            ExtraArgs={
                "ContentType": "application/vnd.pmtiles" if is_archive else "application/json",
                "CacheControl": IMMUTABLE if is_archive else REVALIDATE,
                "Metadata": {"sha256": digest},
            },
            Config=config,
        )
        # Verify the upload receipt; never compare multipart ETags to a SHA256.
        receipt = head(s3, bucket, key)
        if (
            not receipt
            or receipt["ContentLength"] != path.stat().st_size
            or receipt.get("Metadata", {}).get("sha256") != digest
        ):
            raise RuntimeError(f"S3 upload verification failed: {key}")
        uploaded += 1
        print(f"Uploaded s3://{bucket}/{key}", flush=True)
    check()
    if not marker:
        s3.put_object(
            Bucket=bucket,
            Key=marker_key,
            Body=json.dumps(
                {
                    "version": 1,
                    "release": release_id,
                    "sha256": release_hash,
                    "assetCount": read_json(root / "release.json")["assetCount"],
                }
            ).encode(),
            ContentType="application/json",
            CacheControl=REVALIDATE,
            Metadata={"sha256": release_hash},
        )
    print(f"S3 release {release_id}: {uploaded} uploaded, {reused} verified/reused")


def seed_s3(args):
    args.validate({"--source"})
    from pipeline.runtime import Args

    init_s3(Args(["tile-service", "init-s3"]))
    publish_s3(Args(["tile-service", "publish-s3", "--source", args.get("--source", "published")]))
