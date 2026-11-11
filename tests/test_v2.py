"""Thử phần mới của thí nghiệm v2 mà KHÔNG cần cluster, KHÔNG gọi LLM.

Chạy:
    .venv\\Scripts\\python.exe -m tests.test_v2

Hai lớp thử:
  - Code THẬT của `k8s_client` (đọc lịch sử phiên bản, vá cấu hình) chạy trên một
    API giả trả về đúng kiểu đối tượng của thư viện `kubernetes`. Đây là chỗ dễ sai
    nhất, vì nó nói chuyện với Kubernetes bằng JSON Patch.
  - Phần còn lại (rollback, twin, ảnh chụp, bộ chạy) dùng cụm giả `FakeK8s` của
    `tests/test_guards.py`.

Phép thử trên cluster thật nằm ở `scripts/v2_check.py`.
"""

from __future__ import annotations

import copy
import json
import sys
import traceback
from datetime import datetime, timezone

from kubernetes.client import (
    V1Container,
    V1Deployment,
    V1DeploymentSpec,
    V1EnvVar,
    V1EnvVarSource,
    V1LabelSelector,
    V1ObjectFieldSelector,
    V1ObjectMeta,
    V1OwnerReference,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ReplicaSet,
    V1ReplicaSetList,
    V1ReplicaSetSpec,
    V1ResourceRequirements,
)

from src_thesis import k8s_client as K
from src_thesis.agent.actions import ActionExecutor
from src_thesis.agent.twin_manager import TwinManager
from src_thesis.eval import metrics as M
from src_thesis.graph import serialize
from src_thesis.telemetry import snapshot as S
from src_thesis.xai.schema import ProposedAction
from tests import test_guards as G
from tests.test_guards import DEFAULT_STATE, FakeK8s, act

DEFAULT_TWIN = DEFAULT_STATE["default"]

# ----------------------------------------------------------------------
# API GIA cho code that cua k8s_client
# ----------------------------------------------------------------------


def _container(env: dict, cpu="200m", image="img:v1", infra=True, value_from=False):
    items = [V1EnvVar(name=k, value=v) for k, v in env.items()]
    if infra:
        items += [V1EnvVar(name="ENABLE_TRACING", value="1"),
                  V1EnvVar(name="OTEL_SERVICE_NAME", value="twin-frontend")]
    if value_from:
        items.append(V1EnvVar(name="POD_IP", value_from=V1EnvVarSource(
            field_ref=V1ObjectFieldSelector(field_path="status.podIP"))))
    return V1Container(name="server", image=image, env=items,
                       resources=V1ResourceRequirements(
                           limits={"cpu": cpu, "memory": "128Mi"},
                           requests={"cpu": "100m", "memory": "64Mi"}))


def _rs(rev: int, env: dict, uid="uid-1", changed_at=None, cause=None, cpu="200m",
        created=None):
    ann = {}
    if changed_at is not None:
        ann[K.CHANGED_AT_ANNOTATION] = str(changed_at)
    if cause:
        ann[K.CHANGE_CAUSE_ANNOTATION] = cause
    return V1ReplicaSet(
        metadata=V1ObjectMeta(
            name=f"frontend-{rev}", annotations={K.REVISION_ANNOTATION: str(rev)},
            owner_references=[V1OwnerReference(api_version="apps/v1", kind="Deployment",
                                               name="frontend", uid=uid)],
            creation_timestamp=created),
        spec=V1ReplicaSetSpec(
            selector=V1LabelSelector(match_labels={"app": "frontend"}),
            template=V1PodTemplateSpec(metadata=V1ObjectMeta(annotations=ann),
                                       spec=V1PodSpec(containers=[_container(env, cpu)]))))


class FakeApps:
    def __init__(self, dep: V1Deployment, rss=None):
        self.dep, self.rss, self.patches = dep, rss or [], []

    def read_namespaced_deployment(self, name, ns):
        return self.dep

    def list_namespaced_replica_set(self, ns, label_selector=""):
        assert label_selector == "app=frontend", label_selector
        return V1ReplicaSetList(items=self.rss)

    def patch_namespaced_deployment(self, name, ns, body, **kw):
        self.patches.append((body, kw))


def _client(dep, rss=None) -> K.K8sClient:
    c = object.__new__(K.K8sClient)
    c.apps, c.namespace = FakeApps(dep, rss), "default"
    return c


def _dep(env, **kw) -> V1Deployment:
    return V1Deployment(
        metadata=V1ObjectMeta(name="frontend", uid="uid-1",
                              annotations={K.REVISION_ANNOTATION: "3"}),
        spec=V1DeploymentSpec(
            replicas=1, selector=V1LabelSelector(match_labels={"app": "frontend"}),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(annotations={"keep.me": "x"}),
                spec=V1PodSpec(containers=[_container(env, **kw)]))))


