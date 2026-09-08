# flow-risk-assessment-claude-skill

Claude Skill dự đoán rủi ro sự cố production từ PR diff, dựa trên **Business
Flow Manifest**.

Không phụ thuộc ngôn ngữ hay hạ tầng — chỉ cần `git`, và tuỳ chọn tree-sitter
cùng một format coverage chuẩn.

## Vấn đề nó giải quyết

Các công cụ chấm điểm rủi ro theo file trả lời sai câu hỏi. Chúng ước lượng
"code này có khả năng chứa lỗi không", trong khi câu hỏi thật là "nếu có lỗi
thì nó có gây sự cố không, và hậu quả là gì".

Một fault trong nhánh chạy 2 lần/tháng và một fault trong luồng thanh toán chạy
10k lần/ngày cho ra hai xác suất sự cố cách nhau ba bậc độ lớn — dù điểm phức
tạp code giống nhau.

FRA phân rã theo chuỗi nhân quả (mô hình PIE, Voas 1992), và mỗi thừa số có
nguồn dữ liệu riêng:

```
P(sự cố) = P(defect) × P(reach) × P(infect) × P(propagate) × P(¬detect)
              ↑           ↑          ↑            ↑              ↑
        git history  access log  mutation   impact closure  diff coverage
```

## Ba phát hiện có giá trị nhất — không cần hiệu chỉnh

**Armed cut set.** PR có chạm ≥2 basic event trong cùng một minimal cut set?
Nếu có, các lớp phòng thủ vốn độc lập đang bị sửa cùng lúc — mô hình phô mai
Thụy Sĩ được hình thức hóa. Kết quả boolean, đúng ngay từ ngày đầu.

**Canary blindness.** Flow 20 lần/ngày với canary 30 phút @5% chỉ được ~0.02
lần thực thi. Canary sẽ xanh 100% và bạn tưởng đã kiểm chứng.

**Impact mapping.** Chỉ cần `git log`. Co-change coupling bắt được phụ thuộc mà
không static analysis nào thấy: template↔controller, migration↔model,
config↔code, IaC↔app. Đó thường chính là chỗ sự cố xảy ra.

## Cài đặt

**Là Claude Skill** — upload `flow-risk-assessment.skill`, hoặc copy thư mục
này vào nơi chứa skill của bạn.

**Là tooling trong repo:**

```bash
cp scripts/fra.py            <repo>/tools/fra.py
cp assets/CLAUDE.md.template <repo>/CLAUDE.md          # hoặc merge vào file có sẵn
mkdir -p <repo>/docs/fra
cp references/*.md           <repo>/docs/fra/
cp assets/DECISIONS-template.md <repo>/docs/fra/DECISIONS.md
cp assets/ci/github-actions.yml <repo>/.github/workflows/fra.yml
```

## Bắt đầu

```bash
pip install pyyaml

# 0. Kiểm tiền đề — LÀM TRƯỚC. Bốn nhánh quyết định phụ thuộc kết quả này.
git rev-parse --is-shallow-repository        # phải là false
git log --oneline | wc -l                    # cần ≥300
git log -n 100 --pretty=format:"%s" | grep -icE "^(fix|hotfix|revert|patch)"

# 1. Scaffold
python3 tools/fra.py init

# 2. Operational profile từ log thật
python3 tools/fra.py traffic --log access.log --format nginx --days 30

# 3. Viết manifest cho 3 flow critical — xem docs/fra/MANIFEST-GUIDE.md

# 4. Drift detection
python3 tools/fra.py doctor

# 5. KIỂM CHỨNG — cửa chặn thật
python3 tools/fra.py backtest --last 30
```

Bước 5 quan trọng nhất. Đối chiếu bằng tay với các sự cố đã biết. **Nếu FRA
không gắn cờ được những thay đổi đã thực sự gây sự cố, đừng bật lên** — sửa
manifest trước. Đây là bước kiểm chứng rẻ nhất và hay bị bỏ.

## Lộ trình

| Phase | Nội dung | Công sức | Giá trị |
|---|---|---|---|
| 1 | Impact mapping — flow nào bị chạm, canary có mù | 1–2 tuần | **60%** |
| 2 | Diff coverage, symbol-level, LLM invariant check | 2–3 tuần | +20% |
| 3 | Fault tree, minimal cut set, deploy strategy | 2–3 tuần | +10% |
| 4 | SZZ labeling, JIT model, Brier score | liên tục | +10% |

Phase 1 tự nó đã hữu ích. Nhiều team dừng ở đó và vẫn thu được phần lớn giá
trị — đó là kết cục hợp lý, không phải thất bại.

## Cấu trúc

```
flow-risk-assessment/
├── SKILL.md                      # entry point, 12 luật cứng
├── scripts/fra.py                # implementation Phase 1, chạy được ngay
├── references/
│   ├── SPEC.md                   # lý thuyết + công thức, 19 phần
│   ├── IMPLEMENTATION.md         # 25 task có acceptance criteria
│   └── MANIFEST-GUIDE.md         # hướng dẫn viết manifest (cho người)
├── assets/
│   ├── CLAUDE.md.template
│   ├── _TEMPLATE.flow.yaml
│   ├── DECISIONS-template.md
│   └── ci/{github-actions,gitlab-ci}.yml
└── evals/evals.json              # 4 test case, 16 assertion
```

## Giới hạn

**Mô hình không thấy được invariant chưa ai viết ra.** Giới hạn thông tin,
không phải giới hạn kỹ thuật — model phức tạp hơn không sửa được. Cách duy nhất
để cải thiện là viết thêm invariant, thường sau khi trả giá bằng một sự cố.

**Dự đoán mức từng dòng không khả thi.** Precision quá thấp. Đơn vị nhỏ nhất là
symbol.

**Ba phần tư giá trị nằm ở deploy strategy, không ở con số.** Nếu phải chọn giữa
"số chính xác hơn" và "định tuyến deploy tốt hơn", chọn cái sau.

## Nền tảng

| Chủ đề | Nguồn |
|---|---|
| PIE model | Voas, *PIE: A Dynamic Failure-Based Technique* (IEEE TSE 1992) |
| RIPR | Li & Offutt (2014) |
| JIT defect prediction | Kamei et al. (IEEE TSE 2013) |
| Change entropy | Hassan (ICSE 2009) |
| BugCache locality | Kim et al. (ICSE 2007) |
| SZZ | Śliwerski, Zimmermann, Zeller (MSR 2005) |
| FTA → Bayesian Network | Bobbio et al. (2001) |
| Beta-factor common cause | NUREG/CR-5485 |
| Operational profile | Musa (IEEE Software 1993) |
| Swiss cheese model | Reason, *Human Error* (1990) |

Danh sách đầy đủ ở `references/SPEC.md` §19.

## License

MIT — xem `LICENSE`.
