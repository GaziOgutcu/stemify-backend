FROM python:3.11-slim

RUN apt-get update && apt-get install -y ffmpeg git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download models at build time so they're cached in the image
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs')" || true
RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs_6s')" || true

COPY . .
RUN mkdir -p uploads outputs

EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
