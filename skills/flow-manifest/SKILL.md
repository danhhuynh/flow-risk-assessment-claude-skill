---
name: flow-manifest
description: Tạo Business Flow Manifest cho repo bằng cách đọc traffic thật từ AWS CloudWatch Logs. Kiểm SSO trước, liệt kê log group, lấy tần suất endpoint 30 ngày, rồi đề xuất 3 business flow critical kèm hậu quả nếu hỏng. Dùng khi người dùng gọi /flow-manifest, hoặc nói muốn tạo/khởi tạo business flow manifest, bootstrap FRA cho repo, lấy traffic từ CloudWatch để biết endpoint nào chạy nhiều, hoặc tìm xem repo này có những business flow nào. Also use for English phrasings such as create business flow manifest, bootstrap flow risk assessment, read CloudWatch traffic to find critical flows.
allowed-tools: Bash(python3 tools/fra.py:*), Bash(aws sts get-caller-identity:*), Bash(aws logs describe-log-groups:*), Bash(aws configure list-profiles:*), Bash(git log:*), Bash(git ls-files:*), Bash(cp:*), Bash(mkdir:*), Bash(ls:*), Bash(find:*), Read, Write, Edit, Glob, Grep
---

# Tạo Business Flow Manifest từ CloudWatch

Mục tiêu: từ traffic thật trên CloudWatch, đề xuất 3 business flow critical và
dựng khung manifest trong `.fra/flows/`.

Nếu `$ARGUMENTS` có tên log group, dùng luôn và bỏ qua bước 2.

## Bước 1 — Chuẩn bị và kiểm SSO

```bash
mkdir -p tools
cp ~/.claude/skills/flow-risk-assessment/scripts/fra.py tools/ 2>/dev/null \
  || cp "$(find ~ -name fra.py -path '*flow-risk-assessment*' | head -1)" tools/
python3 tools/fra.py init
python3 tools/fra.py aws-check
```

`aws-check` xử lý cả ba trạng thái. Đọc output và hành động tương ứng:

| Output | Làm gì |
|---|---|
| "Chưa cài AWS CLI" | **Dừng.** Đưa lệnh cài cho người dùng, chờ họ xong |
| "Chưa đăng nhập AWS" | **Dừng.** Nhắc họ chạy `aws sso login --profile <profile>` rồi `export AWS_PROFILE=<profile>`. Gợi ý `aws configure list-profiles` nếu họ không biết tên profile. Chờ họ xác nhận |
| "✓ Đã đăng nhập" + danh sách log group | Tiếp bước 2 |

Đây là hai chỗ **phải dừng và chờ người dùng** — không tự chạy `aws sso login`
vì nó mở browser và cần họ tương tác.

## Bước 2 — Chọn log group

`aws-check` in danh sách group kèm dung lượng. Chọn group chứa **HTTP access
log** của repo hiện tại.

Cách nhận biết: tên thường chứa `nginx`, `apache`, `access`, `alb`, `cloudfront`,
hoặc tên service. Group dung lượng lớn nhất thường là access log.

**Tránh** `/aws/lambda/...` trừ khi repo này thật sự là Lambda — chúng chứa
execution log, không phải access log.

Nếu có nhiều group trông hợp lý, hỏi người dùng chọn thay vì tự đoán. Chọn sai
làm mọi số về sau vô nghĩa.

## Bước 3 — Lấy traffic

```bash
python3 tools/fra.py cloudwatch --group <tên-group> --days 30
```

Kiểm output trước khi đi tiếp:

- Tỷ lệ record khớp **≥50%**. Thấp hơn → group này có thể không phải access
  log. Quay lại bước 2.
- Path đã gộp đúng: `/orders/123` và `/orders/999` cùng thành `/orders/:id`
- Top endpoint có khớp với hiểu biết về hệ thống không?

