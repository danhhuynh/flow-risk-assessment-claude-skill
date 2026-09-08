# FRA — Kế hoạch triển khai theo task

Mỗi task có: mục tiêu, file liên quan, acceptance criteria, và cách verify.
Làm theo thứ tự. Không nhảy phase.

Luật cứng ở `CLAUDE.md` §3 thắng mọi hướng dẫn trong file này.

---

## Phase 0 — Kiểm tiền đề (30 phút)

### T0.1 · Kiểm repo có đủ dữ liệu

```bash
git rev-parse --is-shallow-repository        # PHẢI là false
git log --oneline | wc -l                    # cần ≥300 để co-change có nghĩa
git log -n 100 --pretty=format:"%s" | grep -icE "^(fix|hotfix|revert|patch)"
git ls-files | sed -n 's/.*\.\([a-z0-9]\{1,6\}\)$/\1/p' | sort | uniq -c | sort -rn | head
```

**Quyết định phái sinh:**

| Kết quả | Hệ quả |
|---|---|
| `is-shallow` = true | **Dừng.** Clone lại full depth. Co-change và SZZ sẽ trả rỗng mà không báo lỗi |
| <300 commit | Bỏ Tier 0 (co-change). Dùng Tier 1 + 2 |
| <10% commit có tiền tố fix | Bỏ Stage D1 (mine invariant từ history). Lấy invariant từ postmortem hoặc test assertion |
| Không có access log | Bỏ adapter accesslog. Điền `executions_per_day` tay cho 3 flow đầu, đánh dấu `source: manual` |

Ghi kết quả vào `docs/fra/DECISIONS.md`.

### T0.2 · Xác định nguồn coverage

```bash
ls -la | grep -iE "coverage|lcov|clover|cobertura|jacoco"
grep -rn "coverage" --include="*.json" --include="*.xml" --include="Makefile" -l . | head
```

Nếu chưa có coverage: **không phải blocker cho Phase 1**. Phase 2 mới cần.

---

## Phase 1 — Impact mapping

Mục tiêu: trả lời được *"PR này chạm flow nào, và canary có mù không"*.
Không có xác suất nào ở phase này.

### T1.1 · Scaffold

```bash
python3 fra.py init
```

- [ ] `.fra/config.yaml` tồn tại
- [ ] `.fra/flows/example.yaml` tồn tại
- [ ] `.fra/.gitignore` chứa `cache/`
- [ ] `.fra/cache/` được gitignore, `.fra/flows/` **được commit**

### T1.2 · Dựng operational profile

```bash
python3 fra.py traffic --log <path> --format nginx --days 30
```

Acceptance:
- [ ] Tỷ lệ dòng khớp ≥50% (nếu thấp hơn, sai `--format`)
- [ ] Path normalization gộp đúng: `/orders/123` và `/orders/999` → `/orders/:id`
- [ ] Output có ≥10 endpoint

**Endpoint không phải HTTP** (cron, queue, event) không có trong log. Lấy riêng:

| Loại | Nguồn |
|---|---|
| Cron | Đọc cron expression → tính số lần/ngày. Chính xác tuyệt đối |
| Queue | Metric `messages_processed` từ broker |
| Event | Đếm từ event store |
| CLI | Điền tay, thường rất thấp |

### T1.3 · Chọn 3 flow đầu tiên

**Tiêu chí chọn** — theo thứ tự ưu tiên:

1. Mất tiền nếu sai
2. Hỏng dữ liệu không phục hồi được
3. Rò rỉ dữ liệu cá nhân
4. Chặn nghiệp vụ chính, cần can thiệp tay

**Không** chọn theo "code phức tạp nhất" hay "file bị sửa nhiều nhất". Đó là
tiêu chí của defect prediction, không phải của FRA.

- [ ] 3 flow được chọn, có lý do ghi lại
- [ ] Cả 3 đều xuất hiện trong top 50 endpoint theo traffic (nếu là HTTP flow)

### T1.4 · Viết manifest

Theo `references/MANIFEST-GUIDE.md`. Mức file (L1) là đủ cho Phase 1.

