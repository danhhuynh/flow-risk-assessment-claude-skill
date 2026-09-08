---
name: flow-risk-assessment
description: 'Đánh giá và dự đoán rủi ro sự cố production từ PR diff dựa trên Business Flow Manifest (FRA). Dựng manifest từ code và traffic log, map diff sang business flow, tính chuỗi PIE (Reachability-Infection-Propagation), phát hiện armed cut set qua fault tree, tính canary blindness, và chọn chiến lược deploy. Dùng skill này bất cứ khi nào người dùng nhắc tới dự đoán lỗi hoặc rủi ro từ PR/diff/commit, deploy risk, business flow manifest, fault tree hoặc minimal cut set cho phần mềm, xác suất sự cố production, change impact analysis, defect prediction, canary có đủ tín hiệu hay không, hoặc muốn xây/triển khai/mở rộng hệ thống FRA. Cũng dùng khi người dùng hỏi "PR này có nguy hiểm không", "thay đổi này ảnh hưởng flow nào", "nên deploy thế nào cho an toàn", "làm sao biết code này có gây lỗi", hoặc khi đọc/viết/sửa bất kỳ file trong .fra/. Also use for English phrasings such as PR risk assessment, production incident prediction, blast radius analysis, business flow impact mapping, deployment strategy selection.'
---

# Flow Risk Assessment (FRA)

FRA dự đoán rủi ro sự cố production từ PR diff, dựa trên **Business Flow
Manifest** — mô tả tường minh các luồng nghiệp vụ, invariant, và tần suất vận
hành của chúng.

Với mỗi PR, FRA trả lời:

1. Thay đổi này chạm business flow nào?
2. Rủi ro cao hay thấp, và **thừa số nào chi phối**?
3. Nên deploy bằng chiến lược gì?

**Nền tảng lý thuyết:** mô hình PIE (Voas 1992) / RIPR. Một fault chỉ thành
failure khi đi trọn chuỗi Reachability → Infection → Propagation →
Revealability. Đây là lý do FRA **không phải** defect predictor chấm điểm file:
nó phân rã xác suất theo chuỗi nhân quả, và mỗi thừa số có nguồn dữ liệu riêng.

```
P(sự cố | change_unit, flow) =
    P(D)        diff chứa defect        ← JIT model từ git history
  × P(R)        flow thực thi nó        ← ACCESS LOG (dữ liệu cứng nhất)
  × P(I)        defect làm hỏng state   ← mutation score, change_kind
  × P(P)        lan tới kết quả nghiệp vụ ← impact closure, invariant
  × P(¬detect)  thoát test/review/canary ← diff coverage, canary math
```

`P(R)` thường là thừa số chi phối **và** là thừa số biết chính xác nhất. Nếu
chỉ triển khai được một thứ, triển khai nó.

---

## Xác định người dùng đang ở đâu

Trước khi làm gì, xác định tình huống rồi đi đúng nhánh:

| Dấu hiệu | Nhánh | Đọc gì |
|---|---|---|
| Chưa có `.fra/` trong repo | **Bootstrap** | `references/IMPLEMENTATION.md` Phase 0–1 |
| Có `.fra/flows/` rồi, hỏi về một PR cụ thể | **Analyze** | Chạy `scripts/fra.py analyze` |
| Muốn viết hoặc sửa manifest | **Author** | `references/MANIFEST-GUIDE.md` |
| Muốn xây tiếp Phase 2/3/4 | **Build** | `references/IMPLEMENTATION.md` task tương ứng |
| Hỏi về lý thuyết, công thức, hoặc "vì sao thiết kế thế này" | **Explain** | `references/SPEC.md` mục liên quan |
| Vừa có sự cố production | **Learn** | `references/MANIFEST-GUIDE.md` §5 — postmortem → invariant |

Nếu không rõ, hỏi người dùng thay vì đoán. Bootstrap và Build là hai luồng công
việc rất khác nhau.

---

## Luật cứng — không bao giờ vi phạm

Mười hai luật dưới đây là các sai lầm **đã được xác định trước**. Chúng trông vô
hại lúc code nhưng phá hủy giá trị của hệ thống, và phần lớn không phát hiện
được bằng test. Nếu một task có vẻ yêu cầu vi phạm một luật, dừng lại và hỏi
người.

### 1. LLM không bao giờ sinh ra số

```
❌ "Xác suất PR này gây lỗi là bao nhiêu %?"
❌ "Cho điểm rủi ro từ 1 đến 10"
✅ "Invariant inv_atomicity có bị vi phạm không? verdict + evidence file:line"
```

Output LLM không có calibration. Hỏi số thì luôn nhận về con số nghe hợp lý mà
không nền tảng nào. Mọi số phải đến từ: access log, git history, coverage
report, hoặc phép tính tường minh trong code.

### 2. Chỉ invariant `status: confirmed` được tính điểm

`llm_proposed` chỉ xuất hiện dưới dạng advisory. Không bao giờ tự động chuyển
`proposed` → `confirmed`.

