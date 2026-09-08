# FRA Runbook — áp dụng vào một repo mới

Hướng dẫn thực hành, làm theo đúng thứ tự. Khác với `IMPLEMENTATION.md` (kế
hoạch xây FRA), file này là quy trình **áp dụng FRA vào một repo cụ thể** lần
đầu.

Tổng thời gian: **6–10 ngày làm việc**, trong đó Bước 4 chiếm 60%.

---

## Tổng quan các bước

| Bước | Việc | Thời gian | Cửa chặn |
|---|---|---|---|
| 0 | Chọn repo + kiểm tiền đề | 30 phút | ✋ Có thể dừng ở đây |
| 1 | Tìm và parse access log | 2–3 giờ | |
| 2 | Chọn 3 flow critical | 1 giờ | |
| 3 | Viết khung manifest | 3–4 giờ | |
| 4 | **Khai thác invariant** | **4–8 ngày** | Không có đường tắt |
| 5 | `doctor` sạch | 30 phút | ✋ Phải 0 lỗi |
| 6 | **Backtest** | 1–2 giờ | ✋✋ **Cửa chặn thật** |
| 7 | CI comment-only | 1 giờ | |
| 8 | Quan sát 2–4 tuần | — | ✋ Override rate < 30% |

Ba dấu ✋ là chỗ được phép kết luận "repo này chưa phù hợp" và dừng lại. Dừng
sớm tốt hơn là làm tiếp với dữ liệu sai.

---

## Bước 0 — Chọn repo và kiểm tiền đề

### 0.1 Tiêu chí chọn repo thử nghiệm đầu tiên

Cần **cả bốn** điều kiện:

- [ ] ≥300 commit (để co-change coupling có ý nghĩa thống kê)
- [ ] Có access log, APM, hoặc cách nào đó biết tần suất thực tế
- [ ] Có ít nhất 3 luồng nghiệp vụ mà nếu hỏng thì bạn biết hậu quả là gì
- [ ] Bạn hoặc ai đó trong tay có thể trả lời "invariant của flow này là gì"

Điều kiện cuối quan trọng nhất và hay bị bỏ qua. Không có người biết nghiệp vụ,
Bước 4 sẽ bế tắc.

**Không** chọn repo vì nó phức tạp nhất hay nhiều bug nhất. Chọn repo mà **hậu
quả của lỗi rõ ràng nhất**.

### 0.2 Chạy kiểm tra

```bash
cd <repo>
echo "── shallow? (phải false) ──"; git rev-parse --is-shallow-repository
echo "── số commit ──"; git log --oneline | wc -l
echo "── % commit có tiền tố fix (trên 100 gần nhất) ──"
git log -n 100 --pretty=format:"%s" | grep -icE "^(fix|hotfix|bugfix|revert|patch)"
echo "── 15 subject gần nhất ──"; git log -n 15 --pretty=format:"%s"; echo
echo "── ngôn ngữ ──"
git ls-files | sed -n 's/.*\.\([a-z0-9]\{1,6\}\)$/\1/p' | sort | uniq -c | sort -rn | head -8
```

### 0.3 Bảng quyết định

| Kết quả | Hệ quả |
|---|---|
| `is-shallow` = **true** | ✋ **Dừng.** Clone lại full depth: `git clone <url>` (không `--depth`). Không có history thì Tier 0 và Bước 4 đều trả kết quả rỗng **mà không báo lỗi** |
| <300 commit | Bỏ Tier 0. Chạy `fra.py analyze --no-cochange`. Vẫn dùng được, kém một tầng |
| <10% commit có tiền tố fix | Bỏ nhánh D1 ở Bước 4. Lấy invariant từ postmortem, Slack, hoặc phỏng vấn người |
| Không có access log | Bỏ adapter accesslog. Điền `executions_per_day` tay, đánh dấu `source: manual` |

