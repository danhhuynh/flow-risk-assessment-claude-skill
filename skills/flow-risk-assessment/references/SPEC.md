# Flow Risk Assessment (FRA)

**Hệ thống dự đoán rủi ro production từ PR diff, dựa trên Business Flow Manifest**

Version 1.0 · Language-agnostic · Infrastructure-agnostic

---

## 0. Mục tiêu và phạm vi

### Bài toán

Cho một PR diff, trả lời ba câu hỏi:

1. **Thay đổi này chạm vào những business flow nào?** (impact mapping)
2. **Xác suất nó gây sự cố production là cao hay thấp, và vì sao?** (risk quantification)
3. **Nên deploy nó như thế nào?** (decision output)

### Nguyên tắc thiết kế

| Nguyên tắc | Hệ quả thiết kế |
|---|---|
| Không phụ thuộc ngôn ngữ | Chỉ dùng `git` + tree-sitter/ctags + format dữ liệu trung gian |
| Không phụ thuộc hạ tầng | Mọi nguồn dữ liệu ngoài đều qua adapter interface |
| Số phải có nguồn gốc | LLM không bao giờ sinh ra con số. Số đến từ log, git history, coverage |
| Suy giảm mượt | Thiếu dữ liệu → giảm độ chi tiết output, không sập, không đoán bừa |
| Output là quyết định | Không phải điểm số. Là chiến lược deploy cụ thể |
| Manifest sống cùng code | Nằm trong repo, review cùng PR, có drift detection |

### Ngoài phạm vi

- **Không** thay thế test, review, hay static analysis. FRA xếp hạng và định tuyến, không phát hiện lỗi.
- **Không** gác cổng merge dựa trên xác suất chưa hiệu chỉnh (xem §16).
- **Không** dự đoán ở mức từng dòng. Đơn vị nhỏ nhất là symbol (function/method).

---

## 1. Kiến trúc tổng quan

```
┌──────────────────────────────────────────────────────────────────┐
│  BOOTSTRAP (chạy 1 lần, refresh định kỳ)                         │
│                                                                  │
│  Code ──┬─→ Entry point discovery ──┐                            │
│         │                            ├─→ Flow clustering ──┐     │
│  Traces ┴─→ Execution traces ────────┘                     │     │
│                                                             ▼     │
│  Incidents ──→ Invariant mining ──────────────→  FLOW MANIFEST   │
│  Postmortems                                     (.fra/flows/)   │
└──────────────────────────────────────────────────────────────────┘
                                                        │
                                                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  PER-PR (chạy trong CI)                                          │
│                                                                  │
│  git diff ──→ Change Units ──→ Impact Closure ──→ Flows at risk  │
│                    │                  │                  │       │
│                    ▼                  ▼                  ▼       │
│              change_kind        propagation        P(R) từ log    │
│                    │                  │                  │       │
│                    └────────┬─────────┴──────────────────┘       │
│                             ▼                                     │
│                     PIE CHAIN SCORING ←── diff coverage           │
│                             │           ←── mutation score        │
│                             │           ←── JIT base rate         │
│                             ▼                                     │
│                     Fault Tree / BN ←── LLM invariant check       │
│                             │                                     │
│                             ▼                                     │
│                   DECISION OUTPUT (deploy strategy)               │
└──────────────────────────────────────────────────────────────────┘
                                                        │
                                                        ▼
┌──────────────────────────────────────────────────────────────────┐
│  CALIBRATION LOOP (chạy hàng tuần)                               │
│  Deploy → Incident window → SZZ label → Brier score → tune       │
└──────────────────────────────────────────────────────────────────┘
```

### Layout trong repo

```
.fra/
├── config.yaml                 # cấu hình adapter, ngưỡng, trọng số
├── flows/
│   ├── checkout.yaml           # 1 file / flow
│   ├── gift_redemption.yaml
│   └── user_registration.yaml
├── trees/
│   └── gift_redemption.yaml    # fault tree, chỉ cho flow critical
├── calibration/
│   ├── labels.jsonl            # PR → outcome, sinh bởi SZZ
│   └── model.json              # trọng số đã fit
└── cache/
    └── symbols/                # symbol index, gitignored
```

---

## 2. Mô hình lý thuyết

### 2.1 Chuỗi PIE

Nền tảng là **PIE model** (Voas 1992), mở rộng **RIPR** (Li & Offutt 2014). Một fault chỉ thành failure khi đi qua trọn chuỗi:

Với mỗi cặp `(change_unit u, flow f)`:

```
P(fail | u, f) = P(D_u) · P(R_uf) · P(I_u) · P(P_uf) · P(¬detect_u)
```

| Ký hiệu | Ý nghĩa | Nguồn dữ liệu | Độ tin cậy |
|---|---|---|---|
| `P(D_u)` | Diff chứa defect | JIT model từ SZZ history | Cần hiệu chỉnh |
| `P(R_uf)` | Flow `f` thực thi `u` trong prod | **Access log / APM / traces** | **Cao — dữ liệu cứng** |
| `P(I_u)` | Defect làm hỏng state | Mutation score, change_kind | Trung bình |
| `P(P_uf)` | State hỏng lan tới kết quả nghiệp vụ | Impact closure + invariant | Trung bình |
| `P(¬detect_u)` | Thoát test + review + canary | Diff coverage, canary math | Cao |

**Điểm then chốt:** `P(R)` thường là thừa số quyết định và cũng là thừa số bạn biết chính xác nhất. Nếu chỉ triển khai được một thứ, triển khai nó — xem §9.

### 2.2 Tổng hợp với common-cause

Một PR chứa nhiều change unit. Chúng **không độc lập**: cùng tác giả, cùng mô hình tư duy, cùng một hiểu sai về spec. Đây là **common cause failure** kinh điển trong reliability engineering, xử lý bằng **beta-factor model**:

```
p_i = P(fail | u_i, f)          cho mỗi change unit i

P_ind = 1 − Π (1 − (1−β)·p_i)    thành phần độc lập
P_cc  = β · max(p_i)              thành phần nguyên nhân chung
P_f   = P_ind + P_cc − P_ind·P_cc
```

`β` = tỷ lệ sự cố lịch sử mà **nhiều** change unit trong cùng PR bị liên đới. Reliability engineering dùng mặc định 0.1–0.3. Hiệu chỉnh từ dữ liệu của bạn: trong các incident đã gán nhãn, đếm tỷ lệ có ≥2 file bị nhắc trong postmortem.

Bỏ qua `β` là sai lầm phổ biến nhất khi tự dựng mô hình kiểu này — nó khiến PR lớn bị đánh giá thấp một cách hệ thống, vì phép nhân `Π(1−p_i)` giả định các thay đổi bù trừ độc lập cho nhau.

### 2.3 Cut set — nguồn giá trị lớn nhất

Xác suất là phần dễ sai. Phần **không sai** là cấu trúc.

**Minimal cut set** = tập basic event nhỏ nhất mà nếu cùng xảy ra thì top event xảy ra. Phép kiểm tra:

> PR này có chạm **≥2 basic event trong cùng một minimal cut set** không?

Nếu có, các lớp phòng thủ độc lập bị chọc **cùng lúc**. Rủi ro nhảy không tuyến tính và không mô hình chấm điểm nào phát hiện được.

Ví dụ điển hình: một PR sửa cả logic validation *và* job reconciliation vốn để bắt dữ liệu sai. Từng thay đổi vô hại. Cùng lúc thì hai lỗ phô mai Thụy Sĩ thẳng hàng.

**Kết quả này là boolean, không phải xác suất** — nên nó đúng bất kể mô hình đã hiệu chỉnh hay chưa. Đây là lý do §11 nên được triển khai sớm hơn §12.

### 2.4 Vì sao FTA cần chuyển thành Bayesian Network

FTA cổ điển có ba giới hạn trong ngữ cảnh này:

| Giới hạn FTA | Hệ quả | Cách BN xử lý |
|---|---|---|
| Giả định basic event độc lập | Sai vì common cause của PR | Conditional dependency tường minh |
| Nhị phân (fail / not fail) | Không diễn tả được "chậm", "sai một phần" | Node đa trạng thái |
| Không cập nhật được | Sentry báo lỗi thật cũng không dùng được | Evidence propagation, suy diễn ngược |

Phép biến đổi FTA → BN đã được chuẩn hóa (Bobbio et al. 2001): OR gate → node với CPT là phép hợp, AND gate → CPT là phép giao, basic event → root node với prior.

**Chiến lược thực dụng:** viết và review bằng FTA (cây dễ đọc, team hiểu được), tính bằng BN. Cây là interface cho người, BN là engine.

### 2.5 Hai lớp bài toán riêng biệt

Đây là phân định quan trọng nhất của toàn bộ thiết kế. **Business flow và code standard không dùng chung engine.**

**Code standard — xác suất tồn tại là 0 hoặc 1**

Vi phạm chuẩn code là tất định và phát hiện được. Linter/type checker trả lời dứt khoát. Không có gì để dự đoán.

Cái cần tính là hướng ngược: `P(sự cố | vi phạm tồn tại)`. Phân tán cực rộng, và trộn chúng lại là cách nhanh nhất để phá hủy tín hiệu:

| Lớp finding | P(manifest thành sự cố) | Xử lý trong FRA |
|---|---|---|
| Format, naming, import order | ≈ 0 | **Loại hẳn.** Đưa vào chỉ tạo nhiễu |
| Unused variable, dead code | ≈ 0 | Loại |
| Null/undefined handling, type coercion | Thấp–trung bình | Đưa vào `P(I)` |
| Thiếu error handling trên I/O boundary | Trung bình | Đưa vào `P(I)` |
| Thiếu transaction, thiếu idempotency, N+1, unbounded query, resource leak | Cao | Đưa vào `P(I)` với trọng số cao |
| Thiếu authz check, injection, secret hardcode | Ruin risk | **Bỏ qua xác suất. Gate cứng** |

Engine: static analysis → phân loại → tra bảng manifestation đã hiệu chỉnh bằng incident history của bạn. Không cần LLM, không cần xác suất.

**Business flow — xác suất tồn tại là ẩn số thật**

Lỗi nghiệp vụ không phát hiện được bằng static analysis vì code hoàn toàn hợp lệ mà vẫn sai ý định. `total = price * qty` đúng chuẩn tuyệt đối và vẫn sai nếu thiếu chiết khấu.

Đây là chỗ **duy nhất** LLM cần thiết, và cách dùng phải rất hẹp: **kiểm tra vi phạm invariant đã được khai báo**, không phải "tìm bug". Xem §13.

---

## 3. Business Flow Manifest

Artifact quan trọng nhất của hệ thống. Chất lượng manifest là **ràng buộc chặn** — mọi tầng bên dưới không thể tốt hơn nó.

### 3.1 Ví dụ đầy đủ

```yaml
# .fra/flows/gift_redemption.yaml
schema_version: 1
id: gift_redemption
name: "Đổi mã quà tặng thành voucher"
owners: ["@team-commerce"]

# ── Mức độ nghiêm trọng ───────────────────────────────────────────
severity:
  class: critical            # critical | high | medium | low
  rationale: "Mất tiền thật, không tự phục hồi được"
  impact_types: [financial, data_integrity]
  # ↓ Cờ khuếch đại — bốn thứ này quan trọng hơn cả severity class
  idempotent: false
  reversible: false
  compensating_action: manual      # automatic | manual | none
  external_side_effects: true      # gọi API bên thứ 3, gửi email/SMS

# ── Profile vận hành ──────────────────────────────────────────────
operational:
  source: adapter:cloudfront_logs   # hoặc: manual, otel, datadog
  executions_per_day: 12400         # cache lại, refresh hàng tuần
  peak_hour_share: 0.31
  observability:
    has_slo: true
    alertable_metric: "redemption_success_rate"
    natural_detection_latency_min: 5

# ── Các bước và invariant ─────────────────────────────────────────
steps:
  - id: validate_code
    name: "Xác thực mã quà"
    entities:
      - "src/services/GiftCodeValidator.*#func:validate"
      - "src/repositories/GiftCodeRepo.*#func:findByCode"
    invariants:
      - id: inv_single_use
        statement: "Một mã chỉ redeem thành công đúng 1 lần, kể cả khi 2 request đồng thời"
        provenance: incident         # incident | human | llm_proposed
        incident_ref: "INC-2024-041"
        status: confirmed            # confirmed | proposed | deprecated
      - id: inv_expiry
        statement: "Mã quá expires_at không bao giờ pass validation"
        provenance: human
        status: confirmed

  - id: commit_redemption
    name: "Ghi nhận đổi mã và phát voucher"
    entities:
      - "src/services/RedemptionService.*#func:commit"
      - "src/jobs/IssueVoucherJob.*"
    invariants:
      - id: inv_atomicity
        statement: "mark_redeemed và issue_voucher phải nằm trong cùng 1 transaction, hoặc có outbox pattern"
        provenance: incident
        incident_ref: "INC-2024-041"
        status: confirmed
      - id: inv_amount_match
        statement: "voucher.amount == gift_code.face_value, không làm tròn"
        provenance: llm_proposed
        status: proposed              # ← chỉ advisory, không tính điểm

# ── Phụ thuộc ngoài ───────────────────────────────────────────────
dependencies:
  - kind: database
    ref: "orders, gift_codes, vouchers"
  - kind: external_api
    ref: "voucher-provider.example.com"
    timeout_ms: 3000
    has_circuit_breaker: false       # ← cờ đỏ

# ── Ánh xạ entry point ────────────────────────────────────────────
entry_points:
  - kind: http
    pattern: "POST /api/v1/redeem"
  - kind: queue
    pattern: "redemption.retry"

# ── Quản trị drift ────────────────────────────────────────────────
maintenance:
  last_verified: "2026-08-14"
  verified_by: "@danny"
  trace_source: "integration test suite, run 2026-08-14"
```

