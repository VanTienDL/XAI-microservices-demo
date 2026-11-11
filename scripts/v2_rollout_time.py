"""Đo bao lâu thì pod bị bóp CPU chạy xong cấu hình mới. KHÔNG gọi LLM.

Lần đo 2026-11-11: bóp currencyservice xuống 10m, sau 330 giây vẫn còn pod CŨ (200m)
phục vụ, pod mới không sẵn sàng — lỗi không hề xảy ra, nên H1 vô hình. Cần biết
điều đó có xảy ra với S4 (frontend) và S5 (productcatalogservice) không, vì twin
nạp trạng thái phải chờ đúng việc này.

    python scripts/v2_rollout_time.py frontend productcatalogservice currencyservice
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.eval.preflight import ensure_clean_slate  # noqa: E402
from src_thesis.faults.injectors import FaultInjector, load_active_faults  # noqa: E402
from src_thesis.k8s_client import K8sClient  # noqa: E402

LIMIT_S = 420
OUT = Path(__file__).resolve().parents[1] / "data" / "v2_check"


def main() -> int:
    k8s = K8sClient(namespace="default")
    ok, problems = ensure_clean_slate(k8s)
    if not ok:
        print("KHONG CHAY: " + "; ".join(problems))
        return 2
    out = {}
    for dep in sys.argv[1:]:
        FaultInjector(k8s=k8s).inject_cpu_throttle(dep, "10m")
        t0 = time.time()
        done_at = None
        try:
            while time.time() - t0 < LIMIT_S:
                if k8s.rollout_complete(dep):
                    done_at = round(time.time() - t0, 1)
                    break
                time.sleep(5)
            pods = [(p.name, p.ready, p.phase) for p in k8s.list_pods()
                    if p.deployment == dep]
            out[dep] = {"rollout_done_s": done_at, "pods": pods}
            print(f"{dep}: {'xong sau ' + str(done_at) + 's' if done_at else 'CHUA XONG sau ' + str(LIMIT_S) + 's'}"
                  f"  pods={pods}", flush=True)
        finally:
            for f in reversed(load_active_faults()):
                f.revert(K8sClient(namespace=f.namespace))
                k8s.wait_ready(f.ground_truth.target_service, timeout=300)
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}_rollout_time.json"
    path.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"Ghi {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
