#!/usr/bin/env bash
# Tu dong hoa cac buoc truoc phien chinh v2, de khong can may ca nhan mo.
# CHI CHAY TREN MAY AO LINUX:
#
#   tmux new -d -s kltn-v2 'bash scripts/v2_autostart.sh'
#   tail -f data/vm-gcp/logs/v2-autostart.log
#
# 1. Cho scripts/v2_check.py (tmux v2check) xong; khong dat 6/6 thi DUNG.
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

while tmux has-session -t v2check 2>/dev/null; do sleep 30; done
last=$(tail -1 data/vm-gcp/logs/v2-check.log)
stamp "v2_check: $last"
case "$last" in
    "6/6 dat"*) ;;
    *) stamp "v2_check KHONG dat het, DUNG. Xem data/vm-gcp/logs/v2-check.log"; exit 1 ;;
esac

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
