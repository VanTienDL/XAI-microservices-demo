"""Biến graph, diff và metric thành đoạn văn bản cho LLM đọc.

Viết bằng tiếng Anh vì đây là phần đi thẳng vào prompt: model hiểu tốt hơn và tốn
ít token hơn tiếng Việt. Phần tiếng Việt chỉ dành cho người đọc báo cáo.

Nguyên tắc viết: ngắn, có số, không tính từ. Mỗi dòng thừa đều là tiền API.
"""

from __future__ import annotations

# Pod ha tang, khong thuoc he thong nghiep vu duoc nghien cuu.
# Loc khoi POD HEALTH de LLM khong do loi cho chinh he thong quan sat — nguy co
# nay lo ra o kich ban S3 khi pod jaeger vua duoc khoi dong lai va hien trong prompt.
INFRA_PODS = ("jaeger", "opentelemetrycollector", "loadgenerator")

from src_thesis.graph.diff import GraphDiff
from src_thesis.graph.logical_graph import LogicalTopology
from src_thesis.graph.model import ServiceGraph


def describe_topology(topo: LogicalTopology) -> str:
    """Mô tả hệ thống ở trạng thái thiết kế. Phần này không đổi giữa các ca."""
    lines = ["SYSTEM DESIGN (expected topology):"]
    for src in sorted(topo.graph.nodes):
        targets = topo.graph.successors(src)
        if targets:
            lines.append(f"  {src} -> {', '.join(targets)}")
    lines.append("")
    lines.append("OBSERVABILITY LIMITS (important when reading the data below):")
    untraced = sorted(n for n, t in topo.traced.items() if not t)
    lines.append(
        f"  These services emit NO traces of their own: {', '.join(untraced)}."
    )
    lines.append(
        "  Their latency and error rate are measured indirectly from the caller side, "
        "so the numbers include network time and are slightly higher than reality."
    )
    lines.append(
        "  redis-cart is completely invisible: no data at all does NOT mean it is down."
    )
    lines.append(f"  Critical business path: {' -> '.join(topo.critical_path)}")
    return "\n".join(lines)


def describe_runtime_graph(graph: ServiceGraph, max_edges: int = 40) -> str:
    """Mô tả quan hệ gọi nhau quan sát được, kèm số liệu từng cạnh."""
    lines = ["OBSERVED CALL GRAPH (last observation window):"]
    edges = sorted(graph.edges.items(), key=lambda kv: -kv[1].calls)[:max_edges]
    for (s, t), stat in edges:
        lines.append(
            f"  {s} -> {t}: {stat.calls} calls, "
            f"{stat.error_rate * 100:.1f}% errors, "
            f"avg {stat.avg_ms}ms, max {stat.max_ms}ms"
        )
    if not edges:
        lines.append("  (no calls observed)")
    return "\n".join(lines)


def describe_red_metrics(red: dict, max_rows: int = 20) -> str:
    """Mô tả RED metrics từng service."""
    lines = ["SERVICE METRICS (rate / errors / latency):"]
    rows = sorted(red.values(), key=lambda r: -r.request_rate)[:max_rows]
    for r in rows:
        p95 = "n/a" if r.p95_ms != r.p95_ms else f"{r.p95_ms}ms"
        note = "" if r.source == "server" else " [measured from caller side]"
        lines.append(
            f"  {r.service}: {r.request_rate:.2f} req/s, "
            f"{r.error_rate * 100:.1f}% errors, p95 {p95}{note}"
        )
    if not rows:
        lines.append("  (no metrics)")
    return "\n".join(lines)


def describe_resources(resources: dict, top: int = 8) -> str:
    lines = ["POD RESOURCES (top consumers):"]
    rows = sorted(resources.values(), key=lambda r: -r.memory_bytes)[:top]
    for r in rows:
        lines.append(
            f"  {r.pod}: cpu {r.cpu_cores:.3f} cores, "
            f"mem {r.memory_bytes / 1024 / 1024:.0f}Mi"
        )
    return "\n".join(lines)