### 3.2 JSON Schema

Lưu tại `.fra/schema/flow.schema.json`, validate trong CI.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "FRA Business Flow Manifest",
  "type": "object",
  "required": ["schema_version", "id", "name", "severity", "steps"],
  "additionalProperties": false,
  "properties": {
    "schema_version": { "const": 1 },
    "id": { "type": "string", "pattern": "^[a-z][a-z0-9_]{2,63}$" },
    "name": { "type": "string", "minLength": 3 },
    "owners": { "type": "array", "items": { "type": "string" } },

    "severity": {
      "type": "object",
      "required": ["class", "idempotent", "reversible", "compensating_action"],
      "properties": {
        "class": { "enum": ["critical", "high", "medium", "low"] },
        "rationale": { "type": "string" },
        "impact_types": {
          "type": "array",
          "items": { "enum": ["financial", "data_integrity", "availability",
                              "privacy", "compliance", "reputation"] }
        },
        "idempotent": { "type": "boolean" },
        "reversible": { "type": "boolean" },
        "compensating_action": { "enum": ["automatic", "manual", "none"] },
        "external_side_effects": { "type": "boolean" }
      }
    },

    "operational": {
      "type": "object",
      "properties": {
        "source": { "type": "string" },
        "executions_per_day": { "type": "number", "minimum": 0 },
        "peak_hour_share": { "type": "number", "minimum": 0, "maximum": 1 },
        "observability": {
          "type": "object",
          "properties": {
            "has_slo": { "type": "boolean" },
            "alertable_metric": { "type": "string" },
            "natural_detection_latency_min": { "type": "number" }
          }
        }
      }
    },

    "steps": {
      "type": "array",
      "minItems": 1,
      "items": {
        "type": "object",
        "required": ["id", "entities"],
        "properties": {
          "id": { "type": "string", "pattern": "^[a-z][a-z0-9_]*$" },
          "name": { "type": "string" },
          "entities": {
            "type": "array",
            "minItems": 1,
            "items": { "type": "string" },
            "description": "Entity ref theo granularity ladder §6.1"
          },
          "invariants": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["id", "statement", "provenance", "status"],
              "properties": {
                "id": { "type": "string" },
                "statement": { "type": "string", "minLength": 10 },
                "provenance": { "enum": ["incident", "human", "llm_proposed"] },
                "incident_ref": { "type": "string" },
                "status": { "enum": ["confirmed", "proposed", "deprecated"] }
              }
            }
          }
        }
      }
    },

    "dependencies": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kind", "ref"],
        "properties": {
          "kind": { "enum": ["database", "cache", "queue", "external_api",
                             "filesystem", "cron", "cdn"] },
          "ref": { "type": "string" },
          "timeout_ms": { "type": "number" },
          "has_circuit_breaker": { "type": "boolean" }
        }
      }
    },

    "entry_points": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["kind", "pattern"],
        "properties": {
          "kind": { "enum": ["http", "queue", "cron", "cli", "event", "webhook"] },
          "pattern": { "type": "string" }
        }
      }
    },

    "maintenance": {
      "type": "object",
      "properties": {
        "last_verified": { "type": "string", "format": "date" },
        "verified_by": { "type": "string" },
        "trace_source": { "type": "string" }
      }
    }
  }
}
```

### 3.3 Quy tắc bất di bất dịch về invariant

**Chỉ invariant `status: confirmed` được tính vào điểm rủi ro.**

Invariant `llm_proposed` mà chưa ai xác nhận thì chỉ xuất hiện dưới dạng advisory trong PR comment. Đây là hàng rào chống garbage-in duy nhất mà hệ thống có — nếu bỏ, LLM sẽ dần dần tự sinh ra tiêu chuẩn của chính nó rồi tự đánh giá theo tiêu chuẩn đó.

Xếp hạng chất lượng invariant theo provenance:

1. **`incident`** — rút từ sự cố thật đã xảy ra. Đáng tin nhất. Mỗi postmortem nên sinh ra ít nhất một invariant.
2. **`human`** — do người viết, dựa trên hiểu biết nghiệp vụ.
3. **`llm_proposed`** — gợi ý máy. Không bao giờ tự động thành `confirmed`.

### 3.4 Phát hiện drift

Manifest mục ruỗng âm thầm là chế độ hỏng phổ biến nhất của loại hệ thống này. Chạy `fra doctor` trong CI trên **mọi** PR:

| Kiểm tra | Hành động khi fail |
|---|---|
| Entity ref không resolve được (file/symbol đã xóa hoặc đổi tên) | **Fail CI.** Đây là lỗi thật, không phải cảnh báo |
| Flow có `last_verified` > 180 ngày | Warning, gắn nhãn `stale` |
| Flow critical không có `alertable_metric` | Warning kèm giải thích: canary sẽ mù |
| Symbol mới trong path đã map mà không thuộc step nào | Warning: có thể thiếu coverage manifest |
| Invariant `proposed` > 90 ngày | Warning: cần người quyết định confirm hay bỏ |
| File có traffic (theo log) mà không thuộc flow nào | Warning: vùng tối của manifest |

Kiểm tra cuối cùng quan trọng nhất về lâu dài: nó đo **độ phủ manifest** trên code thực sự chạy, không phải trên toàn bộ repo. Code không ai gọi thì không cần manifest.

---

## 4. Bootstrap: từ code sang manifest

Đây là phần khó nhất. **Không** làm bằng một lần gọi LLM trên toàn repo — kết quả sẽ là một danh sách flow nghe hợp lý mà không ánh xạ đúng thực tế, và tệ hơn là không ai phát hiện được nó sai.

Quy trình 5 giai đoạn, mỗi giai đoạn có nguồn dữ liệu và cửa xác nhận riêng.

### Stage A — Phát hiện entry point

Có hai hướng, dùng cả hai và đối chiếu:

**A1. Từ traffic thật (ưu tiên)**

```bash
# Từ access log — bất kể web server nào
# Chuẩn hóa path (thay ID bằng placeholder) rồi đếm
awk '{print $6, $7}' access.log \
  | sed -E 's#/[0-9]+#/:id#g; s#/[0-9a-f]{8,}#/:uuid#g' \
  | sort | uniq -c | sort -rn | head -100
```

Endpoint có traffic **là** entry point. Đây là dữ liệu thực nghiệm, không phải suy đoán. Nó cũng lập tức cho bạn `executions_per_day`.

**A2. Từ code (bổ sung)**

Bắt các nguồn mà log không thấy: cron, queue consumer, event handler, CLI. Dùng tree-sitter query hoặc grep theo pattern framework:

```yaml
# .fra/config.yaml — pattern nhận diện entry point, mở rộng theo stack
entry_point_patterns:
  http:
    - regex: '@(Get|Post|Put|Patch|Delete)Mapping'      # Spring
    - regex: '@(app|router)\.(get|post|put|delete)'      # Flask/FastAPI/Express
    - regex: 'Route::(get|post|put|delete)'              # Laravel
    - regex: 'add_action\(.rest_api_init'                # WordPress
    - regex: 'func\s+\w+\(w\s+http\.ResponseWriter'      # Go
  cron:
    - path_glob: "**/cron/**"
    - regex: 'schedule\(\)|@Scheduled|cron\.'
  queue:
    - regex: '@(RabbitListener|KafkaListener|SqsListener)'
    - regex: 'class\s+\w+(Job|Consumer|Worker|Handler)\b'
  cli:
    - regex: 'argparse|commander|cobra|Console\\Command'
```

**Đối chiếu A1 và A2 là bước có giá trị nhất của toàn bộ Stage A:**

- Có trong code, không có traffic → dead code, hoặc thiếu instrumentation. Cả hai đều đáng biết.
- Có traffic, không tìm thấy trong code → route động, hoặc pattern nhận diện của bạn thiếu. Sửa pattern.

### Stage B — Thu thập execution trace

Cần trả lời: entry point này chạy qua những entity nào? Bốn nguồn, xếp theo chất lượng giảm dần:

| Nguồn | Cách lấy | Chất lượng | Ghi chú |
|---|---|---|---|
| Production distributed trace | OpenTelemetry span → code location | Tốt nhất | Cần instrumentation sẵn |
| Per-test coverage | Chạy từng integration test riêng, thu coverage | Rất tốt | Chậm nhưng chính xác |
| Coverage tổng | Chạy cả suite, thu 1 lần | Trung bình | Không phân biệt được test nào chạm gì |
| Static call graph | Từ import/call graph | Yếu | Overapproximate nặng vì DI, reflection |

**Cách per-test coverage (khuyên dùng khi chưa có OTel):**

```bash
# Nguyên tắc chung, thay tool theo ngôn ngữ
for test in $(fra list-integration-tests); do
  run_single_test_with_coverage "$test" --output "cov/$test.xml"
done
fra trace-index --coverage-dir cov/ --output .fra/cache/traces.json
```

Mọi framework coverage lớn đều xuất được một trong các format chuẩn: **lcov**, **cobertura**, **clover**, **coverage.py JSON**, **JaCoCo XML**, **Go coverprofile**. Viết một parser cho mỗi format là công việc hữu hạn, và có sẵn thư viện. Đây là cách chính để FRA giữ tính language-agnostic ở tầng reachability.

### Stage C — Gom nhóm thành flow

Bây giờ có: tập entry point + tập entity mỗi entry point chạm tới. Gom thành flow theo hai tiêu chí:

1. **Chồng lấn entity** — Jaccard similarity giữa các trace. Entry point dùng chung >40% entity thường thuộc cùng flow.
2. **Ý nghĩa nghiệp vụ** — LLM đặt tên và gom, người xác nhận.

```python
def cluster_flows(traces: dict[str, set[str]], threshold: float = 0.4):
    """traces: entry_point -> set of entity refs"""
    eps = list(traces)
    parent = {e: e for e in eps}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(eps):
        for b in eps[i + 1:]:
            inter = len(traces[a] & traces[b])
            union = len(traces[a] | traces[b])
            if union and inter / union >= threshold:
                parent[find(a)] = find(b)

    clusters = {}
    for e in eps:
        clusters.setdefault(find(e), []).append(e)
    return list(clusters.values())
```

Cụm ra rồi đưa cho LLM đặt tên nghiệp vụ (prompt tại §13.2). **Người phải xác nhận từng flow trước khi nó vào manifest.** Cụm sai ở đây kéo sai mọi thứ phía sau.

Kinh nghiệm hiệu chỉnh threshold: 0.4 là điểm khởi đầu. Quá thấp thì mọi thứ dính vào một siêu-flow khổng lồ (thường vì shared middleware/ORM base class — nên loại các entity xuất hiện trong >60% trace ra khỏi phép tính similarity). Quá cao thì mỗi endpoint thành một flow riêng, mất ý nghĩa.

### Stage D — Khai thác invariant

Xếp theo chất lượng, làm theo thứ tự này:

**D1. Từ incident history — nguồn tốt nhất, và thường bị bỏ qua**

```bash
# Commit hotfix/revert → đọc diff → invariant bị vi phạm là gì?
git log --grep="hotfix\|revert\|urgent\|rollback\|INC-" -i \
        --pretty=format:"%H|%ad|%s" --date=short --since="2 years ago"
