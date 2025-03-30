from fastapi import APIRouter, HTTPException, Request
from datetime import datetime
import os
import psycopg2
import openai
from dotenv import load_dotenv

load_dotenv()

# Configurar OpenAI con la API key
openai.api_key = os.getenv("OPENAI_API_KEY")

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

@router.get("/admin_offers")
async def get_admin_offers():
    """
    Devuelve todas las ofertas de la tabla Job.
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Se usa "Job" entre comillas dobles para respetar el nombre exacto de la tabla.
        cur.execute("""
            SELECT id, title, description, requirements, "expirationDate", "userId"
            FROM "Job"
        """)
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
        print(f"Error al obtener ofertas: {e}")
        raise HTTPException(status_code=500, detail=f"Error al obtener las ofertas: {e}")

@router.put("/update-admin")
async def update_admin_offer(request: Request):
    try:
        data = await request.json()
        print("📥 Datos recibidos:", data)

        job_id = data.get("id")
        title = data.get("title")
        description = data.get("description")
        requirements = data.get("requirements")
        expirationDate = data.get("expirationDate")
        userId = data.get("userId")

        if not job_id or not title or not description or not userId:
            raise HTTPException(status_code=400, detail="Faltan campos obligatorios")

        # Convertir IDs a int
        job_id_int = int(job_id)
        userId_int = int(userId)

        # Convertir fecha
        exp_date = None
        if expirationDate:
            try:
                exp_date = datetime.fromisoformat(expirationDate)
            except Exception as e:
                print("❌ Error al convertir la fecha:", expirationDate, e)
                raise HTTPException(status_code=400, detail="Fecha inválida")

        print("📦 Preparando texto para embedding...")
        text_to_embed = f"{title} {description} {requirements or ''}"
        print("🔠 Texto:", text_to_embed)

        embedding_response = openai.Embedding.create(
            input=text_to_embed,
            model="text-embedding-ada-002"
        )
        embedding = embedding_response['data'][0]['embedding']
        print("✅ Embedding generado")

        # Ejecutar update
        conn = get_db_connection()
        cur = conn.cursor()
        update_query = """
            UPDATE "Job"
            SET title = %s,
                description = %s,
                requirements = %s,
                "expirationDate" = %s,
                "userId" = %s,
                embedding = %s
            WHERE id = %s
            RETURNING id, title, description, requirements, "expirationDate", "userId";
        """
        print("🚀 Ejecutando UPDATE en base de datos")
        cur.execute(update_query, (
            title, description, requirements, exp_date, userId_int, embedding, job_id_int
        ))
        updated_row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if not updated_row:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")

        print("✅ Oferta actualizada:", updated_row)

        return {
            "id": updated_row[0],
            "title": updated_row[1],
            "description": updated_row[2],
            "requirements": updated_row[3],
            "expirationDate": updated_row[4].isoformat() if updated_row[4] else None,
            "userId": updated_row[5]
        }

    except Exception as e:
        import traceback
        print("❌ EXCEPCIÓN EN EL BACKEND")
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail="Error interno del servidor")

@router.delete("/delete-admin")
async def delete_admin_offer(request: Request):
    """
    Elimina una oferta de la tabla Job según el jobId enviado.
    """
    data = await request.json()
    job_id = data.get("jobId")
    if not job_id:
        raise HTTPException(status_code=400, detail="JobId es requerido")
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Se usa "Job" para eliminar la oferta
        cur.execute('DELETE FROM "Job" WHERE id = %s RETURNING id', (job_id,))
        deleted = cur.fetchone()
        if not deleted:
            raise HTTPException(status_code=404, detail="Oferta no encontrada")
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta eliminada", "jobId": job_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error eliminando la oferta: {e}")