def describe_diff(diff: GraphDiff) -> str:
    """Phần lệch giữa thiết kế và thực tế. Thường là manh mối rõ nhất."""
    lines = ["DEVIATIONS FROM DESIGN (strongest signal, read this first):"]
    if diff.is_clean() and not diff.unexpected_edges and not diff.silent_services:
        lines.append("  none - the running system matches the design")
        return "\n".join(lines)

    def block(title: str, findings: list) -> None:
        if not findings:
            return
        lines.append(f"  {title}:")
        for f in findings:
            flag = " [ON CRITICAL PATH]" if f.on_critical_path else ""
            lines.append(f"    {f.source} -> {f.target}: {f.detail}{flag}")

    if diff.throughput_ratio is not None and diff.throughput_ratio < 0.5:
        lines.append(
            f"  WARNING: total call volume is only "
            f"{diff.throughput_ratio * 100:.0f}% of the healthy baseline. When throughput "
            f"collapses, edges disappear from the observation window even though they are "
            f"healthy. Treat MISSING calls below as weak evidence, not as separate faults."
        )
    block("MISSING calls (expected by design, not seen at runtime)", diff.missing_edges)
    block("FAILING calls", diff.error_edges)
    block("SLOW calls", diff.slow_edges)
    block("UNEXPECTED calls (not in design)", diff.unexpected_edges)
    if diff.silent_services:
        lines.append(
            f"  Services that should emit traces but sent nothing: "
            f"{', '.join(diff.silent_services)}"
        )
    return "\n".join(lines)


def describe_cpu(cpu: dict, slow_services: set[str] | None = None,
                 ratio_alert: float = 0.7) -> str:
    """CPU đang dùng so với trần, cho các service đáng ngờ.

    Chỉ in service đang chạm trần hoặc đang bị nghi là chậm — in hết thì tốn token
    mà phần lớn là số 0.07 vô nghĩa.
    """
    lines = ["CPU USAGE vs LIMIT (ratio near 1.0 means the service is CPU-throttled):"]
    rows = []
    for pod, v in cpu.items():
        name = pod.rsplit("-", 2)[0]
        ratio = v.get("ratio")
        interesting = (ratio is not None and ratio >= ratio_alert) or                       (slow_services and name in slow_services)
        if interesting:
            rows.append((name, v))
    for name, v in sorted(rows, key=lambda r: -(r[1].get("ratio") or 0)):
        r = v.get("ratio")
        lines.append(
            f"  {name}: using {v['used_cores']:.3f} of {v['limit_cores']:.3f} cores"
            + (f" ({r * 100:.0f}% of limit)" if r is not None else "")
            + ("  <-- AT LIMIT" if r is not None and r >= ratio_alert else "")
        )
    if not rows:
        # PHAN BIET HAI TRUONG HOP HOAN TOAN KHAC NHAU. Snapshot cua S4 va S5 co
        # truong cpu RONG (Prometheus khong tra ve kube_pod_container_resource_limits),
        # nhung ban cu van in "no service is close to its CPU limit" — tuc la noi voi
        # model rang DA KIEM TRA VA KHONG CO GI, trong khi that ra CHUA DO DUOC GI.
        # Prompt khang dinh mot dieu sai su that thi te hon han prompt thieu du lieu:
        # no chu dong dan model ra khoi dung nguyen nhan.
        if not cpu:
            lines.append(
                "  NO CPU DATA in this window - this is missing data, NOT evidence "
                "that CPU is healthy. Do not rule out resource_exhaustion because "
                "this section is empty."
            )
        else:
            lines.append("  no service is close to its CPU limit")
    return chr(10).join(lines)


def expected_deployments(topo: LogicalTopology) -> list[str]:
    """Danh sach deployment nghiep vu dang le phai dang chay.

    Lay tu so do thiet ke, bo pod ha tang. Dung de phat hien deployment bien mat
    han: khong co danh sach nay thi khong the biet mot cai ten da vang mat, vi
    telemetry chi bao cao nhung gi DANG ton tai.
    """
    return sorted(n for n in topo.graph.nodes if not n.startswith(INFRA_PODS))


