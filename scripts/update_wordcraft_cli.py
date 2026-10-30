#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from scripts.network import with_network_retries

API_URL = "https://api.github.com/repos/storytold/wordcraft/releases/latest"
ASSET_NAME = "wordcraft-cli-{version}-macos-universal.zip"
DOWNLOAD_URL = (
    "https://github.com/storytold/wordcraft/releases/download/v{version}/" + ASSET_NAME
)
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SRI_PATTERN = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
HEX_DIGEST_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")
ALLOWED_DOWNLOAD_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_API_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 256 * 1024 * 1024
TEAM_ID = "DJ6XS33FX8"
SIGNING_AUTHORITY = "Developer ID Application: Learning Machines LLC (DJ6XS33FX8)"
DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages" / "wordcraft-cli-source.json"
)


class UpdateError(RuntimeError):
    pass


def version_key(version: str) -> tuple[int, int, int]:
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"invalid stable version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def validate_response_url(url: str, allowed_hosts: set[str], description: str) -> None:
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


class ReviewedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, allowed_hosts: set[str], description: str):
        super().__init__()
        self.allowed_hosts = allowed_hosts
        self.description = description

    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_response_url(new_url, self.allowed_hosts, self.description)
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_reviewed(request: Request, hosts: set[str], description: str, timeout: int):
    validate_response_url(request.full_url, hosts, description)
    return build_opener(ReviewedRedirectHandler(hosts, description)).open(
        request, timeout=timeout
    )


def release_metadata(release: dict) -> tuple[str, str, str | None]:
    if not isinstance(release, dict):
        raise UpdateError("release metadata must be an object")
    if release.get("draft") is not False or release.get("prerelease") is not False:
        raise UpdateError("latest release is a draft, prerelease, or lacks release status")
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise UpdateError(f"unexpected release tag: {tag!r}")
    version = tag.removeprefix("v")
    version_key(version)
    all_assets = release.get("assets")
    if not isinstance(all_assets, list) or not all(
        isinstance(asset, dict) for asset in all_assets
    ):
        raise UpdateError("release assets must be a list of objects")
    expected_name = ASSET_NAME.format(version=version)
    assets = [asset for asset in all_assets if asset.get("name") == expected_name]
    if len(assets) != 1:
        raise UpdateError(f"expected exactly one {expected_name!r} asset, found {len(assets)}")
    asset = assets[0]
    url = asset.get("browser_download_url")
    if url != DOWNLOAD_URL.format(version=version):
        raise UpdateError(f"unexpected asset URL: {url!r}")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise UpdateError(f"unexpected asset size: {size!r}")
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

    def request_release():
        with open_reviewed(Request(API_URL, headers=headers), {"api.github.com"}, "API", 30) as response:
            validate_response_url(response.geturl(), {"api.github.com"}, "API")
            payload = response.read(MAX_API_BYTES + 1)
            if len(payload) > MAX_API_BYTES:
                raise UpdateError("release metadata exceeded the 2 MiB safety limit")
            return json.loads(payload)

    try:
        return with_network_retries(request_release, "query the latest GitHub release")
    except (OSError, URLError, IncompleteRead, json.JSONDecodeError, UnicodeError) as error:
        raise UpdateError(f"unable to query the latest release: {error}") from error


def download_artifact(url: str, destination: Path) -> str:
    request = Request(url, headers={"User-Agent": "futuping-nix-packages-updater"})

    def attempt_download():
        digest = hashlib.sha256()
        downloaded = 0
        with open_reviewed(request, ALLOWED_DOWNLOAD_HOSTS, "download", 60) as response:
            validate_response_url(response.geturl(), ALLOWED_DOWNLOAD_HOSTS, "download")
            with destination.open("wb") as artifact:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("download exceeded the 128 MiB safety limit")
                    digest.update(chunk)
                    artifact.write(chunk)
        if downloaded == 0:
            raise UpdateError("downloaded asset is empty")
        return digest.hexdigest()

    try:
        return with_network_retries(attempt_download, "download WordCraft CLI")
    except (OSError, URLError, IncompleteRead) as error:
        raise UpdateError(f"unable to download the release asset: {error}") from error


