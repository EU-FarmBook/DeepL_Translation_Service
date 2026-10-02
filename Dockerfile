FROM python:3.12.8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Send large allocations (a document upload body is ~2x the file size) straight to mmap, so they
# go back to the OS when freed. With glibc's adaptive default they stayed in per-thread arenas:
# eight simultaneous 30 MB documents peaked at ~510 MB instead of ~200 MB, against a 512 MB limit.
ENV MALLOC_MMAP_THRESHOLD_=1048576

EXPOSE 8008

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8008"]