**Ghi kết quả vào `docs/fra/DECISIONS.md` ADR-001.** Bốn nhánh trên thay đổi cả
các bước sau, và 3 tháng nữa bạn sẽ không nhớ vì sao mình chọn thế.

---

## Bước 1 — Access log

### 1.1 Tìm log ở đâu

| Hạ tầng | Đường dẫn / cách lấy | Format |
|---|---|---|
| nginx (Debian/Ubuntu) | `/var/log/nginx/access.log*` | `nginx` |
| Apache | `/var/log/apache2/access.log*` | `apache` |
| Bitnami (Lightsail/EC2) | `/opt/bitnami/nginx/logs/access.log` hoặc `/opt/bitnami/apache/logs/access_log` | `nginx` / `apache` |
| CloudFront | Bật standard logging → S3, tải về, `gunzip` | `cloudfront` |
| Cloudflare | Logpush (cần Enterprise) hoặc Analytics API | `json` |
| ALB / ELB | S3 access log | `nginx` (gần đủ) |
| Laravel/Rails/Django | Application log nếu có log request | `json` |
| Docker | `docker logs <container>` | tuỳ app |
| Vercel / Netlify | Dashboard → Logs → export | `json` |

Nếu log đã rotate và nén:

```bash
zcat /var/log/nginx/access.log.*.gz > /tmp/access-all.log
cat /var/log/nginx/access.log >> /tmp/access-all.log
wc -l /tmp/access-all.log
```

**Lấy tối thiểu 7 ngày, tốt nhất 30 ngày.** Dưới 7 ngày thì traffic pattern
theo tuần bị mất — flow chỉ chạy thứ Hai sẽ bị đánh giá sai hoàn toàn.

### 1.2 Cài và chạy

```bash
pip install pyyaml

# Copy script từ skill đã cài
mkdir -p tools
cp ~/.claude/skills/flow-risk-assessment/scripts/fra.py tools/ 2>/dev/null \
  || cp "$(find ~ -name fra.py -path '*flow-risk-assessment*' 2>/dev/null | head -1)" tools/

python3 tools/fra.py init
python3 tools/fra.py traffic --log /tmp/access-all.log --format nginx --days 30
```

### 1.3 Kiểm chất lượng output

- [ ] Tỷ lệ dòng khớp **≥50%**. Thấp hơn → sai `--format`, thử format khác
- [ ] Path đã được gộp: `/orders/123` và `/orders/999` cùng thành `/orders/:id`
- [ ] Có ≥10 endpoint
- [ ] Top endpoint khớp với hiểu biết của bạn về hệ thống

Điều cuối là phép thử tỉnh táo. Nếu endpoint traffic cao nhất là thứ bạn không
ngờ tới, hoặc luồng bạn nghĩ là quan trọng nhất lại không xuất hiện — dừng lại
và tìm hiểu tại sao. Thường là do log thiếu một tầng (CDN cache khiến origin
không thấy traffic), và nếu bỏ qua thì mọi `P(R)` về sau đều sai.

### 1.4 Entry point không phải HTTP

Log không thấy cron, queue, event. Lấy riêng:

```bash
# Cron: đọc trực tiếp, chính xác tuyệt đối
crontab -l
cat /etc/cron.d/* 2>/dev/null
# Laravel
grep -rn "schedule(" app/Console/Kernel.php routes/console.php 2>/dev/null
# Chuyển cron expression sang số lần/ngày:
#   0 */6 * * *   → 4/ngày
#   */15 * * * *  → 96/ngày
#   0 3 * * *     → 1/ngày
#   0 3 * * 1     → 0.14/ngày  (tuần 1 lần)

# Queue consumer: lấy từ metric của broker
# SQS:      NumberOfMessagesReceived (CloudWatch)
# RabbitMQ: rabbitmqctl list_queues name messages_ready message_stats
# Redis:    độ dài queue theo thời gian
```

