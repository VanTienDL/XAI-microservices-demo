# KLTN-PLAN.md — Kế hoạch thực hiện theo phase

Bản dịch KLTN.md thành việc làm được. Mỗi phase có: mục tiêu, các bước nhỏ, dấu hiệu thành công, cổng chặn (không qua cổng thì không sang phase sau).

---

## 3 sự thật về repo phải biết trước

Ba chỗ này ban đầu KLTN.md ghi khác với code thật. **KLTN.md đã được sửa lại theo đúng những gì viết dưới đây**, nên hai file không còn mâu thuẫn. Phần này giữ lại vì nó giải thích *vì sao* phase 0 và phase 4 làm như vậy.

**Sự thật 1 — `release/kubernetes-manifests.yaml` không hề có `ENABLE_TRACING`.**
Tracing nằm ở chỗ khác: [kustomize/components/google-cloud-operations/kustomization.yaml](kustomize/components/google-cloud-operations/kustomization.yaml) — file này vá biến env `ENABLE_TRACING=1` và `COLLECTOR_SERVICE_ADDR=opentelemetrycollector:4317` vào 8 deployment. Kèm theo nó là [otel-collector.yaml](kustomize/components/google-cloud-operations/otel-collector.yaml), và file này **không chạy được trên kind**: nó có initContainer gọi `metadata.google.internal` để lấy project id của Google Cloud, trên máy cậu địa chỉ đó không tồn tại nên pod sẽ treo ở `Init:0/1`. Exporter của nó cũng là `googlecloud`, tức đẩy trace lên GCP.
Cách xử lý: giữ nguyên phần vá env, chỉ thay file collector bằng bản tự viết. Vì service collector của mình cũng đặt tên `opentelemetrycollector` cổng `4317` nên phần vá env dùng lại được y nguyên, không phải sửa gì.

