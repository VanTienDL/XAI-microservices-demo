"""Thử ba cơ chế kiểm soát độ lệch trạng thái mà KHÔNG cần cluster, KHÔNG gọi LLM.

Chạy:
    .venv\\Scripts\\python.exe -m tests.test_guards

Dùng một cluster giả trong bộ nhớ (`FakeK8s`) và thay các lớp nói chuyện với
cluster, Prometheus, LLM bằng bản giả. Graph LangGraph và logic của agent là THẬT —
đây là chỗ cần thử. Phép thử trên cluster thật nằm ở `scripts/guard_check.py`.

Mỗi phép thử là một hàm `test_*`. Sai thì in SAI kèm lý do và mã thoát 1.
"""

from __future__ import annotations

import copy
import sys
import traceback

from src_thesis.agent import actions as actions_mod
from src_thesis.agent import react_loop as rl
from src_thesis.agent import verifier as verifier_mod
from src_thesis.agent.actions import ActionExecutor
from src_thesis.agent.guards import (
    check_preconditions,
    config_drift,
)
from src_thesis.eval import metrics as M
from src_thesis.xai.schema import Explanation, ProposedAction

# ----------------------------------------------------------------------
# CLUSTER GIA
# ----------------------------------------------------------------------

DEFAULT_STATE = {
    "default": {
        "currencyservice": {"replicas": 1, "cpu_limit": "200m", "cpu_request": "100m",
                            "env": {}, "image": "currency:v1"},
        "productcatalogservice": {"replicas": 1, "cpu_limit": "200m",
                                  "cpu_request": "100m", "env": {},
                                  "image": "catalog:v1"},
        "shippingservice": {"replicas": 1, "cpu_limit": "200m", "cpu_request": "100m",
                            "env": {}, "image": "shipping:v1"},
    },
}