# ----------------------------------------------------------------------
# 1. CODE THAT CUA k8s_client
# ----------------------------------------------------------------------

def test_history_filters_owner_and_sorts():
    created = datetime(2026, 11, 1, tzinfo=timezone.utc)
    rss = [_rs(3, {"A": "bad"}, changed_at=2000.0, cause="agent: x"),
           _rs(1, {"A": "good"}, created=created),
           _rs(2, {"A": "good"}, uid="khac", changed_at=1500.0)]
    h = _client(_dep({"A": "bad"}), rss).rollout_history("frontend")
    assert [x["revision"] for x in h] == [1, 3], "RS cua deployment khac phai bi loai"
    assert h[0]["changed_at"] == created.timestamp(), "khong co moc thi dung luc tao RS"
    assert h[1]["changed_at"] == 2000.0 and h[1]["cause"] == "agent: x"
    assert h[1]["fields"]["env"] == {"A": "bad"}, "bien ha tang khong duoc lan vao"


def test_apply_fields_keeps_infra_and_value_from():
    c = _client(_dep({"A": "1", "B": "2"}, value_from=True))
    old = c.apply_template_fields("frontend", {"env": {"A": "9", "C": "3"},
                                               "cpu_limit": "400m", "image": "img:v2"},
                                  cause="agent: rollback to revision 1")
    assert old["env"] == {"A": "1", "B": "2"} and old["cpu_limit"] == "200m"
    ops, kw = c.apps.patches[-1]
    assert kw["_content_type"] == "application/json-patch+json"
    by_path = {o["path"]: o for o in ops}
    env = by_path["/spec/template/spec/containers/0/env"]["value"]
    names = [e["name"] for e in env]
    assert names.count("A") == 1 and "B" not in names and "C" in names, names
    assert {"ENABLE_TRACING", "OTEL_SERVICE_NAME", "POD_IP"} <= set(names), \
        "bien ha tang va valueFrom cua ben nhan phai giu nguyen"
    pod_ip = next(e for e in env if e["name"] == "POD_IP")
    assert "valueFrom" in pod_ip and "value_from" not in pod_ip, pod_ip
    res = by_path["/spec/template/spec/containers/0/resources"]["value"]
    assert res["limits"] == {"cpu": "400m", "memory": "128Mi"}, \
        "doi CPU khong duoc lam mat gioi han RAM"
    ann = by_path["/spec/template/metadata/annotations"]["value"]
    assert ann["keep.me"] == "x" and K.CHANGED_AT_ANNOTATION in ann
    assert ann[K.CHANGE_CAUSE_ANNOTATION] == "agent: rollback to revision 1"
    assert by_path["/spec/template/spec/containers/0/image"]["value"] == "img:v2"


def test_apply_fields_noop_does_not_patch():
    c = _client(_dep({"A": "1"}))
    c.apply_template_fields("frontend", {"env": {"A": "1"}, "cpu_limit": "0.2",
                                         "image": "img:v1"})
    assert c.apps.patches == [], "cau hinh da dung thi khong vá (moi lan va = pod moi)"


def test_unset_env_without_cause_clears_old_cause():
    dep = _dep({"A": "1"})
    dep.spec.template.metadata.annotations[K.CHANGE_CAUSE_ANNOTATION] = "agent: cu"
    c = _client(dep)
    c.unset_env("frontend", "A")
    ops, _ = c.apps.patches[-1]
    ann = next(o for o in ops if o["path"] == "/spec/template/metadata/annotations")
    assert K.CHANGE_CAUSE_ANNOTATION not in ann["value"], \
        "thay doi khong ghi nguyen nhan khong duoc mang nguyen nhan cua lan truoc"


def test_deployment_configs_sees_every_env():
    class Apps(FakeApps):
        def list_namespaced_deployment(self, ns):
            class L:
                items = [self.dep]
            return L()
    dep = _dep({"CURRENCY_SERVICE_ADDR": "currencyservice:7000"})
    c = object.__new__(K.K8sClient)
    c.apps, c.namespace = Apps(dep), "default"
    cfg = c.deployment_configs()["frontend"]
    assert cfg["env.CURRENCY_SERVICE_ADDR"] == "currencyservice:7000", cfg
    assert "env.ENABLE_TRACING" not in cfg, "bien ha tang khong thuoc doi sanh"


# ----------------------------------------------------------------------
# 2. ROLLBACK
# ----------------------------------------------------------------------

