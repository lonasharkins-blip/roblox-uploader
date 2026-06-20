FROM python:3.11-slim

# assimp-utils fornece a ferramenta de linha de comando "assimp",
# usada para converter qualquer formato de modelo 3D em .glb
RUN apt-get update && \
    apt-get install -y --no-install-recommends assimp-utils && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT:-10000} --timeout 180 app:app"]
