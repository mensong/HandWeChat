# -*- coding: utf-8 -*-
"""A2 探针：点开指定会话，dump ChatPage 完整控件树。

用法: python -X utf8 probe/a2_chat_dump.py [会话名]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uiautomation as uia

from handwechat.controls.main_wnd import MainWnd


def walk(ctrl, depth=0, maxd=14, out=None):
    try:
        out.append(
            f"{'  ' * depth}{ctrl.ControlTypeName} class={ctrl.ClassName!r} "
            f"name={ctrl.Name!r} id={ctrl.AutomationId!r} rect={ctrl.BoundingRectangle}"
        )
    except Exception as e:
        out.append(f"{'  ' * depth}<error: {e}>")
        return
    if depth >= maxd:
        out.append(f"{'  ' * depth}...")
        return
    for c in ctrl.GetChildren():
        walk(c, depth + 1, maxd, out)


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "文件传输助手"
    wnd = MainWnd()
    lst = wnd.session_list()
    if lst is None:
        raise SystemExit("session list not found")
    cell = lst.find_cell(name)
    if cell is None:
        raise SystemExit(f"session cell not found: {name!r}")
    cell.Click(simulateMove=False)
    import time
    time.sleep(1.0)

    chat = uia.GroupControl(wnd.control, ClassName="mmui::ChatMessagePage", searchDepth=20)
    if not chat.Exists(3, 0.5):
        raise SystemExit("ChatPage not found")
    out = []
    walk(chat, out=out)
    out_dir = Path(__file__).resolve().parent.parent / "docs" / "uia_tree"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "chat_page.txt"
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"dumped {len(out)} lines -> {path}")


if __name__ == "__main__":
    main()
