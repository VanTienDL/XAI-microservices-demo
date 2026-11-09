"""Gom toàn bộ telemetry tại một thời điểm thành một object duy nhất.

Đây là đầu vào của XAI ở phase 3 và là bằng chứng lưu lại cho chương kết quả.
Mỗi lần thí nghiệm phải ghi một file JSON đầy đủ vào data/runs/ — không có log này
thì không viết được chương kết quả (mục 5 KLTN.md).

Cách dùng:
    snap = take_snapshot(label="truoc-khi-tiem-loi")
    snap.save()                 # ghi ra data/runs/
    print(snap.to_prompt_text()) # đoạn text nhồi cho LLM
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from src_thesis.graph import serialize
from src_thesis.graph.diff import GraphDiff, diff_graphs
from src_thesis.graph.logical_graph import LogicalTopology, load_logical_topology
from src_thesis.graph.model import ServiceGraph
from src_thesis.graph.runtime_graph import build_runtime_graph, edge_source_summary
from src_thesis.k8s_client import K8sClient, PodInfo, describe_fields_change
from src_thesis.telemetry.jaeger_client import JaegerClient
from src_thesis.telemetry.prometheus_client import PrometheusClient

RUNS_DIR = Path(__file__).resolve().parents[2] / "data" / "runs"

# Mặc định nhìn lại bao xa khi liệt kê thay đổi cấu hình, nếu bên gọi không đưa mốc.
# 30 phút phủ được trọn một ca có agent (tối đa 3 vòng). Bộ chạy thí nghiệm luôn đưa
# mốc là lúc bắt đầu ca — xem `changes_since` ở `take_snapshot`.
CHANGE_WINDOW_S = 1800


@dataclass
class SystemSnapshot:
    """Trạng thái hệ thống tại một thời điểm."""

    label: str
    namespace: str
    taken_at: float
    runtime_graph: ServiceGraph
    diff: GraphDiff
    red: dict = field(default_factory=dict)
    resources: dict = field(default_factory=dict)
    pods: list[PodInfo] = field(default_factory=list)
    topology: LogicalTopology | None = None
    span_count: int = 0
    edge_sources: dict = field(default_factory=dict)
    cpu: dict = field(default_factory=dict)
    # Thay đổi cấu hình gần đây (v2). None = snapshot cũ, chưa có mục này: khi đó
    # prompt KHÔNG in mục CONFIGURATION CHANGES, để chạy lại snapshot v1 vẫn ra đúng
    # prompt v1.
    changes: list[dict] | None = None
    changes_window_s: float | None = None
    changes_error: str = ""

    # ------------------------------------------------------------------

    def to_prompt_text(self) -> str:
        """Đoạn text đưa vào prompt của LLM.

        Thứ tự có chủ đích: phần lệch đặt lên đầu vì model chú ý phần đầu nhiều nhất,
        mà đó cũng là manh mối mạnh nhất.
        """
        # Service dang bi nghi la cham -> in kem CPU cua chung du chua cham tran.
        slow = {f.source for f in self.diff.slow_edges}
        slow |= {f.target for f in self.diff.slow_edges}
        parts = [
            serialize.describe_diff(self.diff),
            "",
            serialize.describe_red_metrics(self.red),
            "",
            serialize.describe_cpu(self.cpu, slow),
            "",
            serialize.describe_runtime_graph(self.runtime_graph),
            "",
            serialize.describe_pods(
                self.pods,
                expected=(serialize.expected_deployments(self.topology)
                          if self.topology is not None else None),
            ),
            "",
            serialize.describe_resources(self.resources),
        ]
        if self.changes is not None or self.changes_error:
            # Dat ngay sau POD HEALTH, khong dua len dau: day la them bang chung
            # chu khong phai manh moi duoc uu tien hon cac muc khac.
            pos = next(i for i, x in enumerate(parts)
                       if isinstance(x, str) and x.startswith("POD HEALTH:")) + 1
            parts[pos:pos] = ["", serialize.describe_changes(
                self.changes or [], self.changes_window_s, self.changes_error)]
        if self.topology is not None:
            parts = [serialize.describe_topology(self.topology), ""] + parts
        return "\n".join(parts)

    def fingerprint(self) -> str:
        """Mã băm của trạng thái, dùng làm khóa cache khi gọi LLM (mục 7.5 KLTN.md).

        Cố ý KHÔNG băm theo thời điểm chụp và các con số dao động nhẹ, chỉ băm theo
        phần lệch — hai ca cùng triệu chứng thì dùng lại kết quả cũ, khỏi tốn tiền.
        """
        payload = json.dumps(self.diff.to_dict(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "namespace": self.namespace,
            "taken_at": self.taken_at,
            "taken_at_human": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self.taken_at)
            ),
            "fingerprint": self.fingerprint(),
            "span_count": self.span_count,
            "edge_sources": self.edge_sources,
            "runtime_graph": self.runtime_graph.to_dict(),
            "diff": self.diff.to_dict(),
            "red": {k: v.to_dict() for k, v in self.red.items()},
            "resources": {k: v.to_dict() for k, v in self.resources.items()},
            "cpu": self.cpu,
            "pods": [p.to_dict() for p in self.pods],
            "changes": self.changes,
            "changes_window_s": self.changes_window_s,
            "changes_error": self.changes_error,
        }

    def save(self, runs_dir: Path = RUNS_DIR) -> Path:
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.taken_at))
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in self.label)
        path = runs_dir / f"{stamp}_{safe}.json"
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return path


def take_snapshot(
    label: str = "snapshot",
    namespace: str = "default",
    window: str = "5m",
    lookback_seconds: int = 300,
    baseline: ServiceGraph | None = None,
    k8s: K8sClient | None = None,
    prom: PrometheusClient | None = None,
    jaeger: JaegerClient | None = None,
    changes_since: float | None = None,
) -> SystemSnapshot:
    """Chụp một ảnh trạng thái hệ thống.

    `baseline` là runtime graph lúc hệ thống khỏe mạnh. Truyền vào thì phát hiện
    được "chậm gấp mấy lần", không truyền thì chỉ so được với ngưỡng tuyệt đối.

    `changes_since` (epoch giây): chỉ liệt kê thay đổi cấu hình từ mốc này. Bộ chạy
    thí nghiệm đặt mốc là lúc bắt đầu ca, để thay đổi do dọn dẹp ca TRƯỚC không lọt
    vào prompt của ca sau — cùng loại nhiễu chéo giữa các ca như dấu "pod RECREATED"
    đã gặp ở v1. Không đưa thì nhìn lại `CHANGE_WINDOW_S`.
    """
    k8s = k8s or K8sClient(namespace=namespace)
    prom = prom or PrometheusClient()
    jaeger = jaeger or JaegerClient()

    endpoint_map = k8s.service_endpoint_map(namespace)
    spans = jaeger.recent_spans_all(lookback_seconds=lookback_seconds)
    graph = build_runtime_graph(spans, endpoint_map)
    topo = load_logical_topology()

    now = time.time()
    since = changes_since if changes_since is not None else now - CHANGE_WINDOW_S
    changes, changes_error = None, ""
    try:
        changes = collect_changes(k8s, namespace, since,
                                  serialize.expected_deployments(topo))
    except Exception as e:
        changes_error = f"could not read rollout history: {str(e)[:120]}"

    return SystemSnapshot(
        label=label,
        namespace=namespace,
        taken_at=time.time(),
        runtime_graph=graph,
        diff=diff_graphs(topo, graph, baseline),
        red=prom.red_metrics_all(endpoint_map, window),
        resources=prom.pod_resources(namespace),
        pods=k8s.list_pods(namespace),
        topology=topo,
        span_count=len(spans),
        edge_sources=edge_source_summary(graph),
        cpu=prom.cpu_vs_limit(namespace),
        changes=changes,
        changes_window_s=round(now - since, 1),
        changes_error=changes_error,
    )


def collect_changes(k8s: K8sClient, namespace: str, since: float,
                    deployments: list[str]) -> list[dict]:
    """Các phiên bản pod template mới xuất hiện từ `since`, mới nhất trước.

    Mỗi phiên bản so với phiên bản liền trước nó trong lịch sử, giống
    `kubectl rollout history --revision`. Chỉ ghi sự thật quan sát được: số phiên
    bản, cách đây bao lâu, nguyên nhân nếu có ghi, và cấu hình đổi thế nào.
    """
    present = set(k8s.list_deployments(namespace))
    now = time.time()
    out: list[dict] = []
    for dep in deployments:
        if dep not in present:
            continue
        history = k8s.rollout_history(dep, namespace=namespace)
        for i, h in enumerate(history):
            if i == 0 or h.get("changed_at") is None or h["changed_at"] < since:
                continue
            prev = history[i - 1]
            change = describe_fields_change(prev["fields"], h["fields"])
            if h.get("restarted_at") and h.get("restarted_at") != prev.get("restarted_at"):
                change = ("restart requested" if change == "no configuration difference"
                          else change + "; restart requested")
            out.append({
                "deployment": dep,
                "revision": h["revision"],
                "previous_revision": prev["revision"],
                "current": i == len(history) - 1,
                "age_s": round(now - h["changed_at"], 1),
                "cause": h.get("cause"),
                "change": change,
            })
    return sorted(out, key=lambda c: c["age_s"])
