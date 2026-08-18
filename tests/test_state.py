from __future__ import annotations

import json
from pathlib import Path

from constraintloop.state import (
    load_ratchet_baseline,
    load_ratchet_baseline_digest,
    save_ratchet_baseline,
)


def test_ratchet_baseline_loaders_reject_escaped_paths(tmp_path: Path) -> None:
    assert load_ratchet_baseline(tmp_path, "../outside.json", "count") is None
    assert load_ratchet_baseline_digest(tmp_path, "../outside.json", "count") is None


def test_ratchet_baseline_writer_repairs_invalid_shapes(tmp_path: Path) -> None:
    path = tmp_path / "baselines.json"
    path.write_text("[]", encoding="utf-8")
    save_ratchet_baseline(tmp_path, "baselines.json", "first", 1)
    assert load_ratchet_baseline(tmp_path, "baselines.json", "first") == 1

    path.write_text(json.dumps({"ratchets": []}), encoding="utf-8")
    save_ratchet_baseline(tmp_path, "baselines.json", "second", 2)
    assert load_ratchet_baseline(tmp_path, "baselines.json", "second") == 2
