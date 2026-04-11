FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8844

VOLUME /data

CMD ["gunicorn", "--bind", "0.0.0.0:8844", "--workers", "2", "--threads", "4", "--timeout", "120", "--preload", "app:app"]
