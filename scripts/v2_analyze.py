"""Phân tích thí nghiệm v2 đúng như đã đăng ký trước (docs/v2-dang-ky-truoc.md mục 7).

Viết và chạy thử trên số liệu v1 TRƯỚC khi có số liệu v2, để cách phân tích không
bị chỉnh theo kết quả. Đọc thẳng từ file từng ca (không cần `index.json`), nên chạy
được cả khi phiên còn đang chạy dở — khi đó báo rõ thiếu bao nhiêu ca.

    python scripts/v2_analyze.py
    python scripts/v2_analyze.py --v2 v2-main --twin120 v2-twin120 --twin600 v2-twin600

Ghi ra data/analysis/v2_report.md (đọc được) và v2_report.json (số thô), kèm hai
hình PNG. Phiên nào chưa có thì bỏ qua và ghi chú, không ném lỗi.

Quy ước, giữ đúng như v1:
  - Chỉ số chẩn đoán chỉ tính trên ca CÓ chẩn đoán (S3 hay khỏe trước khi agent nhìn).
  - MTTR chỉ trung bình trên ca hồi phục; ca bị cắt cụt đếm riêng, không nhét vào.
  - Hành động gây hại: dùng nguyên `harmful_actions` của bộ chấm, không chấm lại.
  - Khoảng tin cậy: bootstrap 95% theo CA (lấy lại ca có hoàn lại), 10 000 lần,
    hạt giống cố định 20261109 để chạy lại ra đúng số.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics as st
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src_thesis.agent.actions import RISK_OF_ACTION  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EVAL_DIR = ROOT / "data" / "eval"
OUT_DIR = ROOT / "data" / "analysis"

OLD = ("S1", "S2", "S3", "S4", "S5", "S6")
HELD_OUT = ("H1", "H2")
DURATION_SCEN = ("S1", "S4", "S5")
EXPECTED = {"v2-main": 96, "v2-twin120": 15, "v2-twin600": 15,
            "20261027-094812": 72}
BOOT = 10_000
SEED = 20261109


# ----------------------------------------------------------------------
# DOC
# ----------------------------------------------------------------------

def load(session: str) -> list[dict]:
    """Một dòng mỗi ca: `outcome` của bộ chấm cộng vài trường của file ca."""
    d = EVAL_DIR / session
    rows = []
    for path in sorted(d.glob("*_r*.json")):
        c = json.loads(path.read_text(encoding="utf-8"))
        o = dict(c.get("outcome") or {})
        if not o:
            continue
        o["session"] = session
        o["scenario"] = c.get("scenario", o.get("scenario"))
        o["mode"] = c.get("mode", o.get("mode"))
        o["twin_settle_s"] = c.get("twin_settle_seconds") or c.get("settle_seconds") or 300
        o["prompt_version"] = (c.get("code") or {}).get("prompt_version")
        o["restored_before_case"] = bool(c.get("restored_before_case"))
        rows.append(o)
    return rows


def pick(rows, modes=None, scen=None):
    return [r for r in rows
            if (modes is None or r["mode"] in modes)
            and (scen is None or r["scenario"] in scen)]


# ----------------------------------------------------------------------
# THONG KE
# ----------------------------------------------------------------------

def mean_ci(values: list[float]) -> tuple[float, float, float] | None:
    """Trung bình kèm khoảng tin cậy bootstrap 95%."""
    if not values:
        return None
    rng = random.Random(SEED)
    n = len(values)
    means = sorted(sum(rng.choice(values) for _ in range(n)) / n for _ in range(BOOT))
    return (sum(values) / n, means[int(0.025 * BOOT)], means[int(0.975 * BOOT) - 1])


def diff_ci(a: list[float], b: list[float]) -> tuple[float, float, float] | None:
    """Hiệu trung bình a - b kèm khoảng tin cậy bootstrap 95% (lấy lại riêng hai bên)."""
    if not a or not b:
        return None
    rng = random.Random(SEED)
    na, nb = len(a), len(b)
    ds = sorted(sum(rng.choice(a) for _ in range(na)) / na
                - sum(rng.choice(b) for _ in range(nb)) / nb for _ in range(BOOT))
    return (sum(a) / na - sum(b) / nb, ds[int(0.025 * BOOT)], ds[int(0.975 * BOOT) - 1])


def frac(rows, key) -> tuple[int, int]:
    """(số ca đúng, số ca có giá trị) — bỏ ca không có chẩn đoán."""
    have = [r for r in rows if r.get(key) is not None]
    return sum(1 for r in have if r[key]), len(have)


def harmful_by_risk(rows) -> dict[str, int]:
    """Hành động gây hại chia theo mức rủi ro của Bảng 2 đề cương (không tính ép)."""
    out = {"easy": 0, "medium": 0, "hard": 0}
    for r in rows:
        for e in r.get("action_effects") or []:
            if e.get("verdict") == "harmful":
                out[RISK_OF_ACTION.get(e.get("action"), "hard")] += 1
    return out


def summarize(rows) -> dict:
    rec = [r for r in rows if r.get("recovered")]
    mttr = [r["mttr_s"] for r in rec if r.get("mttr_s") is not None]
    harm = [float(r.get("harmful_actions") or 0) for r in rows]
    took = [float(r.get("took_s") or 0) for r in rows if r["mode"] != "baseline"]
    return {
        "n": len(rows),
        "root_cause": frac(rows, "root_cause_correct"),
        "fault_type": frac(rows, "fault_type_correct"),
        "first_action": frac(rows, "action_correct"),
        "propagation": (st.mean([r["propagation_jaccard"] for r in rows
                                 if r.get("propagation_jaccard") is not None])
                        if any(r.get("propagation_jaccard") is not None for r in rows)
                        else None),
        "recovered": (len(rec), len(rows)),
        "mttr_mean": st.mean(mttr) if mttr else None,
        "mttr_sd": st.stdev(mttr) if len(mttr) > 1 else None,
        "harmful_total": int(sum(harm)),
        "harmful_per_case": mean_ci(harm),
        "harmful_by_risk": harmful_by_risk(rows),
        "wasted_total": sum(int(r.get("wasted_actions") or 0) for r in rows),
        "helpful_total": sum(int(r.get("helpful_actions") or 0) for r in rows),
        "twin_rejected": sum(int(r.get("actions_rejected_by_twin") or 0) for r in rows),
        "twin_better": sum(int(r.get("twin_better") or 0) for r in rows),
        "twin_transferred": sum(int(r.get("twin_better_transferred") or 0) for r in rows),
        "auto_undos": sum(int(r.get("auto_undos") or 0) for r in rows),
        "took_mean": st.mean(took) if took else None,
        "took_sd": st.stdev(took) if len(took) > 1 else None,
        "tokens_mean": (st.mean([(r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
                                 for r in rows]) if rows else None),
        "cost_total": round(sum(float(r.get("cost_usd") or 0) for r in rows), 4),
        "restored_before_case": sum(1 for r in rows if r.get("restored_before_case")),
    }


# ----------------------------------------------------------------------
# VIET BAO CAO
# ----------------------------------------------------------------------

def f_frac(t) -> str:
    return "--" if not t or t[1] == 0 else f"{t[0]}/{t[1]}"


def f_ci(t, nd=2) -> str:
    return "--" if t is None else f"{t[0]:.{nd}f} [{t[1]:.{nd}f}; {t[2]:.{nd}f}]"


def f_num(v, nd=0) -> str:
    return "--" if v is None else f"{v:.{nd}f}"


ROWS = [
    ("Số ca", lambda s: str(s["n"])),
    ("Đúng nguyên nhân gốc", lambda s: f_frac(s["root_cause"])),
    ("Đúng loại lỗi", lambda s: f_frac(s["fault_type"])),
    ("Hành động đầu đúng đáp án", lambda s: f_frac(s["first_action"])),
    ("Jaccard lan truyền", lambda s: f_num(s["propagation"], 3)),
    ("Ca hồi phục", lambda s: f_frac(s["recovered"])),
    ("MTTR trung bình (s), ca hồi phục", lambda s: f_num(s["mttr_mean"])),
    ("Gây hại (tổng)", lambda s: str(s["harmful_total"])),
    ("Gây hại mỗi ca [KTC 95%]", lambda s: f_ci(s["harmful_per_case"])),
    ("Gây hại thấp / TB / cao (Bảng 2)",
     lambda s: "{easy} / {medium} / {hard}".format(**s["harmful_by_risk"])),
    ("Có ích / vô ích", lambda s: f"{s['helpful_total']} / {s['wasted_total']}"),
    ("Bị twin chặn", lambda s: str(s["twin_rejected"])),
    ("Twin phán tốt lên → production tốt lên",
     lambda s: f"{s['twin_transferred']}/{s['twin_better']}"),
    ("Tự hoàn tác", lambda s: str(s["auto_undos"])),
    ("Thời gian agent mỗi ca (s)", lambda s: f_num(s["took_mean"])),
    ("Token mỗi ca", lambda s: f_num(s["tokens_mean"])),
    ("Chi phí LLM (USD, tổng)", lambda s: f"{s['cost_total']:.3f}"),
    ("Ca phải khôi phục cấu hình trước khi chạy", lambda s: str(s["restored_before_case"])),
]


def table(cols: list[tuple[str, dict]]) -> list[str]:
    out = ["| Chỉ số | " + " | ".join(n for n, _ in cols) + " |",
           "|---|" + "---|" * len(cols)]
    for label, fn in ROWS:
        out.append(f"| {label} | " + " | ".join(fn(s) for _, s in cols) + " |")
    return out


def per_scenario(rows, modes, scens) -> list[str]:
    out = ["| Kịch bản | Chế độ | n | Đúng gốc | Hành động đầu đúng | Hồi phục | Gây hại "
           "| Twin chặn |", "|---|---|---|---|---|---|---|---|"]
    for sid in scens:
        for m in modes:
            rs = pick(rows, [m], [sid])
            if not rs:
                continue
            s = summarize(rs)
            out.append(f"| {sid} | {m} | {s['n']} | {f_frac(s['root_cause'])} | "
                       f"{f_frac(s['first_action'])} | {f_frac(s['recovered'])} | "
                       f"{s['harmful_total']} | {s['twin_rejected']} |")
    return out


def completeness(name: str, rows) -> str:
    want = EXPECTED.get(name)
    if want is None:
        return f"`{name}`: {len(rows)} ca"
    flag = "" if len(rows) >= want else f" — **CHƯA ĐỦ, thiếu {want - len(rows)} ca**"
    return f"`{name}`: {len(rows)}/{want} ca{flag}"


def plot(v1, v2, dur, out_dir: Path) -> list[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    made = []
    groups = [("v1 direct", pick(v1, ["direct"], OLD)),
              ("v1 twin", pick(v1, ["twin_verified"], OLD)),
              ("v2 direct", pick(v2, ["direct"], OLD)),
              ("v2 twin", pick(v2, ["twin_verified"], OLD))]
    groups = [(n, rs) for n, rs in groups if rs]
    if groups:
        fig, ax = plt.subplots(figsize=(6, 3.6))
        for i, (n, rs) in enumerate(groups):
            m, lo, hi = mean_ci([float(r.get("harmful_actions") or 0) for r in rs])
            ax.bar(i, m, color="#4C72B0" if "direct" in n else "#DD8452")
            ax.errorbar(i, m, yerr=[[m - lo], [hi - m]], color="black", capsize=4)
            ax.text(i, hi + 0.02, f"n={len(rs)}", ha="center", fontsize=8)
        ax.set_xticks(range(len(groups)), [n for n, _ in groups])
        ax.set_ylabel("Hành động gây hại mỗi ca")
        ax.set_title("S1–S6, khoảng tin cậy bootstrap 95%")
        fig.tight_layout()
        p = out_dir / "v2_gay_hai.png"
        fig.savefig(p, dpi=160)
        plt.close(fig)
        made.append(p.name)

    pts = [(sec, rs) for sec, rs in sorted(dur.items()) if rs]
    if len(pts) >= 2:
        fig, ax1 = plt.subplots(figsize=(6, 3.6))
        xs = [sec for sec, _ in pts]
        harm = [mean_ci([float(r.get("harmful_actions") or 0) for r in rs]) for _, rs in pts]
        took = [st.mean([float(r.get("took_s") or 0) for r in rs]) / 60 for _, rs in pts]
        ax1.errorbar(xs, [h[0] for h in harm],
                     yerr=[[h[0] - h[1] for h in harm], [h[2] - h[0] for h in harm]],
                     marker="o", capsize=4, color="#C44E52", label="gây hại mỗi ca")
        ax1.set_xlabel("Thời lượng quan sát trên twin (giây)")
        ax1.set_ylabel("Gây hại mỗi ca", color="#C44E52")
        ax2 = ax1.twinx()
        ax2.plot(xs, took, marker="s", color="#55A868")
        ax2.set_ylabel("Thời gian agent mỗi ca (phút)", color="#55A868")
        ax1.set_xticks(xs)
        ax1.set_title("S1, S4, S5 — chế độ twin")
        fig.tight_layout()
        p = out_dir / "v2_thoi_luong.png"
        fig.savefig(p, dpi=160)
        plt.close(fig)
        made.append(p.name)
    return made


def main() -> int:
    ap = argparse.ArgumentParser(description="Phan tich thi nghiem v2 (dang ky truoc)")
    ap.add_argument("--v1", default="20261027-094812")
    ap.add_argument("--v2", default="v2-main")
    ap.add_argument("--twin120", default="v2-twin120")
    ap.add_argument("--twin600", default="v2-twin600")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--name", default="v2_report")
    args = ap.parse_args()

    v1, v2 = load(args.v1), load(args.v2)
    t120, t600 = load(args.twin120), load(args.twin600)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    agent = ["direct", "twin_verified"]
    L = [f"# Kết quả thí nghiệm v2 — phân tích theo đăng ký trước", "",
         "Sinh tự động bởi `scripts/v2_analyze.py`. Không sửa tay số liệu trong file này.",
         "", "## Độ đầy đủ", "",
         *[f"- {completeness(n, rs)}" for n, rs in
           ((args.v1, v1), (args.v2, v2), (args.twin120, t120), (args.twin600, t600))],
         ""]
    if v2 and any(r.get("prompt_version") != "v7" for r in v2):
        L += ["**CẢNH BÁO:** có ca trong phiên v2 không chạy bằng prompt v7.", ""]

    s_v1 = {m: summarize(pick(v1, [m], OLD)) for m in ["baseline"] + agent}
    s_v2 = {m: summarize(pick(v2, [m], OLD)) for m in ["baseline"] + agent}
    L += ["## Tổng hợp trên 6 kịch bản cũ (S1–S6)", ""]
    L += table([(f"v1 {m}", s_v1[m]) for m in ["baseline"] + agent]
               + [(f"v2 {m}", s_v2[m]) for m in ["baseline"] + agent])

    harm = lambda rs: [float(r.get("harmful_actions") or 0) for r in rs]  # noqa: E731
    took = lambda rs: [float(r.get("took_s") or 0) for r in rs]  # noqa: E731
    d1 = diff_ci(harm(pick(v2, ["twin_verified"], OLD)), harm(pick(v2, ["direct"], OLD)))
    d2 = diff_ci(took(pick(v2, ["twin_verified"], OLD)), took(pick(v2, ["direct"], OLD)))
    L += ["", "## Giả thuyết", "",
          "**G1 — twin gây hại ít hơn direct (v2, S1–S6).** Hiệu gây hại mỗi ca "
          f"(twin − direct): {f_ci(d1)}. Ủng hộ G1 khi cả khoảng nằm dưới 0; khoảng "
          "chứa 0 nghĩa là chưa đủ bằng chứng với cỡ mẫu này.", "",
          "**G2 — twin tốn thời gian hơn (v2, S1–S6).** Hiệu thời gian agent mỗi ca "
          f"(twin − direct, giây): {f_ci(d2, 0)}.", ""]
    L += ["**G3 — lịch sử thay đổi giúp chọn đúng hành động (direct, v1 → v2).**", ""]
    for sid in ("S1", "S6"):
        a, b = frac(pick(v1, ["direct"], [sid]), "action_correct"), \
            frac(pick(v2, ["direct"], [sid]), "action_correct")
        L.append(f"- {sid}: v1 {f_frac(a)} → v2 {f_frac(b)}")
    L += ["", "**G4 — kịch bản giữ lại (H1, H2), báo cáo riêng.**", ""]
    L += per_scenario(v2, ["baseline"] + agent, HELD_OUT)
    dh = diff_ci(harm(pick(v2, ["twin_verified"], HELD_OUT)),
                 harm(pick(v2, ["direct"], HELD_OUT)))
    L += ["", f"Hiệu gây hại mỗi ca trên H1+H2 (twin − direct): {f_ci(dh)}.", ""]

    dur = {120: pick(t120, ["twin_verified"], DURATION_SCEN),
           300: pick(v2, ["twin_verified"], DURATION_SCEN),
           600: pick(t600, ["twin_verified"], DURATION_SCEN)}
    L += ["**G5 — thời lượng quan sát trên twin (S1, S4, S5, chế độ twin).** Mức 300 giây "
          "lấy từ phiên chính.", ""]
    L += table([(f"{sec}s", summarize(rs)) for sec, rs in dur.items() if rs])

    L += ["", "## Từng kịch bản", "", "### v1", ""]
    L += per_scenario(v1, ["baseline"] + agent, OLD)
    L += ["", "### v2", ""]
    L += per_scenario(v2, ["baseline"] + agent, OLD + HELD_OUT)

    figs = plot(v1, v2, dur, out_dir)
    if figs:
        L += ["", "## Hình", "", *[f"- `{f}`" for f in figs]]

    md = out_dir / f"{args.name}.md"
    md.write_text("\n".join(L) + "\n", encoding="utf-8")
    raw = {"v1": s_v1, "v2": s_v2, "G1_diff_harm": d1, "G2_diff_took": d2,
           "G4_diff_harm_heldout": dh,
           "G5": {str(k): summarize(v) for k, v in dur.items() if v},
           "sessions": {n: len(rs) for n, rs in ((args.v1, v1), (args.v2, v2),
                                                 (args.twin120, t120),
                                                 (args.twin600, t600))}}
    (out_dir / f"{args.name}.json").write_text(
        json.dumps(raw, indent=2, ensure_ascii=False, default=list), encoding="utf-8")
    print(f"Da ghi {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
