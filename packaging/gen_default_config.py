# -*- coding: utf-8 -*-
"""Write default api_config.json / gui_config.json into a packaged bundle."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve field types through sys.modules, so register it first.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: gen_default_config.py <output_dir>", file=sys.stderr)
        return 2

    target = Path(argv[1])
    target.mkdir(parents=True, exist_ok=True)

    api_config = _load_module(
        "_handwechat_api_config", ROOT / "handwechat" / "api" / "config.py"
    ).ApiConfig()
    gui_config = _load_module(
        "_handwechat_gui_config", ROOT / "handwechat" / "gui" / "config.py"
    ).GuiConfig()

    for fileName, payload in (
        ("api_config.json", api_config.to_dict()),
        ("gui_config.json", gui_config.to_dict()),
    ):
        path = target / fileName
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
