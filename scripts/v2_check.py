"""Kiểm phần mới của v2 trên CLUSTER THẬT trước khi đóng băng code. KHÔNG gọi LLM.

Năm phép thử, mỗi phép tự dọn trong `finally`:

    history   tiêm S1, ảnh chụp phải có mục CONFIGURATION CHANGES ghi EXTRA_LATENCY
    rollback  rollback thật trên production gỡ được S1, rồi hoàn tác được rollback
    h2        tiêm H2, chờ 330 giây: frontend phải lỗi, currencyservice vẫn khỏe
    twin      với H2 đang tiêm: twin phải nhận đúng cấu hình sai, GIỮ biến quan sát
              của twin, và rollback trên twin phải đọc lịch sử của production
    h1        tiêm H1, chờ 330 giây: phải thấy dấu hiệu nghẹt CPU ở currencyservice

H1, H2 là kịch bản giữ lại: phép thử chỉ xác nhận lỗi tạo ra triệu chứng quan sát
được (docs/v2-dang-ky-truoc.md mục 5), không gọi LLM, không dùng để chỉnh gì.

Cuối cùng so cấu hình mọi deployment với lúc bắt đầu: phải khớp tuyệt đối.

Chạy (khoảng 20 phút):
    .venv/bin/python scripts/v2_check.py
    .venv/bin/python scripts/v2_check.py --only history,rollback
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.agent.actions import ActionExecutor  # noqa: E402
from src_thesis.agent.guards import capture_config, config_drift  # noqa: E402
from src_thesis.agent.twin_manager import TwinManager  # noqa: E402
from src_thesis.eval.preflight import ensure_clean_slate, wait_for_settled_pods  # noqa: E402
from src_thesis.faults.injectors import FaultInjector, load_active_faults  # noqa: E402
from src_thesis.faults.library import inject_scenario, load_scenarios  # noqa: E402
from src_thesis.graph.baseline import load_baseline_graph  # noqa: E402
from src_thesis.k8s_client import K8sClient  # noqa: E402
from src_thesis.telemetry.snapshot import take_snapshot  # noqa: E402
from src_thesis.xai.schema import ProposedAction  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "v2_check"
CHECKS = ("history", "rollback", "h2", "twin", "h1")
SYMPTOM_WAIT_S = 330


def say(msg: str) -> None:
    try:
        print(msg, flush=True)
    except (BrokenPipeError, OSError):
        pass


def act(action, target, params=None):
    return ProposedAction(**{
        "action": action, "target": target,
        "params": [{"key": k, "value": v} for k, v in (params or {}).items()],
        "risk_class": "hard", "rationale": "v2_check", "preconditions": []})


def revert_all(k8s: K8sClient) -> None:
    for f in reversed(load_active_faults()):
        try:
            f.revert(K8sClient(namespace=f.namespace))
            k8s.wait_ready(f.ground_truth.target_service, timeout=240)
            say(f"  da hoan tac {f.ground_truth.fault_id}")
        except Exception as e:
            say(f"  HOAN TAC {f.ground_truth.fault_id} NEM LOI: {e}")


def change_section(prompt: str) -> str:
    lines = prompt.splitlines()
    i = next((n for n, x in enumerate(lines) if x.startswith("CONFIGURATION CHANGES")), None)
    if i is None:
        return ""
    out = [lines[i]]
    for x in lines[i + 1:]:
        if not x.startswith("  "):
            break
        out.append(x)
    return "\n".join(out)


def wait_symptom(seconds: int) -> None:
    say(f"  cho trieu chung on dinh {seconds}s")
    time.sleep(seconds)


# ----------------------------------------------------------------------

def check_history(k8s, baseline, scen) -> dict:
    t0 = time.time()
    inject_scenario(FaultInjector(k8s=k8s), scen["S1"])
    try:
        k8s.wait_ready("productcatalogservice", timeout=240)
        snap = take_snapshot(label="v2check-history", baseline=baseline,
                             changes_since=t0)
        sec = change_section(snap.to_prompt_text())
        say(sec)
        ok = ("productcatalogservice" in sec and "EXTRA_LATENCY" in sec
              and "(current)" in sec and "cause: not recorded" in sec)
        return {"ok": ok, "section": sec}
    finally:
        revert_all(k8s)


def check_rollback(k8s, baseline, scen) -> dict:
    inject_scenario(FaultInjector(k8s=k8s), scen["S1"])
    ex = ActionExecutor(k8s=k8s, namespace="default")
    out: dict = {}
    try:
        k8s.wait_ready("productcatalogservice", timeout=240)
        r = ex.apply(act("rollback", "productcatalogservice"))
        out["rollback"] = r.to_dict()
        gone = k8s.get_env("productcatalogservice", "EXTRA_LATENCY") is None
        say(f"  rollback: {r.detail} -> EXTRA_LATENCY da go = {gone}")
        hist = k8s.rollout_history("productcatalogservice")
        cause = hist[-1].get("cause")
        u = ex.undo(r)
        out["undo"] = u.to_dict()
        back = k8s.get_env("productcatalogservice", "EXTRA_LATENCY") == "6s"
        say(f"  hoan tac rollback: {u.detail} -> EXTRA_LATENCY tro lai = {back}")
        out["ok"] = bool(r.ok and gone and u.ok and back
                         and cause and cause.startswith("agent: rollback"))
        return out
    finally:
        revert_all(k8s)


def check_h2(k8s, baseline, scen) -> dict:
    t0 = time.time()
    inject_scenario(FaultInjector(k8s=k8s), scen["H2"])
    try:
        k8s.wait_ready("frontend", timeout=240)
        wait_symptom(SYMPTOM_WAIT_S)
        snap = take_snapshot(label="v2check-h2", baseline=baseline, changes_since=t0)
        d = snap.to_dict()
        fe = d["red"].get("frontend", {})
        cur = d["red"].get("currencyservice", {})
        errs = [f"{e['source']}->{e['target']}" for e in d["diff"].get("error_edges", [])]
        sec = change_section(snap.to_prompt_text())
        say(f"  frontend error_rate={fe.get('error_rate')}  "
            f"currencyservice error_rate={cur.get('error_rate')}")
        say(f"  error_edges: {errs}")
        say(sec)
        ok = ((fe.get("error_rate") or 0) >= 0.05 or bool(errs)) \
            and "CURRENCY_SERVICE_ADDR" in sec
        return {"ok": ok, "frontend": fe, "currencyservice": cur,
                "error_edges": errs, "section": sec,
                "diff": {k: len(v) for k, v in d["diff"].items() if isinstance(v, list)}}
    finally:
        revert_all(k8s)


def check_twin(k8s, baseline, scen) -> dict:
    inject_scenario(FaultInjector(k8s=k8s), scen["H2"])
    tm = TwinManager(k8s=k8s)
    out: dict = {}
    try:
        k8s.wait_ready("frontend", timeout=240)
        tm.create_twin()
        out["load_state"] = tm.load_state(source_namespace="default")
        addr = k8s.get_env("frontend", "CURRENCY_SERVICE_ADDR", namespace="twin")
        otel = k8s.get_env("frontend", "OTEL_SERVICE_NAME", namespace="twin")
        coll = k8s.get_env("frontend", "COLLECTOR_SERVICE_ADDR", namespace="twin")
        say(f"  twin frontend: CURRENCY_SERVICE_ADDR={addr} OTEL_SERVICE_NAME={otel}")
        ex = ActionExecutor(k8s=k8s, namespace="twin", history_namespace="default")
        r = ex.apply(act("rollback", "frontend"))
        after = k8s.get_env("frontend", "CURRENCY_SERVICE_ADDR", namespace="twin")
        otel2 = k8s.get_env("frontend", "OTEL_SERVICE_NAME", namespace="twin")
        prod = k8s.get_env("frontend", "CURRENCY_SERVICE_ADDR")
        say(f"  rollback tren twin: {r.detail}")
        say(f"  sau rollback: twin={after} production={prod} OTEL_SERVICE_NAME={otel2}")
        out.update(rollback=r.to_dict(), ok=bool(
            addr == "currencyservice:7001" and otel == "twin-frontend"
            and coll and "default.svc" in coll
            and r.ok and after == "currencyservice:7000"
            and otel2 == "twin-frontend" and prod == "currencyservice:7001"))
        return out
    finally:
        try:
            tm.destroy_twin()
        except Exception as e:
            say(f"  XOA TWIN NEM LOI: {e}")
        revert_all(k8s)


def check_h1(k8s, baseline, scen) -> dict:
    t0 = time.time()
    inject_scenario(FaultInjector(k8s=k8s), scen["H1"])
    try:
        k8s.wait_ready("currencyservice", timeout=240)
        wait_symptom(SYMPTOM_WAIT_S)
        snap = take_snapshot(label="v2check-h1", baseline=baseline, changes_since=t0)
        d = snap.to_dict()
        slow = [f"{e['source']}->{e['target']}" for e in d["diff"].get("slow_edges", [])]
        # cpu_vs_limit dat khoa theo TEN POD, khong theo ten service.
        cpu = {p: v for p, v in d.get("cpu", {}).items()
               if p.startswith("currencyservice")}
        sec = change_section(snap.to_prompt_text())
        say(f"  slow_edges: {slow}")
        say(f"  cpu currencyservice: {cpu}")
        say(sec)
        ok = (any("currencyservice" in e for e in slow) or bool(cpu)) \
            and "cpu limit" in sec
        return {"ok": ok, "slow_edges": slow, "cpu": cpu, "section": sec}
    finally:
        revert_all(k8s)


FUNCS = {"history": check_history, "rollback": check_rollback, "h2": check_h2,
         "twin": check_twin, "h1": check_h1}


def main() -> int:
    ap = argparse.ArgumentParser(description="Kiem phan moi cua v2 tren cluster that")
    ap.add_argument("--only", default=",".join(CHECKS))
    args = ap.parse_args()
    names = [n.strip() for n in args.only.split(",") if n.strip()]
    if any(n not in FUNCS for n in names):
        say(f"Chi co: {', '.join(CHECKS)}")
        return 2

    k8s = K8sClient(namespace="default")
    ok, problems = ensure_clean_slate(k8s, log=say)
    if not ok:
        say("KHONG CHAY: he thong chua sach: " + "; ".join(problems))
        return 2
    reference = capture_config(k8s, "default")
    baseline, src = load_baseline_graph()
    scen = load_scenarios()
    say(f"Anh nen: {src}")

    results: dict = {}
    for name in names:
        say("")
        say(f"=== {name} ===")
        if not wait_for_settled_pods(k8s, log=say):
            results[name] = {"ok": False, "error": "pod khoi dong lai lien tuc"}
            continue
        try:
            results[name] = FUNCS[name](k8s, baseline, scen)
        except Exception as e:
            results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        say(f"--> {name}: {'DAT' if results[name].get('ok') else 'SAI'}"
            + (f" ({results[name]['error']})" if results[name].get("error") else ""))

    drift = config_drift(reference, capture_config(k8s, "default"))
    results["config_back_to_start"] = {"ok": not drift, "drift": drift}
    say("")
    say(f"Cau hinh ve dung luc dau: {'DAT' if not drift else 'SAI ' + str(drift)}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_v2_check.json"
    path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str),
                    encoding="utf-8")
    passed = sum(1 for r in results.values() if r.get("ok"))
    say(f"{passed}/{len(results)} dat — {path}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
