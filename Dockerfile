FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY driver_autoterm/ driver_autoterm/
COPY config.default.ini .

ENTRYPOINT ["python", "-m", "driver_autoterm.main"]
CMD ["--config", "/app/config.ini"]
