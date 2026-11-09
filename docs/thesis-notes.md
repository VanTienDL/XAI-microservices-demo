# Sổ tay khóa luận

Ghi lại mọi thứ cần cho chương kết quả. Ghi ngay lúc làm, đừng để cuối kỳ nhớ lại.

## Trạng thái nền của hệ thống

Dùng để đối chiếu mỗi lần tiêm lỗi và mỗi lần dựng twin.

- Ngày đo: 2026-08-09, sau bước 0.3
- Cluster: kind `boutique`, 1 node, Kubernetes v1.36.1
- Đang chạy: 12 pod Online Boutique, có loadgenerator bơm traffic, chưa có tracing và chưa có Prometheus
- **Tổng CPU: 98m** (khoảng 0.1 lõi)
- **Tổng RAM: 341Mi**

RAM từng pod (`kubectl top pods`):

```
NAME                      CPU(cores)   MEMORY(bytes)
adservice                 5m           86Miâucartservice               10m          51Mi
checkoutservice           2m           6Mi
currencyservice           12m          33Mi
emailservice              4m           36Mi
frontend                  27m          11Mi
loadgenerator             6m           47Mi
paymentservice            2m           21Mi
productcatalogservice     14m          8Mi
recommendationservice     10m          31Mi
redis-cart                4m           5Mi
shippingservice           2m           6Mi
```

Nhận xét: 341Mi thấp hơn nhiều so với 2.5GB dự trù cho Online Boutique ở mục 2 KLTN.md. Đây là RAM *đang dùng*, chưa tính phần Java và .NET giữ sẵn trong heap, nhưng vẫn cho thấy ngân sách RAM rộng hơn dự kiến. Chỉ số này chỉ có ý nghĩa khi so với lần đo sau khi đã bật đủ tracing và Prometheus (bước 0.6) — đó mới là con số nền thật để trừ ra phần còn lại cho twin.

`adservice` chiếm 86Mi, nhiều nhất, đúng như dự đoán vì nó chạy JVM. Đây là một lý do nữa để loại nó khỏi twin (mục 4 KLTN.md).

- p95 latency luồng đặt hàng khi khỏe mạnh: chưa đo được, chờ bước 0.5
- Error rate khi khỏe mạnh: chưa đo được, chờ bước 0.5

## Nhật ký từng phase

Mỗi mục ghi: làm gì, tắc ở đâu, sửa thế nào, mất bao lâu.

### Phase 0 — Hạ tầng

**0.4 xong ngày 2026-08-09.** Jaeger nhận trace từ 7 service. Ô Service trong Jaeger hiện 8 mục vì Jaeger tự ghi trace về chính nó.

Kiểm chứng được cạnh tới `cartservice` qua span client của `frontend`. Tag thật của span đó:

```
rpc.method               hipstershop.CartService/GetCart
rpc.system.name          grpc
rpc.response.status_code OK
server.address           10.96.119.247
server.port              7070
span.kind                client
otel.scope.name          go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc
otel.scope.version       0.69.0
```

Hai điều khác dự đoán ban đầu, đã sửa lại trong KLTN.md mục 4:

- Không có tag `rpc.service`. Tên gRPC nằm trong `rpc.method`, dạng `package.Service/Method`.
- `server.address` là ClusterIP, không phải tên service. Đối chiếu `kubectl get svc` xác nhận `10.96.119.247:7070` đúng là `cartservice`.

Hệ quả cho phase 1: `runtime_graph.py` phải dựng bảng tra ClusterIP → tên service ở mỗi lần snapshot, không được hardcode IP, vì IP đổi khi Service bị tạo lại và namespace `twin` có dải IP riêng.

**0.5 xong ngày 2026-08-09.** spanmetrics chạy, endpoint `localhost:8889/metrics` cho 629 dòng, trong đó 464 dòng có nhãn `server_address`.

Ví dụ đã kiểm chứng, span client của checkoutservice gọi sang cartservice:

```
traces_span_metrics_calls_total{job="checkoutservice", rpc_method="hipstershop.CartService/EmptyCart",
  server_address="10.96.119.247", server_port="7070", service_name="checkoutservice",
  span_kind="SPAN_KIND_CLIENT", status_code="STATUS_CODE_UNSET"} 19
```

Đây là bằng chứng đo được `cartservice` dù nó không phát trace.

**Nhưng độ phủ nhãn không đồng đều giữa các ngôn ngữ.** Span client của `recommendationservice` (Python) **không có** `server_address`, chỉ có `span_name="/hipstershop.ProductCatalogService/ListProducts"`:

```
traces_span_metrics_calls_total{job="recommendationservice", rpc_method="ListProducts",
  service_name="recommendationservice", span_kind="SPAN_KIND_CLIENT",
  span_name="/hipstershop.ProductCatalogService/ListProducts", ...} 471
```

Các service Go dùng semconv mới nên có `server_address`; thư viện Python dùng quy ước cũ nên không. Nghĩa là **bắt buộc phải có cả hai quy tắc** đã ghi ở mục 4 KLTN.md: tra IP là chính, tách tên gRPC từ `rpc_method` hoặc `span_name` là dự phòng. Không được chỉ làm một cái.

Chi tiết nhỏ cần nhớ khi viết truy vấn PromQL: nhãn `rpc_method` không đồng nhất — service Go ghi cả đường dẫn `hipstershop.CartService/EmptyCart`, service Node.js và Python chỉ ghi tên hàm `Convert`, `ListProducts`. Muốn nhóm theo service đích thì dựa vào `server_address` trước, `span_name` sau.

**0.6 xong ngày 2026-08-23.** Cài `kube-prometheus-stack` chart 88.5.3 (Prometheus operator v0.93.1) bằng Helm 4.2.4, vào namespace riêng `monitoring`, cấu hình ở `infra/prometheus-values.yaml`.

13/13 mục tiêu scrape đều UP, trong đó có `opentelemetrycollector` tại `10.244.0.15:8889` — đây là đường RED metrics chảy từ trace vào Prometheus.

RAM của bộ giám sát (`kubectl top pods -n monitoring`), tổng khoảng 0.71 GB:

```
mon-grafana                           524Mi
prometheus-...-prometheus-0           105Mi
mon-kube-state-metrics                 42Mi
mon-kube-prometheus-stack-operator     40Mi
mon-prometheus-node-exporter            3Mi
```

Kiểm chứng truy vấn: `traces_span_metrics_calls_total` cho 6 rồi 7 service, `container_memory_working_set_bytes{namespace="default"}` cho 42 chuỗi.

**Một bài học quan trọng cho phase 3.** Lần đo đầu tiên chỉ thấy 6 service, thiếu `emailservice`. Không phải lỗi cấu hình: `emailservice` chỉ được gọi khi một đơn hàng hoàn tất, mà loadgenerator đặt đơn thưa, nên sau mỗi lần collector khởi động lại phải chờ vài phút mới có chuỗi metric đầu tiên. Jaeger vẫn liệt kê đủ 7 service suốt thời gian đó.

Hệ quả: **vắng metric không có nghĩa là service đã chết.** Prompt của XAI ở phase 3 bắt buộc phải phân biệt hai trường hợp "service không có lưu lượng" và "service ngừng phục vụ", nếu không LLM sẽ kết luận sai ngay ở những ca dễ nhất. Cách phân biệt: đối chiếu với `kube_deployment_status_replicas_available` từ kube-state-metrics, và với danh sách pod lấy qua `k8s_client.py`.

**0.7 xong ngày 2026-08-23. PHASE 0 KẾT THÚC.**

`src_thesis/k8s_client.py` chạy được trên môi trường ảo `.venv`, thư viện `kubernetes` 36.0.3. `scripts/smoke_k8s.py` tắt `cartservice` xuống 0 rồi bật lại về 1 thành công, cluster trở về nguyên trạng.

Một tác dụng phụ có ích: lúc `cartservice` tắt, spanmetrics sinh ra chuỗi `status_code="STATUS_CODE_ERROR"` cho span client của `checkoutservice` gọi `hipstershop.CartService/GetCart`. Đây là bằng chứng **đo được lỗi của service không phát trace**, qua đúng con đường đã thiết kế. Ghi lại vì phase 2 sẽ dựa vào tín hiệu này để chấm điểm F2.

Tên metric chính xác để viết PromQL ở phase 1:

```
traces_span_metrics_calls_total                     (counter)
traces_span_metrics_duration_milliseconds_bucket    (histogram)
traces_span_metrics_duration_milliseconds_sum
traces_span_metrics_duration_milliseconds_count
```

Nhãn `status_code` chỉ có hai giá trị: `STATUS_CODE_UNSET` khi bình thường và `STATUS_CODE_ERROR` khi lỗi. Không có `STATUS_CODE_OK`, nên công thức tỉ lệ lỗi phải lấy `ERROR` chia cho tổng, đừng lọc theo `OK`.

### Phase 1 — Quan sát và mô hình hóa

**Ngày 2026-08-23 — xong 2/8 việc: `prometheus_client.py` và `jaeger_client.py`.**

Đo thử trên hệ thống khỏe mạnh, `scripts/smoke_telemetry.py` cho kết quả:

```
RED phia server        RED phia client (do gian tiep)
productcatalogservice  13.7/s   productcatalogservice  13.8/s
currencyservice         7.5/s   currencyservice         7.6/s
frontend                2.8/s   cartservice             2.5/s
recommendationservice   1.8/s   adservice               1.4/s
checkoutservice         0.11/s  shippingservice         0.81/s
```

Đủ 10 service có số liệu. Ba service không phát trace là `cartservice`, `shippingservice`, `adservice` đều đo được qua span client của người gọi, đúng như thiết kế ở mục 4 KLTN.md. `redis-cart` vẫn là điểm mù duy nhất.

`jaeger_client.py` lấy 266 span từ 20 trace: 180 span client (166 có `server.address`, 14 không — toàn bộ của `recommendationservice` viết bằng Python) và 86 span server. Tỉ lệ này xác nhận phải giữ cả hai quy tắc dựng cạnh.

**Ba lỗi đã vấp, ghi lại để khỏi vấp lại.**

1. *Mốc chia histogram quá thô.* Ban đầu mốc nhỏ nhất là 5ms, trong khi hầu hết service trả lời dưới 5ms, nên mọi service đều ra `p50=2.5ms, p95=4.75ms` giống hệt nhau — con số vô dụng để so sánh trước và sau khi tiêm lỗi. Đã thêm mốc 0.5ms, 1ms, 2ms, 3ms vào `infra/tracing-local/otel-collector.yaml`. Sửa xong p50 phân hóa thành 3.3 đến 19.1ms.
2. *Nhiễu làm sai lưu lượng.* Lệnh kiểm tra sức khỏe `grpc.health.v1.Health/Check` chạy vài giây một lần và span `opentelemetry.proto...TraceService/Export` do chính SDK sinh ra khi gửi telemetry, cả hai đều bị đếm thành lưu lượng nghiệp vụ. Riêng cái thứ hai còn làm `opentelemetrycollector` hiện ra như một service của hệ thống. Đã lọc bằng hằng số `NOISE` trong `prometheus_client.py`.
3. *Cú pháp PromQL.* Viết `span_name!~".*grpc\.health\..*"` làm Prometheus trả lỗi 400 `unknown escape sequence U+002E '.'` — trong chuỗi nháy kép của PromQL, `\.` không phải chuỗi thoát hợp lệ. Cách gọn nhất là để dấu chấm trần, vì `.` khớp mọi ký tự nên vẫn đúng mục đích.

**Cảnh báo RAM cần theo dõi.** Pod `jaeger` đã lên 477Mi và còn tăng, vì Jaeger all-in-one giữ trace trong RAM với hạn mức `MEMORY_MAX_TRACES=50000`. Với ngân sách 6GB thì trước khi vào phase 4 nên hạ xuống 20000 trong `infra/jaeger-all-in-one.yaml`.

**PHASE 1 XONG ngày 2026-08-23.** Đủ 8 file: `prometheus_client.py`, `jaeger_client.py`, `naming.py`, `graph/model.py`, `runtime_graph.py`, `logical_graph.py`, `diff.py`, `serialize.py`, `snapshot.py`.

**Cổng chặn đã đạt.** Chạy `scripts/smoke_snapshot.py` trên hệ thống khỏe mạnh: dựng được 14 cạnh, khớp đúng sơ đồ thiết kế, `DEVIATIONS = none`. Không có cạnh thừa, không có cạnh thiếu, không báo nhầm.

Cách dựng 14 cạnh đó: 6 cạnh từ span server (chắc chắn nhất), 8 cạnh từ tra ClusterIP. Nghĩa là **hơn một nửa graph phụ thuộc vào quy tắc tra IP** — bỏ quy tắc này thì mất `cartservice`, `shippingservice`, `adservice` và cả vài cạnh của `checkoutservice`.

Đoạn text đưa cho LLM dài 4343 ký tự, khoảng 1085 token. Nhân với trần 3 vòng lặp mỗi ca là khoảng 3300 token đầu vào mỗi ca — con số này dùng để ước tính chi phí ở mục 7.5 KLTN.md.

**Phát hiện đáng viết vào báo cáo: Online Boutique có lỗi đứt ngữ cảnh trace.**

Ban đầu diff báo thiếu đúng một cạnh `checkoutservice -> currencyservice`. Truy ra nguyên nhân ở `src/checkoutservice/main.go` dòng 360: hàm `convertCurrency` gọi `Convert(context.TODO(), ...)` thay vì truyền `ctx` nhận được từ request. `context.TODO()` là ngữ cảnh rỗng, không mang theo mã trace, nên span đó không nối vào trace của đơn hàng mà trở thành một trace mồ côi riêng lẻ.

Hệ quả về cách lấy dữ liệu: **không được chỉ lấy trace theo đường vào từ `frontend`**. Phải gọi `recent_spans_all()` để gom từ mọi service rồi khử trùng theo cặp (trace_id, span_id). Sau khi sửa, cạnh xuất hiện với 31 lần gọi.

Đây là ví dụ thật rất hợp để đưa vào báo cáo: chính phép so lệch thiết kế với thực tế đã phát hiện ra một khiếm khuyết quan sát trong hệ thống nghiên cứu, đúng loại tín hiệu mà XAI được thiết kế để dùng.

**Hai lỗi hạ tầng đã vấp.**

1. *Jaeger bị OOMKilled 7 lần.* `MEMORY_MAX_TRACES=50000` với trần RAM 512Mi là quá tay — mỗi trace hơn 20 span. RAM leo tới 477Mi rồi pod chết, script Python đang chạy thì nhận `RemoteDisconnected`. Đã hạ xuống 10000 trong `infra/jaeger-all-in-one.yaml`; sau khi sửa Jaeger đứng ở 173Mi.
2. *Số lần khởi động lại là con số cộng dồn.* Ban đầu `describe_pods` liệt kê pod bất thường theo điều kiện `restarts > 0`, kết quả là cả 11 pod đều bị báo bất thường vì số này tích lũy qua mỗi lần tắt mở cluster. XAI đọc vào sẽ thấy đâu cũng có lỗi. Đã thêm trường `last_restart_age_s` vào `PodInfo` và chỉ báo pod vừa khởi động lại trong 10 phút gần đây — đó mới là chữ ký của lỗi F3.

**Nhiễu lỗi nền của luồng thanh toán — quyết định về phương pháp thí nghiệm.**

Cổng chặn phase 1 thoạt đầu báo đỏ với `checkoutservice -> paymentservice` lỗi 2/21 lần gọi. Không phải lỗi code. Log `paymentservice` ghi:

```
UnacceptedCreditCard [Error]: Sorry, we cannot process visa_electron credit cards.
Only VISA or MasterCard is accepted.
```

