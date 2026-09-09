---
name: fra-bootstrap
description: Bootstrap Flow Risk Assessment cho một repo từ đầu đến khi bật được CI. Chạy trọn quy trình có cửa chặn — kiểm tiền đề repo, đọc traffic thật từ AWS CloudWatch, đề xuất và dựng Business Flow Manifest, khai thác invariant từ lịch sử hotfix, rồi kiểm chứng bằng holdout trước khi cho phép bật CI. Dùng khi người dùng gọi /fra-bootstrap, hoặc nói muốn bootstrap FRA, thiết lập đánh giá rủi ro PR cho repo, tạo business flow manifest từ đầu, hoặc áp dụng flow risk assessment vào một repo mới. Also use for English phrasings such as bootstrap flow risk assessment, set up PR risk assessment for this repo, create business flow manifest end to end.
allowed-tools: Bash(python3 tools/fra.py:*), Bash(aws sts get-caller-identity:*), Bash(aws logs describe-log-groups:*), Bash(aws configure list-profiles:*), Bash(git log:*), Bash(git show:*), Bash(git ls-files:*), Bash(git rev-parse:*), Bash(cp:*), Bash(mkdir:*), Bash(ls:*), Bash(find:*), Bash(pip install pyyaml:*), Read, Write, Edit, Glob, Grep
---

# Bootstrap FRA cho repo này

Chạy 7 giai đoạn. **Mỗi ✋ là cửa chặn — dừng lại, báo người dùng, chờ họ quyết
định. Không tự vượt qua.**

Nếu `$ARGUMENTS` có tên CloudWatch log group, dùng luôn và bỏ qua G2.2.

Báo tiến độ ngắn gọn sau mỗi giai đoạn. Không in lại toàn bộ output của lệnh.

---

## G1 — Tiền đề ✋

```bash
mkdir -p tools
cp ~/.claude/skills/flow-risk-assessment/scripts/fra.py tools/ 2>/dev/null \
  || cp "$(find ~ -name fra.py -path '*flow-risk-assessment*' | head -1)" tools/
python3 -c "import yaml" 2>/dev/null || pip install pyyaml
git rev-parse --is-shallow-repository
git log --oneline | wc -l
git ls-files | sed -n 's/.*\.\([a-z0-9]\{1,6\}\)$/\1/p' | sort | uniq -c | sort -rn | head -6
```

| Kết quả | Hành động |
|---|---|
| shallow = `true` | ✋ **Dừng.** Yêu cầu `git fetch --unshallow`. Không có history thì co-change và toàn bộ G4 trả rỗng **mà không báo lỗi** |
| <300 commit | Ghi nhận: sẽ dùng `--no-cochange`. Tiếp tục |
| ≥300 commit, không shallow | Tiếp G2 |

```bash
python3 tools/fra.py init
```

---

## G2 — Traffic thật

### G2.1 Kiểm SSO ✋

```bash
python3 tools/fra.py aws-check
```

| Output | Hành động |
|---|---|
| "Chưa cài AWS CLI" | ✋ Đưa lệnh cài, chờ người dùng |
| "Chưa đăng nhập AWS" | ✋ Nhắc `aws sso login --profile <p>` rồi `export AWS_PROFILE=<p>`. Gợi ý `aws configure list-profiles` nếu họ không biết tên. **Không tự chạy** — lệnh này mở browser, cần họ tương tác |
| "✓ Đã đăng nhập" + danh sách group | Tiếp G2.2 |

### G2.2 Chọn log group

Chọn group chứa **HTTP access log** của repo này. Tên thường có `nginx`,
`apache`, `access`, `alb`, `cloudfront`, hoặc tên service. Group lớn nhất
thường là access log.

Tránh `/aws/lambda/...` trừ khi repo này thật sự là Lambda — đó là execution
log, không phải access log.

Nhiều group trông hợp lý → **hỏi người dùng**, đừng đoán. Chọn sai làm mọi số
sau này vô nghĩa.

