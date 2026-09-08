# Hướng dẫn viết Business Flow Manifest

Manifest là artifact quan trọng nhất của FRA. **Chất lượng manifest là ràng
buộc chặn** — mọi tầng bên dưới không thể tốt hơn nó.

Tài liệu này dành cho người viết manifest, không phải cho agent.

---

## 1. Nguyên tắc

### Flow là đơn vị nghiệp vụ, không phải đơn vị code

```
✅ "Đổi mã quà tặng thành voucher"
✅ "Khách đăng ký tài khoản và xác thực email"
❌ "RedemptionService"
❌ "Các endpoint /api/v1/*"
```

Phép thử: **người không đọc code có hiểu tên flow không?** Nếu không, đặt lại tên.

### Bắt đầu từ traffic, không từ code

Endpoint có traffic **là** entry point. Đây là dữ liệu thực nghiệm.
Suy ra entry point từ code là suy đoán, và nó bỏ sót route động, đồng thời
thêm vào những route không ai dùng.

```bash
python3 fra.py traffic --log access.log --format nginx --days 30
```

### Ba flow là đủ để bắt đầu

Đừng cố phủ hết repo. Chọn 3 flow theo thứ tự ưu tiên:

1. Mất tiền nếu sai
2. Hỏng dữ liệu không phục hồi được
3. Rò rỉ dữ liệu cá nhân
4. Chặn nghiệp vụ chính, cần can thiệp tay

**Không** chọn theo "code phức tạp nhất" hay "file bị sửa nhiều nhất". Đó là
tiêu chí của defect prediction, không phải của FRA. FRA quan tâm *hậu quả*,
không quan tâm *độ khó*.

---

## 2. Từng field

### `entities` — dùng mức nào?

| Mức | Ví dụ | Khi nào dùng |
|---|---|---|
| L0 glob | `src/payments/**` | Vùng lớn không có ranh giới rõ: config, migration |
| **L1 file** | `src/services/Checkout.py` | **Mặc định.** Đủ chính xác, ít rot |
| L2 symbol | `...#func:Checkout.charge` | Khi file quá lớn và chỉ một phần thuộc flow |
| L3 line | `...#L120-L145` | **Không bao giờ.** Vỡ sau mỗi lần format |

Dùng wildcard extension để manifest sống qua refactor hoặc port ngôn ngữ:

```yaml
entities:
  - "src/services/CheckoutService.*"     # sống qua .php → .ts
  - "src/controllers/Checkout*"          # sống qua đổi hậu tố
```

### `severity.class` — rubric cố định

| Class | Tiêu chí (chọn class **cao nhất** khớp) |
|---|---|
| `critical` | Mất tiền, hỏng dữ liệu không phục hồi, rò rỉ dữ liệu cá nhân, hoặc vi phạm quy định |
| `high` | Chặn nghiệp vụ chính, cần can thiệp tay để khôi phục |
| `medium` | Giảm chất lượng, có workaround, tự phục hồi |
| `low` | Chỉ ảnh hưởng nội bộ hoặc thẩm mỹ |

Đây là **quyết định kinh doanh**. Người điền, không phải AI. Nếu không chắc,
hỏi product owner — đừng suy từ code.

### Bốn cờ khuếch đại — quan trọng hơn cả `class`

```yaml
severity:
  idempotent: false              # retry có gây double-effect?
  reversible: false              # có undo được không?
  compensating_action: manual    # automatic | manual | none
  external_side_effects: true    # gọi API ngoài? gửi email/SMS?
```

Trong thực tế bốn cờ này phân biệt rủi ro tốt hơn `class`. Một flow `medium` mà
không idempotent, không reversible, có side effect ra ngoài thì **nguy hiểm hơn**
một flow `high` có compensating action tự động.

Cách xác định:

