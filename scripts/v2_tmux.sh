#!/usr/bin/env bash
# Bat, xem, dung thi nghiem v2 trong tmux. CHI CHAY TREN MAY AO LINUX.
#
#   bash scripts/v2_tmux.sh start    bat (bo qua neu dang chay)
#   bash scripts/v2_tmux.sh status   dang o buoc nao, moi phien bao nhieu ca
#   bash scripts/v2_tmux.sh stop     dung (Ctrl+C de bo chay tu don ca dang do)
#   bash scripts/v2_tmux.sh boot     cai tu bat lai khi may ao khoi dong lai (crontab)
#   bash scripts/v2_tmux.sh unboot   go tu bat lai (khi thi nghiem da xong)
#
# Chay lai `start` sau khi dung, sau khi may ao khoi dong lai, hay sau khi mot buoc
# hong deu an toan: ca da xong duoc giu, cong chay thu da qua thi khong chay lai.

set -u
cd "$(dirname "$0")/.."
S=kltn-v2

case "${1:-status}" in
start)
    if tmux has-session -t "$S" 2>/dev/null; then
        echo "tmux '$S' dang chay roi. Xem: bash scripts/v2_tmux.sh status"
        exit 0
    fi
    tmux new -d -s "$S" "bash scripts/v2_autostart.sh"
    echo "Da bat tmux '$S'. Xem: bash scripts/v2_tmux.sh status"
    ;;
status)
    tmux has-session -t "$S" 2>/dev/null && echo "tmux '$S': DANG CHAY" \
        || echo "tmux '$S': KHONG CHAY"
    [ -f data/eval/.v2-pilot-passed ] && echo "cong chay thu: $(cat data/eval/.v2-pilot-passed)"
    for s in v2-pilot v2-main v2-twin120 v2-twin600; do
        echo "  $s: $(ls data/eval/$s/*_r*.json 2>/dev/null | wc -l) ca"
    done
    echo "--- autostart"
    grep '^===' data/vm-gcp/logs/v2-autostart.log 2>/dev/null | tail -4
    echo "--- pipeline"
    grep '^===' data/vm-gcp/logs/v2-pipeline.log 2>/dev/null | tail -4
    tail -3 data/vm-gcp/logs/v2-pipeline.log 2>/dev/null
    ;;
stop)
    tmux send-keys -t "$S" C-c 2>/dev/null
    echo "Da gui Ctrl+C. Bo chay don ca dang do roi thoat; cho 1-3 phut roi xem status."
    ;;
boot)
    # May ao khoi dong lai thi tmux mat. Cho 5 phut de cum kind len het roi bat lai;
    # bo chay tu don loi con tiem va twin con song o luot dau (ensure_clean_slate
    # bao loi -> pipeline don roi thu lai).
    line="@reboot sleep 300 && cd $PWD && PATH=/usr/local/bin:/usr/bin:/bin bash scripts/v2_tmux.sh start >> data/vm-gcp/logs/v2-boot.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v 'scripts/v2_tmux.sh start'; echo "$line" ) | crontab -
    echo "Da cai tu bat lai khi khoi dong:"; crontab -l | grep v2_tmux
    ;;
unboot)
    crontab -l 2>/dev/null | grep -v 'scripts/v2_tmux.sh start' | crontab -
    echo "Da go tu bat lai."
    ;;
*)
    echo "Dung: bash scripts/v2_tmux.sh start|status|stop|boot|unboot"
    exit 2
    ;;
esac
