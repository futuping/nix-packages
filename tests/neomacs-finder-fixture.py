"""Fake Neomacs for the opt-in LaunchServices check; never loads user config."""

import json
import os
from pathlib import Path
import sys
import time


directory = Path(os.environ["NEOMACS_FINDER_TEST_DIRECTORY"])
record = directory / (str(os.getpid()) + ".json")
temporary = record.with_suffix(".tmp")
temporary.write_text(
    json.dumps({"pid": os.getpid(), "parent": os.getppid(), "arguments": sys.argv[1:]}),
    encoding="utf-8",
)
temporary.replace(record)
deadline = time.monotonic() + 45
while not (directory / "stop").exists() and time.monotonic() < deadline:
    time.sleep(0.05)