class FakeK8s:
    """Cluster giả. Trạng thái dùng chung giữa mọi instance, như cluster thật.

    Mỗi lần đổi pod template (env, CPU, image, khởi động lại) sinh một phiên bản mới
    trong `history`, giống ReplicaSet của Kubernetes. Đổi số bản sao thì không.
    """

    state: dict = {}

    def __init__(self, namespace: str = "default", context=None):
        self.namespace = namespace

    @classmethod
    def reset(cls, state=None):
        cls.state = copy.deepcopy(state or DEFAULT_STATE)

    def _d(self, dep, ns):
        return FakeK8s.state[ns or self.namespace][dep]

    @staticmethod
    def _fields(d):
        return {"env": dict(d["env"]), "cpu_limit": d["cpu_limit"],
                "cpu_request": d["cpu_request"], "image": d["image"]}

    def _hist(self, dep, ns):
        d = self._d(dep, ns)
        if "history" not in d:
            d["history"] = [{"revision": 1, "fields": self._fields(d),
                             "changed_at": 0.0, "cause": None, "restarted_at": None}]
        return d["history"]

    def _bump(self, dep, ns, cause, restarted=None):
        h = self._hist(dep, ns)
        h.append({"revision": h[-1]["revision"] + 1,
                  "fields": self._fields(self._d(dep, ns)),
                  "changed_at": FakeK8s.clock, "cause": cause,
                  "restarted_at": restarted})

    clock = 1000.0

    def list_deployments(self, namespace=None):
        return sorted(FakeK8s.state[namespace or self.namespace])

    def get_replicas(self, dep, namespace=None):
        return self._d(dep, namespace)["replicas"]

    def get_cpu_limit(self, dep, namespace=None, container=None):
        return self._d(dep, namespace)["cpu_limit"]

    def get_cpu_request(self, dep, namespace=None, container=None):
        return self._d(dep, namespace)["cpu_request"]

    def get_env(self, dep, key, namespace=None, container=None):
        return self._d(dep, namespace)["env"].get(key)

    def set_env(self, dep, key, value, namespace=None, container=None, cause=None):
        self._hist(dep, namespace)
        old = self._d(dep, namespace)["env"].get(key)
        self._d(dep, namespace)["env"][key] = value
        self._bump(dep, namespace, cause)
        return old

    def unset_env(self, dep, key, namespace=None, container=None, cause=None):
        self._hist(dep, namespace)
        old = self._d(dep, namespace)["env"].pop(key, None)
        self._bump(dep, namespace, cause)
        return old

    def scale_deployment(self, dep, replicas, namespace=None):
        old = self._d(dep, namespace)["replicas"]
        self._d(dep, namespace)["replicas"] = replicas
        return old

    def set_cpu_limit(self, dep, cpu, namespace=None, container=None, cause=None):
        self._hist(dep, namespace)
        d = self._d(dep, namespace)
        d["cpu_limit"], d["cpu_request"] = cpu, cpu
        self._bump(dep, namespace, cause)

    def restore_cpu(self, dep, limit, request, namespace=None, container=None,
                    cause=None):
        self._hist(dep, namespace)
        d = self._d(dep, namespace)
        if limit is not None:
            d["cpu_limit"] = limit
        if request is not None:
            d["cpu_request"] = request
        self._bump(dep, namespace, cause)

    def restart_deployment(self, dep, namespace=None, cause=None):
        self._hist(dep, namespace)
        self._bump(dep, namespace, cause, restarted=str(FakeK8s.clock))
        return "now"

    def container_spec(self, dep, namespace=None):
        return self._fields(self._d(dep, namespace))

    def apply_template_fields(self, dep, fields, namespace=None, cause=None):
        self._hist(dep, namespace)
        d = self._d(dep, namespace)
        old = self._fields(d)
        d["env"] = dict(fields.get("env") or {})
        for k in ("cpu_limit", "cpu_request", "image"):
            if fields.get(k) is not None:
                d[k] = fields[k]
        if self._fields(d) != old:
            self._bump(dep, namespace, cause)
        return old

    def rollout_history(self, dep, namespace=None):
        return copy.deepcopy(self._hist(dep, namespace))

    def list_pods(self, namespace=None):
        return []

    def wait_replicas(self, *a, **k):
        return True

    def wait_ready(self, *a, **k):
        return True

    def rollout_complete(self, *a, **k):
        return True

    def deployment_configs(self, namespace=None, env_keys=None):
        out = {}
        for name, d in FakeK8s.state[namespace or self.namespace].items():
            keys = sorted(d["env"]) if env_keys is None else env_keys
            out[name] = {"replicas": d["replicas"], "image": d["image"],
                         "cpu_limit": d["cpu_limit"], "cpu_request": d["cpu_request"],
                         **{f"env.{k}": d["env"].get(k) for k in keys}}
        return out


class FakeProm:
    def __init__(self, *a, **k):
        pass


HEALTHY_RED = {"frontend": {"request_rate": 5.0, "error_rate": 0.0, "p95_ms": 50.0,
                            "source": "server"}}
WORSE_RED = {"frontend": {"request_rate": 5.0, "error_rate": 0.0, "p95_ms": 1500.0,
                          "source": "server"}}
BETTER_RED = {"frontend": {"request_rate": 5.0, "error_rate": 0.0, "p95_ms": 20.0,
                           "source": "server"}}


class FakeVerifier(verifier_mod.TwinVerifier):
    """Phán quyết THẬT (`compare`), số đo giả lấy lần lượt từ một hàng đợi."""

    queues: dict = {}

    def __init__(self, prom=None, k8s=None, namespace="twin", prefix=None):
        self.namespace = namespace
        self.prefix = ""

    def measure(self, window="5m"):
        q = FakeVerifier.queues.get(self.namespace) or []
        return q.pop(0) if q else HEALTHY_RED


class FakeTwin:
    def __init__(self, *a, **k):
        self.created = 0

    def create_twin(self, *a, **k):
        self.created += 1
        FakeK8s.state["twin"] = copy.deepcopy(FakeK8s.state["default"])

    def load_state(self, source_namespace="default"):
        return {}

    def destroy_twin(self, *a, **k):
        FakeK8s.state.pop("twin", None)
        return True


class FakeSnap:
    """Snapshot giả. `FakeSnap.healthy` quyết định hệ thống đang khỏe hay lỗi."""

    healthy = False

    def __init__(self, label):
        self.label = label

    def to_dict(self):
        diff = ({} if FakeSnap.healthy
                else {"error_edges": [{"src": "frontend", "dst": "x"}]})
        return {"label": self.label, "fingerprint": "f", "taken_at": 0.0,
                "red": HEALTHY_RED, "diff": diff}

    def to_prompt_text(self):
        return "SNAPSHOT"


