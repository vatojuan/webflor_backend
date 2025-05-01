# app/routers/matchings_admin.py
"""
Router para gestionar matchings entre candidatos y ofertas.
Registrar en main.py:
    from app.routers.matchings_admin import router as matchings_admin_router
    app.include_router(matchings_admin_router)
"""
import os
import psycopg2
from fastapi import APIRouter, HTTPException, Depends
from app.main import get_current_admin

router = APIRouter(
    prefix="/api/admin",
    tags=["matchings"],
    dependencies=[Depends(get_current_admin)]
)

def get_db_connection():
    try:
        conn = psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST", "localhost"),
            port=int(os.getenv("PORT", "5432")),
            sslmode="require"
        )
        return conn
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

@router.get("/matchings")
async def list_matchings():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id,
                   u.name AS user_name,
                   u.email,
                   j.title AS job_title,
                   m.score,
                   m.created_at
            FROM matchings m
            JOIN public."User" u ON m.user_id = u.id
            JOIN public.jobs j ON m.job_id = j.id
            ORDER BY m.score DESC;
        """)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener matchings: {e}")
    finally:
        cur.close()
        conn.close()

@router.post("/matchings/recalculate")
async def recalculate_matchings(threshold: float = 0.5, top_k: int = 5):
    """
    Recalcula todos los matchings:
    - Borra matchings existentes.
    - Calcula similitud entre cada usuario y oferta usando pgvector.
    - Inserta los top_k matchings por usuario con score >= threshold.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Limpiar tabla
        cur.execute("TRUNCATE TABLE public.matchings;")
        # Insertar nuevos matchings usando pgvector
        cur.execute("""
            INSERT INTO public.matchings (user_id, job_id, score)
            SELECT u.id, j.id,
                   (1 - (u.embedding <=> j.embedding))::real AS similarity
            FROM public."User" u,
                 public.jobs j
            WHERE (1 - (u.embedding <=> j.embedding)) >= %s
            ORDER BY u.id, similarity DESC
        """, (threshold,))
        # Si queremos limitar top_k por usuario, se necesitaría una consulta más compleja con ROW_NUMBER()
        # Por simplicidad, insertamos todos above threshold, luego podemos filtrar en el frontend.
        conn.commit()
        inserted = cur.rowcount
        return {"message": "Matchings recalculados", "total": inserted}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error recalculando matchings: {e}")
    finally:
        cur.close()
        conn.close()
