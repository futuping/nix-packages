"""Validate the installed ShardX / ShardBrowser bundle without launching it."""

import sys
from pathlib import Path

from scripts.update_shardx_launcher import (
    RENAME_VERSION,
    UpdateError,
    application_names,
    command_output,
    inspect_bundle,
    version_key,
)


def main():
    package = Path(sys.argv[1])
    version = sys.argv[2]
    app_name, _ = application_names(version)
    applications = list((package / "Applications").glob("*.app"))
    if len(applications) != 1 or applications[0].name != app_name:
        raise UpdateError(f"unexpected installed application bundles: {applications}")
    inspect_bundle(applications[0], version)
    if version_key(version) >= RENAME_VERSION:
        command_output(
            ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(applications[0])],
            "verify the original application signature",
        )
    print(f"Validated installed {app_name} {version}")


if __name__ == "__main__":
    main()
