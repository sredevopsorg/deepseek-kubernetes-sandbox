FROM python:3.12-slim

# Read-only-rootfs friendly: no .pyc writes, no home directory writes.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY gateway.py .

EXPOSE 8080
# Run as a fixed non-root UID; HOME=/tmp keeps caches out of the image.
USER 10000

CMD ["uvicorn", "gateway:create_app", "--factory", \
     "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
