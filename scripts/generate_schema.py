"""Generate the editor-facing schema from the authoritative Pydantic contract."""

from __future__ import annotations

import json
from pathlib import Path

from constraintloop.models import Contract


def schema_document() -> dict[str, object]:
    generated = Contract.model_json_schema()
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://raw.githubusercontent.com/mauhpr/constraintloop/main/schema/constraintloop.schema.json",
        **generated,
    }


def main() -> None:
    root = Path(__file__).parents[1]
    path = root / "schema" / "constraintloop.schema.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(schema_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(path.relative_to(root))


if __name__ == "__main__":
    main()