```

Với mỗi hotfix, câu hỏi: *"Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?"* Câu trả lời chính là một invariant. LLM đọc diff của hotfix và đề xuất; người xác nhận và gán `provenance: incident`.

Postmortem có sẵn thì càng tốt — mục "root cause" gần như luôn là một invariant bị vi phạm, viết bằng ngôn ngữ khác.

**D2. Từ test assertion**

Test integration đang assert cái gì? Assertion là invariant đã được ai đó nghĩ tới. Trích assertion, tổng quát hóa, xác nhận.

**D3. Từ code (LLM đề xuất, `status: proposed`)**

Chỉ ở đây LLM đọc code và đoán. Kết quả **luôn** là `proposed`, chờ người confirm.

**D4. Từ danh sách kiểm tra chung**

Với flow có `idempotent: false` hoặc `external_side_effects: true`, tự động đề xuất bộ invariant tiêu chuẩn:

- Concurrent request tới cùng resource không tạo double-effect
- Retry sau timeout không tạo double-effect
- Partial failure giữa các step để lại state hợp lệ hoặc được cleanup
- Timeout của external call không làm treo transaction đang mở

### Stage E — Gán severity và trọng số

**Không dùng LLM cho bước này.** Severity là quyết định kinh doanh, không phải suy luận từ code.

Rubric cố định, người điền:

| Class | Tiêu chí (chọn class cao nhất khớp) |
|---|---|
| `critical` | Mất tiền, hỏng dữ liệu không phục hồi được, rò rỉ dữ liệu cá nhân, hoặc vi phạm quy định |
| `high` | Chặn nghiệp vụ chính, cần can thiệp tay để khôi phục |
| `medium` | Giảm chất lượng, có workaround, tự phục hồi |
| `low` | Chỉ ảnh hưởng nội bộ hoặc thẩm mỹ |

Bốn cờ khuếch đại (`idempotent`, `reversible`, `compensating_action`, `external_side_effects`) trong thực tế phân biệt rủi ro tốt hơn cả `class`. Một flow `medium` mà không idempotent, không reversible, có side effect ra ngoài thì nguy hiểm hơn một flow `high` có compensating action tự động.

### Ngân sách công sức

| Stage | Tự động được | Cần người | Thời gian cho ~30 flow |
|---|---|---|---|
| A — entry point | 90% | Sửa pattern | 1–2 ngày |
| B — trace | 95% | Setup coverage runner | 2–4 ngày |
| C — clustering | 70% | Xác nhận + đặt tên | 1 ngày |
| D — invariant | 40% | Xác nhận từng cái | **5–10 ngày** |
| E — severity | 0% | Toàn bộ | 1–2 ngày |

Stage D là chỗ tốn công nhất và không có đường tắt. Nhưng nó cũng là chỗ tạo ra giá trị lớn nhất — kể cả khi bạn dừng lại và không bao giờ triển khai phần tính xác suất, việc viết ra invariant của các flow critical đã tự nó đáng giá.

**Khuyến nghị:** đừng làm cả 30 flow. Bắt đầu với **3 flow critical**. Chạy hết vòng, thấy giá trị, rồi mở rộng.

---

## 5. Cấu hình

```yaml
# .fra/config.yaml
schema_version: 1

# ── Nhận diện symbol ──────────────────────────────────────────────
symbols:
  provider: tree-sitter        # tree-sitter | ctags | lsp | none
  languages: [python, javascript, typescript, php, go, java, ruby, rust, csharp]
  # 'none' → chỉ hoạt động ở mức file (L1). Vẫn dùng được, kém chi tiết hơn

# ── Nguồn dữ liệu, tất cả đều tùy chọn ────────────────────────────
adapters:
  operational_profile:
    kind: file               # file | http | prometheus | datadog | otel | manual
    path: .fra/cache/traffic.json
    refresh_days: 7
  coverage:
    kind: lcov               # lcov | cobertura | clover | jacoco | gocover | coveragepy
    path: coverage/lcov.info
  mutation:
    kind: none               # none | stryker | pitest | mutmut | infection
  incidents:
    kind: none               # none | sentry | pagerduty | jira | file

# ── Tham số mô hình ───────────────────────────────────────────────
model:
  beta_common_cause: 0.20    # §2.2 — hiệu chỉnh từ dữ liệu của bạn
  detection:
    review_effectiveness: 0.15   # thấp một cách thực tế; đo, đừng đoán
    canary_min_executions: 30    # dưới mức này canary không cho tín hiệu
    canary_traffic_share: 0.05
    canary_duration_min: 30
  impact_closure:
    max_depth: 3             # >3 thì closure phình ra vô dụng
    tiers: [cochange, imports, coverage]

# ── Ngưỡng quyết định ─────────────────────────────────────────────
gates:
  hard_block:
    - armed_cut_set                  # §11 — chọc ≥2 event cùng cut set
    - security_class_finding         # ruin risk
    - manifest_entity_unresolved     # drift
  require_flag:
    - critical_flow_touched_no_canary_signal
    - invariant_violation_confirmed
  require_canary:
    - risk_band: [high, very_high]

# ── LLM ───────────────────────────────────────────────────────────
llm:
  enabled: true
  max_context_files: 12
  # Bất biến: LLM không bao giờ được yêu cầu sinh ra số
  forbid_numeric_output: true