`loadgenerator` sinh số thẻ ngẫu nhiên bằng `fake.credit_card_number(card_type="visa")` ([src/loadgenerator/locustfile.py:68](../src/loadgenerator/locustfile.py#L68)). Thư viện Faker đôi khi sinh số rơi vào dải Visa Electron, mà `paymentservice` chỉ nhận VISA và MasterCard. Vậy hệ thống có tỉ lệ đơn hàng thất bại **nền** ngay cả khi hoàn toàn khỏe mạnh.

Đo được: 3.64% trên cửa sổ 10 phút, 1.27% trên 30 phút, 0.60% trên 1 giờ, và 9.5% trong một mẫu 5 phút cụ thể. Dao động lớn vì lưu lượng đặt hàng chỉ khoảng 4 đơn mỗi phút.

Một chi tiết kỹ thuật đáng nhớ: lỗi này **chỉ thấy được ở span client** của `checkoutservice`. Span server của `paymentservice` (viết bằng Node.js) không đặt trạng thái lỗi, nên đo theo phía server sẽ ra tỉ lệ lỗi gần bằng 0 và bỏ sót hoàn toàn.

*Quyết định: loại nhiễu tại nguồn.* Ghi đè `/loadgen/locustfile.py` bằng ConfigMap (`infra/loadgenerator-locustfile.yaml` gắn qua `subPath`), thay số thẻ ngẫu nhiên bằng một số VISA cố định hợp lệ `4432801561520454` — chính là số mặc định trên form thanh toán của web. Không đụng vào 11 service, không dựng lại image.

Lý do chọn cách này thay vì chấp nhận nhiễu: chỉ số 4 ở mục 8 KLTN.md là **harmful action count**, tức đếm hành động của agent làm hệ thống xấu đi. Với nền dao động từ 0 tới 9.5%, muốn khẳng định "hệ thống xấu đi" phải chạy rất nhiều lần lặp mới tách được tín hiệu khỏi nhiễu. Đưa nền về 0 thì mọi lỗi quan sát được đều do mình tiêm hoặc do agent gây ra.

**Phải ghi rõ trong báo cáo** rằng bộ sinh tải đã được sửa, kèm lý do trên. Đây là can thiệp vào thiết lập thí nghiệm, không phải vào hệ thống nghiên cứu, nhưng vẫn phải khai báo.

**Sửa kèm theo trong `diff.py`:** thêm `MIN_ERRORS = 2` và so sánh tương đối với baseline. Một lỗi lẻ trong cửa sổ 5 phút không còn đủ để báo động, vì mẫu quá nhỏ khiến một lỗi đã thành 5%.

**Cổng chặn phase 1 đạt hai lần liên tiếp** (`data/runs/20260823-112036_baseline-clean.json` và `20260823-112419_baseline-clean.json`), cách nhau vài phút, sau khi đã bỏ nhiễu số thẻ. Chạy hai lần là có chủ đích: lần đầu đạt có thể chỉ vì rơi trúng cửa sổ sạch, đúng như chuyện đã xảy ra trước đó.

Trạng thái nền sạch để đối chiếu suốt phase 2:

```
14 canh, 6 tu span server + 8 tu tra ClusterIP
0% loi tren moi canh
p95: productcatalog 0.48ms, currency 0.48ms, frontend ~92ms, checkout ~45ms
snapshot dai ~900 token
```

### Phase 2 — Tiêm lỗi

**Ngày 2026-08-23 — dựng xong bộ tiêm lỗi và kiểm chứng kịch bản S2.**

Ba file: `src_thesis/faults/injectors.py` (4 injector F1-F4), `src_thesis/faults/scenarios.yaml` (6 kịch bản), `scripts/inject.py` (công cụ dòng lệnh).

Cơ chế an toàn: trạng thái cũ ghi ra `data/runs/active_fault.json` **trước** khi phá, nên hoàn tác được cả sau khi đóng terminal hay khởi động lại máy. Đã kiểm chứng trọn vòng tiêm rồi hoàn tác, `currencyservice` trở về 1 bản chạy và snapshot sạch trở lại.

**Phát hiện 1: F2 KHÔNG tạo ra `missing_edges` như dự đoán ban đầu, mà tạo ra `error_edges`.**

Đo thật khi tắt `currencyservice`:

```
checkoutservice -> currencyservice: 58.8% loi (10/17), luc khoe manh 0.0%
frontend -> checkoutservice:        58.8% loi (10/17), luc khoe manh 0.0%  [ON CRITICAL PATH]
frontend -> currencyservice:        26.5% loi (52/196), luc khoe manh 0.0%
```

Lý do: cạnh trong runtime graph dựng từ span **phía người gọi**. Service đích chết thì người gọi vẫn tạo span, chỉ là span mang trạng thái lỗi. Cạnh vẫn tồn tại nên không bị coi là mất.

`missing_edges` thật ra là chữ ký của một loại hỏng khác: người gọi **ngừng gọi hẳn**, ví dụ chính người gọi chết, hoặc một nhánh nghiệp vụ không còn được kích hoạt. Đã sửa lại `expected_symptom` của S2 và S6 trong `scenarios.yaml`.

Chi tiết đẹp để đưa vào báo cáo: tỉ lệ lỗi của `frontend -> checkoutservice` **bằng đúng** tỉ lệ của `checkoutservice -> currencyservice` (cùng 10/17). Đường lan truyền lỗi hiện ra bằng con số, không phải suy đoán — đúng thứ `propagation_path` ở mục 7.2 cần.

**Phát hiện 2: phải chờ LÂU HƠN cửa sổ quan sát, không phải ngắn hơn.**

Chờ 180 giây rồi đo thì cửa sổ 5 phút vẫn chứa 120 giây dữ liệu lúc hệ thống còn khỏe. Hậu quả: `currencyservice` vẫn hiện `2.66 req/s, 0.0% errors` trong bảng metric dù đã tắt hẳn 3 phút — nhìn vào dễ tưởng nó còn sống. Đã nâng `wait_after_inject_s` lên 330 giây cho các kịch bản lỗi kéo dài, giữ 120 giây cho S3 vì lỗi đó chỉ kéo dài 30 giây rồi tự khỏi.

**Hệ quả cho kế hoạch phase 6:** 3 chế độ x 5 kịch bản x 5 lần x khoảng 6 phút mỗi ca là **khoảng 7 tiếng rưỡi chạy liên tục**. Phải tính vào lịch, không để sát hạn nộp.

**Lỗi lặt vặt đã sửa:** console Windows dùng bảng mã cp1252 nên `print` tiếng Việt làm script chết với `UnicodeEncodeError`. Đã thêm `sys.stdout.reconfigure(encoding="utf-8")` vào đầu mọi script trong `scripts/`.

**Kịch bản S3 lần chạy đầu — THẤT BẠI, không tạo ra một triệu chứng nào.**

Cả hai snapshot `20260823-150504_S3-truoc.json` và `20260823-150741_S3-sau.json` đều có phần diff rỗng hoàn toàn. Cạnh `frontend -> checkoutservice` cho 0 lỗi trên 18 lần gọi.

Hai nguyên nhân độc lập, cả hai đều là lỗi thiết kế chứ không phải của hệ thống:

*Nguyên nhân 1 — xóa pod KHÔNG tạo ra "khởi động lại".* Nó tạo ra một **pod mới hoàn toàn**: tên đổi từ `checkoutservice-777dff667-z29h8` sang `checkoutservice-777dff667-rtpn4`, `restarts: 0`, `last_restart_age_s: None`. Trường `last_restart_age_s` đọc từ `lastState.terminated`, mà pod mới sinh thì không có `lastState`. Nghĩa là cách phát hiện dựng ở phase 1 **không bao giờ nhìn thấy được F3**.

Sửa: thêm trường `age_s` (tuổi pod, đọc từ `creationTimestamp`) vào `PodInfo`, và `describe_pods` báo `pod was RECREATED ... ago` cho pod trẻ hơn 10 phút. Tuổi pod mới là chữ ký đúng của F3.

*Nguyên nhân 2 — xóa pod mặc định là xóa NHẸ NHÀNG.* Kubernetes cho 30 giây ân hạn để service đóng kết nối gọn gàng. Mục 6 KLTN.md ghi F3 là "pod chết đột ngột", nên phải truyền `grace_period_seconds=0`. Đã sửa trong `k8s_client.delete_pod`.

*Một điều không sửa được và phải chấp nhận:* `checkoutservice` chỉ được gọi khoảng 4 lần mỗi phút, nên khoảng trống 30 giây rất dễ rơi vào lúc không có đơn hàng nào. Tỉ lệ lỗi bằng 0 là chuyện bình thường với kịch bản này, không phải dấu hiệu injector hỏng. Tín hiệu đáng tin duy nhất của S3 là tuổi pod.

**Bài học chung, đáng viết vào báo cáo:** một lỗi có thể hoàn toàn vô hình với hệ thống quan sát nếu chọn sai đại lượng để đo. Ở đây đo số lần khởi động lại thay vì tuổi pod là đủ để bỏ sót trọn vẹn một loại sự cố. Đây chính là lý do phase 2 phải chạy tay từng kịch bản và đối chiếu với triệu chứng mong đợi trước khi tin vào bất cứ số liệu nào của phase 6.

**S3 chạy lại sau khi sửa — ĐẠT.** `20260823-152522_S3-sau.json`.

```
POD HEALTH:
  checkoutservice-777dff667-m4v87: pod was RECREATED 156s ago (previous pod is gone)
DEVIATIONS: 1 slow_edge, 0 error_edge
frontend -> checkoutservice: 0/15 loi
```

Tuổi pod bắt được sự cố, đúng như dự đoán sau khi sửa. Tỉ lệ lỗi vẫn bằng 0 — khẳng định lại rằng với `checkoutservice` chỉ 4 lần gọi mỗi phút thì khoảng trống 30 giây thường không trúng đơn nào. **Tín hiệu duy nhất đáng tin của S3 là tuổi pod.**

**Jaeger OOMKilled lần thứ ba, phải hạ tới 3000 trace.** Chuỗi hạ dần: 50000 → 10000 → 3000. Tính ngược từ lần chết thứ hai thì mỗi trace tốn khoảng 50KB, nên 10000 trace vẫn vượt trần 512Mi. 3000 trace chứa được 17 phút lưu lượng, thừa cho cửa sổ đo 5 phút.

Kèm theo, `JaegerClient` giờ thử lại 3 lần cách nhau 5 giây khi mất kết nối. Pod tự sống lại sau vài giây nên thử lại là qua được. Bắt buộc phải có trước phase 6: lượt chạy 7 tiếng không được phép chết vì một cú chớp.

**Một điểm nhiễu cần biết:** `POD HEALTH` cũng báo pod `jaeger` vừa được tạo lại, vì vừa khởi động lại nó. Các pod hạ tầng (`jaeger`, `opentelemetrycollector`) không thuộc hệ thống nghiên cứu, nhưng vẫn lọt vào đoạn text đưa cho LLM. Nếu ở phase 3 thấy XAI đổ lỗi cho hạ tầng thì đây là chỗ phải lọc.

**S1 (F1, độ trễ 6s vào productcatalogservice) — ĐẠT, khớp hoàn toàn triệu chứng mong đợi.**

```
SLOW calls:
  checkoutservice -> productcatalogservice: 6002.58ms, nen 0.83ms  (gap 7232 lan) [CRITICAL]
  frontend -> productcatalogservice:        6001.19ms, nen 1.24ms  (gap 4840 lan) [CRITICAL]
  frontend -> checkoutservice:              6020.67ms, nen 84.62ms (gap 71 lan)   [CRITICAL]
  frontend -> recommendationservice:        6005.64ms, nen 5.16ms  (gap 1164 lan)
  recommendationservice -> productcatalogservice: 6002.4ms, nen 2.08ms (gap 2886 lan)
p95 frontend: 30000ms (cham tran do)
```

Con số "chậm gấp bao nhiêu lần" chỉ có được nhờ truyền ảnh nền vào làm baseline. Không có baseline thì chỉ biết 6002ms là vượt ngưỡng 500ms, mất hẳn thông tin service này vốn trả lời trong 0.83ms.

**Hiệu ứng phụ quan trọng: lỗi độ trễ làm sinh ra `missing_edges` giả.**

Lưu lượng sụp từ 13.7 xuống 1.47 request mỗi giây. Diff báo thêm `MISSING calls: checkoutservice -> currencyservice`. Cạnh đó không mất thật — chỉ là hệ thống chậm tới mức gần như không đơn hàng nào hoàn tất trong cửa sổ 5 phút, nên cạnh không xuất hiện.

Hệ quả cho phase 3: prompt phải nói rõ cho LLM rằng **khi thông lượng sụp thì cạnh vắng mặt không đồng nghĩa với cạnh hỏng**. Nếu không, XAI sẽ báo hai nguyên nhân gốc cho một lỗi duy nhất, làm hỏng chỉ số root cause accuracy.

**Lỗi nghiêm trọng đã phát hiện và sửa: `unset_env` không hoàn tác được.**

Hoàn tác S1 thất bại với lỗi 422:

```
Deployment.apps "productcatalogservice" is invalid:
spec.template.spec.containers[0].image: Required value
```

Nguyên nhân: `unset_env` dùng `application/merge-patch+json`, mà kiểu vá đó **thay nguyên mảng** `containers` bằng đúng thứ mình gửi lên. Mảng gửi lên chỉ có `name` và `env`, nên container mất trường `image` và Kubernetes từ chối.

Hậu quả nguy hiểm hơn nhiều so với một lỗi cú pháp thông thường: **lỗi vẫn nằm nguyên trên hệ thống trong khi mình tưởng đã hoàn tác xong.** Nếu không đọc kỹ dòng lỗi mà cứ chạy tiếp kịch bản sau thì mọi số liệu từ đó về sau đều sai.

Sửa: dùng JSON Patch (RFC 6902) với thao tác `replace` nhắm đúng đường dẫn `/spec/template/spec/containers/{idx}/env`. JSON Patch chỉ đụng đúng chỗ được chỉ định, phần còn lại giữ nguyên. Đã kiểm chứng: sau khi hoàn tác, `EXTRA_LATENCY` biến mất và bốn biến môi trường còn lại vẫn nguyên vẹn.

Bài học: **hàm hoàn tác phải được kiểm chứng riêng, không được tin là nó chạy đúng chỉ vì hàm tiêm lỗi chạy đúng.** Mục 5 KLTN.md yêu cầu mọi hành động có hàm nghịch đảo, nhưng viết ra chưa đủ — phải chạy thử từng cái.

**S5 (F4, nghẹt CPU 10m vào productcatalogservice) — ĐẠT, và cho ra dấu hiệu phân biệt quan trọng nhất của phase 2.**

So sánh trực tiếp hai kịch bản cùng nhắm một service:

```
                canh cham (phia goi)  thong luong        p95 phia server
S1 do tre 6s    ~6002ms               sup 13.7 -> 1.47/s  9750ms
S5 nghen CPU    ~90-102ms             giu nguyen 13.56/s  0.48ms
```

**Điểm phân biệt nằm ở p95 phía server.** Ở S5, `productcatalogservice` báo `p95 0.48ms`, tức xử lý vẫn nhanh như lúc khỏe mạnh, trong khi cạnh nhìn từ người gọi là 90ms. Chênh lệch đó là thời gian request **nằm chờ trong hàng đợi** trước khi được cấp CPU — span phía server không đo được vì nó chỉ tính từ lúc bắt đầu xử lý.

Ở S1 thì cả hai phía đều chậm, vì service thật sự ngủ 6 giây ngay trong lúc xử lý.

Đây là dấu hiệu để XAI phân biệt "service chậm do chính nó" với "service chậm do thiếu tài nguyên". Prompt ở phase 3 **phải** đưa cả hai con số và giải thích ý nghĩa chênh lệch, nếu không thì hai kịch bản này không thể phân biệt được và root cause accuracy sẽ mất một nửa số ca.

**Hai thiếu sót của snapshot lộ ra ở kịch bản này, phải sửa trong phase 3.**

1. *Bằng chứng nghẹt CPU không có trong prompt.* `describe_resources` sắp xếp theo RAM và chỉ in 8 pod nhiều nhất, nên `productcatalogservice` không lọt vào. Đoạn text đưa cho LLM hoàn toàn không có thông tin CPU của service đang bị nghi ngờ, cũng không có trần CPU để so. Phải bổ sung: với mỗi service bị đánh dấu chậm, in kèm CPU đang dùng và trần CPU.
2. *"Pod vừa được tạo lại" KHÔNG phải chữ ký riêng của F3.* S5 cũng làm pod được tạo lại (`pod was RECREATED 367s ago`), vì đổi trần CPU buộc Kubernetes thay pod. S1 cũng vậy khi đổi biến môi trường. Nghĩa là tuổi pod chỉ nói "có gì đó vừa thay đổi", không nói được thay đổi gì. XAI phải kết hợp nó với các dấu hiệu khác chứ không được kết luận F3 chỉ từ tuổi pod.

**Lỗi thứ hai của F4 đã sửa: không tiêm được vì ràng buộc CPU.**

Lần chạy đầu hỏng ngay ở bước tiêm:

```
spec.template.spec.containers[0].resources.requests:
Invalid value: "100m": must be less than or equal to cpu limit of 10m
```

Kubernetes bắt buộc lượng CPU xin trước phải nhỏ hơn hoặc bằng trần. Hạ trần xuống 10m mà để nguyên `requests: 100m` thì bị từ chối. Sửa: `set_cpu_limit` hạ cả hai trong **cùng một lần vá** (vá riêng lẻ cũng vi phạm ở bước trung gian), và thêm hàm `restore_cpu` trả cả hai về giá trị cũ. Đã kiểm chứng sau khi hoàn tác: `limits cpu 200m`, `requests cpu 100m`, phần memory không bị đụng tới.

**S4 (F4, nghẹt CPU vào frontend) — ĐẠT, cho ra dấu hiệu hình học ngược với S1 và S5.**

```
FAILING calls:
  frontend -> adservice: 48.8% loi (21/43)
SLOW calls: TAT CA 6 canh xuat phat tu frontend
  adservice 213ms | cartservice 183ms | currencyservice 245ms
  productcatalogservice 157ms | recommendationservice 255ms | shippingservice 158ms
p95 frontend: 4617ms
p95 phia server cua cac dich: productcatalog 0.48ms, currency 0.48ms, recommendation 5.67ms
```

**Quy tắc hình học rút ra, dùng được cho XAI:**

- Cạnh chậm **tụ về** một đỉnh (S1, S5: mọi cạnh đi tới `productcatalogservice`) → đỉnh đích là nguyên nhân.
- Cạnh chậm **tỏa ra** từ một đỉnh (S4: mọi cạnh đi ra từ `frontend`) → chính đỉnh nguồn là nguyên nhân.

Dấu hiệu phụ trợ giống S5: p95 phía server của các service đích vẫn nhanh, chứng tỏ chúng không có lỗi gì.

`frontend -> adservice` có thêm 48.8% lỗi vì frontend đặt hạn chờ ngắn cho quảng cáo, nghẹt CPU làm nó vượt hạn.

**Lỗi thứ ba đã phát hiện và sửa: kịch bản kép chỉ hoàn tác được một nửa.**

Phát hiện khi đọc lại code trước khi chạy S6, chưa kịp gây hậu quả. `ActiveFault.save()` ghi đè một object duy nhất vào `active_fault.json`. Kịch bản kép S6 tiêm hai lỗi liên tiếp, nên lỗi thứ hai **xóa mất dấu vết của lỗi thứ nhất**, và `--revert` chỉ gỡ được lỗi thứ hai. Lỗi F1 sẽ nằm im trên hệ thống trong khi script báo đã hoàn tác xong.

Sửa: `active_fault.json` giờ chứa một **danh sách**, `--revert` gỡ theo thứ tự ngược lại với lúc tiêm, và nếu một mục gỡ thất bại thì nó vẫn nằm lại trong file để thử lại. `--status` liệt kê tất cả.

Đây là lỗi thứ ba liên tiếp nằm ở phần hoàn tác, sau `unset_env` và `set_cpu_limit`. Ba lần đều chỉ lộ ra khi chạy thật.

**S6 (kép: F1 độ trễ + F2 tắt service) — ĐẠT, và hoàn tác đúng hai lỗi theo thứ tự ngược.**

```
DANG CO 2 LOI CHUA HOAN TAC:
  F1-productcatalogservice-latency
  F2-currencyservice-crash
-> go F2 truoc, roi F1. Da hoan tac xong tat ca.
```

Cả hai nguyên nhân đều hiện ra rõ ràng và không lẫn vào nhau:

```
FAILING calls (dau hieu cua F2):
  frontend -> currencyservice:        100% loi (39/39)
  checkoutservice -> currencyservice: 100% loi (7/7)
  frontend -> checkoutservice:        100% loi (8/8)  [CRITICAL]
SLOW calls (dau hieu cua F1):
  checkoutservice -> productcatalogservice: 6002ms, nen 0.8ms   (gap 7503 lan) [CRITICAL]
  frontend -> productcatalogservice:        6001ms, nen 14.38ms (gap 417 lan)  [CRITICAL]
  frontend -> checkoutservice:              6005ms, nen 85.25ms (gap 70 lan)   [CRITICAL]
```

**Nhưng lộ ra vấn đề nghiêm trọng nhất của cả phase 2: 7 cạnh bị báo mất một cách sai lệch.**

```
MISSING calls:
  checkoutservice -> emailservice, -> paymentservice [CRITICAL], -> shippingservice
  frontend -> adservice, -> recommendationservice, -> shippingservice
  recommendationservice -> productcatalogservice
```

Không cạnh nào trong số đó hỏng. Chúng vắng mặt vì thông lượng sụp: hệ thống chậm tới mức rất ít request hoàn tất trong cửa sổ 5 phút. Đưa nguyên xi cho LLM thì nó sẽ báo **bảy nguyên nhân gốc cho hai lỗi**, phá hỏng chỉ số root cause accuracy ở mục 8.

Hiện tượng này đã manh nha ở S1 với một cạnh, tới S6 thì thành bảy. Kịch bản càng nặng, báo động giả càng nhiều.

*Cách xử lý đã cài đặt:* `diff.py` tính thêm `throughput_ratio` — tổng số lần gọi hiện tại chia cho lúc khỏe mạnh. Dưới 0.5 thì mọi `missing_edges` được gắn nhãn KÉM TIN CẬY kèm con số phần trăm cụ thể, và đoạn text cho LLM có thêm một dòng cảnh báo ở đầu mục.

Cố ý **không bỏ hẳn** các cạnh này: bỏ hẳn thì mất luôn trường hợp service chết thật trong lúc hệ thống đang chậm. Để LLM tự cân nhắc với thông tin đầy đủ thì đúng tinh thần XAI hơn.

---

## PHASE 2 KẾT THÚC — tổng kết

Cả 6 kịch bản đều tạo ra dấu hiệu quan sát được và khớp với triệu chứng đã ghi trong `scenarios.yaml`.

| Kịch bản                   | Dấu hiệu nhận dạng đặc trưng                                                              |
| ---------------------------- | ------------------------------------------------------------------------------------------------ |
| S1 độ trễ 6s              | cạnh chậm**tụ về** productcatalog, p95 phía server cũng chậm, thông lượng sụp   |
| S2 tắt service              | cạnh**lỗi**, không phải cạnh mất; tỉ lệ lỗi lan truyền bằng nhau qua các tầng |
| S3 xóa pod                  | **chỉ có** tuổi pod; tỉ lệ lỗi thường bằng 0 vì lưu lượng checkout quá thưa |
| S4 nghẹt CPU frontend       | cạnh chậm**tỏa ra** từ frontend, các service đích vẫn nhanh                        |
| S5 nghẹt CPU productcatalog | cạnh chậm tụ về, nhưng**p95 phía server vẫn nhanh** — khác hẳn S1                |
| S6 kép                      | hai nhóm dấu hiệu tách bạch, kèm 7 cạnh mất giả                                         |

**Bốn lỗi đã phát hiện và sửa, ba trong số đó nằm ở phần hoàn tác:**

1. `unset_env` dùng merge patch làm mất trường `image` → lỗi 422, và lỗi vẫn nằm nguyên trên hệ thống trong khi script tưởng đã gỡ xong. Sửa bằng JSON Patch.
2. `set_cpu_limit` không hạ `requests` cùng lúc → Kubernetes từ chối vì `requests` phải nhỏ hơn hoặc bằng `limits`.
3. `active_fault.json` ghi đè một mục → kịch bản kép chỉ hoàn tác được một nửa. Sửa thành danh sách, gỡ theo thứ tự ngược.
4. F3 dò sai đại lượng: xóa pod tạo ra **pod mới** chứ không phải khởi động lại. Sửa bằng cách đo tuổi pod.

Bài học chung để viết vào báo cáo: **hàm hoàn tác phải được kiểm chứng riêng bằng cách chạy thật.** Ba lỗi loại này không phát hiện được bằng đọc code, và cả ba đều có chung một kiểu nguy hiểm — hệ thống vẫn hỏng trong khi công cụ báo đã sạch.

**Việc phải làm ở phase 3, rút ra từ phase 2:**

- Prompt phải có p95 **phía server** bên cạnh độ trễ **phía người gọi**, vì chênh lệch giữa hai con số là cách duy nhất phân biệt S1 với S5.
- Prompt phải có CPU đang dùng và trần CPU của các service bị đánh dấu chậm. Hiện `describe_resources` sắp theo RAM nên service đang nghẹt CPU không lọt vào danh sách.
- Prompt phải nói rõ: thông lượng sụp thì cạnh vắng mặt không đồng nghĩa cạnh hỏng, và tuổi pod chỉ nói "vừa có thay đổi" chứ không nói thay đổi gì.
- Cân nhắc lọc pod hạ tầng (`jaeger`, `opentelemetrycollector`) khỏi phần POD HEALTH để LLM không đổ lỗi nhầm.

### Phase 3 — XAI

**Ngày 2026-08-23 — code phase 3 xong, chờ khóa API để chạy.**

Sáu file: `xai/schema.py`, `xai/prompt_templates.py`, `xai/reasoner.py`, `eval/metrics.py`, `eval/replay.py`, `scripts/eval_xai.py`.

**Quyết định thiết kế quan trọng nhất: đánh giá chạy trên snapshot đã lưu, không đụng cluster.**

`eval/replay.py` dựng lại đoạn prompt từ file JSON trong `data/runs/`, và ghép mỗi snapshot với file ground truth ghi gần nhất trước nó. Ghép được 7 ca từ dữ liệu phase 2 mà không phải tiêm lỗi lại lần nào.

Lý do làm vậy: nếu mỗi lần sửa prompt lại phải tiêm lỗi lại thì mất 30 phút một lượt, và tệ hơn là mỗi lượt ra một trạng thái hệ thống hơi khác nên **không so sánh được prompt mới với prompt cũ**. Giữ nguyên đầu vào, chỉ đổi prompt — đó là cách duy nhất để biết prompt tốt lên thật hay chỉ gặp may.

Cách dựng lại: tạo object nhẹ có đúng các thuộc tính mà `serialize.py` cần rồi gọi lại chính các hàm đó, không chép lại logic sinh text. Hai bản chép sẽ lệch nhau ngay lần sửa đầu tiên.

**Ba thay đổi so với mục 7.2 KLTN.md, đều có lý do từ phase 2:**

1. Thêm hành động `no_action` vào schema. Kịch bản S3 có đáp án đúng là không làm gì; thiếu lựa chọn này thì agent buộc phải chọn một hành động nào đó và không đo được chỉ số 5 "wasted action count".
2. `params` dùng `dict[str, str]` thay vì dict tự do, vì structured output cần schema đóng.
3. Prompt nhồi thẳng 7 quy tắc đọc dữ liệu rút ra từ phase 2 — hướng cạnh chậm, so p95 phía server với độ trễ phía người gọi, service chết tạo cạnh lỗi chứ không phải cạnh mất, thông lượng sụp sinh cạnh mất giả, tỉ lệ lỗi lan truyền bằng nhau, pod tạo lại không đồng nghĩa pod chết, vắng metric không đồng nghĩa đã chết.

**Chỉ số propagation dùng Jaccard** chứ không dùng tỉ lệ bao phủ. Jaccard phạt cả bỏ sót lẫn kể thừa; dùng bao phủ đơn thuần thì model cứ liệt kê đủ 11 service là đạt điểm tuyệt đối.

**Chấm hành động chỉ xét hành động ĐẦU TIÊN**, vì phase 5 agent cũng chỉ thực hiện cái đầu tiên. Xét cả danh sách thì model cứ đề xuất đủ bảy hành động là chắc chắn trúng.

**Ba khoản nợ phase 2 đã trả trước khi viết prompt:** thêm truy vấn CPU so với trần (`cpu_vs_limit`), thêm mục CPU vào prompt cho các service bị nghi là chậm, và lọc pod hạ tầng (`jaeger`, `opentelemetrycollector`, `loadgenerator`) khỏi phần POD HEALTH.

**Chọn nhà cung cấp LLM: hai tầng Groq + OpenAI.**

Máy không có khóa Anthropic, nhưng có sẵn khóa OpenAI và gói Groq miễn phí. Mục 7.5 KLTN.md vốn đã yêu cầu hai tầng "model rẻ cho chạy loạt, model mạnh cho demo và bảng cuối", nên hai khóa sẵn có khớp đúng vào đó mà không phải mua thêm gì.

- **Tầng chạy loạt: Groq**, model mở `openai/gpt-oss-120b`, miễn phí. Không rơi vào cái bẫy 7–8B mà mục 3 KLTN.md đã loại.
- **Tầng demo và bảng cuối: OpenAI.**

`reasoner.py` viết bằng thư viện OpenAI vì **Groq phục vụ theo đúng giao thức của OpenAI**, chỉ khác địa chỉ máy chủ. Một bộ code chạy được cả hai, đổi nhà cung cấp là đổi một dòng cấu hình. Nhờ vậy so sánh được hai model trên **cùng bộ dữ liệu đầu vào** — đây là một kết quả phụ đáng đưa vào báo cáo, cho thấy phương pháp phụ thuộc vào năng lực model đến mức nào.

**Ba chỗ phải xử lý vì chạy đa nhà cung cấp:**

1. *Schema nghiêm ngặt.* Chế độ `json_schema` của OpenAI đòi mọi object khai báo đủ thuộc tính và cấm thuộc tính lạ. Pydantic không tự sinh đúng vậy nên có hàm `strict_schema()` sửa lại. Kèm theo, `params` trong `ProposedAction` đổi từ từ điển sang **danh sách cặp khóa-giá trị** — từ điển tự do không hợp schema nghiêm ngặt.
2. *Tự hạ cấp khi không hỗ trợ.* Nhà cung cấp nào từ chối `json_schema` thì `reasoner.py` tự chuyển sang chế độ "chỉ cần JSON hợp lệ" và nhét schema vào prompt, chỉ thử một lần rồi nhớ luôn. Pydantic vẫn validate và vẫn retry như cũ, nên chất lượng đầu ra không phụ thuộc vào việc nhà cung cấp có hỗ trợ hay không.
3. *Hạn mức gói miễn phí.* Gặp lỗi 429 thì chờ 20 giây rồi thử lại. Cần cho phase 6 chạy 150 lượt liên tục.

**Ước tính khối lượng phase 3** (6 kịch bản × 5 lần): khoảng 57.500 token vào và 27.000 token ra. Groq miễn phí nên 0 đồng; OpenAI khoảng 0.07 USD. Gói Groq miễn phí thừa sức cho phase 3; phase 6 gấp khoảng 5 lần, có thể chạm hạn mức mỗi phút nhưng đã có cơ chế chờ và thử lại.

**Groq gỡ hết dòng Llama, phải đổi model.** Lần gọi đầu tiên trả về lỗi:

```
API 404: The model `llama-3.3-70b-versatile` does not exist
```

Hỏi thẳng danh sách model của tài khoản (`GET /v1/models`) thì thấy Groq chỉ còn `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.6-27b` và mấy model chuyển giọng nói. Đổi mặc định sang **`openai/gpt-oss-120b`** — model mở 120 tỉ tham số, còn mạnh hơn mức 70B đã tính ban đầu, nên lập luận "không rơi vào bẫy 7–8B" ở trên vẫn đứng vững.

Bài học đưa vào báo cáo: **tên model của nhà cung cấp là thứ hết hạn**. Khóa luận viết trong nhiều tháng mà model bị gỡ giữa chừng thì thí nghiệm cũ không chạy lại được. Vì vậy `reasoner.py` cho phép ghi đè tên model bằng biến môi trường `KLTN_GROQ_MODEL`, và mỗi file kết quả trong `data/eval/` đều ghi kèm tên model đã dùng.

**Hai lỗi làm S2 chẩn đoán sai, phát hiện ngay lần chạy thật đầu tiên.**

Lần đầu chạy `--once S2`, model đoán `checkoutservice / resource_exhaustion` trong khi đáp án là `currencyservice / crash`. Đọc chuỗi suy luận nó xuất ra thì thấy nó **không hề ẩu**, nó lập luận chặt trên dữ liệu được đưa. Lỗi nằm ở dữ liệu và ở prompt, không nằm ở model.

*Lỗi 1 — snapshot giấu mất bằng chứng mạnh nhất.* Kịch bản S2 hạ số bản sao của `currencyservice` về 0, tức là không còn pod nào mang tên đó nữa. Nhưng phần POD HEALTH chỉ ghi:

```
POD HEALTH:
  all 10 pods ready, no restarts
```

Đáng lẽ phải có 11. Nguyên nhân: `describe_pods()` duyệt danh sách pod **đang tồn tại** rồi lọc ra pod bất thường — mà một deployment đã biến mất thì không còn dòng nào để duyệt. Đây là một lớp lỗi đáng ghi vào báo cáo: **telemetry chỉ báo cáo những gì đang tồn tại, nên sự vắng mặt là thứ nó không bao giờ tự nói ra.** Muốn thấy cái thiếu thì phải có danh sách cái đáng lẽ phải có mà đối chiếu.

Sửa: thêm `expected_deployments()` lấy danh sách deployment nghiệp vụ từ sơ đồ thiết kế, đối chiếu với các deployment đang chạy, và in thẳng dòng:

```
POD HEALTH:
  currencyservice: NO PODS AT ALL - deployment scaled to 0 or every pod is gone
  the other 10 pods are ready, no restarts
```

*Lỗi 2 — prompt dạy cách đọc cạnh chậm nhưng quên cạnh lỗi.* Bảy quy tắc ban đầu có "cạnh chậm hội tụ vào đâu thì đó là nguyên nhân", nhưng không có quy tắc tương ứng cho cạnh lỗi. Model thấy `checkoutservice` tự báo 65.4% lỗi nên quy tội cho nó, dù 65.4% đó chính là lỗi nó **chuyển tiếp** từ `currencyservice`.

Thêm bốn quy tắc, đều rút từ số liệu đo được của chính S2:

1. **Cạnh lỗi hội tụ vào thủ phạm.** Hai người gọi khác nhau (`frontend` và `checkoutservice`) cùng lỗi khi gọi `currencyservice` thì `currencyservice` là nguyên nhân. Hai service độc lập không hỏng cùng lúc do trùng hợp.
2. **Tỉ lệ lỗi của một service bao gồm cả lỗi nó chuyển tiếp.** Service nào tự báo lỗi cao mà cạnh gọi ra của nó cũng đang lỗi thì nó là **nạn nhân**, không phải thủ phạm. Phải đi theo cạnh đó xuống dưới trước khi kết luận.
3. **Callee báo 0% lỗi không có nghĩa là nó vô can.** Metric phía server chỉ đếm request đã đến được server. Service chết thì request không tới nơi, nên tỉ lệ lỗi của chính nó đứng yên ở 0% và p95 vẫn thấp — đúng như S2: `currencyservice: 2.66 req/s, 0.0% errors, p95 0.48ms` trong khi nó đã tắt hẳn. Cửa sổ quan sát rộng 5 phút còn giữ lại lưu lượng của lúc chưa hỏng, càng làm số liệu trông sạch.
4. **Đọc POD HEALTH trước khi tin bất kỳ metric nào.** Dòng `NO PODS AT ALL` là kết luận, không phải manh mối.

Sau khi sửa cả hai, S2 đúng cả bốn chỉ số: root cause `currencyservice`, loại lỗi `crash`, độ tin cậy 0.96, Jaccard lan truyền 1.00, hành động đúng.

**Vì sao đây không phải là gian lận điểm.** Cả hai sửa đổi đều **thêm dữ liệu mà một kỹ sư vận hành thật luôn nhìn thấy** (`kubectl get pods` hiện ngay là thiếu một deployment) và **thêm quy tắc đọc dữ liệu, không thêm đáp án**. Prompt không hề nhắc tên `currencyservice` hay tên kịch bản nào. Bốn quy tắc mới là kiến thức chung về hệ phân tán, áp dụng được cho cả 6 kịch bản chứ không riêng S2 — và đó chính là điều `--runs 5` trên cả 6 kịch bản dùng để kiểm chứng.

**Đổi `PROMPT_VERSION` từ `v1` sang `v2`** khi sửa prompt, vì khóa cache có kèm số phiên bản. Không đổi thì lần chạy sau lấy lại kết quả của prompt cũ và tưởng là prompt mới không cải thiện gì.

**Hạn mức Groq gói miễn phí là 8000 token MỖI PHÚT, không phải mỗi ngày.**

Loạt chạy đầu tiên treo 16 phút không in ra dòng nào rồi hàng loạt lượt thất bại. Đọc header trả về mới ra nguyên nhân:

```
x-ratelimit-limit-tokens = 8000
```

Một lần chẩn đoán tốn khoảng 5000 token vào cộng 900 ra, tức là **chỉ lọt đúng một lượt mỗi phút**. Code cũ chờ cứng 20 giây nên lần thử nào cũng rơi lại vào đúng cửa sổ đang bị chặn. Hệ quả cho phase 6: chạy 150 lượt trên Groq mất ít nhất 2 tiếng rưỡi vì lý do hạn mức, không phải vì model chậm. Phải tính vào kế hoạch.

Sửa: đọc thẳng header `retry-after` và `x-ratelimit-reset-tokens` xem nhà cung cấp bảo chờ bao lâu, thay vì đoán một con số.

**Lỗi hạn mức tiêu vào cùng quỹ thử lại với lỗi sai schema.** Bị chặn ba lần liên tiếp là mất trắng lượt đó, dù model chưa hề trả lời sai lần nào. Đã tách thành hai quỹ riêng: `max_retries` cho lỗi suy luận, `max_rate_limit_waits` cho lỗi hạn mức. Bị chặn không phải là suy luận sai nên không đáng bị trừ lượt.

**Ba bài học về ghi log của một loạt chạy dài**, đều trả giá bằng thời gian thật:

1. *Python đệm đầu ra khi bị chuyển hướng vào file.* Loạt chạy im lặng 16 phút, không phân biệt được đang chờ hạn mức với đang treo hẳn. Phải chạy bằng `python -u`.
2. *Phải in ra mỗi lần chờ hạn mức.* Không in thì không có cách nào biết nó còn sống.
3. *Đừng cắt ngắn thông báo lỗi.* Tớ cắt ở 120 ký tự nên mất đúng phần quan trọng: chạm trần nào và phải chờ bao lâu. Đã nới lên 400 ký tự.

**Lỗi nghiêm trọng nhất: prompt khẳng định một điều sai sự thật.**

Snapshot của S4 và S5 — đúng hai kịch bản về CPU — có trường `cpu` rỗng hoàn toàn, vì Prometheus không trả về `kube_pod_container_resource_limits`. Nhưng `describe_cpu()` vẫn in ra:

```
CPU USAGE vs LIMIT: no service is close to its CPU limit
```

Tức là nói với model rằng **đã kiểm tra CPU và không có gì bất thường**, trong khi thật ra **chưa đo được gì cả**. Mà dữ liệu CPU chính là bằng chứng quyết định để phân biệt S5 với S1: cả hai đều làm `productcatalogservice` chậm, chỉ khác ở chỗ CPU có chạm trần hay không.

Đây là lớp lỗi tệ hơn hẳn thiếu dữ liệu: **prompt thiếu dữ liệu chỉ làm model bớt chắc chắn, prompt nói sai sự thật chủ động đẩy model ra khỏi đúng nguyên nhân.** Nguyên tắc rút ra và áp dụng cho cả hệ thống: mọi câu tổng kết dạng "không có gì bất thường" đều phải phân biệt được với "không đo được", nếu không thì đừng in ra.

Sửa: khi `cpu` rỗng thì in thẳng `NO CPU DATA in this window - this is missing data, NOT evidence that CPU is healthy`.

**Đính chính nguyên nhân — chẩn đoán đầu tiên của tớ sai.** Tớ đoán Prometheus không trả về `kube_pod_container_resource_limits`. Kiểm tra trên cluster đang chạy thì metric có đủ, và `cpu_vs_limit()` gọi ra 12 kết quả bình thường. Số liệu chỉ đúng nguyên nhân khi xếp snapshot theo thời gian:

```
moi snapshot chup TRUOC 16:15  ->  cpu: 0
hai snapshot chup SAU  16:15  ->  cpu: 12
```

16:15 chính là lúc `cpu_vs_limit()` được thêm vào code. Toàn bộ snapshot của phase 2 chụp trước khi hàm đó tồn tại, nên trường `cpu` rỗng vì **chưa có ai đi lấy**, không phải vì Prometheus thiếu.

Bài học: khi một trường dữ liệu rỗng, hãy hỏi "code lúc đó đã biết lấy nó chưa" trước khi đi đổ lỗi cho nguồn dữ liệu. Dấu hiệu nhận biết rẻ nhất là **xếp theo thời gian và tìm mốc chuyển** — mốc đó gần như luôn trùng với một lần sửa code.

Cách trả nợ: tiêm lại S4 và S5 để có snapshot mang dữ liệu CPU, chứ không phải sửa code.

**Kết quả mốc phase 3 (prompt v3, OpenAI `gpt-4.1-mini`, 6 kịch bản × 5 lần = 30 ca):**

```
S1  root cause 100%  lan truyen 1.00  hanh dong  20%  loai loi 100%
S2  root cause 100%  lan truyen 1.00  hanh dong 100%  loai loi 100%
S3  root cause 100%  lan truyen 1.00  hanh dong 100%  loai loi 100%
S4  root cause   0%  lan truyen 0.00  hanh dong  60%  loai loi   0%
S5  root cause   0%  lan truyen 0.57  hanh dong 100%  loai loi   0%
S6  root cause 100%  lan truyen 0.77  hanh dong 100%  loai loi 100%

ROOT CAUSE ACCURACY : 66.7% (do lech chuan 0.479)
PROPAGATION ACCURACY: 0.722 (do lech chuan 0.377)
hanh dong dung      : 80.0%
loai loi dung       : 66.7%
```

**Cổng chặn phase 3 đạt** (yêu cầu từ 50% trở lên). Bốn kịch bản đúng tuyệt đối với độ lệch chuẩn bằng 0; hai kịch bản CPU sai hoàn toàn, và sai **rất ổn định** — cùng một đáp án sai ở cả 5 lần. Sai ổn định là dấu hiệu lỗi hệ thống chứ không phải model dao động, nên sửa được bằng dữ liệu và prompt.

Điểm đáng chú ý cho báo cáo: **hai kịch bản CPU chính là hai kịch bản có dữ liệu CPU rỗng.** Đây là bằng chứng trực tiếp cho luận điểm trung tâm của đề tài — chất lượng chẩn đoán bị chặn trên bởi chất lượng telemetry, không phải bởi năng lực suy luận của model.

**Ba quy tắc thêm sau khi soi hai kịch bản sai:**

1. *Một người gọi chậm về NHIỀU callee thắng một cạnh lỗi đơn lẻ.* S4 làm `frontend` nghẹt CPU, sinh ra cạnh chậm tới cả 6 callee trong khi các callee đó giữ p95 thấp. Model lại bám vào cạnh `frontend -> adservice` có 48.8% lỗi và quy tội cho `adservice`. Thật ra `adservice` lỗi vì hết hạn chờ do `frontend` đã chậm sẵn — triệu chứng, không phải nguyên nhân.
2. *Độ trễ lan truyền lên trên y hệt tỉ lệ lỗi.* S5 có `checkoutservice` p95 625ms nên model quy tội cho nó, dù cạnh `checkoutservice -> productcatalogservice` chậm gấp 110 lần. Service nào tự chậm mà cạnh gọi ra cũng chậm thì nó đang **chuyển tiếp** độ trễ. Chỉ service ở đáy chuỗi chậm, không còn cạnh chậm nào của riêng nó, mới là nguyên nhân.
3. *Đếm số người gọi khác nhau trước khi chọn.* S5 có 3 người gọi khác nhau cùng chậm về `productcatalogservice`, còn `checkoutservice` chỉ có 1. Bắt model đếm ra con số đó trước khi kết luận.

Cả ba đều là kiến thức chung về hệ phân tán, áp dụng cho mọi kịch bản, và không hề nhắc tên service hay tên kịch bản nào.

**Kết quả phase 3 sau khi sửa (prompt v4, OpenAI `gpt-4.1-mini`):**

```
                        loat 1    loat 2
S1  root cause           100%      100%
S2  root cause           100%      100%
S3  root cause           100%      100%
S4  root cause            40%       60%
S5  root cause           100%      100%
S6  root cause           100%      100%

ROOT CAUSE ACCURACY      90.0%     93.3%
PROPAGATION ACCURACY     0.772     0.783
hanh dong dung           83.3%     90.0%
loai loi dung            73.3%     76.7%
```

Từ 66.7% lên 90–93%. Chạy hai loạt độc lập để chắc con số không phải may. **Cổng chặn phase 3 đạt** với biên rộng.

Thay đổi lớn nhất đến từ S5: 0% lên 100% root cause và lan truyền 1.00 tuyệt đối, nhờ quy tắc "độ trễ lan truyền lên trên y hệt tỉ lệ lỗi". S4 lên 40–60%, vẫn là kịch bản yếu nhất vì thiếu đúng dữ liệu CPU.

**Phát hiện quan trọng nhất về phương pháp: thêm quy tắc vào prompt KHÔNG phải lúc nào cũng tốt lên.**

Sau v4 tớ thêm hai quy tắc nữa, cả hai đều đúng về mặt kiến thức chung:

1. Hỏng ở service cửa ngõ thì không service nội bộ nào bị ảnh hưởng, đường lan truyền phải rỗng.
2. Thêm bản sao chỉ cứu được service đang quá tải; service chậm mà lưu lượng bình thường và CPU thấp thì nên `rollback` chứ không `scale_up`.

Kết quả v5 **tụt xuống 66.7%**:

```
        v4      v5
S4     40%      0%
S5    100%      0%
S2 hanh dong 100%  ->  40%
TONG   90.0%   66.7%
```

Quy tắc 2 phá hỏng S2, vì ở kịch bản đó `currencyservice` bị hạ về 0 bản sao nên `scale_up` **chính là** đáp án đúng — quy tắc mới dạy model tránh đúng cái hành động cần làm. Đã quay lại v4 và giữ nguyên.

Bài học đưa vào báo cáo: **các quy tắc trong prompt tương tác với nhau, không cộng dồn độc lập.** Một quy tắc đúng trong đa số trường hợp vẫn có thể phá một trường hợp mà nó không áp dụng. Vì vậy mọi thay đổi prompt đều phải đo lại trên toàn bộ bộ kịch bản, không được đo mỗi kịch bản đang sửa. Đây chính là lý do `eval/replay.py` chạy trên snapshot đã lưu: nếu mỗi lần đo phải tiêm lỗi lại thì không ai đủ kiên nhẫn đo lại cả bộ sau mỗi lần sửa một dòng prompt.

**Cảnh báo phải viết vào báo cáo: con số 90–93% có rủi ro overfitting** (khớp quá sát dữ liệu đã thấy). Tớ đã tinh chỉnh prompt bằng cách soi chính 6 ca này rồi thêm quy tắc, nên model được lợi thế mà nó sẽ không có với sự cố chưa từng gặp. Cách xử lý trung thực: phase 6 tiêm lỗi lại từ đầu sinh ra snapshot mới, và **con số của phase 6 mới là con số dùng để kết luận**. Con số phase 3 chỉ nói lên rằng phương pháp có cơ sở để đi tiếp.

Điều làm nhẹ bớt lo ngại này: cả bảy quy tắc thêm vào đều là kiến thức chung về hệ phân tán, không quy tắc nào nhắc tên service hay tên kịch bản. Nhưng nói vậy không thay thế được việc đo trên dữ liệu mới.

**Lỗi thứ bảy, nằm ở ĐÁP ÁN chứ không ở model.** `expected_propagation` của F4-frontend là danh sách rỗng, vì hàm sinh đáp án đi ngược lên tìm những service *gọi* service hỏng, mà `frontend` là cửa ngõ vào nên không service nội bộ nào gọi nó. Model liệt kê 6 callee nên Jaccard bằng 0 ở cả 5 lần. Ở đây model sai thật — các callee đó vẫn khỏe, chính `frontend` mới nghẹt — nhưng quy tắc dạy nó trả về danh sách rỗng lại nằm trong gói v5 đã bị loại vì làm tổng thể tệ đi. Đây là món nợ phase 6: tách hai quy tắc của v5 ra thử riêng từng cái, thay vì thêm cả gói.

**KẾT QUẢ MẠNH NHẤT CỦA CẢ ĐỀ TÀI: thêm dữ liệu CPU, chẩn đoán từ 93.3% lên 100%.**

Sau khi tiêm lại S4 và S5 để có snapshot mang dữ liệu CPU, chạy lại đánh giá phase 3 với **prompt không đổi một chữ nào** (vẫn v4, `gpt-4.1-mini`, 6 kịch bản × 5 lần):

```
                 truoc (thieu CPU)   sau (co CPU)
S4 root cause         40-60%             100%
S5 root cause            0%              100%
S4 loai loi              0-60%           100%
S5 loai loi              0%              100%

ROOT CAUSE ACCURACY     93.3%            100.0%  (do lech chuan 0.000)
loai loi dung           76.7%            100.0%
hanh dong dung          90.0%             86.7%
```

**Đây là một thí nghiệm có đối chứng chuẩn.** Chỉ một biến thay đổi: snapshot có thêm mục CPU. Prompt, model, bộ kịch bản, cách chấm điểm đều giữ nguyên. Nên kết luận rút ra được là kết luận nhân quả, không phải tương quan.

Bằng chứng cụ thể mà model thiếu trước đó:

```
S4: frontend: using 0.010 of 0.010 cores (99% of limit)  <-- AT LIMIT
S5: productcatalogservice: using 0.009 of 0.010 cores (85% of limit)  <-- AT LIMIT
```

**Ý nghĩa cho báo cáo — đây là luận điểm trung tâm và giờ đã có số chứng minh:** chất lượng chẩn đoán bị chặn trên bởi **chất lượng telemetry**, không phải bởi năng lực suy luận của model. Trước khi có dữ liệu CPU, model suy luận hoàn toàn chặt chẽ nhưng vẫn sai, vì nó không thể suy ra thứ không có trong dữ liệu. Sau khi có, cùng model cùng prompt đạt tuyệt đối.

Hệ quả thực tiễn nên viết vào phần kết luận: **đầu tư vào độ phủ telemetry cho lợi tức cao hơn đầu tư vào model mạnh hơn**, ít nhất trong khoảng mà đề tài này đo được.

**Độ lệch chuẩn bằng 0 trên cả 30 ca** cũng đáng nói: khi bằng chứng đủ rõ, tính bất định của LLM gần như biến mất. Dao động quan sát được ở các lần chạy trước chủ yếu đến từ **dữ liệu mơ hồ**, không phải từ bản chất ngẫu nhiên của model.

**Hai chỉ số chưa tuyệt đối, ghi lại trung thực:**

- *Lan truyền 0.767.* Phần lớn do S4 luôn bằng 0 vì lý do kỹ thuật: `expected_propagation` của F4-frontend là danh sách rỗng (frontend là cửa ngõ, không service nội bộ nào gọi nó) còn model liệt kê 6 callee. Quy tắc dạy model trả về danh sách rỗng nằm trong gói v5 đã bị loại vì làm tổng thể tệ đi. Món nợ phase 6: tách hai quy tắc của v5 ra thử riêng từng cái.
- *Hành động 86.7%, giảm nhẹ so với 90.0%.* Chỗ trừ điểm nằm ở S1: model chọn `scale_up` cho lỗi độ trễ chèn bằng biến môi trường, trong khi đáp án là `adjust_resources`, `restart_pod` hoặc `rollback`. Quy tắc sửa việc này cũng nằm trong gói v5 bị loại.

### Phase 4 — Digital Twin

**Bước 4.0 — trả nợ dữ liệu CPU, và chẩn đoán đầu tiên của tớ sai.**

Chi tiết đã ghi ở phần phase 3 (mục đính chính). Tóm lại: metric có đủ, chỉ là `cpu_vs_limit()` chưa tồn tại lúc chụp snapshot phase 2. Cách trả nợ là tiêm lại S4 và S5 chứ không phải sửa code.

**Lỗi chặn đường: `emailservice` CrashLoopBackOff sau khi node bị gián đoạn.**

Mỗi lần máy ngủ hoặc Docker khởi động lại, `emailservice` rơi vào vòng lặp chết với thông báo:

```
Liveness probe failed: timeout: failed to connect service "10.244.0.x:8080" within 1s
```

Hậu quả là **mọi snapshot nền đều bẩn** vì `checkoutservice -> emailservice` báo 100% lỗi, và không tiêm được kịch bản nào.

Tớ đoán sai hai lần trước khi ra đúng:

1. Đoán đầu: thiếu CPU, giống hệt ca `recommendationservice` trước đó. Đo lại: node dùng **9% CPU, 53% RAM** — không hề thiếu.
2. Đoán thứ hai: service khởi động chậm. Đọc log container: `listening on port: 8080` chỉ 0.2 giây sau khi chạy — ứng dụng sống và sẵn sàng.

Sự thật nằm ở chữ **connect**: kubelet không mở nổi kết nối trong 1 giây, chứ không phải service trả lời chậm. Hạn 1 giây của bản gốc Google quá chặt với service Python chạy trong kind trên WSL, nhất là ngay sau khi sandbox mạng của pod vừa được dựng lại.

Sửa: `infra/emailservice-probe-patch.yaml` nới `timeoutSeconds` từ 1 lên 5 và `failureThreshold` từ 3 lên 5. Pod mới chạy ổn định, 0 lần khởi động lại.

**Cố ý chỉ nới riêng `emailservice`.** `recommendationservice` cũng từng CrashLoopBackOff vì lý do gần giống, nhưng ở đó là tranh CPU thật và hạn chặt là một phần của hiện tượng đang nghiên cứu. Không kịch bản lỗi nào phụ thuộc vào hạn thăm dò của `emailservice` — F4 chỉ tác động lên `frontend` và `productcatalogservice` — nên nới ở đây an toàn.

Bài học chung của cả ba lần đoán: **đọc log của chính container trước khi đổ lỗi cho tài nguyên.** Log nói ứng dụng sống thì vấn đề nằm ở tầng giữa ứng dụng và kubelet, không nằm trong ứng dụng.

**Bước 4.1 — manifest bản gọn cho twin.**

`infra/twin/manifests.yaml` sinh ra từ `release/kubernetes-manifests.yaml`, bỏ 4 khối: `adservice`, `recommendationservice`, `loadgenerator`, và `frontend-external`. Còn lại **9 Deployment và 9 Service** — kế hoạch ban đầu tớ ghi 8, đó là do quên đếm `redis-cart`.

Bỏ `frontend-external` vì nó kiểu `LoadBalancer`, mà kind không cấp được IP ngoài nên nó nằm mãi ở trạng thái chờ và chỉ làm rối `kubectl get svc -n twin`.

Giữ `currencyservice`, `shippingservice`, `emailservice` dù chúng không nằm trên đường đi ngắn nhất: `checkoutservice` bắt buộc phải có đủ 6 địa chỉ mới khởi động được, thiếu một cái là gãy luồng đặt hàng — mà đó đúng là luồng cần đo.

`infra/twin/kustomization.yaml` giải hai chỗ vướng đã lường trước:

1. **Địa chỉ collector phải là tên đầy đủ** `opentelemetrycollector.default.svc.cluster.local:4317`. Tên ngắn chỉ phân giải trong cùng namespace. Đây là kiểu hỏng nguy hiểm vì twin vẫn **chạy bình thường**, chỉ là không đo được gì — nhìn bên ngoài mọi thứ đều xanh.
2. **Tên service trong trace mang tiền tố `twin-`.** Không có tiền tố thì twin và production cùng tên trong Prometheus, mà cửa sổ quan sát rộng 5 phút nên số liệu hai bên trộn vào nhau ngay sau khi dựng twin. Có tiền tố thì tách bằng truy vấn, không phải ngồi chờ hết cửa sổ.

**Vấp lại giới hạn của kustomize đã gặp ở phase 0:** không cho tham chiếu file nằm ngoài thư mục gốc của kustomization. Phải chuyển `twin-manifests.yaml` vào thành `infra/twin/manifests.yaml`.

**Bước 4.2 — `twin_manager.py`.**

Ba hàm `create_twin`, `load_state`, `destroy_twin`, cộng `status` và `wait_ready`.

Hai chi tiết đáng ghi:

- **`destroy_twin` chờ namespace biến mất hẳn, không chỉ chờ lệnh trả về.** Kubernetes xóa namespace bất đồng bộ: lệnh trả về ngay nhưng pod còn sống thêm hàng chục giây, và RAM chỉ thực sự được trả lại khi pod cuối cùng chết. Dựng twin mới lúc twin cũ chưa chết hẳn là cách chắc chắn nhất để hết RAM.
- **`create_twin` kiểm tra RAM trước khi dựng** và từ chối nếu node còn dưới 700 MiB. Dựng khi thiếu RAM thì pod bị OOMKilled và có thể kéo theo cả pod của production, tức là hỏng luôn thứ đang muốn quan sát.

**`load_state` tồn tại vì twin dựng từ manifest nên mang cấu hình MẶC ĐỊNH**, còn production tại thời điểm sự cố có thể đã khác. Thử hành động trên bản sao không giống production thì kết quả đo được không nói lên điều gì. Nó chỉ đụng vào deployment có mặt ở cả hai bên — twin thiếu `adservice` và `recommendationservice` nên phải bỏ qua, nếu không thì lỗi giữa chừng và twin nạp trạng thái nửa vời.

**Đo RAM thật:** node còn trống **2826 MiB**, ngưỡng cần 700. Cảnh báo RAM ban đầu của tớ dựa trên số của Windows (còn 1.7 GB trống) là nhìn nhầm tầng — thứ quyết định pod sống chết là RAM bên trong node kind, không phải RAM còn lại của Windows.

**Bước 4.3 — bộ sinh tải riêng cho twin.**

`twin_loadgen.py` chạy từ Windows qua `kubectl port-forward`, không dựng thêm pod nào. Lý do không dùng `loadgenerator` gốc: nó là một Deployment chạy Locust, tốn thêm khoảng 50 MiB và một pod, mà twin chỉ cần đủ tải để sinh trace.

Lý do bắt buộc phải dùng port-forward: **kind không thêm được cổng sau khi tạo cluster**, 5 cổng đã cố định từ đầu và không còn cổng trống cho frontend của twin.

**Tỉ lệ tác vụ giữ đúng bản chính** (index 1, setCurrency 2, browseProduct 10, addToCart 2, viewCart 3, checkout 1), đã kiểm chứng bằng 19000 lần bốc ngẫu nhiên. Đây là điều kiện bắt buộc để so sánh twin với production: hai bên phải chịu cùng hình dạng tải, nếu không thì chênh lệch đo được không biết là do hành động hay do tải khác nhau.

`PortForward` chờ **tới khi gọi thật được**, không chờ một khoảng cố định và cũng không bám vào dòng log `Forwarding from...` — dòng đó in ra trước khi cổng thực sự nhận kết nối.

**Bước 4.4 — `verifier.py`.**

Trả phán quyết `better` / `worse` / `no_change` kèm số liệu từng service. Ba quyết định thiết kế:

1. **Chỉ nhìn 5 service trên luồng nghiệp vụ chính, không lấy trung bình toàn hệ thống.** Trung bình bị `productcatalogservice` áp đảo vì lưu lượng của nó gấp nhiều lần — số đo thật ở S5: 13.56 req/s so với 0.11 req/s của `checkoutservice`. Một hành động phá hỏng hẳn luồng đặt hàng vẫn có thể làm trung bình đẹp lên.
2. **Ngưỡng thay đổi tối thiểu 2 điểm phần trăm lỗi và 15% p95.** Không có ngưỡng thì mọi phép đo đều ra `better` hoặc `worse` do nhiễu tự nhiên. Phase 2 đo được p95 dao động vài phần trăm giữa hai lần chụp liên tiếp trên hệ thống hoàn toàn khỏe.
3. **Tỉ lệ lỗi thắng tuyệt đối so với độ trễ.** Hành động làm hệ thống nhanh hơn nhưng lỗi nhiều hơn bị phán là `worse`: chậm thì người dùng phải chờ, lỗi thì đơn hàng mất hẳn.

**`no_change` KHÔNG được coi là an toàn để đưa lên production.** Hành động không cải thiện gì mà vẫn thi hành thì chỉ thêm rủi ro — đây chính là chỉ số "wasted action count" ở mục 8 KLTN.md.

Đã kiểm chứng bốn trường hợp bằng số liệu giả, gồm ca khó nhất là nhanh hơn nhưng lỗi nhiều hơn, phán quyết đều đúng.


**Bước 4.5 — đo twin fidelity.**

`scripts/twin_fidelity.py`. Đây là chỉ số 7 mục 8 KLTN.md, trả lời câu hỏi: **có đáng tin twin không.** Nếu twin nói một hành động "tốt lên" mà production lại "xấu đi" thì cả kiến trúc twin-verified sụp đổ, vì agent sẽ tin nhầm.

**Quyết định thiết kế quan trọng nhất: phải thử CẢ hành động sai.**

Nếu chỉ thử hành động đúng thì fidelity **luôn ra 100% một cách vô nghĩa** — hành động đúng chính là phép nghịch đảo của lỗi, cả hai môi trường đều khỏi và đều báo `better`. Phép đo chỉ có ý nghĩa khi twin phải **phân biệt** được hành động tốt với hành động vô ích. Nên mỗi kịch bản thử hai hành động:

```
S1  dung: rollback          go bo EXTRA_LATENCY
    sai : scale_up          them ban sao khong go duoc do tre chen moi lan goi
S4  dung: adjust_resources  tra tran CPU ve muc cu
    sai : restart_pod       pod moi van mang dung tran CPU cu
S5  dung: adjust_resources
    sai : restart_pod
```

Fidelity = số lần twin và production ra **cùng một phán quyết**, chia cho tổng số lần thử.

**Loại S2 và S3 khỏi phép đo fidelity, có chủ đích:**

- S2 hạ số bản sao về 0. Hành động sai nào cũng ra "không đổi" vì service vẫn chết, phép đo không phân biệt được gì.
- S3 xóa pod. Kubernetes tự tạo lại trước khi đo xong, không còn gì để sửa.

**Một lỗi phải sửa trong chính verifier.** `measure()` lọc theo tiền tố `twin-`, nên gọi cho production sẽ trả về rỗng. Đã thêm tham số `prefix` để **một lớp đo được cả hai bên**. Đây là điều bắt buộc chứ không phải tiện tay: fidelity là phép so sánh hai môi trường, mà đo bằng hai bộ code khác nhau thì không còn biết chênh lệch đến từ hệ thống hay đến từ code đo.

**Thời gian:** mỗi lần thử khoảng 11 phút (chờ 330 giây sau khi tiêm, 300 giây sau khi chạy hành động). Mỗi kịch bản 4 lần thử, khoảng 50 phút. Cả 3 kịch bản gần 2 tiếng rưỡi.

**Lỗi quy trình tớ tự gây ra, ghi lại để không lặp.** Tớ dựng twin trong lúc S5 đang trong cửa sổ đo, đúng cái điều mình vừa dặn không được làm. Twin tồn tại khoảng 30 giây bên trong cửa sổ 5 phút của S5 rồi bị xóa. Bài học đưa vào quy trình: **trước khi dựng twin, luôn kiểm tra `python scripts/inject.py --status`** — có lỗi đang tiêm nghĩa là đang có phép đo dở dang.

**Lần dựng twin đầu tiên hỏng, và hỏng đúng ở chỗ đã sửa cho production.**

8/9 pod sẵn sàng, riêng `emailservice` CrashLoopBackOff — đúng lỗi hạn thăm dò 1 giây đã sửa ở bước 4.0. Nguyên nhân: `infra/twin/manifests.yaml` chép từ `release/` nên **không mang bản vá** đặt trong `infra/kustomization.yaml`.

Bài học đưa vào báo cáo: **mọi bản vá sửa lỗi của upstream đều phải áp cho cả hai môi trường.** Không áp thì twin và production khác nhau ở đúng chỗ đã từng gây sự cố — mà giống nhau lại chính là điều kiện để con số fidelity có nghĩa. Đã lặp bản vá vào `infra/twin/kustomization.yaml` kèm chú thích giải thích vì sao phải lặp.

**Số đo thật của twin:**

```
9/9 pod san sang, RAM 169 MiB
RAM node con trong sau khi dung twin: 2320 MiB
117 request trong 132s, loi 7.7%, DAT DUOC 5 DON HANG
```

169 MiB nhẹ hơn nhiều so với dự trù 3.8 GB ở mục 2 KLTN.md. Đặt được đơn hàng trong twin chính là tiêu chí thành công của phase 4.

**Lỗi nghiêm trọng thứ hai: số liệu twin và production trộn vào nhau mà không có dấu hiệu gì báo.**

Đo lần đầu chỉ thấy 6 service, thiếu `cartservice` và `shippingservice` — mà `cartservice` nằm trong danh sách service quyết định phán quyết. Truy ra thì lộ một lỗi lớn hơn hẳn.

Hai nguồn số liệu đặt tên theo hai quy ước khác nhau:

- `red_metrics()` đo phía server, lấy thẳng nhãn `service_name`, nên **có** tiền tố `twin-`
- `red_metrics_observed()` đo gián tiếp từ phía người gọi, suy tên ra từ `endpoint_map`, nên **không** có tiền tố

Hậu quả: bộ lọc theo tiền tố loại mất toàn bộ nguồn thứ hai. Và tệ hơn nhiều — nguồn thứ hai **không lọc theo người gọi**, nên `cartservice` của twin và của production dồn vào cùng một khóa. Số liệu hai môi trường trộn vào nhau, không một dấu hiệu nào báo.

Với thí nghiệm fidelity thì đây là kiểu hỏng **làm hỏng luôn kết luận**: fidelity vốn là phép so sánh hai môi trường, mà số liệu hai bên đã trộn thì so cái gì cũng vô nghĩa, và con số vẫn ra đẹp như thường.

Sửa: thêm tham số `caller_prefix` lọc theo tên người gọi (`twin-` lấy riêng twin, `""` loại hết span mang tiền tố twin), và gắn tiền tố vào tên của nguồn gián tiếp cho khớp nguồn phía server. Kiểm chứng sau khi sửa:

```
twin        cartservice 0.43 req/s
production  cartservice 2.59 req/s
```

Bài học chung: **hai nguồn dữ liệu về cùng một thứ mà đặt tên theo hai quy ước là một quả mìn hẹn giờ.** Nó chỉ nổ khi có môi trường thứ hai, tức là muộn hơn hẳn lúc viết code, và nó nổ **im lặng** — không ngoại lệ, không cảnh báo, chỉ có số sai.

**Một chênh lệch cố định giữa twin và production, có chủ đích.** Twin hiện `adservice` và `recommendationservice` trong số liệu dù đã gỡ khỏi manifest, vì `frontend` vẫn gọi chúng và gọi hỏng. Mục 4 KLTN.md đã quyết gỡ hai service này để tiết kiệm RAM. Chênh lệch này **không nằm trên luồng nghiệp vụ chính** và fidelity so **chiều thay đổi** chứ không so giá trị tuyệt đối, nên nó không lật được phán quyết — đây đúng là lý do so delta tốt hơn so giá trị tuyệt đối.

**KẾT QUẢ TWIN FIDELITY S4: 50% (1/2 lần khớp).**

```
S4 dung  adjust_resources   twin=no_change  production=no_change  KHOP
S4 sai   restart_pod        twin=worse      production=no_change  LECH
```

**Con số 50% này còn tệ hơn vẻ ngoài của nó, và phải viết vào báo cáo đúng như vậy: lần "khớp" kia khớp vì CẢ HAI ĐỀU SAI.**

`adjust_resources` chính là phép trả trần CPU về mức cũ, tức là gỡ hẳn nguyên nhân. Đáng ra phải ra `better` ở cả hai môi trường. Ra `no_change` ở cả hai nghĩa là **verifier không nhận ra một cải thiện có thật** — vấn đề nằm ở thước đo, không nằm ở twin.

Lý do nằm ngay trong câu giải thích của phán quyết production:

```
vua nhanh len o frontend, cartservice
vua cham di o checkoutservice, paymentservice
khong ben nao thang ro
```

`checkoutservice` và `paymentservice` chạy **0.08 req/s**, tức khoảng 24 request trong cửa sổ 5 phút. p95 tính trên 24 mẫu nhảy loạn, và nhiễu đó đủ sức lật phán quyết của cả hệ thống.

**Sửa: thêm ngưỡng lưu lượng tối thiểu `MIN_RATE_FOR_VERDICT = 0.3` req/s** (khoảng 90 request mỗi cửa sổ). Service dưới ngưỡng vẫn được in ra cho người đọc thấy, nhưng **không được bỏ phiếu**. Kiểm chứng lại bằng đúng mẫu số liệu của production: phán quyết chuyển từ `no_change` sang `better`.

Nguyên tắc rút ra, áp dụng được cho mọi hệ đo lường: **"không đủ cơ sở để kết luận" và "không có thay đổi" là hai chuyện khác nhau**, gộp chung lại thì phán quyết sai. Đây đúng là cùng một họ với lỗi ở phase 3 khi prompt in "no service is close to its CPU limit" trong lúc thật ra chưa đo được gì.

**~~Chỗ lệch thật giữa twin và production~~ — ĐÍNH CHÍNH, kết luận này không có cơ sở.**

Ban đầu tớ ghi: `restart_pod` làm twin báo `worse` còn production báo `no_change`, và giải thích là "twin tải nhẹ hơn nên phản ứng mạnh hơn với sự cố ngắn hạn — hạn chế thật của twin".

Sai. Lúc đó twin chạy **3 người dùng ảo** còn production chạy **10**, nên chênh lệch hoàn toàn có thể chỉ đến từ tải khác nhau. Đó là **lỗi đo đạc của tớ, không phải tính chất của twin**. Xem mục lỗi thiết kế về tải ở dưới. Phải chạy lại với tải khớp mới được phép kết luận bất cứ điều gì về hạn chế của twin.

**Con số 50% đo TRƯỚC khi sửa verifier, nên chưa phải con số cuối.** Phải chạy lại sau khi có ngưỡng lưu lượng. Ghi lại cả hai con số trong báo cáo và giải thích vì sao chúng khác nhau — đó là một ví dụ tốt cho thấy **thước đo hỏng thì kết luận hỏng theo, dù hệ thống được đo vẫn tốt**.

**CỔNG CHẶN PHASE 4 ĐẠT: 3 vòng dựng–đo–xóa liên tiếp, máy không hết RAM.**

```
RAM trong luc bat dau : 2623 MiB
RAM trong luc ket thuc: 2387 MiB
moi vong: dung 34s, xoa 13s
```

Dựng 34 giây và xóa 13 giây, giống hệt nhau qua cả 3 vòng — twin dựng lại được **nhanh và ổn định**, đủ để vòng lặp ReAct ở phase 5 dùng twin như sân tập thật chứ không phải thứ dựng một lần rồi để đó.

**Lệch 236 MiB giữa đầu và cuối, và tớ KHÔNG kết luận đó là rò rỉ.** Bằng chứng: các con số từng vòng nhảy loạn, có vòng ghi `twin an them: -22 MiB` — tức RAM trống *tăng* sau khi dựng twin, điều không thể xảy ra nếu phép đo tức thời. Nguyên nhân là `kubectl top node` lấy mẫu theo chu kỳ khoảng 30 giây và có tính cả bộ nhớ đệm đĩa, nên chênh lệch trong một vòng 47 giây phần lớn là độ trễ đo đạc.

Bài học về đo lường, cùng họ với hai bài học trước trong phase này: **đừng đọc một phép đo có độ trễ như thể nó tức thời.** Muốn kết luận rò rỉ thì phải chờ metric ổn định rồi mới đo, hoặc chạy nhiều vòng hơn và nhìn xu hướng, không nhìn từng vòng.

**Số đo RAM thật của twin, dùng cho phần tài nguyên trong báo cáo:**

```
9 pod, 169-306 MiB tuy thoi diem do
du tru ban dau o muc 2 KLTN.md: 3.8 GB
```

Nhẹ hơn dự trù hơn 10 lần. Nguyên nhân: dự trù ban đầu ước theo *giới hạn* khai báo trong manifest, còn số này là RAM *đang dùng* thật. Với hệ thống chủ yếu nằm chờ như Online Boutique, hai con số cách nhau rất xa.

**LỖI THIẾT KẾ NẶNG NHẤT CỦA PHASE 4: twin và production chịu tải khác nhau.**

Phát hiện khi chạy loạt fidelity đầy đủ. Lần thử đầu tiên (S1 trên twin) trả về:

```
PHAN QUYET: NO_CHANGE — moi service deu duoi 0.3 req/s
(frontend, productcatalogservice, cartservice, checkoutservice, paymentservice)
khong du mau de ket luan
```

Truy ra: **twin chạy 0.67 req/s trong khi production chạy 2.93 req/s.** Tớ đặt 3 người dùng ảo cho twin với lý lẽ "đủ để mọi cạnh có lưu lượng mà không làm twin nghẹt", còn `loadgenerator` của production đặt `USERS=10`. Tớ cũng rút thời gian chờ giữa hai tác vụ xuống 0.5–3 giây trong khi bản chính dùng 1–10 giây.

Hai hậu quả, hậu quả thứ hai nguy hiểm hơn nhiều:

1. S1 chèn độ trễ 6 giây làm lưu lượng twin sụp dưới ngưỡng, verifier không còn đủ mẫu. Cứ để chạy tiếp thì 11 lần thử còn lại đều ra `no_change` — 2 tiếng rưỡi cho một bảng vô nghĩa.
2. **Nó làm sai lệch chính kết luận về twin.** Ở loạt S4 trước, `restart_pod` làm twin báo `worse` còn production báo `no_change`, và tớ đã ghi đó là "hạn chế của twin: tải nhẹ nên phản ứng mạnh hơn với sự cố ngắn hạn". Kết luận đó **không có cơ sở** — chênh lệch có thể đến hoàn toàn từ tải khác nhau, tức là tớ đã đo nhầm thứ cần đo.

Sửa: 10 người dùng ảo và thời gian chờ 1–10 giây, **đúng bằng cấu hình production**.

Nguyên tắc rút ra, đáng đưa vào chương phương pháp: **so sánh hai môi trường thì mọi biến ngoài biến đang khảo sát đều phải khớp theo CẤU HÌNH, không phải theo cảm giác "đủ dùng".** Tớ đã cẩn thận khớp tỉ lệ tác vụ (index 1, browseProduct 10, checkout 1...) nhưng lại tự ý đổi số người dùng và thời gian chờ — khớp một nửa còn nguy hiểm hơn không khớp gì, vì nó tạo cảm giác đã kiểm soát.

**Chấm điểm lại loạt S4 cũ bằng verifier đã sửa** (`scripts/rescore_fidelity.py`, không đụng tới cluster vì file kết quả đã lưu đủ số liệu thô):

```
                          cu          moi
production adjust_resources  no_change -> better    DOI
production restart_pod       no_change -> worse     DOI
twin       ca hai            no_change -> no_change
```

**Phía production giờ cho phán quyết đúng hoàn toàn.** `adjust_resources` gỡ hẳn nguyên nhân nên phải là `better`; `restart_pod` khởi động lại pod trong lúc vẫn bị bóp CPU nên phải là `worse`. Trước khi sửa, cả hai bị nhiễu từ hai service 0.08 req/s dìm xuống `no_change`. Đây là **bằng chứng bản sửa verifier hoạt động đúng**.

Phía twin vẫn `no_change` cả hai vì lưu lượng 0.2 req/s dưới ngưỡng. Fidelity chấm lại ra 0%, nhưng **con số đó nói về tải của twin chứ không nói về twin**.

Bài học về công cụ: `Verdict` lưu cả `deltas` chứ không chỉ lưu kết luận, nên chấm lại được ngay khi ngưỡng thay đổi mà không mất hàng giờ chạy lại. **Kết luận phụ thuộc vào ngưỡng, mà ngưỡng còn đổi; số liệu thô thì không đổi.** Mọi thí nghiệm nên lưu số liệu thô, không chỉ lưu kết luận.

Ranh giới của mẹo này cũng phải nói rõ: **chấm lại chỉ sửa được lỗi của THƯỚC ĐO, không sửa được lỗi của PHÉP ĐO.** Phần twin chạy sai tải thì bắt buộc phải chạy lại trên cluster.

**Sửa lần một chưa đủ: nâng tải lên 10 người dùng thì port-forward sập.**

Sau khi khớp tải với production, đo lại ngay thì ra kết quả tệ hơn hẳn:

```
3 nguoi dung  qua port-forward:   7.0% loi
10 nguoi dung qua port-forward:  53.7% loi, 117/218 request DUT KET NOI (ma 0)
twin do duoc: frontend 0.20 req/s, moi service con lai 0.00 req/s
```

Mã trạng thái 0 nghĩa là đứt kết nối chứ không phải server trả lỗi. `kubectl port-forward` là một tiến trình đơn ghép kênh qua một kết nối duy nhất, không chịu nổi 10 luồng song song.

**Và nó kéo theo một vấn đề nặng hơn cả chuyện sập**, thứ tớ đáng ra phải thấy từ đầu: đẩy tải từ Windows qua đường hầm **cộng thêm độ trễ mà production không có**, vì production sinh tải từ bên trong cluster. Ngay cả ở 3 người dùng lúc đường hầm còn chạy được, số đo hai bên vẫn không thực sự so sánh được — chỉ là sai ít nên không lộ.

Sửa dứt điểm: **dựng bộ sinh tải bên trong namespace twin**, dùng đúng Deployment, đúng ConfigMap locustfile, đúng `USERS=10` và `RATE=1` của production. Twin lên 10 deployment.

**Đây là lần thứ hai trong phase này tớ đánh đổi sai theo cùng một kiểu.** Ban đầu tớ loại phương án loadgen trong cluster để tiết kiệm 50 MiB RAM, với lý lẽ "mục 2 KLTN.md đã chốt RAM là nút thắt lớn nhất". Lý lẽ đó nghe rất đúng nhưng sai ở chỗ: RAM **chưa bao giờ là ràng buộc thật** trong phase này — đo được node còn trống 2.3 GB, twin chỉ ăn 169–306 MiB. Còn cái đánh đổi đi là **tính so sánh được của phép đo**, tức là chính thứ mà cả thí nghiệm fidelity dựa vào.

Bài học đưa vào báo cáo: **một ràng buộc đã ghi trong tài liệu thiết kế vẫn phải kiểm chứng lại bằng số đo trước khi dùng nó để đánh đổi.** Ràng buộc RAM là thật ở giai đoạn lập kế hoạch, khi mới chỉ có ước lượng 3.8 GB. Đến lúc đo thật thì nó không còn là ràng buộc nữa, nhưng tớ vẫn tiếp tục ra quyết định dựa trên nó.

`twin_loadgen.py` giữ lại làm công cụ thử nhanh bằng tay (`scripts/twin.py --load`), **không dùng cho phép đo**.

**ĐẢO LẠI QUYẾT ĐỊNH MỤC 4 KLTN.md: twin giữ đủ 11 service, không gỡ bớt.**

Sau khi có bộ sinh tải trong cluster, đo lại thì tải khớp gần như hoàn hảo:

```
              twin      production
frontend      2.79        2.93 req/s
cartservice   2.51        2.59 req/s
```

Nhưng chính phép đo đó lộ ra một chênh lệch chưa lường, và nó đánh trúng 2 trên 3 kịch bản fidelity:

```
productcatalogservice   twin 2.94   production 14.45 req/s
```

Gấp 5 lần. Nguyên nhân: production có `recommendationservice` cũng gọi `productcatalogservice`, twin thì đã gỡ service đó. Mà `productcatalogservice` chính là mục tiêu của S1 và S5. **Bóp CPU một service đang chịu 2.94 req/s là tình huống khác hẳn bóp một service chịu 14.45 req/s** — con số fidelity đo ra sẽ nói về một tình huống không tồn tại trong production.

Kèm theo, `adservice` và `recommendationservice` báo **100% lỗi cố định** trong mọi phép đo của twin vì chúng không tồn tại. `frontend` xử lý êm nên luồng chính không gãy, nhưng đó vẫn là hai cạnh khác biệt vĩnh viễn.

RAM đo thật của hai service bị gỡ:

```
adservice             105 MiB
recommendationservice  39 MiB
node con trong       2270 MiB
```

Đã đưa cả hai trở lại. Twin giờ có **12 deployment** (đủ 11 service cộng bộ sinh tải), chỉ khác production một chỗ: `frontend` không mở LoadBalancer vì kind không cấp được IP ngoài.

**Đây là lần thứ ba trong phase 4 tớ phải đảo một quyết định "tiết kiệm RAM".** Ba lần đó là: gỡ `loadgenerator`, đặt 3 người dùng ảo thay vì 10, gỡ `adservice` với `recommendationservice`. Cả ba đều nấp sau cùng một lý lẽ — mục 2 KLTN.md chốt RAM là nút thắt lớn nhất — và cả ba đều sai vì cùng một lý do: **ràng buộc đó đúng lúc lập kế hoạch với ước lượng 3.8 GB, nhưng đo thật thì twin đầy đủ chỉ ăn khoảng 300 MiB trên 2.3 GB đang trống.**

Nguyên tắc viết vào chương phương pháp: **một ràng buộc đã ghi trong tài liệu thiết kế vẫn phải kiểm chứng lại bằng số đo trước khi dùng nó để đánh đổi.** Trích dẫn tài liệu của chính mình nghe rất thuyết phục, và đó chính là chỗ nguy hiểm — nó làm quyết định sai trông như quyết định có căn cứ.

Cái bị đánh đổi cả ba lần đều là **tính so sánh được của phép đo**, tức là chính thứ mà thí nghiệm fidelity dựa vào. Đổi lấy vài chục MiB RAM không thiếu.

**Trạng thái phase 4 khi dừng phiên:** mọi thứ đã sẵn sàng, hệ thống sạch, twin đã xóa. Còn đúng một việc — chạy `python scripts/twin_fidelity.py --scenarios S1,S4,S5` (khoảng 2 tiếng rưỡi) để có con số fidelity dùng được cho báo cáo.

**Loạt fidelity đầy đủ — phần TWIN (6/6 lần thử, ngày 2026-08-24).**

Chạy sau khi sửa xong ba lỗi đo đạc: tải khớp production, bộ sinh tải trong cluster, twin đủ 11 service.

Xác nhận tải đã khớp ngay ở bước đầu:

```
twin co luu luong tren 10 service, frontend 2.72 req/s   (production 2.93)
```

So với lần chạy hỏng hôm trước — chỉ 6 service và 0.20 req/s.

```
S1 dung  rollback          better
S1 sai   scale_up          no_change
S4 dung  adjust_resources  better
S4 sai   restart_pod       worse
S5 dung  adjust_resources  better
S5 sai   restart_pod       no_change
```

**Cả sáu phán quyết đều hợp lý.** Ba hành động đúng đều ra `better`. Ba hành động sai đều **không** ra `better` — hai `no_change` và một `worse`.

Xét riêng vai trò "sân tập" thì đây là tính chất quan trọng nhất của twin: **nó không bao giờ bật đèn xanh cho một hành động vô ích.** Vì `is_safe_to_promote` chỉ đúng khi phán quyết là `better`, tính chất này nghĩa là agent ở phase 5 sẽ không đưa hành động sai nào lên production trong ba kịch bản đã thử.

**Bằng chứng mạnh nhất cho bản sửa ngưỡng lưu lượng:** lần thử `S4 / adjust_resources` hôm qua ra `no_change`, hôm nay ra `better`. Cùng kịch bản, cùng hành động, chỉ khác ngưỡng `MIN_RATE_FOR_VERDICT`. Trước đó tớ mới chỉ chấm điểm lại số liệu cũ để chứng minh bản sửa đúng; lần này nó được kiểm chứng trên **dữ liệu chạy thật** — hai mức bằng chứng khác nhau và mức sau mạnh hơn hẳn.

Giải thích từng phán quyết, để đối chiếu khi viết báo cáo:

- `S1 rollback` → `better`: gỡ biến `EXTRA_LATENCY`, độ trễ biến mất hoàn toàn.
- `S1 scale_up` → `no_change`: độ trễ 6 giây được chèn vào **mỗi lần gọi**, nên hai bản sao thì mỗi lần gọi vẫn chậm đúng 6 giây. Thêm bản sao chỉ cứu được service đang quá tải.
- `S4 adjust_resources` → `better`: trả trần CPU về mức cũ, gỡ hẳn nguyên nhân.
- `S4 restart_pod` → `worse`: pod mới vẫn mang đúng trần CPU 10m, lại phải khởi động lại từ đầu dưới trần đó, nên còn tệ hơn để yên.
- `S5 adjust_resources` → `better`: như S4.
- `S5 restart_pod` → `no_change`: giống S4 về bản chất nhưng `productcatalogservice` nhẹ hơn `frontend` nên cú sốc khởi động lại không đủ vượt ngưỡng.

**TWIN FIDELITY = 100% (6/6 lần khớp).** Đây là chỉ số 7 mục 8 KLTN.md.

```
S1 dung  rollback           twin=better     production=better     KHOP
S1 sai   scale_up           twin=no_change  production=no_change  KHOP
S4 dung  adjust_resources   twin=better     production=better     KHOP
S4 sai   restart_pod        twin=worse      production=worse      KHOP
S5 dung  adjust_resources   twin=better     production=better     KHOP
S5 sai   restart_pod        twin=no_change  production=no_change  KHOP
```

Kết quả thô: `data/fidelity/20260824-113038_fidelity.json`.

**Trùng khớp còn chặt hơn mức chỉ số đòi hỏi.** Chỉ số fidelity chỉ so kết luận cuối, nhưng twin và production còn khớp cả **danh sách service tốt lên**: S1 cùng ra `frontend, productcatalogservice`, S4 cùng ra `frontend, cartservice`. Không chỉ kết luận giống nhau mà đường đi tới kết luận cũng giống nhau.

**Ý nghĩa cho giả thuyết trung tâm của đề tài.** Mục 1 KLTN.md đặt giả thuyết cần chứng minh bằng số: *agent-có-twin gây ít hành động sai hơn agent-sửa-trực-tiếp*. Con số 100% nói rằng **twin là nguồn tin đáng dùng** — nó không nói dối agent về hậu quả của hành động. Cộng với tính chất đo được ở phần twin — ba hành động sai đều không ra `better`, mà `is_safe_to_promote` chỉ đúng khi `better` — thì trong ba kịch bản này agent sẽ **không đưa hành động sai nào lên production**.

**PHẢI GHI RÕ HAI GIỚI HẠN, đừng để con số 100% bị đọc quá lời:**

1. **Chỉ 6 lần thử trên 3 kịch bản.** 100% trên 6 mẫu không phải 100% nói chung. Với 6 phép thử nhị phân, ngay cả một twin chỉ đúng 80% vẫn có khoảng 26% khả năng khớp trọn 6 lần do may. Muốn kết luận mạnh hơn thì phải nhiều kịch bản và nhiều lần lặp hơn — đó là việc của phase 6.
2. **Ba kịch bản này đều là lỗi TĨNH và cục bộ** — một biến môi trường, một trần CPU — nên twin tái hiện dễ. Chưa thử lỗi phụ thuộc trạng thái tích lũy (rò rỉ bộ nhớ, hàng đợi đầy dần) hay lỗi phụ thuộc thời điểm. Đó mới là chỗ twin dễ lệch nhất, và đề tài này chưa chạm tới.

**So sánh với loạt chạy hỏng hôm trước, để thấy ba lỗi đo đạc nặng đến mức nào:**

```
loat hong (twin 3 nguoi dung, do qua port-forward):  fidelity 50%
loat dung (tai khop, loadgen trong cluster, du 11 service):  fidelity 100%
```

Cùng bộ code, cùng kịch bản, cùng hệ thống. Khác biệt duy nhất là ba lỗi đo đạc đã sửa. Và tệ hơn con số 50%: lần khớp duy nhất của loạt hỏng khớp vì **cả hai môi trường đều sai giống nhau**.

**Đây là bài học đáng giá nhất của cả phase 4, và nên đưa vào chương phương pháp:** một hệ đo lường hỏng không báo lỗi, không ném ngoại lệ, không để lại dấu vết nào. Nó chỉ lặng lẽ cho ra những con số trông hoàn toàn hợp lý. Nếu tớ dừng lại ở loạt đầu tiên, báo cáo sẽ ghi "twin fidelity 50%, twin phản ứng mạnh hơn với sự cố ngắn hạn" — một kết luận nghe rất khoa học, có số liệu hậu thuẫn, và **hoàn toàn sai**.

### Phase 5 — ReAct loop

**Trạng thái: code xong (5.1 đến 5.4), CHƯA chạy kiểm thử trên hệ thống có lỗi.**

**5.1 — `src_thesis/agent/actions.py`.**

Bảy hành động của mục 7.2, mỗi cái một hàm kèm hàm hoàn tác và nhãn `risk_class`.

**Điểm thiết kế quan trọng nhất: mọi hàm đều ĐỌC LẠI trạng thái sau khi đổi và so với thứ vừa yêu cầu**, rồi trả về `verified=False` kèm lý do nếu không khớp. Không có bước này thì lệnh chạy xong không lỗi vẫn không chứng minh được gì.

Lý do nằm ở ba lỗi hoàn tác của phase 2, cả ba cùng một tính chất: **hệ thống vẫn hỏng trong khi công cụ báo thành công**. Với agent thì lớp lỗi này nặng hơn hẳn, vì agent **tiếp tục ra quyết định** dựa trên niềm tin rằng nó đã sửa xong — nó sẽ đi sang bước tiếp theo, kết luận nhầm, và có thể làm hỏng thêm.

**`can_apply()` chặn trước khi đụng vào cluster.** LLM có thể đề xuất hành động hợp schema nhưng không thi hành được. Ba trường hợp đã chặn:

- `reroute_traffic` và `purge_queue`: Online Boutique không có service mesh và không có hàng đợi. Hai hành động này nằm trong schema vì mục 7.2 liệt kê, nhưng hệ thống này **không thi hành được** — báo thật thay vì giả vờ làm.
- Deployment không tồn tại.
- Hành động cần tên service cụ thể nhưng nhận được `none`.

Chặn ở đây thì đếm được vào "wasted action count" mục 8; để nó ném lỗi giữa chừng thì vòng lặp chết mà không ghi được gì.

**`restart_pod` khai `undo_kind="none"` cho đúng sự thật** — pod cũ đã chết hẳn, không hoàn tác được theo nghĩa đen. Khai là hoàn tác được rồi im lặng không làm gì còn tệ hơn.

**5.2 — `src_thesis/agent/react_loop.py`, dùng LangGraph 1.2.11.**

Bảy node, 13 cạnh: `observe → reason → select →` rẽ nhánh `→ twin/apply/reject → finish_round`.

**Ba chế độ, để phase 6 so sánh:**

```
twin_verified  hanh dong `hard` phai qua twin  — de tai nay
direct         hanh dong nao cung ap thang     — DOI CHUNG, co y lam lieu
xai_only       chi chan doan, khong hanh dong  — do rieng chat luong XAI
```

Chế độ `direct` cố ý làm liều: nó tồn tại để đo **twin ngăn được bao nhiêu hành động có hại**. Không có nó thì câu "agent-có-twin an toàn hơn" không so với cái gì.

**Vòng lặp nằm NGOÀI graph, không nằm trong.** LangGraph chạy một vòng mỗi lần `invoke`, còn vòng `for` bên ngoài quyết định có đi tiếp không. Cố ý tách vì điều kiện dừng phụ thuộc vào việc đo lại hệ thống sau hành động, mà phép đo đó cần chờ đủ một cửa sổ quan sát 5 phút — nhồi cả phần chờ vào graph làm nó khó đọc và khó thử.

**Phản hồi từ twin được nhồi ngược vào prompt vòng sau.** Đây chính là phần "Observe" của ReAct: agent học từ hậu quả hành động vừa rồi. Khi twin từ chối, prompt vòng sau nhận thêm: *"Action X on Y was tested on the digital twin and REJECTED. Twin verdict: ... Do not propose this same action again."*

**Twin hỏng thì KHÔNG được coi là đã xác nhận.** Bắt mọi ngoại lệ trong nhánh twin và trả `no_change` — mặc định an toàn là không cho áp lên production. Và twin luôn bị xóa trong `finally`, kể cả khi lỗi giữa chừng.

**Một lỗi hiển thị đã sửa ngay khi chạy thử.** Lần chạy đầu in `XAI: THAT BAI` cho một ca mà XAI **chưa hề chạy** — hệ thống đang khỏe nên graph đi thẳng tới `finish_round`. Nguyên nhân: `reasoning_ok=False` là giá trị mặc định, không phân biệt được "chưa chạy" với "chạy và hỏng". Đã thêm cờ `reasoning_ran` tách ba trường hợp.

Đây lại đúng lớp lỗi đã gặp hai lần trước — ở phase 3 với `no service is close to its CPU limit` khi chưa đo được gì, ở phase 4 với `no_change` khi không đủ mẫu. **Giá trị mặc định của một trường luôn có nguy cơ bị đọc như một kết luận.**

**5.3, 5.4 — trần 3 vòng và ghi log.**

Log ghi vào `data/agent_runs/<thoi-diem>_<che-do>_<run_id>.json`, mỗi vòng một bản ghi gồm: snapshot đầu vào kèm mã băm, JSON đầy đủ của XAI, hành động đã chọn kèm mức rủi ro, phán quyết twin, kết quả thi hành, và số token.

Tổng hợp cấp ca có sẵn `actions_applied` và `actions_rejected_by_twin` — hai con số này đi thẳng vào bảng so sánh ba chế độ ở mục 8.

**Đã kiểm chứng không cần cluster:** 20 nhánh rẽ đều đúng — 15 tổ hợp chế độ × hành động, 3 phán quyết twin, 3 trạng thái sau khi quan sát, cộng ca XAI thất bại. Chạy thử `--dry-run` trên hệ thống khỏe mạnh cho kết quả đúng: agent dừng ngay ở vòng 1 với lý do "he thong da khoe manh".

**LỖ HỔNG NGHIÊM TRỌNG PHÁT HIỆN TRƯỚC KHI KIỂM THỬ: agent chạy không có ảnh nền.**

Soi lại code trước khi chạy thật thì thấy `_observe()` gọi `take_snapshot()` mà **không truyền `baseline`**. Hậu quả nằm ở `diff_graphs()`, nó phát hiện cạnh chậm theo hai cách cách nhau rất xa về độ nhạy:

```
co anh nen   : cham gap SLOW_RATIO = 3 lan so voi chinh canh do luc khoe
khong co nen : chi bat khi vuot SLOW_ABSOLUTE_MS = 500ms tuyet doi
```

Đối chiếu số đo thật của ba kịch bản:

```
S1  frontend -> productcatalogservice   157ms
S4  frontend -> checkoutservice         284ms
S5  frontend -> productcatalogservice   101ms
```

Cả ba đều **dưới 500ms**. Kiểm chứng bằng cách so lại trên chính snapshot đã lưu của phase 2:

```
S5  khong nen: 0 canh cham, 0 canh loi -> he thong "SACH"
    co nen   : 6 canh cham, 0 canh loi -> phat hien duoc

S4  khong nen: 0 canh cham, 1 canh loi
    co nen   : 7 canh cham, 1 canh loi
```

**S5 không có nền thì diff hoàn toàn sạch.** Agent sẽ báo "hệ thống khỏe mạnh" trong khi `productcatalogservice` bị bóp CPU còn 5% và mọi cạnh chậm gấp 50–118 lần. S4 may là bắt được nhờ cạnh lỗi của `adservice`, nhưng bỏ sót cả 7 cạnh chậm nên XAI sẽ chẩn đoán sai hướng.

Chỉ S2 chạy được, vì service chết sinh ra cạnh **lỗi** chứ không phải cạnh **chậm**, mà cạnh lỗi không cần nền để phát hiện. Nếu tớ chỉ kiểm thử bằng S2 — đúng kịch bản mà kế hoạch gốc nêu làm tiêu chí thành công — thì phase 5 sẽ "đạt" mà lỗ hổng vẫn nằm nguyên đó tới phase 6.

**Sửa:** thêm `src_thesis/graph/baseline.py` nạp lại ảnh nền từ snapshot đã lưu trong `data/runs/`, dựng lại `ServiceGraph` thật chứ không dùng object giả. Lấy file **mới nhất** vì cấu hình hệ thống đổi giữa các phiên — bản vá nới hạn thăm dò của `emailservice` chẳng hạn — nên nền cũ sẽ so ra báo động giả.

Log của agent ghi thêm `baseline_source` và `has_baseline`. Đọc lại một ca cũ mà không biết nó dùng nền nào thì không giải thích được vì sao nó phát hiện hay bỏ sót.

`agent_run.py` in **cảnh báo nặng** khi không tìm được nền, nói thẳng hậu quả thay vì một dòng log mờ nhạt.

**ĐÂY LÀ LẦN THỨ TƯ TRONG PROJECT CÙNG MỘT LỚP LỖI.** Đủ để thành một mục riêng trong chương phương pháp:

```
phase 3  prompt in "no service is close to its CPU limit"  khi chua do duoc gi
phase 4  verifier tra "no_change"                          khi khong du mau
phase 5  log ghi "XAI that bai"                            khi XAI chua chay
phase 5  diff bao "he thong sach"                          khi khong co nen de so
```

Cả bốn đều là **hệ thống nói "không có gì bất thường" trong khi sự thật là "không đo được"**. Cả bốn đều **im lặng** — không ngoại lệ, không cảnh báo, chỉ có một kết luận trông hoàn toàn hợp lý.

Nguyên nhân chung: **giá trị mặc định của một trường luôn có nguy cơ bị đọc như một kết luận.** `False` mặc định của `reasoning_ok`, danh sách rỗng mặc định của `slow_edges`, `no_change` mặc định khi không đủ dữ liệu — không cái nào được thiết kế để mang nghĩa "chưa biết", nhưng cả ba đều bị đọc như "đã biết và không có gì".

Quy tắc rút ra, áp dụng cho mọi chỗ còn lại của đề tài: **mọi hàm trả về kết luận đều phải phân biệt được ba trạng thái** — đã đo và có, đã đo và không có, chưa đo được. Gộp hai cái sau lại là nguồn sai lầm tốn kém nhất trong cả project này.

**5.5 — KIỂM THỬ THẬT.**

**Ca 1: S2 (`currencyservice` tắt hẳn), chế độ `twin_verified`. THÀNH CÔNG.**

```
VONG 1  3 canh loi, 0 canh cham, 7 canh thieu
        XAI: currencyservice / crash (tin cay 0.96)
        hanh dong: scale_up tren currencyservice [easy]
        ket qua: DA AP — so ban sao 0 -> 1

VONG 2  0 canh loi, 0 canh cham, 0 canh thieu
        he thong KHOE MANH, dung
```

Log: `data/agent_runs/20260824-151213_twin_verified_test-s2.json`.

Đây đúng tiêu chí thành công mà `KLTN-PLAN.md` đặt cho phase 5: tiêm F2 vào `currencyservice`, agent tự đưa về 1 bản sao, hệ thống hồi phục, toàn bộ ghi vào một file JSON.

Ba chi tiết xác nhận thiết kế chạy đúng:

- XAI chẩn đoán đúng ngay vòng 1, độ tin cậy 0.96 — khớp với kết quả phase 3 (S2 đạt 100% qua 5 lần chạy).
- `scale_up` thuộc mức `easy` nên đi thẳng lên production, **không qua twin**. Đúng phân mức rủi ro mục 7.3.
- Vòng 2 in `XAI khong chay — he thong khoe manh` thay vì `XAI that bai`. Bản sửa hiển thị hôm nay hoạt động đúng.

**Một phát hiện phụ về quy trình: agent và sổ theo dõi lỗi là hai thứ độc lập.**

Sau khi agent sửa xong, `currencyservice` đã về 1/1 nhưng `inject.py --status` vẫn báo "đang tiêm lỗi", vì `active_fault.json` chỉ được `inject.py` cập nhật. Agent sửa hệ thống thật nhưng không biết gì về sổ sách của công cụ tiêm lỗi.

Không phải lỗi — hai công cụ vốn độc lập là đúng, agent không nên phụ thuộc vào việc có ai đó ghi sổ. Nhưng **phải nhớ chạy `--revert` sau mỗi ca để dọn sổ**, nếu không thì ca sau `inject.py` sẽ từ chối tiêm vì tưởng còn lỗi cũ. Đã ghi vào quy trình.

**Ca 2: S1 (`productcatalogservice` chậm 6 giây), chế độ `twin_verified`. KẾT QUẢ GIÀU THÔNG TIN NHẤT CỦA CẢ PHASE.**

```
VONG 1  0 canh loi, 5 canh cham, 0 canh thieu
        XAI: productcatalogservice / latency (tin cay 0.92)
        hanh dong: scale_up [easy] -> DA AP, 1 -> 2 ban sao

VONG 2  0 canh loi, 5 canh cham, 0 canh thieu
        XAI: productcatalogservice / latency (tin cay 0.92)
        hanh dong: restart_pod [hard] -> twin: WORSE -> BI CHAN

VONG 3  0 canh loi, 15 canh cham, 0 canh thieu
        XAI THAT BAI: API 413 Request too large
```

Log: `data/agent_runs/20260824-154002_twin_verified_test-s1.json`.

**Bốn phát hiện, cái đầu tiên là thứ cả đề tài cần chứng minh.**

**1. TWIN ĐÃ CHẶN MỘT HÀNH ĐỘNG CÓ HẠI — bằng chứng trực tiếp cho giả thuyết mục 1 KLTN.md.**

Vòng 2 agent định `restart_pod`. Vì hành động này thuộc mức `hard`, nó bị bắt phải thử trên twin trước. Twin phán `worse`, nên nó **không bao giờ chạm vào production**. Đây chính là cơ chế mà đề tài đặt ra: agent-có-twin gây ít hành động có hại hơn agent-sửa-trực-tiếp.

Đáng chú ý hơn: phán quyết `worse` của twin ở đây **khớp với kết quả fidelity phase 4**, nơi `restart_pod` trên twin cũng ra `worse` và production cũng ra `worse`. Twin không chỉ chặn đúng, nó chặn vì lý do đúng.

**2. Bản vá ảnh nền hoạt động, và đây là ca chứng minh.** Vòng 1 bắt được 5 cạnh chậm. Không có bản vá thì diff ra sạch, agent dừng ngay vòng 1, và **twin không bao giờ được dựng** — cả nhánh quan trọng nhất của phase 5 sẽ không bao giờ chạy.

**3. XAI chọn sai hành động ở vòng 1.** `scale_up` không gỡ được độ trễ chèn vào mỗi lần gọi, nên thêm bản sao là vô ích. Khớp đúng số đo phase 3: S1 có độ chính xác hành động chỉ 20–40%, và quy tắc sửa việc này nằm trong gói v5 đã bị loại vì làm tổng thể tệ đi. Đây là món nợ đã biết, giờ nhìn thấy hậu quả thật của nó.

Hệ quả đo được: sau `scale_up`, số cạnh chậm tăng từ 5 lên 15 ở vòng 3. **Hành động vô ích không trung tính — nó làm hệ thống tệ hơn.** Con số này đi thẳng vào chỉ số "harmful action count" mục 8.

**4. Lỗi thật: API 413 ở vòng 3.** Groq gói miễn phí giới hạn 8000 token mỗi phút, mà prompt của agent khoảng 6000 cộng `max_tokens=4000` là vượt trần. Vòng 3 prompt còn phình to hơn vì 15 cạnh chậm.

**413 khác 429 ở chỗ căn bản:** 429 là "dùng quá nhanh", chờ thì hết; 413 là "MỘT request này đã quá to", chờ bao lâu cũng không hết. Đã sửa: gặp 413 thì tự hạ `max_tokens` xuống một nửa rồi thử lại, hạ tới sàn 1200 mà vẫn không được thì báo thật và khuyên đổi sang OpenAI. Mặc định của `agent_run.py` đổi sang `--provider openai` vì lý do này.

**Một điểm dở về theo dõi đã sửa luôn:** `agent_run.py` chỉ in kết quả sau khi cả ca chạy xong, nên ca này chạy 15 phút trong im lặng. Đúng bài học đã trả giá ở phase 3 khi loạt đánh giá treo 16 phút không in gì. Đã thêm gọi lại sau mỗi vòng để in ngay.

**Một phát hiện phụ: agent để lại hậu quả mà `inject.py --revert` không dọn hết.** Sau ca này `productcatalogservice` còn 2 bản sao do `scale_up`, và `--revert` chỉ gỡ biến `EXTRA_LATENCY` chứ không biết gì về hành động của agent. Phải `kubectl scale` về 1 bằng tay. Với phase 6 chạy hàng loạt thì đây là chỗ phải tự động hóa, nếu không mỗi ca sẽ bắt đầu từ một trạng thái khác ca trước.

**Ca 3: S1 chế độ `direct` (đối chứng, không có twin).**

```
v1  6 canh cham | productcatalogservice/latency | scale_up[easy]         | KHONG OK
v2  5 canh cham | productcatalogservice/latency | adjust_resources[easy] | KHONG OK
v3  3 canh loi, 5 canh cham | productcatalogservice/pod_kill | no_action | OK
ket thuc: CON LECH — agent chon khong lam gi
```

**Cặp ca 2 với ca 3 KHÔNG thành đối chứng sạch, và phải nói rõ điều này.**

Ý định ban đầu là giữ mọi thứ giống nhau và chỉ đổi một biến — có twin hay không — để xem twin ngăn được gì. Nhưng agent chế độ `direct` **không hề chọn `restart_pod`**; nó chọn hai hành động mức `easy` rồi kết thúc bằng `no_action`. Biến "có twin" không phải khác biệt duy nhất, vì **bản thân LLM dao động giữa hai lần chạy**.

Đây là hạn chế thật của việc chạy một lần mỗi chế độ. Một cặp ca đơn lẻ không kết luận được gì về nhân quả khi tác nhân được đo còn ngẫu nhiên. Phase 6 chạy 5 lần mỗi chế độ chính là để xử lý chuyện này — và giờ đã có bằng chứng cụ thể cho thấy vì sao con số đó không được cắt bớt.

**Kết quả ca 2 vẫn giữ nguyên giá trị**, chỉ là phải phát biểu cho đúng: nó chứng minh **cơ chế chặn hoạt động** trên hệ thống thật, không chứng minh **twin làm giảm hành động có hại tính trung bình**. Cái sau cần phase 6.

**Lỗi thứ hai bắt được trong ca 3, và là kiểu NGƯỢC với lớp lỗi vẫn theo dõi.**

Vòng 2 báo `KHONG AP DUOC — tran CPU 200m -> 400m (yeu cau 0.4)`. Nhìn kỹ thì hành động **đã thành công**: trần CPU đổi từ 200m lên 400m đúng ý muốn. Kubernetes chuẩn hoá `"0.4"` thành `"400m"`, mà tớ so chuỗi thẳng nên `"400m" != "0.4"` và kết luận thất bại.

Bốn lỗi trước đều là hệ thống báo "ổn" khi thật ra "không biết". Lỗi này ngược lại — báo **thất bại** khi thật ra **thành công**. Nhưng gốc rễ giống nhau: **so sánh mà không tính đến cách biểu diễn**.

Hậu quả nếu không sửa còn nặng hơn vẻ ngoài: agent thấy hành động "thất bại" sẽ thử hành động khác, trong khi trần CPU đã bị đổi rồi — nó chồng thay đổi lên một hệ thống mà nó tưởng chưa đổi gì.

Sửa: thêm `cpu_to_millicores()` so theo số millicore thay vì so chuỗi. Áp cho cả ba chỗ: kiểm tra trước khi đổi, kiểm chứng sau khi đổi, và kiểm chứng khi hoàn tác.

**Phát hiện phụ: `inject.py --revert` không dọn được hậu quả của agent.**

Sau ca 2, `productcatalogservice` còn 2 bản sao; sau ca 3, trần CPU còn 400m. `--revert` chỉ biết hoàn tác thứ **nó** đã tiêm, không biết gì về những gì agent đã đổi. Phải dọn tay bằng `kubectl scale` và `kubectl set resources`.

Với phase 6 chạy hàng loạt thì đây là chỗ **bắt buộc phải tự động hoá**: không dọn thì mỗi ca bắt đầu từ một trạng thái khác ca trước, và toàn bộ phép so sánh giữa các chế độ mất ý nghĩa. `ActionResult` đã lưu sẵn `undo_kind` và `undo_args` nên `ActionExecutor.undo()` làm được việc này — chỉ cần gọi ở cuối mỗi ca.

**CỔNG CHẶN PHASE 5: ĐẠT.**

Yêu cầu là file log JSON phải đủ để dựng lại toàn bộ câu chuyện một ca. Kiểm chứng bằng cách đọc lại cả ba file và in ra được: chế độ, số vòng, thời gian, token, ảnh nền đã dùng, và với từng vòng — trạng thái hệ thống, chẩn đoán của XAI, hành động đã chọn kèm mức rủi ro, phán quyết twin, kết quả thi hành.

```
test-s2         twin_verified  2/3 vong   399s   3873+1162 token  -> KHOE
test-s1         twin_verified  3/3 vong  1182s   7761+2883 token  -> CON LECH
test-s1-direct  direct         3/3 vong   180s  12234+1275 token  -> CON LECH
```

**Số liệu đi thẳng vào mục 8 KLTN.md:** chế độ `twin_verified` mất 1182 giây và chặn 1 hành động; chế độ `direct` mất 180 giây và không chặn gì. Đây chính là đánh đổi mà giả thuyết dự đoán — **twin an toàn hơn nhưng chậm hơn**. Với S1 thì chậm hơn gấp 6.5 lần, phần lớn là thời gian dựng twin và chờ hai cửa sổ quan sát.





### Phase 6 — Thí nghiệm

## Bước 6.0 — Viết xong bộ chạy thí nghiệm (2026-08-24)

Code đã đủ để chạy 75 ca; chưa chạy ca thật nào.

**Sáu file mới, và vì sao mỗi file tồn tại:**

| File | Việc nó làm |
|---|---|
| `src_thesis/faults/library.py` | Nạp và tiêm kịch bản. Tách ra để `inject.py` (chạy tay) và runner (chạy tự động) dùng chung một bản. |
| `src_thesis/eval/preflight.py` | Kiểm tra sạch trước mỗi ca, chờ ảnh nền sạch, chụp nền mới cho cả phiên. |
| `src_thesis/eval/metrics.py` (bổ sung) | Chỉ số 3 tới 7 mục 8. |
| `src_thesis/eval/runner.py` | Vòng lặp 75 ca. |
| `scripts/eval_run.py` | Dòng lệnh, có `--resume` và `--summary`. |
| `scripts/plot_results.py` | Bốn đồ thị cho chương kết quả. |

**Ngưỡng của chỉ số 4 và 5, chốt TRƯỚC khi chạy một ca nào:**

```
harmful : error rate tăng >= 2 điểm phần trăm  HOẶC  p95 tăng >= 20%
helpful : error rate giảm >= 2 điểm phần trăm  HOẶC  p95 giảm >= 20%
wasted  : mọi thay đổi dưới ngưỡng, hoặc hành động không thi hành được
unknown : không service nào đạt 0.3 req/s ở cả hai lần đo
```

Chốt trước là bắt buộc về mặt phương pháp: với 75 ca thì **luôn tìm được** một ngưỡng làm giả thuyết trông đúng, nên chọn ngưỡng sau khi nhìn số liệu là tự lừa mình.

Con số 2 điểm phần trăm lấy đúng `MIN_ERROR_DELTA` của `verifier.py`. Hai chỗ phải cùng một thước: nếu twin phán `worse` theo một thước mà chương kết quả đếm `harmful` theo thước khác, thì câu "twin chặn được hành động có hại" không kiểm chứng được — nó so hai thứ không so được với nhau.

**Năm kết luận về tác động, không phải ba.** `unknown` tách hẳn khỏi `neutral`. Đây là lần thứ sáu cùng một lớp lỗi xuất hiện trong đề tài này, và lần này nó bị chặn ngay từ lúc thiết kế thay vì phải sửa sau khi số liệu đã sai.

**MTTR của ca không hồi phục trả về `None`, không trả về một con số lớn.** Nhét ca không hồi phục vào trung bình bằng "thời gian đã chờ" kéo trung bình xuống, và nó nói dối theo hướng làm chế độ tệ trông tốt hơn. Thống kê gọi kiểu dữ liệu này là **bị cắt cụt** (censored). Mọi bảng và mọi đồ thị đều in `n_censored` cạnh `mttr_mean_s`; hai số này phải đọc cùng nhau mới có nghĩa.

Chế độ `baseline` không sửa gì nên phần lớn ca của nó sẽ bị cắt cụt ở 900 giây. Đó chính là điều cần chứng minh: **không có agent thì lỗi cấu hình nằm đó mãi.** Nhưng 900 giây là một **cái trần**, không phải một phép đo — ghi "baseline không hồi phục trong 900 giây" là đúng, ghi "MTTR của baseline là 900 giây" là sai.

**Trả xong món nợ dọn dẹp của phase 5.** Runner hoàn tác hành động của agent bằng `ActionExecutor.undo()`, đọc `undo_kind` và `undo_args` đã lưu sẵn trong `ActionResult`.

Thứ tự hoàn tác là **ngược lại thứ tự tác động**, giống gỡ chồng sách. Ví dụ S5 bóp trần CPU của `productcatalogservice` xuống 10m rồi agent nâng lên 400m: hoàn tác agent đưa về 10m, hoàn tác lỗi đưa về giá trị gốc. Làm ngược lại thì trần CPU kẹt ở 400m sau khi ca kết thúc, và ca sau bắt đầu từ một hệ thống khác.

**Thứ tự chạy các ca: lặp ngoài, kịch bản giữa, chế độ trong.** Chạy hết 15 ca của lần lặp 1 rồi mới sang lần lặp 2, chứ không chạy hết 5 lần của S1 rồi mới sang S2. Ngắt giữa chừng — mà một phiên 25 tiếng thì gần như chắc chắn có ngắt — thì vẫn còn một lượt quét đầy đủ mọi chế độ và mọi kịch bản để so sánh, thay vì có đầy đủ S1 và không có gì khác. `--resume` bỏ qua ca đã có file nên chạy tiếp được nhiều buổi.

## Hai lỗi bắt được ngay khi viết code

**1. Cache của LLM sẽ làm độ lệch chuẩn ra 0 một cách giả tạo.**

`XaiReasoner` nhớ kết quả trên đĩa, khóa theo dấu vân tay của phần lệch. Mục 8 bắt mỗi kịch bản chạy 5 lần **để có độ lệch chuẩn**, mà bật cache thì lần thứ hai trở đi lấy lại đúng đáp án cũ. Con số độ lệch chuẩn vẫn in ra, vẫn đẹp, và hoàn toàn vô nghĩa — nó đo cache chứ không đo mức dao động của LLM.

Runner tắt cache bằng `use_cache=False`. Đây là lỗi sẽ không bao giờ báo gì cả, chỉ âm thầm biến kết quả thành vô giá trị.

**2. Hàm đọc twin fidelity in ra 0.0% (0/12) trong khi kết quả thật là 100% (6/6).**

Tớ tự đếm lại từ mảng `trials`, mà `trials` có **hai dòng mỗi phép thử** — một của twin, một của production — nên 6 phép thử thành 12 dòng, và không dòng nào có trường `match`. File đã ghi sẵn `fidelity`, `matches`, `total` từ phase 4; đọc thẳng ba trường đó là xong.

Bắt được nhờ chạy thử một ca giả và **nhìn con số có hợp lý không**, chứ hàm không hề ném lỗi. Đây đúng là kiểu lỗi mà chương kết quả sợ nhất: một con số sai in ra bình thản giữa những con số đúng.

**Sửa `--dry-run` mô tả sai trong cả `eval_run.py` lẫn `agent_run.py`.** Cả hai ghi "không đụng tới cluster và không gọi LLM thật". Sai: `--dry-run` vẫn đọc cluster và vẫn gọi LLM, nó chỉ không sửa gì và không chờ. Tức là **vẫn tốn tiền API**. Câu mô tả cũ dễ làm người chạy tưởng dry-run là miễn phí.

## Đã kiểm chứng những gì

Chưa chạy ca thật. Đã kiểm bằng dữ liệu giả và một ca `--dry-run`:

- Bảy trường hợp phân loại tác động, kể cả ca vừa có service tốt lên vừa có service xấu đi (phải ra `harmful`, vì lỗi xếp trên độ trễ) và ca lưu lượng quá thấp (phải ra `unknown`, không được ra `neutral`).
- MTTR trả `None` đúng cho ca không hồi phục.
- `_score` trên một báo cáo giả có 3 vòng, trong đó vòng 2 bị twin chặn: ra đúng root cause, Jaccard 1.0, MTTR 1300 giây, 1 hành động có hại, 1 hành động bị twin chặn, 0.011 đô la.
- Thứ tự hoàn tác: đúng ngược chiều, bỏ qua `no_action` và bỏ qua hành động đã thất bại.
- Bốn đồ thị vẽ ra file được.

## Chia 75 ca ra nhiều buổi

Không phải chạy một mạch. Mỗi ca là một đơn vị khép kín — sạch, tiêm, chạy, đo, hoàn tác — nên cắt giữa hai ca không ảnh hưởng gì tới ca nào.

**Cách chia đề nghị: một lần lặp mỗi buổi, 5 buổi.**

```
buoi 1  python -u scripts/eval_run.py --repeats 1
buoi 2  python -u scripts/eval_run.py --resume <ma-phien> --repeats 2
buoi 3  python -u scripts/eval_run.py --resume <ma-phien> --repeats 3
buoi 4  python -u scripts/eval_run.py --resume <ma-phien> --repeats 4
buoi 5  python -u scripts/eval_run.py --resume <ma-phien> --repeats 5
```

Mỗi buổi 15 ca, khoảng 5,5 giờ. Dùng **cùng một mã phiên** cho cả năm buổi; `--resume` bỏ qua ca đã có file.

Chia theo lần lặp tốt hơn chia theo kịch bản ở một điểm quan trọng: **sau buổi 1 đã có một lượt quét đầy đủ 3 chế độ × 5 kịch bản**. Nếu hết thời gian ở buổi 3 thì vẫn còn một bảng kết quả hoàn chỉnh với n = 3, thay vì có S1 đủ 5 lần và bốn kịch bản kia trắng.

Ai muốn canh theo đồng hồ thì dùng `--budget-minutes 360`. Nó dừng **trước khi bắt đầu một ca mới**, không cắt ngang ca đang chạy — cắt giữa chừng sẽ để lại lỗi đã tiêm và hành động của agent còn nguyên trên hệ thống qua đêm.

**Hạ mốc bỏ cuộc của chế độ `baseline` từ 900 xuống 600 giây.** 600 giây bằng đúng hai cửa sổ quan sát: cần ít nhất hai vì cửa sổ dài 300 giây, nên ngay sau khi hệ thống thật sự khỏe lại thì cửa sổ vẫn còn giữ dữ liệu lúc hỏng — chờ chưa đủ hai cửa sổ thì một ca **đã** hồi phục vẫn bị chấm là không hồi phục.

Không cần dài hơn, vì bốn trong năm kịch bản là lỗi cấu hình mà Kubernetes không bao giờ tự hoàn tác; chỉ S3 tự khỏi sau khoảng 30 giây. Tiết kiệm hơn 2 giờ máy trên 25 ca baseline. Tổng còn 27,5 giờ.

**Một biến lạ mà việc chia buổi tạo ra: ảnh nền đổi giữa các buổi.**

Runner chụp ảnh nền mới ở đầu mỗi phiên. Ảnh nền quyết định độ nhạy của phép phát hiện cạnh chậm — chậm gấp 3 lần so với **chính cạnh đó** lúc khỏe. Nền buổi 1 và nền buổi 2 khác nhau nghĩa là ca của hai buổi được chấm bằng hai cái thước khác nhau.

Chụp nền mới mỗi buổi vẫn đúng hơn dùng nền cũ, vì máy khởi động lại thì độ trễ tuyệt đối đổi theo và nền cũ sẽ sinh báo động giả hàng loạt. Nhưng mức lệch phải **nhìn thấy được**. Đã thêm `describe_baseline_drift()`: mỗi buổi in ra số cạnh mất và thêm, độ trễ trung vị đổi mấy lần, và cảnh báo nếu trung vị lệch quá 1,5 lần — nửa đường tới ngưỡng `SLOW_RATIO = 3`.

Ai chắc chắn máy không khởi động lại giữa các buổi thì ghim nền bằng `--baseline-file <duong-dan>`. Đánh đổi rõ ràng: mọi ca cùng một thước, nhưng nền ghim sai thì sai cho toàn bộ phiên.

Đây vẫn là cùng một nguyên tắc của phase 4: so hai thứ thì mọi biến ngoài biến đang khảo sát phải khớp, và khớp một nửa nguy hiểm hơn không khớp gì vì nó tạo cảm giác đã kiểm soát.

## Còn nợ trước khi chạy đủ 75 ca

- **XAI chọn sai hành động cho S1.** `scale_up` không gỡ được độ trễ chèn mỗi lần gọi. Quy tắc sửa nằm trong gói prompt v5 đã bị loại vì làm tổng thể tệ đi từ 90% xuống 66.7%; phải tách ra thử **từng quy tắc một**, không thử cả gói.
- **Chạy thử vài ca trước** để đo thời gian thật một ca, rồi mới đặt lịch cho đủ 75 ca.
- Hành động vô ích **không trung tính**: sau `scale_up` ở ca S1, số cạnh chậm tăng từ 5 lên 15. Chỉ số 5 đếm nó là `wasted`, nhưng phần thảo luận phải nói rõ là "vô ích" không đồng nghĩa "vô hại".


## Hạn chế đã biết (đưa vào báo cáo)

- Ba service không phát span server: `cartservice`, `shippingservice`, `adservice`. `redis-cart` hoàn toàn không nhìn thấy. Cạnh tới `cartservice` và `shippingservice` suy ra từ span client của `frontend` và `checkoutservice`.
- Online Boutique kiến trúc phẳng, lỗi ít lan nhiều tầng.
- Twin và production không chạy song song, nên MTTR của chế độ twin-verified có cộng thêm thời gian dựng twin.

## Số liệu cuối

Cập nhật 2026-11-09. Mọi con số dưới đây tính lại được bằng script, không chép tay:
`scripts/v2_analyze.py` (bảng tổng hợp) và `scripts/rescore_wait.py` (độ nhạy).

### v1 — phiên chính `20261027-094812` (72 ca, prompt v6, `gpt-4.1-mini`, máy ảo GCP)

Tag `phase-6-done`. Ảnh nền `data/runs/20261027-094813_baseline-clean.json`.

- Đúng nguyên nhân gốc 28/28 ở cả direct và twin (trên ca có chẩn đoán). Loại lỗi
  23/28, hành động đầu đúng đáp án 23/28, Jaccard lan truyền 0,694 và 0,713.
- Hồi phục: baseline 2/12, direct 10/30, twin 12/30. MTTR trên ca hồi phục 212 / 422 /
  652 giây — tập ca hồi phục khác nhau nên không so trực tiếp được.
- **Gây hại: direct 9, twin 10.** Hiệu mỗi ca (twin − direct) 0,03, khoảng tin cậy
  bootstrap 95% [−0,27; 0,33]. Giả thuyết trung tâm KHÔNG được ủng hộ.
- 17/19 hành động gây hại thuộc mức thấp (Bảng 2), áp thẳng không qua twin.
- Twin chặn 4/6 lần thử; 2 lần twin phán tốt lên thì production không tốt lên (0/2).
  Tự hoàn tác 8 lần. Độ trung thực twin 5/6 (`data/fidelity/20261027-071231_fidelity.json`).

### v1 — thí nghiệm phụ `20261029-030608` (15 ca, ép `adjust_resources` qua twin)

- Trên S1, S5, S6: gây hại 4, so với 10 của twin theo Bảng 2 và 9 của direct. Thời
  gian agent mỗi ca 1 611 giây so với 1 228 giây (+31%). 0/15 ca hồi phục.

### Độ nhạy của chỉ số gây hại (`data/analysis/do_nhay_gay_hai.md`)

Chấm lại theo tổng thời gian chờ (lỗi xét trước, ngưỡng 20% như gốc, áp lên tổng):

- Phiên chính: direct 9 → 8, twin 10 → 9. **Kết luận không đổi.**
- Thí nghiệm phụ: 4 → 1. Cả 3 lần "gây hại" ở TH5 là độ trễ nhảy ở service nhỏ. Trên
  S1, S5, S6, twin theo Bảng 2 gây hại 9, ép qua twin còn 1: tác dụng của cổng vào
  twin MẠNH hơn con số định nghĩa gốc cho thấy.

### v2 — đang chạy

Đăng ký trước: `docs/v2-dang-ky-truoc.md`. Ba phiên `v2-main`, `v2-twin120`,
`v2-twin600`, chạy bằng `scripts/v2_pipeline.sh`. Số liệu điền khi phiên xong.
