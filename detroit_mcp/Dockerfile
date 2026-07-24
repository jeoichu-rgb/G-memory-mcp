FROM python:3.12-slim

WORKDIR /app

# 章节数据
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 https://github.com/Baba88611/detroit-ai-player.git /opt/detroit-ai-player

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY resolver.py state.py server.py ./

ENV DETROIT_CHAPTERS_DIR=/opt/detroit-ai-player/01_json
ENV DETROIT_DATA_DIR=/app/data
ENV DETROIT_PORT=8100

EXPOSE 8100

CMD ["python", "server.py"]
