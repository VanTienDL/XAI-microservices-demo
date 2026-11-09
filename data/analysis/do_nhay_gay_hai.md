# Độ nhạy của chỉ số gây hại: từng service so với tổng thời gian chờ

Sinh bởi `scripts/rescore_wait.py`. Gốc = định nghĩa đã chốt trước khi chạy; Chờ = lỗi xét trước, rồi tổng thời gian chờ (ngưỡng 20% như gốc).

## Phiên `20261027-094812`

| Chế độ | Gây hại (gốc) | Gây hại (chờ) | Có ích (gốc) | Có ích (chờ) | Vô ích (gốc) | Vô ích (chờ) |
|---|---|---|---|---|---|---|
| direct | 9 | 8 | 36 | 36 | 16 | 17 |
| twin_verified | 10 | 9 | 30 | 31 | 17 | 17 |

Gây hại theo kịch bản (gốc → chờ):

- S1: direct 8 → 8; twin_verified 6 → 6
- S2: direct 0 → 0; twin_verified 0 → 0
- S4: direct 0 → 0; twin_verified 0 → 0
- S5: direct 1 → 0; twin_verified 1 → 0
- S6: direct 0 → 0; twin_verified 3 → 3

Các lần đổi kết luận:

- direct: harmful -> helpful: 1
- direct: helpful -> neutral: 1
- twin_verified: harmful -> helpful: 1

## Phiên `20261029-030608`

| Chế độ | Gây hại (gốc) | Gây hại (chờ) | Có ích (gốc) | Có ích (chờ) | Vô ích (gốc) | Vô ích (chờ) |
|---|---|---|---|---|---|---|
| twin_verified | 4 | 1 | 11 | 11 | 9 | 12 |

Gây hại theo kịch bản (gốc → chờ):

- S1: twin_verified 1 → 1
- S5: twin_verified 3 → 0
- S6: twin_verified 0 → 0

Các lần đổi kết luận:

- twin_verified: harmful -> neutral: 3

