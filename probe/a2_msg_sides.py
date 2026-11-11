# -*- coding: utf-8 -*-
"""A2 探针：验证消息气泡内层矩形能否区分 self/friend。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import ImageGrab

import uiautomation as uia

from handwechat.controls.main_wnd import MainWnd


def main() -> None:
    wnd = MainWnd()
    wnd.show()
    sl = wnd.session_list()
    if sl is None:
        raise SystemExit("no session list")
    sl.scroll_to_top()
    cell = sl.find_cell("文件传输助手")
    if cell is None:
        raise SystemExit("no target cell")
    cell.Click(simulateMove=False)
    time.sleep(1.2)
    lst = uia.ListControl(wnd.control, AutomationId="chat_message_list", searchDepth=25)
    if not lst.Exists(3, 0.5):
        raise SystemExit("no message list")
    lrect = lst.BoundingRectangle
    cx = (lrect.left + lrect.right) / 2
    print(f"list rect={lrect} center_x={cx:.0f}")
    img = ImageGrab.grab(
        bbox=(lrect.left, lrect.top, lrect.right, lrect.bottom), all_screens=True
    )
    img.save("probe_a2_sides_final.png")
    for item in lst.GetChildren():
        r = item.BoundingRectangle
        cls = (item.ClassName or "").split("::")[-1]
        lefts = []
        rights = []
        for y in range(r.top, min(r.bottom, lrect.bottom), 2):
            row_x = [
                x
                for x in range(r.left, min(r.right, lrect.right), 2)
                if img.getpixel((x - lrect.left, y - lrect.top)) != (255, 255, 255)
            ]
            if row_x:
                lefts.append(row_x[0])
                rights.append(row_x[-1])
        print(
            f"{cls:24s} left={r.left} right={r.right} rows={len(lefts)} "
            f"min_left={min(lefts, default=0)} max_right={max(rights, default=0)} "
            f" {item.Name[:25]!r}"
        )


if __name__ == "__main__":
    main()
