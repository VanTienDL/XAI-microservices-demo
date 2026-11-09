"""Chay bo thi nghiem phase 6.

  python -u scripts/eval_run.py --dry-run              thu luong, khong tiem va khong sua
  python -u scripts/eval_run.py --limit 5              chay thu 5 ca de uoc luong
  python -u scripts/eval_run.py                        chay du 75 ca
  python -u scripts/eval_run.py --budget-minutes 360   chay 6 tieng roi dung sach
  python -u scripts/eval_run.py --resume 20260825-0900 chay tiep phien da ngat
  python -u scripts/eval_run.py --summary 20260825-0900 chi in bang, khong chay gi

LUON DUNG `python -u`. Khong co `-u` thi Python gom dau ra lai khi bi chuyen huong,
va mot phien chay hang chuc tieng se im lang hoan toan — luc do khong phan biet
duoc dang chay voi dang treo. Bai hoc nay da tra gia mot lan o phase 3.

CANH BAO: script nay DOI THAT he thong production — tiem loi, de agent sua, roi
hoan tac. Dung chay khi dang can he thong de lam viec khac. Truoc khi chay phai
chac chan khong con loi nao dang tiem va namespace `twin` da bi xoa.

THOI GIAN: mot ca `twin_verified` mat khoang 31 phut, mot ca `direct` khoang 14
phut, mot ca `baseline` khoang 26 phut (phan lon la cho xem he thong co tu hoi
phuc khong). Du 75 ca vao khoang 30 tieng.

CHIA NHIEU BUOI: dung CUNG mot ma phien cho moi buoi. Ca da xong duoc giu lai va
`--resume` bo qua chung. Hai cach chia:

  theo lan lap  buoi 1 `--repeats 1`, buoi 2 `--resume <ma> --repeats 2`, ...
                moi buoi khoang 5,9 tieng va cho mot luot quet DAY DU 3 che do
                x 5 kich ban — ngung luc nao cung con mot bo so lieu so sanh duoc

  theo dong ho  `--budget-minutes 360` moi buoi, chay tiep bang `--resume`
                dung TRUOC mot ca moi khi het quy, khong cat ngang ca dang chay
"""

import argparse
import sys
from pathlib import Path

# Console Windows mac dinh dung bang ma cp1252, in tieng Viet se vo va script chet.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

# Doc khoa API tu .env o thu muc goc repo. File nay KHONG duoc commit.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

from src_thesis.eval import metrics as M
from src_thesis.eval.runner import (
    DEFAULT_BASELINE_REPEATS,
    DEFAULT_MODES,
    DEFAULT_SCENARIOS,
    EVAL_DIR,
    EvalRunner,
)


