"""Dựng, nạp trạng thái và xóa Digital Twin.

Twin là **bản sao chạy thật** ở namespace `twin`, không phải mô hình toán mô phỏng
(mục 3 KLTN.md đã loại hướng mô phỏng: xây simulation model cho microservices là đề
tài tiến sĩ). Muốn biết hậu quả của một hành động thì chạy thử rồi đo.

Ba hàm chính, tương ứng ba bước trong vòng thí nghiệm ở mục 2 KLTN.md:

    create_twin()          dựng bản sao
    load_state(snapshot)   áp cấu hình hiện tại của production lên bản sao
    destroy_twin()         xóa, trả RAM về

KHÔNG BAO GIỜ chạy twin song song với thí nghiệm trên production. Mục 2 KLTN.md đã
chốt điều này vì RAM là nút thắt lớn nhất của cả project. `create_twin()` vì vậy
kiểm tra RAM trước khi dựng và từ chối nếu không đủ chỗ.

VÌ SAO LOAD_STATE TỒN TẠI: twin dựng từ manifest gốc nên nó mang cấu hình MẶC ĐỊNH.
Production tại thời điểm sự cố có thể đã khác — số bản sao đã đổi, biến môi trường
đã đổi, trần CPU đã đổi. Thử hành động trên một bản sao không giống production thì
kết quả đo được không nói lên điều gì về production.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from src_thesis.agent.actions import _same_fields, describe_fields_change
from src_thesis.k8s_client import K8sClient

TWIN_NAMESPACE = "twin"
OVERLAY_DIR = Path(__file__).resolve().parents[2] / "infra" / "twin"

# 12 deployment: đủ 11 service của Online Boutique cộng bộ sinh tải.
#
# Twin CỐ Ý giống production hết mức, chỉ khác một chỗ: frontend không mở LoadBalancer
# vì kind không cấp được IP ngoài.
#
# Bản đầu tiên từng gỡ adservice, recommendationservice và loadgenerator để tiết kiệm
# RAM. Cả ba quyết định đó đều đã đảo lại sau khi đo thật — lý do và số liệu ghi ở đầu
# infra/twin/manifests.yaml và infra/twin/loadgenerator.yaml.
TWIN_DEPLOYMENTS = [
    "adservice",
    "cartservice",
    "checkoutservice",
    "currencyservice",
    "emailservice",
    "frontend",
    "loadgenerator",
    "paymentservice",
    "productcatalogservice",
    "recommendationservice",
    "redis-cart",
    "shippingservice",
]

# Deployment KHÔNG chép cấu hình từ production: twin có bộ sinh tải riêng (cấu hình
# trong infra/twin/loadgenerator.yaml), chép đè thì nó bắn tải vào production.
NOT_SYNCED = ("loadgenerator",)

# Ngưỡng RAM tối thiểu còn trống trong WSL trước khi cho dựng twin, tính bằng MiB.
# Số đo thật: 14 pod của bản chính dùng khoảng 620 MiB, twin có 9 pod nhẹ hơn nên
# ước khoảng 400 MiB. Để 700 cho có biên.
MIN_FREE_MIB = 700


@dataclass
class TwinStatus:
    """Trạng thái twin tại một thời điểm."""

    exists: bool
    ready: int = 0
    total: int = 0
    not_ready: list[str] = field(default_factory=list)
    memory_mib: float = 0.0

    @property
    def all_ready(self) -> bool:
        return self.exists and self.total > 0 and self.ready == self.total

    def describe(self) -> str:
        if not self.exists:
            return "twin: chua ton tai"
        return (f"twin: {self.ready}/{self.total} pod san sang, "
                f"RAM {self.memory_mib:.0f} MiB"
                + (f", chua san sang: {', '.join(self.not_ready)}" if self.not_ready else ""))


def _kubectl(args: list[str], timeout: int = 300) -> tuple[int, str]:
    """Gọi kubectl và trả về (mã thoát, đầu ra gộp cả lỗi).

    Dùng kubectl thay vì thư viện Python cho phần apply và delete namespace, vì
    `kubectl apply -k` biết cách render kustomize còn thư viện thì không.
    """
    proc = subprocess.run(
        ["kubectl"] + args,
        capture_output=True, text=True, timeout=timeout, encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _pod_memory_mib(namespace: str) -> float:
    """Tổng RAM các pod trong namespace, đọc từ `kubectl top`.

    Trả về 0.0 nếu metrics-server chưa sẵn sàng — không coi đó là lỗi, vì ngay sau
    khi dựng twin thì `kubectl top` chưa có số cho pod mới.
    """
    code, out = _kubectl(["top", "pods", "-n", namespace, "--no-headers"], timeout=30)
    if code != 0:
        return 0.0
    total = 0.0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].endswith("Mi"):
            try:
                total += float(parts[2][:-2])
            except ValueError:
                continue
    return total


def free_memory_mib() -> float:
    """RAM còn trống của node kind, tính bằng MiB.

    Đọc `kubectl top node` rồi lấy phần trăm còn lại nhân dung lượng node. Đây là
    RAM của node bên trong WSL, đúng thứ mà pod tranh nhau.
    """
    code, out = _kubectl(["top", "node", "--no-headers"], timeout=30)
    if code != 0 or not out.strip():
        return -1.0
    parts = out.split()
    # Dinh dang: NAME CPU(cores) CPU% MEMORY(bytes) MEMORY%
    for i, tok in enumerate(parts):
        if tok.endswith("Mi") and i + 1 < len(parts) and parts[i + 1].endswith("%"):
            used = float(tok[:-2])
            pct = float(parts[i + 1][:-1])
            if pct <= 0:
                return -1.0
            return used / pct * 100.0 - used
    return -1.0


class TwinManager:
    """Vòng đời của Digital Twin. Tạo một lần rồi dùng lại cho cả loạt thí nghiệm."""

    def __init__(self, k8s: K8sClient | None = None,
                 namespace: str = TWIN_NAMESPACE,
                 overlay_dir: Path = OVERLAY_DIR):
        self.k8s = k8s or K8sClient()
        self.namespace = namespace
        self.overlay_dir = overlay_dir

    # ------------------------------------------------------------------
    # ĐỌC
    # ------------------------------------------------------------------

    def status(self) -> TwinStatus:
        code, _ = _kubectl(["get", "namespace", self.namespace], timeout=30)
        if code != 0:
            return TwinStatus(exists=False)
        pods = self.k8s.list_pods(self.namespace)
        not_ready = [p.name for p in pods if not p.ready]
        return TwinStatus(
            exists=True,
            ready=sum(1 for p in pods if p.ready),
            total=len(pods),
            not_ready=not_ready,
            memory_mib=_pod_memory_mib(self.namespace),
        )

    # ------------------------------------------------------------------
    # DỰNG
    # ------------------------------------------------------------------

    def create_twin(self, wait: bool = True, timeout: int = 420,
                    check_memory: bool = True) -> TwinStatus:
        """Dựng twin từ lớp phủ kustomize, chờ tới khi mọi pod sẵn sàng.

        `check_memory` kiểm tra RAM trống trước khi dựng. Để `False` chỉ khi cậu đã
        tự đo và biết chắc là đủ — dựng khi thiếu RAM thì pod bị OOMKilled và có thể
        kéo theo cả pod của production, tức là hỏng luôn thứ đang muốn quan sát.
        """
        if check_memory:
            free = free_memory_mib()
            if free >= 0 and free < MIN_FREE_MIB:
                raise RuntimeError(
                    f"RAM khong du: node con trong {free:.0f} MiB, can it nhat "
                    f"{MIN_FREE_MIB} MiB. Dong bot ung dung tren Windows roi thu lai, "
                    f"hoac tang memory trong .wslconfig (nho chay 'wsl --shutdown' sau khi sua)."
                )

        code, out = _kubectl(["apply", "-k", str(self.overlay_dir)], timeout=300)
        if code != 0:
            raise RuntimeError(f"apply that bai: {out.strip()[:400]}")

        if not wait:
            return self.status()
        return self.wait_ready(timeout=timeout)

    def wait_ready(self, timeout: int = 420, poll: int = 10) -> TwinStatus:
        """Chờ tới khi đủ 9 deployment sẵn sàng.

        Timeout mặc định 7 phút vì lần dựng đầu tiên phải kéo image, và trên máy
        RAM hẹp thì các pod tranh CPU nên khởi động chậm hơn hẳn bình thường.
        """
        deadline = time.time() + timeout
        last = self.status()
        while time.time() < deadline:
            last = self.status()
            if last.all_ready and last.total >= len(TWIN_DEPLOYMENTS):
                return last
            time.sleep(poll)
        raise TimeoutError(
            f"twin chua san sang sau {timeout}s. {last.describe()}. "
            f"Kiem tra bang: kubectl get pods -n {self.namespace}"
        )

    # ------------------------------------------------------------------
    # NẠP TRẠNG THÁI
    # ------------------------------------------------------------------

    def load_state(self, source_namespace: str = "default") -> dict[str, dict]:
        """Áp cấu hình hiện tại của production lên twin.

        Sao chép: số bản sao, và TOÀN BỘ cấu hình container — mọi biến môi trường,
        trần và lượng xin CPU, image — trong một lần vá mỗi deployment. Bản v1 chỉ
        chép `EXTRA_LATENCY`, nên lỗi cấu hình ở biến khác (kịch bản H2) không bao
        giờ có mặt trên twin, và twin phán quyết trên một hệ thống KHÔNG có lỗi.

        Giữ nguyên trên twin: ba biến quan sát (`ENV_INFRA_KEYS` trong k8s_client) và
        bộ sinh tải (`NOT_SYNCED`). Chỉ đụng deployment có mặt ở CẢ HAI bên.

        Trả về bảng những gì đã đổi, để ghi vào log thí nghiệm. Ném `RuntimeError`
        nếu twin không chạy được cấu hình mới trong 180 giây — khi đó twin không còn
        là bản sao của production, và không được dùng để phán quyết.
        """
        applied: dict[str, dict] = {}
        twin_deps = set(self.k8s.list_deployments(self.namespace))
        prod_deps = set(self.k8s.list_deployments(source_namespace))

        for dep in sorted((twin_deps & prod_deps) - set(NOT_SYNCED)):
            change: dict = {}

            replicas = self.k8s.get_replicas(dep, namespace=source_namespace)
            if replicas is not None and replicas != self.k8s.get_replicas(
                dep, namespace=self.namespace
            ):
                self.k8s.scale_deployment(dep, replicas, namespace=self.namespace)
                change["replicas"] = replicas

            want = self.k8s.container_spec(dep, namespace=source_namespace)
            old = self.k8s.apply_template_fields(dep, want, namespace=self.namespace)
            if not _same_fields(old, want):
                change["config"] = describe_fields_change(old, want)

            if change:
                applied[dep] = change

        # CHO TWIN CHAY CAU HINH MOI THAT, khong chi ghi xong cau hinh.
        #
        # VI SAO (ban sua cua Nguyen Duc Tho, commit 944b3189): lan chay that S1 tren
        # k3s (2026-10-07), twin chep tran CPU 500m cua production nhung node da het
        # cho, pod moi treo Pending, va pod CU van chay cau hinh cu. Ham van tra ve
        # "da nap", twin do va phan quyet tren mot ban sao SAI ma khong ai biet.
        stuck = []
        for dep in applied:
            if not self.k8s.wait_ready(dep, timeout=180, namespace=self.namespace):
                phases = sorted({p.phase for p in self.k8s.list_pods(self.namespace)
                                 if p.deployment == dep and not p.ready})
                stuck.append(f"{dep} ({', '.join(phases) or 'chua san sang'})")
        if stuck:
            raise RuntimeError(
                f"twin khong nap duoc trang thai production: {'; '.join(stuck)} chua "
                f"chay cau hinh moi sau 180s. Pending thuong la node het CPU, kiem tra: "
                f"kubectl describe pod -n {self.namespace}")

        return applied

    # ------------------------------------------------------------------
    # XÓA
    # ------------------------------------------------------------------

    def destroy_twin(self, wait: bool = True, timeout: int = 240) -> bool:
        """Xóa namespace twin và chờ nó biến mất hẳn.

        PHẢI chờ xóa xong thật sự, không chỉ chờ lệnh trả về. Kubernetes xóa
        namespace theo kiểu bất đồng bộ: lệnh trả về ngay nhưng pod còn sống thêm
        hàng chục giây, và RAM chỉ thực sự được trả lại khi pod cuối cùng chết. Dựng
        twin mới khi twin cũ chưa chết hẳn là cách chắc chắn nhất để hết RAM.
        """
        code, out = _kubectl(["delete", "namespace", self.namespace,
                              "--ignore-not-found"], timeout=timeout)
        if code != 0:
            raise RuntimeError(f"xoa that bai: {out.strip()[:300]}")
        if not wait:
            return True

        deadline = time.time() + timeout
        while time.time() < deadline:
            rc, _ = _kubectl(["get", "namespace", self.namespace], timeout=30)
            if rc != 0:
                return True
            time.sleep(5)
        raise TimeoutError(
            f"namespace {self.namespace} chua bien mat sau {timeout}s. "
            f"Kiem tra: kubectl get namespace {self.namespace} -o yaml"
        )