```

---

## 6. Entity resolution — nền tảng language-agnostic

### 6.1 Thang độ chi tiết

Entity ref là chuỗi định danh, suy giảm mượt theo năng lực công cụ:

```
L0  path glob      src/payments/**
L1  file           src/payments/charge.py
L2  symbol         src/payments/charge.py#func:PaymentService.charge
L3  line range     src/payments/charge.py#L120-L145
```

**Quy tắc dùng:**

- Manifest **nên** dùng L1 hoặc L2. L2 chính xác hơn nhưng rot nhanh hơn khi refactor.
- Manifest **không nên** dùng L3 — vỡ sau mỗi lần reformat, tạo drift giả.
- L0 dùng cho vùng lớn không có ranh giới rõ (`config/**`, `migrations/**`).
- Wildcard extension `src/services/GiftCodeValidator.*` giúp manifest sống qua việc đổi ngôn ngữ hoặc port module.

### 6.2 Trích symbol bằng tree-sitter

tree-sitter là chọn lựa đúng vì một API cho 40+ ngôn ngữ, parse được cả file có syntax error (quan trọng khi phân tích diff), và không cần chạy code hay resolve dependency.

```python
# fra/symbols.py
from dataclasses import dataclass
from tree_sitter import Language, Parser
import tree_sitter_languages as tsl

@dataclass(frozen=True)
class Symbol:
    kind: str          # func | method | class | module
    name: str          # qualified: "PaymentService.charge"
    start_line: int    # 1-based, inclusive
    end_line: int

# Query per language. Node type names khác nhau giữa các grammar,
# nhưng capture name thì thống nhất -> phần còn lại của code dùng chung.
QUERIES = {
    "python": """
        (function_definition name: (identifier) @fn)
        (class_definition   name: (identifier) @cls)
    """,
    "javascript": """
        (function_declaration name: (identifier) @fn)
        (method_definition    name: (property_identifier) @fn)
        (class_declaration    name: (identifier) @cls)
        (variable_declarator name: (identifier) @fn
          value: [(arrow_function) (function_expression)])
    """,
    "typescript": """
        (function_declaration name: (identifier) @fn)
        (method_signature     name: (property_identifier) @fn)
        (method_definition    name: (property_identifier) @fn)
        (class_declaration    name: (type_identifier) @cls)
    """,
    "php": """
        (function_definition name: (name) @fn)
        (method_declaration  name: (name) @fn)
        (class_declaration   name: (name) @cls)
    """,
    "go": """
        (function_declaration name: (identifier) @fn)
        (method_declaration   name: (field_identifier) @fn)
        (type_declaration (type_spec name: (type_identifier) @cls))
    """,
    "java": """
        (method_declaration name: (identifier) @fn)
        (class_declaration  name: (identifier) @cls)
    """,
    "ruby": """
        (method name: (identifier) @fn)
        (class  name: (constant)   @cls)
    """,
    "rust": """
        (function_item name: (identifier) @fn)
        (struct_item   name: (type_identifier) @cls)
        (impl_item     type: (type_identifier) @cls)
    """,
    "c_sharp": """
        (method_declaration name: (identifier) @fn)
        (class_declaration  name: (identifier) @cls)
    """,
}

def extract_symbols(path: str, lang: str, source: bytes) -> list[Symbol]:
    parser = Parser()
    parser.set_language(tsl.get_language(lang))
    tree = parser.parse(source)

    q = tsl.get_language(lang).query(QUERIES[lang])
    classes, funcs = [], []

    for node, cap in q.captures(tree.root_node):
        parent = node.parent
        rec = Symbol(
            kind="class" if cap == "cls" else "func",
            name=source[node.start_byte:node.end_byte].decode("utf8", "replace"),
            start_line=parent.start_point[0] + 1,
            end_line=parent.end_point[0] + 1,
        )
        (classes if cap == "cls" else funcs).append(rec)

    # Qualify method names bằng class bao ngoài -> ổn định hơn khi có
    # nhiều method cùng tên trong 1 file
    out = list(classes)
    for f in funcs:
        owner = next(
            (c for c in classes
             if c.start_line <= f.start_line and f.end_line <= c.end_line),
            None,
        )
        out.append(Symbol(
            kind="method" if owner else "func",
            name=f"{owner.name}.{f.name}" if owner else f.name,
            start_line=f.start_line,
            end_line=f.end_line,
        ))
    return out
```

**Fallback khi không có tree-sitter:** `universal-ctags --output-format=json` cho ra `{name, kind, line}` cho ~150 ngôn ngữ. Không có `end_line` nên phải suy ra bằng "tới symbol kế tiếp", kém chính xác nhưng đủ dùng.

**Fallback cuối:** không có gì → hoạt động ở L1 (file-level). Toàn bộ pipeline vẫn chạy, chỉ mất độ phân giải. Đừng chặn triển khai vì thiếu tree-sitter cho một ngôn ngữ lẻ trong repo.

---

## 7. Phân tích diff

### 7.1 Từ hunk sang change unit

```bash
# --unified=0 cho ra đúng dải dòng thay đổi, không lẫn context
git diff --unified=0 --no-color "$BASE_SHA...$HEAD_SHA"
# Đổi tên: quan trọng, tránh coi rename thành xóa+thêm
git diff --name-status -M -C "$BASE_SHA...$HEAD_SHA"
```

```python
# fra/diff.py
import re, subprocess
from dataclasses import dataclass, field

HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')

@dataclass
class Hunk:
    path: str
    new_start: int
    new_count: int
    old_start: int
    old_count: int

@dataclass
class ChangeUnit:
    """Đơn vị phân tích. Symbol nếu resolve được, ngược lại là module."""
    path: str
    symbol: str | None
    kind: str                      # xem §7.2
    lines_added: int = 0
    lines_removed: int = 0
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.path}#func:{self.symbol}" if self.symbol else self.path


def parse_diff(base: str, head: str) -> list[Hunk]:
    out = subprocess.run(
        ["git", "diff", "--unified=0", "--no-color", f"{base}...{head}"],
        capture_output=True, text=True, check=True,
    ).stdout

    hunks, path = [], None
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
        elif line.startswith("@@") and path:
            m = HUNK.match(line)
            if m:
                hunks.append(Hunk(
                    path=path,
                    old_start=int(m.group(1)), old_count=int(m.group(2) or 1),
                    new_start=int(m.group(3)), new_count=int(m.group(4) or 1),
                ))
    return hunks


def to_change_units(hunks: list[Hunk], symbol_index) -> list[ChangeUnit]:
    """Gộp hunk vào symbol bao ngoài trong cùng (innermost enclosing)."""
    units: dict[tuple[str, str | None], ChangeUnit] = {}

    for h in hunks:
        syms = symbol_index.get(h.path, [])
        # innermost = phạm vi hẹp nhất chứa dòng thay đổi
        enclosing = [
            s for s in syms
            if s.start_line <= h.new_start <= s.end_line
        ]
        sym = min(enclosing, key=lambda s: s.end_line - s.start_line).name \
              if enclosing else None
        # sym is None -> thay đổi ở top-level: import, decorator, constant.
        # Đây KHÔNG phải trường hợp bỏ qua được (xem §7.3)

        key = (h.path, sym)
        u = units.setdefault(key, ChangeUnit(path=h.path, symbol=sym, kind="unknown"))
        u.hunks.append(h)
        u.lines_added += h.new_count
        u.lines_removed += h.old_count

    return list(units.values())
```

### 7.2 Phân loại change_kind

`change_kind` ảnh hưởng mạnh tới `P(I)` và `P(P)`. Phân loại bằng so sánh cấu trúc AST trước/sau, không bằng đếm dòng.

| kind | Nhận diện | Ảnh hưởng | Ghi chú |
|---|---|---|---|
| `pure_addition` | Symbol mới, không caller cũ | `P(P)` thấp | Rủi ro thấp cho flow hiện có |
| `signature_change` | Đổi tham số/return type | `P(P)` **cao** | Lan ra mọi call site |
| `control_flow` | Thêm/sửa/xóa nhánh, vòng lặp, guard | `P(I)` **cao** | Chế độ hỏng phổ biến nhất |
| `data_transform` | Đổi phép tính, format, parse, cast | `P(I)` **cao** | Lỗi âm thầm, khó phát hiện |
| `error_handling` | Sửa try/catch, retry, timeout | `P(I)` cao | Hay biến lỗi ồn thành lỗi im |
| `config_constant` | Đổi hằng số, config, feature flag default | `P(R)` **cao**, LOC thấp | **Nguy hiểm bị đánh giá thấp** |
| `deletion` | Xóa symbol | Kiểm tra caller mồ côi | Grep call site |
| `rename_move` | Chỉ đổi tên/di chuyển | Rủi ro thấp | Nhưng gây manifest drift |
| `dependency_bump` | Đổi lockfile/manifest package | Mô hình riêng | Không dùng chuỗi PIE |
| `schema_migration` | DDL, migration file | Mô hình riêng | Cần phân tích forward/backward compat |
| `test_only` | Chỉ file test | Loại khỏi tính điểm | Nhưng tính vào `P(¬detect)` |

Hai dòng cuối bảng đáng lưu tâm: `dependency_bump` và `schema_migration` **không nên** chạy qua chuỗi PIE. Chúng có chế độ hỏng khác hẳn (transitive CVE, lock contention lúc migrate, backward incompatibility trong rolling deploy) và cần checklist riêng. Cố nhét vào một mô hình sẽ cho ra số vô nghĩa.

```python
KIND_MODIFIERS = {
    # (P(I) multiplier, P(P) multiplier)
    "pure_addition":    (0.6, 0.3),
    "signature_change": (0.8, 1.6),
    "control_flow":     (1.5, 1.1),
    "data_transform":   (1.5, 1.3),
    "error_handling":   (1.3, 1.2),
    "config_constant":  (1.2, 1.5),
    "deletion":         (1.0, 1.4),
    "rename_move":      (0.3, 0.4),
    "test_only":        (0.0, 0.0),
    "unknown":          (1.0, 1.0),
}
```

Các số này là **giả thuyết khởi đầu**, không phải hằng số vật lý. Chúng phải được fit lại từ dữ liệu của bạn ở §15. Nếu bạn không bao giờ hiệu chỉnh chúng, đừng xuất ra phần trăm — dùng dải thứ tự.

### 7.3 Hunk mồ côi

Hunk không nằm trong symbol nào (import, decorator, hằng số top-level, khai báo route) hay bị bỏ qua khi tự dựng loại tool này. Đó là sai lầm — chúng thường có **LOC thấp nhất và blast radius lớn nhất**.

Xử lý: coi module là change unit, và gán `kind` theo nội dung:

- Đổi import → có thể đổi hành vi toàn module. `P(P)` cao.
- Đổi hằng số top-level → `config_constant`.
- Đổi khai báo route/binding DI → `signature_change` ở mức module.
- Thêm decorator (`@transactional`, `@cached`, middleware) → `control_flow` ở mức module. Đây là loại thay đổi một dòng gây sự cố nhiều nhất trong thực tế.

---

## 8. Impact closure — bốn tầng

Từ change unit, cần tìm tập entity bị ảnh hưởng, để đối chiếu với manifest. Đây là phần khó nhất để làm language-agnostic. Giải pháp là phân tầng và **hợp** kết quả.

### Tier 0 — Co-change coupling (git, mọi ngôn ngữ, không cần setup)

Tầng bị đánh giá thấp nhất và hữu dụng nhất. File nào **thường xuyên bị sửa cùng nhau** trong lịch sử?

```python
def cochange_coupling(window_days=365, min_support=4):
    """Trả về {file: {file: confidence}} — coupling thực nghiệm."""
    import subprocess
    from collections import defaultdict, Counter

    out = subprocess.run(
        ["git", "log", f"--since={window_days} days ago",
         "--name-only", "--pretty=format:%%COMMIT%%"],
        capture_output=True, text=True, check=True,
    ).stdout

    commits, cur = [], []
    for line in out.splitlines():
        if line == "%COMMIT%":
            if cur: commits.append(set(cur))
            cur = []
        elif line.strip():
            cur.append(line.strip())
    if cur: commits.append(set(cur))

    # Bỏ commit khổng lồ: refactor/format toàn repo tạo coupling giả
    commits = [c for c in commits if 1 < len(c) <= 25]

    solo = Counter()
    pair = defaultdict(Counter)
    for files in commits:
        for f in files:
            solo[f] += 1
        for a in files:
            for b in files:
                if a != b:
                    pair[a][b] += 1

    coupling = {}
    for a, partners in pair.items():
        if solo[a] < min_support:
            continue
        coupling[a] = {
            b: n / solo[a] for b, n in partners.items()
            if n >= min_support and n / solo[a] >= 0.3
        }
    return coupling
```

Tại sao tầng này quan trọng: nó bắt được các phụ thuộc mà **không** static analysis nào thấy — template ↔ controller, migration ↔ model, config ↔ code đọc config, file dịch ↔ UI, IaC ↔ ứng dụng. Đây thường chính là chỗ sự cố xảy ra: người sửa một bên mà quên bên kia.

Cảnh báo: loại commit >25 file. Một lần format toàn repo sẽ khiến mọi file coupling với mọi file.

### Tier 1 — Import/dependency graph

Tree-sitter query trích import statement, dựng đồ thị, đi ngược để tìm ai phụ thuộc vào file đã đổi. Language-agnostic vì query là per-language nhưng graph là chung.

Giới hạn: **overapproximate nặng**. Import không có nghĩa là gọi. Với DI container, reflection, dynamic dispatch thì gần như vô dụng. Dùng làm sàn dưới, không làm nguồn chính.

### Tier 2 — Coverage-based reachability (khuyên dùng)

Từ Stage B đã có: `entry_point → set(entity)`. Đảo ngược thành `entity → set(entry_point)`. Đây là **reachability thực nghiệm**, chính xác hơn mọi phân tích tĩnh:

```python
def flows_reached(change_units, trace_index, flow_manifest):
    """trace_index: {entity_ref: set(entry_point)}"""
    reached = {}
    for u in change_units:
        # Khớp cả L2 (symbol) và L1 (file) — suy giảm mượt
        eps = trace_index.get(u.ref) or trace_index.get(u.path) or set()
        for f in flow_manifest:
            if any(ep in f.entry_points for ep in eps):
                reached.setdefault(f.id, []).append(u)
    return reached
```

Giới hạn: chỉ thấy được đường mà test/trace đã đi qua. Nhánh không có test là **điểm mù** — và đó là nhánh nguy hiểm nhất. Nên luôn kết hợp Tier 0.

### Tier 3 — Static call graph

Chính xác nhất khi có, nhưng cần tool riêng cho từng ngôn ngữ (`jdeps`, `go/callgraph`, `pyan`, `madge`, `phpstan`). Coi là **tùy chọn nâng cao**, không phải yêu cầu.

### Hợp nhất

```
closure(u) = { u }
           ∪ cochange(u, conf ≥ 0.3)      # Tier 0
           ∪ importers(u, depth ≤ 2)      # Tier 1
           ∪ trace_reverse(u)             # Tier 2
           ∪ callers(u, depth ≤ 3)        # Tier 3, nếu có
```

Mỗi phần tử mang theo **confidence** riêng theo tier nguồn. Đừng đối xử một entity tìm thấy qua co-change confidence 0.31 giống một entity tìm thấy qua coverage trace trực tiếp.

```python
TIER_CONFIDENCE = {
    "self": 1.00,
    "coverage": 0.90,   # thực nghiệm
    "callgraph": 0.75,  # tĩnh, chính xác nhưng có thể lệch runtime
    "cochange": 0.55,   # tương quan, không nhân quả
    "imports": 0.35,    # overapproximate
}
```

**Ràng buộc `max_depth: 3`** là bắt buộc chứ không phải tinh chỉnh. Ở depth 4+ closure phình ra chạm gần hết repo và output trở nên vô nghĩa — mọi PR sẽ "chạm mọi flow".

---

## 9. Operational profile — thừa số quan trọng nhất

`P(R)` là thừa số bạn biết chính xác nhất và cũng thường là thừa số chi phối. Triển khai nó **trước** mọi thứ khác.

### 9.1 Interface adapter

```python
# fra/adapters/base.py
from abc import ABC, abstractmethod

class OperationalProfileAdapter(ABC):
    @abstractmethod
    def executions_per_day(self, entry_point: str) -> float | None:
        """None = không biết. KHÔNG được trả 0 khi không biết."""

    @abstractmethod
    def peak_hour_share(self, entry_point: str) -> float | None:
        ...
```

Phân biệt `None` với `0` là bắt buộc. `0` nghĩa là "đo được và không có traffic" → rủi ro thật sự thấp. `None` nghĩa là "không đo được" → phải rơi về giá trị bảo thủ, không phải giá trị thấp. Nhầm hai thứ này khiến mọi flow chưa có instrumentation tự động được đánh giá an toàn — chính xác là ngược lại thực tế.

### 9.2 Adapter từ access log (mặc định, hoạt động mọi nơi)

```python
import re, json
from collections import Counter
from datetime import datetime

# Chuẩn hóa path: gộp mọi ID về placeholder để nhóm được endpoint
NORMALIZERS = [
    (re.compile(r'/\d+'), '/:id'),
    (re.compile(r'/[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}'), '/:uuid'),
    (re.compile(r'/[0-9a-fA-F]{24,}'), '/:hash'),
    (re.compile(r'\?.*$'), ''),
]

def normalize(path: str) -> str:
    for pat, rep in NORMALIZERS:
        path = pat.sub(rep, path)
    return path

def build_profile(log_path: str, line_regex: str, days: int) -> dict:
    """line_regex phải có named group: method, path"""
    pat = re.compile(line_regex)
    counts = Counter()
    with open(log_path, errors="replace") as fh:
        for line in fh:
            m = pat.search(line)
            if m:
                counts[f"{m.group('method')} {normalize(m.group('path'))}"] += 1

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "window_days": days,
        "endpoints": {ep: n / days for ep, n in counts.items()},
    }
```

Regex cho các format phổ biến:

```yaml
log_formats:
  nginx_combined: '"(?P<method>[A-Z]+) (?P<path>[^ ?"]+)'
  apache_common:  '"(?P<method>[A-Z]+) (?P<path>[^ ?"]+)'
  cloudfront_tsv: '\t(?P<method>[A-Z]+)\t[^\t]*\t(?P<path>[^\t?]+)'
  json_lines:     '"method"\s*:\s*"(?P<method>[A-Z]+)".*?"path"\s*:\s*"(?P<path>[^"?]+)"'
  caddy_json:     '"method"\s*:\s*"(?P<method>[A-Z]+)".*?"uri"\s*:\s*"(?P<path>[^"?]+)"'
```

### 9.3 Entry point không phải HTTP

Cron, queue consumer, event handler không xuất hiện trong access log. Nguồn thay thế:

| Loại | Nguồn tần suất |
|---|---|
| Cron | Đọc trực tiếp cron expression → tính số lần/ngày. Chính xác tuyệt đối |
| Queue consumer | Metric queue depth / message processed từ broker |
| Event handler | Đếm event từ event store hoặc log |
| CLI | Thường rất thấp. Điền tay là hợp lý |

Cron là trường hợp dễ nhất và hay bị bỏ sót: `0 */6 * * *` = 4 lần/ngày, biết chắc chắn, không cần đo.

### 9.4 Chuẩn hóa thành P(R)

`P(R)` cần là **xác suất trên một đơn vị thời gian**, không phải điểm số chuẩn hóa 0–1. Điều này khiến output có đơn vị thật và so sánh được:

```python
def p_reach(executions_per_day: float | None, horizon_days: float = 7.0) -> float:
    """Xác suất entity được thực thi ít nhất 1 lần trong horizon."""
    if executions_per_day is None:
        return 0.5          # unknown → bảo thủ, KHÔNG phải 0
    if executions_per_day <= 0:
        return 0.001        # đo được và không có traffic
    lam = executions_per_day * horizon_days
    return 1.0 - pow(2.718281828, -lam)   # Poisson: P(≥1 event)
