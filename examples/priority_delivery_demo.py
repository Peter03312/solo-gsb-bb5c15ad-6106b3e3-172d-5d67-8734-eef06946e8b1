#!/usr/bin/env python3
"""按保障等级核算各用水点可获流量调用示例（仅依赖标准库，全程只读）。

用法：
    python3 examples/priority_delivery_demo.py [base_url]

默认 base_url 为 http://localhost:8000。

示例管网（examples/sample_network.json）：

    S --P1[阀]-- A --P2[阀]-- B --P4[阀]-- D --P6(无阀)-- U3
                   |           |
                 P3[阀]       P5[阀]
                   |           |
                  U1          U2

脚本依次演示：
  1. 导入管网（版本 1）
  2. 容量充足：U1(等级1)/U2(等级2)/U3(等级2) 各 10 L/h，全部满足
  3. 供水不足：水源只能供 25 -> 等级 1 全额满足，等级 2 按编号逐点分配余量
  4. 同级瓶颈再分配：P3 只有 6 -> U1 逐点最大为 6，等级总量仍最优
  5. 已读版本过期：version_match=false，但仍按当前状态核算
  6. 隔离 P6（关 P4）后再核算：U3 因故障/关阀断供，只交付 U1/U2
  7. 非法请求：编号未覆盖/重复/不存在、等级为 0 或布尔 -> 422 整体拒绝
  8. 确认全程只读：版本仍为 2、阀位/事件不变
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else os.environ.get(
    "BASE_URL", "http://localhost:8000"
)
SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample_network.json")

# sample_network 的全部水源与管段（编号须完整覆盖，不多不少）
SOURCE_CAPS = [{"source_id": "S", "capacity": 1000}]
PIPE_CAPS = [
    {"pipe_id": "P1", "capacity": 100},
    {"pipe_id": "P2", "capacity": 100},
    {"pipe_id": "P3", "capacity": 100},
    {"pipe_id": "P4", "capacity": 100},
    {"pipe_id": "P5", "capacity": 100},
    {"pipe_id": "P6", "capacity": 100},
]
# U1 保障等级 1（最高），U2/U3 保障等级 2
DEMANDS = [
    {"sink_id": "U1", "demand": 10, "level": 1},
    {"sink_id": "U2", "demand": 10, "level": 2},
    {"sink_id": "U3", "demand": 10, "level": 2},
]


def call(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def show(title: str, status: int, body: dict) -> None:
    print(f"\n=== {title} -> HTTP {status} ===")
    print(json.dumps(body, ensure_ascii=False, indent=2))


def priority(version, sources, pipes, demands):
    return call(
        "POST",
        "/api/delivery/priority",
        {"version": version, "sources": sources, "pipes": pipes, "demands": demands},
    )


def delivered_map(body):
    return {d["sink_id"]: d["delivered"] for d in body["deliveries"]}


def main() -> None:
    with open(SAMPLE, encoding="utf-8") as f:
        network = json.load(f)

    # 1. 导入管网
    status, body = call("POST", "/api/network/import", network)
    show("1. 导入管网", status, body)
    assert status == 200 and body["version"] == 1, body

    # 2. 容量充足：全部满足
    status, body = priority(1, SOURCE_CAPS, PIPE_CAPS, DEMANDS)
    show("2. 容量充足（需求 30，全部满足）", status, body)
    assert status == 200, body
    assert body["total_demand"] == 30
    assert body["delivered_total"] == 30
    assert body["all_satisfied"] is True
    assert body["version_match"] is True
    assert delivered_map(body) == {"U1": 10, "U2": 10, "U3": 10}, body

    # 3. 供水不足：水源只能供 25
    # 等级 1 的 U1 全额 10；等级 2 合计 15，按编号 U2 先逐点最大化
    status, body = priority(
        1, [{"source_id": "S", "capacity": 25}], PIPE_CAPS, DEMANDS
    )
    show("3. 水源上限 25（等级 1 优先，等级 2 按编号分配余量）", status, body)
    assert delivered_map(body) == {"U1": 10, "U2": 10, "U3": 5}, body
    assert body["delivered_total"] == 25 and body["all_satisfied"] is False, body
    shortfalls = {d["sink_id"]: d["shortfall"] for d in body["deliveries"]}
    assert shortfalls == {"U1": 0, "U2": 0, "U3": 5}, body

    # 4. 同级瓶颈再分配：P3（U1 唯一通道）只有 6，U1/U2 同为等级 1
    tight_pipes = [
        {"pipe_id": "P1", "capacity": 100},
        {"pipe_id": "P2", "capacity": 100},
        {"pipe_id": "P3", "capacity": 6},
        {"pipe_id": "P4", "capacity": 100},
        {"pipe_id": "P5", "capacity": 100},
        {"pipe_id": "P6", "capacity": 100},
    ]
    status, body = priority(
        1,
        [{"source_id": "S", "capacity": 15}],
        tight_pipes,
        [
            {"sink_id": "U1", "demand": 10, "level": 1},
            {"sink_id": "U2", "demand": 10, "level": 1},
        ],
    )
    show("4. 同级逐点最大（P3=6：U1 得 6、U2 得 9，等级总量 15 最优）", status, body)
    assert delivered_map(body) == {"U1": 6, "U2": 9}, body

    # 5. 过期版本：仍按当前状态核算
    status, body = priority(99, SOURCE_CAPS, PIPE_CAPS, DEMANDS)
    show("5. 已读版本过期（version_match=false，仍按当前版本 1 核算）", status, body)
    assert body["version"] == 1 and body["version_match"] is False, body
    assert body["all_satisfied"] is True, body

    # 6. 隔离 P6（关 P4）后：U3 断供，U1/U2 不受影响
    status, body = call(
        "POST",
        "/api/isolation/submit",
        {"fault_pipe_id": "P6", "close_pipe_ids": ["P4"], "version": 1},
    )
    show("6a. 提交隔离 P6（关 P4，版本 -> 2）", status, body)
    assert status == 200 and body["version"] == 2, body

    status, body = priority(2, SOURCE_CAPS, PIPE_CAPS, DEMANDS)
    show("6b. 隔离后核算（P4 已关、P6 待修复；U3 不可交付）", status, body)
    assert delivered_map(body) == {"U1": 10, "U2": 10, "U3": 0}, body
    assert body["all_satisfied"] is False, body

    # 7. 非法请求（整体拒绝，返回全部问题明细）
    bad = {
        "version": 2,
        # 水源未覆盖（缺 S）且编号重复/不存在
        "sources": [
            {"source_id": "SX", "capacity": 10},
            {"source_id": "SX", "capacity": 10},
        ],
        # 管段未覆盖（缺 P6）、重复（P1）、不存在（PX）
        "pipes": [
            {"pipe_id": "P1", "capacity": 100},
            {"pipe_id": "P1", "capacity": 100},
            {"pipe_id": "PX", "capacity": 100},
            {"pipe_id": "P2", "capacity": 100},
            {"pipe_id": "P3", "capacity": 100},
            {"pipe_id": "P4", "capacity": 100},
            {"pipe_id": "P5", "capacity": 100},
        ],
        # 用水点重复（U1）、不是用水点（S）、不存在（UX）
        "demands": [
            {"sink_id": "U1", "demand": 1, "level": 1},
            {"sink_id": "U1", "demand": 1, "level": 1},
            {"sink_id": "S", "demand": 1, "level": 1},
            {"sink_id": "UX", "demand": 1, "level": 1},
        ],
    }
    status, body = call("POST", "/api/delivery/priority", bad)
    show("7a. 编号未覆盖/重复/不存在 -> 422 DELIVERY_PRIORITY_INVALID", status, body)
    assert status == 422 and body["error"]["code"] == "DELIVERY_PRIORITY_INVALID", body

    bad_type = {
        "version": 2,
        "sources": [{"source_id": "S", "capacity": 1000}],
        "pipes": PIPE_CAPS,
        # 等级为 0（必须为正整数）、需求为布尔（必须为非负整数）
        "demands": [
            {"sink_id": "U1", "demand": 1, "level": 0},
            {"sink_id": "U2", "demand": True, "level": 1},
        ],
    }
    status, body = call("POST", "/api/delivery/priority", bad_type)
    show("7b. 类型/数值错误（等级 0、布尔需求）-> 422", status, body)
    assert status == 422, body

    # 8. 只读核对：版本仍为 2、只有隔离事件 1、P4 仍关闭（核算未改任何状态）
    status, net = call("GET", "/api/network")
    assert status == 200 and net["version"] == 2, net
    status, events = call("GET", "/api/isolation/events")
    assert status == 200 and len(events) == 1 and events[0]["fault_pipe_id"] == "P6"
    print(
        "\n8. 只读核对通过：版本仍为 2，仅 1 条隔离事件（P6），"
        "阀位未被核算接口改动。"
    )
    print("\n全部演示完成。")


if __name__ == "__main__":
    main()
