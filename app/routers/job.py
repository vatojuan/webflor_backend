# app/routers/job.py

from fastapi import APIRouter, HTTPException, Request
from datetime import datetime
import os, traceback, requests
import psycopg2
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST", "localhost"),
            port=int(os.getenv("DB_PORT", 5432)),
            sslmode="require"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

def generate_job_embedding(text: str):
    try:
        openai_api_key = os.getenv("OPENAI_API_KEY")
        resp = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {openai_api_key}"
            },
            json={"model": "text-embedding-ada-002", "input": text}
        )
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as e:
        print("❌ Error generando embedding:", e)
        return None

@router.post("/create-admin")
async def create_admin_job(request: Request):
    payload = await request.json()
    title       = payload.get("title")
    description = payload.get("description")
    requirements= payload.get("requirements")
    expiration  = payload.get("expirationDate")
    user_id     = payload.get("userId")

    # Validaciones básicas
    if not title or not description or not user_id:
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

    # Parseo de fecha
    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except Exception:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use ISO 8601")

    # Parseo de userId
    try:
        user_id = int(user_id)
    except Exception:
        raise HTTPException(status_code=400, detail="userId debe ser un entero")

    # Generación de embedding
    job_text = f"{title}\n\n{description}\n\n{requirements or ''}"
    embedding = generate_job_embedding(job_text)
    if embedding:
        print("✅ Embedding generado exitosamente")
    else:
        print("⚠️ No se generó el embedding, se procederá sin él.")

    # Inserción en BD, ahora con source y label
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO "Job"
                (title, description, requirements, "expirationDate", "userId", embedding, source, label)
            VALUES
                (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            title,
            description,
            requirements,
            exp_date,
            user_id,
            embedding,
            "admin",   # source por defecto
            "manual"   # label por defecto
        ))
        job_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta creada", "jobId": job_id}

    except Exception:
        print("❌ Error al insertar en Job:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor")