- [ ] 3 file trong `.fra/flows/`, không còn `example.yaml`
- [ ] Mỗi flow có đủ 4 cờ khuếch đại: `idempotent`, `reversible`,
      `compensating_action`, `external_side_effects`
- [ ] Mỗi flow có `executions_per_day` **lấy từ log**, không đoán
- [ ] Mỗi flow có ≥1 invariant `status: confirmed`

### T1.5 · Mine invariant từ lịch sử sự cố

Nguồn tốt nhất, và hay bị bỏ qua nhất.

```bash
git log --grep="hotfix\|revert\|urgent\|rollback\|INC-" -i \
        --pretty=format:"%H|%ad|%s" --date=short --since="2 years ago" | head -30
```

Với mỗi hotfix, câu hỏi duy nhất:

> **"Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?"**

Câu trả lời chính là một invariant. Gán `provenance: incident`, kèm
`incident_ref`.

- [ ] Đọc ≥6 hotfix/postmortem gần nhất
- [ ] Mỗi cái sinh ra ≥1 invariant, hoặc ghi rõ lý do không sinh được
- [ ] Invariant từ incident được đánh dấu `provenance: incident`

Nếu postmortem có sẵn: mục "root cause" gần như luôn là một invariant bị vi
phạm, viết bằng ngôn ngữ khác.

### T1.6 · Doctor phải sạch

```bash
python3 fra.py doctor
```

- [ ] 0 lỗi (entity không resolve = lỗi thật, không phải cảnh báo)
- [ ] Cảnh báo còn lại được ghi nhận, không phải bỏ qua im lặng

### T1.7 · Backtest — cửa chặn thật của Phase 1

```bash
python3 fra.py backtest --last 30
```

Đối chiếu bằng tay với danh sách sự cố đã biết:

| Câu hỏi | Nếu trả lời sai thì làm gì |
|---|---|
| Những PR đã gây sự cố có bị gắn cờ flow đúng? | Manifest thiếu entity. Mở rộng `entities` |
| Có gắn cờ tràn lan lên PR vô hại? | Closure quá rộng. Thắt `min_confidence` co-change lên 0.4–0.5 |
| Có PR gây sự cố mà không flow nào bị chạm? | **Vùng tối của manifest.** Đây là phát hiện quan trọng nhất của bước này |

- [ ] Chạy trên ≥30 commit/PR
- [ ] Đối chiếu với ≥5 sự cố đã biết
- [ ] Ghi kết quả vào `docs/fra/DECISIONS.md`

**Nếu FRA không gắn cờ được những thay đổi đã thực sự gây sự cố, KHÔNG bật
lên.** Sửa manifest trước. Đây là bước kiểm chứng rẻ nhất và hay bị bỏ.

### T1.8 · CI hook, chế độ comment-only

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
          fetch-depth: 0          # BẮT BUỘC — thiếu là lỗi im lặng

      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }

      - run: pip install pyyaml

      - name: Validate manifest
        run: python3 fra.py doctor

      - uses: actions/cache@v4
        with:
          path: .fra/cache
          key: fra-cochange-${{ github.base_ref }}-${{ github.run_number }}
          restore-keys: fra-cochange-${{ github.base_ref }}-

      - name: Analyze
        run: |
          python3 fra.py analyze \
            --base "${{ github.event.pull_request.base.sha }}" \
            --head "${{ github.event.pull_request.head.sha }}" \
            --json fra-report.json | tee fra-comment.txt

      - uses: actions/upload-artifact@v4
        with: { name: fra-report, path: fra-report.json }

      # KHÔNG gate ở giai đoạn này. Chỉ comment.
      - name: Comment on PR
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');
            const body = '```\n' + fs.readFileSync('fra-comment.txt','utf8') + '\n```';
            await github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body,
            });
