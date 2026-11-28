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
import sys


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
