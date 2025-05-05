# app/routers/matchings_admin.py
import os
from dotenv import load_dotenv
import psycopg2
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# --------------------------------------------------
# Cargar .env y parámetros de JWT
# --------------------------------------------------
load_dotenv()
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
)
ALGORITHM = os.getenv("ALGORITHM", "HS256")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    """Valida el token JWT de admin y devuelve el 'sub'."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido o expirado")
    return sub

# --------------------------------------------------
# Router y dependencia global
# --------------------------------------------------
router = APIRouter(
    prefix="/api/admin",
    tags=["matchings"],
    dependencies=[Depends(get_current_admin)],
)

def get_db_connection():
    """Establece conexión a PostgreSQL usando psycopg2."""
    try:
        return psycopg2.connect(
            dbname=os.getenv("DBNAME", "postgres"),
            user=os.getenv("USER"),
            password=os.getenv("PASSWORD"),
            host=os.getenv("HOST", "localhost"),
            port=5432,
            sslmode="require"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la conexión a la base de datos: {e}")

@router.get("/matchings")
async def list_matchings():
    """
    Lista todos los matchings ordenados por score descendente.
    Devuelve: [{ id, user_name, email, job_title, score, created_at }, ...]
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            SELECT m.id,
                   u.name    AS user_name,
                   u.email   AS email,
                   j.title   AS job_title,
                   m.score   AS score,
                   m.created_at
            FROM public.matchings m
            JOIN public."User" u ON m.user_id = u.id
            JOIN public."Job"  j ON m.job_id  = j.id
            ORDER BY m.score DESC;
        """)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener matchings: {e}")
    finally:
        cur.close()
        conn.close()

@router.post("/matchings/recalculate")
async def recalculate_matchings(threshold: float = 0.5):
    """
    Recalcula todos los matchings borrando la tabla y generando de nuevo
    según similitud de embeddings (pgvector).
    - threshold: mínimo similarity (0 a 1) para insertar.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Limpiar tabla
        cur.execute("TRUNCATE TABLE public.matchings;")
        # 2) Insertar nuevos matchings
        cur.execute("""
            INSERT INTO public.matchings (user_id, job_id, score)
            SELECT u.id,
                   j.id,
                   (1 - (u.embedding <=> j.embedding))::real AS similarity
            FROM public."User" u
            CROSS JOIN public."Job" j
            WHERE (1 - (u.embedding <=> j.embedding)) >= %s
            ORDER BY u.id, similarity DESC;
        """, (threshold,))
        conn.commit()
        total = cur.rowcount
        return {"message": "Matchings recalculados", "inserted": total}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error recalculando matchings: {e}")
    finally:
        cur.close()
        conn.close()
