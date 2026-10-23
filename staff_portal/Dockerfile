FROM python:3.12-alpine3.20

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY config.yaml /app/config.yaml

ENV DATA_DIR=/data
CMD ["python", "-u", "main.py"]
