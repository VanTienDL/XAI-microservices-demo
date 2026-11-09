"""Vòng lặp ReAct: nối XAI, hành động và Digital Twin thành một agent tự sửa lỗi.

Đây là mục 7.4 KLTN.md, và là chỗ cả đề tài hướng tới.

    Observe  -> chụp snapshot hệ thống
    Reason   -> LLM chẩn đoán, xuất JSON đã validate
    Select   -> lấy hành động ưu tiên cao nhất
    rẽ nhánh theo risk_class:
        easy, medium -> Apply thẳng lên production
        hard         -> VerifyOnTwin: dựng twin, thử, đo
                        better -> Apply lên production
                        khác   -> quay lại Reason kèm kết quả twin làm phản hồi
    Observe lại -> khỏi thì dừng, chưa khỏi thì vòng tiếp, tối đa 3 vòng

BA CHẾ ĐỘ, để phase 6 so sánh (mục 8 KLTN.md):

    direct        bỏ qua twin, hành động nào cũng áp thẳng — đây là ĐỐI CHỨNG
    twin_verified hành động `hard` phải qua twin — đây là đề tài này
    xai_only      chỉ chẩn đoán, không hành động — đo riêng chất lượng XAI

Chế độ `direct` cố ý làm liều: nó tồn tại để đo xem twin ngăn được bao nhiêu hành
động có hại. Không có nó thì con số "agent-có-twin an toàn hơn" không so với cái gì.

KIỂM SOÁT ĐỘ LỆCH TRẠNG THÁI (cờ `guards`, mặc định chỉ bật ở `twin_verified`):

    observe -> reason -> select -> precheck --dieu kien sai--> het vong
                                      |  easy/medium          | hard
                                      v                       v
                                    apply <----better----- twin
               (kiem lai dieu kien, doi sanh cau hinh, neu da qua twin thi
                kiem xem production con loi khong, do truoc, thi hanh)
                                      |
                                    watch   (cho mot cua so, do lai production)
                                      |  te di
                                    tu hoan tac

Bật ở `direct` thì không tách được phần an toàn nào đến từ twin, phần nào đến từ các
cơ chế này — nên `direct` không bật. Nhưng CẢ HAI chế độ đều đo production trước và
sau mỗi hành động ở node `watch`, để chỉ số "hành động gây hại" chấm bằng cùng một
thước. `watch` thay cho lần chờ `settle_seconds` vốn đã có sau mỗi hành động, nên
chế độ `direct` không chậm đi.

VÌ SAO TRẦN 3 VÒNG: không có trần thì agent gặp lỗi nó không sửa được sẽ lặp vô hạn,
mỗi vòng tốn một lượt gọi LLM và ít nhất 5 phút chờ. Hết trần thì dừng và xuất báo
cáo "không tự sửa được" kèm lời giải thích — đó cũng là một kết quả hợp lệ, không
phải thất bại của chương trình.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Annotated, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from src_thesis.agent.actions import ActionExecutor, ActionResult, needs_twin, risk_of
from src_thesis.agent.guards import (
    capture_config,
    check_preconditions,
    config_drift,
    describe_failed,
)
from src_thesis.agent.twin_manager import TwinManager
from src_thesis.agent.verifier import TwinVerifier, Verdict
from src_thesis.graph.baseline import load_baseline_graph
from src_thesis.graph.model import ServiceGraph
from src_thesis.k8s_client import K8sClient
from src_thesis.telemetry.prometheus_client import PrometheusClient
from src_thesis.telemetry.snapshot import take_snapshot
from src_thesis.xai.reasoner import XaiReasoner
from src_thesis.xai.schema import Explanation, ProposedAction

RUNS_DIR = Path(__file__).resolve().parents[2] / "data" / "agent_runs"

MAX_ROUNDS = 3

Mode = Literal["direct", "twin_verified", "xai_only"]


def _append(left: list, right: list) -> list:
    """Gộp danh sách khi LangGraph hợp nhất trạng thái giữa các node."""
    return (left or []) + (right or [])


def compact_red(red: dict) -> dict:
    """Rút bảng RED xuống ba con số mỗi service để nhét vừa nhật ký vòng.

    Giữ `request_rate` chứ không chỉ giữ lỗi và độ trễ: thiếu nó thì không phân
    biệt được "service này không xấu đi" với "service này quá ít lưu lượng để nói
    gì" — đúng cái bẫy đã làm hỏng lần đo fidelity đầu tiên ở phase 4.

    Bỏ các tên mang tiền tố `twin-` để số liệu twin không lẫn vào production khi
    twin đang chạy. Đây là lỗi im lặng nhất của phase 4: hai nguồn trộn vào nhau
    mà con số vẫn ra đẹp.
    """
    out: dict = {}
    for name, v in (red or {}).items():
        if name.startswith("twin-"):
            continue
        out[name] = {
            "request_rate": v.get("request_rate", 0.0),
            "error_rate": v.get("error_rate", 0.0),
            "p95_ms": v.get("p95_ms", 0.0),
            "source": v.get("source", ""),
        }
    return out


@dataclass
class RoundLog:
    """Nhật ký một vòng. Đây là đơn vị nhỏ nhất mà phase 6 đọc lại được."""

    round_no: int
    snapshot_label: str = ""
    snapshot_fingerprint: str = ""
    diff_summary: str = ""
    healthy: bool = False
    # Thoi diem CHUP, khac `started_at` o cho no la moc de tinh MTTR (chi so 3
    # muc 8): he thong duoc coi la hoi phuc tai thoi diem quan sat thay no sach,
    # khong phai tai thoi diem vong bat dau.
    observed_at: float = 0.0
    # Bang RED goc cua vong nay. PHAI luu, vi chi so 4 (harmful action) la phep so
    # RED truoc va sau moi hanh dong: `red` cua vong N la "truoc", `red` cua vong
    # N+1 la "sau" — agent da cho du mot cua so quan sat giua hai lan.
    #
    # Khong luu thi phase 6 phai chay lai ca thi nghiem moi cham diem duoc, trai
    # nguyen tac dau `src_thesis/eval/metrics.py`: cham diem lai tu file JSON.
    red: dict = field(default_factory=dict)
    # Phan biet BA truong hop, khong duoc gop: chua chan doan (he thong khoe nen
    # khong can), chan doan THAT BAI, va chan doan XONG. Gop lai thi log ghi
    # "XAI that bai" cho mot ca ma XAI chua he chay — doc lai se hieu nham hoan toan.
    reasoning_ran: bool = False
    explanation: dict | None = None
    reasoning_ok: bool = False
    reasoning_error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    chosen_action: dict | None = None
    risk_class: str = ""
    twin_verdict: dict | None = None
    twin_used: bool = False
    action_result: dict | None = None
    promoted: bool = False
    skipped_reason: str = ""
    # --- kiem soat do lech trang thai ---
    guards: bool = False
    # Ket qua kiem dieu kien tien quyet (danh sach CheckResult.to_dict()).
    precheck: list = field(default_factory=list)
    # Co doi sanh cau hinh ngay truoc khi ap khong, va lech nhung gi.
    drift_checked: bool = False
    drift: list = field(default_factory=list)
    # "" | "precondition" | "drift" | "recovered". "recovered" la lech ve SUC KHOE:
    # production da tu khoe lai trong luc twin dang thu, nen khong ap nua.
    blocked_by: str = ""
    # Do production truoc/sau hanh dong: {"before", "after", "verdict"}. Day la
    # cap so dung de cham "hanh dong gay hai", ke ca khi da tu hoan tac.
    watch: dict | None = None
    auto_undo: dict | None = None
    started_at: float = field(default_factory=time.time)
    took_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class AgentState(TypedDict, total=False):
    """Trạng thái chảy qua graph LangGraph."""

    run_id: str
    mode: str
    namespace: str
    round_no: int
    max_rounds: int

    snapshot: dict          # snapshot hien tai, dang dict
    prompt_text: str
    explanation: dict | None
    action: dict | None
    verdict: dict | None

    feedback: str           # ket qua twin lan truoc, nhoi lai vao prompt
    # Cau hinh production ma quyet dinh dua tren: chup luc observe, chup lai luc
    # twin chep trang thai. Ngay truoc khi ap se so voi cai nay.
    config_seen: dict | None
    healthy: bool
    stop_reason: str
    rounds: Annotated[list, _append]


class ReactAgent:
    """Agent ReAct. Tạo một lần rồi chạy nhiều ca."""

    def __init__(
        self,
        mode: Mode = "twin_verified",
        namespace: str = "default",
        max_rounds: int = MAX_ROUNDS,
        reasoner: XaiReasoner | None = None,
        twin: TwinManager | None = None,
        settle_seconds: int = 300,
        dry_run: bool = False,
        baseline: ServiceGraph | None = None,
        guards: bool | None = None,
        twin_settle_seconds: int | None = None,
        changes_since: float | None = None,
    ):
        self.mode = mode
        self.namespace = namespace
        self.max_rounds = max_rounds
        self.k8s = K8sClient(namespace=namespace)
        self.prom = PrometheusClient()
        self.executor = ActionExecutor(k8s=self.k8s, namespace=namespace)
        self.reasoner = reasoner or XaiReasoner()
        self.twin = twin or TwinManager()
        self.twin_verifier = TwinVerifier(prom=self.prom, namespace="twin")
        # Cung mot lop do va phan quyet cho production, de "twin noi tot len" va
        # "production do thay tot len" so duoc voi nhau (ti le chuyen giao).
        self.prod_verifier = TwinVerifier(prom=self.prom, k8s=self.k8s,
                                          namespace=namespace)
        self.guards = (mode == "twin_verified") if guards is None else guards

        # ANH NEN — BAT BUOC de phat hien duoc kich ban cham.
        #
        # Khong co nen thi `diff_graphs` chi bat canh cham hon 500ms tuyet doi, ma so
        # do that cua S1, S4, S5 deu duoi 500ms (101-284ms). Agent se nhin ba kich
        # ban do va ket luan "he thong khoe manh" roi dung ngay vong dau — hong im
        # lang, khong bao loi gi.
        #
        # Xem chu thich day du o src_thesis/graph/baseline.py.
        if baseline is not None:
            self.baseline, self.baseline_source = baseline, "do ben goi truyen vao"
        else:
            self.baseline, self.baseline_source = load_baseline_graph()
        # Sau moi hanh dong phai cho DAY mot cua so quan sat roi moi do lai. Bai hoc
        # dat nhat cua phase 2: cho ngan hon cua so thi so lieu "sau" con lan trang
        # thai "truoc", va agent se ket luan nham rang hanh dong khong co tac dung.
        self.settle_seconds = settle_seconds
        # Thoi gian quan sat RIENG tren twin (v2, thi nghiem thoi luong o de cuong).
        # Tach khoi `settle_seconds` vi cai do con la cua so node `watch` do hanh
        # dong gay hai tren production: doi chung thi doi ca dinh nghia chi so 4.
        self.twin_settle_seconds = (settle_seconds if twin_settle_seconds is None
                                    else twin_settle_seconds)
        # Moc bat dau liet ke thay doi cau hinh trong anh chup (v2). Xem
        # `take_snapshot(changes_since=...)`.
        self.changes_since = changes_since
        # dry_run bo qua moi thao tac doi cluster. Dung de thu graph chay dung luong
        # ma khong dung toi he thong that.
        self.dry_run = dry_run
        self._round_log: RoundLog | None = None
        self.graph = self._build_graph()

    # ------------------------------------------------------------------
    # CAC NODE
    # ------------------------------------------------------------------

    def _observe(self, state: AgentState) -> AgentState:
        """Chụp trạng thái hệ thống. Vào vòng nào cũng chụp lại từ đầu."""
        rnd = state.get("round_no", 0) + 1
        self._round_log = RoundLog(round_no=rnd)

        snap = take_snapshot(label=f"agent-{state['run_id']}-r{rnd}",
                             namespace=self.namespace,
                             baseline=self.baseline,
                             changes_since=self.changes_since)
        d = snap.to_dict()
        self._round_log.snapshot_label = d.get("label", "")
        self._round_log.snapshot_fingerprint = d.get("fingerprint", "")
        self._round_log.observed_at = d.get("taken_at", time.time())
        self._round_log.red = compact_red(d.get("red", {}))

        diff = d.get("diff", {})
        n_err = len(diff.get("error_edges", []))
        n_slow = len(diff.get("slow_edges", []))
        n_miss = len(diff.get("missing_edges", []))
        self._round_log.diff_summary = (
            f"{n_err} canh loi, {n_slow} canh cham, {n_miss} canh thieu")
        healthy = n_err == 0 and n_slow == 0 and n_miss == 0
        self._round_log.healthy = healthy
        self._round_log.guards = self.guards

        out: AgentState = {
            "round_no": rnd,
            "snapshot": d,
            "prompt_text": snap.to_prompt_text(),
            "healthy": healthy,
        }
        if self.guards and not self.dry_run:
            # Chup NGAY sau snapshot: day la trang thai ma LLM sap nhin thay.
            out["config_seen"] = capture_config(self.k8s, self.namespace)
        return out

    def _reason(self, state: AgentState) -> AgentState:
        """Gọi LLM chẩn đoán. Output luôn validate bằng Pydantic (mục 5 KLTN.md)."""
        prompt = state["prompt_text"]
        feedback = state.get("feedback", "")
        if feedback:
            # Nhoi ket qua twin cua vong truoc vao prompt. Day chinh la phan
            # "Observe" cua ReAct: agent hoc tu hau qua hanh dong vua roi.
            prompt = (prompt + "\n\nPREVIOUS ATTEMPT IN THIS INCIDENT:\n" + feedback
                      + "\n\nPropose a different action that addresses the same "
                        "root cause, or set root_cause_service to 'none' with "
                        "action no_action if the system has recovered.")

        res = self.reasoner.diagnose(prompt)
        log = self._round_log
        log.reasoning_ran = True
        log.reasoning_ok = res.ok
        log.input_tokens = res.input_tokens
        log.output_tokens = res.output_tokens
        if not res.ok:
            log.reasoning_error = res.errors[-1][:300] if res.errors else "khong ro"
            return {"explanation": None}

        log.explanation = res.explanation.model_dump()
        return {"explanation": log.explanation}

    def _select(self, state: AgentState) -> AgentState:
        """Lấy hành động ưu tiên cao nhất và gắn mức rủi ro."""
        exp = state.get("explanation")
        if not exp:
            return {"action": None}
        actions = exp.get("proposed_actions") or []
        if not actions:
            return {"action": None}
        top = actions[0]
        self._round_log.chosen_action = top
        self._round_log.risk_class = risk_of(top.get("action", ""))
        return {"action": top}

    def _precheck(self, state: AgentState) -> AgentState:
        """Kiểm điều kiện tiên quyết trên production, TRƯỚC khi dựng twin.

        Đặt trước twin vì đây là lớp rẻ: chặn một hành động bất khả thi mất dưới
        một giây, trong khi để twin phát hiện thì phải dựng, chạy và đo hết khoảng
        12 phút. Twin để dành cho tác động chỉ đo mới biết.
        """
        action = ProposedAction(**state["action"])
        log = self._round_log
        if self.dry_run:
            return {"verdict": None}
        ok, results = check_preconditions(action, self.k8s, self.namespace)
        log.precheck = [r.to_dict() for r in results]
        if ok:
            return {"verdict": None}
        log.blocked_by = "precondition"
        log.skipped_reason = f"dieu kien tien quyet sai: {describe_failed(results)}"
        return {"feedback": (
            f"Action {action.action} on {action.target} was NOT executed because "
            f"its preconditions are false on the live cluster: "
            f"{describe_failed(results)}. Re-read the snapshot and propose an action "
            f"that matches the actual state.")}

    def _verify_on_twin(self, state: AgentState) -> AgentState:
        """Dựng twin, nạp trạng thái production, thử hành động, đo, rồi xóa twin.

        Twin luôn bị xóa kể cả khi có lỗi giữa chừng — mục 2 KLTN.md cấm chạy twin
        song song với thí nghiệm production, và twin còn sống là còn ăn RAM.
        """
        action = ProposedAction(**state["action"])
        log = self._round_log
        log.twin_used = True

        if self.dry_run:
            v = Verdict("better", "dry_run: gia dinh twin xac nhan")
            log.twin_verdict = v.to_dict()
            return {"verdict": v.to_dict()}

        config_seen = state.get("config_seen")
        try:
            self.twin.create_twin()
            if self.guards:
                # Twin duoc dong bo tu production O THOI DIEM NAY, nen day moi la
                # trang thai ma phan quyet cua twin dua tren. Doi sanh luc ap phai
                # so voi cai nay, khong phai voi luc observe.
                config_seen = capture_config(self.k8s, self.namespace)
            self.twin.load_state(source_namespace=self.namespace)

            # Lich su phien ban doc tu production: `rollback` tren twin phai quay ve
            # dung cau hinh ma no se quay ve tren production.
            twin_exec = ActionExecutor(namespace="twin",
                                       history_namespace=self.namespace)
            # Cho bo sinh tai trong twin am len du mot cua so, neu khong thi phep do
            # "truoc" con lan luc twin chua co luu luong.
            time.sleep(self.twin_settle_seconds)
            before = self.twin_verifier.measure(window=self._twin_window())

            applied = twin_exec.apply(action)
            if not applied.ok:
                v = Verdict("no_change",
                            f"khong thi hanh duoc tren twin: {applied.detail}")
                log.twin_verdict = v.to_dict()
                return {"verdict": v.to_dict(), "config_seen": config_seen}

            time.sleep(self.twin_settle_seconds)
            after = self.twin_verifier.measure(window=self._twin_window())
            v = self.twin_verifier.compare(before, after)
        except Exception as e:
            # Twin hong thi KHONG duoc coi la da xac nhan. Mac dinh an toan la
            # khong cho ap len production.
            v = Verdict("no_change", f"twin gap loi: {str(e)[:200]}")
        finally:
            try:
                self.twin.destroy_twin()
            except Exception:
                pass

        log.twin_verdict = v.to_dict()
        return {"verdict": v.to_dict(), "config_seen": config_seen}

    def _twin_window(self) -> str:
        """Cửa sổ đo trên twin, BẰNG đúng thời gian chờ.

        Chờ 120 giây mà đo cửa sổ 5 phút thì số "sau" còn 3 phút dữ liệu từ trước
        khi thi hành — bài học pha loãng của phase 2. Mặc định 300 giây trùng với
        cửa sổ "5m" của v1, nên chế độ mặc định đo y như cũ.
        """
        return f"{int(self.twin_settle_seconds)}s"

    def _healthy_now(self, label: str) -> tuple[bool, str]:
        """Chụp nhanh production, trả về (đã khỏe chưa, tóm tắt). Dùng cùng luật
        "khỏe" với node observe."""
        snap = take_snapshot(label=label, namespace=self.namespace,
                             baseline=self.baseline, k8s=self.k8s, prom=self.prom)
        diff = snap.to_dict().get("diff", {})
        n = [len(diff.get(k, [])) for k in ("error_edges", "slow_edges",
                                            "missing_edges")]
        return sum(n) == 0, f"{n[0]} canh loi, {n[1]} canh cham, {n[2]} canh thieu"

    def _guard_before_apply(self, state: AgentState,
                            action: ProposedAction) -> AgentState | None:
        """Ba lần kiểm ngay trước khi đụng vào production. Trả về None nếu được áp,
        hoặc phần trạng thái cần trả ra nếu bị chặn."""
        log = self._round_log

        # (a) Kiem lai dieu kien tien quyet: nhanh twin co the da troi qua 12 phut.
        ok, results = check_preconditions(action, self.k8s, self.namespace)
        log.precheck = [r.to_dict() for r in results]
        if not ok:
            log.blocked_by = "precondition"
            log.skipped_reason = f"dieu kien tien quyet sai luc ap: {describe_failed(results)}"
            return {"feedback": (
                f"Action {action.action} on {action.target} was NOT executed: its "
                f"preconditions became false before execution: "
                f"{describe_failed(results)}.")}

        # (b) Doi sanh cau hinh voi luc quyet dinh.
        log.drift_checked = True
        log.drift = config_drift(state.get("config_seen"),
                                 capture_config(self.k8s, self.namespace))
        if log.drift:
            log.blocked_by = "drift"
            log.skipped_reason = f"lech trang thai: {'; '.join(log.drift)[:300]}"
            return {"feedback": (
                f"Action {action.action} on {action.target} was NOT executed because "
                f"the production configuration changed after the decision was made: "
                f"{'; '.join(log.drift)}. Diagnose again from the new state.")}

        # (c) Lech ve suc khoe: chi xet khi da qua twin, vi chi nhanh do moi co
        # khoang trong vai phut giua luc quan sat va luc ap. Ca dien hinh la xoa
        # pod: Kubernetes tao lai sau ~30 giay, twin thi can ~12 phut.
        if log.twin_used:
            healthy, summary = self._healthy_now(
                f"{log.snapshot_label}-truoc-khi-ap")
            if healthy:
                log.blocked_by = "recovered"
                log.skipped_reason = ("production da tu khoe lai trong luc kiem chung "
                                      f"tren twin ({summary}), khong ap nua")
                return {"feedback": (
                    f"Action {action.action} on {action.target} was NOT executed: "
                    f"production recovered on its own while the action was being "
                    f"tested on the twin.")}
        return None

    def _apply(self, state: AgentState) -> AgentState:
        """Thi hành hành động lên production."""
        action = ProposedAction(**state["action"])
        log = self._round_log

        if action.action != "no_action" and self.guards and not self.dry_run:
            blocked = self._guard_before_apply(state, action)
            if blocked is not None:
                return blocked

        before = None
        if action.action != "no_action" and not self.dry_run:
            # Do production TRUOC khi thi hanh, cho node watch so sanh.
            try:
                before = self.prod_verifier.measure()
            except Exception as e:
                log.watch = {"error": f"khong do duoc truoc khi ap: {str(e)[:200]}"}

        if self.dry_run:
            r = ActionResult(action=action.action, target=action.target,
                             namespace=self.namespace, applied=False, verified=True,
                             detail="dry_run: khong dung toi cluster")
        else:
            r = self.executor.apply(action)
        log.action_result = r.to_dict()
        log.promoted = r.ok
        if before is not None:
            log.watch = {"before": before}

        if action.action == "no_action":
            return {"stop_reason": "agent chon khong lam gi"}
        if not r.ok:
            # Hanh dong that bai cung la mot ket qua, phai ghi lai va nhoi vao vong
            # sau — day chinh la "wasted action count" o muc 8 KLTN.md.
            return {"feedback": f"Action {action.action} on {action.target} failed: "
                                f"{r.detail}"}
        return {"feedback": f"Action {action.action} on {action.target} was applied: "
                            f"{r.detail}"}

    def _watch(self, state: AgentState) -> AgentState:
        """Chờ một cửa sổ, đo lại production, tệ đi thì tự hoàn tác.

        Chờ ở đây THAY cho lần chờ sau hành động vốn có, nên không tốn thêm thời
        gian — trừ khi phải hoàn tác, lúc đó chờ thêm một cửa sổ để vòng sau quan
        sát đúng trạng thái sau hoàn tác.

        Phán quyết dùng đúng `TwinVerifier.compare` mà twin dùng. Hai bên cùng một
        thước thì mới tính được tỉ lệ chuyển giao twin -> production.
        """
        log = self._round_log
        ar = log.action_result or {}
        action = state.get("action") or {}
        feedback = state.get("feedback", "")
        if self.dry_run:
            return {}

        time.sleep(self.settle_seconds)
        watch = log.watch or {}
        before = watch.get("before")
        try:
            after = self.prod_verifier.measure()
        except Exception as e:
            watch["error"] = f"khong do duoc sau khi ap: {str(e)[:200]}"
            log.watch = watch
            return {}
        watch["after"] = after
        if before is not None:
            v = self.prod_verifier.compare(before, after)
            watch["verdict"] = v.to_dict()
        log.watch = watch

        verdict = (watch.get("verdict") or {}).get("verdict")
        if verdict:
            feedback += f" Production check after one window: {verdict}."

        if (self.guards and verdict == "worse"
                and ar.get("undo_kind", "none") != "none"):
            payload = {k: v for k, v in ar.items() if k != "ok"}
            undo = self.executor.undo(ActionResult(**payload))
            log.auto_undo = undo.to_dict()
            time.sleep(self.settle_seconds)
            feedback = (
                f"Action {action.get('action')} on {action.get('target')} was "
                f"applied, production got WORSE "
                f"({(watch.get('verdict') or {}).get('reason', '')}), and the action "
                f"was automatically undone "
                f"({'ok' if undo.ok else 'undo FAILED: ' + undo.detail}). "
                f"Do not propose this same action again.")
        return {"feedback": feedback}

    def _reject(self, state: AgentState) -> AgentState:
        """Twin không xác nhận, không áp lên production."""
        v = state.get("verdict") or {}
        action = state.get("action") or {}
        log = self._round_log
        log.promoted = False
        log.skipped_reason = (
            f"twin phan quyet '{v.get('verdict')}': {v.get('reason', '')}")
        return {
            "feedback": (
                f"Action {action.get('action')} on {action.get('target')} was tested "
                f"on the digital twin and REJECTED. Twin verdict: "
                f"{v.get('verdict')} — {v.get('reason', '')}. "
                f"Do not propose this same action again.")
        }

    def _finish_round(self, state: AgentState) -> AgentState:
        """Đóng nhật ký vòng hiện tại."""
        log = self._round_log
        log.took_s = round(time.time() - log.started_at, 2)
        return {"rounds": [log.to_dict()]}

    # ------------------------------------------------------------------
    # CAC NHANH
    # ------------------------------------------------------------------

    def _after_observe(self, state: AgentState) -> str:
        if state.get("healthy"):
            return "done"
        if state.get("round_no", 1) > self.max_rounds:
            return "exhausted"
        return "reason"

    def _after_select(self, state: AgentState) -> str:
        exp = state.get("explanation")
        action = state.get("action")
        if not exp:
            return "failed"
        if not action:
            return "failed"
        if self.mode == "xai_only":
            return "observe_only"

        name = action.get("action", "")
        if name == "no_action":
            return "apply"
        if self.guards:
            return "precheck"
        return self._after_precheck(state)

    def _after_precheck(self, state: AgentState) -> str:
        if self._round_log is not None and self._round_log.blocked_by:
            return "blocked"
        name = (state.get("action") or {}).get("action", "")
        if self.mode == "twin_verified" and needs_twin(name):
            return "twin"
        return "apply"

    def _after_twin(self, state: AgentState) -> str:
        v = state.get("verdict") or {}
        return "apply" if v.get("verdict") == "better" else "reject"

    def _after_apply(self, state: AgentState) -> str:
        """Chỉ đo lại khi hành động thực sự đã đổi cluster."""
        ar = self._round_log.action_result if self._round_log else None
        if ar and ar.get("applied") and ar.get("action") != "no_action":
            return "watch"
        return "done"

    # ------------------------------------------------------------------

    def _build_graph(self):
        g = StateGraph(AgentState)
        g.add_node("observe", self._observe)
        g.add_node("reason", self._reason)
        g.add_node("select", self._select)
        g.add_node("precheck", self._precheck)
        g.add_node("twin", self._verify_on_twin)
        g.add_node("apply", self._apply)
        g.add_node("watch", self._watch)
        g.add_node("reject", self._reject)
        g.add_node("finish_round", self._finish_round)

        g.add_edge(START, "observe")
        g.add_conditional_edges("observe", self._after_observe, {
            "reason": "reason",
            "done": "finish_round",
            "exhausted": "finish_round",
        })
        g.add_edge("reason", "select")
        g.add_conditional_edges("select", self._after_select, {
            "precheck": "precheck",
            "twin": "twin",
            "apply": "apply",
            "reject": "reject",
            "failed": "finish_round",
            "observe_only": "finish_round",
        })
        g.add_conditional_edges("precheck", self._after_precheck, {
            "twin": "twin",
            "apply": "apply",
            "blocked": "finish_round",
        })
        g.add_conditional_edges("twin", self._after_twin, {
            "apply": "apply",
            "reject": "reject",
        })
        g.add_conditional_edges("apply", self._after_apply, {
            "watch": "watch",
            "done": "finish_round",
        })
        g.add_edge("watch", "finish_round")
        g.add_edge("reject", "finish_round")
        g.add_edge("finish_round", END)
        return g.compile()

    # ------------------------------------------------------------------
    # CHAY
    # ------------------------------------------------------------------

    def run(self, run_id: str | None = None, save: bool = True,
            on_round=None) -> dict:
        """Chạy trọn một ca, tối đa `max_rounds` vòng.

        LangGraph chạy MỘT vòng mỗi lần gọi `invoke`; vòng lặp bên ngoài ở đây quyết
        định có đi tiếp không. Cố ý tách như vậy: điều kiện dừng phụ thuộc vào việc
        đo lại hệ thống sau khi hành động, mà phép đo đó cần chờ đủ một cửa sổ quan
        sát — nhồi cả phần chờ vào trong graph làm nó khó đọc và khó thử.
        """
        run_id = run_id or uuid.uuid4().hex[:8]
        state: AgentState = {
            "run_id": run_id,
            "mode": self.mode,
            "namespace": self.namespace,
            "round_no": 0,
            "max_rounds": self.max_rounds,
            "feedback": "",
            "rounds": [],
        }
        started = time.time()
        stop_reason = ""

        for _ in range(self.max_rounds):
            state = {**state, **self.graph.invoke(state)}
            last = state["rounds"][-1] if state["rounds"] else {}

            # Bao ngay khi mot vong xong, khong doi ca ca chay het. Mot vong co the
            # mat 15 phut khi phai dung twin, va chay 15 phut ma khong in gi thi
            # khong phan biet duoc dang chay voi dang treo — dung bai hoc da tra gia
            # o phase 3 khi loat danh gia treo 16 phut trong im lang.
            if on_round is not None:
                try:
                    on_round(last)
                except Exception:
                    pass

            if state.get("healthy"):
                stop_reason = "he thong da khoe manh"
                break
            if last.get("reasoning_ran") and not last.get("reasoning_ok"):
                stop_reason = "XAI khong chan doan duoc"
                break
            if self.mode == "xai_only":
                stop_reason = "che do xai_only: chi chan doan, khong hanh dong"
                break
            if last.get("chosen_action", {}).get("action") == "no_action":
                stop_reason = "agent chon khong lam gi"
                break
        else:
            stop_reason = f"het tran {self.max_rounds} vong ma chua khoi"

        report = {
            "run_id": run_id,
            "mode": self.mode,
            "namespace": self.namespace,
            "started_at": started,
            "started_at_human": time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(started)),
            "took_s": round(time.time() - started, 2),
            "rounds_used": len(state.get("rounds", [])),
            "max_rounds": self.max_rounds,
            # Ghi lai da chay voi anh nen nao. Doc lai mot ca cu ma khong biet no
            # dung nen nao thi khong giai thich duoc vi sao no phat hien hay bo sot.
            "baseline_source": self.baseline_source,
            "has_baseline": self.baseline is not None,
            "healthy_at_end": bool(state.get("healthy")),
            "stop_reason": stop_reason,
            "total_input_tokens": sum(r.get("input_tokens", 0)
                                      for r in state.get("rounds", [])),
            "total_output_tokens": sum(r.get("output_tokens", 0)
                                       for r in state.get("rounds", [])),
            "actions_applied": sum(1 for r in state.get("rounds", [])
                                   if r.get("promoted")),
            "actions_rejected_by_twin": sum(1 for r in state.get("rounds", [])
                                            if r.get("twin_used")
                                            and not r.get("promoted")
                                            and not r.get("blocked_by")),
            "guards": self.guards,
            "blocked_by_precondition": sum(1 for r in state.get("rounds", [])
                                           if r.get("blocked_by") == "precondition"),
            "drift_checks": sum(1 for r in state.get("rounds", [])
                                if r.get("drift_checked")),
            "blocked_by_drift": sum(1 for r in state.get("rounds", [])
                                    if r.get("blocked_by") == "drift"),
            "blocked_recovered": sum(1 for r in state.get("rounds", [])
                                     if r.get("blocked_by") == "recovered"),
            "auto_undos": sum(1 for r in state.get("rounds", [])
                              if r.get("auto_undo")),
            "rounds": state.get("rounds", []),
        }
        if save:
            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            out = RUNS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}_{self.mode}_{run_id}.json"
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            report["saved_to"] = str(out)
        return report