### G2.3 Lấy traffic

```bash
python3 tools/fra.py cloudwatch --group <group> --days 30
```

Kiểm ba điều trước khi đi tiếp:

- Tỷ lệ record khớp **≥50%**. Thấp hơn → sai group, quay lại G2.2
- Path đã gộp: `/orders/123` và `/orders/999` → `/orders/:id`
- **Top endpoint có khớp hiểu biết về hệ thống không?**

Điều thứ ba là phép thử tỉnh táo. Endpoint traffic cao nhất bất ngờ, hoặc luồng
quan trọng nhất không xuất hiện → **nói ra và hỏi người dùng**. Thường do CDN
cache khiến origin không thấy traffic; bỏ qua thì mọi `P(R)` sau này đều sai.

### G2.4 Entry point không phải HTTP

Không có trong access log. Tìm riêng — chúng thường là nơi đặt reconciliation,
tức là **một lớp phòng thủ**:

```bash
git ls-files | grep -iE 'cron|schedul|job|worker|consumer|batch|command' | head -25
```

Cron: đọc cron expression → số lần/ngày, chính xác tuyệt đối.
`0 */6 * * *` = 4/ngày · `*/15 * * * *` = 96/ngày · `0 3 * * 1` = 0.14/ngày

---

## G3 — Đề xuất flow ✋

Đọc `.fra/cache/traffic.json` và cấu trúc code. Với mỗi ứng viên, lần từ
endpoint → route → controller → service → tầng database để tìm `entities`.

Tiêu chí, theo thứ tự: **mất tiền** → **hỏng dữ liệu không phục hồi** → **rò rỉ
dữ liệu** → **chặn nghiệp vụ chính**.

Không chọn theo độ phức tạp code hay số bug. Tiêu chí là **hậu quả**.

Phép thử nhanh:

> *"Nếu flow này hỏng lúc 3 giờ sáng thứ Bảy và không ai biết trong 6 tiếng,
> chuyện gì xảy ra?"*

**Trước khi đề xuất, quét lịch sử hotfix để tìm vùng bị bỏ sót:**

```bash
python3 tools/fra.py hotfix-map --limit 40
```

Chạy được kể cả khi chưa có flow nào — nó liệt kê mọi hotfix và cho thấy vùng
nào chưa được phủ. Nếu một vùng có nhiều hotfix mà không nằm trong danh sách
ứng viên của bạn, **thêm nó vào**: cả hậu quả lẫn lịch sử đều cảnh báo.

Trình bày bảng rồi ✋ **chờ người dùng xác nhận**:

| Flow | Endpoint | exec/ngày | Entities | Hậu quả nếu hỏng | Hotfix lịch sử |
|---|---|---|---|---|---|

---

## G4 — Dựng manifest

Với mỗi flow đã được xác nhận:

```bash
cp ~/.claude/skills/flow-risk-assessment/assets/_TEMPLATE.flow.yaml \
   .fra/flows/<flow_id>.yaml
rm -f .fra/flows/example.yaml
```

### G4.1 Điền phần cơ học

`id` · `name` · `entities` · `executions_per_day` · `entry_points`

**`entities` dùng mức file, không glob rộng.** Glob như `gateway/**` hoặc
`src/**` khiến mọi PR chạm flow đó và phá hết tín hiệu. `doctor` sẽ cảnh báo
nếu một entity khớp >150 file hoặc >10% repo — sửa ngay khi thấy.

Wildcard extension thì tốt: `src/services/Checkout.*` sống qua refactor.
Line range (`#L120-L145`) thì không bao giờ — vỡ sau mỗi lần format.

**`executions_per_day` lấy từ traffic.json.** Không biết → **để trống**, không
điền `0`. `0` = "đo được, không có traffic" → hệ thống coi rủi ro thấp.
Để trống = "không biết" → hệ thống bảo thủ.

