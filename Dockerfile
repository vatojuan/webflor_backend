FROM python:3.11-slim AS builder

WORKDIR /app

# Instalar dependencias del sistema necesarias
RUN apt-get update && apt-get install -y --no-install-recommends build-essential

# Copiar los requerimientos e instalar dependencias
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir -r requirements.txt

# Descargar modelo de Hugging Face antes del despliegue
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Etapa final: copiar las dependencias al contenedor final
FROM python:3.11-slim

WORKDIR /app

# Copiar las dependencias desde la etapa builder
COPY --from=builder /app /app

# Copiar el resto de la aplicación
COPY . /app

# Asegurar que los paquetes están en el PATH
ENV PATH="/app/.local/bin:$PATH"

# Configurar variables de caché y puerto
ENV HF_HOME=/app/hf_cache
ENV PORT=10000

# Comando de inicio, asegurando que Python lo reconozca correctamente
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10000"]