Cron là loại dễ nhất và hay bị bỏ sót nhất. Một job chạy 1 lần/ngày có
`P(R)` rất khác một endpoint 12k lần/ngày — và job cron thường là chỗ
reconciliation, tức là một lớp phòng thủ.

---

## Bước 2 — Chọn 3 flow critical

### 2.1 Tiêu chí, theo thứ tự ưu tiên

1. **Mất tiền** nếu sai
2. **Hỏng dữ liệu** không phục hồi được
3. **Rò rỉ dữ liệu** cá nhân
4. **Chặn nghiệp vụ chính**, cần can thiệp tay

Không chọn theo độ phức tạp code hay số bug lịch sử. FRA quan tâm **hậu quả**.

### 2.2 Cách chọn nhanh

Đặt câu hỏi này với từng ứng viên:

> *"Nếu flow này hỏng lúc 3 giờ sáng thứ Bảy và không ai biết trong 6 tiếng,
> chuyện gì xảy ra?"*

Câu trả lời "khách bị trừ tiền mà không nhận được hàng" → critical.
Câu trả lời "vài người phải F5 lại" → không phải flow cần chọn đầu tiên.

### 2.3 Prompt cho Claude Code

Nếu bạn không chắc repo có những flow nào:

> Đọc danh sách endpoint trong `.fra/cache/traffic.json` và cấu trúc code. Đề
> xuất 5 business flow ứng viên, mỗi cái kèm: tên nghiệp vụ, endpoint liên
> quan, file nào có thể đang thực thi nó, và hậu quả nếu hỏng. Đừng gán
> severity — tôi sẽ tự quyết.

Câu cuối quan trọng: severity là quyết định kinh doanh, và skill được thiết kế
để không tự gán.

- [ ] 3 flow được chọn, có lý do ghi lại
- [ ] Cả 3 xuất hiện trong top 50 endpoint theo traffic (nếu là HTTP)
- [ ] Ghi vào `DECISIONS.md` ADR-002, kèm các flow đã cân nhắc nhưng bỏ qua

---

## Bước 3 — Viết khung manifest

### 3.1 Copy template

```bash
SKILL=~/.claude/skills/flow-risk-assessment
for f in checkout gift_redemption user_registration; do   # đổi tên theo flow của bạn
  cp $SKILL/assets/_TEMPLATE.flow.yaml .fra/flows/$f.yaml
done
rm -f .fra/flows/example.yaml
ls .fra/flows/
```

### 3.2 Điền — thứ tự quan trọng

Điền theo thứ tự này, vì mỗi phần phụ thuộc phần trước:

**a. `entities` — mức file (L1) là đủ**

```yaml
entities:
  - "src/services/CheckoutService.*"     # wildcard ext: sống qua đổi ngôn ngữ
  - "src/controllers/Checkout*"
```

Không dùng line range. Vỡ sau mỗi lần format.

Cách tìm entities nhanh: từ endpoint, tra route definition, rồi lần theo
controller → service → repository. Hoặc dùng prompt:

> Endpoint `POST /api/checkout` được xử lý bởi những file nào? Lần theo từ
> route definition đến tận tầng database. Trả về danh sách đường dẫn.

**b. Bốn cờ khuếch đại — quan trọng hơn cả `severity.class`**

| Cờ | Câu hỏi kiểm tra |
|---|---|
| `idempotent` | Gọi lại đúng request đó 2 lần, kết quả có giống 1 lần? |
| `reversible` | Có nút undo, hoặc query SQL nào hoàn tác được? |
| `compensating_action` | Nếu sai, ai sửa và bằng cách nào? Script tự động = `automatic` |
| `external_side_effects` | Có gì rời khỏi hệ thống mà không lấy lại được? Email đã gửi thì không |

**c. `executions_per_day` — từ `.fra/cache/traffic.json`**

Không biết thì **để trống**, không điền `0`. `0` nghĩa là "đo được và không có
traffic" → hệ thống coi flow rủi ro thấp. Để trống nghĩa là "không biết" → hệ
thống bảo thủ.

