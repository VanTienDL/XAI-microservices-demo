"""Cổng duy nhất đi vào cluster.

Mọi thao tác đọc và ghi lên Kubernetes của khóa luận đều đi qua file này.
Không rải lệnh kubectl hay gọi thư viện kubernetes ở chỗ khác — quy tắc ở mục 5 KLTN.md.

Hai nguyên tắc bắt buộc, đừng phá:
  1. Mọi hàm làm thay đổi cluster đều TRẢ VỀ GIÁ TRỊ CŨ, để agent hoàn tác được.
  2. Mọi hàm nhận tham số namespace, vì phase 4 sẽ chạy y hệt trên namespace `twin`.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

from kubernetes import client, config
from kubernetes.client.rest import ApiException

DEFAULT_NAMESPACE = "default"


@dataclass
class PodInfo:
    """Tóm tắt một pod, đủ dùng cho snapshot và cho prompt của XAI."""

    name: str
    deployment: str
    phase: str          # Running, Pending, Failed...
    ready: bool
    restarts: int       # CỘNG DỒN từ lúc pod sinh ra, gồm cả những lần tắt mở cluster
    reason: str         # OOMKilled, CrashLoopBackOff... rỗng nếu bình thường
    # Lần khởi động lại gần nhất cách đây bao nhiêu giây. None nghĩa là chưa từng.
    # Đây mới là tín hiệu dùng được: `restarts` cộng dồn nên gần như luôn khác 0,
    # còn cái này cho biết pod VỪA chết — chữ ký của lỗi F3 (xóa pod đột ngột).
    last_restart_age_s: float | None = None
    # Pod duoc tao cach day bao nhieu giay.
    # BAT BUOC co: loi F3 xoa pod KHONG tao ra "khoi dong lai" ma tao ra POD MOI
    # hoan toan (ten khac, restarts ve 0, khong co lastState). Da kiem chung o kich
    # ban S3: pod checkoutservice-...-z29h8 bi xoa, pod ...-rtpn4 hien ra voi
    # restarts=0. Khong do tuoi pod thi F3 vo hinh.
    age_s: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class K8sClient:
    """Bọc thư viện kubernetes. Tạo một lần rồi dùng lại."""

    def __init__(self, namespace: str = DEFAULT_NAMESPACE, context: str | None = None):
        # Đọc file ~/.kube/config, chính là cấu hình kubectl đang dùng.
        config.load_kube_config(context=context)
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.namespace = namespace

    # ------------------------------------------------------------------
    # ĐỌC — không đổi gì trong cluster
    # ------------------------------------------------------------------

    def list_pods(self, namespace: str | None = None) -> list[PodInfo]:
        ns = namespace or self.namespace
        out: list[PodInfo] = []
        for p in self.core.list_namespaced_pod(ns).items:
            statuses = p.status.container_statuses or []
            ready = bool(statuses) and all(s.ready for s in statuses)
            restarts = sum(s.restart_count for s in statuses)
            reason = ""
            last_age: float | None = None
            for s in statuses:
                st = s.state
                if st and st.waiting and st.waiting.reason:
                    reason = st.waiting.reason
                elif st and st.terminated and st.terminated.reason:
                    reason = st.terminated.reason
                last = s.last_state.terminated if s.last_state else None
                if last and last.finished_at:
                    age = time.time() - last.finished_at.timestamp()
                    last_age = age if last_age is None else min(last_age, age)
            age = None
            if p.metadata.creation_timestamp:
                age = time.time() - p.metadata.creation_timestamp.timestamp()
            out.append(
                PodInfo(
                    name=p.metadata.name,
                    deployment=(p.metadata.labels or {}).get("app", ""),
                    phase=p.status.phase or "",
                    ready=ready,
                    restarts=restarts,
                    reason=reason,
                    last_restart_age_s=round(last_age, 1) if last_age is not None else None,
                    age_s=round(age, 1) if age is not None else None,
                )
            )
        return sorted(out, key=lambda x: x.name)

    def list_deployments(self, namespace: str | None = None) -> list[str]:
        ns = namespace or self.namespace
        return sorted(d.metadata.name for d in self.apps.list_namespaced_deployment(ns).items)

    def service_endpoint_map(self, namespace: str | None = None) -> dict[tuple[str, int], str]:
        """Bảng tra (ClusterIP, cổng) -> tên service.

        Đây là mảnh ghép quan trọng nhất cho phase 1. Span client của các service Go
        chỉ ghi `server.address` là địa chỉ IP chứ không phải tên, nên muốn biết nó
        gọi tới ai thì phải tra bảng này.

        BẮT BUỘC gọi lại mỗi lần chụp snapshot: ClusterIP đổi khi Service bị tạo lại,
        và namespace `twin` có dải IP hoàn toàn khác.
        """
        ns = namespace or self.namespace
        table: dict[tuple[str, int], str] = {}
        for svc in self.core.list_namespaced_service(ns).items:
            ip = svc.spec.cluster_ip
            if not ip or ip == "None":
                continue
            for port in svc.spec.ports or []:
                table[(ip, int(port.port))] = svc.metadata.name
        return table

    def get_logs(self, pod_name: str, tail: int = 50, namespace: str | None = None) -> str:
        ns = namespace or self.namespace
        try:
            return self.core.read_namespaced_pod_log(pod_name, ns, tail_lines=tail)
        except ApiException as e:
            return f"[khong lay duoc log: {e.reason}]"

    def get_logs_of_deployment(self, deployment: str, tail: int = 50,
                               namespace: str | None = None) -> str:
        """Lấy log của pod đầu tiên thuộc deployment. Dùng khi không nhớ tên pod."""
        ns = namespace or self.namespace
        pods = self.core.list_namespaced_pod(ns, label_selector=f"app={deployment}").items
        if not pods:
            return f"[khong co pod nao cua {deployment}]"
        return self.get_logs(pods[0].metadata.name, tail=tail, namespace=ns)

    def get_replicas(self, deployment: str, namespace: str | None = None) -> int:
        ns = namespace or self.namespace
        d = self.apps.read_namespaced_deployment(deployment, ns)
        return int(d.spec.replicas or 0)

    def get_env(self, deployment: str, key: str, namespace: str | None = None,
                container: str | None = None) -> str | None:
        """Đọc một biến môi trường. Trả None nếu biến chưa được đặt."""
        c = self._container(deployment, namespace, container)
        for e in c.env or []:
            if e.name == key:
                return e.value
        return None

    def get_cpu_limit(self, deployment: str, namespace: str | None = None,
                      container: str | None = None) -> str | None:
        c = self._container(deployment, namespace, container)
        if c.resources and c.resources.limits:
            return c.resources.limits.get("cpu")
        return None

    def get_cpu_request(self, deployment: str, namespace: str | None = None,
                        container: str | None = None) -> str | None:
        """Lượng CPU xin trước. Kubernetes bắt buộc giá trị này <= trần CPU."""
        c = self._container(deployment, namespace, container)
        if c.resources and c.resources.requests:
            return c.resources.requests.get("cpu")
        return None

    def deployment_configs(self, namespace: str | None = None,
                           env_keys: tuple[str, ...] = ("EXTRA_LATENCY",)) -> dict[str, dict]:
        """Cấu hình của mọi deployment trong namespace, đọc bằng MỘT lần gọi API.

        Dùng cho phép đối sánh trạng thái trước khi thi hành (`agent/guards.py`).
        Chỉ lấy thứ có thể bị đổi bởi lỗi tiêm hoặc bởi hành động của agent: số bản
        sao, image, trần CPU, lượng CPU xin trước, và các biến môi trường trong
        `env_keys`. Cố ý KHÔNG lấy số pod đang sẵn sàng: con số đó dao động tự nhiên
        mỗi khi pod khởi động, so nó thì báo lệch giả liên tục.
        """
        ns = namespace or self.namespace
        out: dict[str, dict] = {}
        for d in self.apps.list_namespaced_deployment(ns).items:
            c = d.spec.template.spec.containers[0]
            res = c.resources
            env = {e.name: e.value for e in (c.env or []) if e.name in env_keys}
            out[d.metadata.name] = {
                "replicas": int(d.spec.replicas or 0),
                "image": c.image,
                "cpu_limit": (res.limits or {}).get("cpu") if res else None,
                "cpu_request": (res.requests or {}).get("cpu") if res else None,
                **{f"env.{k}": env.get(k) for k in env_keys},
            }
        return out

    # ------------------------------------------------------------------
    # GHI — mọi hàm trả về giá trị cũ để hoàn tác
    # ------------------------------------------------------------------

    def scale_deployment(self, deployment: str, replicas: int,
                         namespace: str | None = None) -> int:
        """Đổi số bản chạy. Trả về số cũ.

        Hoàn tác: scale_deployment(deployment, so_cu).
        """
        ns = namespace or self.namespace
        old = self.get_replicas(deployment, ns)
        self.apps.patch_namespaced_deployment_scale(
            deployment, ns, {"spec": {"replicas": replicas}}
        )
        return old

    def set_env(self, deployment: str, key: str, value: str,
                namespace: str | None = None, container: str | None = None) -> str | None:
        """Đặt một biến môi trường. Trả về giá trị cũ, hoặc None nếu trước đó chưa có.

        Đổi env làm Kubernetes tạo pod mới, mất khoảng 10-20 giây mới phục vụ lại.
        Hoàn tác: set_env(giá trị cũ) nếu cũ khác None, ngược lại unset_env(key).
        """
        ns = namespace or self.namespace
        name = container or self._container(deployment, ns).name
        old = self.get_env(deployment, key, ns, name)
        patch = {
            "spec": {"template": {"spec": {"containers": [
                {"name": name, "env": [{"name": key, "value": str(value)}]}
            ]}}}
        }
        self.apps.patch_namespaced_deployment(deployment, ns, patch)
        return old

    def unset_env(self, deployment: str, key: str, namespace: str | None = None,
                  container: str | None = None) -> str | None:
        """Xóa hẳn một biến môi trường. Trả về giá trị cũ, None nếu vốn không có."""
        ns = namespace or self.namespace
        c_obj = self._container(deployment, ns, container)
        old = None
        kept: list[dict] = []
        for e in c_obj.env or []:
            if e.name == key:
                old = e.value
            elif e.value is not None:
                kept.append({"name": e.name, "value": e.value})
            else:
                kept.append({"name": e.name, "valueFrom": e.value_from.to_dict()})
        if old is None:
            return None

        # PHẢI dùng JSON Patch (RFC 6902), KHÔNG dùng merge patch.
        #
        # Đã vấp thật khi hoàn tác kịch bản S1: merge patch THAY nguyên mảng
        # `containers` bằng đúng thứ mình gửi lên, nên container mất trường `image`
        # và Kubernetes từ chối với lỗi 422:
        #     spec.template.spec.containers[0].image: Required value
        # Hậu quả nghiêm trọng hơn nhiều so với một lỗi cú pháp: lỗi vẫn nằm nguyên
        # trên hệ thống mà script lại tưởng đã hoàn tác xong.
        #
        # JSON Patch chỉ đụng đúng đường dẫn được chỉ định, phần còn lại giữ nguyên.
        d = self.apps.read_namespaced_deployment(deployment, ns)
        idx = next(
            i for i, c in enumerate(d.spec.template.spec.containers)
            if c.name == c_obj.name
        )
        self.apps.patch_namespaced_deployment(
            deployment, ns,
            [{"op": "replace",
              "path": f"/spec/template/spec/containers/{idx}/env",
              "value": kept}],
            _content_type="application/json-patch+json",
        )
        return old

    def set_cpu_limit(self, deployment: str, cpu: str, namespace: str | None = None,
                      container: str | None = None) -> dict:
        """Đặt trần CPU, ví dụ "10m" là 1% một lõi. Trả về {"limit", "request"} cũ.

        PHẢI hạ cả lượng xin trước cùng lúc. Đã vấp thật ở kịch bản S5: hạ trần
        xuống 10m trong khi requests vẫn là 100m thì Kubernetes từ chối với lỗi 422:

            spec.template.spec.containers[0].resources.requests:
            Invalid value: "100m": must be less than or equal to cpu limit of 10m

        Vá cả hai trong CÙNG một request, vì vá riêng lẻ cũng vi phạm ràng buộc ở
        bước trung gian.
        """
        ns = namespace or self.namespace
        c = self._container(deployment, ns, container)
        old_limit = (c.resources.limits or {}).get("cpu") if c.resources else None
        old_request = (c.resources.requests or {}).get("cpu") if c.resources else None
        patch = {
            "spec": {"template": {"spec": {"containers": [
                {"name": c.name,
                 "resources": {"limits": {"cpu": cpu}, "requests": {"cpu": cpu}}}
            ]}}}
        }
        self.apps.patch_namespaced_deployment(deployment, ns, patch)
        return {"limit": old_limit, "request": old_request}

    def restore_cpu(self, deployment: str, limit: str | None, request: str | None,
                    namespace: str | None = None, container: str | None = None) -> None:
        """Trả trần CPU và lượng xin trước về giá trị cũ, trong cùng một lần vá."""
        ns = namespace or self.namespace
        c = self._container(deployment, ns, container)
        resources: dict = {}
        if limit is not None:
            resources["limits"] = {"cpu": limit}
        if request is not None:
            resources["requests"] = {"cpu": request}
        if not resources:
            return
        patch = {
            "spec": {"template": {"spec": {"containers": [
                {"name": c.name, "resources": resources}
            ]}}}
        }
        self.apps.patch_namespaced_deployment(deployment, ns, patch)

    def delete_pod(self, pod_name: str, namespace: str | None = None,
                   grace_period_seconds: int | None = None) -> str:
        """Xóa một pod. Kubernetes tự tạo pod mới thay thế nên không cần hoàn tác.

        `grace_period_seconds=0` là xóa ĐỘT NGỘT, không cho service dọn dẹp — đúng
        tinh thần "pod chết đột ngột" của lỗi F3 ở mục 6 KLTN.md. Để mặc định thì
        Kubernetes cho 30 giây ân hạn, service đóng kết nối gọn gàng và gần như
        không ai thấy gì.
        """
        ns = namespace or self.namespace
        if grace_period_seconds is None:
            self.core.delete_namespaced_pod(pod_name, ns)
        else:
            self.core.delete_namespaced_pod(
                pod_name, ns, grace_period_seconds=grace_period_seconds
            )
        return pod_name

    def restart_deployment(self, deployment: str, namespace: str | None = None) -> str:
        """Khởi động lại toàn bộ pod của deployment. Trả về mốc thời gian đã ghi vào."""
        ns = namespace or self.namespace
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kltn.restartedAt": stamp
        }}}}}
        self.apps.patch_namespaced_deployment(deployment, ns, patch)
        return stamp

    # ------------------------------------------------------------------
    # CHỜ — dùng sau mỗi hành động, trước khi đo lại
    # ------------------------------------------------------------------

    def wait_replicas(self, deployment: str, expected: int, timeout: int = 120,
                      namespace: str | None = None) -> bool:
        """Chờ số pod sẵn sàng đúng bằng `expected`. True nếu kịp, False nếu quá giờ.

        Dùng cho cả F2 (scale về 0) lẫn lúc hoàn tác (chờ về lại 1).
        """
        ns = namespace or self.namespace
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.apps.read_namespaced_deployment(deployment, ns)
            if int(d.status.ready_replicas or 0) == expected:
                return True
            time.sleep(2)
        return False

    def wait_ready(self, deployment: str, timeout: int = 120,
                   namespace: str | None = None) -> bool:
        """Chờ deployment rollout xong HẲN: mọi pod đang chạy đều mang cấu hình mới.

        Bản cũ chỉ đếm pod sẵn sàng, mà pod CŨ vẫn được đếm cho tới khi bị thay. Đổi
        trần CPU xong, hàm trả về sau chưa tới 1 giây trong khi pod mới còn chưa
        chạy — hành động báo `verified` khi hệ thống chưa hề chạy cấu hình mới. Bạn
        cùng nhóm bắt được lỗi này trên k3s (2026-10-07). Với tự hoàn tác thì lỗi này
        nặng hơn: phép đo "sau" sẽ đo nhầm pod cũ.

        Điều kiện giống `kubectl rollout status`: controller đã thấy bản cấu hình mới
        nhất, và số pod hiện có, pod đã cập nhật, pod sẵn sàng đều bằng số khai báo.
        """
        ns = namespace or self.namespace
        deadline = time.time() + timeout
        while time.time() < deadline:
            d = self.apps.read_namespaced_deployment(deployment, ns)
            want = int(d.spec.replicas or 0)
            st = d.status
            if ((st.observed_generation or 0) >= (d.metadata.generation or 0)
                    and int(st.updated_replicas or 0) == want
                    and int(st.replicas or 0) == want
                    and int(st.available_replicas or 0) == want):
                return True
            time.sleep(2)
        return False

    # ------------------------------------------------------------------
    # Nội bộ
    # ------------------------------------------------------------------

    def _container(self, deployment: str, namespace: str | None = None,
                   container: str | None = None) -> Any:
        """Lấy đối tượng container trong deployment. Mặc định lấy container đầu tiên.

        Online Boutique đặt tên container là `server` cho 11 service, `redis` cho
        redis-cart, `main` cho loadgenerator — nên không hardcode được, phải tra.
        """
        ns = namespace or self.namespace
        d = self.apps.read_namespaced_deployment(deployment, ns)
        containers = d.spec.template.spec.containers
        if container is None:
            return containers[0]
        for c in containers:
            if c.name == container:
                return c
        raise ValueError(f"deployment {deployment} khong co container ten {container}")
