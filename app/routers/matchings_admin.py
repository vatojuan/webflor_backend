# app/routers/matchings_admin.py
import os
from dotenv import load_dotenv
import psycopg2
from fastapi import APIRouter, HTTPException, Depends, Query, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from typing import Optional, List

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
    if not token:
        raise HTTPException(status_code=401, detail="Token no proporcionado")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub:
            raise HTTPException(status_code=401, detail="Token inválido o expirado")
    except JWTError:
        raise HTTPException(status_code=401, detail="Token inválido o expirado")
    return sub

# --------------------------------------------------
# Router y dependencia global
# --------------------------------------------------
router = APIRouter(
    prefix="/api/admin",
    tags=["matchings"],
    dependencies=[Depends(get_current_admin)],
)

# --------------------------------------------------
# Conexión a la base de datos
# --------------------------------------------------
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

# --------------------------------------------------
# Listar matchings con paginación y filtros
# --------------------------------------------------
@router.get("/matchings")
async def list_matchings(
    skip: int = Query(0, ge=0, description="Número de registros a omitir"),
    limit: int = Query(50, gt=0, le=500, description="Máximo de registros a retornar"),
    user_id: Optional[int] = Query(None, description="Filtrar por ID de usuario"),
    job_id: Optional[int] = Query(None, description="Filtrar por ID de oferta"),
    min_score: float = Query(0.0, ge=0.0, le=1.0, description="Score mínimo"),
):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # Construir consulta dinámica
        base = [
            "SELECT m.id, u.name AS user_name, u.email, j.title AS job_title, m.score, m.created_at",
            "FROM public.matchings m",
            "JOIN public.\"User\" u ON m.user_id = u.id",
            "JOIN public.\"Job\" j ON m.job_id = j.id",
            "WHERE m.score >= %s"
        ]
        params: List = [min_score]
        if user_id:
            base.append("AND m.user_id = %s"); params.append(user_id)
        if job_id:
            base.append("AND m.job_id = %s"); params.append(job_id)
        base.append("ORDER BY m.score DESC LIMIT %s OFFSET %s"); params.extend([limit, skip])
        query = " \n".join(base)
        cur.execute(query, params)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
        return [dict(zip(cols, row)) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error al obtener matchings: {e}")
    finally:
        cur.close(); conn.close()

# --------------------------------------------------
# Recalcular matchings (total o incremental)
# --------------------------------------------------
def recalc_db(user_id: Optional[int], job_id: Optional[int], threshold: float):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        if user_id or job_id:
            # Incremental: borrar y recalcular solo para filtros
            conds = []
            params = []
            if user_id:
                conds.append("user_id = %s"); params.append(user_id)
            if job_id:
                conds.append("job_id = %s"); params.append(job_id)
            where = " AND ".join(conds)
            cur.execute(f"DELETE FROM public.matchings WHERE {where}", params)
            insert_cond = []
            insert_params = [threshold]
            if user_id:
                insert_cond.append("u.id = %s"); insert_params.append(user_id)
            if job_id:
                insert_cond.append("j.id = %s"); insert_params.append(job_id)
            recalc_where = " AND ".join(insert_cond)
            sql = f"""
                INSERT INTO public.matchings (user_id, job_id, score)
                SELECT u.id, j.id, (1 - (u.embedding <=> j.embedding))::real AS similarity
                FROM public.\"User\" u CROSS JOIN public.\"Job\" j
                WHERE similarity >= %s {(' AND ' + recalc_where) if recalc_where else ''};
            """
            cur.execute(sql, insert_params)
        else:
            # Full recalc
            cur.execute("TRUNCATE TABLE public.matchings;")
            cur.execute("""
                INSERT INTO public.matchings (user_id, job_id, score)
                SELECT u.id, j.id, (1 - (u.embedding <=> j.embedding))::real AS similarity
                FROM public.\"User\" u CROSS JOIN public.\"Job\" j
                WHERE (1 - (u.embedding <=> j.embedding)) >= %s;
            """, (threshold,))
        conn.commit()
        return cur.rowcount
    finally:
        cur.close(); conn.close()

@router.post("/matchings/recalculate")
async def recalculate_matchings(
    background_tasks: BackgroundTasks,
    threshold: float = Query(0.5, ge=0.0, le=1.0, description="Score mínimo para insertar"),
    user_id: Optional[int] = Query(None, description="Solo recalc para este usuario"),
    job_id: Optional[int] = Query(None, description="Solo recalc para esta oferta"),
):
    """
    Recalcula matchings en background.
    - threshold: score mínimo
    - user_id/job_id: recalc incremental
    """
    background_tasks.add_task(recalc_db, user_id, job_id, threshold)
    target = f"user {user_id}" if user_id else f"job {job_id}" if job_id else "toda la tabla"
    return {"message": f"Recalc task iniciada para {target} con threshold >= {threshold}"}
