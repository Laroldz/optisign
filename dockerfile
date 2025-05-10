# ───────────────── Base image ────────────────
FROM python:3.11-slim

# ───────────────── Non-root user (security) ─
ENV USER app
RUN adduser --disabled-password --gecos "" $USER
USER $USER
WORKDIR /app

# ───────────────── Install dependencies ─────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ───────────────── Copy app code ─────────────
COPY scan.py .

# ───────────────── Entry point ───────────────
ENTRYPOINT ["python", "-u", "scan.py"]
