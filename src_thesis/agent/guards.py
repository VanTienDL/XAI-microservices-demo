"""Kiểm soát độ lệch trạng thái giữa lúc quyết định và lúc thi hành.

Đề cương cam kết bốn cơ chế. File này chứa phần KIỂM TRA của hai cơ chế đầu; cơ chế
thứ ba (tự hoàn tác) nằm ở node `watch` của `react_loop.py` vì nó cần chờ và đo.

1. **Kiểm tra điều kiện tiên quyết** — `check_preconditions`.
   Hai nguồn điều kiện:
     - LLM tự khai trong `ProposedAction.preconditions` ("currencyservice có 0 bản
       sao"). Đây là chỗ lời giải thích bị đem đối chiếu với cluster thật: LLM lập
       luận trên trạng thái sai thì bị bắt ở đây.
     - Code tự thêm luật bắt buộc theo loại hành động (`_code_rules`): không có pod
       thì không có gì để khởi động lại, không có biến để gỡ thì rollback vô nghĩa.
   Giá trị LLM viết sai dạng thì ghi "không kiểm được" và KHÔNG chặn: chặn vì lỗi
   chính tả là phạt LLM vì chuyện không liên quan tới trạng thái hệ thống.

2. **Đối sánh trạng thái trước thi hành** — `capture_config`, `config_drift`.
   Chụp cấu hình mọi deployment lúc LLM quan sát (hoặc lúc twin chép trạng thái từ
   production), chụp lại ngay trước khi áp, khác nhau thì không áp. Không đọc được
   trạng thái cũng tính là lệch: không biết thì không làm.

4. **Thi hành thăm dò phạm vi hẹp — KHÔNG CÀI**, vì ba lý do của môi trường:
     - mỗi service chạy đúng 1 bản sao, không có "một phần pod" để thử trước;
     - Online Boutique không có service mesh, không chia được lưu lượng theo tỉ lệ;
     - các service gọi nhau qua gRPC, kết nối HTTP/2 sống lâu nên Kubernetes chia
       lưu lượng theo KẾT NỐI chứ không theo request: một pod thăm dò có thể nhận
       0% hoặc 100% lưu lượng, phép đo trên nó không nói lên gì.
   Đây là giới hạn của môi trường thí nghiệm, ghi vào báo cáo, không phải bỏ quên.

Mọi hàm ở đây nhận `k8s` từ bên ngoài, nên thử được bằng một cluster giả
(`tests/test_guards.py`) mà không cần cluster thật.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import re

from src_thesis.agent.actions import cpu_to_millicores
from src_thesis.xai.schema import ProposedAction


@dataclass
class CheckResult:
    """Kết quả kiểm một điều kiện."""

    source: str          # "llm" hoac "code"
    kind: str
    target: str
    expected: str
    actual: str
    ok: bool | None      # None = khong kiem duoc

    def describe(self) -> str:
        mark = {True: "dung", False: "SAI", None: "khong kiem duoc"}[self.ok]
        return (f"[{self.source}] {self.kind} {self.target} ky vong {self.expected}, "
                f"that {self.actual} -> {mark}")

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------
# 1. DIEU KIEN TIEN QUYET
# ----------------------------------------------------------------------

def _code_rules(action: ProposedAction) -> list[tuple[str, str, str]]:
    """Luật bắt buộc theo loại hành động, dạng (kind, target, value)."""
    t = action.target
    if action.action == "restart_pod":
        return [("replicas_gte", t, "1")]
    if action.action == "scale_down":
        return [("replicas_gte", t, "2")]
    if action.action == "rollback":
        # v1 doi bien EXTRA_LATENCY phai co mat — tuc luat biet san cach tiem loi.
        # v2 chi doi dieu ma moi lenh quay lui that deu doi: co phien ban de ve.
        p = action.params_dict()
        return [("has_earlier_revision", t,
                 str(p.get("to_revision") or p.get("revision") or ""))]
    if action.action == "adjust_resources":
        p = action.params_dict()
        wanted = p.get("cpu") or p.get("cpu_limit")
        if wanted is not None:
            return [("cpu_param_valid", t, wanted)]
    if action.action in ("scale_up", "scale_down"):
        wanted = action.params_dict().get("replicas")
        if wanted is not None:
            return [("replicas_param_valid", t, wanted)]
    return []


def _check_one(k8s, namespace: str, source: str, kind: str, target: str,
               value: str) -> CheckResult:
    def res(actual, ok):
        return CheckResult(source, kind, target, str(value), str(actual), ok)

    # Hai luat chi kiem tham so, khong can doc cluster.
    if kind == "cpu_param_valid":
        m = cpu_to_millicores(value)
        ok = m is not None and m > 0
        return res(f"{m:g}m" if ok else "khong phai luong CPU duong", ok)
    if kind == "replicas_param_valid":
        try:
            int(str(value))
            return res("so nguyen", True)
        except ValueError:
            return res("khong phai so", False)

    try:
        if kind in ("replicas_eq", "replicas_gte"):
            try:
                want = int(str(value))
            except ValueError:
                return res("gia tri sai dang", None)
            actual = k8s.get_replicas(target, namespace=namespace)
            ok = actual == want if kind == "replicas_eq" else actual >= want
            return res(actual, ok)
        if kind == "cpu_limit_eq":
            want = cpu_to_millicores(value)
            if want is None:
                return res("gia tri sai dang", None)
            actual = k8s.get_cpu_limit(target, namespace=namespace)
            a = cpu_to_millicores(actual)
            return res(actual, a is not None and abs(a - want) < 1e-6)
        if kind == "has_earlier_revision":
            history = k8s.rollout_history(target, namespace=namespace)
            revs = [h["revision"] for h in history]
            if len(revs) < 2:
                return res(f"chi co {len(revs)} phien ban", False)
            if not str(value).strip():
                return res(f"phien ban {revs}", True)
            m = re.search(r"\d+", str(value))
            if not m:
                return res("to_revision sai dang", False)
            want = int(m.group(0))
            return res(f"phien ban {revs}, hien tai {revs[-1]}",
                       want in revs and want != revs[-1])
        if kind == "env_present":
            found = [k for k in str(value).split(",")
                     if k and k8s.get_env(target, k, namespace=namespace) is not None]
            return res(",".join(found) or "khong co bien nao", bool(found))
    except Exception as e:
        # Khong doc duoc deployment (vi du ten sai) la mot su that ve cluster:
        # hanh dong tren mot deployment khong ton tai thi khong co nghia.
        return res(f"loi doc: {str(e)[:80]}", False)
    return res("loai dieu kien la", None)


def check_preconditions(action: ProposedAction, k8s,
                        namespace: str = "default") -> tuple[bool, list[CheckResult]]:
    """Kiểm mọi điều kiện của một hành động. Trả về (được thi hành, chi tiết).

    Được thi hành khi KHÔNG có điều kiện nào SAI. "Không kiểm được" không chặn.
    """
    if action.action == "no_action":
        return True, []
    results = [_check_one(k8s, namespace, "code", k, t, v)
               for k, t, v in _code_rules(action)]
    results += [_check_one(k8s, namespace, "llm", p.kind, p.target, p.value)
                for p in action.preconditions]
    return all(r.ok is not False for r in results), results


def describe_failed(results: list[CheckResult]) -> str:
    """Câu phản hồi cho vòng sau: điều kiện nào sai, giá trị thật là gì."""
    return "; ".join(r.describe() for r in results if r.ok is False)


# ----------------------------------------------------------------------
# 2. DOI SANH TRANG THAI
# ----------------------------------------------------------------------

def capture_config(k8s, namespace: str = "default") -> dict | None:
    """Chụp cấu hình mọi deployment. None nếu không đọc được."""
    try:
        return k8s.deployment_configs(namespace)
    except Exception:
        return None


def config_drift(before: dict | None, after: dict | None) -> list[str]:
    """So hai lần chụp. Trả về danh sách thay đổi, rỗng nghĩa là không lệch.

    Một bên không đọc được thì tính là lệch — đối sánh mà không có số liệu để so
    thì không được coi là "khớp".
    """
    if before is None or after is None:
        return ["khong doc duoc trang thai de doi sanh"]
    changes: list[str] = []
    for dep in sorted(set(before) | set(after)):
        b, a = before.get(dep), after.get(dep)
        if b is None:
            changes.append(f"{dep}: deployment moi xuat hien")
            continue
        if a is None:
            changes.append(f"{dep}: deployment bien mat")
            continue
        for key in sorted(set(b) | set(a)):
            if key.startswith("cpu"):
                same = cpu_to_millicores(b.get(key)) == cpu_to_millicores(a.get(key))
            else:
                same = b.get(key) == a.get(key)
            if not same:
                changes.append(f"{dep}.{key}: {b.get(key)} -> {a.get(key)}")
    return changes
