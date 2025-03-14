from fastapi import APIRouter, HTTPException, Request
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()

def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST"),
            port=5432,
            sslmode="require"
        )
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

@router.post("/job/create-admin")
async def create_admin_job(request: Request):
    data = await request.json()
    title = data.get("title")
    description = data.get("description")
    requirements = data.get("requirements")
    expirationDate = data.get("expirationDate")
    userId = data.get("userId")

    if not title or not description or not userId:
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO jobs (title, description, requirements, "expirationDate", "userId")
            VALUES (%s, %s, %s, %s, %s) RETURNING id;
        """
        values = (title, description, requirements, expirationDate if expirationDate else None, userId)
        cur.execute(query, values)
        job_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta creada", "jobId": job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")
