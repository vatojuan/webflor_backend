from fastapi import APIRouter, HTTPException, Request
from datetime import datetime
import os, traceback
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

@router.post("/create-admin")
async def create_admin_job(request: Request):
    data = await request.json()
    title = data.get("title")
    description = data.get("description")
    requirements = data.get("requirements")
    expirationDate = data.get("expirationDate")
    userId = data.get("userId")

    if not title or not description or not userId:
        raise HTTPException(status_code=400, detail="Faltan campos obligatorios")
    
    # Convertir la fecha de expiración, si se envió, al objeto datetime
    exp_date = None
    if expirationDate:
        try:
            # Ajustamos el formato ISO (reemplazamos "Z" por "+00:00" si es necesario)
            exp_date = datetime.fromisoformat(expirationDate.replace("Z", "+00:00"))
        except Exception as e:
            raise HTTPException(status_code=400, detail="Formato de fecha inválido")
    
    # Convertir userId a entero
    try:
        userId_int = int(userId)
    except Exception as e:
        raise HTTPException(status_code=400, detail="userId debe ser un entero")

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        query = """
            INSERT INTO "Job" (title, description, requirements, "expirationDate", "userId")
            VALUES (%s, %s, %s, %s, %s) RETURNING id;
        """
        values = (title, description, requirements, exp_date, userId_int)
        cur.execute(query, values)
        job_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta creada", "jobId": job_id}
    except Exception as e:
        print("❌ Error al insertar en jobs:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor")