Không có hàng rào này, hệ thống sẽ dần tự sinh tiêu chuẩn rồi tự đánh giá theo
tiêu chuẩn của chính nó.

### 3. `None` ≠ `0` ở operational profile

```python
❌ epd = profile.get(endpoint, 0)
✅ epd = profile.get(endpoint)          # None nếu không biết
   p_r = 0.5 if epd is None else ...    # unknown → bảo thủ
```

`0` = "đo được, không có traffic" → rủi ro thật sự thấp.
`None` = "không đo được" → phải bảo thủ.

Nhầm hai thứ này khiến mọi flow chưa instrument **tự động được đánh giá an
toàn** — ngược hẳn thực tế. Đây là một dòng code trông hoàn toàn bình thường.

### 4. Diff coverage, không phải total coverage

Chỉ tính coverage trên các dòng đã thay đổi. Repo coverage 80% vẫn có thể có
diff coverage 0% trên đúng PR nguy hiểm.

### 5. Impact closure phải có `max_depth ≤ 3`

Ở depth 4+, closure phình ra chạm gần hết repo và mọi PR sẽ "chạm mọi flow".
Ràng buộc bắt buộc, không phải tinh chỉnh.

### 6. Gate chỉ dựa trên phát hiện cấu trúc

```
✅ Gate được:   armed cut set, security finding, manifest drift
                → boolean, đúng bất kể đã hiệu chỉnh hay chưa
❌ Không gate:  risk_band, probability value
                → chỉ để xếp hạng và định tuyến deploy strategy
```

### 7. Không xuất số khi chưa hiệu chỉnh

| Nhãn có sẵn | Được xuất gì |
|---|---|
| 0 | Chỉ phát hiện cấu trúc. **Không số, không dải** |
| 1–49 | Dải thứ tự, ghi rõ "chưa hiệu chỉnh" |
| 50–199 | Dải + base rate lịch sử |
| 200+ | Xác suất kèm khoảng tin cậy 90% |

Xuất số khi chưa hiệu chỉnh **tệ hơn không xuất gì**, vì con số tạo ra sự tự tin
không có cơ sở.

### 8. Luôn tính common cause (`β`)

```python
❌ p_flow = 1 - prod(1 - p for p in ps)
✅ p_ind  = 1 - prod(1 - (1-beta)*p for p in ps)
   p_cc   = beta * max(ps)
   p_flow = p_ind + p_cc - p_ind*p_cc
```

Change unit trong cùng PR **không độc lập**: cùng tác giả, cùng mô hình tư duy
sai. Bỏ `β` khiến PR lớn bị đánh giá thấp một cách hệ thống.

### 9. Style lint không vào điểm rủi ro

Loại hẳn: formatting, naming, import order, unused variable, dead code.
`P(thành sự cố) ≈ 0` và chúng chiếm >90% output linter. Trộn vào là cách nhanh
nhất để phá tín hiệu.

Bảng tra finding nào được vào `P(I)` và trọng số bao nhiêu:
`references/SPEC.md` Phụ lục A.

### 10. Entity ref không dùng line range

```
✅ src/services/Checkout.py                      (L1 — mặc định)
✅ src/services/Checkout.py#func:Checkout.charge (L2)
❌ src/services/Checkout.py#L120-L145            (L3 — vỡ sau mỗi lần format)
```

### 11. `dependency_bump` và `schema_migration` không chạy qua chuỗi PIE

Chế độ hỏng khác hẳn (transitive CVE, lock contention, backward
incompatibility trong rolling deploy). Cần checklist riêng.

### 12. Phải có nút ghi đè, và phải ghi log

Dev bị chặn mà không có đường ra sẽ tìm cách lách hoặc bỏ dùng hệ thống. Tỷ lệ
ghi đè là chỉ số sức khỏe quan trọng nhất: **nếu > 0.30, sửa mô hình, đừng sửa
dev.**

---

## Ba phát hiện có giá trị nhất — đều không cần hiệu chỉnh

Khi ưu tiên công việc, ba thứ này cho giá trị cao nhất trên công sức bỏ ra, và
chúng đúng ngay từ ngày đầu vì đều là kết quả boolean hoặc phép tính tường minh.

### Armed cut set

> PR này có chạm **≥2 basic event trong cùng một minimal cut set** không?

Nếu có, các lớp phòng thủ vốn độc lập bị chọc **cùng lúc** — mô hình phô mai
Thụy Sĩ được hình thức hóa.

Ví dụ điển hình: PR sửa cả validation logic *và* job reconciliation vốn để bắt
dữ liệu sai. Từng thay đổi vô hại. Cùng lúc thì hai lỗ thẳng hàng.

Khuyến nghị: **tách PR, deploy từng lớp riêng, có khoảng nghỉ.** Giữ lại tính
độc lập của các lớp phòng thủ theo thời gian, dù code cuối cùng vẫn như nhau.

### Canary blindness

Canary chỉ cho tín hiệu nếu đủ số lần thực thi trong cửa sổ:

```
expected = executions_per_day × (canary_minutes / 1440) × traffic_share
mù nếu expected < 30
```

