"""测试/生产运行指纹测试。"""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import json
import subprocess
import sys


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
CHIBA_ROOT = PLUGIN_ROOT.parents[1]
SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "runtime_fingerprint.py"
DIFF_SCRIPT_PATH = PLUGIN_ROOT / "scripts" / "diff_runtime_fingerprints.py"


def _load_fingerprint_module() -> Any:
    spec = spec_from_file_location("meme_runtime_fingerprint", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mapping_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        result = [str(key).lower() for key in value]
        for item in value.values():
            result.extend(_mapping_keys(item))
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            result.extend(_mapping_keys(item))
        return result
    return []


def test_runtime_fingerprint_contains_behavior_critical_inputs() -> None:
    module = _load_fingerprint_module()
    payload = module.build_fingerprint(
        chiba_root=CHIBA_ROOT,
        plugin_root=PLUGIN_ROOT,
        environment="test",
        release_id="reviewed-semantic-meme-library-20260729-multiprototype-v1",
    )
    compatibility = payload["compatibility"]
    assert len(payload["compatibility_sha256"]) == 64
    assert compatibility["plugin"]["id"] == "chiba.meme-semantic-plugin"
    assert compatibility["plugin"]["config"]["plugin"]["enabled"] is True
    assert compatibility["release"]["card_count"] == 27
    assert set(compatibility["model_routing"]["tasks"]) == {
        "planner",
        "replyer",
        "embedding",
        "utils",
    }
    keys = _mapping_keys(payload)
    assert "api_key" not in keys
    assert "authorization" not in keys
    assert "password" not in keys


def test_identical_compatibility_matches_across_environment_labels(
    tmp_path: Path,
) -> None:
    module = _load_fingerprint_module()
    left = module.build_fingerprint(
        chiba_root=CHIBA_ROOT,
        plugin_root=PLUGIN_ROOT,
        environment="staging",
        release_id="reviewed-semantic-meme-library-20260729-multiprototype-v1",
    )
    right = dict(left)
    right["environment"] = "production"
    right["captured_at"] = "later"
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    left_path.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
    right_path.write_text(json.dumps(right, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT_PATH), str(left_path), str(right_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    result = json.loads(completed.stdout)
    assert result["match"] is True
    assert result["difference_count"] == 0


def test_behavior_difference_blocks_promotion(tmp_path: Path) -> None:
    module = _load_fingerprint_module()
    left = module.build_fingerprint(
        chiba_root=CHIBA_ROOT,
        plugin_root=PLUGIN_ROOT,
        environment="staging",
        release_id="reviewed-semantic-meme-library-20260729-multiprototype-v1",
    )
    right = json.loads(json.dumps(left))
    right["environment"] = "production"
    right["compatibility"]["plugin"]["config"]["serving"][
        "minimum_similarity"
    ] = 0.99
    left_path = tmp_path / "left.json"
    right_path = tmp_path / "right.json"
    left_path.write_text(json.dumps(left, ensure_ascii=False), encoding="utf-8")
    right_path.write_text(json.dumps(right, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(DIFF_SCRIPT_PATH), str(left_path), str(right_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 1
    result = json.loads(completed.stdout)
    assert result["match"] is False
    assert any(
        difference["path"].endswith("serving.minimum_similarity")
        for difference in result["differences"]
    )