```

Hai chi tiết gây lỗi im lặng nếu bỏ sót:

- **`fetch-depth: 0`** — thiếu nó thì co-change và SZZ trả về kết quả rỗng mà
  không báo lỗi
- **Cache co-change theo `base_ref`** — tính lại mỗi PR mất vài phút trên repo lớn

- [ ] CI chạy xanh trên 1 PR thật
- [ ] Comment xuất hiện đúng
- [ ] **Không** có step nào fail dựa trên risk band

### T1.9 · Vận hành thử 2–4 tuần

- [ ] Chạy ở chế độ comment-only, không gate
- [ ] Thu phản hồi: comment có hữu ích không, có gây nhiễu không
- [ ] Đếm số lần dev nói "cái này không liên quan" → đó là override rate ngầm
- [ ] Nếu >30% comment bị coi là nhiễu: thắt closure, đừng bật gate

---

## Phase 2 — Detection & invariant

### T2.1 · Symbol index bằng tree-sitter

File: `fra/symbols/treesitter.py`, `fra/symbols/index.py`

```bash
pip install tree-sitter tree-sitter-languages
```

Query per language nằm trong `SPEC.md` §6.2. Capture name thống nhất (`@fn`,
`@cls`) nên phần còn lại của code dùng chung.

Acceptance:
- [ ] Trích được symbol cho ngôn ngữ chính của repo
- [ ] Method được qualify bằng class bao ngoài: `PaymentService.charge`
- [ ] Cache invalidate theo `git sha` + file mtime
- [ ] Fallback `universal-ctags` khi không có grammar
- [ ] Fallback cuối: hoạt động ở L1, **không sập**

### T2.2 · Change unit ở mức L2

File: `fra/diff/parser.py`

- [ ] `git diff --unified=0` cho ra đúng dải dòng, không lẫn context
- [ ] Rename được xử lý bằng `-M -C`, không thành xóa+thêm
- [ ] Mỗi hunk gộp vào **innermost enclosing symbol**
- [ ] **Hunk mồ côi được xử lý**, không bỏ qua (xem dưới)

**Hunk mồ côi** = thay đổi ngoài mọi symbol: import, decorator, hằng số
top-level, khai báo route/DI. Chúng có **LOC thấp nhất và blast radius lớn
nhất** — bỏ qua chúng là sai lầm phổ biến nhất khi tự dựng loại tool này.

| Nội dung hunk mồ côi | change_kind |
|---|---|
| Đổi import | `signature_change` mức module |
| Hằng số top-level | `config_constant` |
| Khai báo route / DI binding | `signature_change` mức module |
| Thêm decorator (`@transactional`, `@cached`, middleware) | `control_flow` mức module |

Dòng cuối đáng chú ý: đây là loại thay đổi một dòng gây sự cố nhiều nhất trong
thực tế.

### T2.3 · Change kind classifier

File: `fra/diff/classifier.py`

11 loại + `unknown`, bảng đầy đủ trong `SPEC.md` §7.2. Phân loại bằng **so
sánh cấu trúc AST trước/sau**, không bằng đếm dòng.

- [ ] Test cho từng loại
- [ ] `test_only` → loại khỏi scoring nhưng tính vào `P(¬detect)`
- [ ] `dependency_bump` và `schema_migration` → **route sang checklist riêng**,
      không vào chuỗi PIE (luật cứng §3.11)

### T2.4 · Coverage adapter

File: `fra/adapters/coverage/`

- [ ] ≥2 format: chọn theo stack (lcov / cobertura / clover / jacoco /
      gocover / coveragepy)
- [ ] Trả `None` khi không có dữ liệu cho file đó, **không phải `0.0`**
- [ ] `diff_coverage()` chỉ tính dòng có executable statement

### T2.5 · Canary blindness → deploy strategy

Đã có phép tính ở Phase 1. Phase 2 nối nó vào quyết định.

Khi canary mù, output phải đề xuất thay thế cụ thể:

| Điều kiện | Đề xuất |
|---|---|
| Traffic thấp, flow critical | Feature flag + verify tay N giao dịch đầu |
| Có thể replay request | Shadow traffic, so sánh output, không commit side effect |
| Flow có thể bơm test data | Synthetic probe đủ số lần để có tín hiệu |
| Chấp nhận chờ | Kéo cửa sổ canary tới `min_exec / (rate × share)` |

- [ ] In ra thời lượng canary cần thiết, tính ngược từ traffic
- [ ] Đề xuất chiến lược thay thế khi thời lượng đó không thực tế

### T2.6 · LLM invariant check

File: `fra/llm/client.py`, `fra/llm/prompts/`, `fra/llm/guards.py`

Prompt đầy đủ ở `SPEC.md` §13.3. Nhắc lại các ràng buộc bắt buộc:

- [ ] Chỉ gửi invariant `status: confirmed` (luật cứng §3.2)
- [ ] `guards.py` reject response chứa số phần trăm / điểm số (luật cứng §3.1)
- [ ] Bắt buộc `evidence` có `file` + `line` thật, verify được trong context
- [ ] `verdict` cho phép `unaffected` và `cannot_determine`
- [ ] `proposed_new_invariants` ghi với `status: proposed`, **không tính điểm**
- [ ] `prompt_version` ghi vào output JSON

**Trường `challenge` phải được parse và in ra.** Nó là cơ chế duy nhất khiến
LLM báo lại rằng impact mapping đã sai — và mapping sai là chế độ hỏng phổ biến
nhất của pipeline, vì Tier 0 và Tier 1 đều overapproximate.

---

## Phase 3 — Fault tree

Chỉ dựng cho flow `severity.class: critical`. Với flow khác, chi phí bảo trì
vượt giá trị.

### T3.1 · Dựng cây từ postmortem

File: `.fra/trees/<flow_id>.yaml`, format ở `SPEC.md` §11.1

Cách dựng: bắt đầu từ **top event diễn đạt bằng ngôn ngữ nghiệp vụ**, không
phải ngôn ngữ code.

```
✅ "Khách bị trừ tiền nhưng không nhận được voucher"
❌ "RedemptionService.commit() throws exception"
```

Rồi hỏi ngược: *"Điều gì phải cùng xảy ra để chuyện này thành hiện thực?"*
AND gate cho điều kiện đồng thời, OR gate cho nguyên nhân độc lập.

- [ ] Cây cho ≥3 flow critical
- [ ] Mỗi basic event có `entities` resolve được (`fra doctor` kiểm)
- [ ] Mỗi basic event có `prior` (giá trị khởi đầu, sẽ hiệu chỉnh sau)
- [ ] Top event viết bằng ngôn ngữ nghiệp vụ

### T3.2 · Minimal cut set

File: `fra/tree/cutsets.py`, thuật toán ở `SPEC.md` §11.2

- [ ] Khai triển sum-of-products đúng (OR = hợp, AND = tích Descartes)
- [ ] `minimalize()` bỏ mọi superset
- [ ] Test với cây OR lồng AND lồng OR
- [ ] Test với cut set kích thước 1 (single point of failure)

### T3.3 · Armed cut set detection — output giá trị nhất

File: `fra/tree/armed.py`

> PR này có chạm **≥2 basic event trong cùng một minimal cut set** không?

Nếu có, các lớp phòng thủ vốn độc lập bị chọc **cùng lúc**. Đây là mô hình phô
mai Thụy Sĩ được hình thức hóa.

**Kết quả này là boolean, không phải xác suất** — nên nó đúng bất kể mô hình đã
hiệu chỉnh hay chưa. Đó là lý do Phase 3 nên làm trước Phase 4.

- [ ] Gate `hard_block` khi armed
- [ ] Gate `high` khi chạm cut set kích thước 1
- [ ] Khuyến nghị hành động: **tách PR, deploy từng lớp riêng, có khoảng nghỉ**

Khuyến nghị cuối quan trọng: tách deploy giữ lại tính độc lập của các lớp phòng
thủ **theo thời gian**, dù code cuối cùng vẫn như nhau.

### T3.4 · Bảng deploy strategy

File: `fra/report/decision.py`, bảng ở `SPEC.md` §14.2

- [ ] Mọi nhánh trong bảng được cover
- [ ] `rollback_trigger` cụ thể (metric + ngưỡng + thời gian), không chung chung
- [ ] `dependency_bump` / `schema_migration` route sang checklist riêng

---

## Phase 4 — Calibration

### T4.1 · SZZ labeling

File: `fra/calibration/szz.py`, chi tiết ở `SPEC.md` §15.1

**Bốn bộ lọc là bắt buộc.** Không có chúng thì nhãn nhiễu tới mức vô dụng:

- [ ] Bỏ commit fix chạm >20 file
- [ ] Bỏ dòng bị xóa chỉ là comment / whitespace / import
- [ ] Bỏ commit gây lỗi cách commit fix >18 tháng
- [ ] Bỏ dòng blame về commit chỉ đổi format (`git log --follow -w`)

### T4.2 · Nhãn từ incident

SZZ có nhiễu; incident thì không.

```
label(PR) = 1 nếu trong 14 ngày sau khi PR được deploy:
              - có hotfix/revert chạm entity mà PR đã chạm, HOẶC
              - có incident/Sentry issue mới quy được về release chứa PR
            0 nếu ngược lại
