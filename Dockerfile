FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway mounts the persistent volume at /app/data
# Set DATABASE_URL=/app/data/bookmarks.db in Railway environment variables
RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