**d. `severity.class`** — rubric cố định, bạn quyết định

**e. Invariant** — để Bước 4

### 3.3 Kiểm

```bash
python3 tools/fra.py doctor
```

Ở giai đoạn này sẽ còn cảnh báo về invariant (chưa có) — bình thường. Nhưng
**entity không resolve là lỗi**, phải sửa hết.

---

## Bước 4 — Khai thác invariant

**Đây là bước tốn 60% thời gian và không có đường tắt.** Cũng là bước tạo giá
trị lớn nhất — kể cả khi bạn dừng ở đây và không bao giờ chạy phần tính xác
suất, việc viết ra invariant của các flow critical đã tự nó đáng giá.

### 4.1 Nguồn D1 — lịch sử hotfix (tốt nhất)

```bash
git log --grep="hotfix\|revert\|urgent\|rollback\|INC-\|critical" -i \
        --pretty=format:"%H|%ad|%s" --date=short --since="2 years ago" | head -40
```

Với **từng** hotfix, đọc diff và đặt đúng một câu hỏi:

> **"Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?"**

Câu trả lời chính là một invariant. Nó đáng tin nhất vì đã được thực tế kiểm
chứng bằng một sự cố thật.

```bash
git show <sha> --stat        # xem chạm file nào
git show <sha>               # đọc diff
```

**Prompt cho Claude Code:**

> Đọc commit `<sha>`. Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?
> Phát biểu thành invariant có thể viết test được. Nếu không rút ra được
> invariant nào (ví dụ đây chỉ là refactor được gắn nhãn fix), nói thẳng.

Câu cuối cần thiết — nhiều commit gắn nhãn "fix" thật ra không phải fix bug.

### 4.2 Nguồn D2 — test assertion

Test integration đang assert cái gì? Assertion là invariant mà ai đó đã nghĩ
tới nhưng chưa viết ra thành phát biểu.

```bash
grep -rn "assert\|expect\|should" tests/ --include="*Test*" | head -40
```

### 4.3 Nguồn D4 — bộ mẫu bắt buộc

Với **mọi** flow có `idempotent: false` hoặc `external_side_effects: true`,
kiểm bốn invariant sau. Chúng là nguồn sự cố phổ biến nhất trong hệ phân tán:

- [ ] Hai request đồng thời tới cùng resource không tạo double-effect
- [ ] Retry sau timeout không tạo double-effect
- [ ] Partial failure giữa các step để lại state hợp lệ hoặc được cleanup
- [ ] Timeout của external call không làm treo transaction đang mở

### 4.4 Phép thử cho mỗi invariant

> **Có viết được một test fail khi invariant này bị vi phạm không?**

Nếu không → phát biểu lại cho cụ thể hơn.

```
✅ "Một mã chỉ redeem thành công đúng 1 lần, kể cả khi 2 request đồng thời"
✅ "mark_redeemed và issue_voucher phải cùng 1 transaction, hoặc có outbox"

❌ "Code phải xử lý lỗi tốt"           (không kiểm chứng được)
❌ "Nên validate input"                 (lời khuyên, không phải invariant)
❌ "Hàm charge() trả về boolean"        (signature, không phải invariant)
```

### 4.5 Quy tắc `status` — hàng rào chống garbage-in

| status | Tính vào điểm rủi ro? |
|---|---|
| `confirmed` — người đã đọc và đồng ý | **Có** |
| `proposed` — LLM đề xuất, chưa xác nhận | Không, chỉ advisory |
| `deprecated` — không còn đúng | Không |

**Không confirm hàng loạt.** Đọc từng cái. Đây là hàng rào duy nhất ngăn hệ
thống tự sinh tiêu chuẩn rồi tự đánh giá theo tiêu chuẩn của chính nó.

### 4.6 Mục tiêu

