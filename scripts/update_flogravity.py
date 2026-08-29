#!/usr/bin/env python3

from __future__ import annotations

import argparse
import base64
from collections.abc import Callable, Iterable
import hashlib
import hmac
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
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ElementTree


APPCAST_URL = "https://d.vkr.me/fg/mac/appcast.xml"
DOWNLOAD_URL = (
    "https://d.vkr.me/fg/mac/releases/{version}/FloGravity-{version}.dmg"
)
ALLOWED_HOST = "d.vkr.me"
SPARKLE_NAMESPACE = "http://www.andymatuschak.org/xml-namespaces/sparkle"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
BUILD_PATTERN = re.compile(r"^[1-9][0-9]*$")
SYSTEM_VERSION_PATTERN = re.compile(r"^[0-9]+(?:\.[0-9]+){1,2}$")
SRI_PATTERN = re.compile(r"^sha256-[A-Za-z0-9+/]{43}=$")
MAX_APPCAST_BYTES = 1024 * 1024
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
APP_NAME = "浮引.app"
BUNDLE_ID = "me.vkr.fg"
EXECUTABLE_NAME = "浮引"
TEAM_ID = "3MFNWTLLFG"
SIGNING_AUTHORITY = "Developer ID Application: JUN LIU (3MFNWTLLFG)"
SPARKLE_PUBLIC_KEY = "163IhAWnm86c3DmcO88e+QYx0+uL9DCgGMpz/ERge1M="
ED25519_FIELD = 2**255 - 19
ED25519_ORDER = 2**252 + 27742317777372353535851937790883648493
ED25519_D = -121665 * pow(121666, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
ED25519_SQRT_MINUS_ONE = pow(2, (ED25519_FIELD - 1) // 4, ED25519_FIELD)
ED25519_IDENTITY = (0, 1)
EXPECTED_SOURCE_KEYS = {
    "version",
    "bundleVersion",
    "minimumSystemVersion",
    "url",
    "hash",
    "size",
    "sparkleSignature",
}
DEFAULT_SOURCE = (
    Path(__file__).resolve().parents[1] / "packages" / "flogravity-source.json"
)


class UpdateError(RuntimeError):
    pass


class ValidatedRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]):
        super().__init__()
        self.validator = validator

    def redirect_request(self, request, response, code, message, headers, new_url):
        self.validator(new_url)
        return super().redirect_request(
            request,
            response,
            code,
            message,
            headers,
            new_url,
        )


def open_url(request: Request, validator: Callable[[str], None], timeout: int):
    opener = build_opener(ValidatedRedirectHandler(validator))
    return opener.open(request, timeout=timeout)


def version_key(version: str) -> tuple[int, int, int]:
    if not VERSION_PATTERN.fullmatch(version):
        raise UpdateError(f"invalid stable version: {version!r}")
    return tuple(int(part) for part in version.split("."))


def build_number(value: str) -> int:
    if not BUILD_PATTERN.fullmatch(value):
        raise UpdateError(f"invalid bundle version: {value!r}")
    return int(value)


def validate_system_version(value: str) -> None:
    if not SYSTEM_VERSION_PATTERN.fullmatch(value):
        raise UpdateError(f"invalid minimum system version: {value!r}")


