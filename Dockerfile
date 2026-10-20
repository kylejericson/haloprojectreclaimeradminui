FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN dpkg --add-architecture i386 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libc6-i386 lib32gcc-s1 lib32stdc++6 \
    && mkdir -p /opt/steamcmd /home/steamcmd \
    && curl -fsSL https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz -o /tmp/steamcmd.tar.gz \
    && tar -xzf /tmp/steamcmd.tar.gz -C /opt/steamcmd \
    && rm -f /tmp/steamcmd.tar.gz \
    && chmod +x /opt/steamcmd/steamcmd.sh \
    && printf '#!/bin/sh\nexport HOME=/home/steamcmd\nexec /opt/steamcmd/steamcmd.sh "$@"\n' > /opt/steamcmd/reclaimer-steamcmd.sh \
    && chmod +x /opt/steamcmd/reclaimer-steamcmd.sh \
    && chown -R 65534:65534 /opt/steamcmd /home/steamcmd \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/home/steamcmd STEAMCMD_DIR=/opt/steamcmd STEAMCMD_PATH=/opt/steamcmd/steamcmd.sh STEAMCMD_HOME=/home/steamcmd
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./app.py
COPY static ./static
USER 65534:65534
VOLUME ["/opt/steamcmd", "/home/steamcmd"]
EXPOSE 8780
CMD ["python", "/app/app.py"]