def describe_pods(pods: list, recent_restart_s: float = 600.0,
                  expected: list[str] | None = None) -> str:
    """Chỉ liệt kê pod không bình thường. Pod khỏe mạnh không đáng tốn token.

    KHÔNG lọc theo `restarts` vì số đó cộng dồn cả những lần tắt mở cluster, gần như
    pod nào cũng khác 0. Chỉ quan tâm pod VỪA khởi động lại trong `recent_restart_s`
    giây gần đây — đó mới là dấu hiệu sự cố đang diễn ra.
    """
    pods = [p for p in pods if not p.name.startswith(INFRA_PODS)]
    bad = [
        p for p in pods
        if not p.ready
        or p.reason
        or (p.last_restart_age_s is not None and p.last_restart_age_s <= recent_restart_s)
        or (p.age_s is not None and p.age_s <= recent_restart_s)
    ]
    lines = ["POD HEALTH:"]

    # Deployment BIEN MAT HAN. Kich ban S2 ha so ban sao ve 0, luc do khong con
    # pod nao mang ten do nua, nen vong lap `bad` o tren khong the thay gi: no chi
    # duyet nhung pod DANG TON TAI. Thieu doan nay thi snapshot chi ghi
    # "all 10 pods ready" va giau mat bang chung manh nhat, la dang le phai co 11.
    gone: list[str] = []
    if expected:
        running = {p.deployment for p in pods}
        gone = [d for d in expected if d not in running]
    for d in gone:
        lines.append(
            f"  {d}: NO PODS AT ALL - deployment scaled to 0 or every pod is gone"
        )

    if not bad:
        if gone:
            lines.append(f"  the other {len(pods)} pods are ready, no restarts")
        else:
            lines.append(f"  all {len(pods)} pods ready, no restarts")
        return "\n".join(lines)
    for p in bad:
        when = (f", restarted {p.last_restart_age_s:.0f}s ago"
                if p.last_restart_age_s is not None else "")
        if p.age_s is not None and p.age_s <= recent_restart_s:
            when += f", pod was RECREATED {p.age_s:.0f}s ago (previous pod is gone)"
        lines.append(
            f"  {p.name}: phase={p.phase}, ready={p.ready}, "
            f"reason={p.reason or 'n/a'}{when}"
        )
    return "\n".join(lines)


def describe_changes(changes: list[dict], window_s: float | None = None,
                     error: str = "") -> str:
    """Mục CONFIGURATION CHANGES (v2): phiên bản pod template mới trong khoảng quan sát.

    Chỉ ghi sự thật — số phiên bản, bao lâu trước, nguyên nhân nếu có ghi, cấu hình
    đổi thế nào. KHÔNG kèm nhận xét kiểu "thay đổi này đáng ngờ": quyết định thay
    đổi nào liên quan tới sự cố là việc của LLM, gợi ý ở đây là gài sẵn đáp án.

    Đổi số bản sao không phải một phiên bản (đúng như Kubernetes), nên kịch bản hạ
    service về 0 bản sao không hiện ở đây; bằng chứng của nó nằm ở POD HEALTH.
    """
    span = f"last {window_s / 60:.0f} min" if window_s else "observation period"
    lines = [f"CONFIGURATION CHANGES ({span}; pod template revisions, newest first; "
             f"replica scaling is not a revision):"]
    if error:
        lines.append(f"  unavailable: {error}")
        return "\n".join(lines)
    if not changes:
        lines.append("  no pod template changes in this period")
        return "\n".join(lines)
    for c in changes:
        cause = c.get("cause") or "not recorded"
        cur = " (current)" if c.get("current") else ""
        lines.append(
            f"  {c['deployment']} revision {c['revision']}{cur}, "
            f"{c['age_s'] / 60:.1f} min ago, from revision {c['previous_revision']}, "
            f"cause: {cause}: {c['change']}")
    return "\n".join(lines)