```

- [ ] Nối `deploy → release tag → incident window`
- [ ] Nhãn từ incident có **trọng số mẫu cao hơn** nhãn từ SZZ

### T4.3 · JIT model

File: `fra/calibration/features.py`, `train.py`

10 đặc trưng, bảng ở `SPEC.md` §15.2. Tất cả language-agnostic.

- [ ] Logistic regression, **không** dùng model phức tạp hơn
- [ ] `CalibratedClassifierCV(method="isotonic")` — bắt buộc, LogisticRegression
      thô cho xác suất lệch trên dữ liệu mất cân bằng
- [ ] Hệ số được in ra trong PR comment (để giải thích được)

Lý do ưu tiên logistic: hệ số đọc được, dễ giải thích, ít overfit trên dữ liệu
nhỏ. Đây là ưu tiên có chủ đích, không phải giới hạn kỹ thuật.

### T4.4 · Đánh giá — không dùng accuracy

File: `fra/calibration/evaluate.py`

Accuracy vô nghĩa: nếu 85% PR không gây sự cố, model đoán "không sự cố" mọi lần
đã đạt 85%.

| Chỉ số | Ngưỡng dùng được |
|---|---|
| Brier score | < Brier của base rate không đổi |
| Reliability diagram | Nằm gần đường chéo |
| precision@10 | > 0.30 |
| Lift decile đầu | > 2.0× |
| **Override rate** | **< 0.30** |

- [ ] Cả 5 chỉ số được tính và báo cáo hàng tuần
- [ ] Reliability diagram công khai cho team xem

`override_rate` là chỉ số duy nhất không thuộc thống kê và là chỉ số quan trọng
nhất. **Nếu > 0.30, sửa mô hình, đừng sửa dev.**

### T4.5 · Bật calibrated mode

- [ ] ≥200 nhãn
- [ ] Brier tốt hơn base rate
- [ ] precision@10 > 0.30
- [ ] Ngưỡng dải đặt ở **phân vị** của phân bố 3 tháng, không phải số tuyệt đối
- [ ] Output có khoảng tin cậy 90%

Cách trình bày (luật cứng §3.7):

```
❌ "PR này có 3.7% xác suất gây sự cố"

