"""Kiểm tra trước khi chạy một ca thí nghiệm, và chờ hệ thống về trạng thái sạch.

VÌ SAO CẦN: phase 6 chạy 75 ca liên tiếp. Nếu một ca bắt đầu khi hệ thống còn dư âm
của ca trước, thì mọi số đo của ca đó lẫn hai lần tiêm — và không có gì báo cho ta
biết điều đó. Số liệu vẫn ra, vẫn đẹp, chỉ là sai.

Hai loại kiểm tra ở đây khác nhau về bản chất:

    `ensure_clean_slate`     — kiểm tra CẤU HÌNH: còn lỗi chưa hoàn tác không, twin
                               còn sống không, mọi deployment có sẵn sàng không
    `wait_for_clean_baseline` — kiểm tra HÀNH VI: số liệu quan sát được có sạch không

Phải qua cả hai. Cấu hình sạch mà hành vi chưa sạch nghĩa là hệ thống đang hồi phục
nhưng cửa sổ quan sát 5 phút vẫn còn giữ dữ liệu lúc hỏng.
"""

from __future__ import annotations

import time
from pathlib import Path

from src_thesis.faults.injectors import load_active_faults
from src_thesis.graph.baseline import find_baseline_file, graph_from_dict
from src_thesis.graph.model import ServiceGraph
from src_thesis.k8s_client import K8sClient
from src_thesis.telemetry.snapshot import SystemSnapshot, take_snapshot

# Nhãn dùng khi lưu ảnh nền mới. Phải nằm trong `BASELINE_LABELS` của
# `src_thesis/graph/baseline.py`, nếu không thì agent sẽ không tìm ra nó.
BASELINE_LABEL = "baseline-clean"

TWIN_NAMESPACE = "twin"


def _log(msg: str, log=None) -> None:
    (log or print)(msg)


# Nguong "vua tao lai" cua prompt (`serialize.describe_pods`, mac dinh 600 giay).
# Hai cho PHAI cung mot so: pod tre hon nguong nay se hien "RECREATED" trong prompt.
SETTLED_AGE_S = 600.0


def young_pods(k8s: K8sClient, min_age_s: float = SETTLED_AGE_S,
               namespace: str = "default") -> list[tuple[str, float]]:
    """Pod ứng dụng trẻ hơn `min_age_s`, dạng (tên, tuổi giây), trẻ nhất trước.

    Tuổi lấy SỐ NHỎ HƠN giữa tuổi pod và thời gian từ lần khởi động lại gần nhất —
    đúng cách prompt xét. Pod hạ tầng bỏ qua bằng đúng bộ lọc mà prompt dùng.
    """
    from src_thesis.graph.serialize import INFRA_PODS

    out: list[tuple[str, float]] = []
    for p in k8s.list_pods(namespace):
        if p.name.startswith(INFRA_PODS):
            continue
        ages = [a for a in (p.age_s, p.last_restart_age_s) if a is not None]
        if ages and min(ages) < min_age_s:
            out.append((p.name, min(ages)))
    return sorted(out, key=lambda x: x[1])


def wait_for_settled_pods(k8s: K8sClient, min_age_s: float = SETTLED_AGE_S,
                          max_wait_s: float = 900.0, log=None,
                          sleep=time.sleep) -> bool:
    """Chờ tới khi không còn pod ứng dụng nào trẻ hơn `min_age_s`.

    VÌ SAO CẦN: dọn dẹp sau mỗi ca làm Kubernetes tạo lại pod, và tắt rồi bật máy thì
    MỌI pod đều vừa tạo lại. Ca sau bắt đầu lúc đó thì prompt ghi pod là "RECREATED"
    — một manh mối không thuộc lỗi đang tiêm, và cũng chính là chữ ký của S3. Bạn
    cùng nhóm đo được 7/8 vòng bị nhiễm kiểu này trước khi sửa, 0/17 sau khi sửa.

    Mỗi lượt chờ đúng phần còn thiếu của pod trẻ nhất cộng 5 giây rồi kiểm lại. Quá
    `max_wait_s` nghĩa là có pod khởi động lại liên tục — lỗi thật, trả False.
    """
    deadline = time.time() + max_wait_s
    while True:
        young = young_pods(k8s, min_age_s)
        if not young:
            return True
        name, age = young[0]
        need = min_age_s - age + 5
        if time.time() + need > deadline:
            _log(f"  {len(young)} pod van tre hon {min_age_s:.0f}s sau "
                 f"{max_wait_s:.0f}s cho — co pod khoi dong lai lien tuc? "
                 f"({', '.join(n for n, _ in young)})", log)
            return False
        _log(f"  {len(young)} pod tre hon {min_age_s:.0f}s (tre nhat {name}, "
             f"{age:.0f}s), cho {need:.0f}s", log)
        sleep(need)