def test_rollback_to_explicit_revision():
    FakeK8s.reset()
    k = FakeK8s()
    k.set_cpu_limit("productcatalogservice", "10m")             # rev 2
    k.set_cpu_limit("productcatalogservice", "20m", cause="agent: adjust_resources")
    ex = ActionExecutor(k8s=k, namespace="default")
    r = ex.apply(ProposedAction(**act("rollback", "productcatalogservice",
                                      {"to_revision": "revision 1"}, risk="hard")))
    assert r.ok, r
    assert k.get_cpu_limit("productcatalogservice") == "200m", \
        "ve phien ban 1 phai bo ca loi tiem lan thay doi cua agent"
    assert k.rollout_history("productcatalogservice")[-1]["cause"] == \
        "agent: rollback to revision 1"
    bad = ex.apply(ProposedAction(**act("rollback", "productcatalogservice",
                                        {"to_revision": "99"}, risk="hard")))
    assert not bad.ok and not bad.applied, bad


def test_twin_rollback_reads_production_history():
    FakeK8s.reset()
    FakeK8s.state["twin"] = copy.deepcopy(FakeK8s.state["default"])
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s")
    # Twin vua nap trang thai: cung cau hinh loi, nhung lich su rieng cua twin
    # (phien ban truoc la ban sach cua twin).
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s", namespace="twin")
    FakeK8s().set_env("productcatalogservice", "OTHER", "x", namespace="twin")
    ex = ActionExecutor(k8s=FakeK8s(namespace="twin"), namespace="twin",
                        history_namespace="default")
    r = ex.apply(ProposedAction(**act("rollback", "productcatalogservice", risk="hard")))
    assert r.ok, r
    assert FakeK8s.state["twin"]["productcatalogservice"]["env"] == {}, \
        "twin phai ve dung cau hinh ma production se ve (phien ban 1 cua production)"
    assert FakeK8s.state["default"]["productcatalogservice"]["env"] == {
        "EXTRA_LATENCY": "6s"}, "thu tren twin khong duoc dung toi production"


# ----------------------------------------------------------------------
# 3. TWIN NAP TRANG THAI
# ----------------------------------------------------------------------

def test_load_state_copies_any_env_but_not_loadgenerator():
    FakeK8s.reset()
    st = FakeK8s.state["default"]
    st["frontend"] = {"replicas": 1, "cpu_limit": "200m", "cpu_request": "100m",
                      "env": {"CURRENCY_SERVICE_ADDR": "currencyservice:7000"},
                      "image": "fe:v1"}
    st["loadgenerator"] = {"replicas": 1, "cpu_limit": "300m", "cpu_request": "300m",
                           "env": {"USERS": "10"}, "image": "lg:v1"}
    FakeK8s.state["twin"] = copy.deepcopy(st)
    FakeK8s.state["twin"]["loadgenerator"]["env"] = {"USERS": "10", "TWIN": "1"}
    k = FakeK8s()
    k.set_env("frontend", "CURRENCY_SERVICE_ADDR", "currencyservice:7001")
    k.scale_deployment("currencyservice", 0)
    applied = TwinManager(k8s=k).load_state()
    tw = FakeK8s.state["twin"]
    assert tw["frontend"]["env"]["CURRENCY_SERVICE_ADDR"] == "currencyservice:7001", \
        "loi cau hinh o bien bat ky phai co mat tren twin (v1 chi chep EXTRA_LATENCY)"
    assert tw["currencyservice"]["replicas"] == 0
    assert tw["loadgenerator"]["env"]["TWIN"] == "1", "bo sinh tai cua twin giu nguyen"
    assert set(applied) == {"frontend", "currencyservice"}, applied


def test_load_state_tolerates_rollout_stuck_like_production():
    from src_thesis.agent import twin_manager as T

    class Slow(FakeK8s):
        prod_done = False

        def wait_ready(self, dep, timeout=120, namespace=None):
            return False if namespace == "twin" else True

        def rollout_complete(self, dep, namespace=None):
            return Slow.prod_done

    FakeK8s.reset()
    FakeK8s.state["twin"] = copy.deepcopy(FakeK8s.state["default"])
    Slow().set_cpu_limit("productcatalogservice", "10m")
    orig = T.LOAD_READY_TIMEOUT_S
    try:
        applied = TwinManager(k8s=Slow()).load_state()
        assert applied["productcatalogservice"].get("rollout") ==             "chua xong, giong production", applied
        Slow.prod_done = True
        FakeK8s.state["twin"] = copy.deepcopy(DEFAULT_TWIN)
        try:
            TwinManager(k8s=Slow()).load_state()
            raise AssertionError("production xong ma twin ket thi phai bao loi")
        except RuntimeError:
            pass
    finally:
        T.LOAD_READY_TIMEOUT_S = orig


