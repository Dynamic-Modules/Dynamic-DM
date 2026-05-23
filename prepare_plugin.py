from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def main() -> None:
    context_path = Path(required_env("DYNAMIC_MODULES_PREPARE_CONTEXT"))
    output_path = Path(required_env("DYNAMIC_MODULES_PREPARE_OUTPUT"))
    host_root = Path(required_env("DYNAMIC_MODULES_HOST_ROOT"))
    build_dir = Path(required_env("DYNAMIC_MODULES_BUILD_DIR"))
    module_id = os.environ.get("DYNAMIC_MODULES_PREPARE_PLUGIN_MODULE", "dynamic-dm")

    context = json.loads(context_path.read_text(encoding="utf-8"))
    dm_index_path = build_dir / "dm" / "index.json"
    dm_index = {
        "api_version": 1,
        "load_order": context.get("load_order", []),
        "modules": {
            item_id: dm_module_entry(item)
            for item_id, item in context.get("modules", {}).items()
        },
    }
    write_json(dm_index_path, dm_index)

    write_json(
        output_path,
        {
            "generated": {
                "dynamic_dm_index_file": relative_to_host(host_root, dm_index_path),
            },
            "modules": {
                module_id: {
                    "dynamic_dm": {
                        "api_version": 1,
                        "capabilities": ["dm_metadata_index", "dm_patch_conversion"],
                    },
                },
            },
        },
    )


def dm_module_entry(module: dict[str, Any]) -> dict[str, Any]:
    return {
        "dm_files": module.get("dm_files", []),
        "test_files": module.get("test_files", []),
        "hooks": module.get("hooks", []),
        "patches": module.get("patches", []),
        "local_module_patches": module.get("local_module_patches", []),
    }


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def relative_to_host(host_root: Path, path: Path) -> str:
    return path.resolve().relative_to(host_root.resolve()).as_posix()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
