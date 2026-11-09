"""Phân tích độ nhạy của chỉ số 4: chấm lại "hành động gây hại" theo TỔNG THỜI GIAN CHỜ.

Báo cáo tiến độ lần 2 (mục 6, mục 7) đã hứa việc này: định nghĩa gốc chấm "gây hại"
khi BẤT KỲ service nào chậm đi quá 20%, kể cả một service trả lời trong 1 ms. Một ca
TH5 bị chấm gây hại trong khi frontend nhanh lên 69%. Câu hỏi: kết luận của báo cáo
có đổi không nếu dùng định nghĩa ít nhạy với nhiễu hơn?

Định nghĩa thay thế — lấy nguyên cách của Nguyễn Đức Thọ trong `verifier.py` (nhánh
của Thọ, commit fd9b761b và trước đó), KHÔNG thêm hằng số mới:
  1. Lỗi xét trước, giữ nguyên định nghĩa gốc: service đủ lưu lượng có tỉ lệ lỗi
     tăng từ 2 điểm phần trăm -> gây hại; giảm từ 2 điểm -> có ích.
  2. Nếu lỗi không đổi: tổng thời gian chờ = tổng (p95 × lưu lượng trung bình) trên
     các service đủ lưu lượng. Tăng từ 20% tổng lúc trước -> gây hại, giảm từ 20% ->
     có ích, còn lại -> vô ích. Vẫn là ngưỡng 20% của định nghĩa gốc, chỉ áp lên
     TỔNG thay vì lên từng service.

Chỉ đọc file ca, không đụng cluster. Số đo trước/sau lấy đúng cặp mà bộ chấm gốc
dùng (`watch` nếu có), nên khác biệt chỉ đến từ định nghĩa.

    python scripts/rescore_wait.py 20261027-094812 20261029-030608
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.eval import metrics as M  # noqa: E402

EVAL_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"
OUT_DIR = Path(__file__).resolve().parents[1] / "data" / "analysis"


def wait_verdict(before: dict, after: dict, applied: bool) -> tuple[str, str]:
    if not applied:
        return "not_applied", ""
    voting = []
    for svc in M.EFFECT_SERVICES:
        b, a = before.get(svc), after.get(svc)
        if not b or not a:
            continue
        if min(b.get("request_rate", 0.0), a.get("request_rate", 0.0)) < M.MIN_RATE_FOR_EFFECT:
            continue
        voting.append((svc, b, a))
    if not voting:
        return "unknown", "khong service nao du luu luong"

    worse = [s for s, b, a in voting
             if a.get("error_rate", 0) - b.get("error_rate", 0) >= M.HARMFUL_ERROR_DELTA]
    if worse:
        return "harmful", f"loi tang o {', '.join(worse)}"
    better = [s for s, b, a in voting
              if b.get("error_rate", 0) - a.get("error_rate", 0) >= M.HARMFUL_ERROR_DELTA]
    if better:
        return "helpful", f"loi giam o {', '.join(better)}"

    delta = base = 0.0
    for _, b, a in voting:
        rate = (b.get("request_rate", 0.0) + a.get("request_rate", 0.0)) / 2
        delta += (a.get("p95_ms", 0.0) - b.get("p95_ms", 0.0)) * rate
        base += b.get("p95_ms", 0.0) * rate
    note = f"tong thoi gian cho {delta:+.0f} tren nen {base:.0f} ms/s"
    if base > 0 and delta >= base * M.HARMFUL_P95_RATIO:
        return "harmful", note
    if base > 0 and delta <= -base * M.HARMFUL_P95_RATIO:
        return "helpful", note
    return "neutral", note


def action_pairs(case: dict):
    """Cặp (hành động, trước, sau) đúng như `EvalRunner._score` chọn."""
    rounds = (case.get("report") or {}).get("rounds") or []
    final_red = case.get("final_red") or {}
    for i, r in enumerate(rounds):
        ar = r.get("action_result")
        if not ar or ar.get("action") == "no_action":
            continue
        watch = r.get("watch") or {}
        if watch.get("before") is not None and watch.get("after") is not None:
            before, after = watch["before"], watch["after"]
        else:
            before = r.get("red") or {}
            after = rounds[i + 1].get("red") if i + 1 < len(rounds) else final_red
        yield ar, before, after or {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Cham lai chi so gay hai theo tong thoi gian cho")
    ap.add_argument("sessions", nargs="+")
    args = ap.parse_args()

    report: dict = {}
    lines = ["# Độ nhạy của chỉ số gây hại: từng service so với tổng thời gian chờ", "",
             "Sinh bởi `scripts/rescore_wait.py`. Gốc = định nghĩa đã chốt trước khi "
             "chạy; Chờ = lỗi xét trước, rồi tổng thời gian chờ (ngưỡng 20% như gốc).", ""]
    for session in args.sessions:
        totals = defaultdict(Counter)
        flips = Counter()
        mismatch = 0
        per_scen = defaultdict(lambda: defaultdict(Counter))
        for path in sorted((EVAL_DIR / session).glob("*_r*.json")):
            case = json.loads(path.read_text(encoding="utf-8"))
            mode, sid = case.get("mode"), case.get("scenario")
            if mode == "baseline":
                continue
            stored = [e for e in (case.get("outcome") or {}).get("action_effects") or []
                      if e.get("action") != "no_action"]
            for k, (ar, before, after) in enumerate(action_pairs(case)):
                orig = M.classify_action_effect(ar.get("action", ""), ar.get("target", ""),
                                                before, after,
                                                applied=bool(ar.get("ok"))).verdict
                if k < len(stored) and stored[k].get("verdict") != orig:
                    mismatch += 1
                alt, _ = wait_verdict(before, after, bool(ar.get("ok")))
                totals[mode]["goc_" + orig] += 1
                totals[mode]["cho_" + alt] += 1
                per_scen[sid][mode]["goc_harmful"] += orig == "harmful"
                per_scen[sid][mode]["cho_harmful"] += alt == "harmful"
                if orig != alt:
                    flips[f"{mode}: {orig} -> {alt}"] += 1

        lines += [f"## Phiên `{session}`", ""]
        if mismatch:
            lines += [f"**CẢNH BÁO:** {mismatch} hành động chấm lại theo định nghĩa gốc "
                      f"không khớp kết quả đã lưu — kiểm tra trước khi dùng số dưới đây.", ""]
        lines += ["| Chế độ | Gây hại (gốc) | Gây hại (chờ) | Có ích (gốc) | Có ích (chờ) "
                  "| Vô ích (gốc) | Vô ích (chờ) |", "|---|---|---|---|---|---|---|"]
        for mode, c in sorted(totals.items()):
            lines.append(
                f"| {mode} | {c['goc_harmful']} | {c['cho_harmful']} | {c['goc_helpful']} "
                f"| {c['cho_helpful']} | {c['goc_neutral'] + c['goc_not_applied']} "
                f"| {c['cho_neutral'] + c['cho_not_applied']} |")
        lines += ["", "Gây hại theo kịch bản (gốc → chờ):", ""]
        for sid in sorted(per_scen):
            parts = [f"{m} {c['goc_harmful']} → {c['cho_harmful']}"
                     for m, c in sorted(per_scen[sid].items())]
            lines.append(f"- {sid}: " + "; ".join(parts))
        lines += ["", "Các lần đổi kết luận:", ""]
        lines += [f"- {k}: {v}" for k, v in sorted(flips.items())] or ["- không có"]
        lines.append("")
        report[session] = {"totals": {m: dict(c) for m, c in totals.items()},
                           "flips": dict(flips), "mismatch_with_stored": mismatch}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md = OUT_DIR / "do_nhay_gay_hai.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT_DIR / "do_nhay_gay_hai.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\n".join(lines))
    print(f"Da ghi {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
