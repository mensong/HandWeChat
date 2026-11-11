# -*- coding: utf-8 -*-
"""A2 探针：查看当前 MainView 的浅层控件状态。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uiautomation as uia
from handwechat.controls.main_wnd import MainWnd


def walk(ctrl, depth=0, max_depth=10):
    print("  " * depth, ctrl.ControlTypeName, ctrl.ClassName,
          repr(ctrl.Name), repr(ctrl.AutomationId), ctrl.BoundingRectangle)
    if depth >= max_depth:
        return
    for child in ctrl.GetChildren():
        walk(child, depth + 1, max_depth)


def main():
    wnd = MainWnd()
    lst = wnd.session_list()
    if lst is None:
        raise SystemExit("session list not found")
    cell = lst.find_cell("文件传输助手")
    if cell is None:
        raise SystemExit("session cell not found")
    walk(cell, max_depth=10)


if __name__ == "__main__":
    main()
