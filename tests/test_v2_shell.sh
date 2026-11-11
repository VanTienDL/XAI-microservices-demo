#!/usr/bin/env bash
# Thu luong dieu khien cua autostart + pipeline bang chuong trinh gia (tests/fake_py.sh).
# CHI CHAY TREN LINUX (can flock, tmux). Khong dung toi cluster, khong goi LLM.
#
#   bash tests/test_v2_shell.sh
#
# Ba tinh huong:
#   a. H1 kiem khong dat -> pipeline van chay, bo H1 (84 ca phien chinh)
#   b. v2-main hong mai -> v2-twin120, v2-twin600 van chay; v2-main duoc thu them
#      mot vong o cuoi; pipeline thoat ma khac 0 va ghi ro phien thieu
#   c. Ctrl+C trong tmux -> dung that, khong chay tiep vong lap

set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
FAILS=0
ok() { echo "DAT  $1"; }
bad() { echo "SAI  $1"; FAILS=$((FAILS + 1)); }

setup() {
    T=$(mktemp -d)
    mkdir -p "$T/scripts" "$T/data/eval"
    cp "$REPO"/scripts/v2_autostart.sh "$REPO"/scripts/v2_pipeline.sh "$REPO"/scripts/v2_tmux.sh "$T/scripts/"
    export PY="$REPO/tests/fake_py.sh" FAKE_CALLS="$T/calls.log" PAUSE_S=0 MAX_PASSES=2
    export LOG="$T/run.log"
}

# --- a ---
setup
H1_OK=0 FAIL_SESSIONS="" bash "$T/scripts/v2_autostart.sh"; code=$?
main=$(ls "$T/data/eval/v2-main" | wc -l)
if [ "$code" = 0 ] && [ "$main" = 84 ] && ! grep -q 'SCEN v2-main .*H1' "$FAKE_CALLS" \
   && grep -q 'SKIP_H1=1' "$T/data/eval/.v2-pilot-passed"; then
    ok "a. H1 hong -> bo H1, pipeline chay du ($main ca)"
else
    bad "a. code=$code main=$main"; cat "$LOG"
fi
# chay lai: da qua cong -> khong chay lai kiem tra va chay thu
before=$(grep -c 'v2_check' "$FAKE_CALLS")
bash "$T/scripts/v2_autostart.sh"
after=$(grep -c 'v2_check' "$FAKE_CALLS")
[ "$before" = "$after" ] && ok "a2. chay lai sau khi qua cong -> vao thang pipeline" \
    || bad "a2. chay lai van kiem H1 lan nua"
rm -rf "$T"

# --- b ---
setup
H1_OK=1 FAIL_SESSIONS="v2-main" bash "$T/scripts/v2_autostart.sh"; code=$?
t120=$(ls "$T/data/eval/v2-twin120" 2>/dev/null | wc -l)
t600=$(ls "$T/data/eval/v2-twin600" 2>/dev/null | wc -l)
tries=$(grep -c 'SCEN v2-main' "$FAKE_CALLS")
if [ "$code" != 0 ] && [ "$t120" = 15 ] && [ "$t600" = 15 ] && [ "$tries" = 4 ] \
   && grep -q 'van thieu: v2-main' "$LOG"; then
    ok "b. v2-main hong -> hai phien sau van du 15/15, v2-main thu $tries luot qua 2 vong"
else
    bad "b. code=$code t120=$t120 t600=$t600 tries=$tries"; tail -20 "$LOG"
fi
rm -rf "$T"

# --- c ---
setup
touch "$T/data/eval/.v2-pilot-passed"; echo "SKIP_H1=0" >"$T/data/eval/.v2-pilot-passed"
S=v2test-$$
tmux new -d -s "$S" "cd $T && PY=$PY FAKE_CALLS=$FAKE_CALLS PAUSE_S=0 MAX_PASSES=2 LOG=$LOG SLOW=1 bash scripts/v2_autostart.sh"
sleep 3
tmux send-keys -t "$S" C-c
sleep 3
alive=$(tmux has-session -t "$S" 2>/dev/null && echo yes || echo no)
runs=$(grep -c 'SCEN ' "$FAKE_CALLS")
if [ "$alive" = no ] && [ "$runs" = 1 ] && grep -q 'dung theo yeu cau' "$LOG" \
   && grep -q 'nhan Ctrl+C' "$FAKE_CALLS"; then
    ok "c. Ctrl+C -> eval_run don dep, pipeline dung that (chi 1 lan goi)"
else
    bad "c. con song=$alive so lan goi=$runs"; tail -10 "$LOG"
    tmux kill-session -t "$S" 2>/dev/null
fi
rm -rf "$T"

echo ""
[ "$FAILS" = 0 ] && echo "3/3 tinh huong dat" || echo "$FAILS tinh huong SAI"
exit "$FAILS"
