"""Vòng lặp thí nghiệm phase 6 — 3 chế độ × 6 kịch bản × 5 lần, baseline 2 lần = 72 ca.

Đây là chỗ sinh ra toàn bộ số liệu của chương kết quả. Mỗi ca đi đúng bảy bước:

    1. kiểm tra hệ thống sạch (cấu hình và hành vi)
    2. tiêm lỗi, ghi lại ground truth và mốc thời gian
    3. chờ đủ `wait_after_inject_s` cho triệu chứng ổn định
    4. chạy chế độ tương ứng
    5. đo lần cuối, để có số "sau" cho hành động cuối cùng
    6. HOÀN TÁC hành động của agent, rồi mới hoàn tác lỗi đã tiêm
    7. ghi file JSON của ca

BƯỚC 6 LÀ MÓN NỢ MANG TỪ PHASE 5. `scripts/inject.py --revert` chỉ hoàn tác thứ
nó tiêm, nó không biết gì về những gì agent đã đổi — số bản sao, trần CPU. Chạy 75
ca mà không dọn thì mỗi ca bắt đầu từ một trạng thái khác ca trước, và không có gì
báo cho ta biết.

THỨ TỰ HOÀN TÁC LÀ NGƯỢC LẠI THỨ TỰ TÁC ĐỘNG, giống gỡ chồng sách: agent hành động
sau nên phải hoàn tác trước. Ví dụ S5 bóp trần CPU của `productcatalogservice`
xuống 10m, rồi agent nâng lên 400m. Hoàn tác agent đưa về 10m, hoàn tác lỗi đưa về
giá trị gốc. Làm ngược lại thì trần CPU kẹt ở 400m sau khi ca kết thúc.

THỨ TỰ CHẠY CÁC CA cố ý lặp-ngoài, kịch bản-giữa, chế độ-trong. Chạy hết 15 ca của
lần lặp 1 rồi mới sang lần lặp 2, thay vì chạy hết 5 lần của S1 rồi mới sang S2.
Ngắt giữa chừng thì vẫn có một lượt quét đầy đủ mọi chế độ và mọi kịch bản để so
sánh, thay vì có đầy đủ S1 và không có gì khác.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import traceback
from pathlib import Path

from src_thesis.agent.actions import ActionExecutor, ActionResult
from src_thesis.agent.react_loop import ReactAgent, compact_red
from src_thesis.eval import metrics as M
from src_thesis.eval.preflight import (
    capture_baseline,
    compare_with_previous_baseline,
    ensure_clean_slate,
    wait_clean_slate,
    wait_for_clean_baseline,
    wait_for_settled_pods,
)
from src_thesis.faults.injectors import FaultInjector, load_active_faults
from src_thesis.faults.library import (
    inject_scenario,
    load_scenarios,
    recommended_order,
    wait_seconds,
)
from src_thesis.graph.baseline import find_baseline_file, graph_from_dict
from src_thesis.graph.model import ServiceGraph
from src_thesis.k8s_client import K8sClient
from src_thesis.telemetry.snapshot import take_snapshot
from src_thesis.xai.reasoner import PROMPT_VERSION, XaiReasoner

REPO_ROOT = Path(__file__).resolve().parents[2]
EVAL_DIR = REPO_ROOT / "data" / "eval"

# File nho cua mot phien. Chay ngat quang nhieu buoi (--resume) thi moi buoi phai
# do bang CUNG MOT THUOC: cung anh nen, cung code, cung prompt, cung model. Khong
# co file nay thi moi buoi chup anh nen moi — tuc doi thuoc do giua chung phien ma
# khong ai biet.
SESSION_FILE = "session.json"

# Nhung thu ma doi mot dong la doi ket qua. Doi file ngoai danh sach nay (tai lieu,
# script ve do thi) thi khong can chan resume.
FINGERPRINT_GLOBS = ("src_thesis/**/*.py", "src_thesis/**/*.yaml",
                     "scripts/eval_run.py", "infra/**/*.yaml")


def code_fingerprint(root: Path = REPO_ROOT) -> dict:
    """Dấu vân tay của code đang chạy.

    Băm NỘI DUNG file chứ không chỉ lấy mã commit: chạy từ một thư mục chưa commit
    thì mã commit vẫn y nguyên dù code đã đổi. Mã commit và cờ "còn thay đổi chưa
    commit" vẫn ghi kèm để người đọc tìm lại được phiên bản.
    """
    h = hashlib.sha256()
    files = sorted({p for g in FINGERPRINT_GLOBS for p in root.glob(g)
                    if "__pycache__" not in p.parts})
    for p in files:
        h.update(p.relative_to(root).as_posix().encode())
        # Bo \r de cung mot code tren Windows va Linux cho cung mot dau van tay.
        h.update(p.read_bytes().replace(b"\r\n", b"\n"))

    def git(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=root, capture_output=True,
                                  text=True, timeout=15).stdout.strip()
        except Exception:
            return ""

    return {"code_sha": h.hexdigest()[:16], "n_files": len(files),
            "git_head": git("rev-parse", "--short", "HEAD"),
            "git_dirty": bool(git("status", "--porcelain", "--", "src_thesis",
                                  "scripts", "infra"))}

# Ba chế độ của mục 8 KLTN.md. `xai_only` không nằm trong ba chế độ đó — nó chỉ đo
# riêng chất lượng XAI và có ở đây để chạy thử rẻ tiền, KHÔNG đưa vào bảng so sánh.
DEFAULT_MODES = ("baseline", "direct", "twin_verified")
DEFAULT_SCENARIOS = ("S1", "S2", "S3", "S4", "S5", "S6")

# Baseline khong goi LLM va bon trong sau kich ban la loi CAU HINH ma Kubernetes
# khong bao gio tu go, nen ket qua cua no gan nhu tat dinh: lap 5 lan chi xac nhan
# lai mot dieu da biet, ton them khoang 6 gio may. Hai lan la du de thay no lap lai.
DEFAULT_BASELINE_REPEATS = 2

# Chế độ `baseline` không sửa gì, nên phải có mốc bỏ cuộc. Con số này là MỘT CÁI
# TRẦN, không phải một phép đo: ghi "baseline không hồi phục trong 600 giây" là
# đúng, ghi "MTTR của baseline là 600 giây" là sai.
#
# VÌ SAO ĐÚNG 600: bằng HAI cửa sổ quan sát. Cần ít nhất hai vì cửa sổ dài 300 giây,
# nên ngay sau khi hệ thống thật sự khỏe lại thì cửa sổ vẫn còn giữ dữ liệu lúc hỏng
# — chờ chưa đủ hai cửa sổ thì một ca ĐÃ hồi phục vẫn bị chấm là không hồi phục.
#
# Và vì sao không cần dài hơn: bốn trong năm kịch bản là lỗi CẤU HÌNH — biến môi
# trường, số bản sao, trần CPU — mà Kubernetes không bao giờ tự hoàn tác. Chỉ S3
# (xóa pod) tự khỏi, sau khoảng 30 giây. Chờ thêm 300 giây nữa chỉ để xác nhận lại
# một điều đã biết, mà nhân với 25 ca baseline là mất thêm hơn 2 giờ máy.
BASELINE_GIVE_UP_S = 600
BASELINE_POLL_GAP_S = 90


class CaseAborted(Exception):
    """Ca này không chạy được. Ghi lại lý do rồi đi tiếp ca sau."""


class RunAborted(Exception):
    """Cả phiên phải dừng — hệ thống không dọn sạch được, ca sau sẽ bị nhiễm."""


class EvalRunner:
    """Chạy toàn bộ hoặc một phần bộ 75 ca."""

    def __init__(
        self,
        run_id: str | None = None,
        modes: tuple = DEFAULT_MODES,
        scenarios: tuple = DEFAULT_SCENARIOS,
        repeats: int = 5,
        provider: str = "openai",
        model: str | None = None,
        settle_seconds: int = 300,
        max_rounds: int = 3,
        out_dir: Path = EVAL_DIR,
        dry_run: bool = False,
        baseline_give_up_s: int = BASELINE_GIVE_UP_S,
        baseline_file: str | None = None,
        baseline_repeats: int | None = DEFAULT_BASELINE_REPEATS,
        allow_code_change: bool = False,
        log=print,
    ):
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.modes = tuple(modes)
        self.scenarios = tuple(scenarios)
        self.repeats = repeats
        self.baseline_repeats = repeats if baseline_repeats is None else baseline_repeats
        self.settle_seconds = settle_seconds
        self.max_rounds = max_rounds
        self.dry_run = dry_run
        self.baseline_give_up_s = baseline_give_up_s
        self.log = log

        self.out_dir = Path(out_dir) / self.run_id
        self.k8s = K8sClient(namespace="default")
        self.executor = ActionExecutor(k8s=self.k8s, namespace="default")
        self.scenario_defs = load_scenarios()

        # TAT CACHE. `XaiReasoner` nho ket qua theo dau van tay cua phan lech, nen
        # chay lai cung mot kich ban co the lay lai dung dap an cu. Muc 8 KLTN.md
        # bat moi kich ban chay 5 lan DE CO DO LECH CHUAN — bat cache thi do lech
        # chuan ra 0 mot cach gia tao, va con so do khong noi len dieu gi ve muc
        # dao dong that cua LLM.
        self.reasoner = XaiReasoner(provider=provider, model=model, use_cache=False)
        self.model_name = self.reasoner.model

        # MOT anh nen duy nhat cho ca phien. Doi nen giua chung thi do nhay cua
        # phep phat hien doi theo, va cac ca truoc sau khong con so duoc voi nhau.
        self.baseline: ServiceGraph | None = None
        self.baseline_note = "chua chup"
        # Ghim nen: dung dung mot file nen cho moi buoi chay. Danh doi ro rang —
        # moi ca duoc cham bang cung mot thuoc, nhung neu may khoi dong lai va do
        # tre tuyet doi doi thi nen ghim tro thanh sai.
        self.baseline_file = baseline_file
        self.allow_code_change = allow_code_change
        self.fingerprint = {**code_fingerprint(), "prompt_version": PROMPT_VERSION,
                            "model": self.model_name}

    # ------------------------------------------------------------------
    # CHUAN BI
    # ------------------------------------------------------------------

    def prepare(self) -> bool:
        """Kiểm tra sạch và chụp ảnh nền cho cả phiên. False nghĩa là không chạy được."""
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.log(f"Phien: {self.run_id}   ghi vao {self.out_dir}")
        self.log(f"LLM  : {self.reasoner.provider.name} / {self.model_name} "
                 f"(cache TAT)")

        ok, problems = ensure_clean_slate(self.k8s, log=self.log)
        if not ok:
            self.log("")
            self.log("KHONG CHAY DUOC — he thong chua sach:")
            for p in problems:
                self.log(f"  - {p}")
            return False

        fp = self.fingerprint
        self.log(f"Code : {fp['code_sha']} ({fp['n_files']} file, git {fp['git_head']}"
                 f"{', CHUA COMMIT' if fp['git_dirty'] else ''}), "
                 f"prompt {fp['prompt_version']}")
        session = self._load_session()
        if session is not None and not self._session_matches(session):
            return False

        if self.dry_run:
            self.baseline_note = "dry_run: khong chup nen"
            return True

        # Tat may roi bat lai thi moi pod deu vua tao lai. Chup nen luc do thi muc
        # "luc khoe" bi doi len (pod moi khoi dong cham), lam phat hien canh cham kem
        # nhay suot ca phien — loi nay ban cung nhom da gap that.
        if not wait_for_settled_pods(self.k8s, log=self.log):
            self.log("KHONG CHAY DUOC — co pod khoi dong lai lien tuc")
            return False

        # Resume: dung lai DUNG anh nen cua buoi dau, tru khi nguoi chay ghim file khac.
        if session is not None and not self.baseline_file:
            prev = session.get("baseline_file")
            if prev and (REPO_ROOT / prev).exists():
                self.baseline_file = str(REPO_ROOT / prev)
                self.log(f"Resume: dung lai anh nen cua phien ({prev})")
            elif prev:
                self.log(f"KHONG CHAY DUOC — phien ghi anh nen {prev} nhung file "
                         f"khong con. Ghim file khac bang --baseline-file neu chap "
                         f"nhan doi thuoc do.")
                return False

        if self.baseline_file:
            path = Path(self.baseline_file)
            if not path.exists():
                self.log(f"KHONG CO anh nen da ghim: {path}")
                return False
            d = json.loads(path.read_text(encoding="utf-8"))
            graph = graph_from_dict(d.get("runtime_graph", {}))
            if not graph.edges:
                self.log(f"{path.name} khong co canh nao, khong dung lam nen duoc")
                return False
            self.baseline = graph
            self.baseline_note = f"{path.name} (GHIM, {len(graph.edges)} canh)"
            self.log(f"Dung anh nen da ghim: {self.baseline_note}")
            if session is None:
                self._save_session(path)
            return True

        # Lay nen cua phien TRUOC ngay bay gio, truoc khi nen moi duoc luu de.
        prev = find_baseline_file()

        self.log("Chup anh nen cho ca phien...")
        graph, path = capture_baseline(log=self.log)
        if graph is None:
            return False
        self.baseline = graph
        self.baseline_note = f"{path.name} ({len(graph.edges)} canh)"
        compare_with_previous_baseline(graph, prev, log=self.log)
        self._save_session(path)
        return True

    # ------------------------------------------------------------------
    # FILE PHIEN
    # ------------------------------------------------------------------

    def _load_session(self) -> dict | None:
        path = self.out_dir / SESSION_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _session_matches(self, session: dict) -> bool:
        """Resume chỉ được phép khi code, prompt và model y như buổi đầu."""
        diffs = [f"{k}: {session.get(k)} -> {self.fingerprint.get(k)}"
                 for k in ("code_sha", "prompt_version", "model")
                 if session.get(k) != self.fingerprint.get(k)]
        if not diffs:
            return True
        self.log("Code/prompt/model DA DOI so voi luc bat dau phien:")
        for d in diffs:
            self.log(f"  - {d}")
        if self.allow_code_change:
            self.log("  --allow-code-change: van chay tiep, nho ghi ro vao bao cao.")
            return True
        self.log("KHONG CHAY TIEP: cac ca truoc va sau se do bang hai thuoc khac "
                 "nhau. Tra code ve nhu cu, hoac mo phien moi, hoac them "
                 "--allow-code-change neu chap nhan.")
        return False

    def _save_session(self, baseline_path: Path | None) -> None:
        rel = None
        if baseline_path is not None:
            try:
                rel = Path(baseline_path).resolve().relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = str(baseline_path)
        data = {**self.fingerprint, "run_id": self.run_id, "baseline_file": rel,
                "created_at_human": time.strftime("%Y-%m-%d %H:%M:%S")}
        (self.out_dir / SESSION_FILE).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def plan(self) -> list[tuple[str, str, int]]:
        """Danh sách ca theo thứ tự chạy: lặp ngoài, kịch bản giữa, chế độ trong."""
        order = [s for s in recommended_order() if s in self.scenarios]
        order += [s for s in self.scenarios if s not in order]
        cases: list[tuple[str, str, int]] = []
        for rep in range(1, self.repeats + 1):
            for sid in order:
                for mode in self.modes:
                    if mode == "baseline" and rep > self.baseline_repeats:
                        continue
                    cases.append((sid, mode, rep))
        return cases

    def case_path(self, sid: str, mode: str, rep: int) -> Path:
        return self.out_dir / f"{sid}_{mode}_r{rep}.json"

    def case_minutes(self, mode: str) -> float:
        """Ước lượng thời gian một ca, từ số đo thật của phase 5 chứ không phải đoán.

        Ba phần cộng lại: khoảng 9 phút chờ hệ thống sạch rồi tiêm và chờ triệu
        chứng ổn định, thời gian chạy chế độ, và khoảng 2 phút dọn dẹp.
        """
        per_mode = {"baseline": 9 + self.baseline_give_up_s / 60 + 2,
                    "xai_only": 9 + 3 + 2,
                    "direct": 9 + 3 + 2,
                    "twin_verified": 9 + 20 + 2}
        return per_mode.get(mode, 15)

    def estimate_minutes(self, cases: list) -> float:
        return sum(self.case_minutes(m) for _, m, _ in cases)

    # ------------------------------------------------------------------
    # MOT CA
    # ------------------------------------------------------------------

    def run_case(self, sid: str, mode: str, rep: int) -> dict:
        """Chạy trọn một ca. Ném `CaseAborted` nếu ca hỏng, `RunAborted` nếu cả phiên hỏng."""
        scenario = self.scenario_defs[sid]
        case_id = f"{sid}_{mode}_r{rep}"
        self.log("")
        self.log("=" * 70)
        self.log(f"CA {case_id}")
        self.log("=" * 70)

        # --- 1. sach chua ---
        ok, problems = (wait_clean_slate(self.k8s, log=self.log) if not self.dry_run
                        else ensure_clean_slate(self.k8s, log=self.log))
        if not ok:
            raise RunAborted("; ".join(problems))

        if not self.dry_run:
            # Don dep ca truoc lam Kubernetes tao lai pod. Khong cho thi prompt cua
            # ca nay mang dau "RECREATED" cua ca truoc.
            if not wait_for_settled_pods(self.k8s, log=self.log):
                raise CaseAborted("co pod khoi dong lai lien tuc")
            snap = wait_for_clean_baseline(f"eval-{case_id}-truoc",
                                           baseline=self.baseline,
                                           save=False, log=self.log)
            if snap is None:
                raise CaseAborted("he thong khong tro ve trang thai sach")

        # --- 2. tiem loi ---
        inj = FaultInjector()
        self.log(f"Tiem {sid}: {scenario.get('fault')} vao {scenario.get('target')}")
        if self.dry_run:
            faults, ground_truths, injected_at = [], [], time.time()
        else:
            faults = inject_scenario(inj, scenario)
            ground_truths = [f.ground_truth.to_dict() for f in faults]
            injected_at = min(f.ground_truth.injected_at for f in faults)
            for gt in ground_truths:
                self.log(f"  {gt['fault_id']} -> {gt['target_service']} "
                         f"({gt['fault_type']})")

        case: dict = {
            "case_id": case_id,
            "run_id": self.run_id,
            "scenario": sid,
            "mode": mode,
            "repeat": rep,
            "model": self.model_name,
            # Ca nao cung mang dau van tay code: doc lai mot ca le van biet no do
            # bang phien ban code, prompt nao.
            "code": self.fingerprint,
            "baseline_source": self.baseline_note,
            "injected_at": injected_at,
            "injected_at_human": time.strftime("%Y-%m-%d %H:%M:%S",
                                               time.localtime(injected_at)),
            "ground_truth": ground_truths,
            "settle_seconds": self.settle_seconds,
            "errors": [],
        }

        try:
            # --- 3. cho trieu chung on dinh ---
            self._wait(wait_seconds(scenario), "cho trieu chung on dinh")

            # --- 4. chay che do ---
            if mode == "baseline":
                report, final_red = self._run_baseline_mode(case_id, injected_at)
            else:
                report, final_red = self._run_agent_mode(mode, case_id)
            case["report"] = report
            case["final_red"] = final_red

            # --- 5, 6 tinh diem ---
            outcome = self._score(case)
            case["outcome"] = outcome.to_dict()
        finally:
            # --- 6. don dep, LUON chay du buoc tren co hong hay khong ---
            case["cleanup"] = self._cleanup(case.get("report") or {})

        self._save_case(sid, mode, rep, case)
        return case

    def _wait(self, seconds: int, why: str) -> None:
        if self.dry_run:
            self.log(f"  (dry_run) bo qua {seconds}s {why}")
            return
        self.log(f"  {why}: {seconds}s")
        remain = seconds
        while remain > 0:
            step = min(60, remain)
            time.sleep(step)
            remain -= step
            if remain > 0:
                self.log(f"    con {remain}s")

    # ------------------------------------------------------------------
    # CAC CHE DO
    # ------------------------------------------------------------------

    def _run_baseline_mode(self, case_id: str, injected_at: float) -> tuple[dict, dict]:
        """Chế độ đối chứng: không agent, không sửa, chỉ theo dõi.

        Đo xem hệ thống có TỰ hồi phục không và mất bao lâu. S3 (xóa pod) tự khỏi
        sau khoảng 30 giây nên chế độ này sẽ hồi phục; bốn kịch bản còn lại là lỗi
        cấu hình nên sẽ không, và chúng bị cắt cụt ở `baseline_give_up_s`.

        Đó chính là điều cần chứng minh: không có agent thì lỗi cấu hình nằm đó mãi.
        """
        started = time.time()
        rounds, recovered_at = self._observe_until_healthy(
            f"eval-{case_id}", started + self.baseline_give_up_s)

        report = {
            "run_id": case_id,
            "mode": "baseline",
            "namespace": "default",
            "started_at": started,
            "took_s": round(time.time() - started, 2),
            "rounds_used": len(rounds),
            "max_rounds": 0,
            "baseline_source": self.baseline_note,
            "has_baseline": self.baseline is not None,
            "healthy_at_end": recovered_at is not None,
            "stop_reason": ("he thong tu hoi phuc" if recovered_at
                            else f"bo cuoc sau {self.baseline_give_up_s}s, "
                                 f"khong co agent nen khong ai sua"),
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "actions_applied": 0,
            "actions_rejected_by_twin": 0,
            "gave_up_at": None if recovered_at else time.time(),
            "rounds": rounds,
        }
        return report, rounds[-1]["red"] if rounds else {}

    def _observe_until_healthy(self, label: str, deadline: float,
                               first_round_no: int = 1) -> tuple[list[dict], float | None]:
        """Chụp snapshot mỗi `BASELINE_POLL_GAP_S` giây cho tới khi hệ thống khỏe
        hoặc tới `deadline`. Trả về (các vòng theo dõi, thời điểm thấy khỏe hoặc None).

        Dùng chung cho baseline và cho phần theo dõi sau khi agent dừng, để MTTR của
        ba chế độ đo bằng cùng một luật "khỏe" và cùng một nhịp quan sát.
        """
        rounds: list[dict] = []
        while True:
            no = first_round_no + len(rounds)
            snap = take_snapshot(label=f"{label}-r{no}", baseline=self.baseline)
            d = snap.to_dict()
            diff = d.get("diff", {})
            n_err = len(diff.get("error_edges", []))
            n_slow = len(diff.get("slow_edges", []))
            n_miss = len(diff.get("missing_edges", []))
            healthy = n_err == 0 and n_slow == 0 and n_miss == 0
            rounds.append({
                "round_no": no,
                "snapshot_label": d.get("label", ""),
                "observed_at": d.get("taken_at", time.time()),
                "diff_summary": f"{n_err} canh loi, {n_slow} canh cham, "
                                f"{n_miss} canh thieu",
                "healthy": healthy,
                "red": compact_red(d.get("red", {})),
                "reasoning_ran": False,
            })
            self.log(f"  theo doi vong {no}: {rounds[-1]['diff_summary']}")
            if healthy:
                return rounds, rounds[-1]["observed_at"]
            if time.time() >= deadline or self.dry_run:
                return rounds, None
            time.sleep(BASELINE_POLL_GAP_S)

    def _run_agent_mode(self, mode: str, case_id: str) -> tuple[dict, dict]:
        """Chạy agent ReAct ở chế độ `direct`, `twin_verified` hoặc `xai_only`."""
        agent = ReactAgent(
            mode=mode,
            namespace="default",
            max_rounds=self.max_rounds,
            settle_seconds=self.settle_seconds,
            dry_run=self.dry_run,
            reasoner=self.reasoner,
            baseline=self.baseline,
        )

        def on_round(r: dict) -> None:
            self.log(f"  vong {r.get('round_no')}: {r.get('diff_summary')}")
            exp = r.get("explanation") or {}
            if exp:
                self.log(f"    chan doan {exp.get('root_cause_service')} / "
                         f"{exp.get('fault_type')}")
            act = r.get("chosen_action") or {}
            if act:
                self.log(f"    hanh dong {act.get('action')} tren "
                         f"{act.get('target')} [{r.get('risk_class')}]")
            if r.get("twin_used"):
                v = r.get("twin_verdict") or {}
                self.log(f"    twin: {str(v.get('verdict')).upper()}")
            if r.get("skipped_reason"):
                self.log(f"    BI CHAN: {r['skipped_reason'][:100]}")

        report = agent.run(run_id=case_id, save=True, on_round=on_round)

        # THEO DOI TIEP SAU KHI AGENT DUNG. Agent dung vi chon `no_action`, vi het
        # tran vong, hay vi XAI hong — deu KHONG co lan quan sat nao sau do, nen
        # report ghi "chua khoe" du he thong khoe ngay sau. Phien dau tren may ao do
        # sai dung cho nay: ca ba ca S3 co agent bi ghi "khong hoi phuc" trong khi
        # baseline cung kich ban ghi hoi phuc sau 211 giay, ma he thong tu khoe
        # giong het nhau. MTTR tinh toi luc he thong khoe, bat ke ai sua, nen phai
        # theo doi bang dung luat va nhip cua baseline.
        final_red: dict = {}
        if not self.dry_run and not report.get("healthy_at_end"):
            post, recovered_at = self._observe_until_healthy(
                f"eval-{case_id}-sau", time.time() + self.baseline_give_up_s,
                first_round_no=len(report.get("rounds") or []) + 1)
            report["post_rounds"] = post
            if recovered_at is not None:
                report["healthy_at_end"] = True
                report["recovered_at"] = recovered_at
                report["recovered_after_agent_stopped"] = True
            else:
                report["gave_up_at"] = time.time()
            final_red = post[-1]["red"] if post else {}
        elif not self.dry_run:
            # DO LAN CUOI. Vong cuoi cung co the vua thi hanh mot hanh dong roi vong
            # lap ket thuc — khong co lan quan sat nao sau no. Thieu phep do nay thi
            # hanh dong cuoi cung khong tinh duoc harmful hay khong.
            snap = take_snapshot(label=f"eval-{case_id}-final",
                                 baseline=self.baseline)
            final_red = compact_red(snap.to_dict().get("red", {}))
        return report, final_red

    # ------------------------------------------------------------------
    # CHAM DIEM
    # ------------------------------------------------------------------

    def _score(self, case: dict) -> M.CaseOutcome:
        """Tính đủ 6 chỉ số đo được từ một ca (chỉ số 7 đo riêng ở phase 4)."""
        report = case.get("report") or {}
        rounds = report.get("rounds") or []
        gts = case.get("ground_truth") or []
        notes: list[str] = []

        out = M.CaseOutcome(
            scenario=case["scenario"], mode=case["mode"],
            repeat=case["repeat"], run_id=report.get("run_id", case["case_id"]),
            rounds_used=report.get("rounds_used", 0),
            input_tokens=report.get("total_input_tokens", 0),
            output_tokens=report.get("total_output_tokens", 0),
            took_s=report.get("took_s", 0.0),
            actions_rejected_by_twin=report.get("actions_rejected_by_twin", 0),
            blocked_by_precondition=report.get("blocked_by_precondition", 0),
            drift_checks=report.get("drift_checks", 0),
            blocked_by_drift=report.get("blocked_by_drift", 0),
            blocked_recovered=report.get("blocked_recovered", 0),
            auto_undos=report.get("auto_undos", 0),
        )
        for r in rounds:
            if (r.get("twin_verdict") or {}).get("verdict") != "better":
                continue
            out.twin_better += 1
            prod = ((r.get("watch") or {}).get("verdict") or {}).get("verdict")
            if r.get("promoted") and prod == "better":
                out.twin_better_transferred += 1
        out.cost_usd = M.cost_usd(case.get("model", ""), out.input_tokens,
                                  out.output_tokens)

        # --- chi so 1 va 2: chan doan cua VONG DAU TIEN co goi LLM ---
        first = next((r for r in rounds if r.get("reasoning_ok")
                      and r.get("explanation")), None)
        if first is not None and gts:
            exp = first["explanation"]
            # S6 tiem hai loi: chi dung mot trong hai nguyen nhan goc la dat. Cham
            # chat hon the la phat model vi mot chuyen ma schema khong cho phep no
            # dien dat — `root_cause_service` chi co mot o.
            targets = [g.get("target_service", "") for g in gts]
            predicted = exp.get("root_cause_service", "")
            out.predicted_root = predicted
            out.expected_root = " hoac ".join(targets)
            out.root_cause_correct = any(
                M.root_cause_correct(predicted, t) for t in targets)
            expected_prop: list[str] = []
            for g in gts:
                expected_prop += g.get("expected_propagation", [])
            out.propagation_jaccard = M.propagation_accuracy(
                exp.get("propagation_path") or [], expected_prop)
            out.fault_type_correct = any(
                exp.get("fault_type") == g.get("fault_type") for g in gts)
            actions = [a.get("action", "")
                       for a in (exp.get("proposed_actions") or [])]
            correct: list[str] = []
            for g in gts:
                correct += g.get("correct_actions", [])
            out.action_correct = M.action_correct(actions, correct)
        elif case["mode"] != "baseline":
            notes.append("khong vong nao chan doan thanh cong")

        # --- chi so 3: MTTR ---
        recovered_at = report.get("recovered_at")
        if recovered_at is None and report.get("healthy_at_end"):
            healthy_round = next((r for r in rounds if r.get("healthy")), None)
            if healthy_round is not None:
                recovered_at = healthy_round.get("observed_at")
        gave_up = report.get("gave_up_at") or (
            report.get("started_at", 0) + report.get("took_s", 0))
        out.mttr_s, out.recovered, out.censored_at_s = M.mttr(
            case["injected_at"], recovered_at, gave_up)
        if not out.recovered:
            notes.append("khong hoi phuc — ca nay bi cat cut, dung nhet vao "
                         "trung binh MTTR")

        # --- chi so 4 va 5: tac dong cua tung hanh dong ---
        final_red = case.get("final_red") or {}
        effects: list[dict] = []
        for i, r in enumerate(rounds):
            ar = r.get("action_result")
            if not ar:
                continue
            if ar.get("action") == "no_action":
                effects.append({"action": "no_action", "target": ar.get("target", ""),
                                "verdict": "no_action",
                                "reason": "agent co y khong lam gi"})
                continue
            # Uu tien cap so do cua node `watch`: do ngay truoc khi ap va mot cua so
            # sau khi ap. Lay RED cua vong sau nhu cach cu thi SAI khi da tu hoan tac:
            # vong sau do trang thai SAU hoan tac, va mot hanh dong co hai trong
            # nhu vo hai. File ca cu chua co `watch` thi van cham theo cach cu.
            watch = r.get("watch") or {}
            if watch.get("before") is not None and watch.get("after") is not None:
                before, after = watch["before"], watch["after"]
            else:
                before = r.get("red") or {}
                after = (rounds[i + 1].get("red") if i + 1 < len(rounds)
                         else final_red)
            eff = M.classify_action_effect(
                ar.get("action", ""), ar.get("target", ""),
                before, after or {}, applied=bool(ar.get("ok")))
            effects.append(eff.to_dict())

        out.action_effects = effects
        out.harmful_actions = sum(1 for e in effects if e["verdict"] == "harmful")
        out.helpful_actions = sum(1 for e in effects if e["verdict"] == "helpful")
        out.wasted_actions = sum(1 for e in effects
                                 if e["verdict"] in ("neutral", "not_applied"))
        out.unknown_effect_actions = sum(1 for e in effects
                                         if e["verdict"] == "unknown")
        if out.unknown_effect_actions:
            notes.append(f"{out.unknown_effect_actions} hanh dong khong du luu luong "
                         f"de ket luan — KHAC voi 'khong doi gi'")

        out.notes = notes
        return out

    # ------------------------------------------------------------------
    # DON DEP
    # ------------------------------------------------------------------

    def _cleanup(self, report: dict) -> dict:
        """Hoàn tác hành động của agent, rồi hoàn tác lỗi đã tiêm. Theo thứ tự đó.

        Trả về nhật ký dọn dẹp để lưu vào file ca — dọn dẹp thất bại mà không ghi
        lại thì ca sau bị nhiễm mà không ai biết vì sao.
        """
        log: dict = {"agent_undos": [], "fault_revert": [], "clean_after": False}

        if self.dry_run:
            log["skipped"] = "dry_run"
            return log

        # --- hoan tac hanh dong cua agent, theo thu tu NGUOC ---
        # Hoan tac moi hanh dong DA DOI cluster (`applied`), khong chi cai `ok`: mot
        # hanh dong doi xong ma doc lai khong khop van de lai thay doi tren cluster.
        # Bo qua cai da duoc agent tu hoan tac thanh cong.
        applied: list[dict] = []
        for r in report.get("rounds") or []:
            ar = r.get("action_result")
            if (r.get("auto_undo") or {}).get("ok"):
                continue
            if ar and ar.get("applied") and ar.get("undo_kind", "none") != "none":
                applied.append(ar)
        for ar in reversed(applied):
            payload = {k: v for k, v in ar.items() if k != "ok"}
            try:
                res = self.executor.undo(ActionResult(**payload))
                log["agent_undos"].append(res.to_dict())
                self.log(f"  hoan tac {ar.get('action')} tren {ar.get('target')}: "
                         f"{'OK' if res.ok else 'THAT BAI'} — {res.detail}")
            except Exception as e:
                log["agent_undos"].append({"action": ar.get("action"),
                                           "error": str(e)[:300]})
                self.log(f"  hoan tac {ar.get('action')} NEM LOI: {e}")

        # --- hoan tac loi da tiem ---
        for active in reversed(load_active_faults()):
            fid = active.ground_truth.fault_id
            target = active.ground_truth.target_service
            try:
                active.revert(K8sClient(namespace=active.namespace))
                ready = self.k8s.wait_ready(target, timeout=180)
                log["fault_revert"].append({"fault_id": fid, "target": target,
                                            "ready": ready})
                self.log(f"  hoan tac {fid}: {target} san sang = {ready}")
            except Exception as e:
                log["fault_revert"].append({"fault_id": fid, "error": str(e)[:300]})
                self.log(f"  hoan tac {fid} NEM LOI: {e}")

        ok, problems = wait_clean_slate(self.k8s, log=self.log)
        log["clean_after"] = ok
        log["problems_after"] = problems
        return log

    # ------------------------------------------------------------------
    # GHI FILE
    # ------------------------------------------------------------------

    def _save_case(self, sid: str, mode: str, rep: int, case: dict) -> Path:
        path = self.case_path(sid, mode, rep)
        path.write_text(json.dumps(case, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        self.log(f"  da ghi: {path.name}")
        return path

    def load_outcomes(self) -> list[M.CaseOutcome]:
        """Đọc lại mọi ca đã chạy trong phiên này."""
        out: list[M.CaseOutcome] = []
        for path in sorted(self.out_dir.glob("*.json")):
            if path.name == "index.json":
                continue
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            o = d.get("outcome")
            if o:
                out.append(M.CaseOutcome(**o))
        return out

    def write_index(self) -> Path:
        """Gộp mọi ca đã chạy thành một file tổng, kèm tóm tắt theo chế độ."""
        outcomes = self.load_outcomes()
        by_mode: dict[str, list] = {}
        for o in outcomes:
            by_mode.setdefault(o.mode, []).append(o)

        index = {
            "run_id": self.run_id,
            "written_at_human": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": self.model_name,
            "code": self.fingerprint,
            "baseline_source": self.baseline_note,
            "thresholds": {
                "harmful_error_delta": M.HARMFUL_ERROR_DELTA,
                "harmful_p95_ratio": M.HARMFUL_P95_RATIO,
                "min_rate_for_effect": M.MIN_RATE_FOR_EFFECT,
                "baseline_give_up_s": self.baseline_give_up_s,
            },
            "n_cases": len(outcomes),
            "by_mode": {m: M.summarize_mode(v, m).to_dict()
                        for m, v in by_mode.items()},
            "cases": [o.to_dict() for o in outcomes],
        }
        path = self.out_dir / "index.json"
        path.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return path

    # ------------------------------------------------------------------
    # CHAY CA PHIEN
    # ------------------------------------------------------------------

    def run_all(self, limit: int | None = None, resume: bool = True,
                budget_minutes: float | None = None) -> int:
        """Chạy mọi ca trong kế hoạch. Trả về mã thoát cho dòng lệnh.

        `budget_minutes` là quỹ thời gian của BUỔI chạy này. Hết quỹ thì dừng
        TRƯỚC KHI bắt đầu một ca mới, không cắt ngang ca đang chạy.

        Vì sao không cắt ngang: một ca bị cắt giữa chừng để lại lỗi đã tiêm và hành
        động của agent còn nguyên trên hệ thống. Buổi sau chạy tiếp sẽ bắt đầu từ
        một hệ thống bẩn, và `ensure_clean_slate` sẽ chặn lại. Thà chạy quá giờ vài
        chục phút còn hơn để lại một hệ thống hỏng qua đêm.
        """
        if not self.prepare():
            return 1

        cases = self.plan()
        todo = [c for c in cases
                if not (resume and self.case_path(*c).exists())]
        skipped = len(cases) - len(todo)
        if limit is not None:
            todo = todo[:limit]

        self.log("")
        self.log(f"Ke hoach: {len(cases)} ca, da co {skipped}, se chay {len(todo)}")
        self.log(f"Uoc luong: {self.estimate_minutes(todo) / 60:.1f} gio")
        if budget_minutes:
            self.log(f"Quy thoi gian buoi nay: {budget_minutes / 60:.1f} gio — "
                     f"het quy thi dung TRUOC mot ca moi, khong cat ngang ca dang chay")
        self.log("")

        started = time.time()
        done = 0
        for sid, mode, rep in todo:
            if budget_minutes is not None:
                elapsed = (time.time() - started) / 60
                need = self.case_minutes(mode)
                if elapsed + need > budget_minutes:
                    self.log("")
                    self.log(f"HET QUY THOI GIAN buoi nay: da chay {elapsed:.0f} phut, "
                             f"ca ke tiep can them {need:.0f} phut.")
                    self.log(f"Da xong {done} ca, con {len(todo) - done} ca.")
                    self.log(f"Buoi sau chay tiep: "
                             f"python -u scripts/eval_run.py --resume {self.run_id}")
                    break
            try:
                self.run_case(sid, mode, rep)
                done += 1
            except CaseAborted as e:
                self.log(f"  BO CA {sid}_{mode}_r{rep}: {e}")
            except RunAborted as e:
                self.log("")
                self.log(f"DUNG CA PHIEN: {e}")
                self.log("Ca sau se bi nhiem neu chay tiep. Sua roi chay lai — "
                         "cac ca da xong duoc giu, khong phai chay lai.")
                self.write_index()
                return 1
            except KeyboardInterrupt:
                self.log("")
                self.log("NGUOI DUNG NGAT. Dang don dep truoc khi thoat...")
                self._cleanup({})
                self.write_index()
                return 130
            except Exception as e:
                self.log(f"  CA {sid}_{mode}_r{rep} NEM LOI: {e}")
                self.log(traceback.format_exc()[:1500])
                self._cleanup({})

            self.log(f"  tien do: {done}/{len(todo)}")

        path = self.write_index()
        self.log("")
        self.log(f"Xong {done}/{len(todo)} ca. Tong hop: {path}")
        return 0
