"""Thử ba cơ chế kiểm soát độ lệch trạng thái trên CLUSTER THẬT, không gọi LLM.

Vì sao cần, ngoài `tests/test_guards.py`: trong thí nghiệm chính gần như không có
gì khác đổi cấu hình production, nên số lần "bị chặn vì lệch" gần như chắc chắn
bằng 0. Đó là kết quả đúng của môi trường, không phải bằng chứng cơ chế hoạt động.
Bằng chứng là bốn phép thử dưới đây: TỰ TẠO độ lệch rồi xem agent có chặn không.

Hành động được chọn sẵn như thể LLM vừa đề xuất. Gọi thẳng các node của agent thật
(`_precheck`, `_guard_before_apply`, `_apply`, `_watch`) trên cluster thật. Phần
nối các node với nhau đã thử ở `tests/test_guards.py`.

    precondition  hạ currencyservice về 0 bản sao, đưa restart_pod vào  -> phải chặn
    drift         chụp cấu hình, đổi tay shippingservice lên 2 bản sao   -> phải chặn
    recovered     hệ thống đang khỏe, giả như vừa thử trên twin xong     -> phải bỏ không áp
    undo          hạ trần CPU productcatalogservice xuống 10m            -> phải tự hoàn tác

Ba phép đầu mất dưới 1 phút. `undo` mất khoảng 10 phút vì phải chờ hai cửa sổ quan
sát (đo sau khi áp, rồi chờ sau khi hoàn tác).

Mỗi phép thử TỰ DỌN trong `finally`, và cả script từ chối chạy nếu hệ thống chưa sạch.

Chạy:
    .venv\\Scripts\\python.exe scripts\\guard_check.py
    .venv\\Scripts\\python.exe scripts\\guard_check.py --only precondition,drift
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.agent.guards import capture_config
from src_thesis.agent.react_loop import ReactAgent, RoundLog
from src_thesis.eval.preflight import ensure_clean_slate
from src_thesis.graph.baseline import load_baseline_graph
from src_thesis.k8s_client import K8sClient

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "guard_check"


class NoLLM:
    """Đặt vào chỗ reasoner để chắc chắn script không gọi LLM, không tốn tiền."""

    model = "khong-goi-llm"

    def diagnose(self, prompt):
        raise RuntimeError("guard_check khong duoc goi LLM")


def act(action, target, params=None, risk="easy"):
    return {"action": action, "target": target,
            "params": [{"key": k, "value": v} for k, v in (params or {}).items()],
            "risk_class": risk, "rationale": "guard_check", "preconditions": []}


def new_round(agent: ReactAgent, label: str) -> RoundLog:
    agent._round_log = RoundLog(round_no=1, snapshot_label=f"guard-{label}",
                                guards=True)
    return agent._round_log


# ----------------------------------------------------------------------

def check_precondition(agent: ReactAgent, k8s: K8sClient) -> dict:
    svc = "currencyservice"
    old = k8s.get_replicas(svc)
    try:
        k8s.scale_deployment(svc, 0)
        k8s.wait_replicas(svc, 0, timeout=120)
        log = new_round(agent, "precondition")
        started = time.time()
        agent._precheck({"action": act("restart_pod", svc, risk="hard")})
        took = round(time.time() - started, 2)
        ok = log.blocked_by == "precondition"
        return {"pass": ok, "took_s": took, "blocked_by": log.blocked_by,
                "checks": log.precheck}
    finally:
        k8s.scale_deployment(svc, old)
        k8s.wait_ready(svc, timeout=180)


def check_drift(agent: ReactAgent, k8s: K8sClient) -> dict:
    svc = "shippingservice"
    old = k8s.get_replicas(svc)
    try:
        seen = capture_config(k8s)
        k8s.scale_deployment(svc, old + 1)
        log = new_round(agent, "drift")
        target = "currencyservice"
        before = k8s.get_replicas(target)
        out = agent._guard_before_apply(
            {"config_seen": seen},
            agent_action(act("scale_up", target, {"replicas": str(before + 1)})))
        untouched = k8s.get_replicas(target) == before
        return {"pass": log.blocked_by == "drift" and untouched and out is not None,
                "blocked_by": log.blocked_by, "drift": log.drift,
                "target_untouched": untouched}
    finally:
        k8s.scale_deployment(svc, old)
        k8s.wait_ready(svc, timeout=180)


def check_recovered(agent: ReactAgent, k8s: K8sClient) -> dict:
    """Hệ thống đang khỏe, giả như vừa thử trên twin xong. Agent phải bỏ không áp.

    Không dựng twin thật: thứ cần thử là phép chụp nhanh production bằng Prometheus
    và Jaeger thật, còn đường đi qua twin đã thử ở tests/.
    """
    log = new_round(agent, "recovered")
    log.twin_used = True
    target = "currencyservice"
    before = k8s.get_replicas(target)
    agent._guard_before_apply(
        {"config_seen": capture_config(k8s)},
        agent_action(act("scale_up", target, {"replicas": str(before + 1)})))
    untouched = k8s.get_replicas(target) == before
    return {"pass": log.blocked_by == "recovered" and untouched,
            "blocked_by": log.blocked_by, "reason": log.skipped_reason,
            "target_untouched": untouched}


def check_undo(agent: ReactAgent, k8s: K8sClient) -> dict:
    svc = "productcatalogservice"
    old_limit, old_req = k8s.get_cpu_limit(svc), k8s.get_cpu_request(svc)
    try:
        log = new_round(agent, "undo")
        state = {"action": act("adjust_resources", svc, {"cpu_limit": "10m"}),
                 "config_seen": capture_config(k8s)}
        state.update(agent._apply(state) or {})
        print(f"  da ap: {(log.action_result or {}).get('detail')}", flush=True)
        print(f"  cho {agent.settle_seconds}s roi do lai production...", flush=True)
        agent._watch(state)
        verdict = ((log.watch or {}).get("verdict") or {})
        restored = k8s.get_cpu_limit(svc)
        undone = bool(log.auto_undo and log.auto_undo.get("ok"))
        return {"pass": verdict.get("verdict") == "worse" and undone,
                "verdict": verdict.get("verdict"), "reason": verdict.get("reason"),
                "auto_undo": log.auto_undo, "cpu_limit_after": restored,
                "cpu_limit_before": old_limit}
    finally:
        # Luoi an toan: tu hoan tac hong hoac phan quyet khong ra "worse" thi van
        # phai tra tran CPU ve nhu cu.
        if k8s.get_cpu_limit(svc) != old_limit:
            k8s.restore_cpu(svc, old_limit, old_req)
            k8s.wait_ready(svc, timeout=180)


def agent_action(d: dict):
    from src_thesis.xai.schema import ProposedAction
    return ProposedAction(**d)


CHECKS = {
    "precondition": check_precondition,
    "drift": check_drift,
    "recovered": check_recovered,
    "undo": check_undo,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Thu co che kiem soat do lech tren cluster that")
    ap.add_argument("--only", default=",".join(CHECKS),
                    help="danh sach phep thu, cach nhau bang dau phay")
    ap.add_argument("--settle", type=int, default=300,
                    help="giay cho truoc khi do lai o phep thu undo")
    args = ap.parse_args()
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        print(f"Khong co phep thu: {', '.join(unknown)}")
        return 2

    k8s = K8sClient(namespace="default")
    ok, problems = ensure_clean_slate(k8s, log=print)
    if not ok:
        print("He thong chua sach, khong chay:")
        for p in problems:
            print(f"  - {p}")
        return 1

    baseline, src = load_baseline_graph()
    print(f"Anh nen: {src}")
    agent = ReactAgent(mode="twin_verified", reasoner=NoLLM(), baseline=baseline,
                       settle_seconds=args.settle)

    results: dict = {}
    for n in names:
        print(f"\n=== {n} ===", flush=True)
        try:
            results[n] = CHECKS[n](agent, k8s)
        except Exception as e:
            results[n] = {"pass": False, "error": str(e)[:300]}
        r = results[n]
        print(f"  {'DAT' if r.get('pass') else 'SAI'}  "
              + json.dumps({k: v for k, v in r.items() if k != 'pass'},
                           ensure_ascii=False)[:400])

    ok, problems = ensure_clean_slate(k8s, log=print)
    results["_clean_after"] = {"pass": ok, "problems": problems}
    print(f"\nHe thong sach sau khi thu: {ok}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_guard_check.json"
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Da ghi: {out}")
    passed = sum(1 for k, r in results.items() if not k.startswith("_") and r.get("pass"))
    print(f"{passed}/{len(names)} dat")
    return 0 if passed == len(names) and ok else 1


if __name__ == "__main__":
    sys.exit(main())
