FROM python:3.11-slim

WORKDIR /app

# Cài đặt các thư viện hệ thống cần thiết (FFmpeg, Opus codec, âm thanh)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    pkg-config \
    gcc \
    build-essential \
    && ldconfig \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt dependencies Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép mã nguồn server
COPY . .

# Port mặc định
EXPOSE 8000
ENV PORT=8000

# Khởi chạy server AI
CMD ["python", "server.py"]

