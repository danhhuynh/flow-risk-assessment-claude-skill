# FRA — Nhật ký quyết định

Ghi mọi quyết định lệch khỏi `SPEC.md`, kèm lý do. Format: ADR nhẹ.

Mục đích: 6 tháng sau, khi ai đó hỏi "vì sao ngưỡng là 0.4 chứ không phải 0.3",
câu trả lời phải ở đây chứ không nằm trong đầu một người.

---

## ADR-001 · Kết quả kiểm tiền đề repo (T0.1)

**Ngày:** ____
**Người:** ____

| Kiểm tra | Kết quả | Hệ quả |
|---|---|---|
| `is-shallow-repository` | | |
| Số commit | | |
| % commit có tiền tố fix | | |
| Ngôn ngữ chính | | |
| Có access log | | |
| Có coverage | | |

**Quyết định phái sinh:**

-

---

## ADR-002 · Ba flow đầu tiên (T1.3)

**Ngày:** ____

| Flow | Lý do chọn | Severity | executions/day |
|---|---|---|---|
| | | | |
| | | | |
| | | | |

**Flow đã cân nhắc nhưng chưa làm, kèm lý do:**

-

---

## ADR-003 · Kết quả backtest (T1.7)

**Ngày:** ____
**Số commit/PR đã chạy:** ____

| Sự cố đã biết | FRA có gắn cờ đúng flow? | Ghi chú |
|---|---|---|
| | | |

**Tỷ lệ gắn cờ tràn lan trên PR vô hại:** ____

**Điều chỉnh đã làm sau backtest:**

-

**Kết luận: có bật lên hay không, và vì sao:**

-

---

## Mẫu cho ADR tiếp theo

```markdown
## ADR-00N · <tiêu đề>

**Ngày:** ____
**Người:** ____
**Trạng thái:** đề xuất | đã chấp nhận | đã thay thế bởi ADR-00M

**Bối cảnh**
Vấn đề gì cần quyết? Ràng buộc nào đang có?

**Quyết định**
Chọn gì.

**Lý do**
Vì sao chọn cái này thay vì các lựa chọn khác.

**Đánh đổi**
Mất gì khi chọn cái này. (Bắt buộc điền — nếu không thấy đánh đổi nào,
có thể chưa hiểu đủ vấn đề.)

**Cách kiểm chứng**
Làm sao biết quyết định này đúng hay sai sau 3 tháng?
```
