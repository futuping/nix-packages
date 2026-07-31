#!/usr/bin/env python3

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DOWNLOAD_URL = (
    "https://cdn.ego.app/channel/egobrowser_npx_referral/"
    "setup/macos/arm64/egolite.dmg"
)
DOWNLOAD_HOST = "cdn.ego.app"
APP_NAME = "ego lite.app"
BUNDLE_ID = "com.citrolabs.ego.lite"
TEAM_ID = "JGQLC6YQYJ"
SIGNING_AUTHORITY = "Developer ID Application: CITRO LABS PTE. LIMITED (JGQLC6YQYJ)"
EXECUTABLE_NAME = "ego lite"
VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){3}$")
BUNDLE_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
SRI_PATTERN = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
UPSTREAM_HASH_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
S3_VERSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,256}$")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024
EXPECTED_SOURCE_KEYS = {
    "version",
    "bundleVersion",
    "url",
    "hash",
    "upstreamHash",
    "s3VersionId",
}
DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages" / "ego-lite-source.json"
)


class UpdateError(RuntimeError):
    pass


def version_key(version: str) -> tuple[int, int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"invalid application version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def validate_download_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != DOWNLOAD_HOST
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or url != DOWNLOAD_URL
    ):
        raise UpdateError(f"unexpected download URL: {url!r}")


def response_validators(headers) -> tuple[int, str, str]:
    content_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-apple-diskimage":
        raise UpdateError(f"unexpected content type: {content_type!r}")

    content_length_text = headers.get("Content-Length")
    try:
        content_length = int(content_length_text)
    except (TypeError, ValueError) as error:
        raise UpdateError(
            f"invalid content length: {content_length_text!r}"
        ) from error
    if not 0 < content_length <= MAX_DOWNLOAD_BYTES:
        raise UpdateError(f"unexpected content length: {content_length}")

    upstream_hash_hex = headers.get("X-Amz-Meta-Sha256", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", upstream_hash_hex):
        raise UpdateError(
            f"invalid upstream SHA-256 metadata: {upstream_hash_hex!r}"
        )

    s3_version_id = headers.get("X-Amz-Version-Id", "")
    if not S3_VERSION_ID_PATTERN.fullmatch(s3_version_id):
        raise UpdateError(f"invalid S3 version ID: {s3_version_id!r}")

    return content_length, upstream_hash_hex, s3_version_id


def download_artifact(url: str, destination: Path) -> tuple[str, str, str]:
    validate_download_url(url)
    request = Request(
        url,
        headers={"User-Agent": "futuping-nix-packages-updater"},
    )
    digest = hashlib.sha256()
    downloaded = 0

    try:
        with urlopen(request, timeout=60) as response:
            final_url = response.geturl()
            validate_download_url(final_url)
            content_length, upstream_hash_hex, s3_version_id = response_validators(
                response.headers
            )

            with destination.open("wb") as artifact:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("download exceeded the 256 MiB safety limit")
                    digest.update(chunk)
                    artifact.write(chunk)
    except (OSError, URLError) as error:
        raise UpdateError(f"unable to download ego lite: {error}") from error

    if downloaded != content_length:
        raise UpdateError(
            f"downloaded {downloaded} bytes, expected {content_length} bytes"
        )

    downloaded_hash_hex = digest.hexdigest()
    if downloaded_hash_hex != upstream_hash_hex:
        raise UpdateError("downloaded SHA-256 does not match CDN metadata")
    return downloaded_hash_hex, upstream_hash_hex, s3_version_id


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, check=False, text=True)


def command_output(command: list[str], description: str) -> str:
    result = run(command)
    if result.returncode != 0:
        details = (result.stderr or result.stdout).strip()
        raise UpdateError(f"unable to {description}: {details}")
    return (result.stdout or result.stderr).strip()


def plist_string(plist: dict, key: str) -> str:
    value = plist.get(key)
    if not isinstance(value, str) or not value:
        raise UpdateError(f"missing or invalid {key} in application Info.plist")
    return value