def fake_take_snapshot(label="snapshot", **kwargs):
    return FakeSnap(label)


class FakeResult:
    def __init__(self, exp):
        self.ok, self.explanation = True, exp
        self.input_tokens, self.output_tokens, self.errors = 10, 5, []


class FakeReasoner:
    """LLM giả: trả về đúng một lời giải thích dựng sẵn, có thể kèm "tác dụng phụ"
    để giả lập một thay đổi trên cluster xảy ra trong lúc LLM đang suy nghĩ."""

    def __init__(self, action: dict, side_effect=None):
        self.action, self.side_effect, self.calls = action, side_effect, 0

    def diagnose(self, prompt):
        self.calls += 1
        if self.side_effect:
            self.side_effect()
        exp = Explanation(
            root_cause_service=self.action["target"], fault_type="crash",
            confidence=0.9, reasoning_chain=["x"], propagation_path=[],
            evidence=["x"], proposed_actions=[ProposedAction(**self.action)])
        return FakeResult(exp)


def make_agent(mode: str, action: dict, side_effect=None, max_rounds=1):
    """Dựng agent THẬT, chỉ thay lớp ngoài cùng nói chuyện với thế giới."""
    rl.K8sClient = FakeK8s
    rl.PrometheusClient = FakeProm
    rl.TwinVerifier = FakeVerifier
    rl.take_snapshot = fake_take_snapshot
    rl.time.sleep = lambda s: None
    actions_mod.K8sClient = FakeK8s
    agent = rl.ReactAgent(mode=mode, max_rounds=max_rounds, settle_seconds=0,
                          reasoner=FakeReasoner(action, side_effect),
                          twin=FakeTwin(), baseline=object())
    return agent


def act(action, target, params=None, risk="easy", pre=None):
    return {"action": action, "target": target,
            "params": [{"key": k, "value": v} for k, v in (params or {}).items()],
            "risk_class": risk, "rationale": "thu", "preconditions": pre or []}


# ----------------------------------------------------------------------
# 1. DIEU KIEN TIEN QUYET — ham thuan
# ----------------------------------------------------------------------

def test_restart_without_pods_is_blocked():
    FakeK8s.reset()
    FakeK8s.state["default"]["currencyservice"]["replicas"] = 0
    ok, res = check_preconditions(ProposedAction(**act("restart_pod", "currencyservice",
                                                       risk="hard")), FakeK8s())
    assert not ok, "restart_pod khi 0 ban sao phai bi chan"
    assert res[0].source == "code" and res[0].ok is False


def test_llm_wrong_belief_is_blocked():
    FakeK8s.reset()
    a = act("scale_up", "currencyservice", {"replicas": "1"},
            pre=[{"kind": "replicas_eq", "target": "currencyservice", "value": "0"}])
    ok, res = check_preconditions(ProposedAction(**a), FakeK8s())
    assert not ok, "LLM tin la 0 ban sao trong khi that la 1, phai bi chan"
    assert any(r.source == "llm" and r.ok is False for r in res)


def test_malformed_llm_value_does_not_block():
    FakeK8s.reset()
    a = act("scale_up", "currencyservice", {"replicas": "2"},
            pre=[{"kind": "replicas_eq", "target": "currencyservice", "value": "mot"}])
    ok, res = check_preconditions(ProposedAction(**a), FakeK8s())
    assert ok, "gia tri sai dang thi 'khong kiem duoc', khong duoc chan"
    assert any(r.ok is None for r in res)


def test_word_as_cpu_param_is_blocked():
    FakeK8s.reset()
    a = act("adjust_resources", "productcatalogservice", {"cpu_limit": "increase"})
    ok, _ = check_preconditions(ProposedAction(**a), FakeK8s())
    assert not ok, "cpu_limit='increase' khong thi hanh duoc, phai bi chan truoc"


