"""Bảy hành động sửa lỗi mà agent được phép thi hành, theo mục 7.2 và 7.3 KLTN.md.

Mỗi hành động có ba thứ:
  - hàm thi hành, trả về `ActionResult` nói rõ đã đổi gì
  - hàm hoàn tác, dựng lại từ chính `ActionResult` đó
  - `risk_class` quyết định agent được tự làm hay phải thử trên twin trước

PHÂN MỨC RỦI RO (mục 7.3 KLTN.md, Bảng 2 đề cương):
  easy    scale_up, scale_down, adjust_resources   agent tự làm
  medium  reroute_traffic, purge_queue             agent tự làm — nhưng Online
                                                   Boutique không có chỗ để làm,
                                                   nên luôn bị `can_apply` chặn
  hard    restart_pod, rollback                    PHẢI qua twin xác nhận

Ở chế độ `twin_verified`, mọi mức đều được đo lại sau khi áp và tự hoàn tác nếu
production tệ đi (`react_loop._watch`). Chỉ `restart_pod` là không hoàn tác được.

CHỖ NGUY HIỂM NHẤT CỦA CẢ FILE NÀY LÀ HÀM HOÀN TÁC.

Phase 2 có ba lỗi hoàn tác, cả ba cùng một tính chất: **hệ thống vẫn hỏng trong khi
công cụ báo thành công**. Cụ thể:

  - `unset_env` dùng merge patch làm mất luôn trường `image`, API trả 422 nhưng
    script vẫn đi tiếp và lỗi vẫn nằm nguyên đó.
  - `set_cpu_limit` không sửa `requests` cùng lúc nên API từ chối vì requests lớn
    hơn limit.
  - `active_fault.json` ghi đè một object đơn nên kịch bản kép chỉ hoàn tác được một
    nửa.

Với agent thì lớp lỗi này nặng hơn hẳn, vì agent **tiếp tục ra quyết định** dựa trên
niềm tin rằng nó đã sửa xong. Vì vậy mọi hàm ở đây:

  1. Đọc lại trạng thái sau khi đổi và so với thứ mình vừa yêu cầu.
  2. Trả về `verified=False` kèm lý do nếu không khớp, thay vì im lặng thành công.

`no_action` là hành động hạng nhất, không phải trường hợp đặc biệt. Kịch bản S3 có
đáp án đúng là không làm gì, và mục 8 KLTN.md đo "wasted action count" — thiếu
`no_action` thì agent buộc phải làm gì đó và chỉ số này luôn bằng 0.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from src_thesis.k8s_client import K8sClient
from src_thesis.xai.schema import ProposedAction

RISK_OF_ACTION: dict[str, str] = {
    "no_action": "easy",
    "scale_up": "easy",
    "scale_down": "easy",
    "adjust_resources": "easy",
    "reroute_traffic": "medium",
    "purge_queue": "medium",
    "restart_pod": "hard",
    "rollback": "hard",
}

# Hành động phải được twin xác nhận trước khi áp lên production (mục 7.3, 7.4).
NEEDS_TWIN = {"restart_pod", "rollback"}

# Biến môi trường mà `rollback` gỡ bỏ. Đây là các biến do người vận hành hoặc thí
# nghiệm đặt vào, không phải cấu hình gốc của Online Boutique — gỡ chúng đưa service
# về đúng trạng thái xuất xưởng.
ROLLBACK_ENV_KEYS = ("EXTRA_LATENCY",)

MAX_REPLICAS = 4          # tran an toan, tranh agent scale vo han
MIN_REPLICAS = 1
DEFAULT_CPU_LIMIT = "200m"


def cpu_to_millicores(value: str | None) -> float | None:
    """Doi mot luong CPU cua Kubernetes ve millicore de SO SANH DUOC.

    VI SAO CAN: Kubernetes CHUAN HOA lai luong CPU khi luu. Yeu cau "0.4" thi doc
    lai duoc "400m" — cung mot gia tri, khac cach viet. So chuoi thang thi ket luan
    sai rang hanh dong that bai, trong khi no da thanh cong.

    Do thay o ca kiem thu S1 che do direct: tran CPU doi tu 200m len 400m dung y
    muon, nhung `verified` ra False vi "400m" != "0.4". Day la loi NGUOC voi lop loi
    thuong gap trong project nay — he thong bao that bai trong khi da thanh cong —
    nhung cung mot goc re: so sanh ma khong tinh den cach bieu dien.
    """
    if value is None:
        return None
    v = str(value).strip()
    if not v:
        return None
    try:
        if v.endswith("m"):
            return float(v[:-1])
        return float(v) * 1000.0
    except ValueError:
        return None


@dataclass
class ActionResult:
    """Kết quả một lần thi hành hành động, đủ để hoàn tác lại."""

    action: str
    target: str
    namespace: str
    applied: bool                       # co thuc su doi gi khong
    verified: bool                      # doc lai co dung nhu yeu cau khong
    detail: str                         # mo ta cho nguoi doc
    undo_kind: str = "none"             # none | scale | cpu | env | restart
    undo_args: dict = field(default_factory=dict)
    error: str = ""
    took_s: float = 0.0

    @property
    def ok(self) -> bool:
        """Thi hành xong VÀ đã kiểm chứng lại bằng số."""
        return self.applied and self.verified and not self.error

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ok"] = self.ok
        return d


class ActionExecutor:
    """Thi hành và hoàn tác hành động trên một namespace bất kỳ.

    Cùng một lớp dùng cho cả twin và production — bắt buộc phải vậy. Viết đường tắt
    riêng cho twin thì thứ agent thử trên twin không còn là thứ nó làm trên
    production, và kết quả twin xác nhận sẽ không nói được gì về production.
    """

    def __init__(self, k8s: K8sClient | None = None, namespace: str = "default"):
        self.k8s = k8s or K8sClient(namespace=namespace)
        self.namespace = namespace

    # ------------------------------------------------------------------

    def can_apply(self, action: str, target: str) -> tuple[bool, str]:
        """Kiểm tra trước khi thi hành. Trả về (được phép, lý do nếu không).

        Gọi hàm này TRƯỚC khi đụng vào cluster. LLM có thể đề xuất hành động hợp
        schema nhưng không thi hành được — ví dụ `purge_queue` cho một hệ thống
        không có hàng đợi. Chặn ở đây thì đếm được vào "wasted action count" mục 8,
        còn để nó ném lỗi giữa chừng thì vòng lặp chết mà không ghi được gì.
        """
        if action not in RISK_OF_ACTION:
            return False, f"khong biet hanh dong '{action}'"
        if action == "no_action":
            return True, ""
        if action in ("reroute_traffic", "purge_queue"):
            # Online Boutique khong co service mesh va khong co hang doi. Hai hanh
            # dong nay nam trong schema vi muc 7.2 liet ke, nhung he thong nay khong
            # thi hanh duoc. Bao that thay vi gia vo lam.
            return False, (f"'{action}' khong ap dung duoc cho Online Boutique: "
                           f"khong co service mesh va khong co hang doi")
        if not target or target == "none":
            return False, f"'{action}' can ten deployment cu the, nhan duoc '{target}'"
        try:
            deployments = set(self.k8s.list_deployments(self.namespace))
        except Exception as e:
            return False, f"khong doc duoc danh sach deployment: {e}"
        if target not in deployments:
            return False, (f"khong co deployment '{target}' trong namespace "
                           f"'{self.namespace}'")
        return True, ""

    # ------------------------------------------------------------------

    def apply(self, proposed: ProposedAction) -> ActionResult:
        """Thi hành một hành động do XAI đề xuất."""
        started = time.time()
        action = proposed.action
        target = proposed.target
        params = proposed.params_dict()

        allowed, why = self.can_apply(action, target)
        if not allowed:
            return ActionResult(action=action, target=target,
                                namespace=self.namespace, applied=False,
                                verified=False, detail=why, error=why,
                                took_s=round(time.time() - started, 2))

        try:
            result = self._dispatch(action, target, params)
        except Exception as e:
            result = ActionResult(
                action=action, target=target, namespace=self.namespace,
                applied=False, verified=False,
                detail=f"that bai khi thi hanh: {e}", error=str(e)[:300],
            )
        result.took_s = round(time.time() - started, 2)
        return result

    def _dispatch(self, action: str, target: str, params: dict) -> ActionResult:
        if action == "no_action":
            return ActionResult(
                action=action, target=target, namespace=self.namespace,
                applied=True, verified=True,
                detail="khong lam gi, dung y do",
            )
        if action == "scale_up":
            return self._scale(target, params, direction=+1)
        if action == "scale_down":
            return self._scale(target, params, direction=-1)
        if action == "adjust_resources":
            return self._adjust_resources(target, params)
        if action == "restart_pod":
            return self._restart(target)
        if action == "rollback":
            return self._rollback(target)
        raise ValueError(f"khong biet hanh dong '{action}'")

    # ------------------------------------------------------------------
    # easy
    # ------------------------------------------------------------------

    def _scale(self, target: str, params: dict, direction: int) -> ActionResult:
        """Tăng hoặc giảm số bản sao.

        LLM có thể ghi thẳng số mong muốn qua tham số `replicas`. Không ghi thì
        cộng trừ một từ số hiện tại.
        """
        before = self.k8s.get_replicas(target, namespace=self.namespace)
        if before is None:
            raise RuntimeError(f"khong doc duoc so ban sao cua {target}")

        wanted = params.get("replicas")
        if wanted is not None:
            try:
                after = int(wanted)
            except (TypeError, ValueError):
                raise ValueError(f"tham so replicas khong phai so: {wanted!r}")
        else:
            after = before + direction

        after = max(MIN_REPLICAS, min(MAX_REPLICAS, after))
        name = "scale_up" if direction > 0 else "scale_down"

        if after == before:
            return ActionResult(
                action=name, target=target, namespace=self.namespace,
                applied=False, verified=True,
                detail=f"so ban sao da la {before}, khong doi gi",
            )

        self.k8s.scale_deployment(target, after, namespace=self.namespace)
        self.k8s.wait_replicas(target, after, timeout=180, namespace=self.namespace)

        # KIEM CHUNG: doc lai, khong tin vao viec lenh tra ve khong loi.
        actual = self.k8s.get_replicas(target, namespace=self.namespace)
        verified = actual == after
        return ActionResult(
            action=name, target=target, namespace=self.namespace,
            applied=True, verified=verified,
            detail=f"so ban sao {before} -> {actual} (yeu cau {after})",
            undo_kind="scale", undo_args={"replicas": before},
            error="" if verified else f"yeu cau {after} nhung doc lai duoc {actual}",
        )

    def _adjust_resources(self, target: str, params: dict) -> ActionResult:
        """Đổi trần CPU.

        `set_cpu_limit` phải sửa cả `limits` lẫn `requests` trong MỘT lần gọi —
        bài học phase 2: sửa riêng `limits` thì API từ chối với lỗi
        `requests "100m" must be <= cpu limit of 10m`.
        """
        before_limit = self.k8s.get_cpu_limit(target, namespace=self.namespace)
        before_request = self.k8s.get_cpu_request(target, namespace=self.namespace)
        wanted = params.get("cpu") or params.get("cpu_limit") or DEFAULT_CPU_LIMIT

        want_m, before_m = cpu_to_millicores(wanted), cpu_to_millicores(before_limit)
        if want_m is not None and before_m is not None and abs(want_m - before_m) < 1e-6:
            return ActionResult(
                action="adjust_resources", target=target, namespace=self.namespace,
                applied=False, verified=True,
                detail=f"tran CPU da la {before_limit}, khong doi gi",
            )

        self.k8s.set_cpu_limit(target, wanted, namespace=self.namespace)
        self.k8s.wait_ready(target, timeout=180, namespace=self.namespace)

        actual = self.k8s.get_cpu_limit(target, namespace=self.namespace)
        # So theo SO millicore, khong so chuoi: Kubernetes chuan hoa "0.4" thanh "400m".
        want_m, actual_m = cpu_to_millicores(wanted), cpu_to_millicores(actual)
        verified = (want_m is not None and actual_m is not None
                    and abs(want_m - actual_m) < 1e-6)
        return ActionResult(
            action="adjust_resources", target=target, namespace=self.namespace,
            applied=True, verified=verified,
            detail=f"tran CPU {before_limit} -> {actual} (yeu cau {wanted})",
            undo_kind="cpu",
            undo_args={"limit": before_limit, "request": before_request},
            error="" if verified else f"yeu cau {wanted} nhung doc lai duoc {actual}",
        )

    # ------------------------------------------------------------------
    # hard — phai qua twin xac nhan
    # ------------------------------------------------------------------

    def _restart(self, target: str) -> ActionResult:
        """Khởi động lại toàn bộ pod của deployment.

        Không hoàn tác được theo nghĩa đen: pod cũ đã chết hẳn. `undo_kind` để
        `none` cho đúng sự thật, thay vì giả vờ có đường lùi.
        """
        self.k8s.restart_deployment(target, namespace=self.namespace)
        self.k8s.wait_ready(target, timeout=240, namespace=self.namespace)

        pods = [p for p in self.k8s.list_pods(self.namespace)
                if p.deployment == target]
        ready = [p for p in pods if p.ready]
        verified = len(ready) > 0
        return ActionResult(
            action="restart_pod", target=target, namespace=self.namespace,
            applied=True, verified=verified,
            detail=f"da khoi dong lai, {len(ready)}/{len(pods)} pod san sang",
            undo_kind="none",
            error="" if verified else "khong pod nao san sang sau khi khoi dong lai",
        )

    def _rollback(self, target: str) -> ActionResult:
        """Gỡ các biến môi trường do người vận hành đặt vào, đưa service về gốc.

        Dùng JSON Patch chứ không dùng merge patch — bài học phase 2: merge patch
        thay cả mảng `containers` nên làm mất trường `image` và API trả 422.
        `unset_env` trong `k8s_client` đã cài đúng cách này.
        """
        removed, kept = [], []
        # Nho gia tri cu cua tung bien da go, de tu hoan tac duoc khi rollback lam
        # production te di. Ban dau rollback de undo_kind "none", tuc la mot hanh
        # dong `hard` khong co duong lui — dung loai can duong lui nhat.
        old_values: list[dict] = []
        for key in ROLLBACK_ENV_KEYS:
            old = self.k8s.get_env(target, key, namespace=self.namespace)
            if old is None:
                continue
            self.k8s.unset_env(target, key, namespace=self.namespace)
            removed.append(key)
            old_values.append({"key": key, "value": old})

        if not removed:
            return ActionResult(
                action="rollback", target=target, namespace=self.namespace,
                applied=False, verified=True,
                detail="khong co bien moi truong nao de go, service da o trang thai goc",
            )

        self.k8s.wait_ready(target, timeout=240, namespace=self.namespace)

        # KIEM CHUNG: doc lai tung bien, phai bien mat that.
        for key in removed:
            if self.k8s.get_env(target, key, namespace=self.namespace) is not None:
                kept.append(key)
        verified = not kept
        return ActionResult(
            action="rollback", target=target, namespace=self.namespace,
            applied=True, verified=verified,
            detail=f"da go bien: {', '.join(removed)}",
            undo_kind="env",
            undo_args={"items": old_values},
            error="" if verified else f"van con sau khi go: {', '.join(kept)}",
        )

    # ------------------------------------------------------------------
    # HOAN TAC
    # ------------------------------------------------------------------

    def undo(self, result: ActionResult) -> ActionResult:
        """Hoàn tác một hành động, dựng lại từ chính `ActionResult` của nó.

        Dùng khi thử trên twin xong phải dọn sạch, và khi hành động trên production
        làm mọi thứ tệ đi.

        Trả về một `ActionResult` mới mô tả việc hoàn tác — cũng có `verified`, vì
        hoàn tác hỏng mà báo thành công chính là lớp lỗi nguy hiểm nhất của phase 2.
        """
        kind = result.undo_kind
        target, args = result.target, result.undo_args

        if kind == "none":
            return ActionResult(
                action=f"undo_{result.action}", target=target,
                namespace=self.namespace, applied=False, verified=True,
                detail="hanh dong nay khong hoan tac duoc, khong co gi de lam",
            )
        try:
            if kind == "scale":
                want = int(args["replicas"])
                self.k8s.scale_deployment(target, want, namespace=self.namespace)
                self.k8s.wait_replicas(target, want, timeout=180,
                                       namespace=self.namespace)
                actual = self.k8s.get_replicas(target, namespace=self.namespace)
                verified = actual == want
                detail = f"so ban sao ve lai {actual} (yeu cau {want})"
            elif kind == "cpu":
                self.k8s.restore_cpu(target, args.get("limit"), args.get("request"),
                                     namespace=self.namespace)
                self.k8s.wait_ready(target, timeout=180, namespace=self.namespace)
                actual = self.k8s.get_cpu_limit(target, namespace=self.namespace)
                a_m = cpu_to_millicores(actual)
                w_m = cpu_to_millicores(args.get("limit"))
                verified = (a_m is not None and w_m is not None
                            and abs(a_m - w_m) < 1e-6)
                detail = f"tran CPU ve lai {actual} (yeu cau {args.get('limit')})"
            elif kind == "env":
                # Hai dang: mot bien {"key", "value"}, hoac nhieu bien {"items": [...]}
                # do `rollback` ghi lai.
                items = args.get("items") or [{"key": args["key"],
                                               "value": args.get("value")}]
                for it in items:
                    if it.get("value") is None:
                        self.k8s.unset_env(target, it["key"], namespace=self.namespace)
                    else:
                        self.k8s.set_env(target, it["key"], it["value"],
                                         namespace=self.namespace)
                self.k8s.wait_ready(target, timeout=180, namespace=self.namespace)
                parts, verified = [], True
                for it in items:
                    actual = self.k8s.get_env(target, it["key"], namespace=self.namespace)
                    verified = verified and actual == it.get("value")
                    parts.append(f"{it['key']}={actual!r} (yeu cau {it.get('value')!r})")
                detail = "bien ve lai " + ", ".join(parts)
            else:
                raise ValueError(f"khong biet cach hoan tac '{kind}'")
        except Exception as e:
            return ActionResult(
                action=f"undo_{result.action}", target=target,
                namespace=self.namespace, applied=False, verified=False,
                detail=f"hoan tac that bai: {e}", error=str(e)[:300],
            )

        return ActionResult(
            action=f"undo_{result.action}", target=target,
            namespace=self.namespace, applied=True, verified=verified,
            detail=detail,
            error="" if verified else "hoan tac xong nhung doc lai khong khop",
        )


# Hanh dong bi ep len muc `hard` cho mot phien thi nghiem rieng, KHONG doi bang
# RISK_OF_ACTION goc. Dung cho thi nghiem phu ve cong vao twin: phien chinh cho thay
# 17/19 hanh dong gay hai la `adjust_resources` muc thap, di vong qua twin. Ep no
# qua twin ma giu nguyen prompt thi tach duoc dung tac dung cua cong vao twin.
_EXTRA_HARD: set[str] = set()


def set_extra_hard_actions(names) -> None:
    """Đặt danh sách hành động bị ép lên mức `hard` (bắt buộc qua twin)."""
    names = {n.strip() for n in (names or []) if n and n.strip()}
    unknown = names - set(RISK_OF_ACTION)
    if unknown:
        raise ValueError(f"khong biet hanh dong: {', '.join(sorted(unknown))}")
    _EXTRA_HARD.clear()
    _EXTRA_HARD.update(names)


def extra_hard_actions() -> list[str]:
    return sorted(_EXTRA_HARD)


def risk_of(action: str) -> str:
    """Mức rủi ro của một hành động. Không biết thì coi là `hard` cho an toàn."""
    if action in _EXTRA_HARD:
        return "hard"
    return RISK_OF_ACTION.get(action, "hard")


def needs_twin(action: str) -> bool:
    """Hành động này có bắt buộc phải qua twin xác nhận không (mục 7.3 KLTN.md)."""
    return action in NEEDS_TWIN or risk_of(action) == "hard"
