# VibChat 💬

App chat realtime giống Messenger, build bằng Flask + SocketIO + SQLite.

## Tính năng
- 🔐 Đăng ký / Đăng nhập (username + password)
- 💬 Chat realtime (WebSocket)
- 🏠 Nhiều phòng / group chat
- 🖼️ Gửi hình ảnh
- 😊 Gửi Sticker (emoji packs)
- ❤️ React tin nhắn (6 loại react)
- ⌨️ Hiển thị "đang nhập..."
- 🟢 Đếm người online

---

## Cài đặt local

```bash
# 1. Clone / giải nén project
cd messenger

# 2. Tạo virtual environment
python -m venv venv
source venv/bin/activate       # Linux/Mac
venv\Scripts\activate          # Windows

# 3. Cài dependencies
pip install -r requirements.txt

# 4. Chạy app
python app.py
```

Mở trình duyệt: http://localhost:5000

---

## Deploy lên server thật

### Option 1: Railway (khuyến nghị - miễn phí)
1. Tạo tài khoản tại https://railway.app
2. New Project → Deploy from GitHub
3. Thêm biến môi trường: `SECRET_KEY=your-random-secret-key`
4. Railway tự detect `Procfile` và deploy

### Option 2: Render.com
1. Tạo tài khoản tại https://render.com
2. New Web Service → Connect GitHub repo
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `gunicorn --worker-class eventlet -w 1 app:app`
5. Thêm env var: `SECRET_KEY=your-random-secret-key`

### Option 3: VPS (Ubuntu)
```bash
# Cài gunicorn
pip install gunicorn eventlet

# Chạy production
SECRET_KEY=your-secret-key gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 app:app

# Dùng nginx làm reverse proxy (khuyến nghị)
# Cấu hình nginx proxy_pass về localhost:5000
# Thêm proxy_set_header Upgrade $http_upgrade; cho WebSocket
```

---

## Cấu trúc project
```
messenger/
├── app.py              # Flask backend + SocketIO
├── requirements.txt    # Dependencies
├── Procfile            # Deploy config
├── messenger.db        # SQLite DB (tự tạo khi chạy)
├── templates/
│   ├── auth.html       # Trang đăng nhập / đăng ký
│   └── chat.html       # Giao diện chat chính
└── static/
    └── uploads/        # Ảnh được upload (tự tạo)
```

---

## Lưu ý deploy
- Thay `SECRET_KEY` bằng chuỗi ngẫu nhiên dài (dùng `python -c "import secrets; print(secrets.token_hex(32))"`)
- SQLite phù hợp cho dự án nhỏ. Nếu cần scale, chuyển sang PostgreSQL
- Thư mục `uploads/` cần persistent storage trên cloud (dùng S3 hoặc Cloudinary)
- WebSocket cần server hỗ trợ long-lived connections (Railway/Render OK, Heroku free tier không ổn)