def test_rollback_without_earlier_revision_is_blocked():
    FakeK8s.reset()
    ok, _ = check_preconditions(ProposedAction(**act("rollback", "productcatalogservice",
                                                     risk="hard")), FakeK8s())
    assert not ok, "chi co mot phien ban thi khong co gi de quay lui"
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s")
    ok, _ = check_preconditions(ProposedAction(**act("rollback", "productcatalogservice",
                                                     risk="hard")), FakeK8s())
    assert ok, "co phien ban truoc thi duoc quay lui"
    ok, _ = check_preconditions(ProposedAction(**act(
        "rollback", "productcatalogservice", {"to_revision": "2"}, risk="hard")),
        FakeK8s())
    assert not ok, "quay ve chinh phien ban hien tai la vo nghia"


def test_no_action_always_passes():
    FakeK8s.reset()
    ok, res = check_preconditions(ProposedAction(**act("no_action", "none")), FakeK8s())
    assert ok and res == []


# ----------------------------------------------------------------------
# 2. DOI SANH TRANG THAI — ham thuan
# ----------------------------------------------------------------------

def test_drift_detects_changes_and_ignores_notation():
    FakeK8s.reset()
    k = FakeK8s()
    before = k.deployment_configs()
    assert config_drift(before, k.deployment_configs()) == []
    FakeK8s.state["default"]["productcatalogservice"]["cpu_limit"] = "0.2"
    assert config_drift(before, k.deployment_configs()) == [], \
        "0.2 va 200m la cung mot gia tri, khong duoc bao lech"
    FakeK8s.state["default"]["shippingservice"]["replicas"] = 2
    d = config_drift(before, k.deployment_configs())
    assert d == ["shippingservice.replicas: 1 -> 2"], d
    assert config_drift(None, before), "khong doc duoc thi phai tinh la lech"


# ----------------------------------------------------------------------
# 3. ROLLBACK HOAN TAC DUOC
# ----------------------------------------------------------------------

def test_rollback_can_be_undone():
    FakeK8s.reset()
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s")
    ex = ActionExecutor(k8s=FakeK8s(), namespace="default")
    r = ex.apply(ProposedAction(**act("rollback", "productcatalogservice", risk="hard")))
    assert r.ok and r.undo_kind == "template", r
    assert FakeK8s().get_env("productcatalogservice", "EXTRA_LATENCY") is None
    u = ex.undo(r)
    assert u.ok, u
    assert FakeK8s().get_env("productcatalogservice", "EXTRA_LATENCY") == "6s"


# ----------------------------------------------------------------------
# 4. CA VONG LAP AGENT
# ----------------------------------------------------------------------

def test_agent_precheck_blocks_before_twin():
    FakeK8s.reset()
    FakeK8s.state["default"]["currencyservice"]["replicas"] = 0
    FakeSnap.healthy = False
    agent = make_agent("twin_verified", act("restart_pod", "currencyservice",
                                            risk="hard"))
    rep = agent.run(save=False)
    r = rep["rounds"][0]
    assert r["blocked_by"] == "precondition", r["blocked_by"]
    assert not r["twin_used"], "bi chan o precheck thi khong duoc dung twin"
    assert agent.twin.created == 0
    assert rep["blocked_by_precondition"] == 1


def test_agent_drift_blocks_apply():
    FakeK8s.reset()
    FakeSnap.healthy = False

    def someone_scales():
        FakeK8s.state["default"]["shippingservice"]["replicas"] = 2

    agent = make_agent("twin_verified",
                       act("scale_up", "currencyservice", {"replicas": "2"}),
                       side_effect=someone_scales)
    rep = agent.run(save=False)
    r = rep["rounds"][0]
    assert r["blocked_by"] == "drift", r["blocked_by"]
    assert r["drift"] == ["shippingservice.replicas: 1 -> 2"], r["drift"]
    assert FakeK8s.state["default"]["currencyservice"]["replicas"] == 1, \
        "bi chan vi lech thi khong duoc dung vao cluster"
    assert rep["drift_checks"] == 1 and rep["blocked_by_drift"] == 1


