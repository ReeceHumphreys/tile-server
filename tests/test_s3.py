"""Exercise the real boto3 publisher against an isolated S3 implementation."""

import hashlib
import json

import pytest
from moto import mock_aws

from tests.make_release import main as make_release
from tile_service import releases, s3
from tile_service.runtime import sha256

pytestmark = pytest.mark.s3


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL_S3", raising=False)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    with mock_aws():
        client = s3.client()
        client.create_bucket(Bucket="test-tiles")
        monkeypatch.setattr(s3, "client", lambda endpoint=None: client)
        yield client


@pytest.fixture
def release(tmp_path):
    root = tmp_path / "release"
    make_release(str(root))
    return root


def publish(root):
    s3.upload(root, bucket="test-tiles", release_id="v1", part_size_mib=5)


def test_publish_ranges_metadata_and_immutable_retry(store, release, capsys):
    publish(release)
    files = releases.files(release)
    name = next(name for name in files if name.endswith(".pmtiles"))
    key = "releases/v1/" + name
    receipt = store.head_object(Bucket="test-tiles", Key=key)
    assert receipt["Metadata"]["sha256"] == sha256(release / name)
    assert receipt["ContentType"] == "application/vnd.pmtiles"
    assert "immutable" in receipt["CacheControl"]
    part = store.get_object(Bucket="test-tiles", Key=key, Range="bytes=0-6")
    assert part["ResponseMetadata"]["HTTPStatusCode"] == 206
    assert part["Body"].read() == b"PMTiles"
    marker = store.get_object(Bucket="test-tiles", Key="releases/v1/_complete.json")
    published = json.loads((release / "release.json").read_text())
    assert json.load(marker["Body"])["assetCount"] == published["assetCount"] == len(files) - 1
    assert (
        store.head_object(Bucket="test-tiles", Key="releases/v1/release.json")["CacheControl"]
        == "no-cache"
    )
    publish(release)
    assert "0 uploaded" in capsys.readouterr().out
    document = next(iter((release / "data").rglob("manifest.json")))
    document.write_text(document.read_text() + "\n")
    with pytest.raises(RuntimeError, match="different content"):
        publish(release)


def test_partial_upload_resumes_without_exposing_completion(store, release, monkeypatch, capsys):
    upload = store.upload_file

    def interrupted(filename, bucket, key, **kwargs):
        if key.endswith("manifest.json"):
            raise OSError("simulated network outage")
        return upload(filename, bucket, key, **kwargs)

    monkeypatch.setattr(store, "upload_file", interrupted)
    with pytest.raises(OSError, match="outage"):
        publish(release)
    assert s3.head(store, "test-tiles", "releases/v1/_complete.json") is None
    monkeypatch.setattr(store, "upload_file", upload)
    publish(release)
    assert "6 verified/reused" in capsys.readouterr().out
    assert s3.head(store, "test-tiles", "releases/v1/_complete.json")


def test_multipart_upload_keeps_sha_metadata_and_correct_bytes(store, release):
    manifest_path = next(iter((release / "data").rglob("manifest.json")))
    manifest = json.loads(manifest_path.read_text())
    face = next(iter(manifest["storage"]["archives"]))
    old = manifest_path.parent / manifest["storage"]["archives"][face]
    # Extra trailing bytes leave the fixture PMTiles offsets valid while forcing multipart upload.
    payload = old.read_bytes() + bytes(6 * 1024**2)
    digest = hashlib.sha256(payload).hexdigest()
    new = old.with_name("face-" + digest + ".pmtiles")
    new.write_bytes(payload)
    manifest["storage"]["archives"][face] = new.relative_to(manifest_path.parent).as_posix()
    manifest_path.write_text(json.dumps(manifest))
    publish(release)
    key = "releases/v1/" + new.relative_to(release).as_posix()
    receipt = store.head_object(Bucket="test-tiles", Key=key)
    assert "-" in receipt["ETag"]
    assert receipt["Metadata"]["sha256"] == digest
    response = store.get_object(Bucket="test-tiles", Key=key, Range="bytes=5242878-5242884")
    assert response["Body"].read() == payload[5242878:5242885]


def test_missing_completed_object_is_an_error(store, release):
    publish(release)
    store.delete_object(Bucket="test-tiles", Key="releases/v1/release.json")
    with pytest.raises(RuntimeError, match="missing an object"):
        publish(release)


def test_release_rejects_escaping_asset(release, tmp_path):
    manifest = next(iter((release / "data").rglob("manifest.json")))
    archive = next(iter(manifest.parent.rglob("*.pmtiles")))
    external = tmp_path / "outside.pmtiles"
    archive.rename(external)
    archive.symlink_to(external)
    with pytest.raises(RuntimeError, match="escaping"):
        releases.files(release)


def test_init_is_local_only_and_configures_cors(store):
    with pytest.raises(RuntimeError, match="explicit emulator"):
        s3.init_local("test-tiles")
    with pytest.raises(RuntimeError, match="restricted to local"):
        s3.init_local("test-tiles", "https://s3.us-east-1.amazonaws.com")
    s3.init_local("test-tiles", "http://localhost:4566")
    cors = store.get_bucket_cors(Bucket="test-tiles")["CORSRules"][0]
    assert cors["AllowedMethods"] == ["GET", "HEAD"]
    assert "Range" in cors["AllowedHeaders"] and "ETag" in cors["ExposeHeaders"]
