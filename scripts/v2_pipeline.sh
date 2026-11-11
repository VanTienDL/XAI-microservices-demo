#!/usr/bin/env bash
# Chay tron thi nghiem v2 khong nguoi trong (docs/v2-dang-ky-truoc.md muc 6).
# CHI CHAY TREN MAY AO LINUX. Bat bang scripts/v2_tmux.sh, khong goi truc tiep.
#
# Ba phien, chay lan luot, DOC LAP voi nhau:
#   v2-main      kich ban S1-S6, H1, H2 x (direct 5 + twin 5 + baseline 2), twin 300s
#   v2-twin120   S1,S4,S5 x twin 5 = 15 ca, quan sat tren twin 120s
#   v2-twin600   S1,S4,S5 x twin 5 = 15 ca, quan sat tren twin 600s
# Muc 300s cua thi nghiem thoi luong lay tu chinh v2-main (cung code, cung cau hinh).
#
# CHIU LOI:
#   - Mot phien chua du ca thi thu lai toi da MAX_PASSES luot (ca da xong giu nguyen,
#     chi chay ca con thieu). Giua cac luot don loi con tiem va twin con song.
#   - Mot phien van thieu sau MAX_PASSES luot thi GHI LAI VA CHAY TIEP phien sau,
#     khong dung ca pipeline. Het ba phien thi quay lai cac phien con thieu them
#     mot vong nua.
#   - Chay lai script nay bat ky luc nao la chay tiep dung cho dang do.
#   - Khoa (flock) chan hai ban pipeline chay chong len nhau tren cung mot cluster.
#
# Bien moi truong:
#   SKIP_H1=1   bo H1 khoi phien chinh (autostart dat khi kiem tra H1 khong dat)
#   PY, LOG     de thu script bang chuong trinh gia (tests), binh thuong khong dat

set -u
cd "$(dirname "$0")/.."

PY=${PY:-.venv/bin/python}
LOG=${LOG:-data/vm-gcp/logs/v2-pipeline.log}
HARD=adjust_resources,scale_up,scale_down
MAX_PASSES=${MAX_PASSES:-4}
PAUSE_S=${PAUSE_S:-120}
mkdir -p "$(dirname "$LOG")" data/eval
exec >>"$LOG" 2>&1

exec 9>data/eval/.v2-pipeline.lock
if ! flock -n 9; then
    echo "=== $(date -u '+%F %T') UTC: da co mot pipeline khac dang chay, thoat"
    exit 0
fi

stamp() { echo "=== $(date -u '+%F %T') UTC: $*"; }

# Ctrl+C (scripts/v2_tmux.sh stop): Python bat tin hieu, don ca dang do roi thoat ma
# 130. Bash coi do la "con tu xu ly" va CHAY TIEP vong lap neu khong bat o day.
STOP=0
trap 'STOP=1' INT TERM
stop_if_asked() {
    if [ "$STOP" = 1 ] || [ "${1:-0}" -eq 130 ]; then
        stamp "dung theo yeu cau (Ctrl+C)"
        exit 130
    fi
}

n_cases() {
    ls "data/eval/$1"/*_r*.json 2>/dev/null | wc -l
}

if [ "${SKIP_H1:-0}" = "1" ]; then
    MAIN_SCEN=S1,S2,S3,S4,S5,S6,H2
    MAIN_WANT=84
else
    MAIN_SCEN=S1,S2,S3,S4,S5,S6,H1,H2
    MAIN_WANT=96
fi

# id|so ca can|tham so rieng
SESSIONS=(
    "v2-main|$MAIN_WANT|--scenarios $MAIN_SCEN --modes baseline,direct,twin_verified --repeats 5 --baseline-repeats 2"
    "v2-twin120|15|--scenarios S1,S4,S5 --modes twin_verified --repeats 5 --twin-settle 120"
    "v2-twin600|15|--scenarios S1,S4,S5 --modes twin_verified --repeats 5 --twin-settle 600"
)

cleanup_between() {
    $PY scripts/inject.py --revert || true
    $PY scripts/twin.py --destroy || true
    sleep "$PAUSE_S"
}

# Tra 0 neu phien du ca, 1 neu van thieu sau MAX_PASSES luot.
run_session() {
    local id=$1 want=$2 extra=$3 pass=0 have
    while :; do
        have=$(n_cases "$id")
        if [ "$have" -ge "$want" ]; then
            stamp "phien $id du $have/$want ca"
            return 0
        fi
        pass=$((pass + 1))
        if [ "$pass" -gt "$MAX_PASSES" ]; then
            stamp "phien $id van thieu ($have/$want) sau $MAX_PASSES luot, BO QUA, chay phien sau"
            return 1
        fi
        stamp "phien $id luot $pass (dang co $have/$want ca)"
        # shellcheck disable=SC2086
        $PY -u scripts/eval_run.py --resume "$id" --hard-actions "$HARD" $extra
        local code=$?
        stamp "phien $id luot $pass ket thuc, ma thoat $code"
        stop_if_asked "$code"
        cleanup_between
        stop_if_asked
    done
}

stamp "BAT DAU pipeline v2, git $(git rev-parse --short HEAD 2>/dev/null), SKIP_H1=${SKIP_H1:-0}"

for round in 1 2; do
    incomplete=()
    for s in "${SESSIONS[@]}"; do
        IFS='|' read -r id want extra <<<"$s"
        run_session "$id" "$want" "$extra" || incomplete+=("$id")
    done
    if [ ${#incomplete[@]} -eq 0 ]; then
        stamp "XONG pipeline v2: ca ba phien du ca"
        exit 0
    fi
    stamp "vong $round xong, con thieu: ${incomplete[*]}"
done

stamp "KET THUC pipeline v2, van thieu: ${incomplete[*]} — xem log tung phien"
exit 1