def test_agent_auto_undo_when_worse():
    FakeK8s.reset()
    FakeSnap.healthy = False
    FakeVerifier.queues = {"default": [HEALTHY_RED, WORSE_RED]}
    agent = make_agent("twin_verified", act("adjust_resources", "productcatalogservice",
                                            {"cpu_limit": "10m"}))
    rep = agent.run(save=False)
    r = rep["rounds"][0]
    assert r["watch"]["verdict"]["verdict"] == "worse", r["watch"]
    assert r["auto_undo"] and r["auto_undo"]["ok"], r["auto_undo"]
    assert FakeK8s.state["default"]["productcatalogservice"]["cpu_limit"] == "200m", \
        "tu hoan tac phai tra tran CPU ve 200m"
    assert rep["auto_undos"] == 1


def test_direct_mode_records_but_never_undoes():
    FakeK8s.reset()
    FakeSnap.healthy = False
    FakeVerifier.queues = {"default": [HEALTHY_RED, WORSE_RED]}
    agent = make_agent("direct", act("adjust_resources", "productcatalogservice",
                                     {"cpu_limit": "10m"}))
    rep = agent.run(save=False)
    r = rep["rounds"][0]
    assert not r["guards"]
    assert r["watch"]["verdict"]["verdict"] == "worse", \
        "direct van phai do truoc/sau de cham harmful bang cung thuoc"
    assert r["auto_undo"] is None
    assert FakeK8s.state["default"]["productcatalogservice"]["cpu_limit"] == "10m"


def test_agent_skips_when_recovered_during_twin():
    FakeK8s.reset()
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s")
    FakeSnap.healthy = False
    FakeVerifier.queues = {"twin": [WORSE_RED, BETTER_RED]}
    agent = make_agent("twin_verified", act("rollback", "productcatalogservice",
                                            risk="hard"))
    # He thong tu khoe lai trong luc twin dang thu: doi co ngay khi twin bi xoa.
    orig_destroy = agent.twin.destroy_twin

    def destroy_and_recover(*a, **k):
        FakeSnap.healthy = True
        return orig_destroy()

    agent.twin.destroy_twin = destroy_and_recover
    rep = agent.run(save=False)
    r = rep["rounds"][0]
    assert r["twin_verdict"]["verdict"] == "better", r["twin_verdict"]
    assert r["blocked_by"] == "recovered", r["blocked_by"]
    assert FakeK8s.state["default"]["productcatalogservice"]["env"].get(
        "EXTRA_LATENCY") == "6s", "da tu khoe thi khong duoc ap rollback"
    assert rep["actions_rejected_by_twin"] == 0, \
        "twin noi better, bi chan vi lech khong phai la twin chan"


# ----------------------------------------------------------------------
# 5. CHI SO
# ----------------------------------------------------------------------

def test_mode_summary_rates():
    a = M.CaseOutcome("S1", "twin_verified", 1, "a", drift_checks=4, blocked_by_drift=1,
                      blocked_recovered=1, twin_better=2, twin_better_transferred=1)
    b = M.CaseOutcome("S2", "twin_verified", 1, "b")
    s = M.summarize_mode([a, b])
    assert s.drift_failure_rate == 0.5, s.drift_failure_rate
    assert s.twin_transfer_rate == 0.5, s.twin_transfer_rate
    empty = M.summarize_mode([b])
    assert empty.drift_failure_rate is None and empty.twin_transfer_rate is None, \
        "chua doi sanh lan nao thi la None, khong phai 0"


def test_old_case_files_still_load():
    old = {"scenario": "S1", "mode": "direct", "repeat": 1, "run_id": "x"}
    assert M.CaseOutcome(**old).auto_undos == 0
    p = ProposedAction(action="scale_up", target="x", risk_class="easy", rationale="r")
    assert p.preconditions == []


# ----------------------------------------------------------------------
# 6. CHAY NGAT QUANG: cho pod lang, dau van tay code, file phien
# ----------------------------------------------------------------------