def inspect_archive(archive_path: Path, expected_version: str) -> None:
    directory = f"wordcraft-cli-{expected_version}-macos-universal"
    executable_name = f"{directory}/wordcraft-cli"
    # v0.12.0 adds upstream attribution and copyright notices alongside licenses.
    # Keep exact document names; arbitrary files and nested paths remain invalid.
    document_pattern = re.compile(
        r"(?:README\.md|ATTRIBUTION\.md|NOTICE|LICENSE(?:-MIT|-APACHE)?|COPYRIGHT|"
        r"OFL-[A-Za-z0-9_-]+\.txt)"
    )
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(set(names)) != len(names) or names.count(executable_name) != 1:
                raise UpdateError("archive must contain one unique wordcraft-cli executable")
            if sum(entry.file_size for entry in entries) > MAX_UNPACKED_BYTES:
                raise UpdateError("archive exceeds the 256 MiB unpacked safety limit")
            for entry in entries:
                name = entry.filename
                mode = entry.external_attr >> 16
                if name == directory + "/" and entry.is_dir():
                    continue
                relative = name.removeprefix(directory + "/")
                if (
                    name == relative
                    or "/" in relative
                    or (relative != "wordcraft-cli" and not document_pattern.fullmatch(relative))
                    or entry.is_dir()
                    or not stat.S_ISREG(mode)
                    or entry.flag_bits & 1
                ):
                    raise UpdateError(f"unexpected archive entry: {name!r}")
            executable_info = archive.getinfo(executable_name)
            if not (executable_info.external_attr >> 16) & 0o111:
                raise UpdateError("wordcraft-cli is not executable")
            executable_bytes = archive.read(executable_info)
            if len(executable_bytes) < 48 or executable_bytes[:4] != b"\xca\xfe\xba\xbe":
                raise UpdateError("wordcraft-cli is not a universal Mach-O")
            count = struct.unpack_from(">I", executable_bytes, 4)[0]
            architectures = [
                struct.unpack_from(">I", executable_bytes, offset)[0]
                for offset in (8, 28)
            ]
            if count != 2 or set(architectures) != {0x01000007, 0x0100000C}:
                raise UpdateError("wordcraft-cli must contain exactly arm64 and x86_64")
            # On macOS, check the original code seal without executing downloaded code.
            if sys.platform == "darwin":
                with tempfile.TemporaryDirectory(prefix="wordcraft-cli-verify-") as temporary:
                    executable = Path(temporary) / "wordcraft-cli"
                    executable.write_bytes(executable_bytes)
                    verification = subprocess.run(
                        [
                            "/usr/bin/codesign", "--verify", "--strict",
                            "--all-architectures", str(executable),
                        ],
                        capture_output=True, check=False, text=True,
                    )
                    if verification.returncode:
                        raise UpdateError(
                            "upstream signature verification failed: " + verification.stderr.strip()
                        )
                    signing_details = subprocess.run(
                        ["/usr/bin/codesign", "-dv", "--verbose=4", str(executable)],
                        capture_output=True, check=False, text=True,
                    )
                    if signing_details.returncode:
                        raise UpdateError("unable to inspect upstream signing identity")
                    metadata = signing_details.stdout + signing_details.stderr
                    teams = re.findall(r"^TeamIdentifier=(.+)$", metadata, re.M)
                    authorities = re.findall(r"^Authority=(.+)$", metadata, re.M)
                    if teams != [TEAM_ID] or not authorities or authorities[0] != SIGNING_AUTHORITY:
                        raise UpdateError("unexpected upstream signing identity")
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError(f"unable to inspect CLI archive: {error}") from error


def sri_sha256(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def read_source(path: Path) -> dict[str, str]:
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"unable to read {path}: {error}") from error
    if not isinstance(source, dict) or set(source) != {"version", "url", "hash"}:
        raise UpdateError("unexpected source keys")
    if not all(isinstance(value, str) for value in source.values()):
        raise UpdateError("every source value must be a string")
    version_key(source["version"])
    if source["url"] != DOWNLOAD_URL.format(version=source["version"]):
        raise UpdateError(f"unexpected configured URL: {source['url']!r}")
    if not SRI_PATTERN.fullmatch(source["hash"]):
        raise UpdateError(f"invalid configured hash: {source['hash']!r}")
    decoded = base64.b64decode(source["hash"][7:], validate=True)
    if sri_sha256(decoded.hex()) != source["hash"]:
        raise UpdateError("configured SRI hash is not canonical")
    return source


def write_source(path: Path, source: dict[str, str]) -> None:
    payload = json.dumps(source, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
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
        raise UpdateError(f"configured version {current['version']} is newer than upstream {version}")
    with tempfile.TemporaryDirectory(prefix="wordcraft-cli-update-") as temporary:
        artifact = Path(temporary) / ASSET_NAME.format(version=version)
        downloaded_digest = download_artifact(url, artifact)
        if api_digest is not None and downloaded_digest != api_digest:
            raise UpdateError("downloaded SHA-256 does not match the GitHub release asset digest")
        inspect_archive(artifact, version)
    latest = {"version": version, "url": url, "hash": sri_sha256(downloaded_digest)}
    if latest_key == current_key:
        if latest != current:
            raise UpdateError(f"upstream asset for version {version} changed; manual review required")
        print(f"WordCraft CLI {version} is already current")
        return False
    if check:
        print(f"WordCraft CLI {version} is available", file=sys.stderr)
        return True
    write_source(path, latest)
    print(f"Updated WordCraft CLI from {current['version']} to {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the official WordCraft universal macOS CLI")
    parser.add_argument("--check", action="store_true", help="check without writing")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="source metadata JSON path")
    arguments = parser.parse_args()
    try:
        changed = update(arguments.source, check=arguments.check)
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1 if arguments.check and changed else 0


if __name__ == "__main__":
    raise SystemExit(main())