```

Với flow chạy 12,400 lần/ngày thì `P(R) ≈ 1.0` trong horizon 7 ngày — nói cách khác, **bất kỳ** defect nào ở đó chắc chắn sẽ được thực thi. Với flow chạy 2 lần/tháng thì `P(R) ≈ 0.37`. Chênh lệch này là thật và không mô hình chấm điểm file nào bắt được.

Điểm quan trọng phái sinh: khi `P(R) ≈ 1`, con số đó **hết vai trò phân biệt**. Lúc này thứ quyết định là `P(¬detect)` và severity — nên với flow traffic cao, hãy dồn nỗ lực vào coverage và khả năng phát hiện, không vào việc cố tính chính xác hơn.

---

## 10. Escape probability — nơi canary bị mù

```
P(¬detect) = (1 − d_test) · (1 − d_review) · (1 − d_canary)
```

### 10.1 d_test — diff coverage, không phải total coverage

Total coverage là con số vô dụng cho mục đích này. Cái quan trọng là **những dòng vừa đổi có được test không**.

```python
def diff_coverage(change_units, coverage_map) -> float:
    """coverage_map: {path: {line: hit_count}}"""
    covered = total = 0
    for u in change_units:
        lines = coverage_map.get(u.path, {})
        for h in u.hunks:
            for ln in range(h.new_start, h.new_start + max(h.new_count, 1)):
                if ln in lines:            # dòng này có executable statement
                    total += 1
                    if lines[ln] > 0:
                        covered += 1
    return covered / total if total else 0.0
```

Điều chỉnh bằng mutation score nếu có. "Covered" chỉ nghĩa là dòng được thực thi, không nghĩa là hành vi được assert:

```python
def d_test(dc: float, mutation_score: float | None) -> float:
    effectiveness = mutation_score if mutation_score is not None else 0.5
    return dc * effectiveness
```

Hệ số 0.5 mặc định phản ánh một thực tế đã được đo nhiều lần: test suite điển hình bắt được khoảng một nửa số lỗi mà chúng "phủ". Nếu bạn có mutation testing, dùng số thật.

### 10.2 d_review — đo, đừng đoán

Hầu hết team đánh giá quá cao khả năng review bắt lỗi. Cách đo từ dữ liệu của bạn:

```
d_review = 1 − (số incident mà PR gây ra đã có ≥1 approval) / (tổng incident có PR truy được)
```

Nếu 90% incident đến từ PR đã được approve, thì `d_review = 0.10`. Con số đó không phải để chê team — nó là bản chất của review: người review giỏi bắt lỗi cấu trúc và lỗi rõ ràng, kém bắt lỗi nghiệp vụ ẩn, đúng loại lỗi mà FRA nhắm tới.

Mặc định 0.15 nếu chưa đo.

### 10.3 d_canary — sự mù toán học của canary

Đây là kết quả có giá trị nhất của toàn phần §10, và nó thường gây ngạc nhiên.

Canary chỉ phát hiện được lỗi nếu **có đủ số lần thực thi trong cửa sổ canary** để lỗi biểu hiện thành tín hiệu thống kê:

```python
import math

def d_canary(executions_per_day: float | None, cfg) -> tuple[float, str]:
    if executions_per_day is None:
        return 0.0, "unknown_traffic"

    window_days = cfg.canary_duration_min / (60 * 24)
    expected = executions_per_day * window_days * cfg.canary_traffic_share

    if expected < cfg.canary_min_executions:
        return 0.0, (
            f"CANARY BLIND: chỉ ~{expected:.1f} lần thực thi trong cửa sổ "
            f"{cfg.canary_duration_min} phút @ {cfg.canary_traffic_share:.0%} traffic. "
            f"Cần ≥{cfg.canary_min_executions}. Canary sẽ PASS rồi lỗi ở full rollout."
        )

    # Đủ traffic: hiệu quả tăng theo log số lần thực thi, chặn trên 0.8
    eff = min(0.8, 0.3 + 0.15 * math.log10(expected / cfg.canary_min_executions + 1))
    return eff, f"canary_ok(~{expected:.0f} executions)"
```

**Vì sao điều này quan trọng:** một flow chạy 20 lần/ngày, canary 30 phút ở 5% traffic → khoảng **0.02 lần thực thi**. Canary sẽ xanh 100% và nói với bạn rằng thay đổi an toàn. Nó không nói vậy — nó chỉ chưa thấy gì.

Đây là chế độ hỏng thực tế rất phổ biến và không ai để ý, vì canary xanh tạo cảm giác đã kiểm chứng. FRA nên **luôn** in ra dòng cảnh báo này khi nó áp dụng, kể cả với PR rủi ro thấp.

Khi canary mù, chiến lược thay thế:

- **Shadow traffic** — replay request thật vào code mới, so sánh output, không commit side effect
- **Synthetic probe** — bơm giao dịch test qua flow, đủ số lần để có tín hiệu
- **Feature flag + rollout theo cohort** — chấp nhận không có tín hiệu tự động, bù bằng khả năng tắt tức thì
- **Kéo dài cửa sổ canary** — tính ngược từ `canary_min_executions / (rate × share)` để ra thời lượng cần thiết. Với 20 lần/ngày và share 5%, cần **~30 ngày**. Con số này tự nó là một luận điểm: đừng dùng canary cho flow tần suất thấp.

---

## 11. Fault tree và minimal cut set

Chỉ dựng cho flow `severity.class: critical`. Với flow khác, chi phí bảo trì vượt giá trị.

### 11.1 Format

```yaml
# .fra/trees/gift_redemption.yaml
schema_version: 1
flow: gift_redemption
top_event: "Khách bị trừ tiền nhưng không nhận được voucher"

root:
  gate: OR
  children:
    - gate: AND
      id: cs_validation_and_reconciliation
      label: "Validation cho qua dữ liệu sai VÀ reconciliation không bắt được"
      children:
        - basic: validation_accepts_invalid
          entities:
            - "src/services/GiftCodeValidator.*#func:validate"
          prior: 0.02
        - basic: reconciliation_misses_orphan
          entities:
            - "src/jobs/ReconcileRedemptionsJob.*"
          prior: 0.05

    - gate: AND
      id: cs_transaction_and_retry
      label: "Transaction vỡ giữa 2 bước VÀ retry không idempotent"
      children:
        - basic: partial_commit
          entities:
            - "src/services/RedemptionService.*#func:commit"
          prior: 0.01
        - basic: retry_not_idempotent
          entities:
            - "src/jobs/IssueVoucherJob.*"
          prior: 0.08

    # OR đơn: một mình đủ gây top event -> cut set kích thước 1
    - basic: provider_timeout_unhandled
      entities:
        - "src/clients/VoucherProviderClient.*"
      prior: 0.03
      note: "Không có circuit breaker — xem dependencies trong manifest"
```

### 11.2 Tính minimal cut set

Với cây dưới ~50 node, khai triển boolean trực tiếp là đủ nhanh và dễ kiểm chứng:

```python
def cut_sets(node) -> list[set[str]]:
    """Khai triển sum-of-products. OR = hợp danh sách, AND = tích Descartes."""
    if "basic" in node:
        return [{node["basic"]}]

    children = [cut_sets(c) for c in node["children"]]

    if node["gate"] == "OR":
        result = [cs for group in children for cs in group]
    elif node["gate"] == "AND":
        result = [set()]
        for group in children:
            result = [a | b for a in result for b in group]
    else:
        raise ValueError(f"unknown gate: {node['gate']}")

    return minimalize(result)


def minimalize(sets: list[set[str]]) -> list[set[str]]:
    """Bỏ mọi tập là superset của tập khác — định nghĩa của 'minimal'."""
    out = []
    for s in sorted(sets, key=len):
        if not any(m <= s for m in out):
            out.append(s)
    return out
```

### 11.3 Kiểm tra cut set bị chọc

Đây là output có giá trị nhất của toàn hệ thống, và nó **không phụ thuộc hiệu chỉnh**:

```python
def armed_cut_sets(tree, change_units, resolver):
    """Cut set nào có ≥2 basic event bị PR này chạm tới?"""
    touched = {u.ref for u in change_units} | {u.path for u in change_units}
    basics = collect_basic_events(tree)      # {id: [entity refs]}

    hit = {
        bid for bid, ents in basics.items()
        if any(resolver.matches(e, touched) for e in ents)
    }

    findings = []
    for cs in cut_sets(tree["root"]):
        overlap = cs & hit
        if len(overlap) >= 2:
            findings.append({
                "severity": "hard_block",
                "cut_set": sorted(cs),
                "armed": sorted(overlap),
                "message": (
                    f"PR này chạm {len(overlap)}/{len(cs)} basic event trong cùng "
                    f"một minimal cut set. Các lớp phòng thủ vốn độc lập đang bị "
                    f"sửa CÙNG LÚC. Cần review riêng biệt cho từng lớp, "
                    f"hoặc tách thành các PR deploy tách biệt."
                ),
            })
        elif len(overlap) == 1 and len(cs) == 1:
            findings.append({
                "severity": "high",
                "cut_set": sorted(cs),
                "armed": sorted(overlap),
                "message": "Single-point-of-failure cut set bị chạm.",
            })
    return findings
```

Khuyến nghị hành động khi phát hiện armed cut set: **tách PR để deploy riêng từng lớp**, có khoảng nghỉ giữa hai lần deploy. Điều này giữ lại tính độc lập của các lớp phòng thủ theo thời gian, dù code cuối cùng vẫn như nhau.

### 11.4 Chuyển sang Bayesian Network

Khi cần suy diễn có phụ thuộc hoặc cập nhật bằng evidence:

| FTA | BN tương đương |
|---|---|
| Basic event | Root node, prior = `prior` |
| OR gate | Node con, CPT = `1 − Π(1 − p_i)` |
| AND gate | Node con, CPT = `Π p_i` |
| Common cause của PR | **Node ẩn mới** làm parent của các basic event bị PR chạm |
| Sentry báo lỗi | Evidence trên node quan sát → suy diễn ngược ra nguyên nhân |

Node common-cause là lý do chính để chuyển sang BN: nó biểu diễn tường minh cái mà FTA không làm được — rằng các basic event bị cùng một PR sửa thì tương quan với nhau.

Dùng `pgmpy` (Python) hoặc bất kỳ engine BN nào. Với cây nhỏ, inference chính xác bằng variable elimination là đủ.

---

## 12. Scoring và tổng hợp

```python
# fra/scoring.py
from dataclasses import dataclass

SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.6, "medium": 0.3, "low": 0.1}

def severity_multiplier(sev) -> float:
    """Bốn cờ khuếch đại phân biệt rủi ro tốt hơn cả severity class."""
    m = SEVERITY_WEIGHT[sev.cls]
    if not sev.idempotent:            m *= 1.4
    if not sev.reversible:            m *= 1.5
    if sev.compensating_action == "manual": m *= 1.3
    if sev.compensating_action == "none":   m *= 1.6
    if sev.external_side_effects:     m *= 1.3
    return min(m, 3.0)                # chặn trên, tránh nhân dồn vô hạn


def unit_probability(u, flow, ctx) -> float:
    """Chuỗi PIE cho một cặp (change_unit, flow)."""
    i_mod, p_mod = KIND_MODIFIERS[u.kind]

    p_d = ctx.jit_base_rate(u)                     # §15
    p_r = p_reach(flow.operational.executions_per_day, ctx.horizon_days)
    p_i = min(1.0, ctx.base_infection * i_mod)
    p_p = min(1.0, ctx.propagation_confidence(u, flow) * p_mod)

    d_t = d_test(ctx.diff_coverage(u), ctx.mutation_score(u))
    d_r = ctx.cfg.review_effectiveness
    d_c, _ = d_canary(flow.operational.executions_per_day, ctx.cfg)
    p_escape = (1 - d_t) * (1 - d_r) * (1 - d_c)

    return p_d * p_r * p_i * p_p * p_escape


