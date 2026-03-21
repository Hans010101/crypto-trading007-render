FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render.com / Cloud Run 会动态注入 PORT 环境变量，默认回退到 8888
CMD uvicorn web_dashboard:app --host 0.0.0.0 --port ${PORT:-8888} --workers 1