✅ "PR tương đương trong repo này bị hotfix 12% trong 14 ngày (n=340).
    PR này ở phân vị 88 — cao hơn nền, chủ yếu vì chạm flow critical
    không idempotent với diff coverage 31% và canary không cho tín hiệu
    ở tần suất này."
```

Câu thứ hai dài hơn, nhưng nói rõ: base rate, cỡ mẫu, vị trí tương đối, và
**thừa số chi phối**. Người đọc kiểm chứng và phản biện được.

---

## Bảo trì định kỳ

| Việc | Tần suất | Lệnh / cách làm |
|---|---|---|
| Refresh co-change cache | Tuần | `fra analyze --refresh-cochange` |
| Refresh operational profile | Tuần | `fra traffic --log ... --days 30` |
| Review invariant `proposed` >90 ngày | Tháng | `fra doctor` cảnh báo |
| Xác minh lại manifest (`last_verified`) | 6 tháng | Chạy lại trace, đối chiếu entities |
| Báo cáo override rate | Tuần | `fra evaluate --metric override_rate` |
| Postmortem → invariant mới | Mỗi sự cố | **Bắt buộc.** Mỗi postmortem ≥1 invariant |
| Đối chiếu dự đoán với sự cố thật | Tháng | `fra evaluate --window 30d` |

Dòng "postmortem → invariant" là dòng quan trọng nhất trong bảng. Nó là cơ chế
duy nhất giúp hệ thống học được thứ mà nó không thể tự suy ra.