def flow_probability(units, flow, ctx) -> dict:
    """Tổng hợp với beta-factor common cause (§2.2)."""
    ps = [unit_probability(u, flow, ctx) for u in units]
    ps = [p for p in ps if p > 0]
    if not ps:
        return {"p": 0.0, "band": "none"}

    beta = ctx.cfg.beta_common_cause

    p_ind = 1.0
    for p in ps:
        p_ind *= (1 - (1 - beta) * p)
    p_ind = 1 - p_ind

    p_cc = beta * max(ps)
    p_raw = p_ind + p_cc - p_ind * p_cc

    return {
        "p": p_raw,
        "weighted": p_raw * severity_multiplier(flow.severity),
        "units": len(ps),
        "dominant_unit": max(range(len(ps)), key=lambda i: ps[i]),
    }
```

### 12.1 Xuất dải, không xuất số, cho tới khi đã hiệu chỉnh

Đây là quy tắc cứng. §15 định nghĩa khi nào được xuất số.

```python
BANDS = [
    (0.001, "very_low"),
    (0.010, "low"),
    (0.050, "medium"),
    (0.150, "high"),
    (1.000, "very_high"),
]

def to_band(weighted: float) -> str:
    for threshold, name in BANDS:
        if weighted <= threshold:
            return name
    return "very_high"
```

Ngưỡng dải cũng phải hiệu chỉnh: đặt chúng ở **phân vị** của phân bố điểm trên 3 tháng PR gần nhất, chứ không dùng số tuyệt đối. Nếu 80% PR bị gắn `high` thì dải đã mất hết chức năng phân biệt.

---

## 13. Tích hợp LLM

### 13.1 Hàng rào bắt buộc

| Quy tắc | Lý do |
|---|---|
| **Không bao giờ hỏi LLM một con số** | Output LLM không có hiệu chỉnh. Hỏi "bao nhiêu %" luôn nhận về số nghe hợp lý mà không nền tảng |
| Chỉ kiểm tra invariant `confirmed` | Chống việc LLM tự sinh tiêu chuẩn rồi tự đánh giá theo nó |
| Bắt buộc trích dẫn `file:line` | Claim không kiểm chứng được thì bỏ |
| Luôn cho phép trả lời "không có phát hiện" | Nếu không, LLM sẽ luôn tìm ra cái gì đó |
| Luôn có câu hỏi phản biện | Buộc mô hình chất vấn dữ liệu thay vì hợp lý hóa nó |
| Mỗi mục đích một lần gọi riêng | Prompt gộp làm loãng chất lượng từng phần |
| Output JSON theo schema | Parse được, so sánh được giữa các lần chạy |

### 13.2 Prompt: gom nhóm flow (Bootstrap Stage C)

```
Bạn đang giúp gom nhóm entry point của một codebase thành các business flow.

DỮ LIỆU (từ execution trace, coi là chính xác):
Cụm #{n} — các entry point dùng chung {pct}% entity:
{danh sách entry point}

Entity được chạm nhiều nhất trong cụm:
{top 15 entity ref}

TRẢ VỀ JSON:
{
  "flow_id": "snake_case, <=32 ký tự",
  "flow_name": "tên nghiệp vụ bằng tiếng Việt, người không đọc code cũng hiểu",
  "confidence": "high" | "medium" | "low",
  "should_split": true | false,
  "split_rationale": "nếu should_split=true, giải thích nên tách thế nào",
  "uncertain_entry_points": ["ep có thể không thuộc cụm này"]
}

Nếu cụm này trông như KẾT QUẢ GOM SAI (ví dụ các entry point chỉ dùng chung
middleware/base class/ORM chứ không cùng nghiệp vụ), đặt should_split=true và
nói rõ. Đây là câu trả lời hợp lệ và hữu ích.
```

### 13.3 Prompt: kiểm tra vi phạm invariant (per-PR, quan trọng nhất)

```
Bạn kiểm tra xem một diff có vi phạm các invariant ĐÃ ĐƯỢC KHAI BÁO của một
business flow hay không.

BUSINESS FLOW: {flow.name}
Severity: {flow.severity.class}
Idempotent: {flow.severity.idempotent}
Reversible: {flow.severity.reversible}
Compensating action: {flow.severity.compensating_action}

INVARIANT CẦN KIỂM TRA (chỉ những cái này, không thêm):
{với mỗi invariant confirmed: id | statement | provenance | incident_ref}

DIFF:
{unified diff, chỉ các change unit thuộc flow này}

NGỮ CẢNH CODE (trạng thái sau khi áp diff):
{nội dung các symbol bị chạm + caller trực tiếp}

Với TỪNG invariant ở trên, trả về một phần tử:

{
  "findings": [
    {
      "invariant_id": "inv_atomicity",
      "verdict": "violated" | "weakened" | "unaffected" | "cannot_determine",
      "evidence": [
        {"file": "src/x.py", "line": 42, "why": "giải thích ngắn, cụ thể"}
      ],
      "failure_scenario": "Kịch bản cụ thể ở production dẫn tới vi phạm. Phải nêu rõ điều kiện kích hoạt (concurrency, timeout, input đặc biệt, thứ tự thực thi).",
      "missing_context": "thông tin cần thêm để kết luận, nếu verdict=cannot_determine",
      "suggested_test": "test case cụ thể chặn được kịch bản này, không phải mô tả chung"
    }
  ],
  "proposed_new_invariants": [
    {
      "statement": "invariant chưa được khai báo mà diff này cho thấy là cần thiết",
      "rationale": "vì sao"
    }
  ],
  "challenge": "Dữ liệu hoặc giả định nào ở trên có thể SAI? Flow này có thực sự liên quan tới diff không? Nếu bạn cho rằng việc map diff vào flow này là sai, nói thẳng ra."
}

RÀNG BUỘC:
- Không đưa ra bất kỳ con số xác suất, phần trăm, hay điểm số nào.
- "unaffected" và "cannot_determine" là câu trả lời tốt. Đừng tìm vi phạm
  cho đủ số lượng.
- Mọi evidence phải có file và line thật, trích được từ ngữ cảnh đã cho.
- Không đề xuất chung chung kiểu "nên thêm validation" hay "nên viết thêm
  test". Phải chỉ vào code cụ thể và kịch bản cụ thể.
- proposed_new_invariants sẽ được ghi với status=proposed và KHÔNG tính vào
  điểm rủi ro cho tới khi người xác nhận.
```

Trường `challenge` không phải trang trí. Nó là cơ chế duy nhất khiến LLM báo lại rằng impact mapping ở §8 đã sai — và mapping sai là chế độ hỏng phổ biến nhất của toàn pipeline, vì Tier 0/1 đều overapproximate.

### 13.4 Prompt: phát hiện armed cut set

```
FAULT TREE của flow {flow.name}:
{cây dạng text thụt lề, kèm entity của từng basic event}

MINIMAL CUT SET đã tính (coi là chính xác, do thuật toán sinh):
{danh sách cut set}

CHANGE UNIT trong PR này:
{danh sách entity ref}

TRẢ VỀ JSON:
{
  "armed_cut_sets": [
    {
      "cut_set_id": "...",
      "armed_basic_events": ["..."],
      "combined_failure_scenario": "Kịch bản cụ thể khi cả các basic event này cùng lỗi. Phải là một câu chuyện có thứ tự thời gian, không phải mô tả trừu tượng.",
      "why_layers_not_independent": "Vì sao PR này khiến các lớp phòng thủ mất tính độc lập"
    }
  ],
  "missing_basic_events": [
    {
      "description": "chế độ hỏng mà cây hiện tại chưa có, nhưng diff này gợi ra",
      "suggested_placement": "nên đặt dưới gate nào"
    }
  ]
}

Không đưa ra con số nào.
```

### 13.5 Prompt: diễn giải cuối

Chạy sau khi mọi số đã tính xong. Đây là chỗ LLM tạo giá trị nhiều nhất mà rủi ro thấp nhất:

```
Bạn viết phần diễn giải cho báo cáo rủi ro PR. Mọi con số dưới đây đã được
tính bởi pipeline thống kê — coi là chính xác, không tính lại, không thay đổi.

{JSON output đầy đủ của pipeline}

