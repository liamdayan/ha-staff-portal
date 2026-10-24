# A maintained tag: Docker rebuilds it with security fixes, and Supervisor builds the add-on
# from it on install and update. Move to a newer Alpine before this one's support ends.
FROM python:3.13-alpine3.24

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ /app/
COPY config.yaml /app/config.yaml

ENV DATA_DIR=/data
CMD ["python", "-u", "main.py"]