def wait_clean_slate(k8s: K8sClient | None = None, max_wait_s: float = 240.0,
                     poll_s: float = 10.0, log=None,
                     sleep=time.sleep) -> tuple[bool, list[str]]:
    """Như `ensure_clean_slate`, nhưng kiểm lại nhiều lần trước khi kết luận bẩn.

    VÌ SAO CẦN: hoàn tác lỗi độ trễ xong, pod CŨ còn đang xử lý dở các request ngủ 6
    giây nên mất vài chục giây mới tắt hẳn — trong lúc đó nó vẫn nằm trong danh sách
    pod với trạng thái chưa sẵn sàng. Kiểm đúng một lần thì bắt trúng khoảnh khắc đó:
    phiên đầu tiên trên máy ảo đã dừng cả phiên sau 7 ca chỉ vì vậy.

    Thứ thật sự bẩn (lỗi chưa hoàn tác, twin còn sống) không tự hết, nên chờ thêm
    vài phút cũng chỉ làm chậm việc báo lỗi, không che mất nó.
    """
    deadline = time.time() + max_wait_s
    while True:
        ok, problems = ensure_clean_slate(k8s, log=lambda m: None)
        if ok or time.time() + poll_s > deadline:
            return ok, problems
        _log(f"  chua sach ({'; '.join(problems)[:120]}), kiem lai sau {poll_s:.0f}s",
             log)
        sleep(poll_s)


def ensure_clean_slate(k8s: K8sClient | None = None, log=None) -> tuple[bool, list[str]]:
    """Kiểm tra cấu hình sạch. Trả về (sạch, danh sách vấn đề).

    KHÔNG tự sửa gì. Một script tự dọn dẹp khi thấy trạng thái lạ là script sẽ
    xóa mất chính bằng chứng của lỗi mà ta cần đọc.
    """
    k8s = k8s or K8sClient(namespace="default")
    problems: list[str] = []

    faults = load_active_faults()
    if faults:
        ids = ", ".join(f.ground_truth.fault_id for f in faults)
        problems.append(
            f"con {len(faults)} loi chua hoan tac ({ids}). "
            f"Chay: python scripts/inject.py --revert")

    # Twin sống song song với thí nghiệm production là điều mục 2 KLTN.md cấm:
    # hai môi trường cùng bơm trace vào một collector, và máy chỉ có chừng đó RAM.
    try:
        pods = k8s.list_pods(TWIN_NAMESPACE)
    except Exception:
        pods = []
    if pods:
        problems.append(
            f"namespace '{TWIN_NAMESPACE}' con {len(pods)} pod dang chay. "
            f"Chay: python scripts/twin.py --destroy")

    try:
        prod = k8s.list_pods("default")
    except Exception as e:
        problems.append(f"khong doc duoc pod cua namespace default: {e}")
        return False, problems

    not_ready = [p.name for p in prod if not p.ready]
    if not_ready:
        problems.append(
            f"{len(not_ready)} pod chua san sang: {', '.join(not_ready[:5])}")

    return (not problems), problems


def wait_for_clean_baseline(
    label: str,
    baseline: ServiceGraph | None = None,
    max_tries: int = 6,
    gap_s: int = 60,
    save: bool = True,
    log=None,
) -> SystemSnapshot | None:
    """Chụp ảnh nền, lặp lại cho tới khi diff sạch. Trả về None nếu hết lượt.

    `baseline` là ảnh nền cũ dùng để so. TRUYỀN VÀO thì phép kiểm tra sạch chạy
    đúng độ nhạy mà agent sẽ chạy — bắt được cạnh chậm gấp 3 lần. KHÔNG truyền thì
    chỉ bắt cạnh chậm hơn 500ms tuyệt đối, tức là dễ dãi hơn agent, và ca thí nghiệm
    sẽ bắt đầu trên một hệ thống mà agent coi là đang hỏng.

    Chờ tối đa 6 phút. Vẫn không sạch thì dừng hẳn: lỗi đó là lỗi thật chưa sửa,
    không phải dư âm.
    """
    for i in range(1, max_tries + 1):
        _log(f"  chup anh NEN (lan {i}/{max_tries})...", log)
        snap = take_snapshot(label=label, baseline=baseline)
        if snap.diff.is_clean():
            if save:
                path = snap.save()
                _log(f"  anh nen SACH: {path.name}", log)
            else:
                _log("  anh nen SACH", log)
            return snap

        _log(f"  chua sach: {len(snap.diff.error_edges)} canh loi, "
             f"{len(snap.diff.slow_edges)} canh cham, "
             f"{len(snap.diff.missing_edges)} canh thieu", log)
        for f in (snap.diff.error_edges + snap.diff.slow_edges)[:3]:
            _log(f"    {f.source} -> {f.target}: {f.detail}", log)
        if i < max_tries:
            _log(f"  cho {gap_s}s roi thu lai...", log)
            time.sleep(gap_s)

    _log("", log)
    _log("HE THONG CHUA SACH sau 6 phut cho.", log)
    _log("Day khong phai du am cua ca truoc ma la loi that chua duoc sua.", log)
    _log("Kiem tra: kubectl get pods   va   python scripts/smoke_snapshot.py", log)
    return None


