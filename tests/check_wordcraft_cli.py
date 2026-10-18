"""Native headless package check, run with the flake's pinned Python.

Exercises the packaged executable through MCP and only creates disposable
documents. It neither starts the desktop app nor reads user documents.
"""

import argparse
import asyncio
import base64
import json
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET
import zipfile


async def run_check(executable):
    with tempfile.TemporaryDirectory(prefix="wordcraft-mcp-check-") as temporary:
        root = Path(temporary)
        with tempfile.TemporaryFile() as stderr:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                "mcp",
                "--author", "Codex",
                "--automation-read-root", str(root),
                "--automation-write-root", str(root),
                cwd=root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
                limit=32 * 1024 * 1024,
            )
            request_id = 0

            async def rpc(method, params=None, notification=False):
                nonlocal request_id
                request_id += 1
                message = {"jsonrpc": "2.0", "method": method}
                if not notification:
                    message["id"] = request_id
                if params is not None:
                    message["params"] = params
                process.stdin.write((json.dumps(message) + "\n").encode())
                await process.stdin.drain()
                if notification:
                    return None
                while True:
                    raw = await asyncio.wait_for(process.stdout.readline(), timeout=45)
                    if not raw:
                        await process.wait()
                        stderr.seek(0)
                        detail = stderr.read().decode(errors="replace")
                        raise RuntimeError(
                            "MCP server exited %s: %s" % (process.returncode, detail)
                        )
                    response = json.loads(raw)
                    if response.get("id") != request_id:
                        continue
                    if "error" in response:
                        raise RuntimeError("%s: %s" % (method, response["error"]))
                    return response["result"]

            async def tool(name, arguments=None, raw=False):
                result = await rpc(
                    "tools/call", {"name": name, "arguments": arguments or {}}
                )
                assert not result.get("isError"), "%s: %s" % (name, result)
                if raw:
                    return result
                text = "\n".join(
                    item["text"] for item in result["content"]
                    if item.get("type") == "text"
                )
                return json.loads(text)

            try:
                initialization = await rpc(
                    "initialize",
                    {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "wordcraft-package-check", "version": "1"},
                    },
                )
                assert initialization["serverInfo"]["name"] == "wordcraft", initialization
                assert initialization["protocolVersion"] == "2025-06-18", initialization
                assert "headless session (no window)" in initialization["instructions"]
                await rpc("notifications/initialized", notification=True)
                tools = (await rpc("tools/list", {}))["tools"]
                tool_names = {item["name"] for item in tools}
                required = {
                    "list_commands", "new_document", "type_text", "execute", "get_text",
                    "inspect_document", "save_document", "render_page", "open_document",
                }
                assert required <= tool_names, required - tool_names
                commands = await tool("list_commands", {"query": "review.trackChanges"})
                assert any(item["id"] == "review.trackChanges" for item in commands), commands
                print("PASS: headless MCP handshake and %s tools" % len(tools))

                await tool("new_document", {"template": "blank"})
                # --author names edits; it does not enable tracked changes.
                await tool(
                    "execute", {"command": "review.trackChanges", "params": {"value": True}}
                )
                lines = ["WordCraft headless MCP verification.", "Tracked insertion by Codex."]
                await tool("type_text", {"text": "\n".join(lines), "paragraphs": True})
                text = (await tool("get_text"))["text"]
                assert all(line in text for line in lines), text
                inspected = await tool("inspect_document")
                assert inspected["trackChanges"] is True, inspected
                assert inspected["pages"] >= 1, inspected
                assert [item["text"] for item in inspected["blocks"]] == lines, inspected
                document_path = root / "verification.docx"
                saved = await tool("save_document", {"path": str(document_path)})
                assert saved["saved"] is True, saved
                assert document_path.stat().st_size > 0

                namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
                namespaces = {"w": namespace}
                with zipfile.ZipFile(document_path) as archive:
                    document = ET.fromstring(archive.read("word/document.xml"))
                    insertions = document.findall(".//w:ins", namespaces)
                    authors = {item.get("{" + namespace + "}author") for item in insertions}
                    assert insertions and authors == {"Codex"}, authors
                    settings = ET.fromstring(archive.read("word/settings.xml"))
                    assert settings.find(".//w:trackRevisions", namespaces) is not None
                await tool("open_document", {"path": str(document_path)})
                reopened = await tool("inspect_document")
                assert reopened["trackChanges"] is True, reopened
                assert (await tool("get_text"))["text"] == text
                print("PASS: DOCX save/reopen, tracked changes and Codex revision author")

                rendered = await tool("render_page", {"page": 1, "scale": 0.75}, raw=True)
                picture = next(
                    item for item in rendered["content"] if item.get("type") == "image"
                )
                assert picture["mimeType"] == "image/png", picture["mimeType"]
                png = base64.b64decode(picture["data"], validate=True)
                assert png.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid rendered PNG signature"
                assert png[12:16] == b"IHDR", "Missing PNG image header"
                width = int.from_bytes(png[16:20], "big")
                height = int.from_bytes(png[20:24], "big")
                assert width > 100 and height > 100, (width, height)
                print("PASS: page rendered to PNG (%s x %s)" % (width, height))
            finally:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
            assert process.returncode == 0, "MCP server exit status: %s" % process.returncode
    print("PASS: MCP server exited cleanly; temporary documents removed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, help="packaged wordcraft-cli executable")
    args = parser.parse_args()
    asyncio.run(run_check(args.executable.resolve(strict=True)))