# ----------------------------------------------------------------------
# 4. ANH CHUP: MUC CONFIGURATION CHANGES
# ----------------------------------------------------------------------

def test_collect_changes_respects_cutoff_and_describes_diff():
    FakeK8s.reset()
    k = FakeK8s()
    FakeK8s.clock = 1000.0
    k.set_env("productcatalogservice", "EXTRA_LATENCY", "6s")      # ca truoc
    k.unset_env("productcatalogservice", "EXTRA_LATENCY", cause="agent: undo rollback")
    FakeK8s.clock = 5000.0
    k.set_env("productcatalogservice", "EXTRA_LATENCY", "6s")      # ca nay
    S.time.time = lambda: 5354.0
    try:
        ch = S.collect_changes(k, "default", since=4000.0,
                               deployments=["productcatalogservice", "khong_co"])
    finally:
        import time as _t
        S.time.time = _t.time
    assert len(ch) == 1, "thay doi truoc moc bat dau ca khong duoc lot vao"
    c = ch[0]
    assert c["revision"] == 4 and c["previous_revision"] == 3 and c["current"]
    assert c["change"] == 'env EXTRA_LATENCY: (unset) -> "6s"', c["change"]
    text = serialize.describe_changes(ch, 354.0)
    assert "cause: not recorded" in text and "5.9 min ago" in text, text
    assert "suspicious" not in text.lower() and "fault" not in text.lower()


def test_old_snapshot_prompt_has_no_change_section():
    from src_thesis.graph.diff import GraphDiff
    from src_thesis.graph.model import ServiceGraph
    base = dict(label="x", namespace="default", taken_at=0.0,
                runtime_graph=ServiceGraph(kind="runtime"), diff=GraphDiff())
    old = S.SystemSnapshot(**base)
    assert "CONFIGURATION CHANGES" not in old.to_prompt_text(), \
        "snapshot v1 phai cho dung prompt v1"
    new = S.SystemSnapshot(**base, changes=[], changes_window_s=300)
    text = new.to_prompt_text()
    i, j = text.index("POD HEALTH:"), text.index("CONFIGURATION CHANGES")
    assert i < j, "muc thay doi dat sau POD HEALTH, khong dua len dau"


# ----------------------------------------------------------------------
# 5. DAP AN VA CHAM DIEM
# ----------------------------------------------------------------------

def test_fault_type_accepts_listed_labels_only():
    assert M.fault_type_matches("latency", {"fault_type": "latency"})
    assert not M.fault_type_matches("configuration_error", {"fault_type": "latency"}), \
        "file dap an v1 phai cham nhu cu"
    gt = {"fault_type": "latency", "accepted_fault_types": ["latency",
                                                             "configuration_error"]}
    assert M.fault_type_matches("configuration_error", gt)
    assert not M.fault_type_matches("crash", gt)


def test_v2_ground_truths():
    from src_thesis.faults import injectors as I
    from src_thesis.faults.library import load_scenarios
    saved = (I.ActiveFault.save, I.ActiveFault.save_ground_truth, I.expected_propagation)
    I.ActiveFault.save = lambda self, *a, **k: None
    I.ActiveFault.save_ground_truth = lambda self, *a, **k: None
    I.expected_propagation = lambda t: []
    try:
        FakeK8s.reset()
        FakeK8s.state["default"]["frontend"] = {
            "replicas": 1, "cpu_limit": "200m", "cpu_request": "100m",
            "env": {"CURRENCY_SERVICE_ADDR": "currencyservice:7000"}, "image": "fe:v1"}
        inj = I.FaultInjector(k8s=FakeK8s())
        h2 = load_scenarios()["H2"]
        f5 = inj.inject_bad_config(h2["target"], **h2["params"])
        gt = f5.ground_truth
        assert gt.correct_actions == ["rollback"] and gt.fault_type == "configuration_error"
        assert FakeK8s.state["default"]["frontend"]["env"]["CURRENCY_SERVICE_ADDR"] == \
            "currencyservice:7001"
        f5.revert(FakeK8s())
        assert FakeK8s.state["default"]["frontend"]["env"]["CURRENCY_SERVICE_ADDR"] == \
            "currencyservice:7000", "hoan tac F5 phai tra dung gia tri cu"
        f4 = inj.inject_cpu_throttle("currencyservice")
        assert "rollback" in f4.ground_truth.correct_actions
        try:
            inj.inject_bad_config("frontend", "KHONG_CO", "x")
            raise AssertionError("F5 chi duoc doi bien da co")
        except ValueError:
            pass
    finally:
        I.ActiveFault.save, I.ActiveFault.save_ground_truth, I.expected_propagation = saved
    held = {k for k, v in load_scenarios().items() if v.get("held_out")}
    assert held == {"H1", "H2"}, held


