FROM python:3.11-slim

# Create non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /home/appuser -s /bin/bash appuser

# Install system deps (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Set up app directory
WORKDIR /app
COPY pyproject.toml requirements.txt ./
COPY src/ src/
COPY config/ config/

# Install Python deps
RUN pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir -e .

# Create writable data volume
RUN mkdir -p /data && chown appuser:appuser /data
VOLUME /data

# Environment
ENV CLAWLESS_DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

# Switch to non-root user
USER appuser

ENTRYPOINT ["python", "-m", "clawless.main"]
CMD ["--channel", "text"]

# Run with: docker run --read-only --tmpfs /tmp -v clawless_data:/data -it clawless