def test_wait_for_settled_pods():
    from src_thesis.eval import preflight as P
    from src_thesis.k8s_client import PodInfo

    clock = {"t": 0.0}

    class Pods:
        def list_pods(self, ns):
            return [
                PodInfo("jaeger-x", "jaeger", "Running", True, 0, "", None,
                        10 + clock["t"]),             # ha tang: bo qua
                PodInfo("frontend-x", "frontend", "Running", True, 0, "", None,
                        400 + clock["t"]),            # con thieu 200s
                PodInfo("cart-x", "cartservice", "Running", True, 3, "",
                        100 + clock["t"], 9999),      # vua khoi dong lai: tinh 100s
            ]

    waits = []

    def fake_sleep(s):
        waits.append(s)
        clock["t"] += s

    young = P.young_pods(Pods())
    assert [n for n, _ in young] == ["cart-x", "frontend-x"], young
    ok = P.wait_for_settled_pods(Pods(), sleep=fake_sleep, log=lambda m: None)
    assert ok and waits == [505.0], waits
    clock["t"] = 0.0
    assert not P.wait_for_settled_pods(Pods(), max_wait_s=60, sleep=fake_sleep,
                                       log=lambda m: None), \
        "cho qua han thi phai tra False, khong treo"


def test_code_fingerprint_ignores_line_endings(tmp=None):
    import tempfile
    from pathlib import Path
    from src_thesis.eval.runner import code_fingerprint

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "src_thesis").mkdir()
        f = root / "src_thesis" / "a.py"
        f.write_bytes(b"x = 1\n")
        a = code_fingerprint(root)["code_sha"]
        f.write_bytes(b"x = 1\r\n")
        b = code_fingerprint(root)["code_sha"]
        f.write_bytes(b"x = 2\n")
        c = code_fingerprint(root)["code_sha"]
    assert a == b, "cung code tren Windows va Linux phai cung dau van tay"
    assert a != c, "doi mot ky tu thi dau van tay phai doi"


def test_resume_refuses_changed_code():
    from src_thesis.eval.runner import EvalRunner

    r = object.__new__(EvalRunner)
    r.log = lambda m: None
    r.fingerprint = {"code_sha": "aaa", "prompt_version": "v6", "model": "m",
                     "extra_hard_actions": [], "twin_settle_seconds": 300}
    r.allow_code_change = False
    # Phien cu chua co khoa extra_hard_actions: coi nhu khong ep hanh dong nao.
    assert r._session_matches({"code_sha": "aaa", "prompt_version": "v6", "model": "m"})
    assert not r._session_matches({"code_sha": "bbb", "prompt_version": "v6",
                                   "model": "m"})
    assert not r._session_matches({"code_sha": "aaa", "prompt_version": "v6", "model": "m",
                                   "extra_hard_actions": ["adjust_resources"]}), \
        "doi cong vao twin la thi nghiem khac, khong duoc resume lan"
    assert not r._session_matches({"code_sha": "aaa", "prompt_version": "v6", "model": "m",
                                   "twin_settle_seconds": 120}), \
        "doi thoi luong quan sat tren twin la thi nghiem khac"
    r.allow_code_change = True
    assert r._session_matches({"code_sha": "bbb", "prompt_version": "v6", "model": "m"})


def test_forced_hard_action_goes_through_twin():
    from src_thesis.agent import actions as A

    assert A.risk_of("adjust_resources") == "easy"
    A.set_extra_hard_actions(["adjust_resources"])
    try:
        assert A.risk_of("adjust_resources") == "hard" and A.needs_twin("adjust_resources")
        assert A.risk_of("scale_up") == "easy", "chi ep dung hanh dong duoc chon"
        FakeK8s.reset()
        FakeSnap.healthy = False
        FakeVerifier.queues = {"twin": [HEALTHY_RED, WORSE_RED]}
        agent = make_agent("twin_verified", act("adjust_resources",
                                                "productcatalogservice",
                                                {"cpu_limit": "400m"}))
        rep = agent.run(save=False)
        r = rep["rounds"][0]
        assert r["twin_used"] and r["twin_verdict"]["verdict"] == "worse", r["twin_verdict"]
        assert FakeK8s.state["default"]["productcatalogservice"]["cpu_limit"] == "200m", \
            "twin noi te di thi khong duoc ap len he thong chinh"
    finally:
        A.set_extra_hard_actions([])
    assert A.risk_of("adjust_resources") == "easy", "phai tra ve nhu cu sau phep thu"
    try:
        A.set_extra_hard_actions(["khong_ton_tai"])
        raise AssertionError("ten hanh dong la phai bi tu choi")
    except ValueError:
        pass


