# app/routers/file_processing.py
from fastapi import APIRouter, File, UploadFile, HTTPException, Depends, BackgroundTasks
from sentence_transformers import SentenceTransformer
import PyPDF2
import io
import psycopg2
from dotenv import load_dotenv
import os
from datetime import datetime

from app.routers.match import run_matching_for_user  # <-- Importación añadida

load_dotenv()

router = APIRouter(
    prefix="/files",
    tags=["files"],
)

# Cargar el modelo de embeddings (ajusta el nombre del modelo si es necesario)
model = SentenceTransformer('all-MiniLM-L6-v2')  # Modelo de 384 dimensiones

def extract_text_from_pdf(file_bytes: bytes) -> str:
    try:
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        text = ""
        for page in reader.pages:
            text += page.extract_text() or ""
        return text
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Error extrayendo el texto: {e}")

def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER", "postgres.apnfioxjddccokgkljvd"),
            password=os.getenv("PASSWORD", "Pachamama190"),
            host=os.getenv("HOST", "aws-0-sa-east-1.pooler.supabase.com"),
            port=5432,
            sslmode="require"
        )
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error conexión BD: {e}")

@router.post("/upload")
async def upload_file(
    background_tasks: BackgroundTasks,
    user_id: int,
    file: UploadFile = File(...)
):
    """
    1) Verifica PDF, extrae texto.
    2) Genera embedding y lo guarda en tabla embeddings.
    3) Dispara run_matching_for_user(user_id) en segundo plano.
    """
    # 1) Validar que sea PDF
    if file.content_type != "application/pdf":
        raise HTTPException(status_code=400, detail="Solo se aceptan archivos PDF")
    
    # 2) Leer bytes y extraer texto
    file_bytes = await file.read()
    text = extract_text_from_pdf(file_bytes)
    if not text:
        raise HTTPException(status_code=400, detail="No se pudo extraer texto del archivo")
    
    # 3) Generar embedding
    embedding = model.encode(text).tolist()
    
    # 4) Guardar embedding en BD
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        insert_query = """
            INSERT INTO embeddings (user_id, file_name, embedding, created_at)
            VALUES (%s, %s, %s, %s)
            RETURNING id;
        """
        cur.execute(insert_query, (user_id, file.filename, embedding, datetime.utcnow()))
        inserted_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error almacenando el embedding: {e}")
    
    # 5) Disparar matching en segundo plano
    background_tasks.add_task(run_matching_for_user, user_id)
    
    return {"message": "Archivo procesado y embedding almacenado", "embedding_id": inserted_id}