### G4.2 Severity ✋

**Không tự gán.** Hỏi người dùng, đưa rubric:

- `critical` — mất tiền, hỏng dữ liệu không phục hồi, rò rỉ, vi phạm quy định
- `high` — chặn nghiệp vụ chính, sửa tay
- `medium` — có workaround, tự phục hồi
- `low` — nội bộ hoặc thẩm mỹ

Bốn cờ khuếch đại: có thể **đề xuất** dựa trên code, nhưng phải để họ xác nhận.

| Cờ | Câu hỏi |
|---|---|
| `idempotent` | Gọi lại đúng request đó 2 lần, kết quả giống 1 lần? |
| `reversible` | Có undo, hoặc query nào hoàn tác được? |
| `compensating_action` | Nếu sai, ai sửa và bằng cách nào? |
| `external_side_effects` | Có gì rời hệ thống mà không lấy lại được? |

**Nếu flow A có `compensating_action: automatic` và cơ chế bù đó chính là flow B
trong manifest, nói ra.** Kiểm ngay: B có `compensating_action` gì? Nếu là
`none` thì B là **cut set kích thước 1** — nó hỏng thì A mất lớp phòng thủ mà
không ai biết. Đây là phát hiện có giá trị cao nhất ở giai đoạn này.

### G4.3 Invariant từ lịch sử ✋

Nguồn tốt nhất. Với **từng** flow, tìm riêng chứ không dùng một cửa sổ chung:

```bash
git log --grep="hotfix\|revert\|urgent\|^fix" -i --no-merges \
        --pretty=format:"%h|%ad|%s" --date=short -- <đường dẫn của flow>
```

**Đừng giới hạn ở 30 commit gần nhất.** Bằng chứng thường nằm sâu hơn — một
flow ít bị sửa sẽ không xuất hiện trong cửa sổ hẹp, và kết luận "không có bằng
chứng" sẽ sai.

Với mỗi hotfix, một câu hỏi:

> **"Fix này thiết lập lại điều gì mà lẽ ra phải luôn đúng?"**

Phép thử cho mỗi invariant: **viết được test fail khi nó bị vi phạm không?**
Không → phát biểu lại cụ thể hơn.

Schema đúng — sai tên field thì invariant **không được tính mà không báo lỗi**:

```yaml
- id: R1
  statement: "phát biểu cụ thể, test được"
  provenance: incident        # incident | human | llm_proposed
  incident_ref: "abc1234"     # bắt buộc khi provenance=incident
  status: proposed            # ⚠ LUÔN 'proposed' — chờ người confirm
```

Không dùng `source`, `desc`, `description`. `doctor` sẽ bắt.

✋ **Ghi `status: proposed` cho mọi invariant bạn viết.** Chỉ người dùng được
đổi sang `confirmed`. Trình bày danh sách và hỏi họ confirm từng cái.

### G4.4 Doctor phải sạch ✋

```bash
python3 tools/fra.py doctor
```

**Phải 0 lỗi.** Ba loại lỗi và cách sửa:

| Lỗi | Sửa |
|---|---|
| entity không resolve | Đường dẫn sai — sửa manifest |
| invariant field không hợp lệ | Đổi sang `provenance`/`incident_ref`/`status` |
| invariant thiếu field bắt buộc | Bổ sung `statement`, `provenance`, `status` |

Cảnh báo glob quá rộng: **sửa ngay**, đừng để lại. Nó là nguyên nhân số một của
việc mọi PR chạm mọi flow.

---

## G5 — Kiểm chứng ✋✋ (cửa chặn quan trọng nhất)

```bash
python3 tools/fra.py validate --split-date <ngày>
```

Chọn `--split-date` sao cho **holdout có ≥5 hotfix**. Thường 3–6 tháng trước.
Nếu holdout rỗng, thử ngày sớm hơn.