# ----------------------------------------------------------------------
# 7. LOI LO RA O PHIEN DAU TREN MAY AO (20261027-071311)
# ----------------------------------------------------------------------

def test_error_drop_outranks_latency_rise():
    """So that cua ca S2_direct_r1: scale_up cuu currencyservice. Ban cu cham
    `harmful` vi p95 frontend tang 1.73 -> 21.95ms, trong khi loi giam 76.8% -> 14.4%."""
    before = {"frontend": {"request_rate": 2.9228, "error_rate": 0.7683, "p95_ms": 1.73},
              "checkoutservice": {"request_rate": 0.0912, "error_rate": 1.0,
                                  "p95_ms": 1.95}}
    after = {"frontend": {"request_rate": 3.014, "error_rate": 0.1444, "p95_ms": 21.95},
             "checkoutservice": {"request_rate": 0.1088, "error_rate": 0.2258,
                                 "p95_ms": 72.5}}
    eff = M.classify_action_effect("scale_up", "currencyservice", before, after)
    assert eff.verdict == "helpful", (eff.verdict, eff.reason)
    # Chieu nguoc lai van phai la harmful: loi tang thang moi thu.
    eff = M.classify_action_effect("x", "y", after, before)
    assert eff.verdict == "harmful", eff.verdict


def test_agent_stop_is_followed_until_healthy():
    """Agent dung vi no_action khi he thong chua khoe: runner phai theo doi tiep va
    MTTR lay luc thay khoe, khong cat cut."""
    from src_thesis.eval import runner as R

    healthy_after = {"n": 2}

    class Snap:
        def __init__(self, label):
            healthy_after["n"] -= 1
            self.ok = healthy_after["n"] <= 0
            self.label = label

        def to_dict(self):
            return {"label": self.label, "taken_at": 1000.0 if self.ok else 900.0,
                    "diff": {} if self.ok else {"slow_edges": [1]}, "red": {}}

    r = object.__new__(R.EvalRunner)
    r.log = lambda m: None
    r.baseline, r.dry_run, r.baseline_give_up_s = None, False, 600
    orig_snap, orig_sleep = R.take_snapshot, R.time.sleep
    R.take_snapshot = lambda label, baseline=None: Snap(label)
    R.time.sleep = lambda s: None
    try:
        rounds, rec = r._observe_until_healthy("x", deadline=float("inf"),
                                               first_round_no=3)
    finally:
        R.take_snapshot, R.time.sleep = orig_snap, orig_sleep
    assert rec == 1000.0 and [x["round_no"] for x in rounds] == [3, 4], rounds

    case = {"scenario": "S3", "mode": "direct", "repeat": 1, "case_id": "c",
            "injected_at": 800.0, "model": "gpt-4.1-mini", "ground_truth": [],
            "report": {"run_id": "c", "rounds": [], "healthy_at_end": True,
                       "recovered_at": 1000.0, "started_at": 850, "took_s": 5},
            "final_red": {}}
    out = r._score(case)
    assert out.recovered and out.mttr_s == 200.0, (out.recovered, out.mttr_s)


def test_wait_clean_slate_retries_transient():
    from src_thesis.eval import preflight as P

    answers = [(False, ["1 pod chua san sang: x"]), (False, ["1 pod chua san sang: x"]),
               (True, [])]
    orig = P.ensure_clean_slate
    P.ensure_clean_slate = lambda k8s=None, log=None: answers.pop(0)
    try:
        ok, _ = P.wait_clean_slate(object(), sleep=lambda s: None, log=lambda m: None)
        assert ok, "pod dang tat la tam thoi, phai cho chu khong dung ca phien"
        P.ensure_clean_slate = lambda k8s=None, log=None: (False, ["con 1 loi"])
        ok, probs = P.wait_clean_slate(object(), max_wait_s=0, sleep=lambda s: None,
                                       log=lambda m: None)
        assert not ok and probs == ["con 1 loi"], "ban that thi van phai bao ban"
    finally:
        P.ensure_clean_slate = orig


def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"DAT  {name}")
        except Exception:
            failed += 1
            print(f"SAI  {name}")
            print("     " + traceback.format_exc().strip().splitlines()[-1])
    print("")
    print(f"{len(tests) - failed}/{len(tests)} dat")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
