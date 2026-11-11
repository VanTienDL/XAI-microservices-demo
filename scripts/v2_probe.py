"""Đo thử một lỗi có tạo ra triệu chứng mà AGENT nhìn thấy hay không. KHÔNG gọi LLM.

Dùng để chọn kịch bản giữ lại thay H1: `v2_check` cho thấy bóp CPU currencyservice
không sinh cạnh chậm nào, mà agent coi "không cạnh lỗi, không cạnh chậm, không cạnh
thiếu" là hệ thống khỏe và dừng ngay vòng đầu — lỗi đó vô hình với nó.

Mỗi lỗi: chờ pod ổn định, tiêm, chờ 330 giây, chụp ảnh như agent chụp, in phần lệch,
RED và CPU của service bị tiêm, rồi hoàn tác. Kết luận "agent thấy" đúng bằng luật
`healthy` của `react_loop._observe`.

    python scripts/v2_probe.py F4:currencyservice:10m F4:recommendationservice:10m F2:shippingservice
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.eval.preflight import ensure_clean_slate, wait_for_settled_pods  # noqa: E402
from src_thesis.faults.injectors import FaultInjector, load_active_faults  # noqa: E402
from src_thesis.faults.library import inject_one  # noqa: E402
from src_thesis.graph.baseline import load_baseline_graph  # noqa: E402
from src_thesis.k8s_client import K8sClient  # noqa: E402
from src_thesis.telemetry.snapshot import take_snapshot  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "v2_check"
WAIT_S = 330


def say(m: str) -> None:
    try:
        print(m, flush=True)
    except OSError:
        pass


def probe(spec: str, k8s: K8sClient, baseline) -> dict:
    parts = spec.split(":")
    fault, target = parts[0], parts[1]
    params = {"cpu": parts[2]} if fault == "F4" and len(parts) > 2 else {}
    t0 = time.time()
    inject_one(FaultInjector(k8s=k8s), fault, target, params)
    try:
        k8s.wait_ready(target, timeout=240) if fault != "F2" else time.sleep(20)
        say(f"  cho {WAIT_S}s")
        time.sleep(WAIT_S)
        snap = take_snapshot(label=f"probe-{target}", baseline=baseline, changes_since=t0)
        d = snap.to_dict()
        diff = d["diff"]
        n = {k: len(diff.get(k, [])) for k in ("error_edges", "slow_edges", "missing_edges")}
        sees = sum(n.values()) > 0
        edges = {k: [f"{e['source']}->{e['target']} ({e['detail']})" for e in diff.get(k, [])]
                 for k in n}
        red = {s: d["red"].get(s) for s in (target, "frontend")}
        cpu = {p: v for p, v in d.get("cpu", {}).items() if p.startswith(target)}
        say(f"  agent THAY su co: {sees}  {n}")
        for k, es in edges.items():
            for e in es[:6]:
                say(f"    {k}: {e}")
        say(f"  RED {target}: {red[target]}")
        say(f"  RED frontend: {red['frontend']}")
        say(f"  CPU: {cpu}")
        return {"spec": spec, "agent_sees": sees, "counts": n, "edges": edges,
                "red": red, "cpu": cpu}
    finally:
        for f in reversed(load_active_faults()):
            f.revert(K8sClient(namespace=f.namespace))
            k8s.wait_ready(f.ground_truth.target_service, timeout=240)
            say(f"  da hoan tac {f.ground_truth.fault_id}")


def main() -> int:
    k8s = K8sClient(namespace="default")
    ok, problems = ensure_clean_slate(k8s, log=say)
    if not ok:
        say("KHONG CHAY: " + "; ".join(problems))
        return 2
    baseline, src = load_baseline_graph()
    say(f"Anh nen: {src}")
    out = []
    for spec in sys.argv[1:]:
        say(f"\n=== {spec} ===")
        wait_for_settled_pods(k8s, log=say)
        try:
            out.append(probe(spec, k8s, baseline))
        except Exception as e:
            out.append({"spec": spec, "error": str(e)[:300]})
            say(f"  LOI: {e}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_probe.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    say("\nTOM TAT")
    for r in out:
        say(f"  {r['spec']}: agent thay = {r.get('agent_sees')} {r.get('counts', r.get('error'))}")
    say(f"Ghi {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
