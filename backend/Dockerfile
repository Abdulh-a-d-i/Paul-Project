# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Prevent Python from buffering output and creating pyc files
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# --- NO EXPOSE needed ---

# --- Uvicorn CMD using Shell Form ---
# Note: No square brackets, let the shell handle $PORT
# Adjust 'main:app' if needed (e.g., 'src.main:app')
CMD uvicorn main:app --host 0.0.0.0 --port $PORT