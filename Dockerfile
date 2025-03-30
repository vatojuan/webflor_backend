# Etapa 1: builder
FROM python:3.11-slim AS builder

WORKDIR /app

# Instalar dependencias del sistema necesarias para compilar paquetes
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Copiar los requerimientos e instalar dependencias
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Descargar modelo de Hugging Face en etapa build
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Etapa 2: final
FROM python:3.11-slim

WORKDIR /app

# Copiar todo desde el builder
COPY --from=builder /app /app

# Copiar el resto de la app por si hay archivos que no se incluyeron antes (opcional, ya están en builder)
COPY . /app

# Establecer variable de entorno para el cache de HF y path
ENV HF_HOME=/app/hf_cache
ENV PATH="/app/.local/bin:$PATH"
ENV PORT=10000

# Exponer el puerto que Render usará
EXPOSE 10000

# Comando de inicio para FastAPI
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