- [ ] Mỗi flow ≥3 invariant `confirmed`
- [ ] Ít nhất 1 invariant có `provenance: incident`
- [ ] Mọi invariant đều qua được phép thử ở 4.4

Nếu một flow không rút được invariant nào từ lịch sử: đó là tín hiệu đáng chú
ý. Hoặc flow đó thật sự ổn định, hoặc sự cố của nó chưa từng được ghi lại. Khả
năng thứ hai phổ biến hơn.

---

## Bước 5 — Doctor phải sạch

```bash
python3 tools/fra.py doctor
```

- [ ] **0 lỗi.** Entity không resolve là lỗi thật, không phải cảnh báo
- [ ] Cảnh báo còn lại được đọc và ghi nhận, không bỏ qua im lặng

Cảnh báo hay gặp và ý nghĩa:

| Cảnh báo | Nghĩa là |
|---|---|
| `thiếu operational.executions_per_day` | Không tính được canary blindness cho flow này |
| `flow critical không có alertable_metric` | Kể cả khi canary có tín hiệu, không có gì báo động |
| `chưa có invariant nào status=confirmed` | Bước 4 chưa xong cho flow này |

---

## Bước 6 — Backtest (cửa chặn thật)

**Bước quan trọng nhất của toàn runbook, và hay bị bỏ nhất.**

```bash
python3 tools/fra.py backtest --last 30
```

### 6.1 Đối chiếu bằng tay

Lấy danh sách ≥5 sự cố đã biết (từ hotfix, postmortem, hoặc ký ức của team),
rồi kiểm:

| Câu hỏi | Nếu sai thì làm gì |
|---|---|
| PR/commit đã gây sự cố có bị gắn cờ **flow đúng**? | Manifest thiếu entity. Mở rộng `entities` rồi chạy lại |
| Có gắn cờ **tràn lan** lên commit vô hại? | Closure quá rộng. Tăng `cochange.min_confidence` lên 0.4–0.5 |
| Có sự cố mà **không flow nào** bị chạm? | **Vùng tối của manifest** — phát hiện quan trọng nhất của bước này |

### 6.2 Quyết định

```
✋ Nếu FRA không gắn cờ được những thay đổi đã thực sự gây sự cố:
   KHÔNG bật lên. Quay lại Bước 3–4, mở rộng manifest.
```

Đây là bước kiểm chứng rẻ nhất bạn có. Bỏ nó nghĩa là bật một hệ thống mà bạn
không biết nó có hoạt động hay không — và tệ hơn, team sẽ tin nó.

- [ ] Ghi kết quả vào `DECISIONS.md` ADR-003
- [ ] Ghi rõ: có bật lên hay không, và vì sao

---

## Bước 7 — CI, chế độ comment-only

```bash
SKILL=~/.claude/skills/flow-risk-assessment
mkdir -p .github/workflows
cp $SKILL/assets/ci/github-actions.yml .github/workflows/fra.yml
# hoặc GitLab: cp $SKILL/assets/ci/gitlab-ci.yml → gộp vào .gitlab-ci.yml
```

Hai chi tiết gây **lỗi im lặng** nếu bỏ sót — cả hai đã có trong template,
đừng xoá:

- **`fetch-depth: 0`** — thiếu nó, co-change trả kết quả rỗng mà không báo lỗi
- **Cache co-change theo `base_ref`** — tính lại mỗi PR mất vài phút trên repo lớn

- [ ] CI chạy xanh trên 1 PR thật
- [ ] Comment xuất hiện đúng
- [ ] **Không** có step nào fail dựa trên risk band

Chưa gate. Gate chỉ bật sau Bước 8, và chỉ cho phát hiện cấu trúc.

---

## Bước 8 — Quan sát 2–4 tuần

### 8.1 Theo dõi

- [ ] Đếm số lần dev nói "cái này không liên quan" → đó là override rate ngầm
- [ ] Comment có được đọc không, hay bị bỏ qua
- [ ] Có phát hiện nào thực sự thay đổi quyết định của ai đó?