Viết một đoạn tóm tắt <=200 từ cho người review PR, trả lời:
1. Rủi ro chính là gì, diễn đạt bằng ngôn ngữ nghiệp vụ (không phải "risk
   score 0.08" mà "nếu sai thì khách bị trừ tiền mà không nhận voucher").
2. Thừa số nào chi phối con số này? (P(R) cao? coverage thấp? canary mù?)
3. Việc gì nên làm trước khi merge, xếp theo tỷ lệ giá trị / công sức.
4. Có dấu hiệu nào cho thấy đánh giá này SAI hoặc quá cao? Nói thẳng nếu có.

Không thêm số mới. Không nhắc lại toàn bộ dữ liệu.
```

---

## 14. Output contract

### 14.1 JSON

```json
{
  "schema_version": 1,
  "pr": { "number": 1284, "base_sha": "a1b2c3d", "head_sha": "e4f5g6h" },
  "maturity": "calibrated",
  "change_units": [
    {
      "ref": "src/services/RedemptionService.py#func:RedemptionService.commit",
      "kind": "control_flow",
      "lines_added": 14,
      "lines_removed": 6,
      "diff_coverage": 0.31
    }
  ],
  "flows": [
    {
      "id": "gift_redemption",
      "name": "Đổi mã quà tặng thành voucher",
      "severity_class": "critical",
      "reached_via": ["coverage", "cochange"],
      "reach_confidence": 0.90,
      "executions_per_day": 12400,
      "risk_band": "high",
      "probability": { "value": 0.041, "ci_90": [0.018, 0.087] },
      "dominant_factor": "escape_probability",
      "factor_breakdown": {
        "p_defect": 0.18, "p_reach": 1.00, "p_infect": 0.42,
        "p_propagate": 0.55, "p_escape": 0.78
      },
      "invariant_findings": [
        {
          "invariant_id": "inv_atomicity",
          "provenance": "incident",
          "incident_ref": "INC-2024-041",
          "verdict": "weakened",
          "evidence": [
            { "file": "src/services/RedemptionService.py", "line": 88,
              "why": "issue_voucher() gọi sau khi transaction đã commit" }
          ],
          "failure_scenario": "Nếu voucher provider timeout sau khi mark_redeemed đã commit, mã bị đánh dấu đã dùng mà voucher chưa phát. Retry sẽ fail vì mã đã redeemed.",
          "suggested_test": "Mock provider timeout sau mark_redeemed, assert mã vẫn ở trạng thái redeemable"
        }
      ]
    }
  ],
  "structural_findings": [
    {
      "kind": "armed_cut_set",
      "severity": "hard_block",
      "flow": "gift_redemption",
      "cut_set": ["partial_commit", "retry_not_idempotent"],
      "armed": ["partial_commit", "retry_not_idempotent"],
      "message": "PR chạm 2/2 basic event trong cùng cut set. Hai lớp phòng thủ độc lập bị sửa cùng lúc."
    },
    {
      "kind": "canary_blind",
      "severity": "warning",
      "flow": "admin_bulk_export",
      "message": "~0.7 lần thực thi trong cửa sổ canary 30 phút @5%. Cần ≥30. Canary sẽ PASS rồi lỗi ở full rollout."
    }
  ],
  "decision": {
    "gate": "hard_block",
    "reasons": ["armed_cut_set:gift_redemption"],
    "required_before_merge": [
      "Tách PR: deploy thay đổi transaction và thay đổi retry ở hai lần riêng biệt",
      "Thêm test cho inv_atomicity (xem suggested_test)"
    ],
    "deploy_strategy": {
      "mode": "feature_flag",
      "canary_percent": null,
      "canary_duration_min": null,
      "rationale": "Cut set bị chọc + canary không cho tín hiệu đủ ở flow này",
      "rollback_trigger": "redemption_success_rate < 99.5% trong 10 phút",
      "manual_verification": [
        "Kiểm tra 5 redemption đầu tiên sau khi bật flag"
      ]
    }
  },
  "narrative": "..."
}
```

### 14.2 Bảng quyết định deploy

Đây là phần **ba phần tư giá trị** của toàn hệ thống nằm ở đó. Độ chính xác dự đoán vốn có hạn, nên giá trị thật của con số không phải để gác cổng mà để **định tuyến chiến lược triển khai**.

| Điều kiện | Chiến lược |
|---|---|
| Armed cut set | **Hard block.** Tách PR, deploy từng lớp riêng, có khoảng nghỉ |
| Security-class finding | **Hard block.** Ruin risk, bỏ qua xác suất |
| Manifest entity không resolve | **Hard block.** Sửa drift trước |
| Vi phạm invariant `confirmed` trên flow critical | Feature flag + verify tay + rollback trigger |
| Band `high`/`very_high`, canary có tín hiệu | Canary 5% → 25% → 100%, alert trên metric của flow |
| Band `high`/`very_high`, canary mù | Feature flag + shadow traffic hoặc synthetic probe |
| Band `medium` | Deploy thường + alert trên metric của flow bị chạm |
| Band `low`/`very_low` | Deploy thường |
| `dependency_bump` hoặc `schema_migration` | Checklist riêng, không dùng chuỗi PIE |

### 14.3 PR comment

Ngắn. Chi tiết nằm ở artifact JSON, link tới.

```markdown
## FRA · risk: **high** · gate: **hard_block**

**Flow bị ảnh hưởng**

| Flow | Severity | Band | Yếu tố chi phối |
|---|---|---|---|
| gift_redemption | critical | high | escape probability (diff coverage 31%) |
| admin_bulk_export | medium | low | — |

**Phát hiện cấu trúc**

🔴 **Armed cut set** — `gift_redemption`
PR chạm cả `partial_commit` và `retry_not_idempotent`, hai basic event trong
cùng một minimal cut set. Hai lớp phòng thủ vốn độc lập đang bị sửa cùng lúc.
→ Tách thành hai PR, deploy riêng.

⚠️ **Canary mù** — `admin_bulk_export`
~0.7 lần thực thi trong cửa sổ canary. Canary sẽ xanh mà không kiểm chứng gì.

**Invariant**

`inv_atomicity` (từ INC-2024-041): **weakened**
`RedemptionService.py:88` — `issue_voucher()` gọi sau khi transaction commit.
Nếu provider timeout, mã bị đánh dấu đã dùng mà voucher chưa phát.

**Trước khi merge**
1. Tách PR theo cut set
2. Thêm test: mock provider timeout sau `mark_redeemed`, assert mã còn redeemable

<sub>[Báo cáo đầy đủ](...) · `fra explain gift_redemption` · [Ghi đè](...) (có ghi log)</sub>
```

Nút ghi đè là bắt buộc, và **phải ghi log**. Tỷ lệ ghi đè là chỉ số sức khỏe quan trọng nhất của hệ thống (§17).

---

## 15. Vòng hiệu chỉnh

Không có phần này thì FRA là một máy tạo cảm giác an toàn. Đây là phần phân biệt công cụ thật với dashboard.

### 15.1 Gán nhãn bằng SZZ (chỉ cần git, mọi ngôn ngữ)

```bash
# 1. Tìm commit fix
git log --grep="fix\|hotfix\|revert\|bug\|INC-" -i \
        --pretty=format:"%H|%ad|%s" --date=short --since="2 years ago"

# 2. Với mỗi commit fix, blame các dòng BỊ XÓA về commit cha
#    -> commit nào đã tạo ra dòng lỗi
git show --format="" --unified=0 "$FIX_SHA" -- "$FILE" \
  | grep -E '^-[^-]' > /tmp/removed_lines

git blame -L "$START,$END" "$FIX_SHA^" -- "$FILE" --porcelain \
  | grep -m1 '^[0-9a-f]\{40\}'

# 3. Ánh xạ commit gây lỗi -> PR (merge commit hoặc squash message)
git log --merges --ancestry-path "$BUG_SHA..HEAD" --pretty=format:"%H|%s" | tail -1
```

**Bộ lọc bắt buộc** — không có chúng thì nhãn nhiễu tới mức vô dụng:

- Bỏ commit fix chạm >20 file (thường là refactor được gắn nhãn "fix")
- Bỏ dòng bị xóa chỉ là comment, whitespace, hoặc import
- Bỏ commit gây lỗi cách commit fix >18 tháng (khả năng cao là trùng hợp)
- Bỏ dòng bị blame về commit chỉ đổi format (kiểm bằng `git log --follow -w`)

**Bổ sung bằng incident thật.** SZZ có nhiễu; incident thì không. Nối `deploy → release tag → incident window`:

```
label(PR) = 1 nếu trong 14 ngày sau khi PR được deploy:
              - có hotfix/revert chạm entity mà PR đã chạm, HOẶC
              - có incident/Sentry issue mới quy được về release chứa PR
            0 nếu ngược lại
```

Nhãn từ incident đáng tin hơn nhãn từ SZZ. Nếu có cả hai, gán trọng số mẫu cao hơn cho nhãn incident.

### 15.2 JIT model cho P(D)

Đặc trưng lấy từ diff, tất cả đều language-agnostic:

| Đặc trưng | Cách tính | Lý do |
|---|---|---|
| `lines_added`, `lines_removed` | Từ diff | Cơ bản |
| `files_touched` | Từ diff | |
| `subsystems_touched` | Số thư mục cấp 1–2 riêng biệt | Diffusion |
| `entropy` | `−Σ pᵢ log pᵢ`, `pᵢ` = tỷ lệ dòng đổi ở file i | Thay đổi rải rác rủi ro hơn (Hassan 2009) |
| `author_familiarity` | Số commit trước của tác giả trên các file này | Mạnh |
| `file_age_days` | Tuổi trung bình của các dòng bị đổi | Code cũ ổn định hơn |
| `prior_bugfix_count` | Số lần các file này xuất hiện trong commit fix | **Mạnh nhất** (BugCache locality) |
| `is_weekend`, `hour_of_day` | Từ commit timestamp | Yếu nhưng thật |
| `review_count` | Số approval | |
| `time_to_merge_hours` | | Merge quá nhanh trên PR lớn là tín hiệu |

Logistic regression là đủ, và **nên** ưu tiên hơn model phức tạp: hệ số đọc được, dễ giải thích trong PR comment, ít overfit trên dữ liệu nhỏ.

```python
from sklearn.linear_model import LogisticRegression
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

# Hiệu chỉnh isotonic là bắt buộc: LogisticRegression thô cho ra
# xác suất lệch trên dữ liệu mất cân bằng
model = make_pipeline(
    StandardScaler(),
    CalibratedClassifierCV(
        LogisticRegression(class_weight="balanced", max_iter=1000),
        method="isotonic", cv=5,
    ),
)
```

### 15.3 Đánh giá — không dùng accuracy

Accuracy vô nghĩa ở đây. Nếu 85% PR không gây sự cố thì model đoán "không sự cố" mọi lần đã đạt 85%.

| Chỉ số | Đo gì | Ngưỡng dùng được |
|---|---|---|
| **Brier score** | Chất lượng hiệu chỉnh | < Brier của base rate không đổi |
| **Reliability diagram** | Xác suất dự đoán có khớp tần suất thực? | Nằm gần đường chéo |
| **precision@10** | Trong 10 PR rủi ro nhất, bao nhiêu thật sự sinh sự cố? | > 0.30 |
| **Lift ở decile đầu** | So với chọn ngẫu nhiên | > 2.0× |
| **Override rate** | Tỷ lệ dev bấm ghi đè | **< 0.30** |

`Override rate` là chỉ số quan trọng nhất và duy nhất không thuộc thống kê. Nếu dev ghi đè hơn 30% cảnh báo `high`, mô hình là nhiễu và đang dạy team bỏ qua nó. Sửa mô hình, đừng sửa dev.

### 15.4 Chế độ theo độ chín — quan trọng

**Xuất số khi chưa hiệu chỉnh là tệ hơn không xuất gì**, vì con số tạo ra sự tự tin không có cơ sở.

| Nhãn có sẵn | Chế độ | Được xuất gì |
|---|---|---|
| 0 | `structural_only` | Chỉ phát hiện cấu trúc: flow bị chạm, armed cut set, canary mù, invariant violation, drift. **Không số, không dải** |
| 1–49 | `ordinal` | Dải thứ tự từ luật cấu trúc + severity. Ghi rõ "chưa hiệu chỉnh" |
| 50–199 | `ordinal_with_baserate` | Dải + base rate lịch sử của PR tương đương |
| 200+ | `calibrated` | Xác suất kèm khoảng tin cậy 90%, kèm reliability diagram công khai |

Chế độ `structural_only` **đã rất hữu ích**. Nó cho: flow nào bị chạm, cut set nào bị chọc, canary có mù không, invariant nào bị yếu đi, manifest có rot không. Không một cái nào trong số đó cần hiệu chỉnh.

Nhiều team dừng ở `structural_only` và vẫn thu được phần lớn giá trị. Đó là kết cục hoàn toàn hợp lý, không phải thất bại.

### 15.5 Cách trình bày trung thực

❌ `"PR này có 3.7% xác suất gây sự cố"`

✅ `"PR tương đương trong repo này bị hotfix 12% trong 14 ngày (n=340). PR này ở phân vị 88 — cao hơn nền, chủ yếu vì chạm flow critical không idempotent với diff coverage 31% và canary không cho tín hiệu ở tần suất này."`

Câu thứ hai dài hơn, nhưng nó nói rõ: base rate, cỡ mẫu, vị trí tương đối, và **thừa số chi phối**. Người đọc kiểm chứng và phản biện được. Câu đầu thì không.

---

## 16. Triển khai theo giai đoạn

Mỗi giai đoạn phải tự mang lại giá trị. Đừng làm Phase 4 trước Phase 1.

### Phase 1 — Impact mapping (1–2 tuần) · **60% giá trị**

- Manifest cho **3 flow critical** (làm tay, dùng Stage A + D1)
- Diff → change unit ở mức L1 (file). Chưa cần tree-sitter
- Impact closure Tier 0 (co-change) — chỉ cần `git log`
- Operational profile từ access log
- Output: `structural_only` mode

Ra được ngay: *"PR này chạm flow checkout (12k lần/ngày, critical, không idempotent) và flow admin_export (20 lần/ngày, medium)."*

Không có xác suất nào. Vẫn hữu ích hơn phần lớn tool review tự động.

### Phase 2 — Detection & invariant (2–3 tuần) · **+20%**

- tree-sitter cho ngôn ngữ chính → change unit mức L2
- Diff coverage
- Tính `d_canary` → cảnh báo canary mù
- LLM invariant check với invariant `confirmed`
- Mở rộng manifest lên 10–15 flow

Ra được: *"canary sẽ mù ở flow này"* và *"inv_atomicity bị yếu đi tại file:line"*. Cả hai đều là phát hiện cụ thể, kiểm chứng được.

### Phase 3 — Fault tree (2–3 tuần) · **+10%**

- Fault tree cho 3 flow critical (từ postmortem)
- Minimal cut set + kiểm tra armed
- Bảng quyết định deploy strategy

Ra được: armed cut set detection — phát hiện mà không công cụ nào khác cho được.

### Phase 4 — Hiệu chỉnh (liên tục) · **+10% và độ tin cậy**

- SZZ labeling trên lịch sử
- JIT model cho `P(D)`
- Brier score + reliability diagram công khai
- Chuyển sang `calibrated` mode khi đủ 200 nhãn
- Theo dõi override rate hàng tuần

### Đường tắt nếu chỉ có 3 ngày

Làm đúng ba thứ này, bỏ hết phần còn lại:

1. Manifest tay cho 3 flow critical, chỉ cần `entities` + `severity` + `executions_per_day`
2. Script: diff → file list → khớp `entities` → in ra flow nào bị chạm
3. Thêm cảnh báo canary mù (một phép chia)

Ba thứ này đã bắt được phần lớn sự cố mà loại hệ thống này nhắm tới, vì đa số sự cố production không đến từ code phức tạp — chúng đến từ việc **không ai nhận ra thay đổi này chạm vào flow quan trọng**.

---

## 17. Anti-pattern và chế độ hỏng

### 17.1 Sai lầm khi xây

| Anti-pattern | Vì sao sai | Làm đúng |
|---|---|---|
| Gate merge bằng xác suất chưa hiệu chỉnh | Tạo lòng tin không có cơ sở, dev học cách bỏ qua | Gate bằng phát hiện cấu trúc (boolean); xác suất chỉ để xếp hạng |
| Hỏi LLM sinh xác suất | LLM không hiệu chỉnh, luôn trả số nghe hợp lý | LLM enumerate và map; số đến từ log + history |
| Dự đoán mức từng dòng | Precision quá thấp, nhiễu > tín hiệu | Mức symbol / commit |
| Trộn style lint vào điểm rủi ro | Phá tín hiệu; 95% finding là format | Loại hẳn khỏi mô hình |
| Manifest do LLM sinh toàn bộ, không ai review | Hệ thống tự đánh giá theo tiêu chuẩn tự sinh | Chỉ invariant `confirmed` được tính điểm |
| Bỏ qua common cause (`β`) | PR lớn bị đánh giá thấp một cách hệ thống | Beta-factor model §2.2 |
| Dùng total coverage thay diff coverage | Repo coverage 80% vẫn có thể diff coverage 0% | Chỉ tính dòng đã đổi |
| Coi `None` như `0` ở operational profile | Flow chưa instrument tự động thành "an toàn" | `None` → giá trị bảo thủ |
| Impact closure không giới hạn depth | Mọi PR "chạm mọi flow", output vô nghĩa | `max_depth ≤ 3` |
| Entity ref dùng line range (L3) | Vỡ sau mỗi lần format, tạo drift giả | L1/L2 |
| Nhét `dependency_bump` vào chuỗi PIE | Chế độ hỏng khác hẳn | Checklist riêng |
| Không có nút ghi đè | Dev bị chặn sẽ tìm cách lách hoặc bỏ dùng | Có ghi đè, và **ghi log** |

### 17.2 Chế độ hỏng lâu dài

**Manifest rot** — chế độ hỏng phổ biến nhất, và nó âm thầm. Code đi tiếp, manifest ở lại. Phòng: `fra doctor` fail CI khi entity không resolve; ownership rõ ràng; kiểm tra "file có traffic mà không thuộc flow nào".

**Alert fatigue** — nếu 60% PR bị gắn `high` thì nhãn `high` mất nghĩa. Phòng: đặt ngưỡng dải ở **phân vị** của phân bố thực tế, không phải số tuyệt đối; theo dõi override rate hàng tuần.

**Goodhart's law** — khi dev biết có điểm, họ sẽ tối ưu điểm. Biểu hiện cụ thể: tách PR thành nhiều PR nhỏ để giảm điểm, kể cả khi tách làm **tăng** tổng rủi ro vì mất tính nguyên tử của thay đổi. Phòng: theo dõi tỷ lệ PR-nhỏ-liên-tiếp; đừng đưa risk score vào đánh giá hiệu suất cá nhân.

**Giới hạn thông tin không khắc phục được** — mô hình không thấy được invariant chưa ai viết ra. Đây **không** phải giới hạn kỹ thuật sửa được bằng model tốt hơn. Cách duy nhất để cải thiện là viết thêm invariant, thường sau khi trả giá bằng một sự cố. Chấp nhận điều này và biến mỗi postmortem thành ít nhất một invariant mới.

**Ảo tưởng độ chính xác** — con số `0.041` gợi ý độ chính xác 3 chữ số mà mô hình không có. Luôn xuất kèm khoảng tin cậy, và làm tròn thô hơn bạn nghĩ là cần.

---

## 18. Cấu trúc reference implementation

Ngôn ngữ orchestrator độc lập hoàn toàn với ngôn ngữ được phân tích. Python là chọn lựa thực dụng (tree-sitter binding tốt, thư viện thống kê sẵn), nhưng Go hay TypeScript đều được.

```
fra/
├── cli.py                   # fra analyze | doctor | bootstrap | calibrate | explain
├── config.py
├── manifest/
│   ├── schema.py            # validate theo JSON Schema §3.2
│   ├── loader.py
│   └── doctor.py            # drift detection §3.4
├── symbols/
│   ├── treesitter.py        # §6.2
│   ├── ctags.py             # fallback
│   └── index.py             # cache, invalidate theo file mtime + git sha
├── diff/
│   ├── parser.py            # §7.1
│   └── classifier.py        # §7.2 change_kind từ AST diff
├── impact/
│   ├── cochange.py          # Tier 0 §8
│   ├── imports.py           # Tier 1
│   ├── coverage.py          # Tier 2
│   ├── callgraph.py         # Tier 3, optional
│   └── closure.py           # hợp nhất + confidence
├── adapters/
│   ├── base.py              # ABC §9.1
│   ├── operational/         # accesslog, prometheus, datadog, otel, manual
│   ├── coverage/            # lcov, cobertura, clover, jacoco, gocover, coveragepy
│   ├── mutation/            # stryker, pitest, mutmut, infection
│   └── incidents/           # sentry, pagerduty, jira, file
├── tree/
│   ├── loader.py
│   ├── cutsets.py           # §11.2
│   ├── armed.py             # §11.3
│   └── bayes.py             # §11.4, optional
├── scoring/
│   ├── pie.py               # §12
│   ├── aggregate.py         # beta-factor
│   └── bands.py
├── llm/
│   ├── client.py            # provider-agnostic
│   ├── prompts/             # §13, versioned — thay đổi prompt = thay đổi mô hình
│   └── guards.py            # chặn output số, bắt buộc file:line
├── calibration/
│   ├── szz.py               # §15.1
│   ├── features.py          # §15.2
│   ├── train.py
│   └── evaluate.py          # Brier, reliability, precision@k, override rate
└── report/
    ├── json.py              # §14.1
    ├── comment.py           # §14.2
    └── decision.py          # bảng deploy strategy
```

**Ghi chú về `llm/prompts/`:** phải version hóa và commit. Đổi prompt là đổi mô hình — nếu không track, bạn sẽ không giải thích được vì sao điểm rủi ro tuần này khác tuần trước. Ghi prompt version vào output JSON.

### CI integration

```yaml
# .github/workflows/fra.yml — nguyên tắc áp dụng cho mọi CI
name: FRA
on: pull_request

jobs:
  assess:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0          # BẮT BUỘC: cần full history cho co-change + SZZ

      - name: Validate manifest
        run: fra doctor --fail-on unresolved_entity

      - name: Restore caches
        uses: actions/cache@v4
        with:
          path: |
            .fra/cache/symbols
            .fra/cache/cochange.json
          key: fra-${{ hashFiles('.fra/config.yaml') }}-${{ github.base_ref }}

      # Coverage tùy chọn — thiếu thì pipeline vẫn chạy, chỉ mất d_test
      - name: Tests with coverage
        continue-on-error: true
        run: make test-coverage

      - name: Analyze
        env:
          FRA_LLM_API_KEY: ${{ secrets.FRA_LLM_API_KEY }}
        run: |
          fra analyze \
            --base "${{ github.event.pull_request.base.sha }}" \
            --head "${{ github.event.pull_request.head.sha }}" \
            --output fra-report.json \
            --comment fra-comment.md

      - uses: actions/upload-artifact@v4
        with: { name: fra-report, path: fra-report.json }

      - name: Comment
        run: fra post-comment --file fra-comment.md --pr ${{ github.event.number }}

      # Gate CHỈ dựa trên phát hiện cấu trúc, không dựa trên xác suất
      - name: Gate
        run: fra gate --report fra-report.json --on structural_only
```

Hai chi tiết dễ bỏ sót và cả hai đều gây lỗi im lặng:

- `fetch-depth: 0` — thiếu nó thì co-change và SZZ không có dữ liệu, và chúng sẽ trả về kết quả rỗng thay vì báo lỗi.
- Cache co-change theo `base_ref` — tính lại trên mỗi PR mất vài phút trên repo lớn.

---

## 19. Tài liệu nền

| Chủ đề | Nguồn |
|---|---|
| PIE model | Voas, *PIE: A Dynamic Failure-Based Technique* (IEEE TSE, 1992) |
| RIPR model | Li & Offutt, *Test Oracle Strategies for Model-Based Testing* (2014) |
| JIT defect prediction | Kamei et al., *A Large-Scale Empirical Study of Just-in-Time Quality Assurance* (IEEE TSE, 2013) |
| Change entropy | Hassan, *Predicting Faults Using the Complexity of Code Changes* (ICSE 2009) |
| BugCache locality | Kim et al., *Predicting Faults from Cached History* (ICSE 2007) |
| SZZ algorithm | Śliwerski, Zimmermann, Zeller, *When Do Changes Induce Fixes?* (MSR 2005) |
| FTA → BN | Bobbio et al., *Improving the Analysis of Dependable Systems by Mapping Fault Trees into Bayesian Networks* (2001) |
| Beta-factor common cause | NUREG/CR-5485, *Guidelines on Modeling Common-Cause Failures* |
| Operational profile | Musa, *Operational Profiles in Software-Reliability Engineering* (IEEE Software, 1993) |
| Change impact analysis | Arnold & Bohner, *Impact Analysis — Towards a Framework for Comparison* (1993) |
| Swiss cheese model | Reason, *Human Error* (1990) |
| STPA | Leveson, *Engineering a Safer World* (2011) |
| Calibration scoring | Brier, *Verification of Forecasts Expressed in Terms of Probability* (1950) |

---

## Phụ lục A — Bảng tra manifestation cho code standard

Dùng cho §2.5. Cột `P(manifest)` là **giả thuyết khởi đầu**, phải thay bằng số hiệu chỉnh từ incident history của bạn.

| Lớp finding | P(manifest) | Vào đâu | Ví dụ rule |
|---|---|---|---|
| Formatting, naming, import order | 0.00 | **Loại** | prettier, gofmt, PSR-12 |
| Unused variable/import, dead code | 0.00 | Loại | no-unused-vars |
| Complexity threshold | 0.01 | Chỉ báo cáo | cyclomatic > 15 |
| Missing type annotation | 0.03 | `P(I)` × 1.1 | mypy, TS strict |
| Null/undefined dereference | 0.15 | `P(I)` × 1.3 | nullability check |
| Unchecked array/map access | 0.12 | `P(I)` × 1.3 | |
| Type coercion / implicit cast | 0.10 | `P(I)` × 1.2 | `==` vs `===` |
| Missing error handling trên I/O | 0.20 | `P(I)` × 1.4 | errcheck, no-floating-promises |
| Swallowed exception | 0.18 | `P(I)` × 1.4 | catch rỗng |
| N+1 query | 0.25 | `P(I)` × 1.4 | ORM linter |
| Unbounded query / thiếu LIMIT | 0.30 | `P(I)` × 1.5 | |
| Thiếu transaction quanh multi-write | 0.35 | `P(I)` × 1.6 | |
| Thiếu idempotency key | 0.30 | `P(I)` × 1.5 | |
| Resource leak (file, conn, lock) | 0.25 | `P(I)` × 1.4 | |
| Thiếu timeout trên external call | 0.28 | `P(I)` × 1.5 | |
| Thiếu circuit breaker | 0.20 | `P(I)` × 1.3 | |
| Race condition / TOCTOU | 0.35 | `P(I)` × 1.6 | |
| **Thiếu authz check** | — | **Hard gate** | Ruin risk |
| **Injection (SQL/cmd/template)** | — | **Hard gate** | |
| **Secret hardcode** | — | **Hard gate** | gitleaks, trufflehog |
| **Crypto yếu / random không an toàn** | — | **Hard gate** | |

Bốn dòng cuối áp dụng **ruin risk**: severity không phục hồi được, nên bỏ qua xác suất và chặn tuyệt đối. Đây là áp dụng trực tiếp nguyên tắc của Taleb — với rủi ro có thể xóa sổ, phép tính kỳ vọng không dùng được.

## Phụ lục B — Checklist khởi động

```
□ .fra/config.yaml — chọn adapter, để mặc định cho phần chưa có
□ Chọn 3 flow critical. Tiêu chí: mất tiền, hỏng dữ liệu, hoặc rò rỉ
□ Với mỗi flow:
    □ entry_points — từ access log, không đoán từ code
    □ entities — mức L1 (file) là đủ để bắt đầu
    □ severity + 4 cờ khuếch đại
    □ executions_per_day — từ log
□ Đọc lại 6 postmortem / hotfix gần nhất
    □ Mỗi cái → tối thiểu 1 invariant, provenance: incident
□ Chạy `fra doctor` — sửa mọi entity không resolve
□ Chạy `fra analyze` trên 20 PR đã merge gần nhất
    □ Đối chiếu: nó có gắn cờ đúng những PR đã gây sự cố?
    □ Nó có gắn cờ tràn lan lên PR vô hại? (nếu có, thắt closure lại)
□ Bật ở chế độ comment-only, KHÔNG gate, trong 2–4 tuần
□ Theo dõi override rate. Nếu > 0.30, sửa mô hình trước khi bật gate
□ Bật gate CHỈ cho: armed cut set, security finding, manifest drift
```

Bước "chạy trên 20 PR đã merge" là bước kiểm chứng rẻ nhất và hay bị bỏ. Nếu FRA không gắn cờ được PR đã thực sự gây sự cố trong quá khứ, đừng bật nó lên — sửa manifest trước.