def print_summary(run_id: str) -> int:
    """In bang tong hop cua mot phien da chay."""
    import json

    path = EVAL_DIR / run_id / "index.json"
    if not path.exists():
        print(f"Khong co {path}. Chay `--resume {run_id}` de tao lai bang tong hop.")
        return 1
    data = json.loads(path.read_text(encoding="utf-8"))

    print(f"Phien {data['run_id']}   model {data.get('model')}")
    print(f"Anh nen: {data.get('baseline_source')}")
    print(f"So ca  : {data.get('n_cases')}")
    th = data.get("thresholds", {})
    print(f"Nguong harmful: loi tang >= {th.get('harmful_error_delta')} "
          f"hoac p95 tang >= {th.get('harmful_p95_ratio')}")
    print(f"Nguong du lieu: chi ket luan khi >= {th.get('min_rate_for_effect')} req/s")
    print("")

    for mode in ("baseline", "xai_only", "direct", "twin_verified"):
        s = (data.get("by_mode") or {}).get(mode)
        if not s:
            continue
        print(f"--- {mode} ({s['n_cases']} ca) ---")
        if s.get("root_cause_accuracy") is not None:
            print(f"  chi so 1 root cause : {s['root_cause_accuracy'] * 100:.1f}% "
                  f"(do lech {s['root_cause_std']:.3f})")
        if s.get("propagation_mean") is not None:
            print(f"  chi so 2 propagation: {s['propagation_mean']:.3f}")
        print(f"  chi so 3 MTTR       : ", end="")
        if s.get("mttr_mean_s") is not None:
            print(f"{s['mttr_mean_s']:.0f}s tren {s['n_recovered']} ca hoi phuc")
        else:
            print("khong ca nao hoi phuc")
        print(f"           khong hoi phuc: {s['n_censored']}/{s['n_cases']} ca "
              f"(bi cat cut, KHONG tinh vao trung binh tren)")
        print(f"  chi so 4 harmful    : {s['harmful_total']} "
              f"({s['harmful_per_case']:.2f} moi ca)")
        print(f"  chi so 5 wasted     : {s['wasted_total']} "
              f"({s['wasted_per_case']:.2f} moi ca)")
        if s.get("unknown_effect_total"):
            print(f"           khong ket luan duoc: {s['unknown_effect_total']} "
                  f"hanh dong thieu luu luong")
        print(f"  bi twin chan        : {s['rejected_by_twin_total']}")
        if s.get("twin_better_total"):
            rate = s.get("twin_transfer_rate")
            print(f"  chuyen giao twin->prod: "
                  f"{rate * 100:.0f}% tren {s['twin_better_total']} lan twin noi better")
        if s.get("drift_checks_total"):
            rate = s.get("drift_failure_rate") or 0.0
            print(f"  that bai do do lech : {rate * 100:.0f}% tren "
                  f"{s['drift_checks_total']} lan doi sanh "
                  f"(lech cau hinh {s.get('blocked_by_drift_total', 0)}, "
                  f"tu khoe truoc khi ap {s.get('blocked_recovered_total', 0)})")
        if s.get("blocked_by_precondition_total") or s.get("auto_undo_total"):
            print(f"  chan vi dieu kien   : {s.get('blocked_by_precondition_total', 0)}"
                  f"   tu hoan tac: {s.get('auto_undo_total', 0)}")
        print(f"  chi so 6 token/ca    : {s['tokens_per_case']:.0f}", end="")
        if s.get("cost_per_case") is not None:
            print(f"   tien/ca: ${s['cost_per_case']:.5f}")
        else:
            print("   (chua biet gia model nay)")
        print(f"  thoi gian trung binh: {s['took_mean_s']:.0f}s")
        print("")

    # Chi so 7 do rieng o phase 4, doc tu file fidelity moi nhat.
    fid_dir = Path(EVAL_DIR).parents[0] / "fidelity"
    files = sorted(fid_dir.glob("*_fidelity.json")) if fid_dir.exists() else []
    if files:
        rate, match, total = M.twin_fidelity(files[-1])
        if rate is not None:
            print(f"chi so 7 twin fidelity: {rate * 100:.1f}% ({match}/{total}) "
                  f"— {files[-1].name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Chay bo thi nghiem phase 6")
    ap.add_argument("--modes", default=",".join(DEFAULT_MODES),
                    help="danh sach che do, cach nhau bang dau phay")
    ap.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS))
    ap.add_argument("--repeats", type=int, default=5,
                    help="so lan moi kich ban; muc 8 KLTN.md doi toi thieu 5")
    ap.add_argument("--baseline-repeats", type=int, default=DEFAULT_BASELINE_REPEATS,
                    help="so lan cho rieng che do baseline (khong goi LLM, gan nhu "
                         "tat dinh); dat bang --repeats neu muon chay du")
    ap.add_argument("--limit", type=int, default=None,
                    help="chay toi da bao nhieu ca roi dung, de uoc luong thoi gian")
    ap.add_argument("--resume", default=None,
                    help="ma phien cu, chay tiep nhung ca chua co file")
    ap.add_argument("--summary", default=None,
                    help="chi in bang tong hop cua mot phien, khong chay gi")
    ap.add_argument("--settle", type=int, default=300,
                    help="giay cho sau moi hanh dong (mac dinh 300 = mot cua so)")
    ap.add_argument("--max-rounds", type=int, default=3)
    ap.add_argument("--provider", default="openai", choices=["openai", "groq"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--baseline-give-up", type=int, default=600,
                    help="giay cho che do baseline truoc khi ket luan khong hoi phuc")
    ap.add_argument("--budget-minutes", type=float, default=None,
                    help="quy thoi gian cua BUOI nay; het quy thi dung truoc mot ca "
                         "moi chu khong cat ngang ca dang chay")
    ap.add_argument("--baseline-file", default=None,
                    help="ghim mot file anh nen cho moi buoi, thay vi chup nen moi "
                         "moi buoi; dung khi may khong khoi dong lai giua cac buoi")
    ap.add_argument("--hard-actions", default="",
                    help="hanh dong bi ep len muc hard (bat buoc qua twin), cach nhau "
                         "bang dau phay; vi du adjust_resources. Mac dinh theo Bang 2 "
                         "de cuong")
    ap.add_argument("--twin-settle", type=int, default=None,
                    help="giay quan sat tren twin truoc va sau khi thu hanh dong, "
                         "cung la be rong cua so do tren twin (mac dinh = --settle). "
                         "Khong doi cua so watch tren production")
    ap.add_argument("--allow-code-change", action="store_true",
                    help="cho resume du code/prompt/model da doi so voi luc mo phien; "
                         "mac dinh tu choi vi hai nua phien do bang hai thuoc khac nhau")
    ap.add_argument("--dry-run", action="store_true",
                    help="khong tiem loi, khong sua gi, khong cho — nhung VAN doc "
                         "cluster va VAN goi LLM that, tuc la van ton tien API")
    args = ap.parse_args()

    if args.summary:
        return print_summary(args.summary)

    if args.repeats < 5 and not args.dry_run and args.limit is None:
        print(f"CANH BAO: --repeats {args.repeats} it hon 5.")
        print("Muc 8 KLTN.md doi toi thieu 5 lan moi kich ban de co do lech chuan.")
        print("LLM dao dong manh, duoi 5 lan thi con so khong ket luan duoc gi.")
        print("")

    runner = EvalRunner(
        run_id=args.resume,
        modes=tuple(m.strip() for m in args.modes.split(",") if m.strip()),
        scenarios=tuple(s.strip() for s in args.scenarios.split(",") if s.strip()),
        repeats=args.repeats,
        provider=args.provider,
        model=args.model,
        settle_seconds=args.settle,
        max_rounds=args.max_rounds,
        dry_run=args.dry_run,
        baseline_give_up_s=args.baseline_give_up,
        baseline_file=args.baseline_file,
        baseline_repeats=args.baseline_repeats,
        allow_code_change=args.allow_code_change,
        hard_actions=tuple(a.strip() for a in args.hard_actions.split(",") if a.strip()),
        twin_settle_seconds=args.twin_settle,
    )

    code = runner.run_all(limit=args.limit, budget_minutes=args.budget_minutes)
    if code == 0:
        print("")
        print_summary(runner.run_id)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
