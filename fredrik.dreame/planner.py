"""Tool-free Luna planning using the existing cleaning-program contract."""

import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile

from cloud import CloudError, compact, require, strict_json


CONTRACT = Path.home() / "Work/dreame-local/android/cleaning-program.mjs"
MODEL = "gpt-6-luna"


def run_bounded(command, text=None, *, cwd=None, timeout=180, limit=65536, validation=False):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                cwd=cwd, start_new_session=True, text=True,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except OSError as error:
            raise CloudError("PLANNER_UNAVAILABLE", "The planner command could not be started; check Node and Copilot CLI.") from error
        try:
            process.communicate(text, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise CloudError("PLANNER_TIMEOUT", "Luna planning timed out. No cleaning was started.") from error
        if process.returncode:
            if validation:
                stderr.seek(0)
                detail = stderr.read(4096).decode("utf-8", errors="replace")
                raise CloudError("INVALID_PLAN", "Luna's response failed the cleaning contract. No cleaning was started. " + detail)
            raise CloudError("PLANNER_FAILED", "Planning or strict JSON validation failed. Check the Copilot login/model and the requested capability constraints. No cleaning was started.")
        if stdout.tell() > limit:
            raise CloudError("INVALID_PLAN", "The planner response exceeded its size limit. No cleaning was started.")
        stdout.seek(0)
        try:
            return stdout.read().decode("utf-8")
        except UnicodeError as error:
            raise CloudError("INVALID_PLAN", "The planner did not return UTF-8 text.") from error


def model_answer(output):
    try:
        events = [strict_json(line) for line in output.splitlines() if line.strip()]
    except (ValueError, UnicodeError) as error:
        raise CloudError("INVALID_PLAN", "Luna returned invalid JSONL events. No cleaning was started.") from error
    messages = []
    for event in events:
        if not isinstance(event, dict):
            raise CloudError("INVALID_PLAN", "Luna returned an invalid event.")
        kind = event.get("type")
        if not isinstance(kind, str):
            raise CloudError("INVALID_PLAN", "Luna returned an invalid event type.")
        if kind.startswith("tool."):
            raise CloudError("PLANNER_TOOL_REQUEST", "The data-only planner attempted to use a tool. No cleaning was started.")
        if event.get("type") == "assistant.message":
            data = event.get("data", {})
            if not isinstance(data, dict) or data.get("toolRequests") or data.get("model") != MODEL or not isinstance(data.get("content"), str):
                raise CloudError("INVALID_PLAN", "Luna returned an unexpected model response.")
            messages.append(data["content"])
    if len(messages) != 1:
        raise CloudError("INVALID_PLAN", "Luna did not return one unambiguous final JSON response.")
    return messages[0]


def plan(prompt, request_id, private_root):
    require(isinstance(prompt, str) and 1 <= len(prompt.strip()) <= 4000, "Enter a cleaning request of at most 4000 characters.")
    if not CONTRACT.is_file() or not shutil.which("node") or not shutil.which("copilot"):
        raise CloudError("PLANNER_UNAVAILABLE", "The existing cleaning contract, Node and signed-in Copilot CLI are required.")
    contract = run_bounded(["node", str(CONTRACT), "prompt"], timeout=15)
    request = contract + "\nUSER_REQUEST=" + compact({"requestId": request_id, "utterance": prompt})
    command = [
        "copilot", "--model", MODEL, "--available-tools=", "--disable-builtin-mcps",
        "--no-custom-instructions", "--no-ask-user",
        "--no-auto-update", "--no-remote", "--no-remote-export", "--log-level", "none",
        "--silent", "--stream", "off", "--no-color", "--output-format", "json",
    ]
    configuration = Path.home() / ".copilot/mcp-config.json"
    if configuration.is_file():
        try:
            config = strict_json(configuration.read_text())
        except (ValueError, OSError, UnicodeError) as error:
            raise CloudError("PLANNER_CONFIGURATION", "The MCP configuration could not be read safely to disable its servers.") from error
        servers = config.get("mcpServers") if isinstance(config, dict) else None
        if not isinstance(servers, dict) or not all(isinstance(name, str) and name and not name.startswith("-") for name in servers):
            raise CloudError("PLANNER_CONFIGURATION", "The MCP server configuration has an unexpected format.")
        for name in servers:
            command.extend(["--disable-mcp-server", name])
    with tempfile.TemporaryDirectory(prefix="planner-", dir=private_root) as directory:
        output = model_answer(run_bounded(command, request, cwd=directory, limit=2 * 1024 * 1024))
    validated = run_bounded(["node", str(CONTRACT), "validate", request_id], output, timeout=15, validation=True)
    if len(validated.encode()) > 32768:
        raise CloudError("INVALID_PLAN", "The validated program is too large. No cleaning was started.")
    return strict_json(validated)
