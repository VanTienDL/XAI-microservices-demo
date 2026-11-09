"""Khuôn dạng JSON mà LLM bắt buộc phải xuất ra.

Theo mục 7.2 KLTN.md. Mọi output của LLM đều validate bằng Pydantic, sai schema thì
retry, không tin mù (mục 5 KLTN.md).

Ba chỗ khác so với mục 7.2, đều có lý do rút ra từ phase 2:

1. Thêm hành động `no_action`. Kịch bản S3 xóa pod và Kubernetes tự tạo lại, nên
   đáp án đúng là KHÔNG LÀM GÌ. Danh sách gốc ở mục 7.2 không có lựa chọn này, mà
   thiếu nó thì agent buộc phải chọn một hành động nào đó và ta không đo được chỉ
   số 5 "wasted action count" ở mục 8.

2. Thêm loại lỗi `pod_kill`. File đáp án của S3 ghi `fault_type: pod_kill`, nhưng
   danh sách nhãn ban đầu không có nhãn đó, nên model KHÔNG THỂ trả lời đúng dù
   suy luận hoàn hảo — chỉ số fault_type của S3 luôn bằng 0 vì lý do kỹ thuật chứ
   không phải vì chất lượng chẩn đoán. Bộ nhãn của schema phải phủ được bộ nhãn
   của đáp án, nếu không thì phép đo vô nghĩa.

3. `params` là DANH SÁCH cặp khóa-giá trị, không phải từ điển. Chế độ JSON nghiêm
   ngặt của OpenAI đòi mọi object phải khai báo đủ thuộc tính và cấm thuộc tính lạ,
   nên từ điển tự do không dùng được. Danh sách cặp thì mọi nhà cung cấp đều nhận.
   Giá trị số viết dưới dạng chuỗi: "1", "200m", "512Mi". Dùng `params_dict()` để
   lấy lại dạng từ điển khi cần.

4. Thêm `preconditions` cho mỗi hành động — đề cương cam kết JSON có "điều kiện
   tiên quyết" làm cơ sở cho cơ chế kiểm soát độ lệch trạng thái. Đây là DỮ LIỆU CÓ
   KIỂU, không phải văn xuôi: LLM khai trạng thái nó tin là đúng ("currencyservice
   đang có 0 bản sao"), và `src_thesis/agent/guards.py` đem đối chiếu với cluster
   ngay trước khi thi hành. Chỉ có những loại mà snapshot cho LLM nhìn thấy — không
   có loại nào về biến môi trường, vì snapshot không chứa biến môi trường.

5. (v2) Thêm loại lỗi `configuration_error`, để bộ nhãn phủ được đáp án của kịch
   bản H2 (cấu hình địa chỉ sai). Cùng bài học với nhãn `pod_kill` ở điểm 2.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

FaultType = Literal[
    "latency",               # service chậm hẳn đi nhưng vẫn trả lời
    "crash",                 # service ngừng phục vụ
    "pod_kill",              # pod bị xóa, Kubernetes đã tự tạo lại
    "resource_exhaustion",   # hết CPU hoặc RAM
    "dependency_failure",    # bản thân nó ổn, thứ nó gọi mới hỏng
    "configuration_error",   # v2: cấu hình sai (biến môi trường, địa chỉ, giới hạn)
    "unknown",
]

ActionName = Literal[
    "no_action",
    "scale_up",
    "scale_down",
    "adjust_resources",
    "reroute_traffic",
    "purge_queue",
    "restart_pod",
    "rollback",
]

RiskClass = Literal["easy", "medium", "hard"]

PreconditionKind = Literal[
    "replicas_eq",      # deployment co dung N ban sao
    "replicas_gte",     # deployment co it nhat N ban sao
    "cpu_limit_eq",     # tran CPU dung bang gia tri nay, vi du "10m"
]


class ActionParam(BaseModel):
    """Một tham số của hành động. Dạng cặp khóa-giá trị để hợp schema nghiêm ngặt."""

    key: str
    value: str


class Precondition(BaseModel):
    """Một điều kiện phải đúng trên cluster thì hành động mới có nghĩa."""

    kind: PreconditionKind
    target: str = Field(description="Ten deployment duoc kiem")
    value: str = Field(description="Gia tri ky vong, vi du '0' hoac '10m'")


class ProposedAction(BaseModel):
    """Một hành động sửa lỗi được đề xuất."""

    action: ActionName
    target: str = Field(description="Ten deployment bi tac dong, vi du cartservice")
    params: list[ActionParam] = Field(
        default_factory=list,
        description="Tham so cua hanh dong, vi du key=replicas value=1",
    )

    risk_class: RiskClass = Field(
        description="easy va medium: agent duoc tu lam. hard: phai thu tren twin truoc"
    )
    rationale: str = Field(description="Vi sao hanh dong nay se sua duoc van de")
    # default_factory de doc lai duoc file JSON cu, luc schema chua co truong nay.
    preconditions: list[Precondition] = Field(
        default_factory=list,
        description="Trang thai phai dung tren cluster thi hanh dong moi co nghia",
    )

    def params_dict(self) -> dict[str, str]:
        """Đổi về từ điển để phase 5 truyền thẳng vào hàm hành động."""
        return {p.key: p.value for p in self.params}


class Explanation(BaseModel):
    """Lời giải thích đầy đủ của XAI cho một sự cố."""

    root_cause_service: str = Field(
        description="Ten deployment gay ra su co. Neu he thong khoe manh thi ghi 'none'"
    )
    fault_type: FaultType
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning_chain: list[str] = Field(
        description="Tung buoc suy luan, moi buoc mot cau, hien thi cho nguoi doc"
    )
    propagation_path: list[str] = Field(
        description="Cac service chiu anh huong lan truyen, khong ke root cause"
    )
    evidence: list[str] = Field(
        description="Trich dan so lieu cu the da dung, vi du 'frontend -> X: 58.8% loi'"
    )
    proposed_actions: list[ProposedAction]

    def top_action(self) -> ProposedAction | None:
        """Hành động ưu tiên cao nhất. Phase 5 dùng để chọn việc phải làm."""
        return self.proposed_actions[0] if self.proposed_actions else None
