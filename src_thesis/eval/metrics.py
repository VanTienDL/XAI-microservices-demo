"""Chỉ số đánh giá, theo mục 8 KLTN.md.

Phase 3 chỉ dùng chỉ số 1 và 2 — chất lượng của XAI. Bốn chỉ số còn lại cần agent
nên để sang phase 5 và 6.

Nguyên tắc: mọi hàm ở đây nhận `Explanation` và `GroundTruth`, không đụng tới cluster.
Nhờ vậy chấm điểm lại được bất cứ lúc nào từ file JSON đã lưu, không phải chạy lại
thí nghiệm.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from statistics import mean, stdev


@dataclass
class CaseScore:
    """Điểm của một ca."""

    scenario: str
    root_cause_correct: bool
    propagation_jaccard: float
    action_correct: bool
    confidence: float
    fault_type_correct: bool
    predicted_root: str
    expected_root: str

    def to_dict(self) -> dict:
        return asdict(self)


def root_cause_correct(predicted: str, expected: str) -> bool:
    """Chỉ số 1 — có chỉ đúng service gây lỗi không.

    So khớp chính xác sau khi chuẩn hóa chữ hoa thường và khoảng trắng. Cố ý KHÔNG
    khớp gần đúng: "frontend" và "productcatalogservice" là hai đáp án khác nhau,
    chấm nới tay ở đây là tự thổi phồng kết quả của chính mình.
    """
    return predicted.strip().lower() == expected.strip().lower()


def propagation_accuracy(predicted: list[str], expected: list[str]) -> float:
    """Chỉ số 2 — độ trùng của đường lan truyền, tính bằng Jaccard.

    Jaccard = số phần tử chung chia cho số phần tử của hợp hai tập. Chọn Jaccard vì
    nó phạt cả hai kiểu sai: bỏ sót service bị ảnh hưởng, và kể thừa service không
    liên quan. Dùng tỉ lệ bao phủ đơn thuần thì model cứ liệt kê hết 11 service là
    đạt điểm tuyệt đối.

    Hai tập đều rỗng trả về 1.0: không có gì lan truyền và model cũng nói vậy.
    """
    p = {s.strip().lower() for s in predicted if s.strip()}
    e = {s.strip().lower() for s in expected if s.strip()}
    if not p and not e:
        return 1.0
    if not p or not e:
        return 0.0
    return round(len(p & e) / len(p | e), 4)


def action_correct(predicted_actions: list[str], correct_actions: list[str]) -> bool:
    """Hành động ưu tiên cao nhất có nằm trong danh sách đáp án đúng không.

    Chỉ xét hành động ĐẦU TIÊN, vì phase 5 agent cũng chỉ thực hiện cái đầu tiên.
    Xét cả danh sách thì model cứ đề xuất đủ bảy hành động là chắc chắn trúng.
    """
    if not predicted_actions:
        return "no_action" in correct_actions
    return predicted_actions[0].strip().lower() in {
        a.strip().lower() for a in correct_actions
    }


def score_case(explanation, ground_truth: dict, scenario: str = "") -> CaseScore:
    """Chấm một ca. `ground_truth` là dict đọc từ file trong data/runs/."""
    expected_root = ground_truth.get("target_service", "")
    actions = [a.action for a in explanation.proposed_actions]
    return CaseScore(
        scenario=scenario or ground_truth.get("fault_id", ""),
        root_cause_correct=root_cause_correct(explanation.root_cause_service,
                                              expected_root),
        propagation_jaccard=propagation_accuracy(
            explanation.propagation_path,
            ground_truth.get("expected_propagation", []),
        ),
        action_correct=action_correct(actions,
                                      ground_truth.get("correct_actions", [])),
        confidence=explanation.confidence,
        fault_type_correct=(
            explanation.fault_type == ground_truth.get("fault_type", "")
        ),
        predicted_root=explanation.root_cause_service,
        expected_root=expected_root,
    )


@dataclass
class Aggregate:
    """Tổng hợp nhiều lần chạy. Có độ lệch chuẩn vì LLM không ổn định."""

    n: int
    root_cause_accuracy: float
    root_cause_std: float
    propagation_mean: float
    propagation_std: float
    action_accuracy: float
    fault_type_accuracy: float
    mean_confidence: float

    def to_dict(self) -> dict:
        return asdict(self)


def aggregate(scores: list[CaseScore]) -> Aggregate:
    """Gộp điểm nhiều lần chạy.

    Mục 8 KLTN.md bắt buộc mỗi kịch bản chạy tối thiểu 5 lần để có độ lệch chuẩn —
    LLM không ổn định, một lần chạy không có giá trị khoa học.
    """
    if not scores:
        return Aggregate(0, 0, 0, 0, 0, 0, 0, 0)

    rc = [1.0 if s.root_cause_correct else 0.0 for s in scores]
    pp = [s.propagation_jaccard for s in scores]

    def sd(xs: list[float]) -> float:
        return round(stdev(xs), 4) if len(xs) > 1 else 0.0

    return Aggregate(
        n=len(scores),
        root_cause_accuracy=round(mean(rc), 4),
        root_cause_std=sd(rc),
        propagation_mean=round(mean(pp), 4),
        propagation_std=sd(pp),
        action_accuracy=round(
            mean([1.0 if s.action_correct else 0.0 for s in scores]), 4),
        fault_type_accuracy=round(
            mean([1.0 if s.fault_type_correct else 0.0 for s in scores]), 4),
        mean_confidence=round(mean([s.confidence for s in scores]), 4),
    )


# ======================================================================
# PHASE 6 — CHỈ SỐ 3 TỚI 7 MỤC 8 KLTN.md
#
# Bốn chỉ số dưới đây cần agent đã chạy xong, nên chúng nhận vào `report` — file
# JSON mà `ReactAgent.run()` ghi ra — chứ không nhận `Explanation`. Nguyên tắc của
# đầu file vẫn giữ nguyên: KHÔNG đụng tới cluster, chấm điểm lại lúc nào cũng được.
# ======================================================================

# --- Ngưỡng của chỉ số 4 (harmful) và 5 (wasted) ---
#
# Định nghĩa bằng số và CHỐT TRƯỚC KHI CHẠY. Nhìn số liệu rồi mới chọn ngưỡng là
# tự lừa mình: với 75 ca, luôn tìm được một ngưỡng làm giả thuyết trông đúng.
#
# 2 điểm phần trăm cho error rate lấy đúng `MIN_ERROR_DELTA` của
# `src_thesis/agent/verifier.py`. Hai chỗ phải cùng ngưỡng, vì nếu twin phán "worse"
# theo một thước mà chương kết quả đếm "harmful" theo thước khác, thì câu "twin chặn
# được hành động có hại" không kiểm chứng được.
HARMFUL_ERROR_DELTA = 0.02
HARMFUL_P95_RATIO = 0.20

# Dưới mức lưu lượng này thì KHÔNG KẾT LUẬN GÌ. `checkoutservice` chạy 0.08 req/s,
# khoảng 24 request mỗi cửa sổ, p95 nhảy loạn — ở phase 4 chính chỗ này đã lật
# phán quyết và cho ra con số fidelity 50% sai.
#
# "Không đủ cơ sở để kết luận" KHÁC "không có thay đổi". Gộp hai cái làm một là lớp
# lỗi đã tái xuất năm lần trong đề tài này.
MIN_RATE_FOR_EFFECT = 0.3

# Service dùng để đánh giá tác động. Trùng `CRITICAL_SERVICES` của verifier.
EFFECT_SERVICES = (
    "frontend",
    "productcatalogservice",
    "cartservice",
    "checkoutservice",
    "paymentservice",
)

# Giá theo 1 triệu token, đô la Mỹ. Chỉ số 6 mục 8 đòi chi phí mỗi ca.
# Cập nhật khi đổi model — và GHI LẠI model nào ứng với con số nào, vì tên model
# hết hạn (Groq đã gỡ toàn bộ dòng Llama giữa chừng đề tài này).
PRICE_PER_MTOK = {
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-oss-120b": (0.0, 0.0),   # goi mien phi cua Groq
}


@dataclass
class ActionEffect:
    """Tác động đo được của MỘT hành động lên production."""

    action: str
    target: str
    verdict: str          # harmful | helpful | neutral | unknown | not_applied
    reason: str
    degraded: list = None
    improved: list = None
    measured: list = None

    def __post_init__(self) -> None:
        self.degraded = self.degraded or []
        self.improved = self.improved or []
        self.measured = self.measured or []

    def to_dict(self) -> dict:
        return asdict(self)


def classify_action_effect(
    action: str,
    target: str,
    before: dict,
    after: dict,
    applied: bool = True,
    services: tuple = EFFECT_SERVICES,
) -> ActionEffect:
    """Chỉ số 4 và 5 — hành động này làm hệ thống xấu đi, tốt lên, hay không đổi.

    `before` và `after` là bảng RED của production: tên service ánh xạ tới dict có
    `error_rate`, `p95_ms`, `request_rate`.

    Năm kết luận có thể, và chúng KHÁC NHAU về ý nghĩa:

        not_applied  hành động không thi hành được (bị chặn, hoặc thất bại)
        harmful      có ít nhất một service xấu đi quá ngưỡng — chỉ số 4
        helpful      có service tốt lên và không service nào xấu đi
        neutral      mọi thay đổi đều dưới ngưỡng — chỉ số 5, hành động vô ích
        unknown      không service nào đủ lưu lượng để kết luận

    `unknown` KHÔNG được gộp vào `neutral`. Gộp là nói rằng "tôi không đo được"
    bằng câu "không có gì thay đổi", và đó là câu sai.

    Lỗi xếp trên độ trễ: một service trả lỗi thì người dùng mất đơn hàng, còn chậm
    thì vẫn mua được. Nên chỉ cần error rate vượt ngưỡng là harmful, bất kể p95.
    """
    if not applied:
        return ActionEffect(action=action, target=target, verdict="not_applied",
                            reason="hanh dong khong thi hanh duoc")

    degraded = []
    improved = []
    measured = []
    notes = []

    for svc in services:
        b, a = before.get(svc), after.get(svc)
        if not b or not a:
            continue
        if (b.get("request_rate", 0.0) < MIN_RATE_FOR_EFFECT
                or a.get("request_rate", 0.0) < MIN_RATE_FOR_EFFECT):
            continue
        measured.append(svc)

        de = a.get("error_rate", 0.0) - b.get("error_rate", 0.0)
        pb, pa = b.get("p95_ms", 0.0), a.get("p95_ms", 0.0)
        ratio = ((pa - pb) / pb) if pb > 0 else 0.0

        # Ti le loi xet TRUOC, ca hai chieu, roi moi toi do tre — cung thu tu voi
        # `TwinVerifier.compare`. Ban dau xet "p95 tang" truoc "loi giam", va do sai
        # that o S2: service chet thi request bao loi NGAY nen p95 thap gia tao; sua
        # xong thi p95 ve muc that. Ca hai ca S2 bi cham `harmful` ("frontend p95
        # tang 1169%") cho mot hanh dong chua dung benh, trong khi `watch` noi better.
        if de >= HARMFUL_ERROR_DELTA:
            degraded.append(svc)
            notes.append(f"{svc} loi tang {de * 100:.1f} diem")
        elif -de >= HARMFUL_ERROR_DELTA:
            improved.append(svc)
            notes.append(f"{svc} loi giam {-de * 100:.1f} diem")
        elif ratio >= HARMFUL_P95_RATIO:
            degraded.append(svc)
            notes.append(f"{svc} p95 tang {ratio * 100:.0f}%")
        elif ratio <= -HARMFUL_P95_RATIO:
            improved.append(svc)
            notes.append(f"{svc} p95 giam {-ratio * 100:.0f}%")

    if not measured:
        return ActionEffect(
            action=action, target=target, verdict="unknown",
            reason=(f"khong service nao dat {MIN_RATE_FOR_EFFECT} req/s o ca hai lan "
                    f"do — khong du co so ket luan, KHONG phai khong doi gi"))

    if degraded:
        return ActionEffect(action=action, target=target, verdict="harmful",
                            reason="; ".join(notes), degraded=degraded,
                            improved=improved, measured=measured)
    if improved:
        return ActionEffect(action=action, target=target, verdict="helpful",
                            reason="; ".join(notes), improved=improved,
                            measured=measured)
    return ActionEffect(
        action=action, target=target, verdict="neutral",
        reason=f"moi thay doi duoi nguong tren {len(measured)} service do duoc",
        measured=measured)


def mttr(injected_at: float, recovered_at: float | None,
         gave_up_at: float | None = None):
    """Chỉ số 3 — thời gian từ lúc tiêm lỗi tới lúc hệ thống hồi phục.

    Trả về `(mttr_giay, da_hoi_phuc, bi_cat_o_giay)`.

    Ca KHÔNG hồi phục trả về `(None, False, thời_điểm_bỏ_cuộc)` chứ không trả về
    một con số lớn. Nhét ca không hồi phục vào trung bình bằng "thời gian đã chờ"
    là kéo trung bình xuống — nó nói dối theo hướng làm chế độ tệ trông tốt hơn.
    Thống kê gọi kiểu dữ liệu này là **bị cắt cụt** (censored), và phải báo cáo
    riêng số ca bị cắt cụt bên cạnh trung bình của các ca hồi phục được.
    """
    if recovered_at is not None:
        return round(recovered_at - injected_at, 1), True, None
    if gave_up_at is not None:
        return None, False, round(gave_up_at - injected_at, 1)
    return None, False, None


def cost_usd(model: str, input_tokens: int, output_tokens: int):
    """Chỉ số 6 — tiền API của một ca. Trả về None nếu chưa biết giá của model."""
    price = PRICE_PER_MTOK.get(model)
    if price is None:
        return None
    pin, pout = price
    return round(input_tokens / 1e6 * pin + output_tokens / 1e6 * pout, 6)


@dataclass
class CaseOutcome:
    """Toàn bộ chỉ số của MỘT ca, đủ để dựng bảng kết quả mục 8."""

    scenario: str
    mode: str
    repeat: int
    run_id: str

    # chi so 1, 2 — None khi che do khong goi LLM (baseline)
    root_cause_correct: bool | None = None
    propagation_jaccard: float | None = None
    fault_type_correct: bool | None = None
    action_correct: bool | None = None
    predicted_root: str = ""
    expected_root: str = ""

    # chi so 3
    recovered: bool = False
    mttr_s: float | None = None
    censored_at_s: float | None = None

    # chi so 4, 5
    harmful_actions: int = 0
    wasted_actions: int = 0
    helpful_actions: int = 0
    unknown_effect_actions: int = 0
    action_effects: list = None

    # chi so 6
    rounds_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    took_s: float = 0.0
    actions_rejected_by_twin: int = 0

    # co che kiem soat do lech trang thai — chi khac 0 o che do bat `guards`
    blocked_by_precondition: int = 0
    drift_checks: int = 0
    blocked_by_drift: int = 0
    blocked_recovered: int = 0
    auto_undos: int = 0
    # chuyen giao twin -> production: so lan twin phan `better`, va trong do bao
    # nhieu lan production DO DUOC cung `better` sau khi ap
    twin_better: int = 0
    twin_better_transferred: int = 0

    notes: list = None

    def __post_init__(self) -> None:
        self.action_effects = self.action_effects or []
        self.notes = self.notes or []

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ModeSummary:
    """Tổng hợp một chế độ trên nhiều ca. Có độ lệch chuẩn và số ca bị cắt cụt."""

    mode: str
    n_cases: int
    root_cause_accuracy: float | None
    root_cause_std: float | None
    propagation_mean: float | None
    recovery_rate: float
    n_recovered: int
    n_censored: int
    mttr_mean_s: float | None
    mttr_std_s: float | None
    harmful_total: int
    harmful_per_case: float
    wasted_total: int
    wasted_per_case: float
    unknown_effect_total: int
    rejected_by_twin_total: int
    tokens_per_case: float
    cost_per_case: float | None
    took_mean_s: float
    # co che kiem soat do lech trang thai
    blocked_by_precondition_total: int = 0
    auto_undo_total: int = 0
    drift_checks_total: int = 0
    blocked_by_drift_total: int = 0
    blocked_recovered_total: int = 0
    # Ti le that bai do do lech trang thai (Bang 3 de cuong): so lan bi chan vi
    # lech CAU HINH hoac lech SUC KHOE, chia cho so lan doi sanh. None khi chua
    # doi sanh lan nao — khac voi 0.
    drift_failure_rate: float | None = None
    # Ti le chuyen giao twin -> production: trong cac lan twin phan `better`, bao
    # nhieu lan production do duoc cung `better`. Lan bi chan o buoc ap tinh la
    # chuyen giao that bai. None khi twin chua phan `better` lan nao.
    twin_better_total: int = 0
    twin_transfer_rate: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def summarize_mode(outcomes: list, mode: str = "") -> ModeSummary:
    """Gộp các ca của cùng một chế độ.

    MTTR chỉ lấy trung bình trên các ca ĐÃ hồi phục, và số ca không hồi phục báo
    riêng ở `n_censored`. Đọc `mttr_mean_s` mà bỏ qua `n_censored` là đọc sai: một
    chế độ chỉ sửa được 2 ca dễ trong 25 ca sẽ có MTTR trung bình rất đẹp.
    """
    n = len(outcomes)
    if n == 0:
        return ModeSummary(mode, 0, None, None, None, 0.0, 0, 0, None, None,
                           0, 0.0, 0, 0.0, 0, 0, 0.0, None, 0.0)

    def sd(xs: list) -> float:
        return round(stdev(xs), 4) if len(xs) > 1 else 0.0

    rc = [1.0 if o.root_cause_correct else 0.0
          for o in outcomes if o.root_cause_correct is not None]
    pp = [o.propagation_jaccard for o in outcomes
          if o.propagation_jaccard is not None]
    mt = [o.mttr_s for o in outcomes if o.mttr_s is not None]
    costs = [o.cost_usd for o in outcomes if o.cost_usd is not None]
    drift_checks = sum(o.drift_checks for o in outcomes)
    drift_fail = sum(o.blocked_by_drift + o.blocked_recovered for o in outcomes)
    twin_better = sum(o.twin_better for o in outcomes)
    transferred = sum(o.twin_better_transferred for o in outcomes)

    return ModeSummary(
        mode=mode or outcomes[0].mode,
        n_cases=n,
        root_cause_accuracy=round(mean(rc), 4) if rc else None,
        root_cause_std=sd(rc) if rc else None,
        propagation_mean=round(mean(pp), 4) if pp else None,
        recovery_rate=round(sum(1 for o in outcomes if o.recovered) / n, 4),
        n_recovered=sum(1 for o in outcomes if o.recovered),
        n_censored=sum(1 for o in outcomes if not o.recovered),
        mttr_mean_s=round(mean(mt), 1) if mt else None,
        mttr_std_s=round(sd(mt), 1) if mt else None,
        harmful_total=sum(o.harmful_actions for o in outcomes),
        harmful_per_case=round(sum(o.harmful_actions for o in outcomes) / n, 4),
        wasted_total=sum(o.wasted_actions for o in outcomes),
        wasted_per_case=round(sum(o.wasted_actions for o in outcomes) / n, 4),
        unknown_effect_total=sum(o.unknown_effect_actions for o in outcomes),
        rejected_by_twin_total=sum(o.actions_rejected_by_twin for o in outcomes),
        tokens_per_case=round(
            sum(o.input_tokens + o.output_tokens for o in outcomes) / n, 1),
        cost_per_case=round(sum(costs) / n, 6) if costs else None,
        took_mean_s=round(mean([o.took_s for o in outcomes]), 1),
        blocked_by_precondition_total=sum(o.blocked_by_precondition for o in outcomes),
        auto_undo_total=sum(o.auto_undos for o in outcomes),
        drift_checks_total=drift_checks,
        blocked_by_drift_total=sum(o.blocked_by_drift for o in outcomes),
        blocked_recovered_total=sum(o.blocked_recovered for o in outcomes),
        drift_failure_rate=(round(drift_fail / drift_checks, 4)
                            if drift_checks else None),
        twin_better_total=twin_better,
        twin_transfer_rate=(round(transferred / twin_better, 4)
                            if twin_better else None),
    )


def twin_fidelity(fidelity_file):
    """Chỉ số 7 — đọc lại kết quả đo fidelity của phase 4.

    Trả về `(tỉ_lệ_khớp, số_khớp, tổng_số)`. Chỉ số này đo riêng ở phase 4 chứ
    không sinh ra từ 75 ca, vì nó cần chạy CÙNG một hành động ở cả hai môi trường —
    thứ mà agent chạy bình thường không làm.
    """
    import json
    from pathlib import Path

    path = Path(fidelity_file)
    if not path.exists():
        return None, 0, 0
    data = json.loads(path.read_text(encoding="utf-8"))

    # Doc dung con so ma `scripts/twin_fidelity.py` DA GHI, khong tu tinh lai.
    #
    # `trials` co HAI dong moi phep thu — mot cua twin, mot cua production — nen
    # dem `trials` ra 12 trong khi so phep thu that la 6. Tu tinh lai o day mot
    # lan da cho ra "0/12" trong khi ket qua that la "6/6", va no khong bao loi,
    # chi in ra mot con so sai. Nguon duy nhat cua chi so 7 la file goc.
    if "matches" in data and "total" in data:
        match, total = int(data["matches"]), int(data["total"])
        if total <= 0:
            return None, 0, 0
        rate = data.get("fidelity")
        return (round(float(rate), 4) if rate is not None
                else round(match / total, 4)), match, total

    return None, 0, 0
