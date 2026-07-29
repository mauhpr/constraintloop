"""Verify the installed OpenAI SDK exposes the request surface we call."""

from __future__ import annotations

import inspect

from openai import OpenAI


def main() -> int:
    client = OpenAI(api_key="offline-compatibility-check")
    parameters = inspect.signature(client.responses.parse).parameters
    required = {
        "model",
        "instructions",
        "input",
        "max_output_tokens",
        "reasoning",
        "text_format",
    }
    missing = sorted(required - parameters.keys())
    if missing:
        print(f"OpenAI SDK responses.parse is missing parameters: {missing}")
        return 1
    print("OpenAI SDK responses.parse compatibility passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
