# XAI + ReAct Agent + Digital Twin cho Microservices

Repo khóa luận. Fork từ [GoogleCloudPlatform/microservices-demo](https://github.com/GoogleCloudPlatform/microservices-demo) (Online Boutique) — hệ thống 11 microservice dùng làm đối tượng nghiên cứu.

## Đọc theo thứ tự

1. [KLTN.md](KLTN.md) — bản giao việc: đề tài, ràng buộc, những thứ không làm, kiến trúc cần xây.
2. [KLTN-PLAN.md](KLTN-PLAN.md) — kế hoạch chia 7 phase, mỗi phase có bước nhỏ và tiêu chí thành công.
3. [docs/van-hanh.md](docs/van-hanh.md) — cách tắt project để trả RAM và cách mở lại. Đọc trước khi nghỉ tay.
4. [docs/thesis-notes.md](docs/thesis-notes.md) — sổ ghi số liệu nền và những phát hiện trong lúc làm.

## Kết quả và số liệu

- [docs/v2-dang-ky-truoc.md](docs/v2-dang-ky-truoc.md) — giả thuyết, chỉ số, đáp án của thí nghiệm v2, chốt trước khi chạy.
- `data/eval/<mã-phiên>/` — mỗi ca một file JSON đủ để chấm lại: telemetry, lời giải thích, hành động, kết quả twin, dọn dẹp.
- `data/analysis/` — bảng tổng hợp và phân tích độ nhạy, sinh bằng script, không sửa tay.

## Chạy lại thí nghiệm

Chạy trên Linux (máy ảo), trong thư mục repo, đã có cụm kind theo phase 0 và file `.env` chứa khóa API.

1. Thử không cần cụm: `.venv/bin/python -m tests.test_guards` và `.venv/bin/python -m tests.test_v2`.
2. Thử trên cụm thật, không gọi LLM: `.venv/bin/python scripts/v2_check.py`.
3. Chạy trọn v2 (khoảng 3–4 ngày, chạy tiếp được sau gián đoạn): `tmux new -d -s kltn-v2 'bash scripts/v2_pipeline.sh'`, xem tiến độ bằng `tail -f data/vm-gcp/logs/v2-pipeline.log`.
4. Phân tích: `.venv/bin/python scripts/v2_analyze.py` và `.venv/bin/python scripts/rescore_wait.py v2-main`.

## Còn lại gì của repo gốc

Đã xóa terraform, helm-chart, istio, CI của Google, docs của Google, và các bản sao manifest trùng lặp. Lấy lại được bằng `git checkout <commit-trước-khi-dọn> -- <đường-dẫn>`.

- `release/kubernetes-manifests.yaml` — file cài Online Boutique, image đã pin ở `v0.10.6`.
- `kustomize/components/google-cloud-operations/` — nguồn của phần vá biến env bật tracing. Bước 0.4 copy từ đây.
- `src/` — mã nguồn 11 service. Chỉ đọc để tra cứu, không sửa. Code khóa luận nằm ở `src_thesis/`.
- `protos/` — định nghĩa gRPC, dùng khi viết `data/logical_topology.yaml`.

## Giấy phép

Mã nguồn gốc theo Apache License 2.0, xem [LICENSE](LICENSE).
