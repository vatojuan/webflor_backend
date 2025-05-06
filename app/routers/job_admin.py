# app/routers/job_admin.py
from fastapi import APIRouter, HTTPException, Request
from datetime import datetime
import os
import psycopg2
import traceback
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(prefix="/api/job", tags=["job_admin"])

# --------------------------------------
# Helper: conexión DB
# --------------------------------------
def get_db_connection():
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST", "localhost"),
            port=int(os.getenv("DB_PORT", "5432")),
            sslmode="require"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

@router.get("/admin_offers")
async def get_admin_offers():
    """
    Devuelve todas las ofertas de la tabla jobs.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, title, description, requirements, expirationDate, userId
            FROM jobs
            ORDER BY id DESC
            """
        )
        rows = cur.fetchall()
        offers = []
        for row in rows:
            offers.append({
                "id": row[0],
                "title": row[1],
                "description": row[2],
                "requirements": row[3],
                "expirationDate": row[4].isoformat() if row[4] else None,
                "userId": row[5]
            })
        cur.close()
        conn.close()
        return {"offers": offers}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error al obtener las ofertas: {e}")

@router.put("/update-admin")
async def update_admin_offer(request: Request):
    """
    Actualiza una oferta de la tabla jobs y recalcula embeddings.
    """
    try:
        data = await request.json()
        job_id = data.get("id")
        title = data.get("title")
        description = data.get("description")
        requirements = data.get("requirements")
        expirationDate = data.get("expirationDate")
        userId = data.get("userId")

        if not job_id or not title or not description or not userId:
            raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

        try:
            job_id = int(job_id)
            userId = int(userId)
        except ValueError:
            raise HTTPException(status_code=400, detail="jobId o userId inválido")

        exp_date = None
        if expirationDate:
            try:
                exp_date = datetime.fromisoformat(expirationDate)
            except ValueError:
                raise HTTPException(status_code=400, detail="Formato de fecha inválido. Use 'YYYY-MM-DD'")

        # Recalcular embedding
        text_to_embed = f"{title} {description} {requirements or ''}"
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.embeddings.create(
            input=text_to_embed,
            model="text-embedding-ada-002"
        )
        embedding = resp.data[0].embedding

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE jobs
            SET title = %s,
                description = %s,
                requirements = %s,
                expirationDate = %s,
                userId = %s,
                embedding = %s
            WHERE id = %s
            RETURNING id, title, description, requirements, expirationDate, userId
            """,
            (title, description, requirements, exp_date, userId, embedding, job_id)
        )
        updated = cur.fetchone()
        if not updated:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")
        conn.commit()
        cur.close()
        conn.close()

        return {
            "id": updated[0],
            "title": updated[1],
            "description": updated[2],
            "requirements": updated[3],
            "expirationDate": updated[4].isoformat() if updated[4] else None,
            "userId": updated[5]
        }
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error interno del servidor")

@router.delete("/delete-admin")
async def delete_admin_offer(request: Request):
    """
    Elimina una oferta de la tabla jobs según jobId enviado en JSON.
    """
    try:
        data = await request.json()
        job_id = data.get("jobId")
        if not job_id:
            raise HTTPException(status_code=400, detail="jobId es requerido")
        job_id = int(job_id)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM jobs WHERE id = %s RETURNING id",
            (job_id,)
        )
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta eliminada", "jobId": job_id}
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error eliminando la oferta: {e}")
