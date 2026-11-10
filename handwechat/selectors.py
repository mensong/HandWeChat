# -*- coding: utf-8 -*-
"""控件定位规则 —— 唯一的微信版本适配点。

所有规则取自微信 4.1.15.56 真机 UIA dump（docs/uia_tree/main_window.txt）。
微信升级后若自检失败，用 probe/m0_uia_dump.py 重新 dump 并 diff 本文件。
"""

from __future__ import annotations

import re

# ---- 窗口 ---------------------------------------------------------------
# win32 类是 Qt51514QWindowIcon，UIA 层类名是 mmui::MainWindow
MAIN_WINDOW_CLASS = "mmui::MainWindow"
MAIN_WINDOW_TITLE = "微信"
LOGIN_WINDOW_CLASS = "mmui::LoginWindow"

# ---- 主窗口内部（4.1.15.56 实测） ---------------------------------------
SESSION_LIST_CLASS = "mmui::XTableView"
SESSION_LIST_ID = "session_list"
SESSION_CELL_CLASS = "mmui::ChatSessionCell"
SESSION_CELL_ID_PREFIX = "session_item_"
MAIN_TABBAR_CLASS = "mmui::MainTabBar"
CHAT_PAGE_CLASS = "mmui::ChatPage"

CHAT_MESSAGE_PAGE_ID = "chat_message_page"
MESSAGE_LIST_ID = "chat_message_list"
MESSAGE_TIME_ITEM_CLASS = "mmui::ChatItemView"
MESSAGE_TEXT_ITEM_CLASS = "mmui::ChatTextItemView"
MESSAGE_BUBBLE_ITEM_CLASS = "mmui::ChatBubbleItemView"
CHAT_INPUT_FIELD_ID = "chat_input_field"
CHAT_TITLE_LABEL_ID = (
    "content_view.top_content_view.title_h_view.left_v_view."
    "left_content_v_view.left_ui_.big_title_line_h_view."
    "current_chat_name_label"
)

GROUP_TITLE_RE = re.compile(r"^(.*?)\s*\((\d+)\)\s*$")

# ---- 搜索（Ctrl+F 弹出的全局搜索） ---------------------------------------
# 4.1.15.56 实测：搜索框本体不是 EditControl，搜索文本直接发给主窗口即可。
SEARCH_POPOVER_CLASS = "mmui::SearchContentPopover"
SEARCH_LIST_ID = "search_list"
SEARCH_ITEM_CLASS = "mmui::SearchContentCellView"
SEARCH_ITEM_ID_PREFIX = "search_item_"

# ---- 会话格子 Name 文本标记（4.1.15.56 实测格式） ------------------------
# 格式为多行文本：会话名 / [已置顶] / ([有人@我] )([N条] )预览 / 时间
CELL_PINNED_MARK = "已置顶"
CELL_AT_ME_MARK = "[有人@我]"
CELL_UNREAD_RE = re.compile(r"^\[(\d+)条\]")
