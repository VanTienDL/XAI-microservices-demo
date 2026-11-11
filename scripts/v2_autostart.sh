#!/usr/bin/env bash
# Cac buoc truoc phien chinh v2, de khong can may ca nhan mo.
# CHI CHAY TREN MAY AO LINUX. Bat bang scripts/v2_tmux.sh.
#
# 1. Kiem kich ban H1 tren cluster that (scripts/v2_check.py --only h1). Lan
#    2026-11-09 da dat history, rollback, h2, twin tren dung code nay; chi H1 doi.
#    KHONG DAT -> van chay tiep, nhung bo H1 khoi phien chinh (SKIP_H1=1): mot kich
#    ban agent khong nhin thay thi chay cung vo ich, con cac phan khac van dung.
# 2. Chay thu 2 ca S2 (phien v2-pilot, KHONG tinh vao ket qua).
# 3. scripts/v2_pilot_gate.py kiem 2 ca do. KHONG QUA -> DUNG. Day la cho duy nhat
#    dung han: cong truot nghia la LLM khong goi duoc, prompt lo dap an, hoac don
#    dep hong — moi ca phia sau deu la so lieu hong, chay tiep chi ton tien.
# 4. Chuyen sang scripts/v2_pipeline.sh (tu chiu loi tung phien).
#
# Da qua cong roi thi chay lai script nay se bo qua buoc 1-3 va vao thang pipeline.

set -u
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
LOG=${LOG:-data/vm-gcp/logs/v2-autostart.log}
HARD=adjust_resources,scale_up,scale_down
PASSED=data/eval/.v2-pilot-passed
mkdir -p "$(dirname "$LOG")" data/eval
exec >>"$LOG" 2>&1

stamp() { echo "=== $(date -u '+%F %T') UTC: $*"; }

STOP=0
trap 'STOP=1' INT TERM
stop_if_asked() {
    if [ "$STOP" = 1 ] || [ "${1:-0}" -eq 130 ]; then
        stamp "dung theo yeu cau (Ctrl+C)"
        exit 130
    fi
}

stamp "BAT DAU autostart, git $(git rev-parse --short HEAD 2>/dev/null)"

if [ -f "$PASSED" ]; then
    stamp "da qua cong truoc do ($(cat "$PASSED")), vao thang pipeline"
    export SKIP_H1=$(grep -o 'SKIP_H1=[01]' "$PASSED" | cut -d= -f2)
    exec bash scripts/v2_pipeline.sh
fi

SKIP_H1=0
stamp "kiem H1: v2_check --only h1"
$PY -u scripts/v2_check.py --only h1
code=$?
stop_if_asked "$code"
if [ "$code" -eq 0 ]; then
    stamp "H1 dat"
else
    SKIP_H1=1
    stamp "H1 KHONG dat -> phien chinh se BO H1 (ghi vao muc 9 dang ky truoc khi phan tich)"
fi

stamp "chay thu v2-pilot: S2 x (direct, twin) x 1"
$PY -u scripts/eval_run.py --resume v2-pilot --scenarios S2 \
    --modes direct,twin_verified --repeats 1 --hard-actions "$HARD"
code=$?
stamp "chay thu ket thuc, ma thoat $code"
stop_if_asked "$code"

if ! $PY scripts/v2_pilot_gate.py v2-pilot --expect 2; then
    stamp "KHONG QUA CONG chay thu, DUNG. Phien chinh chua bat."
    exit 1
fi

echo "qua cong $(date -u '+%F %T') SKIP_H1=$SKIP_H1" >"$PASSED"
stamp "qua cong, vao pipeline (log rieng: data/vm-gcp/logs/v2-pipeline.log)"
export SKIP_H1
exec bash scripts/v2_pipeline.sh
