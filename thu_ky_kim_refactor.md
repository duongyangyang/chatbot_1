# Refactor đề xuất cho Thư ký Kim

## Database

``` sql
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  metadata_json TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  notes TEXT,
  status TEXT DEFAULT 'pending',
  priority INTEGER DEFAULT 3,
  start_at DATETIME,
  due_at DATETIME,
  remind_at DATETIME,
  repeat_rule TEXT,
  completed_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  content TEXT NOT NULL,
  type TEXT,
  importance INTEGER DEFAULT 1,
  last_used_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  payload_json TEXT,
  status TEXT DEFAULT 'pending',
  run_at DATETIME,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

## API tối giản

-   POST /chat
-   GET /history
-   POST /tasks
-   GET /tasks
-   POST /tasks/{id}/complete
-   POST /memory/save
-   POST /memory/search
-   GET /pending
-   POST /pending/read

## Frontend

1.  Bỏ màn hình "Xem database" tổng hợp nhiều bảng.
2.  Đổi thành:
    -   Tasks
    -   Memories
    -   Chat History
3.  Reminder là thuộc tính của task (`remind_at`), không cần bảng riêng.
4.  Morning planning / daily review chuyển thành `jobs`.

## Cần sửa backend

Các endpoint hiện tại: - /approve-schedule - /pending - /pending/read

nên chuyển sang thao tác với: - tasks - jobs

thay vì schedule_draft, pending_report, reminder riêng.

## Lưu ý

File HTML bạn gửi chỉ là frontend. Không có mã backend/database hiện tại
nên không thể tạo bản "code hoàn thiện" chính xác mà không thấy: -
server.py / app.py - schema.sql - models - API handlers

Cần toàn bộ backend để refactor hoàn chỉnh.