| Cờ | Câu hỏi kiểm tra |
|---|---|
| `idempotent` | Gọi lại đúng request đó 2 lần thì kết quả có giống 1 lần? |
| `reversible` | Có nút undo, hoặc query SQL nào hoàn tác được không? |
| `compensating_action` | Nếu sai, ai sửa và bằng cách nào? Script tự động = `automatic` |
| `external_side_effects` | Có gì rời khỏi hệ thống mà không lấy lại được? Email đã gửi thì không |

### `executions_per_day` — lấy từ log, đừng đoán

Đây là thừa số quan trọng nhất của toàn mô hình, và là thừa số bạn **biết chính
xác nhất**. Đừng làm hỏng nó bằng cách đoán.

| Loại flow | Nguồn |
|---|---|
| HTTP | `fra.py traffic` |
| Cron | Đọc cron expression. `0 */6 * * *` = 4/ngày. Chính xác tuyệt đối |
| Queue | Metric `messages_processed` từ broker |
| Event | Đếm từ event store |
| CLI | Điền tay, ghi `source: manual` |

Nếu thật sự không biết: **để trống**, không điền `0`. `0` nghĩa là "đo được và
không có traffic" → hệ thống sẽ coi flow này rủi ro thấp. Để trống nghĩa là
"không biết" → hệ thống rơi về giá trị bảo thủ.

---

## 3. Viết invariant — phần khó nhất

### Invariant là gì

Một phát biểu **luôn phải đúng**, bất kể input, thời điểm, hay thứ tự thực thi.

```
✅ "Một mã chỉ redeem thành công đúng 1 lần, kể cả khi 2 request đồng thời"
✅ "mark_redeemed và issue_voucher phải cùng 1 transaction, hoặc có outbox"
✅ "voucher.amount == gift_code.face_value, không làm tròn"

❌ "Code phải xử lý lỗi tốt"                    (không kiểm chứng được)
❌ "Nên validate input"                          (là lời khuyên, không phải invariant)
❌ "Hàm charge() trả về boolean"                 (là signature, không phải invariant)
```

Phép thử: **có thể viết một test fail khi invariant bị vi phạm không?** Nếu
không, phát biểu lại cho cụ thể hơn.

### Nguồn invariant, xếp theo chất lượng

**1. Từ sự cố đã xảy ra (`provenance: incident`) — tốt nhất**

```bash
git log --grep="hotfix\|revert\|urgent\|rollback\|INC-" -i \
        --pretty=format:"%H|%ad|%s" --date=short --since="2 years ago"
```

Với mỗi hotfix, một câu hỏi:

> **"Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?"**

Câu trả lời là invariant. Nó đáng tin nhất vì đã được thực tế kiểm chứng bằng
một sự cố thật.

Nếu có postmortem: mục "root cause" gần như luôn là một invariant bị vi phạm,
chỉ viết bằng ngôn ngữ khác.

**2. Từ test assertion (`provenance: human`)**

Test integration đang assert cái gì? Assertion là invariant mà ai đó đã nghĩ
tới. Trích ra, tổng quát hóa, xác nhận.

**3. Từ bộ mẫu chung (`provenance: human`)**

Với flow có `idempotent: false` hoặc `external_side_effects: true`, luôn kiểm
bốn invariant sau:

- [ ] Hai request đồng thời tới cùng resource không tạo double-effect
- [ ] Retry sau timeout không tạo double-effect
- [ ] Partial failure giữa các step để lại state hợp lệ hoặc được cleanup
- [ ] Timeout của external call không làm treo transaction đang mở

**4. LLM đề xuất (`provenance: llm_proposed`) — luôn `status: proposed`**

Chỉ ở đây LLM đọc code và đoán. Kết quả **không bao giờ** tự động thành
`confirmed`. Người phải đọc và quyết định.

### `status` — hàng rào chống garbage-in

