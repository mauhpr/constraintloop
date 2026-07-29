"""Verify the installed Anthropic SDK exposes the request surface we call."""

from __future__ import annotations

import inspect

from anthropic import Anthropic


def main() -> int:
    client = Anthropic(api_key="offline-compatibility-check")
    parameters = inspect.signature(client.messages.create).parameters
    required = {"model", "max_tokens", "system", "messages"}
    missing = sorted(required - parameters.keys())
    if missing:
        print(f"Anthropic SDK messages.create is missing parameters: {missing}")
        return 1
    print("Anthropic SDK messages.create compatibility passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
