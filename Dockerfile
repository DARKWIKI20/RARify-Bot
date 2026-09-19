FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    tar \
    p7zip-full \
    ca-certificates \
    && wget https://www.rarlab.com/rar/rarlinux-x64-624.tar.gz \
    && tar -xzf rarlinux-x64-624.tar.gz \
    && cp rar/rar /usr/local/bin/ \
    && cp rar/default.sfx /usr/local/bin/ \
    && rm -rf rar rarlinux-x64-624.tar.gz \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