Flow 20 lần/ngày, canary 30 phút @5% → **0.02 lần thực thi**. Canary sẽ xanh
100% và nói rằng thay đổi an toàn. Nó không nói vậy — nó chỉ chưa thấy gì.

Luôn in cảnh báo này khi áp dụng, kể cả với PR rủi ro thấp. Kèm thời lượng cần
thiết tính ngược từ traffic, và đề xuất thay thế (feature flag, shadow traffic,
synthetic probe).

### Impact mapping

Chỉ cần `git log`. Đa số sự cố production không đến từ code phức tạp mà từ việc
**không ai nhận ra thay đổi này chạm flow nào**.

Tier 0 (co-change coupling) bắt được phụ thuộc mà không static analysis nào
thấy: template ↔ controller, migration ↔ model, config ↔ code đọc config, IaC ↔
app. Đó thường chính là chỗ sự cố xảy ra — người sửa một bên mà quên bên kia.

---

## Dùng script

`scripts/fra.py` là implementation Phase 1, chạy được ngay. Chỉ cần `pyyaml`,
không phụ thuộc ngôn ngữ của repo được phân tích.

```bash
python3 scripts/fra.py init                                    # scaffold .fra/
python3 scripts/fra.py traffic --log access.log --format nginx --days 30
python3 scripts/fra.py doctor                                  # drift detection
python3 scripts/fra.py analyze --base <sha> --head <sha>
python3 scripts/fra.py backtest --last 30                      # kiểm chứng
```

Format log hỗ trợ: `nginx`, `apache`, `cloudfront`, `json`.

**`backtest` là cửa chặn thật của Phase 1.** Đối chiếu bằng tay với các sự cố đã
biết. Nếu FRA không gắn cờ được những thay đổi đã thực sự gây sự cố, **không bật
lên** — sửa manifest trước. Đây là bước kiểm chứng rẻ nhất và hay bị bỏ.

---

## Ba việc không được tự quyết

Đây là **quyết định kinh doanh**, không suy ra được từ code:

1. Gán `severity.class` cho một flow
2. Confirm một invariant `llm_proposed`
3. Đặt hoặc thay đổi ngưỡng gate

Luôn hỏi người. Nếu tự quyết, hệ thống sẽ dần đánh giá theo tiêu chuẩn nó tự
sinh ra — mất hết giá trị.

Ngoài ra không tự làm: bật `calibrated` mode, sửa/xóa nhãn trong
`.fra/calibration/labels.jsonl`.

---

## Giới hạn cần thừa nhận

Ghi ở đây để không ai cố "sửa" chúng bằng model phức tạp hơn.

**Mô hình không thấy được invariant chưa ai viết ra.** Giới hạn thông tin, không
phải giới hạn kỹ thuật. Cách duy nhất để cải thiện là viết thêm invariant,
thường sau khi trả giá bằng một sự cố. Mỗi postmortem phải sinh ra ≥1 invariant.

**Dự đoán mức từng dòng không khả thi.** Precision quá thấp, nhiễu nhiều hơn
tín hiệu. Đơn vị nhỏ nhất là symbol.

**Goodhart's law sẽ đến.** Khi dev biết có điểm, họ sẽ tối ưu điểm — biểu hiện
cụ thể là tách PR nhỏ để giảm điểm, kể cả khi tách làm *tăng* tổng rủi ro vì mất
tính nguyên tử. Không đưa risk score vào đánh giá hiệu suất cá nhân.

**Ba phần tư giá trị nằm ở deploy strategy, không ở con số.** Độ chính xác dự
đoán vốn có hạn. Giá trị thật là định tuyến: flow nào cần feature flag, flow nào
canary sẽ mù, PR nào phải tách. Nếu phải chọn giữa "số chính xác hơn" và "định
tuyến tốt hơn", chọn cái sau.

---

## File tham chiếu

Đọc theo nhu cầu, đừng load hết:

| File | Khi nào đọc | Dòng |
|---|---|---|
| `references/SPEC.md` | Cần công thức, schema đầy đủ, hoặc lý do thiết kế. Có mục lục 19 phần + 2 phụ lục | ~2150 |
| `references/IMPLEMENTATION.md` | Nhận một task cụ thể. 25 task, mỗi task có acceptance criteria | ~490 |
| `references/MANIFEST-GUIDE.md` | Viết hoặc sửa manifest. Dành cho người, không cho agent | ~250 |
| `assets/_TEMPLATE.flow.yaml` | Tạo flow mới. Copy vào `.fra/flows/<id>.yaml` | ~95 |
| `assets/DECISIONS-template.md` | Seed `docs/fra/DECISIONS.md` trong repo đích | ~90 |
| `assets/ci/` | Hook vào CI. Có sẵn GitHub Actions và GitLab CI | — |

Mục hay cần trong `SPEC.md`: §2 lý thuyết · §3 manifest schema · §4 bootstrap
code→manifest · §6 entity resolution · §7 diff analysis · §8 impact closure ·
§10 canary math · §11 fault tree · §13 prompt LLM · §15 calibration · Phụ lục A
bảng tra code standard.