def inspect_application(dmg_path: Path) -> tuple[str, str]:
    if sys.platform != "darwin":
        raise UpdateError("ego lite artifact validation requires macOS")

    with tempfile.TemporaryDirectory(prefix="ego-lite-mount-") as temporary:
        mount_path = Path(temporary) / "mount"
        mount_path.mkdir()
        attach = run(
            [
                "/usr/bin/hdiutil",
                "attach",
                str(dmg_path),
                "-nobrowse",
                "-readonly",
                "-mountpoint",
                str(mount_path),
            ]
        )
        if attach.returncode != 0:
            raise UpdateError(f"unable to mount DMG: {attach.stderr.strip()}")

        try:
            applications = list(mount_path.glob("*.app"))
            if len(applications) != 1 or applications[0].name != APP_NAME:
                names = sorted(application.name for application in applications)
                raise UpdateError(f"unexpected application bundles in DMG: {names}")
            application = applications[0]

            try:
                info = plistlib.loads(
                    (application / "Contents" / "Info.plist").read_bytes()
                )
            except (OSError, plistlib.InvalidFileException) as error:
                raise UpdateError(f"unable to read application Info.plist: {error}") from error

            identifier = plist_string(info, "CFBundleIdentifier")
            if identifier != BUNDLE_ID:
                raise UpdateError(f"unexpected bundle ID: {identifier!r}")
            version = plist_string(info, "CFBundleShortVersionString")
            version_key(version)
            bundle_version = plist_string(info, "CFBundleVersion")
            if not BUNDLE_VERSION_PATTERN.fullmatch(bundle_version):
                raise UpdateError(f"invalid bundle version: {bundle_version!r}")
            executable_name = plist_string(info, "CFBundleExecutable")
            if executable_name != EXECUTABLE_NAME:
                raise UpdateError(f"unexpected bundle executable: {executable_name!r}")

            executable = application / "Contents" / "MacOS" / EXECUTABLE_NAME
            architectures = command_output(
                ["/usr/bin/lipo", "-archs", str(executable)],
                "inspect application architecture",
            ).split()
            if architectures != ["arm64"]:
                raise UpdateError(
                    f"unexpected application architectures: {architectures}"
                )

            signature_details = command_output(
                ["/usr/bin/codesign", "-dv", "--verbose=4", str(application)],
                "inspect application signing metadata",
            )
            team_match = re.search(r"^TeamIdentifier=(.+)$", signature_details, re.M)
            if team_match is None or team_match.group(1) != TEAM_ID:
                actual_team = team_match.group(1) if team_match else None
                raise UpdateError(f"unexpected signing Team ID: {actual_team!r}")
            authorities = re.findall(r"^Authority=(.+)$", signature_details, re.M)
            if not authorities or authorities[0] != SIGNING_AUTHORITY:
                raise UpdateError(f"unexpected signing authorities: {authorities}")

            verification = run(
                [
                    "/usr/bin/codesign",
                    "--verify",
                    "--deep",
                    "--strict",
                    "--verbose=2",
                    str(application),
                ]
            )
            if verification.returncode != 0:
                verification_details = (
                    verification.stderr or verification.stdout
                ).strip()
                raise UpdateError(
                    f"upstream signature verification failed: {verification_details}"
                )

            helper = (
                application
                / "Contents"
                / "Frameworks"
                / "ego Framework.framework"
                / "Versions"
                / "Current"
                / "Helpers"
                / "ego-browser"
            )
            if not helper.is_file() or not os.access(helper, os.X_OK):
                raise UpdateError("bundled ego-browser executable is missing")
            helper_architectures = command_output(
                ["/usr/bin/lipo", "-archs", str(helper)],
                "inspect ego-browser architecture",
            ).split()
            if helper_architectures != ["arm64"]:
                raise UpdateError(
                    f"unexpected ego-browser architectures: {helper_architectures}"
                )

            return version, bundle_version
        finally:
            detach = run(["/usr/bin/hdiutil", "detach", str(mount_path), "-quiet"])
            if detach.returncode != 0:
                print(
                    f"warning: unable to detach {mount_path}: {detach.stderr.strip()}",
                    file=sys.stderr,
                )


def sri_sha256(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def read_source(path: Path) -> dict[str, str]:
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"unable to read {path}: {error}") from error

    if set(source) != EXPECTED_SOURCE_KEYS:
        raise UpdateError(f"unexpected source keys: {sorted(source)}")
    if not all(isinstance(value, str) for value in source.values()):
        raise UpdateError("every source value must be a string")
    version_key(source["version"])
    if not BUNDLE_VERSION_PATTERN.fullmatch(source["bundleVersion"]):
        raise UpdateError(f"invalid bundle version: {source['bundleVersion']!r}")
    validate_download_url(source["url"])
    if not SRI_PATTERN.fullmatch(source["hash"]):
        raise UpdateError(f"invalid configured hash: {source['hash']!r}")
    upstream_hash_match = UPSTREAM_HASH_PATTERN.fullmatch(source["upstreamHash"])
    if upstream_hash_match is None:
        raise UpdateError(
            f"invalid configured upstream hash: {source['upstreamHash']!r}"
        )
    if source["hash"] != sri_sha256(upstream_hash_match.group(1)):
        raise UpdateError("configured SRI hash does not match upstream hash metadata")
    if not S3_VERSION_ID_PATTERN.fullmatch(source["s3VersionId"]):
        raise UpdateError(
            f"invalid configured S3 version ID: {source['s3VersionId']!r}"
        )
    return source


def write_source(path: Path, source: dict[str, str]) -> None:
    payload = json.dumps(source, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as temporary_file:
            temporary_file.write(payload)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def update(path: Path, *, check: bool = False) -> bool:
    current = read_source(path)

    with tempfile.TemporaryDirectory(prefix="ego-lite-update-") as temporary:
        artifact = Path(temporary) / "egolite.dmg"
        downloaded_hash, upstream_hash, s3_version_id = download_artifact(
            DOWNLOAD_URL, artifact
        )
        version, bundle_version = inspect_application(artifact)

    current_key = version_key(current["version"])
    latest_key = version_key(version)
    if latest_key < current_key:
        raise UpdateError(
            f"configured version {current['version']} is newer than upstream {version}"
        )

    latest = {
        "version": version,
        "bundleVersion": bundle_version,
        "url": DOWNLOAD_URL,
        "hash": sri_sha256(downloaded_hash),
        "upstreamHash": f"sha256:{upstream_hash}",
        "s3VersionId": s3_version_id,
    }

    if latest_key == current_key:
        if latest != current:
            raise UpdateError(
                f"upstream artifact for version {version} changed; manual review required"
            )
        print(f"ego lite {version} is already current")
        return False

    if check:
        print(f"ego lite {version} is available", file=sys.stderr)
        return True

    write_source(path, latest)
    print(f"Updated ego lite from {current['version']} to {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the official ego lite Apple Silicon application"
    )
    parser.add_argument("--check", action="store_true", help="check without writing")
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="source metadata JSON path",
    )
    arguments = parser.parse_args()

    try:
        changed = update(arguments.source, check=arguments.check)
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1 if arguments.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
