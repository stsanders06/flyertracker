FROM python:3.11-alpine
WORKDIR /app
RUN pip install --no-cache-dir flask requests cryptography
COPY app/ .
COPY config.yaml /config.yaml
CMD ["python", "app.py"]
