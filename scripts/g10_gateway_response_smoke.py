# -*- coding: utf-8 -*-
"""response 列表状态机冒烟：id、编辑/删除、图片按 name 合并、权限与上限。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handwechat.gateway.protocol import ResponseImage, ResponseItem
from handwechat.gateway.response import ResponseError, ResponseState


def expect_error(action, label: str) -> None:
    try:
        action()
    except ResponseError:
        return
    raise AssertionError(f"未按预期拒绝：{label}")


def texts(state: ResponseState) -> list[str]:
    return [part["text"] for part in state.final_parts() if part.get("text")]


def image_names(state: ResponseState) -> list[str]:
    return [
        part["image"]["name"]
        for part in state.final_parts()
        if part.get("image")
    ]


def scenario_items() -> None:
    state = ResponseState()
    state.apply(
        "a",
        True,
        (
            ResponseItem.new(text="一"),
            ResponseItem.new(text="二"),
        ),
    )
    snapshot = state.snapshot()
    assert [item["id"] for item in snapshot] == ["r-1", "r-2"], snapshot
    assert [item["app"] for item in snapshot] == ["a", "a"], snapshot

    state.apply(
        "b",
        True,
        (
            ResponseItem.keep("r-2"),
            ResponseItem.new(text="三"),
        ),
    )
    snapshot = state.snapshot()
    assert [item["id"] for item in snapshot] == ["r-2", "r-3"], snapshot
    assert snapshot[1]["app"] == "b", snapshot

    state.apply(
        "b",
        True,
        (
            ResponseItem(id="r-2", text="二改"),
            ResponseItem.keep("r-3"),
        ),
    )
    snapshot = state.snapshot()
    assert snapshot[0]["text"] == "二改", snapshot
    assert snapshot[0]["app"] == "a", snapshot
    assert snapshot[0]["edited_by"] == "b", snapshot
    assert texts(state) == ["二改", "三"], texts(state)
    print("PASS response 项：新增、删除、修改、顺序与 edited_by")


def scenario_images() -> None:
    state = ResponseState()
    state.apply(
        "a",
        True,
        (
            ResponseItem.new(
                text="图",
                images=[ResponseImage.from_bytes("a.gif", b"abc")],
            ),
        ),
    )
    snapshot = state.snapshot()
    assert snapshot[0]["images"] == [{"name": "a.gif", "size": 3}], snapshot

    # 保留 a、新增 b
    state.apply(
        "b",
        True,
        (
            ResponseItem(
                id="r-1",
                images=[
                    ResponseImage(name="a.gif"),
                    ResponseImage.from_bytes("b.gif", b"defgh"),
                ],
            ),
        ),
    )
    assert image_names(state) == ["a.gif", "b.gif"], image_names(state)

    # 只列 b，等价于删除 a
    state.apply(
        "b",
        True,
        (ResponseItem(id="r-1", images=[ResponseImage(name="b.gif")]),),
    )
    assert image_names(state) == ["b.gif"], image_names(state)

    # 省略 images：保持不变
    state.apply("b", True, (ResponseItem.keep("r-1"),))
    assert image_names(state) == ["b.gif"], image_names(state)

    # 空数组：删除全部图片，文本保留
    state.apply("b", True, (ResponseItem(id="r-1", images=[]),))
    assert image_names(state) == [], image_names(state)
    assert texts(state) == ["图"], texts(state)
    print("PASS 图片按 name 保留、新增、删除，省略与 [] 语义不同")


def scenario_permissions() -> None:
    state = ResponseState()
    state.apply("a", True, (ResponseItem.new(text="原"),))

    expect_error(
        lambda: state.apply(
            "b", False, (ResponseItem(id="r-1", text="篡改"),)
        ),
        "can_rewrite=false 修改已有项",
    )
    expect_error(
        lambda: state.apply("b", False, (ResponseItem.new(text="new"),)),
        "can_rewrite=false 删除已有项",
    )

    state.apply(
        "b",
        False,
        (ResponseItem.keep("r-1"), ResponseItem.new(text="new")),
    )
    snapshot = state.snapshot()
    assert [item["id"] for item in snapshot] == ["r-1", "r-2"], snapshot
    assert snapshot[1]["app"] == "b", snapshot

    expect_error(
        lambda: state.apply("b", False, (ResponseItem.keep("r-1"),)),
        "can_rewrite=false 删除已有项",
    )
    print("PASS can_rewrite=false 只能追加新项，不能修改或删除已有项")


def scenario_caps() -> None:
    state = ResponseState(max_items=1, max_text=5, max_images=1)
    expect_error(
        lambda: state.apply(
            "a",
            True,
            (ResponseItem.new(text="一"), ResponseItem.new(text="二")),
        ),
        "项数超限",
    )
    expect_error(
        lambda: state.apply("a", True, (ResponseItem.new(text="123456"),)),
        "文本超限",
    )

    state = ResponseState(max_images=1)
    expect_error(
        lambda: state.apply(
            "a",
            True,
            (
                ResponseItem.new(
                    images=[
                        ResponseImage.from_bytes("a.gif", b"a"),
                        ResponseImage.from_bytes("b.gif", b"b"),
                    ]
                ),
            ),
        ),
        "图片数量超限",
    )

    state = ResponseState(max_image_bytes=10)
    big = ResponseImage(
        name="big.gif",
        data=base64.b64encode(b"12345678901").decode("ascii"),
        size=11,
    )
    expect_error(
        lambda: state.apply(
            "a", True, (ResponseItem.new(images=[big]),)
        ),
        "单张图片超限",
    )
    print("PASS 项数、文本、图片数量与单张大小上限")


def scenario_invalid_ids() -> None:
    state = ResponseState()
    state.apply("a", True, (ResponseItem.new(text="原"),))

    expect_error(
        lambda: state.apply("b", True, (ResponseItem.keep("r-9"),)),
        "未知 id",
    )
    expect_error(
        lambda: state.apply(
            "b",
            True,
            (ResponseItem.keep("r-1"), ResponseItem.keep("r-1")),
        ),
        "重复 id",
    )
    print("PASS 未知或重复的 response id 会被拒绝")


def main() -> None:
    scenario_items()
    scenario_images()
    scenario_permissions()
    scenario_caps()
    scenario_invalid_ids()
    print("全部通过")


if __name__ == "__main__":
    main()
