#!/usr/bin/env bash
# Chuong trinh GIA thay cho .venv/bin/python, de thu luong dieu khien cua
# scripts/v2_autostart.sh va scripts/v2_pipeline.sh ma khong dung toi cluster.
# Chay tu tests/test_v2_shell.sh, khong dung truc tiep.
#
#   FAIL_SESSIONS="v2-main"   cac phien khong bao gio tao duoc ca nao
#   H1_OK=0|1                 ket qua gia cua v2_check --only h1
#   SLOW=1                    eval_run ngu lau de thu Ctrl+C
echo "FAKE $*" >>"$FAKE_CALLS"
[ "$1" = "-u" ] && shift
script=$1; shift
case "$script" in
scripts/v2_check.py) [ "${H1_OK:-1}" = 1 ] ;;
scripts/v2_pilot_gate.py) exit 0 ;;
scripts/inject.py|scripts/twin.py) exit 0 ;;
scripts/eval_run.py)
    id=""; scen=""; reps=1; modes=""
    while [ $# -gt 0 ]; do
        case "$1" in
        --resume) id=$2; shift ;;
        --scenarios) scen=$2; shift ;;
        --repeats) reps=$2; shift ;;
        --modes) modes=$2; shift ;;
        esac
        shift
    done
    echo "SCEN $id $scen" >>"$FAKE_CALLS"
    if [ "${SLOW:-0}" = 1 ]; then
        trap 'echo "FAKE eval_run nhan Ctrl+C, don dep" >>"$FAKE_CALLS"; exit 130' INT
        sleep 30 & wait $!
    fi
    case " ${FAIL_SESSIONS:-} " in *" $id "*) exit 1 ;; esac
    mkdir -p "data/eval/$id"
    for s in ${scen//,/ }; do
        for m in ${modes//,/ }; do
            for r in $(seq 1 "$reps"); do
                [ "$m" = baseline ] && [ "$r" -gt 2 ] && continue
                echo '{}' >"data/eval/$id/${s}_${m}_r${r}.json"
            done
        done
    done
    exit 0 ;;
*) echo "FAKE: khong biet $script" >>"$FAKE_CALLS"; exit 99 ;;
esac