| status | Ý nghĩa | Tính vào điểm rủi ro? |
|---|---|---|
| `confirmed` | Người đã đọc và đồng ý | **Có** |
| `proposed` | LLM đề xuất, chưa ai xác nhận | Không — chỉ advisory |
| `deprecated` | Không còn đúng, giữ lại làm sử liệu | Không |

Đây là hàng rào duy nhất ngăn hệ thống tự sinh tiêu chuẩn rồi tự đánh giá theo
tiêu chuẩn của chính nó. Đừng bỏ nó để tiết kiệm thời gian.

---

## 4. Sai lầm thường gặp

| Sai lầm | Vì sao sai | Sửa thế nào |
|---|---|---|
| Flow tương ứng 1-1 với class/service | Flow là đơn vị nghiệp vụ, không phải code | Gộp theo entry point + trace |
| Một siêu-flow chứa tất cả | Thường vì gom theo shared middleware/ORM base | Loại entity xuất hiện ở >60% trace khỏi phép similarity |
| Mỗi endpoint một flow | Mất ý nghĩa nghiệp vụ, manifest phình | Gộp endpoint dùng chung >40% entity |
| `executions_per_day: 0` khi không biết | Hệ thống coi là rủi ro thấp | Để trống |
| Invariant kiểu "phải xử lý lỗi tốt" | Không kiểm chứng được | Viết lại thành phát biểu test được |
| Entity dùng line range | Vỡ sau mỗi lần format | Dùng L1/L2 |
| Confirm hết invariant LLM đề xuất | Phá hàng rào chống garbage-in | Đọc từng cái, confirm có chọn lọc |
| Cố phủ hết repo ngay từ đầu | Không bao giờ xong, manifest rot trước khi dùng được | 3 flow critical trước |
| Gán severity theo độ phức tạp code | FRA quan tâm hậu quả, không phải độ khó | Dùng rubric ở §2 |

---

## 5. Vòng bảo trì

Manifest rot âm thầm là chế độ hỏng phổ biến nhất của loại hệ thống này. Code
đi tiếp, manifest ở lại.

### `fra doctor` chạy trên mọi PR

| Kiểm tra | Mức |
|---|---|
| Entity ref không resolve | **Lỗi — fail CI.** Đây là lỗi thật, không phải cảnh báo |
| `last_verified` > 180 ngày | Cảnh báo, gắn nhãn `stale` |
| Flow critical không có `alertable_metric` | Cảnh báo: canary sẽ mù |
| Invariant `proposed` > 90 ngày | Cảnh báo: cần người quyết định |
| File có traffic mà không thuộc flow nào | Cảnh báo: vùng tối của manifest |

Kiểm tra cuối quan trọng nhất về lâu dài: nó đo **độ phủ manifest trên code
thực sự chạy**, không phải trên toàn bộ repo. Code không ai gọi thì không cần
manifest.

### Sau mỗi sự cố — bắt buộc

1. Postmortem sinh ra ≥1 invariant mới, `provenance: incident`
2. Nếu sự cố ở flow chưa có trong manifest → thêm flow đó
3. Nếu FRA đã gắn cờ PR gây sự cố nhưng bị ghi đè → xem lại vì sao dev không tin
4. Nếu FRA **không** gắn cờ → tìm xem manifest thiếu entity nào

Bước 1 là cơ chế duy nhất giúp hệ thống học được thứ nó không thể tự suy ra.
Mô hình không thấy được invariant chưa ai viết ra, và đó là giới hạn thông tin,
không phải giới hạn kỹ thuật.

### Xác minh lại mỗi 6 tháng

```bash
# Chạy lại trace, đối chiếu với entities trong manifest
fra.py doctor
# Cập nhật last_verified sau khi đối chiếu xong
```

Cập nhật `maintenance.last_verified` và `verified_by`. Nếu không ai xác minh
được một flow trong 12 tháng, đó là dấu hiệu flow đó không còn ai sở hữu —
đáng bàn hơn là đáng bỏ qua.
