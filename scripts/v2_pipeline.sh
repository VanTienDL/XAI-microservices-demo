#!/usr/bin/env bash
# Chay tron thi nghiem v2 khong nguoi trong (docs/v2-dang-ky-truoc.md muc 6).
# CHI CHAY TREN MAY AO LINUX, trong tmux:
#
#   tmux new -d -s kltn-v2 'bash scripts/v2_pipeline.sh'
#   tail -f data/vm-gcp/logs/v2-pipeline.log        # xem tien do
#
# Ba phien, chay lan luot:
#   v2-main      8 kich ban x (direct 5 + twin 5 + baseline 2) = 96 ca, twin 300s
#   v2-twin120   S1,S4,S5 x twin 5 = 15 ca, quan sat tren twin 120s
#   v2-twin600   S1,S4,S5 x twin 5 = 15 ca, quan sat tren twin 600s
# Muc 300s cua thi nghiem thoi luong lay tu chinh v2-main (cung code, cung cau hinh).
#
# Chiu duoc gian doan: moi phien dung ma phien co dinh nen chay lai script nay la
# chay tiep dung cho dang do (ca da xong khong chay lai). Mot phien chua du ca thi
# thu lai toi da MAX_PASSES luot; giua cac luot co don dep.

set -u
cd "$(dirname "$0")/.."

PY=.venv/bin/python
LOG=data/vm-gcp/logs/v2-pipeline.log
HARD=adjust_resources,scale_up,scale_down
MAX_PASSES=4
mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1

stamp() { echo "=== $(date -u '+%F %T') UTC: $*"; }

n_cases() {
    ls "data/eval/$1"/*_r*.json 2>/dev/null | wc -l
}

run_session() {
    local id=$1 want=$2
    shift 2
    local pass=0
    while :; do
        local have
        have=$(n_cases "$id")
        if [ "$have" -ge "$want" ]; then
            stamp "phien $id du $have/$want ca"
            return 0
        fi
        pass=$((pass + 1))
        if [ "$pass" -gt "$MAX_PASSES" ]; then
            stamp "phien $id van thieu ($have/$want) sau $MAX_PASSES luot, DUNG PIPELINE"
            return 1
        fi
        stamp "phien $id luot $pass (dang co $have/$want ca)"
        $PY -u scripts/eval_run.py --resume "$id" --hard-actions "$HARD" "$@"
        stamp "phien $id luot $pass ket thuc, ma thoat $?"
        # Don truoc luot sau: loi con tiem do mot ca chet giua chung, twin con song.
        $PY scripts/inject.py --revert || true
        $PY scripts/twin.py --destroy || true
        sleep 120
    done
}

stamp "BAT DAU pipeline v2, git $(git rev-parse --short HEAD)"

run_session v2-main 96 \
    --scenarios S1,S2,S3,S4,S5,S6,H1,H2 \
    --modes baseline,direct,twin_verified \
    --repeats 5 --baseline-repeats 2 || exit 1

run_session v2-twin120 15 \
    --scenarios S1,S4,S5 --modes twin_verified --repeats 5 \
    --twin-settle 120 || exit 1

run_session v2-twin600 15 \
    --scenarios S1,S4,S5 --modes twin_verified --repeats 5 \
    --twin-settle 600 || exit 1

stamp "XONG pipeline v2"
