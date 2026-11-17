# Độ nhạy của chỉ số gây hại: từng service so với tổng thời gian chờ

Sinh bởi `scripts/rescore_wait.py`. Gốc = định nghĩa đã chốt trước khi chạy; Chờ = lỗi xét trước, rồi tổng thời gian chờ (ngưỡng 20% như gốc).

## Phiên `v2-main`

| Chế độ | Gây hại (gốc) | Gây hại (chờ) | Có ích (gốc) | Có ích (chờ) | Vô ích (gốc) | Vô ích (chờ) |
|---|---|---|---|---|---|---|
| direct | 0 | 0 | 40 | 40 | 0 | 0 |
| twin_verified | 2 | 2 | 37 | 37 | 0 | 0 |

Gây hại theo kịch bản (gốc → chờ):

- H1: direct 0 → 0; twin_verified 0 → 0
- H2: direct 0 → 0; twin_verified 0 → 0
- S1: direct 0 → 0; twin_verified 2 → 2
- S2: direct 0 → 0; twin_verified 0 → 0
- S4: direct 0 → 0; twin_verified 0 → 0
- S5: direct 0 → 0; twin_verified 0 → 0
- S6: direct 0 → 0; twin_verified 0 → 0

Các lần đổi kết luận:

- không có

