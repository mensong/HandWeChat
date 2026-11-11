# -*- coding: utf-8 -*-
"""M0 probe: dump WeChat 4.x main window UIA tree for analysis.

Output: docs/uia_tree/main_window.txt
"""
import sys
import time
from pathlib import Path

import uiautomation as uia

WIN_CLASS = "mmui::MainWindow"  # UIA-level class; win32 class is Qt51514QWindowIcon
WIN_NAME = "\u5fae\u4fe1"  # WeChat main window title
OUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "uia_tree"


def find_main_window(timeout: float = 10.0) -> uia.WindowControl:
    win = uia.WindowControl(ClassName=WIN_CLASS, Name=WIN_NAME, searchDepth=1)
    if not win.Exists(timeout, 0.5):
        raise SystemExit(f"main window not found (class={WIN_CLASS!r}); is WeChat running and visible?")
    return win


def summarize_nav(win: uia.WindowControl) -> str:
    lines = ["== top-level children =="]
    for child in win.GetChildren():
        lines.append(f"  {child.ControlTypeName} class={child.ClassName!r} name={child.Name!r} rect={child.BoundingRectangle}")
    lines.append("")
    lines.append("== all mmui::XTab* controls ==")

    def walk(ctrl, depth=0):
        try:
            if ctrl.ClassName and "XTab" in ctrl.ClassName:
                lines.append(f"  depth={depth} {ctrl.ControlTypeName} class={ctrl.ClassName!r} name={ctrl.Name!r} enabled={ctrl.IsEnabled}")
        except Exception as e:
            lines.append(f"  walk error at depth={depth}: {e}")
            return
        for c in ctrl.GetChildren():
            walk(c, depth + 1)

    walk(win)
    return "\n".join(lines)


def main() -> None:
    win = find_main_window()
    print("attached:", win.ClassName, win.Name, win.BoundingRectangle)
    time.sleep(1.0)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = OUT_DIR / "main_window.txt"
    with dump_path.open("w", encoding="utf-8") as f:
        for ctrl, depth in uia.WalkControl(win, includeTop=True, maxDepth=25):
            try:
                line = f"{'  ' * depth}{ctrl.ControlTypeName} class={ctrl.ClassName!r} name={ctrl.Name!r} id={ctrl.AutomationId!r} rect={ctrl.BoundingRectangle}"
            except Exception as e:
                line = f"{'  ' * depth}<error: {e}>"
            f.write(line + "\n")
    print(f"full tree dumped -> {dump_path}")

    summary_path = OUT_DIR / "nav_summary.txt"
    summary_path.write_text(summarize_nav(win), encoding="utf-8")
    print(f"nav summary -> {summary_path}")
    print(summary_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()