Điều cuối là phép thử tỉnh táo. Nếu endpoint traffic cao nhất là thứ bất ngờ,
hoặc luồng quan trọng nhất không xuất hiện — **nói ra và hỏi người dùng**.
Thường là do CDN cache khiến origin không thấy traffic, và bỏ qua thì mọi
`P(R)` về sau đều sai.

## Bước 4 — Đề xuất 3 flow critical

Đọc `.fra/cache/traffic.json` và cấu trúc code. Với mỗi flow ứng viên, lần từ
endpoint → route definition → controller → service → tầng database để tìm
`entities`.

Trình bày dạng bảng:

| Flow | Endpoint | executions/day | Entities | Hậu quả nếu hỏng |
|---|---|---|---|---|

Tiêu chí chọn, theo thứ tự ưu tiên:

1. Mất tiền
2. Hỏng dữ liệu không phục hồi được
3. Rò rỉ dữ liệu cá nhân
4. Chặn nghiệp vụ chính, cần can thiệp tay

**Không** chọn theo độ phức tạp code hay số bug lịch sử. Tiêu chí là **hậu quả**.

Phép thử nhanh cho từng ứng viên:

> *"Nếu flow này hỏng lúc 3 giờ sáng thứ Bảy và không ai biết trong 6 tiếng,
> chuyện gì xảy ra?"*

Cũng nhớ kiểm entry point **không phải HTTP** — cron, queue consumer, event
handler. Chúng không có trong access log nhưng thường là nơi đặt
reconciliation, tức là một lớp phòng thủ:

```bash
git ls-files | grep -iE 'cron|schedul|job|worker|consumer|command' | head -20
```

Cron thì đọc trực tiếp cron expression để ra số lần/ngày — chính xác tuyệt đối.

## Bước 5 — Dựng khung manifest

Copy template cho mỗi flow đã được người dùng xác nhận:

```bash
cp ~/.claude/skills/flow-risk-assessment/assets/_TEMPLATE.flow.yaml \
   .fra/flows/<flow_id>.yaml
rm -f .fra/flows/example.yaml
```

Điền: `id`, `name`, `entities`, `executions_per_day`, `entry_points`.

**Để trống và báo lại cho người dùng:** `severity.class`, bốn cờ khuếch đại, và
toàn bộ `invariants`.

Cuối cùng:

```bash
python3 tools/fra.py doctor
```

Sửa hết lỗi entity không resolve. Cảnh báo về invariant và severity là bình
thường ở giai đoạn này.

## Ba việc không được tự quyết

Đây là **quyết định kinh doanh**, không suy ra được từ code:

1. **`severity.class`** — hỏi người dùng, đưa rubric: critical (mất tiền / hỏng
   dữ liệu / rò rỉ) · high (chặn nghiệp vụ, sửa tay) · medium (có workaround) ·
   low (nội bộ)
2. **Bốn cờ khuếch đại** — `idempotent`, `reversible`, `compensating_action`,
   `external_side_effects`. Có thể đề xuất dựa trên code, nhưng phải để họ xác
   nhận
3. **Invariant** — không tự viết vào manifest ở bước này

Nếu tự quyết, hệ thống sẽ đánh giá theo tiêu chuẩn nó tự sinh ra — mất hết giá
trị.

## Hai luật cứng

**`executions_per_day` lấy từ log, không đoán.** Không biết thì **để trống**,
không điền `0`. `0` nghĩa là "đo được và không có traffic" → hệ thống coi flow
rủi ro thấp. Để trống nghĩa là "không biết" → hệ thống bảo thủ.

**`entities` dùng mức file, không dùng line range.** Line range vỡ sau mỗi lần
format. Dùng wildcard extension (`src/services/Checkout.*`) để sống qua refactor.

## Xong rồi thì gì

Báo cho người dùng bước tiếp theo: viết invariant cho từng flow — nguồn tốt
nhất là lịch sử hotfix. Chi tiết ở `references/RUNBOOK.md` Bước 4 của skill
`flow-risk-assessment`.