Vì sao cần holdout: manifest ở G3–G4 được xây **sau khi** đã xem hotfix gần
đây, nên recall trên cửa sổ đó bị nhiễu — bạn đã tuning theo chính tập test.
Chỉ recall trên holdout là bằng chứng thật.

| Kết quả | Hành động |
|---|---|
| Holdout recall ≥60% | ✓ Đạt. Tiếp G6 |
| Holdout recall <60% | ✋ **Dừng.** Xem danh sách "KHÔNG bắt được", bổ sung entities hoặc thêm flow, chạy lại |
| Holdout rỗng | ✋ Thử `--split-date` sớm hơn. Nếu repo quá mới, báo người dùng rằng **chưa kiểm chứng được** và đừng bật CI |

Cũng kiểm `backtest` để thấy tỷ lệ gắn cờ:

```bash
python3 tools/fra.py backtest --last 30
```

Flow nào bị gắn cờ >40% → entities quá rộng, quay lại G4.1.
Tổng gắn cờ >50% → closure quá rộng, tăng `cochange.min_confidence` lên 0.4–0.5.

**Đừng bật CI khi G5 chưa đạt.** Team sẽ tin một hệ thống chưa kiểm chứng, và
đó tệ hơn không có gì.

---

## G6 — CI comment-only

```bash
mkdir -p .github/workflows
cp ~/.claude/skills/flow-risk-assessment/assets/ci/github-actions.yml \
   .github/workflows/fra.yml
```

GitLab thì dùng `assets/ci/gitlab-ci.yml`.

Hai dòng **không được xoá**, cả hai gây lỗi im lặng nếu thiếu:

- `fetch-depth: 0` — thiếu thì co-change trả rỗng mà không báo lỗi
- cache co-change theo `base_ref` — thiếu thì mỗi PR tính lại mất vài phút

Sửa đường dẫn script trong workflow cho khớp (`tools/fra.py`).

**Không bật gate.** Chỉ comment, trong 2–4 tuần.

---

## G7 — Ghi lại quyết định

Tạo `docs/fra/DECISIONS.md` từ
`~/.claude/skills/flow-risk-assessment/assets/DECISIONS-template.md`, điền:

- **ADR-001** — kết quả G1, và bốn nhánh quyết định phái sinh
- **ADR-002** — flow đã chọn, lý do, và flow đã cân nhắc nhưng bỏ qua
- **ADR-003** — kết quả G5: recall holdout, split-date, hotfix nào không bắt
  được, và kết luận có bật CI hay không

---

## Báo cáo cuối

```
FRA bootstrap — <repo>

Flow:        <n> flow, <m> critical
Traffic:     <group>, <x> record/30 ngày
Invariant:   <a> confirmed, <b> proposed
Doctor:      <e> lỗi, <w> cảnh báo
Holdout:     recall <c>/<t> (<p>%), split <date>
CI:          comment-only | CHƯA BẬT (lý do)

Cut set kích thước 1 phát hiện được:
  <flow> — compensating_action: none, là lớp bù của <flow khác>

Canary mù:
  <flow> (<epd>/ngày) — cần <t> để có tín hiệu, dùng feature flag

Vùng tối của manifest:
  <k> hotfix không bắt được — <liệt kê ngắn>

Việc còn lại của người dùng:
  1. Confirm <b> invariant đang ở proposed
  2. Thêm alertable_metric cho flow critical
  3. Quan sát 2–4 tuần, theo dõi override rate; <30% thì bật gate
```

---

## Ba việc tuyệt đối không tự quyết

1. **`severity.class`** và bốn cờ khuếch đại
2. **Đổi invariant sang `status: confirmed`**
3. **Bật CI gate**, hoặc tuyên bố G5 đạt khi recall <60%

Nếu tự quyết, hệ thống sẽ đánh giá theo tiêu chuẩn nó tự sinh ra — mất hết giá
trị. Ba thứ này là quyết định kinh doanh, không suy ra được từ code.
