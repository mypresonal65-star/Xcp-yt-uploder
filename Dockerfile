FROM python:3.11-slim

# Install system dependencies including ffmpeg, aria2, and mp4decrypt (Bento4)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    wget \
    unzip \
    ca-certificates \
    && (wget -q https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip \
        && unzip -q Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip \
        && cp Bento4-SDK-1-6-0-641.x86_64-unknown-linux/bin/mp4decrypt /usr/local/bin/ \
        && chmod +x /usr/local/bin/mp4decrypt \
        && rm -rf Bento4-SDK* || true) \
    && rm -rf /var/lib/apt/lists/*


# Set working directory
WORKDIR /app

# Copy dependency definition
COPY requirements.txt .

# Install python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Create downloads folder
RUN mkdir -p downloads

# Render sets PORT environment variable dynamically (defaults to 8000)
ENV PORT=8000
EXPOSE 8000

# Start Uvicorn ASGI server
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
