FROM python:3.12-slim

WORKDIR /bench

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY loadgen.py .

# Defaults to the in-network gateway address so `make bench` needs no flags.
ENTRYPOINT ["python", "loadgen.py", \
            "--url", "http://gateway:8000/v1/chat/completions"]