def capture_baseline(log=None) -> tuple[ServiceGraph | None, Path | None]:
    """Chụp một ảnh nền MỚI lúc hệ thống khỏe và lưu lại.

    Gọi một lần ở đầu mỗi phiên chạy 75 ca. Ảnh nền cũ từ phiên trước có thể đã lệch:
    cấu hình đổi, bản vá mới, hoặc đơn giản là máy đang chạy tải khác. Dùng nền cũ
    thì phép so lệch ra kết quả sai lệch về phía báo động giả — và báo động giả
    trong 75 ca thì lẫn vào kết quả không gỡ ra được.

    Ảnh nền đầu tiên chụp KHÔNG có nền để so (chỉ dựa vào topology và ngưỡng tuyệt
    đối), nên nó lỏng hơn. Đây là chỗ không tránh được: phải có một điểm khởi đầu.
    """
    snap = wait_for_clean_baseline(BASELINE_LABEL, baseline=None, save=False, log=log)
    if snap is None:
        return None, None
    path = snap.save()
    _log(f"  nen moi: {path.name} — {len(snap.runtime_graph.edges)} canh, "
         f"{snap.span_count} span", log)
    return snap.runtime_graph, path


def describe_baseline_drift(old: ServiceGraph, new: ServiceGraph) -> tuple[str, bool]:
    """So ảnh nền mới với ảnh nền của phiên trước. Trả về (mô tả, có lệch nhiều không).

    VÌ SAO CẦN: chạy 75 ca chia nhiều ngày thì mỗi ngày chụp một ảnh nền mới. Ảnh
    nền quyết định độ nhạy của phép phát hiện cạnh chậm — chậm gấp 3 lần so với
    CHÍNH CẠNH ĐÓ lúc khỏe. Nền hai ngày lệch nhau nghĩa là ca ngày 1 và ca ngày 2
    được chấm bằng hai cái thước khác nhau.

    Chụp nền mới mỗi ngày vẫn đúng hơn là dùng nền cũ: máy khởi động lại, tải khác,
    độ trễ tuyệt đối đổi theo. Nhưng mức lệch phải NHÌN THẤY ĐƯỢC, không được âm
    thầm. Đây là cùng một nguyên tắc với bài học phase 4: so hai môi trường thì mọi
    biến ngoài biến đang khảo sát phải khớp, và khớp một nửa nguy hiểm hơn không
    khớp gì vì nó tạo cảm giác đã kiểm soát.
    """
    old_edges = set(old.edges)
    new_edges = set(new.edges)
    common = old_edges & new_edges

    lines = [f"  canh: {len(old_edges)} -> {len(new_edges)}, "
             f"chung {len(common)}"]
    gone = old_edges - new_edges
    added = new_edges - old_edges
    if gone:
        lines.append(f"  mat {len(gone)} canh: "
                     + ", ".join(f"{a}->{b}" for a, b in list(gone)[:3]))
    if added:
        lines.append(f"  them {len(added)} canh: "
                     + ", ".join(f"{a}->{b}" for a, b in list(added)[:3]))

    ratios = []
    for key in common:
        o, n = old.edges[key].avg_ms, new.edges[key].avg_ms
        if o > 0 and n > 0:
            ratios.append(n / o)

    big = bool(gone or added)
    if ratios:
        ratios.sort()
        mid = ratios[len(ratios) // 2]
        worst = max(ratios + [1 / r for r in ratios])
        lines.append(f"  do tre trung vi doi {mid:.2f} lan, "
                     f"canh lech nhat {worst:.2f} lan")
        # 1.5 lan la nua duong toi nguong SLOW_RATIO = 3. Vuot muc nay thi nen moi
        # va nen cu da du khac de lam lech ket qua phat hien.
        big = big or mid > 1.5 or mid < 1 / 1.5

    return chr(10).join(lines), big


def compare_with_previous_baseline(new: ServiceGraph, path: Path | None = None,
                                   log=None) -> bool:
    """In mức lệch giữa nền mới và nền của phiên trước. True nghĩa là lệch nhiều.

    `path` phải là ảnh nền TRƯỚC khi chụp nền mới. Bên gọi có trách nhiệm lấy nó
    trước, vì `find_baseline_file()` luôn trả về cái mới nhất — gọi sau khi đã lưu
    nền mới thì nó tự so với chính nó và luôn báo "không lệch gì".
    """
    if path is None:
        path = find_baseline_file()
    if path is None:
        _log("  (chua co anh nen truoc do de so)", log)
        return False
    import json

    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    old = graph_from_dict(d.get("runtime_graph", {}))
    if not old.edges:
        return False

    text, big = describe_baseline_drift(old, new)
    _log(f"  so voi nen truoc ({path.name}):", log)
    for line in text.split(chr(10)):
        _log(line, log)
    if big:
        _log("  CANH BAO: nen moi lech nhieu so voi nen phien truoc.", log)
        _log("  Cac ca chay hom nay duoc cham bang mot thuoc khac cac ca hom truoc.", log)
        _log("  Ghi dieu nay vao phan han che cua chuong ket qua.", log)
    return big
