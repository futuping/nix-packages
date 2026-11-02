#!/usr/bin/env python3

"""Monitor official stable releases; import an interactively downloaded PKG.

ToDesk's download host requires browser verification. This updater never
downloads installers, replays browser cookies, or executes installer scripts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from xml.parsers.expat import ExpatError

from scripts.network import with_network_retries

API_URL = "https://www.todesk.com/api/config/getConfig?type=1"
DOWNLOAD_URL = "https://dl.todesk.com/macos/ToDesk_{version}.pkg"
VERSION_PATTERN = re.compile(r"^(0|[1-9][0-9]*)(?:\.(0|[1-9][0-9]*)){3}$")
SRI_PATTERN = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
MAX_API_BYTES = 1024 * 1024
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
BUNDLE_ID = "com.youqu.todesk.mac"
EXECUTABLE_NAME = "ToDesk"
TEAM_ID = "KM56KD59W4"
SIGNER = "Hainan Youqu Technology Co., Ltd. (KM56KD59W4)"
INSTALLER_AUTHORITY = "Developer ID Installer: " + SIGNER
APPLICATION_AUTHORITY = "Developer ID Application: " + SIGNER
APPLICATION_PATH = Path("ToDesk.pkg/Payload/Applications/ToDesk.app")
DEFAULT_SOURCE = Path(__file__).resolve().parents[1] / "packages" / "todesk-source.json"


class UpdateError(RuntimeError):
    pass


def version_key(version: str) -> tuple[int, int, int, int]:
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"invalid stable version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def stable_metadata(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 200000:
        raise UpdateError("official configuration did not report success")
    data = payload.get("data")
    entries = data.get("list") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise UpdateError("official configuration must contain a list")
    wanted = {"mac_version", "mac_link", "mac_link_new", "new_percent"}
    fields = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise UpdateError("invalid official configuration entry")
        name = entry["name"]
        if name not in wanted:
            continue
        if name in fields:
            raise UpdateError(f"duplicate official configuration field: {name}")
        if type(entry.get("type")) is not int or entry["type"] != 1 or not isinstance(entry.get("value"), str):
            raise UpdateError(f"invalid official configuration field: {name}")
        fields[name] = entry["value"]
    if set(fields) != wanted:
        raise UpdateError("missing stable macOS configuration fields")
    version = fields["mac_version"]
    version_key(version)
    percent = fields["new_percent"]
    if not re.fullmatch(r"-1|0|[1-9][0-9]?|100", percent):
        raise UpdateError(f"invalid stable rollout percentage: {percent!r}")
    # The website probabilistically uses *_new. Follow that link only after
    # full rollout; never select the independent *_gray preview fields.
    url = fields["mac_link_new"] if percent == "100" and fields["mac_link_new"] else fields["mac_link"]
    if url != DOWNLOAD_URL.format(version=version):
        raise UpdateError(f"unexpected stable macOS artifact URL: {url!r}")
    return version, url


def validate_api_url(url: str) -> None:
    if url != API_URL:
        raise UpdateError(f"unexpected official configuration URL: {url!r}")


class ReviewedRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_api_url(new_url)
        return super().redirect_request(request, response, code, message, headers, new_url)


def open_metadata(request: Request):
    validate_api_url(request.full_url)
    return build_opener(ReviewedRedirectHandler()).open(request, timeout=30)


def official_metadata() -> dict:
    request = Request(API_URL, headers={
        "Accept": "application/json",
        "User-Agent": "futuping-nix-packages-updater",
    })

    def fetch():
        with open_metadata(request) as response:
            validate_api_url(response.geturl())
            if response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise UpdateError("official configuration response is not JSON")
            payload = response.read(MAX_API_BYTES + 1)
            if len(payload) > MAX_API_BYTES:
                raise UpdateError("official configuration exceeded the 1 MiB safety limit")
            return json.loads(payload)

    try:
        return with_network_retries(fetch, "query the stable ToDesk release")
    except (OSError, URLError, IncompleteRead, json.JSONDecodeError, UnicodeError) as error:
        raise UpdateError(f"unable to query the stable ToDesk release: {error}") from error


def sri_sha256(digest: bytes) -> str:
    return "sha256-" + base64.b64encode(digest).decode("ascii")


def read_source(path: Path) -> dict[str, str]:
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError) as error:
        raise UpdateError(f"unable to read {path}: {error}") from error
    if not isinstance(source, dict) or set(source) != {"version", "url", "hash"}:
        raise UpdateError("unexpected source keys")
    if not all(isinstance(value, str) for value in source.values()):
        raise UpdateError("every source value must be a string")
    version_key(source["version"])
    if source["url"] != DOWNLOAD_URL.format(version=source["version"]):
        raise UpdateError(f"unexpected configured URL: {source['url']!r}")
    if not SRI_PATTERN.fullmatch(source["hash"]):
        raise UpdateError("invalid configured SHA-256 SRI hash")
    if sri_sha256(base64.b64decode(source["hash"][7:], validate=True)) != source["hash"]:
        raise UpdateError("configured SRI hash is not canonical")
    return source


def copy_artifact(artifact: Path, destination: Path) -> str:
    """Hash the exact private snapshot subsequently verified, never the live file."""
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = artifact.stat()
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_ARTIFACT_BYTES:
            raise UpdateError("installer must be a nonempty regular file of at most 512 MiB")
        with artifact.open("rb") as source, destination.open("wb") as target:
            header = source.read(4)
            if header != b"xar!":
                raise UpdateError("installer is not a XAR package (a browser challenge is not an installer)")
            chunk = header
            while chunk:
                size += len(chunk)
                if size > MAX_ARTIFACT_BYTES:
                    raise UpdateError("installer exceeded the 512 MiB safety limit")
                digest.update(chunk)
                target.write(chunk)
                chunk = source.read(1024 * 1024)
    except OSError as error:
        raise UpdateError(f"unable to read the local installer: {error}") from error
    return sri_sha256(digest.digest())


def command_output(arguments: list[str], description: str, *, timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            arguments, capture_output=True, check=False, text=True, timeout=timeout,
            env=dict(os.environ, LC_ALL="C", LANG="C"),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise UpdateError(f"unable to {description}: {error}") from error
    if result.returncode:
        raise UpdateError(f"unable to {description}: {(result.stderr or result.stdout).strip()}")
    return result.stdout + result.stderr


def inspect_bundle(application: Path, expected_version: str) -> None:
    try:
        with (application / "Contents/Info.plist").open("rb") as stream:
            metadata = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError, ExpatError) as error:
        raise UpdateError(f"unable to read the ToDesk bundle metadata: {error}") from error
    if not isinstance(metadata, dict) or (
        metadata.get("CFBundleIdentifier") != BUNDLE_ID
        or metadata.get("CFBundleShortVersionString") != expected_version
        or metadata.get("CFBundleExecutable") != EXECUTABLE_NAME
    ):
        raise UpdateError("unexpected ToDesk bundle identity, version, or executable")
    command_output(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures", str(application)],
        "verify the original ToDesk application signature",
    )
    signature = command_output(
        ["/usr/bin/codesign", "-d", "--verbose=4", str(application)],
        "inspect the ToDesk application signer",
    )
    teams = re.findall(r"^TeamIdentifier=(.+)$", signature, re.M)
    authorities = re.findall(r"^Authority=(.+)$", signature, re.M)
    if teams != [TEAM_ID] or not authorities or authorities[0] != APPLICATION_AUTHORITY:
        raise UpdateError("unexpected ToDesk application signing identity")
    executable = application / "Contents/MacOS" / EXECUTABLE_NAME
    architectures = command_output(["/usr/bin/lipo", "-archs", str(executable)], "inspect ToDesk architectures")
    if set(architectures.split()) != {"arm64", "x86_64"}:
        raise UpdateError("ToDesk must contain the reviewed arm64 and x86_64 architectures")


def inspect_installer(artifact: Path, expected_version: str) -> None:
    if sys.platform != "darwin":
        raise UpdateError("importing a ToDesk installer requires macOS identity tools")
    signature = command_output(["/usr/sbin/pkgutil", "--check-signature", str(artifact)], "verify the installer signature")
    identities = re.findall(r"^\s*1\. (.+)$", signature, re.M)
    if identities != [INSTALLER_AUTHORITY]:
        raise UpdateError("unexpected ToDesk installer signing identity")
    if not re.search(r"^\s*Status: signed by a developer certificate issued by Apple for distribution\s*$", signature, re.M):
        raise UpdateError("ToDesk installer does not have a trusted Developer ID signature")
    if not re.search(r"^\s*Notarization: trusted by the (?:Apple notary service|macOS Gatekeeper policy)\s*$", signature, re.M):
        raise UpdateError("ToDesk installer notarization is not trusted")
    with tempfile.TemporaryDirectory(prefix="todesk-inspect-") as temporary:
        expanded = Path(temporary) / "expanded"
        # Expansion reads the package but never executes its installation scripts.
        command_output(["/usr/sbin/pkgutil", "--expand-full", str(artifact), str(expanded)], "expand the ToDesk installer")
        application = expanded / APPLICATION_PATH
        if not application.is_dir() or application.is_symlink():
            raise UpdateError("ToDesk installer has an unreviewed application layout")
        inspect_bundle(application, expected_version)


def write_source(path: Path, source: dict[str, str]) -> None:
    payload = json.dumps(source, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def update(path: Path, *, artifact: Path | None = None, check: bool = False) -> bool:
    current = read_source(path)
    version, url = stable_metadata(official_metadata())
    current_key = version_key(current["version"])
    latest_key = version_key(version)
    if latest_key < current_key:
        raise UpdateError(f"configured version {current['version']} is newer than upstream {version}")
    if artifact is None:
        if latest_key == current_key:
            print(f"ToDesk {version} metadata is current; installer hash was not reverified")
            return False
        print(f"ToDesk {version} is available; source pin unchanged. Download the official installer in a browser, then run --artifact PATH.")
        return True
    with tempfile.TemporaryDirectory(prefix="todesk-import-") as temporary:
        snapshot = Path(temporary) / f"ToDesk_{version}.pkg"
        digest = copy_artifact(artifact, snapshot)
        inspect_installer(snapshot, version)
    candidate = {"version": version, "url": url, "hash": digest}
    if latest_key == current_key:
        if candidate != current:
            raise UpdateError(f"installer for version {version} changed; manual review required")
        print(f"ToDesk {version} installer matches the existing source pin")
        return False
    if check:
        print(f"ToDesk {version} installer validated; source pin unchanged")
        return True
    write_source(path, candidate)
    print(f"Updated ToDesk source from {current['version']} to {version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report pending updates without writing")
    parser.add_argument("--artifact", type=Path, help="verify and import an interactively downloaded official PKG")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="source metadata JSON path")
    arguments = parser.parse_args()
    try:
        changed = update(arguments.source, artifact=arguments.artifact, check=arguments.check)
    except UpdateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 1 if changed and (arguments.check or arguments.artifact is None) else 0


if __name__ == "__main__":
    raise SystemExit(main())
