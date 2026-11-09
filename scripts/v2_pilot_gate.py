"""Cổng tự động sau lần chạy thử v2: qua thì mới được bật phiên chính.

Đọc các ca của phiên chạy thử (mặc định `v2-pilot`) và kiểm những thứ mà một lỗi
cấu hình hay một chỗ lộ đáp án sẽ làm hỏng cả 126 ca phía sau:

  - đủ số ca, ca nào cũng chấm điểm được
  - mọi prompt đã gửi LLM có mục CONFIGURATION CHANGES (v2 thật sự đang chạy)
  - không prompt nào chứa dấu vết của bộ thí nghiệm (mã lỗi, mã kịch bản, đáp án)
  - prompt v7, có lưu snapshot đầy đủ cho đánh giá lời giải thích
  - dọn dẹp sau mỗi ca sạch, không ca nào phải khôi phục cấu hình trước khi chạy

    python scripts/v2_pilot_gate.py v2-pilot --expect 2

Mã thoát 0 = qua cổng. Khác 0 = KHÔNG bật phiên chính.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1] / "data" / "eval"

# Chuoi chi bo thi nghiem biet. Xuat hien trong prompt nghia la LLM duoc nhac bai.
LEAKS = ("fault_id", "ground_truth", "correct_action", "inject", "scenario",
         "groundtruth", "F1-", "F2-", "F3-", "F4-", "F5-", "held_out", "v2-pilot",
         "runner:", "kltn.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ap.add_argument("--expect", type=int, default=2)
    args = ap.parse_args()

    problems: list[str] = []
    files = sorted((EVAL_DIR / args.session).glob("*_r*.json"))
    if len(files) < args.expect:
        problems.append(f"chi co {len(files)}/{args.expect} ca")

    n_prompts = 0
    for path in files:
        c = json.loads(path.read_text(encoding="utf-8"))
        name = path.name
        if not c.get("outcome"):
            problems.append(f"{name}: khong co outcome")
        if (c.get("code") or {}).get("prompt_version") != "v7":
            problems.append(f"{name}: prompt {c.get('code', {}).get('prompt_version')}")
        if not (c.get("cleanup") or {}).get("clean_after"):
            problems.append(f"{name}: don dep khong sach "
                            f"{(c.get('cleanup') or {}).get('problems_after')}")
        if c.get("restored_before_case"):
            problems.append(f"{name}: phai khoi phuc cau hinh truoc khi chay")
        rounds = (c.get("report") or {}).get("rounds") or []
        if not rounds:
            problems.append(f"{name}: khong co vong nao")
        for r in rounds:
            if not r.get("reasoning_ran"):
                continue
            n_prompts += 1
            text = r.get("prompt_text") or ""
            if "CONFIGURATION CHANGES" not in text:
                problems.append(f"{name} vong {r.get('round_no')}: prompt thieu "
                                f"CONFIGURATION CHANGES")
            low = text.lower()
            for leak in LEAKS:
                if leak.lower() in low:
                    problems.append(f"{name} vong {r.get('round_no')}: prompt chua "
                                    f"'{leak}'")
            if not r.get("snapshot"):
                problems.append(f"{name} vong {r.get('round_no')}: khong luu snapshot")
    if files and n_prompts == 0:
        problems.append("khong ca nao goi LLM — khong kiem duoc prompt")

    print(f"Cong chay thu {args.session}: {len(files)} ca, {n_prompts} prompt")
    for p in problems:
        print(f"  KHONG DAT: {p}")
    print("QUA CONG" if not problems else "KHONG QUA CONG")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
