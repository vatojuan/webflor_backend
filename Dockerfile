FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema necesarias para compilar
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Copiar e instalar requirements
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Descargar modelo de Hugging Face
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copiar el resto de la app
COPY . .

# Variables de entorno y puerto
ENV HF_HOME=/app/hf_cache
ENV PATH="/app/.local/bin:$PATH"
ENV PORT=10000

EXPOSE 10000

# Comando de inicio
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
