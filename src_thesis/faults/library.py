"""Nạp và tiêm kịch bản lỗi — một nguồn duy nhất cho cả chạy tay lẫn chạy tự động.

VÌ SAO TÁCH RA THÀNH FILE RIÊNG: `scripts/inject.py` (chạy tay, dùng từ phase 2) và
`src_thesis/eval/runner.py` (chạy 75 ca của phase 6) bắt buộc phải tiêm lỗi **giống
hệt nhau**. Nếu để hai bản sao của cùng một logic thì chúng trôi dần khỏi nhau, và
lúc đó số liệu phase 6 không còn so được với các lần chạy tay đã ghi trong
`docs/thesis-notes.md` — mà toàn bộ chương kết quả dựa trên việc hai bên so được.

Đây là cùng một bài học với bản vá `emailservice` ở phase 4: giống nhau giữa hai
đường chạy chính là điều kiện để con số có nghĩa.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from src_thesis.faults.injectors import ActiveFault, FaultInjector

SCENARIOS_FILE = (
    Path(__file__).resolve().parents[2] / "src_thesis" / "faults" / "scenarios.yaml"
)


def load_scenarios(path: Path = SCENARIOS_FILE) -> dict[str, dict]:
    """Đọc `scenarios.yaml`, trả về map từ mã kịch bản sang định nghĩa."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {s["id"]: s for s in data["scenarios"]}


def recommended_order(path: Path = SCENARIOS_FILE) -> list[str]:
    """Thứ tự chạy đề nghị ghi trong `scenarios.yaml`.

    Thứ tự này không phải ngẫu nhiên: kịch bản tự hồi phục và ít rủi ro chạy trước,
    kịch bản dễ làm web không truy cập được (bóp CPU của frontend) để gần cuối.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(data.get("recommended_order") or [])


def inject_one(inj: FaultInjector, fault: str, target: str,
               params: dict | None = None) -> ActiveFault:
    """Tiêm MỘT lỗi đơn. `fault` là mã F1..F4 trong `scenarios.yaml`."""
    params = params or {}
    if fault == "F1":
        return inj.inject_latency(target, params.get("extra_latency", "6s"))
    if fault == "F2":
        return inj.inject_crash(target)
    if fault == "F3":
        return inj.inject_pod_kill(target)
    if fault == "F4":
        return inj.inject_cpu_throttle(target, params.get("cpu", "10m"))
    if fault == "F5":
        return inj.inject_bad_config(target, params["key"], params["value"])
    raise ValueError(f"khong biet loai loi {fault}")


def inject_scenario(inj: FaultInjector, scenario: dict) -> list[ActiveFault]:
    """Tiêm trọn một kịch bản, kể cả kịch bản kép. Trả về danh sách lỗi đã tiêm.

    Trả về LIST chứ không phải một `ActiveFault`, vì S6 tiêm hai lỗi cùng lúc. Bên
    gọi phải chấp nhận có nhiều hơn một nguyên nhân gốc — gộp về một cái là tự bỏ
    mất chính thứ mà S6 dùng để kiểm tra.
    """
    if scenario.get("fault") == "combined":
        out: list[ActiveFault] = []
        for step in scenario["steps"]:
            out.append(inject_one(inj, step["fault"], step["target"],
                                  step.get("params") or {}))
        return out
    return [inject_one(inj, scenario["fault"], scenario["target"],
                       scenario.get("params") or {})]


def wait_seconds(scenario: dict) -> int:
    """Thời gian phải chờ sau khi tiêm, trước khi đo.

    Đọc từ `wait_after_inject_s` chứ không hằng số hóa ở đây: S3 chỉ chờ 120 giây
    vì lỗi tự khỏi sau khoảng 30 giây, chờ lâu hơn thì cửa sổ quan sát trôi qua mất
    triệu chứng. Các kịch bản còn lại chờ 330 giây, dài hơn cửa sổ 300 giây, để số
    liệu không còn lẫn giai đoạn hệ thống còn khỏe.
    """
    return int(scenario["wait_after_inject_s"])
