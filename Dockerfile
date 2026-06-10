FROM python:3.13-slim

# Install PostgreSQL client tools
RUN apt-get update && apt-get install -y \
    postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies explicitly
RUN pip install --no-cache-dir \
    psycopg2-binary \
    azure-storage-blob \
    prometheus_client 

# Copy verify script
COPY verify.py /app/verify.py

WORKDIR /app

CMD ["python", "verify.py"]