def validate_sparkle_signature(value: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError as error:
        raise UpdateError("invalid Sparkle EdDSA signature") from error
    if len(decoded) != 64:
        raise UpdateError("invalid Sparkle EdDSA signature")


def ed25519_recover_x(y_coordinate: int) -> int:
    numerator = (y_coordinate * y_coordinate - 1) % ED25519_FIELD
    denominator = (
        ED25519_D * y_coordinate * y_coordinate + 1
    ) % ED25519_FIELD
    square = numerator * pow(
        denominator,
        ED25519_FIELD - 2,
        ED25519_FIELD,
    ) % ED25519_FIELD
    x_coordinate = pow(
        square,
        (ED25519_FIELD + 3) // 8,
        ED25519_FIELD,
    )
    if x_coordinate * x_coordinate % ED25519_FIELD != square:
        x_coordinate = x_coordinate * ED25519_SQRT_MINUS_ONE % ED25519_FIELD
    if x_coordinate * x_coordinate % ED25519_FIELD != square:
        raise ValueError("point is not on the Ed25519 curve")
    if x_coordinate & 1:
        x_coordinate = ED25519_FIELD - x_coordinate
    return x_coordinate


ED25519_BASE_Y = 4 * pow(5, ED25519_FIELD - 2, ED25519_FIELD) % ED25519_FIELD
ED25519_BASE_POINT = (ed25519_recover_x(ED25519_BASE_Y), ED25519_BASE_Y)


def ed25519_point_add(
    left: tuple[int, int],
    right: tuple[int, int],
) -> tuple[int, int]:
    left_x, left_y = left
    right_x, right_y = right
    product = left_x * right_x * left_y * right_y % ED25519_FIELD
    x_numerator = (left_x * right_y + right_x * left_y) % ED25519_FIELD
    y_numerator = (left_y * right_y + left_x * right_x) % ED25519_FIELD
    x_denominator = (1 + ED25519_D * product) % ED25519_FIELD
    y_denominator = (1 - ED25519_D * product) % ED25519_FIELD
    return (
        x_numerator
        * pow(x_denominator, ED25519_FIELD - 2, ED25519_FIELD)
        % ED25519_FIELD,
        y_numerator
        * pow(y_denominator, ED25519_FIELD - 2, ED25519_FIELD)
        % ED25519_FIELD,
    )


def ed25519_scalar_multiply(
    scalar: int,
    point: tuple[int, int],
) -> tuple[int, int]:
    result = ED25519_IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = ed25519_point_add(result, addend)
        addend = ed25519_point_add(addend, addend)
        scalar >>= 1
    return result


def ed25519_encode_point(point: tuple[int, int]) -> bytes:
    x_coordinate, y_coordinate = point
    encoded = bytearray(y_coordinate.to_bytes(32, "little"))
    encoded[31] |= (x_coordinate & 1) << 7
    return bytes(encoded)


def ed25519_decode_point(encoded: bytes) -> tuple[int, int]:
    if len(encoded) != 32:
        raise ValueError("invalid Ed25519 point length")
    encoded_value = int.from_bytes(encoded, "little")
    sign = encoded_value >> 255
    y_coordinate = encoded_value & ((1 << 255) - 1)
    if y_coordinate >= ED25519_FIELD:
        raise ValueError("non-canonical Ed25519 point")
    x_coordinate = ed25519_recover_x(y_coordinate)
    if x_coordinate & 1 != sign:
        x_coordinate = ED25519_FIELD - x_coordinate
    if x_coordinate == 0 and sign:
        raise ValueError("non-canonical Ed25519 point")
    point = (x_coordinate, y_coordinate)
    left = (y_coordinate * y_coordinate - x_coordinate * x_coordinate) % ED25519_FIELD
    right = (
        1 + ED25519_D * x_coordinate * x_coordinate * y_coordinate * y_coordinate
    ) % ED25519_FIELD
    if left != right or ed25519_encode_point(point) != encoded:
        raise ValueError("invalid Ed25519 point")
    return point


def verify_ed25519_chunks(
    public_key: bytes,
    signature: bytes,
    message_chunks: Iterable[bytes],
) -> bool:
    if len(public_key) != 32 or len(signature) != 64:
        return False
    try:
        public_point = ed25519_decode_point(public_key)
        signature_point = ed25519_decode_point(signature[:32])
    except ValueError:
        return False

    scalar = int.from_bytes(signature[32:], "little")
    if scalar >= ED25519_ORDER:
        return False
    if (
        ed25519_scalar_multiply(ED25519_ORDER, public_point) != ED25519_IDENTITY
        or ed25519_scalar_multiply(ED25519_ORDER, signature_point)
        != ED25519_IDENTITY
        or public_point == ED25519_IDENTITY
        or signature_point == ED25519_IDENTITY
    ):
        return False

    challenge_hash = hashlib.sha512()
    challenge_hash.update(signature[:32])
    challenge_hash.update(public_key)
    for chunk in message_chunks:
        challenge_hash.update(chunk)
    challenge = int.from_bytes(challenge_hash.digest(), "little") % ED25519_ORDER

    left = ed25519_scalar_multiply(scalar, ED25519_BASE_POINT)
    right = ed25519_point_add(
        signature_point,
        ed25519_scalar_multiply(challenge, public_point),
    )
    return hmac.compare_digest(
        ed25519_encode_point(left),
        ed25519_encode_point(right),
    )


def verify_ed25519(public_key: bytes, signature: bytes, message: bytes) -> bool:
    return verify_ed25519_chunks(public_key, signature, [message])


def verify_sparkle_signature(artifact: Path, signature_text: str) -> None:
    validate_sparkle_signature(signature_text)
    public_key = base64.b64decode(SPARKLE_PUBLIC_KEY, validate=True)
    signature = base64.b64decode(signature_text, validate=True)

    def artifact_chunks() -> Iterable[bytes]:
        with artifact.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                yield chunk

    if not verify_ed25519_chunks(public_key, signature, artifact_chunks()):
        raise UpdateError("FloGravity Sparkle Ed25519 signature verification failed")


def validate_https_url(url: str, description: str) -> None:
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as error:
        raise UpdateError(f"invalid {description} URL: {url!r}") from error
    if (
        parsed.scheme != "https"
        or parsed.hostname != ALLOWED_HOST
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.fragment
    ):
        raise UpdateError(f"unexpected {description} URL: {url!r}")


def validate_appcast_url(url: str) -> None:
    validate_https_url(url, "appcast")
    if url != APPCAST_URL:
        raise UpdateError(f"unexpected appcast URL: {url!r}")


def expected_download_url(version: str) -> str:
    version_key(version)
    return DOWNLOAD_URL.format(version=version)


def validate_download_url(url: str, version: str) -> None:
    validate_https_url(url, "download")
    if url != expected_download_url(version):
        raise UpdateError(f"unexpected download URL: {url!r}")


def fetch_appcast() -> bytes:
    validate_appcast_url(APPCAST_URL)
    request = Request(
        APPCAST_URL,
        headers={"User-Agent": "futuping-nix-packages-updater"},
    )
    try:
        with open_url(request, validate_appcast_url, timeout=30) as response:
            final_url = response.geturl()
            validate_appcast_url(final_url)
            payload = response.read(MAX_APPCAST_BYTES + 1)
    except (OSError, URLError) as error:
        raise UpdateError(f"unable to fetch FloGravity appcast: {error}") from error

    if not payload:
        raise UpdateError("FloGravity appcast is empty")
    if len(payload) > MAX_APPCAST_BYTES:
        raise UpdateError("FloGravity appcast exceeded the 1 MiB safety limit")
    return payload


def required_text(parent: ElementTree.Element, tag: str, label: str) -> str:
    element = parent.find(tag)
    value = element.text.strip() if element is not None and element.text else ""
    if not value:
        raise UpdateError(f"missing {label} in FloGravity appcast")
    return value


def parse_appcast(payload: bytes) -> dict[str, object]:
    upper_payload = payload.upper()
    if b"<!DOCTYPE" in upper_payload or b"<!ENTITY" in upper_payload:
        raise UpdateError("FloGravity appcast must not contain a DTD or entities")
    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise UpdateError(f"invalid FloGravity appcast XML: {error}") from error

    items = root.findall("./channel/item")
    if not items:
        raise UpdateError("FloGravity appcast contains no releases")

    releases: list[dict[str, object]] = []
    seen_versions: set[str] = set()
    sparkle = f"{{{SPARKLE_NAMESPACE}}}"

    for item in items:
        version = required_text(
            item,
            f"{sparkle}shortVersionString",
            "short version",
        )
        version_key(version)
        if version in seen_versions:
            raise UpdateError(f"duplicate FloGravity release version: {version}")
        seen_versions.add(version)

        bundle_version = required_text(item, f"{sparkle}version", "bundle version")
        build_number(bundle_version)
        minimum_system_version = required_text(
            item,
            f"{sparkle}minimumSystemVersion",
            "minimum system version",
        )
        validate_system_version(minimum_system_version)

        enclosures = item.findall("enclosure")
        if len(enclosures) != 1:
            raise UpdateError(
                f"expected exactly one enclosure for FloGravity {version}, "
                f"found {len(enclosures)}"
            )
        enclosure = enclosures[0]
        url = enclosure.get("url", "")
        validate_download_url(url, version)

        length_text = enclosure.get("length", "")
        try:
            size = int(length_text)
        except ValueError as error:
            raise UpdateError(f"invalid enclosure length: {length_text!r}") from error
        if not 0 < size <= MAX_DOWNLOAD_BYTES:
            raise UpdateError(f"unexpected enclosure length: {size}")

        media_type = enclosure.get("type", "")
        if media_type not in {
            "application/octet-stream",
            "application/x-apple-diskimage",
        }:
            raise UpdateError(f"unexpected enclosure type: {media_type!r}")

        signature = enclosure.get(f"{sparkle}edSignature", "")
        validate_sparkle_signature(signature)
        releases.append(
            {
                "version": version,
                "bundleVersion": bundle_version,
                "minimumSystemVersion": minimum_system_version,
                "url": url,
                "size": size,
                "sparkleSignature": signature,
            }
        )

    return max(releases, key=lambda release: version_key(str(release["version"])))


def download_artifact(url: str, version: str, size: int, destination: Path) -> str:
    validate_download_url(url, version)
    request = Request(
        url,
        headers={"User-Agent": "futuping-nix-packages-updater"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    validator = lambda candidate: validate_download_url(candidate, version)

    try:
        with open_url(request, validator, timeout=60) as response:
            final_url = response.geturl()
            validate_download_url(final_url, version)

            content_type = (
                response.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type not in {
                "application/octet-stream",
                "application/x-apple-diskimage",
            }:
                raise UpdateError(f"unexpected download content type: {content_type!r}")

            content_length_text = response.headers.get("Content-Length", "")
            try:
                content_length = int(content_length_text)
            except ValueError as error:
                raise UpdateError(
                    f"invalid download content length: {content_length_text!r}"
                ) from error
            if content_length != size:
                raise UpdateError(
                    f"download content length {content_length} does not match "
                    f"appcast length {size}"
                )

            with destination.open("wb") as artifact:
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_DOWNLOAD_BYTES:
                        raise UpdateError("download exceeded the 128 MiB safety limit")
                    digest.update(chunk)
                    artifact.write(chunk)
    except (OSError, URLError) as error:
        raise UpdateError(f"unable to download FloGravity: {error}") from error

    if downloaded != size:
        raise UpdateError(f"downloaded {downloaded} bytes, expected {size} bytes")
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


def inspect_application(
    dmg_path: Path,
    expected_version: str,
    expected_bundle_version: str,
    expected_minimum_system_version: str,
) -> None:
    if sys.platform != "darwin":
        raise UpdateError("FloGravity artifact validation requires macOS")

    with tempfile.TemporaryDirectory(prefix="flogravity-mount-") as temporary:
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
            bundle_version = plist_string(info, "CFBundleVersion")
            if bundle_version != expected_bundle_version:
                raise UpdateError(f"unexpected bundle build: {bundle_version!r}")
            minimum_system_version = plist_string(info, "LSMinimumSystemVersion")
            if minimum_system_version != expected_minimum_system_version:
                raise UpdateError(
                    f"unexpected minimum system version: {minimum_system_version!r}"
                )
            executable_name = plist_string(info, "CFBundleExecutable")
            if executable_name != EXECUTABLE_NAME:
                raise UpdateError(f"unexpected bundle executable: {executable_name!r}")
            sparkle_public_key = plist_string(info, "SUPublicEDKey")
            if sparkle_public_key != SPARKLE_PUBLIC_KEY:
                raise UpdateError("unexpected Sparkle public key in application bundle")

            executable = application / "Contents" / "MacOS" / EXECUTABLE_NAME
            architectures = set(
                command_output(
                    ["/usr/bin/lipo", "-archs", str(executable)],
                    "inspect application architecture",
                ).split()
            )
            if architectures != {"arm64", "x86_64"}:
                raise UpdateError(
                    f"unexpected application architectures: {sorted(architectures)}"
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

            command_output(
                [
                    "/usr/bin/codesign",
                    "--verify",
                    "--deep",
                    "--strict",
                    "--verbose=2",
                    str(application),
                ],
                "verify application signature",
            )
            gatekeeper = command_output(
                [
                    "/usr/sbin/spctl",
                    "--assess",
                    "--type",
                    "execute",
                    "--verbose=4",
                    str(application),
                ],
                "verify application notarization",
            )
            if "source=Notarized Developer ID" not in gatekeeper:
                raise UpdateError(f"unexpected Gatekeeper assessment: {gatekeeper!r}")
        finally:
            had_exception = sys.exc_info()[0] is not None
            detach = run(["/usr/bin/hdiutil", "detach", str(mount_path), "-quiet"])
            if detach.returncode != 0:
                details = detach.stderr.strip()
                if not had_exception:
                    raise UpdateError(f"unable to detach DMG: {details}")
                print(f"warning: unable to detach DMG: {details}", file=sys.stderr)


def sri_sha256(hex_digest: str) -> str:
    return "sha256-" + base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


def read_source(path: Path) -> dict[str, object]:
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise UpdateError(f"unable to read {path}: {error}") from error

    if not isinstance(source, dict) or set(source) != EXPECTED_SOURCE_KEYS:
        keys = sorted(source) if isinstance(source, dict) else []
        raise UpdateError(f"unexpected source keys: {keys}")

    string_keys = EXPECTED_SOURCE_KEYS - {"size"}
    if any(not isinstance(source[key], str) for key in string_keys):
        raise UpdateError("every source value except size must be a string")
    if not isinstance(source["size"], int) or isinstance(source["size"], bool):
        raise UpdateError("source size must be an integer")

    version = str(source["version"])
    version_key(version)
    build_number(str(source["bundleVersion"]))
    validate_system_version(str(source["minimumSystemVersion"]))
    validate_download_url(str(source["url"]), version)
    if not SRI_PATTERN.fullmatch(str(source["hash"])):
        raise UpdateError(f"invalid configured hash: {source['hash']!r}")
    size = int(source["size"])
    if not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise UpdateError(f"unexpected configured size: {size}")
    validate_sparkle_signature(str(source["sparkleSignature"]))
    return source


def write_source(path: Path, source: dict[str, object]) -> None:
    payload = json.dumps(source, indent=2, ensure_ascii=False) + "\n"
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
    release = parse_appcast(fetch_appcast())
    current_version = str(current["version"])
    latest_version = str(release["version"])
    current_key = version_key(current_version)
    latest_key = version_key(latest_version)

    if latest_key < current_key:
        raise UpdateError(
            f"configured version {current_version} is newer than upstream {latest_version}"
        )
    if latest_key > current_key and build_number(str(release["bundleVersion"])) <= build_number(
        str(current["bundleVersion"])
    ):
        raise UpdateError("newer application version did not increase the bundle version")

    with tempfile.TemporaryDirectory(prefix="flogravity-update-") as temporary:
        dmg_path = Path(temporary) / f"FloGravity-{latest_version}.dmg"
        downloaded_digest = download_artifact(
            str(release["url"]),
            latest_version,
            int(release["size"]),
            dmg_path,
        )
        verify_sparkle_signature(dmg_path, str(release["sparkleSignature"]))
        inspect_application(
            dmg_path,
            latest_version,
            str(release["bundleVersion"]),
            str(release["minimumSystemVersion"]),
        )

    latest = {
        "version": latest_version,
        "bundleVersion": str(release["bundleVersion"]),
        "minimumSystemVersion": str(release["minimumSystemVersion"]),
        "url": str(release["url"]),
        "hash": sri_sha256(downloaded_digest),
        "size": int(release["size"]),
        "sparkleSignature": str(release["sparkleSignature"]),
    }

    if latest_key == current_key:
        if latest != current:
            raise UpdateError(
                f"upstream artifact for version {latest_version} changed; "
                "manual review required"
            )
        print(f"FloGravity {latest_version} is already current")
        return False

    if check:
        print(f"FloGravity {latest_version} is available", file=sys.stderr)
        return True

    write_source(path, latest)
    print(f"Updated FloGravity from {current_version} to {latest_version}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Update the official universal FloGravity macOS application"
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
