#!/usr/bin/env python3

from __future__ import annotations

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


API_URL = "https://api.github.com/repos/ProxyShard/ShardBrowser/releases/latest"
ASSET_NAME = "ShardX.Launcher_{version}_aarch64.dmg"
DOWNLOAD_URL = (
    "https://github.com/ProxyShard/ShardBrowser/releases/download/"
    "v{version}/" + ASSET_NAME
)
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SRI_PATTERN = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
HEX_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
APP_NAME = "ShardX Launcher.app"
BUNDLE_ID = "com.shardx.launcher"
EXECUTABLE_NAME = "shardx-launcher"
DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[1]
    / "packages"
    / "shardx-launcher-source.json"
)


class UpdateError(RuntimeError):
    pass


def version_key(version: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"invalid stable version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def validate_response_url(
    url: str, allowed_hosts: set[str], description: str
) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise UpdateError(f"invalid {description} response URL: {url!r}") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise UpdateError(f"unexpected {description} response URL: {url!r}")


def release_metadata(release: dict) -> tuple[str, str, str | None]:
    if release.get("draft"):
        raise UpdateError("latest release is a draft")
    if release.get("prerelease"):
        raise UpdateError("latest release is a prerelease")

    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError(f"unexpected release tag: {tag!r}")
    version = tag.removeprefix("v")
    version_key(version)

    expected_name = ASSET_NAME.format(version=version)
    assets = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name") == expected_name
    ]
    if len(assets) != 1:
        raise UpdateError(
            f"expected exactly one {expected_name!r} asset, found {len(assets)}"
        )

    asset = assets[0]
    url = asset.get("browser_download_url")
    expected_url = DOWNLOAD_URL.format(version=version)
    if url != expected_url:
        raise UpdateError(f"unexpected asset URL: {url!r}")

    digest = asset.get("digest")
    if digest is None:
        digest_hex = None
    elif isinstance(digest, str) and (match := HEX_DIGEST_PATTERN.fullmatch(digest)):
        digest_hex = match.group(1)
    else:
        raise UpdateError(f"unexpected asset digest: {digest!r}")

    return version, url, digest_hex


def github_release() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "futuping-nix-packages-updater",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = Request(API_URL, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            validate_response_url(
                response.geturl(), {"api.github.com"}, "API"
            )
            return json.load(response)
    except (OSError, URLError, json.JSONDecodeError) as error:
        raise UpdateError(f"unable to query the latest release: {error}") from error


def download_artifact(url: str, destination: Path) -> str:
    request = Request(
        url,
        headers={"User-Agent": "futuping-nix-packages-updater"},
    )
    digest = hashlib.sha256()
    downloaded = 0

    try:
        with urlopen(request, timeout=60) as response:
            validate_response_url(
                response.geturl(), ALLOWED_DOWNLOAD_HOSTS, "download"
            )

            with destination.open("wb") as artifact:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("download exceeded the 64 MiB safety limit")
                    digest.update(chunk)
                    artifact.write(chunk)
    except (OSError, URLError) as error:
        raise UpdateError(f"unable to download the release asset: {error}") from error

    if downloaded == 0:
        raise UpdateError("downloaded asset is empty")
    return digest.hexdigest()


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


def inspect_application(dmg_path: Path, expected_version: str) -> None:
    if sys.platform != "darwin":
        return

    with tempfile.TemporaryDirectory(prefix="shardx-launcher-mount-") as temporary:
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
                raise UpdateError(
                    f"unable to read application Info.plist: {error}"
                ) from error

            identifier = plist_string(info, "CFBundleIdentifier")
            if identifier != BUNDLE_ID:
                raise UpdateError(f"unexpected bundle ID: {identifier!r}")
            version = plist_string(info, "CFBundleShortVersionString")
            if version != expected_version:
                raise UpdateError(f"unexpected bundle version: {version!r}")
            executable_name = plist_string(info, "CFBundleExecutable")
            if executable_name != EXECUTABLE_NAME:
                raise UpdateError(f"unexpected bundle executable: {executable_name!r}")

            executable = application / "Contents" / "MacOS" / EXECUTABLE_NAME
            architectures = command_output(
                ["/usr/bin/lipo", "-archs", str(executable)],
                "inspect application architecture",
            ).split()
            if architectures != ["arm64"]:
                raise UpdateError(f"unexpected application architectures: {architectures}")
        finally:
            detach = run(["/usr/bin/hdiutil", "detach", str(mount_path)])
            if detach.returncode != 0 and sys.exc_info()[0] is None:
                raise UpdateError(f"unable to detach DMG: {detach.stderr.strip()}")


def sri_sha256(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def read_source(path: Path) -> dict[str, str]:
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"unable to read {path}: {error}") from error

    if set(source) != {"version", "url", "hash"}:
        raise UpdateError(f"unexpected source keys: {sorted(source)}")
    if not all(isinstance(value, str) for value in source.values()):
        raise UpdateError("every source value must be a string")
    version_key(source["version"])
    if source["url"] != DOWNLOAD_URL.format(version=source["version"]):
        raise UpdateError(f"unexpected configured URL: {source['url']!r}")
    if not SRI_PATTERN.fullmatch(source["hash"]):
        raise UpdateError(f"invalid configured hash: {source['hash']!r}")
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
    version, url, api_digest = release_metadata(github_release())
    current_key = version_key(current["version"])
    latest_key = version_key(version)

    if latest_key < current_key:
        raise UpdateError(
            f"configured version {current['version']} is newer than upstream {version}"
        )

    with tempfile.TemporaryDirectory(prefix="shardx-launcher-update-") as temporary:
        dmg_path = Path(temporary) / ASSET_NAME.format(version=version)
        downloaded_digest = download_artifact(url, dmg_path)
        if api_digest is not None and downloaded_digest != api_digest:
            raise UpdateError(
                "downloaded SHA-256 does not match the GitHub release asset digest"
            )
        inspect_application(dmg_path, version)
    latest = {
        "version": version,
        "url": url,
        "hash": sri_sha256(downloaded_digest),
    }

    if latest_key == current_key:
        if latest != current:
            raise UpdateError(
                f"upstream asset for version {version} changed; manual review required"
            )
        print(f"ShardX Launcher {version} is already current")
        return False

    if check:
        print(f"ShardX Launcher {version} is available", file=sys.stderr)
        return True

    write_source(path, latest)
    print(f"Updated ShardX Launcher from {current['version']} to {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the official ShardX Launcher Apple Silicon application"
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
