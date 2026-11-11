#!/usr/bin/env bash
# Tu dong hoa cac buoc truoc phien chinh v2, de khong can may ca nhan mo.
# CHI CHAY TREN MAY AO LINUX:
#
#   tmux new -d -s kltn-v2 'bash scripts/v2_autostart.sh'
#   tail -f data/vm-gcp/logs/v2-autostart.log
#
# 1. Chay scripts/v2_check.py voi cac phep thu trong CHECKS (mac dinh: h1); khong
#    dat het thi DUNG. Lan 2026-11-09 da dat history, rollback, h2, twin tren dung
#    code nay; chi kich ban H1 thay doi nen chi kiem lai H1.
# 2. Chay thu 2 ca S2 (phien v2-pilot, KHONG tinh vao ket qua).
# 3. scripts/v2_pilot_gate.py kiem 2 ca do; khong qua cong thi DUNG.
# 4. Bat scripts/v2_pipeline.sh trong chinh phien tmux nay.

set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
LOG=data/vm-gcp/logs/v2-autostart.log
HARD=adjust_resources,scale_up,scale_down
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

stamp() { echo "=== $(date -u '+%F %T') UTC: $*"; }

stamp "BAT DAU autostart, git $(git rev-parse --short HEAD)"

CHECKS=${CHECKS:-h1}
stamp "v2_check --only $CHECKS"
if ! $PY -u scripts/v2_check.py --only "$CHECKS"; then
    stamp "v2_check KHONG dat het, DUNG. Phien chinh chua bat."
    exit 1
fi
stamp "v2_check dat"

stamp "chay thu v2-pilot: S2 x (direct, twin) x 1"
$PY -u scripts/eval_run.py --resume v2-pilot --scenarios S2 \
    --modes direct,twin_verified --repeats 1 --hard-actions "$HARD"
stamp "chay thu ket thuc, ma thoat $?"

if ! $PY scripts/v2_pilot_gate.py v2-pilot --expect 2; then
    stamp "KHONG QUA CONG chay thu, DUNG. Phien chinh chua bat."
    exit 1
fi

stamp "qua cong, bat pipeline (log rieng: data/vm-gcp/logs/v2-pipeline.log)"
exec bash scripts/v2_pipeline.sh