### 8.2 Quyết định bật gate

```
Override rate < 30%  → bật gate cho: armed cut set, security finding, drift
Override rate ≥ 30%  → mô hình là nhiễu. Thắt closure, đừng bật gate.
                       Sửa mô hình, đừng sửa dev.
```

Chỉ gate trên **phát hiện cấu trúc** (boolean, đúng bất kể hiệu chỉnh).
Không bao giờ gate trên risk band hay xác suất.

---

## Vấn đề thường gặp

| Triệu chứng | Nguyên nhân | Sửa |
|---|---|---|
| `analyze` không thấy flow nào | `entities` không khớp đường dẫn thật | Kiểm bằng `doctor`; dùng wildcard `*` |
| Mọi PR đều chạm mọi flow | Closure quá rộng, hoặc entities dùng glob quá lớn | Tăng `min_confidence`; thu hẹp glob |
| Co-change trả rỗng | Repo shallow, hoặc <300 commit | `git fetch --unshallow`, hoặc `--no-cochange` |
| Traffic parse <50% | Sai `--format` | Thử format khác; xem 3 dòng log đầu để nhận diện |
| Endpoint không gộp được | Path có ID dạng lạ | Thêm regex vào `NORMALIZERS` trong `fra.py` |
| `doctor` báo entity không resolve | File đã đổi tên/xoá | Cập nhật manifest — đây là drift thật, không phải lỗi tool |
| Canary luôn báo mù | Flow tần suất thấp | Đúng, không phải bug. Dùng feature flag |
| Coupling giả tràn lan | Có commit format toàn repo | Đã lọc >25 file; hạ `max_commit_files` nếu cần |

---

## Kết quả mong đợi sau khi xong

Bạn sẽ có, ngay cả khi không làm Phase 2–4:

1. **Manifest tường minh** cho 3 flow critical — tự nó là tài liệu nghiệp vụ mà
   trước đó chỉ nằm trong đầu người
2. **Cảnh báo trên PR** cho biết flow nào bị chạm, kèm severity và traffic thật
3. **Phát hiện canary mù** — biết trước những thay đổi mà canary sẽ không kiểm
   chứng được
4. **Danh sách invariant** rút từ sự cố thật, dùng được để viết test

Bạn sẽ **không** có (cần Phase 2–4):

- Xác suất có hiệu chỉnh
- Armed cut set (cần fault tree, Phase 3)
- Diff coverage và LLM invariant check (Phase 2)

Đó là kết cục hợp lý cho lần đầu, không phải thất bại. Phase 1 chiếm khoảng
60% giá trị của toàn hệ thống.

---

## Bảng prompt tra nhanh

Dán vào Claude Code trong repo đã cài skill:

| Cần gì | Prompt |
|---|---|
| Bắt đầu từ đầu | `Repo này chưa có FRA. Kiểm tiền đề rồi hướng dẫn tôi bootstrap.` |
| Tìm flow ứng viên | `Đọc .fra/cache/traffic.json và cấu trúc code. Đề xuất 5 business flow ứng viên kèm hậu quả nếu hỏng. Đừng gán severity.` |
| Tìm entities | `Endpoint POST /api/x được xử lý bởi file nào? Lần từ route đến tầng database.` |
| Rút invariant | `Đọc commit <sha>. Fix này thiết lập lại điều gì lẽ ra phải luôn đúng? Nếu không rút được, nói thẳng.` |
| Đánh giá PR | `PR này chạm business flow nào? Rủi ro cao hay thấp, thừa số nào chi phối?` |
| Sau sự cố | `Vừa có hotfix ở <sha>. Rút invariant và thêm vào manifest.` |
| Kiểm manifest | `Đọc .fra/flows/. Manifest có chỗ nào sai hoặc thiếu theo MANIFEST-GUIDE?` |
| Xây tiếp | `Làm task T2.1 trong references/IMPLEMENTATION.md.` |
