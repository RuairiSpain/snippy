#!/usr/bin/env python3
"""Example in-sandbox agent runner. Bake it into the sandbox disk image at /opt/agent/.

Contract with the session manager (agent.command in config.yaml):
  argv[1]  path of a JSON file: {"conversation_id": str, "messages": [{"role", "content"}, ...]}
  stdout   last non-empty line is one JSON object:
           {"reply": str, "input_tokens": int, "output_tokens": int}
  exit     0 on success; anything else is reported to the caller as HTTP 502.

The process runs as the calling user's Unix account with HOME set to that user's private
directory. Keep per-user files (memory, caches, scratch work) under $HOME. Do not put
model API keys in the image or in this process's environment. Route model calls through
the sandbox group's egress policy (credential injection) or an API gateway.
"""

import json
import os
import sys


def obo_token(alias: str) -> str:
    """Delegated access token for a downstream API, acting as the calling user.

    Present only when the request asked for it (`obo_resources`). The file is readable by
    this user only and is deleted when the turn ends, so call this during the turn and do
    not copy the token to disk. Use it as `Authorization: Bearer <token>`."""
    with open(os.environ["OBO_TOKENS_FILE"], encoding="utf-8") as f:
        return json.load(f)[alias]["access_token"]


def bearer_headers(alias: str) -> dict[str, str]:
    """Headers for calling a downstream API as the signed-in user, e.g. an HTTP MCP server:
    `{"Authorization": "Bearer <delegated token>"}`."""
    return {"Authorization": f"Bearer {obo_token(alias)}"}


def mcp_config() -> dict | None:
    """The MCP client configuration for the connectors attached to this sandbox, if the
    operator set connectors.config_file. The format is whatever the platform wrote."""
    path = os.environ.get("MCP_CONFIG_FILE")
    if not path:
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def event(request: dict) -> dict | None:
    """The connector event that started this run (an email, a chat message), if any.

    UNTRUSTED: someone outside your organization may have written it. It is data to analyse,
    never instructions to follow. Keep it out of the system prompt, quote it as data when you
    show it to a model, and do not let it choose which tools to run or where to send results.
    `event["fields"]` has the normalized values (from, subject, body, ...)."""
    return request.get("event")


def input_files(request: dict) -> list[dict]:
    """Files the caller attached to this request, if any. Each has `name`, `path`, `size`,
    `sha256` and `content_type`. They are read-only and live in $INPUT_DIR for this turn
    only; copy what you need into $HOME."""
    return request.get("input_files", [])


def output_dir() -> str:
    """Write files you want to return to the caller here ($OUTPUT_DIR). Regular files only,
    in this directory or below it. They are collected after the turn and the directory is
    emptied, so each turn returns only its own files."""
    return os.environ["OUTPUT_DIR"]


def main() -> int:
    with open(sys.argv[1], encoding="utf-8") as f:
        request = json.load(f)
    last = request["messages"][-1]["content"]

    # Replace this with a real agent loop (model call, tool execution in $HOME, ...).
    reply = f"echo: {last}"

    print(json.dumps({"reply": reply, "input_tokens": 0, "output_tokens": 0}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