**Sự thật 2 — chỉ 7 trong 11 service phát trace.**
Có trace: `frontend`, `productcatalogservice`, `currencyservice`, `recommendationservice`, `checkoutservice`, `paymentservice`, `emailservice`.
Không có: `cartservice` (C#, code không import OpenTelemetry), `shippingservice` (Go, cũng không import và không đọc `ENABLE_TRACING`), `adservice` (Java, hàm `initTracing()` chỉ in `Tracing enabled but temporarily unavailable` rồi thôi — [AdService.java:214](src/adservice/src/main/java/hipstershop/AdService.java#L214)).
Hệ quả: tiêu chí 0.4 ban đầu ("thấy trace đi qua frontend → cartservice → checkoutservice → paymentservice") không xảy ra đúng như vậy, vì `cartservice` không có span của chính nó.
Nhưng vẫn cứu được: `frontend` ([main.go:231](src/frontend/main.go#L231)) và `checkoutservice` ([main.go:216](src/checkoutservice/main.go#L216)) đều bọc kết nối gRPC bằng `otelgrpc.NewClientHandler()`, nên **span phía người gọi** vẫn có. Các cạnh tới `cartservice` và `shippingservice` dựng từ span client, không phải từ span server. `redis-cart` thì hoàn toàn không nhìn thấy — điểm mù, ghi vào phần hạn chế của báo cáo.
Span client **không có tag `rpc.service`** (đã kiểm chứng trên Jaeger ở bước 0.4). Nó có `rpc.method = hipstershop.CartService/GetCart` và `server.address` là **ClusterIP** chứ không phải tên service. Cách xác định đích: tra `server.address` + `server.port` vào bảng ClusterIP dựng lại mỗi lần snapshot. Chi tiết ở mục 4 KLTN.md.
Thêm một chi tiết dễ sập: các service Go không đặt tên service trong code, chúng lấy từ biến `OTEL_SERVICE_NAME`. Thiếu biến này thì trace hiện `unknown_service` và graph vô dụng.

**Sự thật 3 — danh sách service của twin ban đầu sẽ làm checkout gãy.**
`checkoutservice` bắt buộc có 6 địa chỉ: productcatalog, shipping, payment, email, currency, cart (xem env trong [release/kubernetes-manifests.yaml](release/kubernetes-manifests.yaml)). KLTN.md bảo bỏ `currencyservice`, `shippingservice`, `emailservice` khỏi twin — bỏ xong thì đặt hàng trong twin sẽ lỗi, mà luồng đặt hàng chính là thứ cần đo.
Cách xử lý: twin bỏ đúng 3 thứ là `adservice`, `recommendationservice`, `loadgenerator`. Ba cái bị bỏ trong KLTN.md thì giữ lại — chúng đều nhẹ (currency và email dưới 100MB mỗi cái).

---

## Quy ước làm việc

**Nhánh git.** Repo này là fork của Google, `main` phải để dành cho việc kéo code gốc về sau này. Code khóa luận làm trên nhánh riêng:

```powershell
git checkout -b thesis/main
```

Sau này mỗi phase một nhánh con `thesis/phase-1-observe`, làm xong merge vào `thesis/main`. Làm một mình nên không cần pull request, nhưng mỗi phase xong thì commit và gắn tag `phase-1-done` để lúc gãy còn chỗ lùi về.

**Thư mục.** Code khóa luận nằm ở `infra/`, `src_thesis/`, `data/`, `docs/`. Lưu ý: repo đã có sẵn thư mục `src/` chứa 11 service của Google — đừng đổ code Python vào đó, đặt tên khác là `src_thesis/` để khỏi lẫn.

Repo đã dọn sạch phần thừa của Google (terraform, helm-chart, istio, CI, docs gốc, các bản sao manifest trùng lặp). Còn lại đúng những thứ dùng đến: `release/kubernetes-manifests.yaml`, `kustomize/components/google-cloud-operations/`, `src/`, `protos/`.

**File không commit.** `.gitignore` đã bổ sung `.env`, `data/runs/`, `__pycache__/`; `.venv/` vốn đã có sẵn.

**Chỗ chạy lệnh.** Mặc định mọi lệnh trong file này chạy ở PowerShell trên Windows, tại thư mục `d:\KLTN\XAI-microservices-demo`. Chỗ nào phải vào Ubuntu/WSL tớ ghi rõ.

---

## Phase 0 — Hạ tầng chạy được (1 tuần, làm ngay)

Mục tiêu: một cluster kind có Online Boutique chạy, trace chảy vào Jaeger, metric RED chảy vào Prometheus, và một hàm Python bật/tắt được service.

### 0.1 — Docker + WSL2 + giới hạn RAM

1. PowerShell quyền admin: `wsl --install`, khởi động lại máy.
2. Cài Docker Desktop, vào Settings > Resources bật WSL2 backend.
3. Tạo file `C:\Users\TIEN\.wslconfig` với nội dung `memory=10GB`, `processors=6`, `swap=8GB` như mục 2 của KLTN.md.
4. `wsl --shutdown` rồi mở lại Docker Desktop.

Thành công khi: `docker run hello-world` in ra dòng chào, và Task Manager thấy `Vmmem`/`vmmemWSL` không vượt quá 10GB.

### 0.2 — kind + kubectl

1. `winget install Kubernetes.kind` và `winget install Kubernetes.kubectl`.
2. Viết `infra/kind-cluster.yaml`: một node control-plane, mở sẵn `extraPortMappings` cổng 8080 để khỏi phải port-forward suốt.
3. `kind create cluster --name boutique --config infra/kind-cluster.yaml`.

Thành công khi: `kubectl get nodes` in ra một dòng, cột STATUS là `Ready`.

### 0.3 — Cài Online Boutique bản gốc

1. `kubectl apply -f release/kubernetes-manifests.yaml`.
2. Chờ 3–5 phút, theo dõi bằng `kubectl get pods -w`.
3. `kubectl port-forward deployment/frontend 8080:8080`, mở `localhost:8080`, đặt thử một đơn hàng cho tới màn hình "Your order is complete".

Thành công khi: 12 pod đều `Running` và đơn hàng đặt xong. Nếu pod nào `Pending` vì thiếu RAM thì dừng lại xử lý ngay, đừng đi tiếp.

Ghi lại: chạy `kubectl top pods` (cần metrics-server, nếu chưa có thì cài sau ở 0.6) và lưu con số RAM nền vào `docs/thesis-notes.md`. Con số này dùng để đối chiếu ở phase 4 khi dựng twin.

### 0.4 — Tracing về Jaeger nội bộ (bước dễ vỡ nhất)

Đây là chỗ chiếm nhiều thời gian nhất phase 0. Làm theo đúng thứ tự, mỗi bước kiểm tra xong mới sang bước sau.

1. Viết `infra/jaeger-all-in-one.yaml`: Deployment `jaeger` dùng image `jaegertracing/all-in-one`, đặt env `COLLECTOR_OTLP_ENABLED=true`, kèm Service mở cổng `16686` (giao diện web) và `4317` (nhận OTLP).
2. `kubectl apply -f infra/jaeger-all-in-one.yaml`, rồi `kubectl port-forward svc/jaeger 16686:16686` và mở `localhost:16686`. Giao diện Jaeger hiện lên là được, lúc này chưa có trace nào cả.
3. Copy thư mục `kustomize/components/google-cloud-operations` thành `infra/tracing-local`. Xóa file `otel-collector.yaml` trong bản copy, viết lại `infra/tracing-local/otel-collector.yaml` của mình: bỏ initContainer, bỏ exporter `googlecloud`, thay bằng exporter `otlp` trỏ tới `jaeger:4317`. Giữ nguyên tên Service là `opentelemetrycollector` và cổng `4317` — giữ nguyên thì phần vá env trong `kustomization.yaml` dùng lại được, không phải sửa dòng nào.
4. Viết `infra/kustomization.yaml` gồm `resources` trỏ tới `../release/kubernetes-manifests.yaml` và `components` trỏ tới `./tracing-local`.
5. `kubectl apply -k infra/`. Các pod sẽ khởi động lại vì env đổi.
6. Đặt lại một đơn hàng trên `localhost:8080`, rồi vào Jaeger UI, chọn service `frontend`, bấm Find Traces.

Thành công khi: thấy trace của `frontend` có span con sang `productcatalogservice`, `currencyservice`, `checkoutservice`, và trong `checkoutservice` có span sang `paymentservice`. Nhớ điểm lệch 2 ở trên: sẽ **không** có span mang tên service `cartservice` — thay vào đó là span client nằm trong `frontend` với thuộc tính `rpc.service = hipstershop.CartService`. Nhìn thấy span client đó là đạt.

Nếu quá 1 tuần vẫn không có trace: dừng, hỏi lại, theo mục 10 của KLTN.md.

### 0.5 — spanmetrics: có RED metrics mà không sửa code service

1. Sửa `infra/tracing-local/otel-collector.yaml`: thêm mục `connectors` với `spanmetrics`, thêm exporter `prometheus` mở cổng `8889`.
2. Pipeline `traces` có exporter là `[otlp, spanmetrics]`; thêm pipeline `metrics` nhận từ `[spanmetrics]` và xuất ra `[prometheus]`.
3. Mở thêm cổng `8889` trong Service `opentelemetrycollector`.
4. `kubectl apply -k infra/`, rồi `kubectl port-forward svc/opentelemetrycollector 8889:8889` và mở `localhost:8889/metrics`.

Thành công khi: trang metrics có các dòng bắt đầu bằng `traces_span_metrics_duration` và `traces_span_metrics_calls_total`, kèm nhãn `service_name` mang tên các service của Boutique. Đây chính là ba chỉ số RED (số request, tỉ lệ lỗi, độ trễ) mà mục 7.1 cần.

### 0.6 — Metric hạ tầng

1. Thêm repo helm: `helm repo add prometheus-community https://prometheus-community.github.io/helm-charts` rồi `helm repo update`.
2. Cài bản đã cắt bớt cho nhẹ:

```powershell
helm install mon prometheus-community/kube-prometheus-stack --set alertmanager.enabled=false --set prometheus.prometheusSpec.retention=2d
```

3. Cho Prometheus đọc luôn cổng 8889 của collector: viết `infra/collector-servicemonitor.yaml` (một đối tượng `ServiceMonitor` trỏ vào Service `opentelemetrycollector`, cổng tên `prometheus`), rồi apply.
4. `kubectl port-forward svc/mon-kube-prometheus-stack-prometheus 9090:9090`, mở `localhost:9090`, chạy thử hai câu truy vấn: `traces_span_metrics_calls_total` và `container_memory_working_set_bytes`.

Thành công khi: cả hai câu đều trả về dữ liệu. Câu đầu là metric RED từ trace, câu sau là RAM từng container.

Cảnh báo RAM: đây là lúc dễ hết RAM nhất trong phase 0. Nếu máy lag nặng hoặc pod bị `OOMKilled`, tắt Grafana bằng `--set grafana.enabled=false` (đồ thị cho báo cáo có thể vẽ bằng Python ở phase 6, không nhất thiết phải có Grafana).

### 0.7 — Viên gạch Python đầu tiên

1. Tạo môi trường ảo: `python -m venv .venv` rồi `.\.venv\Scripts\Activate.ps1`.
2. `pip install kubernetes prometheus-api-client requests pydantic python-dotenv`, rồi `pip freeze > requirements.txt`.
3. Viết `src_thesis/k8s_client.py` bọc thư viện `kubernetes`, cung cấp: `list_pods`, `get_logs`, `scale_deployment`, `set_env`, `set_cpu_limit`, `delete_pod`. Mỗi hàm đổi trạng thái phải trả về giá trị cũ để hoàn tác được — đây là yêu cầu của mục 5 và checklist mục 12.
4. Viết `scripts/smoke_k8s.py` gọi thử: hạ `cartservice` xuống 0 replica, chờ, rồi đưa về 1.

Thành công khi: chạy script, `kubectl get pods` thấy pod cartservice biến mất rồi hiện lại.

**Cổng chặn phase 0:** trace vào Jaeger, spanmetrics vào Prometheus, hàm Python scale được deployment. Thiếu một trong ba thì không sang phase 1.

---

## Phase 1 — Quan sát và mô hình hóa (1,5 tuần)

Mục tiêu: từ cluster đang chạy, lấy ra được một object Python mô tả đầy đủ "hệ thống lúc này đang thế nào", và một đoạn text mô tả graph để nhồi cho LLM.

1. `src_thesis/telemetry/prometheus_client.py` — hàm `get_red_metrics(window)` trả về dict `{service: {rate, error_rate, p95_latency}}`, dựng từ `traces_span_metrics_*`. Thêm `get_resource_metrics()` lấy CPU/RAM từng pod.
2. `src_thesis/telemetry/jaeger_client.py` — gọi API `/api/traces` của Jaeger, lấy N trace gần nhất, trả về danh sách span đã chuẩn hóa.
3. `src_thesis/graph/runtime_graph.py` — từ danh sách span dựng graph có hướng. Hai quy tắc cạnh:
   - Span server: cạnh từ service cha sang service con theo quan hệ span.
   - Span client không có span server đối ứng: tra `server.address` + `server.port` vào bảng ClusterIP (lấy từ `k8s_client.list_services()`, dựng lại mỗi lần snapshot vì IP đổi khi Service tạo lại), đối chiếu chéo với phần trước dấu `/` của `rpc.method`.

   Quy tắc thứ hai là chỗ vớt lại `cartservice` và `shippingservice` (sự thật 2).
4. `data/logical_topology.yaml` — viết tay sơ đồ thiết kế 11 service theo đúng phần "Các service và quan hệ" ở mục 4 KLTN.md.
5. `src_thesis/graph/logical_graph.py` — đọc file YAML trên thành cùng kiểu dữ liệu graph với bước 3.
6. `src_thesis/graph/diff.py` — so hai graph, trả về: cạnh có trong thiết kế mà runtime không thấy (dấu hiệu service chết), cạnh runtime có mà thiết kế không có (gọi sai chỗ), cạnh có nhưng latency vọt bất thường.
7. `src_thesis/graph/serialize.py` — biến graph + diff + metric thành đoạn text tiếng Anh gọn cho LLM đọc. Giới hạn độ dài, vì text này đi vào prompt và tính tiền theo token.
8. `src_thesis/telemetry/snapshot.py` — gom tất cả ở trên thành một object `SystemSnapshot`, có hàm `to_json()` để lưu vào `data/runs/`.

Thành công khi: chạy một lệnh, in ra file JSON snapshot đầy đủ, và đoạn text mô tả graph đọc vào thấy hiểu được hệ thống đang chạy bình thường.

**Cổng chặn:** `diff.py` chạy trên hệ thống khỏe mạnh phải cho ra kết quả gần như rỗng. Nếu lúc bình thường mà diff đã báo đầy lỗi thì tín hiệu này vô dụng cho XAI, phải sửa trước khi đi tiếp.

---

## Phase 2 — Tiêm lỗi (1 tuần)

Mục tiêu: bốn cách phá hệ thống, mỗi cách có nút hoàn tác và một file ground truth.

1. `src_thesis/faults/injectors.py` — bốn hàm theo mục 6 KLTN.md: F1 đặt env `EXTRA_LATENCY=6s` cho `productcatalogservice` (biến này có thật, xem [server.go:88](src/productcatalogservice/server.go#L88)); F2 scale về 0; F3 xóa pod; F4 hạ `resources.limits.cpu`. Mọi hàm gọi qua `k8s_client.py`, mỗi hàm có hàm nghịch đảo tương ứng.
2. Mỗi injector khi chạy ghi file JSON ground truth đúng cấu trúc ở mục 6 (có `expected_propagation` và `correct_action_class`).
3. `src_thesis/faults/scenarios.yaml` — 6 kịch bản: S1 (F1 trên productcatalog), S2 (F2 trên currencyservice), S3 (F3 trên checkoutservice), S4 (F4 trên frontend), S5 (F4 trên productcatalog), S6 (kép F1+F2).
4. `scripts/inject.py` — công cụ chạy tay: `--list`, `S2 --watch`, `--revert`, `--status`.
5. Chạy tay từng kịch bản theo `recommended_order` trong file, đối chiếu kết quả với `expected_symptom`.

### Tình trạng: PHASE 2 XONG (2026-08-23)

Cả 6 kịch bản đã chạy và kiểm chứng trọn vòng: tiêm, bắt triệu chứng, hoàn tác, hệ thống về sạch. 35 file bằng chứng trong `data/runs/`. Bảng dấu hiệu nhận dạng của từng kịch bản và bốn lỗi đã sửa nằm ở `docs/thesis-notes.md`.

Hai giả định ban đầu đã bị số liệu bác bỏ, chi tiết ở `docs/thesis-notes.md`:

- **F2 không tạo ra `missing_edges`** như dự đoán, mà tạo ra `error_edges`. Cạnh dựng từ span phía người gọi, nên service đích chết thì cạnh vẫn còn, chỉ mang trạng thái lỗi. `missing_edges` là chữ ký của loại hỏng khác: người gọi ngừng gọi hẳn.
- **Phải chờ lâu hơn cửa sổ quan sát, không phải ngắn hơn.** Chờ 2 phút như kế hoạch ban đầu thì cửa sổ 5 phút vẫn chứa 3 phút dữ liệu lúc còn khỏe, số liệu bị pha loãng tới mức service đã tắt hẳn vẫn hiện `0.0% errors`. Đã nâng lên 330 giây.

Thành công khi: mỗi kịch bản đều nhìn thấy dấu hiệu rõ ràng và **khớp với `expected_symptom`** đã ghi trong file. Không khớp thì sửa lại mô tả trong file cho đúng số liệu thật, đừng sửa số liệu cho vừa mô tả.

Nếu kịch bản nào không tạo ra dấu hiệu quan sát được thì bỏ nó, thay bằng kịch bản khác. Lỗi mà telemetry không thấy thì XAI không có cửa đoán đúng.

**Cổng chặn:** sau mỗi lần hoàn tác, hệ thống phải trở về trạng thái sạch. `inject.py` tự kiểm tra điều này: nó chụp ảnh nền và chỉ tiêm khi diff sạch, chờ tối đa 6 phút, quá thì dừng và báo lỗi thật chưa sửa.

---

## Phase 3 — XAI (1,5 tuần)

Mục tiêu: đưa snapshot vào, nhận JSON giải thích đúng schema ra, và biết nó đúng bao nhiêu phần trăm.

1. `src_thesis/xai/schema.py` — chép nguyên hai class `Explanation` và `ProposedAction` ở mục 7.2 KLTN.md.
2. `src_thesis/xai/prompt_templates.py` — prompt gồm: mô tả hệ thống, dữ liệu snapshot dạng text, schema JSON, và 2 ví dụ few-shot.
3. `src_thesis/xai/reasoner.py` — gọi API LLM, ép định dạng JSON, validate bằng Pydantic. Sai schema thì retry tối đa 3 lần, mỗi lần nhồi lại thông báo lỗi Pydantic vào prompt. Cache theo hash của snapshot. Ghi lại số token mỗi lần gọi.
4. Chuẩn bị API key trong `.env`, đọc bằng `python-dotenv`. Không commit file này.
5. `src_thesis/eval/metrics.py` — hai hàm đầu tiên: `root_cause_accuracy` và `propagation_accuracy` (so `propagation_path` với `expected_propagation` bằng Jaccard).
6. Chạy đánh giá riêng phần XAI: 5 kịch bản × 5 lần = 25 ca, chỉ đo hai chỉ số trên, chưa có agent.

Thành công khi: có bảng số cho biết XAI đoán đúng nguyên nhân gốc bao nhiêu phần trăm, kèm độ lệch chuẩn.

**Cổng chặn:** root cause accuracy dưới 50% thì dừng lại sửa prompt, đừng xây agent lên trên một XAI đoán bừa. Số này cũng là số đầu tiên đưa vào báo cáo được.

---

### Tình trạng: PHASE 3 XONG (2026-08-23)

**Cổng chặn đạt với biên rộng: root cause accuracy 93.3%**, yêu cầu 50%. Đo hai loạt độc lập 6 kịch bản × 5 lần trên `gpt-4.1-mini`, ra 90.0% và 93.3%.

```
S1 100%   S2 100%   S3 100%   S4 40-60%   S5 100%   S6 100%
PROPAGATION 0.78   hanh dong dung 90%   loai loi dung 77%
```

Khác kế hoạch ban đầu ở ba chỗ, đều có lý do trong `docs/thesis-notes.md`:

- **6 kịch bản × 5 lần = 30 ca**, không phải 25. Kịch bản kép S6 cũng chấm điểm được.
- **Đánh giá chạy trên snapshot đã lưu**, không đụng cluster (`eval/replay.py`). Bắt buộc phải vậy: mỗi lần sửa prompt phải đo lại cả 6 kịch bản, mà tiêm lỗi lại thì mất 30 phút một lượt và mỗi lượt ra trạng thái hơi khác nên không so sánh được.
- **Hai nhà cung cấp qua một bộ code** (`reasoner.py` dùng thư viện OpenAI, Groq phục vụ theo đúng giao thức đó).

Bảy lỗi đã sửa trong phase này, ba lỗi đáng nhớ nhất:

1. **Snapshot giấu sự vắng mặt.** Deployment bị hạ về 0 bản sao thì không còn pod nào để liệt kê, nên POD HEALTH ghi "all 10 pods ready" mà không nói đáng lẽ phải có 11. Telemetry chỉ báo cáo cái đang tồn tại; muốn thấy cái thiếu thì phải có danh sách cái đáng lẽ phải có mà đối chiếu.
2. **Prompt khẳng định điều sai sự thật.** Trường `cpu` rỗng nhưng vẫn in "no service is close to its CPU limit". Prompt thiếu dữ liệu chỉ làm model bớt chắc chắn; prompt nói sai chủ động đẩy model ra khỏi đúng nguyên nhân.
3. **Bộ nhãn schema không phủ bộ nhãn đáp án.** Đáp án S3 ghi `pod_kill` mà schema không có nhãn đó, nên chỉ số loại lỗi của S3 luôn bằng 0 vì lý do kỹ thuật chứ không phải vì chẩn đoán kém.

**Phát hiện về phương pháp: thêm quy tắc vào prompt không đơn điệu tăng.** Gói quy tắc v5 làm kết quả tụt từ 90% xuống 66.7%, vì một quy tắc đúng trong đa số trường hợp lại phá đúng trường hợp nó không áp dụng. Mọi thay đổi prompt phải đo lại trên **toàn bộ** bộ kịch bản.

**CẬP NHẬT sau khi trả nợ dữ liệu CPU ở bước 4.0: root cause accuracy đạt 100%, độ lệch chuẩn 0.**

Tiêm lại S4 và S5 để snapshot có dữ liệu CPU, rồi chạy lại với **prompt không đổi một chữ**. S4 từ 40–60% lên 100%, S5 từ 0% lên 100%, loại lỗi từ 76.7% lên 100%. Chỉ một biến thay đổi nên đây là **bằng chứng nhân quả** cho luận điểm trung tâm: chất lượng chẩn đoán bị chặn trên bởi chất lượng telemetry, không phải bởi năng lực model.

**Cảnh báo cho báo cáo:** con số này có rủi ro overfitting, vì prompt được chỉnh bằng cách soi chính 6 ca này. Phase 6 tiêm lỗi lại từ đầu, và **con số phase 6 mới là con số dùng để kết luận**.

**Nợ mang sang phase 6:**

- ~~Trường `cpu` rỗng trong snapshot~~ **ĐÃ TRẢ ở bước 4.0.** Nguyên nhân không phải Prometheus thiếu metric mà là `cpu_vs_limit()` chưa tồn tại lúc chụp snapshot phase 2. Đã tiêm lại S4 và S5.
- Bảng so sánh Groq với OpenAI: Groq gói miễn phí chỉ cho 8000 token mỗi phút mà một lần chẩn đoán tốn khoảng 5900, nên một loạt 30 ca mất trên 30 phút và hôm nay đã cạn hạn mức. Tính vào lịch phase 6.
- `expected_propagation` của F4-frontend là danh sách rỗng vì `frontend` là cửa ngõ, không service nội bộ nào gọi nó.

---

## Phase 4 — Digital Twin (1,5 tuần)

Mục tiêu: dựng được bản sao ở namespace `twin`, đo được, xóa được, và biết nó giống production đến mức nào.

1. `infra/twin-manifests.yaml` — copy `release/kubernetes-manifests.yaml`, xóa block của `adservice`, `recommendationservice`, `loadgenerator`. **Giữ lại** `currencyservice`, `shippingservice`, `emailservice`, lý do ở sự thật 3.
2. `src_thesis/agent/twin_manager.py` — `create_twin()` tạo namespace `twin` rồi apply manifest; `load_state(snapshot)` áp lại cấu hình hiện tại của production lên twin (số replica, biến env, resource limit); `destroy_twin()` xóa namespace.
3. Bơm traffic vào twin: viết một loadgen Python nhẹ trong `src_thesis/agent/twin_manager.py` gọi API frontend của twin, thay vì bật loadgenerator gốc (tiết kiệm RAM).
4. `src_thesis/agent/verifier.py` — đo RED metrics của twin trước và sau khi thử action, trả về phán quyết `better` / `worse` / `no_change` kèm con số cụ thể.
5. Đo twin fidelity: với mỗi kịch bản lỗi, tiêm lỗi vào twin, chạy một action, ghi kết quả. Rồi làm y hệt trên production, so hai kết quả. Tỉ lệ khớp chính là twin fidelity ở chỉ số 7 mục 8.

Thành công khi: `create_twin()` → đặt được đơn hàng trong twin → `destroy_twin()` xong RAM trở về mức nền.

**Cổng chặn:** phải chạy đủ chu trình tạo–đo–xóa ít nhất 3 lần liên tiếp mà máy không hết RAM. Nhớ mục 2 KLTN.md: không bao giờ chạy twin song song với thí nghiệm production.

Nếu twin fidelity thấp: đó là kết quả nghiên cứu, báo cáo trung thực, không phải thất bại (mục 10 KLTN.md).

---

### Tình trạng: PHASE 4 XONG (2026-08-24)

**Cổng chặn ĐẠT:** 3 vòng dựng–đo–xóa liên tiếp, mỗi vòng dựng 34 giây xóa 13 giây, máy không hết RAM. Twin 9 pod chỉ ăn **169–306 MiB**, nhẹ hơn dự trù 3.8 GB ở mục 2 hơn 10 lần — dự trù ước theo giới hạn khai báo, còn đây là RAM đang dùng thật.

**Đã có, khác kế hoạch ở hai chỗ:**

- `infra/twin/` là một lớp phủ kustomize (namespace, manifest bản gọn, bản vá tracing), không phải một file `twin-manifests.yaml` đơn lẻ — kustomize không cho tham chiếu file ngoài thư mục gốc.
- Thêm `twin_loadgen.py` và hai công cụ dòng lệnh `scripts/twin.py`, `scripts/twin_fidelity.py` ngoài kế hoạch.

**Tiêu chí thành công đã đạt:** `create_twin()` → đặt được 5 đơn hàng trong twin → `destroy_twin()` → RAM về mức nền.

**TWIN FIDELITY = 100% (6/6 lần khớp)**, đo trên S1, S4, S5 với hai hành động mỗi kịch bản (một đúng, một sai). Kết quả thô ở `data/fidelity/20260824-113038_fidelity.json`.

```
S1 dung  rollback           twin=better     production=better     KHOP
S1 sai   scale_up           twin=no_change  production=no_change  KHOP
S4 dung  adjust_resources   twin=better     production=better     KHOP
S4 sai   restart_pod        twin=worse      production=worse      KHOP
S5 dung  adjust_resources   twin=better     production=better     KHOP
S5 sai   restart_pod        twin=no_change  production=no_change  KHOP
```

Twin và production còn khớp cả **danh sách service tốt lên**, tức là đường đi tới kết luận cũng giống nhau chứ không chỉ kết luận cuối.

**Ba hành động sai đều KHÔNG ra `better`.** Vì `is_safe_to_promote` chỉ đúng khi phán quyết là `better`, tính chất này nghĩa là agent ở phase 5 sẽ không đưa hành động sai nào lên production trong ba kịch bản đã thử. Đây là bằng chứng trực tiếp cho giả thuyết ở mục 1 KLTN.md.

**Hai giới hạn phải ghi trong báo cáo, đừng để 100% bị đọc quá lời:**

1. Chỉ 6 lần thử trên 3 kịch bản. Với 6 phép thử nhị phân, một twin chỉ đúng 80% vẫn có khoảng 26% khả năng khớp trọn 6 lần do may.
2. Ba kịch bản đều là lỗi **tĩnh và cục bộ** (một biến môi trường, một trần CPU) nên twin tái hiện dễ. Chưa thử lỗi phụ thuộc trạng thái tích lũy hay thời điểm — đó mới là chỗ twin dễ lệch nhất.

**Lần đo đầu tiên ra 50% và con số đó SAI**, do ba lỗi đo đạc: twin chạy 3 người dùng thay vì 10, đo qua `kubectl port-forward` vốn sập ở mức tải bằng production, và twin thiếu `recommendationservice` nên `productcatalogservice` chỉ chịu 2.94 req/s so với 14.45. Cùng bộ code, cùng kịch bản, sửa ba lỗi đó thì 50% thành 100%.

**Ba lỗi lớn của phase này, chi tiết ở `docs/thesis-notes.md`:**

1. **Số liệu twin và production trộn vào nhau không dấu hiệu báo.** Hai nguồn RED đặt tên theo hai quy ước khác nhau: đo phía server có tiền tố `twin-`, đo gián tiếp từ phía người gọi thì không. Nguồn thứ hai lại không lọc theo người gọi nên `cartservice` hai bên dồn chung một khóa. Với thí nghiệm fidelity — vốn là phép so hai môi trường — đây là kiểu hỏng làm hỏng luôn kết luận mà con số vẫn ra đẹp.
2. **Verifier để service lưu lượng thấp lật phán quyết.** `checkoutservice` và `paymentservice` chạy 0.08 req/s, khoảng 24 request mỗi cửa sổ, p95 nhảy loạn. Đã thêm ngưỡng `MIN_RATE_FOR_VERDICT = 0.3` req/s. Nguyên tắc: **"không đủ cơ sở để kết luận" khác với "không có thay đổi"**, gộp chung thì phán quyết sai — cùng họ với lỗi prompt nói sai sự thật ở phase 3.
3. **Bản vá sửa lỗi upstream không được áp cho twin.** Lần dựng twin đầu tiên hỏng đúng ở `emailservice`, vì manifest twin chép từ `release/` nên không mang bản vá nới hạn thăm dò. Giống nhau giữa hai môi trường chính là điều kiện để con số fidelity có nghĩa.

4. **Twin và production chịu tải khác nhau — lỗi nặng nhất của phase này.** Tớ đặt twin 3 người dùng ảo trong khi `loadgenerator` của production đặt `USERS=10`, và rút thời gian chờ xuống 0.5–3 giây trong khi bản chính dùng 1–10 giây. Hậu quả: twin chạy 0.67 req/s so với 2.93 req/s. Kịch bản S1 làm lưu lượng twin sụp dưới ngưỡng và verifier không còn đủ mẫu. Nguy hiểm hơn, nó **làm sai lệch chính kết luận về twin**: chênh lệch quan sát được có thể đến hoàn toàn từ tải chứ không từ bản chất twin. Đã sửa cho khớp đúng cấu hình production.

   Nguyên tắc: **so sánh hai môi trường thì mọi biến ngoài biến đang khảo sát phải khớp theo CẤU HÌNH, không phải theo cảm giác "đủ dùng".** Khớp một nửa còn nguy hiểm hơn không khớp gì, vì nó tạo cảm giác đã kiểm soát.

**Bằng chứng bản sửa verifier hoạt động đúng.** Chấm điểm lại loạt S4 cũ bằng `scripts/rescore_fidelity.py` (không đụng cluster, vì file kết quả lưu đủ số liệu thô): phía production chuyển từ `no_change` sang `better` cho `adjust_resources` và sang `worse` cho `restart_pod` — đúng cả hai. Phía twin vẫn `no_change` vì tải quá thấp, nên phải chạy lại.

---

## Phase 5 — ReAct loop (1 tuần)

Mục tiêu: nối XAI và twin thành một vòng lặp tự động.

1. `pip install langgraph langchain-core`.
2. `src_thesis/agent/actions.py` — bảy action ở schema mục 7.2, mỗi cái là một hàm Python có hàm hoàn tác. Gắn `risk_class` cho từng cái theo mục 7.3.
3. `src_thesis/agent/react_loop.py` — graph LangGraph các node: Observe → Reason → SelectAction → phân nhánh theo `risk_class` → (easy/medium: áp thẳng production) hoặc (hard: dựng twin, thử, đo, tốt thì áp production, xấu thì quay lại Reason kèm kết quả twin làm feedback).
4. Trần 3 vòng. Hết trần thì dừng và xuất báo cáo "không tự sửa được" kèm explanation.
5. Ghi log đầy đủ mỗi vòng vào `data/runs/<run_id>.json`: snapshot đầu vào, JSON của XAI, action đã chọn, kết quả twin, kết quả cuối, số token.

Thành công khi: tiêm F2 vào `currencyservice`, chạy agent, agent tự scale lại về 1 replica và hệ thống hồi phục, toàn bộ được ghi vào một file JSON.

**Cổng chặn:** file log JSON phải đủ để dựng lại toàn bộ câu chuyện một ca. Thiếu log thì phase 6 không viết được chương kết quả.

---

### Tình trạng: PHASE 5 XONG (2026-08-24)

**Cổng chặn ĐẠT:** log JSON dựng lại được trọn vẹn câu chuyện một ca — chế độ, số vòng, token, ảnh nền đã dùng, và với từng vòng: trạng thái hệ thống, chẩn đoán XAI, hành động kèm mức rủi ro, phán quyết twin, kết quả thi hành.

**Đã có:** `actions.py` (7 hành động + hoàn tác + phân mức rủi ro), `react_loop.py` (LangGraph 1.2.11, 7 node 13 cạnh, 3 chế độ), `scripts/agent_run.py`, `src_thesis/graph/baseline.py`.

**Ba ca kiểm thử trên hệ thống thật:**

```
test-s2         twin_verified  2/3 vong   399s  -> KHOE MANH, agent tu sua duoc
test-s1         twin_verified  3/3 vong  1182s  -> twin CHAN 1 hanh dong co hai
test-s1-direct  direct         3/3 vong   180s  -> khong chan gi
```

**Ca S2 đạt đúng tiêu chí thành công của kế hoạch:** tiêm F2 vào `currencyservice`, agent chẩn đoán đúng (tin cậy 0.96), tự đưa về 1 bản sao, hệ thống hồi phục.

**Ca S1 cho kết quả quan trọng nhất: TWIN ĐÃ CHẶN MỘT HÀNH ĐỘNG CÓ HẠI.** Agent định `restart_pod`, twin thử trước và phán `worse`, nên nó không bao giờ chạm vào production. Phán quyết này khớp với đo fidelity phase 4 — twin chặn đúng và chặn vì lý do đúng.

**Đánh đổi đo được, đi thẳng vào mục 8:** `twin_verified` mất 1182s và chặn 1 hành động; `direct` mất 180s và không chặn gì. Chậm hơn 6.5 lần để an toàn hơn — đúng thứ giả thuyết dự đoán.

**PHẢI GHI RÕ MỘT HẠN CHẾ:** cặp ca S1 twin_verified với S1 direct **không phải đối chứng sạch**, vì agent chế độ `direct` không hề chọn `restart_pod` — LLM dao động giữa hai lần chạy nên "có twin hay không" không phải khác biệt duy nhất. Ca S1 chứng minh **cơ chế chặn hoạt động**, không chứng minh **twin làm giảm hành động có hại tính trung bình**. Cái sau cần phase 6 với 5 lần mỗi chế độ.

**Ba lỗi lớn phát hiện và sửa, chi tiết ở `docs/thesis-notes.md`:**

1. **Agent chạy không có ảnh nền** — phát hiện trước khi kiểm thử. Không có nền thì chỉ bắt được cạnh chậm hơn 500ms tuyệt đối, mà S1/S4/S5 đều dưới 500ms (101–284ms). Kiểm chứng trên snapshot phase 2: S5 không nền cho diff **hoàn toàn sạch**. Agent sẽ báo "hệ thống khỏe mạnh" trên hệ thống đang hỏng. Đã thêm `graph/baseline.py`.
2. **API 413 với Groq** — prompt agent khoảng 6000 token cộng `max_tokens=4000` vượt trần 8000 token/phút. Khác 429 ở chỗ chờ bao lâu cũng không hết. Đã tự hạ `max_tokens` khi gặp 413, và đổi mặc định sang `--provider openai`.
3. **So chuỗi lượng CPU** — Kubernetes chuẩn hoá `"0.4"` thành `"400m"`, nên hành động thành công bị báo là thất bại. Đã thêm `cpu_to_millicores()`.

**Nợ mang sang phase 6:**

- **Tự động dọn hậu quả của agent sau mỗi ca.** `inject.py --revert` chỉ hoàn tác thứ nó tiêm, không biết gì về những gì agent đã đổi (số bản sao, trần CPU). Chạy hàng loạt mà không dọn thì mỗi ca bắt đầu từ trạng thái khác ca trước. `ActionResult` đã lưu sẵn `undo_kind` và `undo_args` nên chỉ cần gọi `ActionExecutor.undo()` ở cuối mỗi ca.
- **XAI chọn sai hành động cho S1** — `scale_up` không gỡ được độ trễ chèn mỗi lần gọi. Khớp số đo phase 3 (S1 hành động đúng chỉ 20–40%). Quy tắc sửa nằm trong gói prompt v5 đã bị loại vì làm tổng thể tệ đi; phase 6 phải tách ra thử riêng từng quy tắc.
- Hành động vô ích **không trung tính**: sau `scale_up` ở ca S1, số cạnh chậm tăng từ 5 lên 15.

---

## Phase 6 — Thí nghiệm và viết (2 tuần)

Mục tiêu: bảng số liệu so ba chế độ, đủ để bảo vệ giả thuyết ở mục 0 KLTN.md.

1. `src_thesis/eval/runner.py` — chạy vòng lặp 3 chế độ (baseline, direct, twin-verified) × 5 kịch bản × 5 lần = 75 ca. Mỗi ca: khôi phục trạng thái nền, tiêm lỗi, chạy chế độ tương ứng, đo, hoàn tác.
2. Bổ sung `src_thesis/eval/metrics.py` đủ 7 chỉ số mục 8. Quan trọng nhất là chỉ số 4 (harmful action count) — định nghĩa rõ ràng bằng số trước khi chạy: action nào làm error rate tăng hoặc p95 latency tăng quá 20% so với trước khi thực hiện thì tính là harmful.
3. Chạy thử 5 ca trước để ước lượng thời gian và tiền API, rồi mới chạy đủ 75 ca. Chạy bằng model rẻ.
4. Vẽ đồ thị bằng matplotlib từ `data/runs/`: MTTR ba chế độ, harmful action ba chế độ, twin fidelity.
5. Viết `docs/thesis-notes.md` thành chương kết quả: giả thuyết, số liệu, và phần thảo luận về trade-off giữa harmful action ít hơn và MTTR lâu hơn.

Thành công khi: có bảng số cho thấy twin-verified có harmful action thấp hơn direct, và MTTR cao hơn. Nếu số liệu **không** cho thấy như vậy thì vẫn báo cáo trung thực và giải thích vì sao — đó vẫn là kết quả khoa học.

---

### Tình trạng: PHASE 6 — CODE XONG, CHƯA CHẠY CA NÀO (2026-08-24)

**Đã có:** `src_thesis/faults/library.py`, `src_thesis/eval/preflight.py`,
`src_thesis/eval/runner.py`, phần bổ sung chỉ số 3–7 trong `src_thesis/eval/metrics.py`,
`scripts/eval_run.py`, `scripts/plot_results.py`.

**Lệnh chạy:**

```
python -u scripts/eval_run.py --limit 5
python -u scripts/eval_run.py
python -u scripts/eval_run.py --resume <ma-phien>
python scripts/plot_results.py <ma-phien>
```

**Ngưỡng chỉ số 4 và 5 đã chốt trước khi chạy:** error rate tăng từ 2 điểm phần trăm
hoặc p95 tăng từ 20% thì tính là harmful. Dưới 0.3 req/s thì không kết luận gì —
`unknown` tách riêng khỏi `wasted`.

**Đã trả món nợ dọn dẹp của phase 5:** runner tự hoàn tác hành động của agent trước,
rồi mới hoàn tác lỗi đã tiêm, theo thứ tự ngược chiều tác động.

**Hai lỗi bắt được lúc viết code, chi tiết ở `docs/thesis-notes.md`:** cache của LLM
sẽ làm độ lệch chuẩn ra 0 giả tạo nếu không tắt; hàm đọc twin fidelity đếm nhầm
`trials` (12 dòng cho 6 phép thử) và in ra 0.0% thay vì 100%.

**Ước lượng thời gian, từ số đo thật của phase 5:** một ca `twin_verified` khoảng 31
phút, `direct` khoảng 14 phút, `baseline` khoảng 26 phút. Đủ 75 ca vào khoảng 25 giờ
máy, chia nhiều buổi được nhờ `--resume`. Tiền API dưới 1 đô la với `gpt-4.1-mini`.

**Còn nợ trước khi chạy đủ:** XAI chọn sai hành động cho S1 — quy tắc sửa nằm trong
gói prompt v5 đã bị loại, phải tách ra thử từng quy tắc một.

---

### Tình trạng: PHASE 6 v1 XONG, v2 ĐANG CHẠY (cập nhật 2026-11-09)

Mục "CODE XONG, CHƯA CHẠY CA NÀO" ở trên đã cũ. Thực tế:

- **v1 chạy xong** trên máy ảo GCP: phiên chính `20261027-094812` (72 ca) và thí nghiệm
  phụ `20261029-030608` (15 ca), tag `phase-6-done`. Số liệu cuối ở mục "Số liệu cuối"
  của `docs/thesis-notes.md`. Giả thuyết trung tâm KHÔNG được ủng hộ (gây hại twin 10,
  direct 9); nguyên nhân là cổng vào twin bỏ sót hành động mức thấp và ảnh chụp thiếu
  bằng chứng thay đổi cấu hình.
- **v2** sửa đúng hai nguyên nhân đó, đăng ký trước ở `docs/v2-dang-ky-truoc.md`: lịch sử
  thay đổi cấu hình trong ảnh chụp, `rollback` quay về phiên bản thật, prompt v7, mọi
  hành động đổi cấu hình qua twin, hai kịch bản giữ lại H1, H2, và thí nghiệm thời
  lượng quan sát trên twin (120 / 300 / 600 giây).
- Kiểm: `tests/test_guards.py` 22/22, `tests/test_v2.py` 14/14, `scripts/v2_check.py` trên
  cluster thật. Chạy: `scripts/v2_pipeline.sh` trong tmux. Phân tích:
  `scripts/v2_analyze.py`, độ nhạy: `scripts/rescore_wait.py`.

## Tổng thời gian

Phase 0 một tuần, phase 1 và 3 và 4 mỗi cái một tuần rưỡi, phase 2 và 5 mỗi cái một tuần, phase 6 hai tuần. Cộng lại khoảng 9,5 tuần nếu không tắc chỗ nào. Phase 0 bước 0.4 là chỗ dễ trượt tiến độ nhất, đặt hạn cứng một tuần cho nó.