# ----------------------------------------------------------------------
# 6. VONG LAP VA BO CHAY
# ----------------------------------------------------------------------

def test_twin_window_follows_twin_settle():
    seen = []

    class RecVerifier(G.FakeVerifier):
        def measure(self, window="5m"):
            seen.append((self.namespace, window))
            return super().measure(window)

    FakeK8s.reset()
    FakeK8s().set_env("productcatalogservice", "EXTRA_LATENCY", "6s")
    G.FakeSnap.healthy = False
    G.FakeVerifier.queues = {"twin": [G.HEALTHY_RED, G.WORSE_RED]}
    agent = G.make_agent("twin_verified", act("rollback", "productcatalogservice",
                                              risk="hard"))
    agent.twin_verifier = RecVerifier(namespace="twin")
    agent.twin_settle_seconds = 120
    agent.run(save=False)
    twin = [w for ns, w in seen if ns == "twin"]
    assert twin == ["120s", "120s"], seen


def test_runner_restores_reference_config():
    from src_thesis.eval import runner as R
    FakeK8s.reset()
    r = object.__new__(R.EvalRunner)
    r.k8s, r.log = FakeK8s(), (lambda m: None)
    r.config_reference = FakeK8s().deployment_configs()
    orig = R.wait_for_settled_pods
    R.wait_for_settled_pods = lambda *a, **k: True
    try:
        assert r._restore_reference_config() == []
        FakeK8s().set_cpu_limit("currencyservice", "20m", cause="agent: adjust_resources")
        FakeK8s().scale_deployment("shippingservice", 2)
        drift = r._restore_reference_config()
        assert len(drift) >= 2, drift
        assert FakeK8s().deployment_configs() == r.config_reference
    finally:
        R.wait_for_settled_pods = orig


def _fake_reasoner(responses):
    """XaiReasoner that, chi thay client goi API bang hang doi tra loi/loi gia."""
    import httpx
    from openai import APIConnectionError
    from src_thesis.xai import reasoner as RS

    class Msg:
        def __init__(self, content):
            self.message = type("M", (), {"content": content})()

    class Resp:
        def __init__(self, content):
            self.choices = [Msg(content)]
            self.usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 5})()

    class Client:
        def __init__(self):
            self.chat = self
            self.completions = self
            self.calls = 0

        def create(self, **kw):
            self.calls += 1
            item = responses.pop(0) if responses else "conn"
            if item == "conn":
                raise APIConnectionError(request=httpx.Request("POST", "http://x"))
            return Resp(item)

    r = object.__new__(RS.XaiReasoner)
    r._strict_ok, r._schema, r.include_example = True, {}, False
    r.provider = type("P", (), {"name": "fake"})()
    r.model, r.max_retries, r.max_tokens, r.temperature = "m", 3, 100, 0.0
    r.max_rate_limit_waits, r.client = 2, Client()
    return r, RS


GOOD = json.dumps({
    "root_cause_service": "currencyservice", "fault_type": "crash", "confidence": 0.9,
    "reasoning_chain": ["x"], "propagation_path": [], "evidence": ["x"],
    "proposed_actions": [{"action": "scale_up", "target": "currencyservice",
                          "params": [], "risk_class": "easy", "rationale": "x",
                          "preconditions": []}]})


def test_llm_outage_raises_instead_of_fake_result():
    r, RS = _fake_reasoner([])
    orig = RS.time.sleep
    RS.time.sleep = lambda s: None
    try:
        try:
            r._call_with_retry("x")
            raise AssertionError("mat ket noi keo dai phai nem LLMUnavailable")
        except RS.LLMUnavailable:
            pass
        assert r.client.calls == RS.MAX_CONNECTION_RETRIES,             "phai co tran so lan thu, ban cu thu mai mai"
        r, RS = _fake_reasoner(["conn", "conn", GOOD])
        res = r._call_with_retry("x")
        assert res.ok, "dut mang ngan roi goi lai duoc thi phai chay binh thuong"
        r, RS = _fake_reasoner(["khong phai json"] * 3)
        res = r._call_with_retry("x")
        assert not res.ok, "LLM tra sai khuon la ket qua that, khong nem loi"
    finally:
        RS.time.sleep = orig


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
