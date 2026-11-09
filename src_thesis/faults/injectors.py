"""Bốn cách phá hệ thống có chủ đích, kèm nút hoàn tác.

Viết bằng Python chứ không phải bash, vì agent ở phase 5 phải gọi được chúng bằng
code (mục 3 KLTN.md). Mọi thao tác đi qua `k8s_client.py`.

AN TOÀN — đọc trước khi dùng:
Mỗi lần tiêm lỗi, trạng thái cũ được ghi NGAY ra `data/runs/active_fault.json`
TRƯỚC khi thực sự phá. Nếu script chết giữa chừng, mất điện, hay cậu lỡ đóng
terminal, vẫn hoàn tác được bằng:

    python scripts/inject.py --revert

Không có cơ chế này thì một lần treo máy là cluster kẹt ở trạng thái hỏng mà không
nhớ giá trị cũ là bao nhiêu.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from src_thesis.graph.logical_graph import load_logical_topology
from src_thesis.k8s_client import K8sClient

RUNS_DIR = Path(__file__).resolve().parents[2] / "data" / "runs"
ACTIVE_FAULT_FILE = RUNS_DIR / "active_fault.json"


@dataclass
class GroundTruth:
    """Đáp án đúng của một ca. Không có nó thì không chấm điểm được agent."""

    fault_id: str
    target_service: str
    fault_type: str                    # latency | crash | pod_kill | resource_exhaustion
    params: dict
    expected_propagation: list[str]    # service nào chịu ảnh hưởng lan truyền
    correct_action_class: str          # easy | medium | hard
    correct_actions: list[str]         # hành động nào được coi là đúng
    injected_at: float
    injected_at_human: str = ""
    # v2: các nhãn loại lỗi được chấp nhận, xem `metrics.fault_type_matches`.
    # Rỗng = chỉ chấp nhận `fault_type` (file đáp án v1).
    accepted_fault_types: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["injected_at_human"] = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(self.injected_at)
        )
        return d


@dataclass
class ActiveFault:
    """Lỗi đang được tiêm, kèm đủ thông tin để hoàn tác."""

    ground_truth: GroundTruth
    undo_kind: str                     # env | scale | cpu | none
    undo_args: dict
    namespace: str = "default"
    reverted: bool = False
    _k8s: K8sClient | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "ground_truth": self.ground_truth.to_dict(),
            "undo_kind": self.undo_kind,
            "undo_args": self.undo_args,
            "namespace": self.namespace,
            "reverted": self.reverted,
        }

    def save(self, path: Path = ACTIVE_FAULT_FILE) -> Path:
        """Ghi vào DANH SÁCH lỗi đang tiêm, không ghi đè.

        Phải là danh sách vì kịch bản kép S6 tiêm hai lỗi cùng lúc. Bản đầu tiên ghi
        đè một object duy nhất, nên lỗi thứ hai xóa mất dấu vết của lỗi thứ nhất và
        hoàn tác chỉ gỡ được một nửa — nửa còn lại nằm im trên hệ thống.

        Mục đã hoàn tác thì bị xóa khỏi danh sách.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        items = _read_state(path)
        items = [i for i in items
                 if i["ground_truth"]["fault_id"] != self.ground_truth.fault_id]
        if not self.reverted:
            items.append(self.to_dict())
        path.write_text(
            json.dumps({"faults": items}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path

    def save_ground_truth(self, runs_dir: Path = RUNS_DIR) -> Path:
        """Ghi riêng file đáp án, đặt tên theo thời điểm để không đè lên nhau."""
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime(
            "%Y%m%d-%H%M%S", time.localtime(self.ground_truth.injected_at)
        )
        path = runs_dir / f"{stamp}_groundtruth_{self.ground_truth.fault_id}.json"
        path.write_text(
            json.dumps(self.ground_truth.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return path

    def revert(self, k8s: K8sClient | None = None) -> None:
        """Trả hệ thống về đúng trạng thái trước khi tiêm."""
        k8s = k8s or self._k8s or K8sClient(namespace=self.namespace)
        a = self.undo_args
        if self.undo_kind == "env":
            if a.get("old_value") is None:
                k8s.unset_env(a["deployment"], a["key"], self.namespace)
            else:
                k8s.set_env(a["deployment"], a["key"], a["old_value"], self.namespace)
        elif self.undo_kind == "scale":
            k8s.scale_deployment(a["deployment"], a["old_replicas"], self.namespace)
        elif self.undo_kind == "cpu":
            k8s.restore_cpu(
                a["deployment"], a.get("old_cpu"), a.get("old_cpu_request"),
                self.namespace,
            )
        elif self.undo_kind == "none":
            # F3 xóa pod: Kubernetes tự tạo pod mới, không cần làm gì.
            pass
        self.reverted = True
        self.save()


def _read_state(path: Path = ACTIVE_FAULT_FILE) -> list[dict]:
    """Đọc danh sách lỗi đang tiêm. Hiểu được cả định dạng cũ (một object)."""
    if not path.exists():
        return []
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(d, dict) and "faults" in d:
        return list(d["faults"])
    if isinstance(d, dict) and "ground_truth" in d:      # định dạng cũ
        return [] if d.get("reverted") else [d]
    return []


def load_active_faults(path: Path = ACTIVE_FAULT_FILE) -> list[ActiveFault]:
    """Tất cả lỗi đang tiêm, theo đúng thứ tự đã tiêm."""
    return [_from_dict(d) for d in _read_state(path)]


def load_active_fault(path: Path = ACTIVE_FAULT_FILE) -> ActiveFault | None:
    """Lỗi đang tiêm gần nhất. Giữ lại cho tương thích với code cũ."""
    faults = load_active_faults(path)
    return faults[-1] if faults else None


def _from_dict(d: dict) -> ActiveFault:
    gt = d["ground_truth"]
    return ActiveFault(
        ground_truth=GroundTruth(
            fault_id=gt["fault_id"],
            target_service=gt["target_service"],
            fault_type=gt["fault_type"],
            params=gt["params"],
            expected_propagation=gt["expected_propagation"],
            correct_action_class=gt["correct_action_class"],
            correct_actions=gt.get("correct_actions", []),
            injected_at=gt["injected_at"],
            accepted_fault_types=gt.get("accepted_fault_types", []),
        ),
        undo_kind=d["undo_kind"],
        undo_args=d["undo_args"],
        namespace=d.get("namespace", "default"),
    )


def expected_propagation(target: str) -> list[str]:
    """Những service sẽ chịu ảnh hưởng khi `target` hỏng.

    Tính tự động bằng cách lần ngược sơ đồ thiết kế: ai gọi target, ai gọi người đó,
    và cứ thế. Tính tự động thay vì viết tay để đáp án không bị lệch khi sơ đồ đổi.
    """
    topo = load_logical_topology()
    seen: set[str] = set()
    frontier = [target]
    while frontier:
        cur = frontier.pop()
        for caller in topo.graph.predecessors(cur):
            if caller in seen or caller == target:
                continue
            seen.add(caller)
            frontier.append(caller)
    # loadgenerator là bộ sinh tải, không phải service nghiệp vụ
    seen.discard("loadgenerator")
    return sorted(seen)


class FaultInjector:
    """Bốn injector theo mục 6 KLTN.md."""

    def __init__(self, k8s: K8sClient | None = None, namespace: str = "default"):
        self.k8s = k8s or K8sClient(namespace=namespace)
        self.namespace = namespace

    # ------------------------------------------------------------------

    def _finish(self, fault: ActiveFault) -> ActiveFault:
        fault._k8s = self.k8s
        fault.save()
        fault.save_ground_truth()
        return fault

    # F1 --------------------------------------------------------------

    def inject_latency(
        self,
        service: str = "productcatalogservice",
        extra_latency: str = "6s",
    ) -> ActiveFault:
        """F1 — làm service chậm hẳn đi.

        Chỉ `productcatalogservice` hỗ trợ, vì chỉ nó đọc biến `EXTRA_LATENCY`
        (src/productcatalogservice/server.go:88). Biến này làm mỗi lần gọi ngủ thêm
        đúng khoảng thời gian đã đặt.

        Lưu ý: đổi biến môi trường làm Kubernetes tạo pod mới, mất 10 tới 20 giây
        mới thấy triệu chứng. Đừng đo ngay lập tức rồi kết luận là lỗi không ăn.

        ĐÁP ÁN CHỈ CÒN `rollback`. Bản đầu chấp nhận cả `adjust_resources` và
        `restart_pod`, nhưng cả hai đều không gỡ được lỗi: pod mới sinh từ cùng
        template nên vẫn mang `EXTRA_LATENCY`, còn CPU lúc lỗi chỉ dùng khoảng 1%
        trần. Đã đo thật trên k3s (repo của bạn cùng nhóm, 2026-10-08): nâng CPU lên
        400m thì p95 của service vẫn đứng yên ở 9750ms. Chấm hai hành động đó là
        "đúng" là chấm đúng cho một hành động không chữa được bệnh.
        """
        if service != "productcatalogservice":
            raise ValueError(
                "chi productcatalogservice doc bien EXTRA_LATENCY; "
                "dung F4 neu muon lam service khac cham"
            )
        old = self.k8s.get_env(service, "EXTRA_LATENCY", self.namespace)
        fault = ActiveFault(
            ground_truth=GroundTruth(
                fault_id=f"F1-{service}-latency",
                target_service=service,
                fault_type="latency",
                params={"extra_latency": extra_latency},
                expected_propagation=expected_propagation(service),
                correct_action_class="hard",
                correct_actions=["rollback"],
                injected_at=time.time(),
                accepted_fault_types=["latency", "configuration_error"],
            ),
            undo_kind="env",
            undo_args={"deployment": service, "key": "EXTRA_LATENCY", "old_value": old},
            namespace=self.namespace,
        )
        self._finish(fault)
        self.k8s.set_env(service, "EXTRA_LATENCY", extra_latency, self.namespace)
        return fault

    # F5 --------------------------------------------------------------

    def inject_bad_config(self, service: str, key: str, value: str) -> ActiveFault:
        """F5 (v2) — đổi một biến cấu hình sang giá trị sai, như một lần triển khai lỗi.

        Kịch bản H2 dùng nó để đổi `CURRENCY_SERVICE_ADDR` của frontend sang sai cổng:
        frontend vẫn khởi động và vẫn qua kiểm tra sức khỏe (`/_healthz` không gọi
        currencyservice, và `grpc.NewClient` không kết nối ngay), nhưng mọi trang cần
        đổi tiền đều lỗi. Tên biến không tự nói ra lỗi, khác `EXTRA_LATENCY` của S1:
        đó là chủ đích, để kiểm tra LLM có thật sự dùng lịch sử thay đổi hay chỉ đọc
        tên biến.

        Đáp án `rollback`: tăng bản sao hay CPU, khởi động lại đều sinh pod từ cùng
        cấu hình sai. Biến phải đã tồn tại — đổi giá trị chứ không thêm biến lạ.
        """
        old = self.k8s.get_env(service, key, self.namespace)
        if old is None:
            raise ValueError(f"{service} khong co bien {key}; F5 chi doi bien da co")
        fault = ActiveFault(
            ground_truth=GroundTruth(
                fault_id=f"F5-{service}-config",
                target_service=service,
                fault_type="configuration_error",
                params={"key": key, "value": value, "old_value": old},
                expected_propagation=expected_propagation(service),
                correct_action_class="hard",
                correct_actions=["rollback"],
                injected_at=time.time(),
                accepted_fault_types=["configuration_error"],
            ),
            undo_kind="env",
            undo_args={"deployment": service, "key": key, "old_value": old},
            namespace=self.namespace,
        )
        self._finish(fault)
        self.k8s.set_env(service, key, value, self.namespace)
        return fault

    # F2 --------------------------------------------------------------

    def inject_crash(self, service: str) -> ActiveFault:
        """F2 — tắt hẳn service bằng cách hạ số bản chạy về 0.

        Triệu chứng rõ nhất: cạnh tới service này biến mất khỏi runtime graph, và
        các cạnh của người gọi nó chuyển sang lỗi.
        """
        old = self.k8s.get_replicas(service, self.namespace)
        fault = ActiveFault(
            ground_truth=GroundTruth(
                fault_id=f"F2-{service}-crash",
                target_service=service,
                fault_type="crash",
                params={"replicas": 0, "old_replicas": old},
                expected_propagation=expected_propagation(service),
                correct_action_class="easy",
                correct_actions=["scale_up"],
                injected_at=time.time(),
            ),
            undo_kind="scale",
            undo_args={"deployment": service, "old_replicas": old},
            namespace=self.namespace,
        )
        self._finish(fault)
        self.k8s.scale_deployment(service, 0, self.namespace)
        return fault

    # F3 --------------------------------------------------------------

    def inject_pod_kill(self, service: str) -> ActiveFault:
        """F3 — xóa pod đột ngột.

        Kubernetes tự tạo pod mới nên hệ thống tự hồi phục sau khoảng 30 giây.
        Đây là ca kiểm tra agent có biết KHÔNG LÀM GÌ hay không — hành động đúng ở
        đây là chờ. Agent nào cũng nhảy vào sửa thì sẽ lộ ra ở kịch bản này.
        """
        pods = [p for p in self.k8s.list_pods(self.namespace) if p.deployment == service]
        if not pods:
            raise ValueError(f"khong tim thay pod nao cua {service}")
        victim = pods[0].name
        fault = ActiveFault(
            ground_truth=GroundTruth(
                fault_id=f"F3-{service}-podkill",
                target_service=service,
                fault_type="pod_kill",
                params={"pod": victim},
                expected_propagation=expected_propagation(service),
                correct_action_class="easy",
                correct_actions=["no_action"],
                injected_at=time.time(),
            ),
            undo_kind="none",
            undo_args={"deployment": service, "pod": victim},
            namespace=self.namespace,
        )
        self._finish(fault)
        # grace_period 0 = chet dot ngot, khong cho don dep (xem docstring delete_pod)
        self.k8s.delete_pod(victim, self.namespace, grace_period_seconds=0)
        return fault

    # F4 --------------------------------------------------------------

    def inject_cpu_throttle(self, service: str, cpu: str = "10m") -> ActiveFault:
        """F4 — bóp trần CPU xuống rất thấp.

        "10m" nghĩa là 1% của một lõi. Service không chết nhưng chậm hẳn và có thể
        trượt kiểm tra sức khỏe. Khác F1 ở chỗ triệu chứng kèm theo CPU throttling
        nhìn thấy trong metric hạ tầng — đây là điểm để phân biệt hai loại lỗi.
        """
        old_limit = self.k8s.get_cpu_limit(service, self.namespace)
        old_request = self.k8s.get_cpu_request(service, self.namespace)
        fault = ActiveFault(
            ground_truth=GroundTruth(
                fault_id=f"F4-{service}-cpu",
                target_service=service,
                fault_type="resource_exhaustion",
                params={"cpu_limit": cpu, "old_cpu_limit": old_limit,
                        "old_cpu_request": old_request},
                expected_propagation=expected_propagation(service),
                correct_action_class="easy",
                # v2 them rollback: quay lui phien ban gio dua tran CPU ve truoc khi
                # bi bop, tuc chua dung benh. Ban v1 rollback chi go EXTRA_LATENCY.
                correct_actions=["adjust_resources", "scale_up", "rollback"],
                injected_at=time.time(),
                accepted_fault_types=["resource_exhaustion", "configuration_error"],
            ),
            undo_kind="cpu",
            undo_args={"deployment": service, "old_cpu": old_limit,
                       "old_cpu_request": old_request},
            namespace=self.namespace,
        )
        self._finish(fault)
        self.k8s.set_cpu_limit(service, cpu, self.namespace)
        return fault
