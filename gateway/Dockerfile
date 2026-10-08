FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY metrics.py backends.py app.py ./

RUN adduser --disabled-password --gecos "" --uid 10001 app
USER 10001

EXPOSE 8000

# Single worker, same reason as every prometheus_client deployment: counters
# live in process memory, so multiple workers make Prometheus see phantom
# counter resets. Concurrency here comes from asyncio and DECODE_SLOTS, not
# from processes, so one worker is also the correct design -- the semaphore
# must be shared to be an admission control at all.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1"]
