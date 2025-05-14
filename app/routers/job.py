# app/routers/job.py

import os
import traceback
import requests
import psycopg2

from datetime import datetime
from fastapi import APIRouter, HTTPException, Request, Depends
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from dotenv import load_dotenv

load_dotenv()

# ─── Router SIN prefix ─────────────────────────────────────────
router = APIRouter(tags=["job"])

# ─── Seguridad / OAuth2 ────────────────────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")
oauth2     = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin_sub(token: str = Depends(oauth2)) -> str:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return payload["sub"]
    except (JWTError, KeyError):
        raise HTTPException(status_code=401, detail="Token inválido o expirado")

# ─── Helpers de BD y embedding ──────────────────────────────────
def get_db_connection():
    return psycopg2.connect(
        dbname   = os.getenv("DBNAME"),
        user     = os.getenv("USER"),
        password = os.getenv("PASSWORD"),
        host     = os.getenv("HOST"),
        port     = int(os.getenv("DB_PORT", 5432)),
        sslmode  = "require"
    )

def get_admin_id_by_email(email: str) -> int | None:
    conn = get_db_connection()
    cur  = conn.cursor()
    cur.execute('SELECT id FROM "User" WHERE email = %s LIMIT 1;', (email,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else None

def generate_embedding(text: str):
    try:
        resp = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"
            },
            json={"model": "text-embedding-ada-002", "input": text}
        ).json()
        return resp["data"][0]["embedding"]
    except Exception:
        return None

# ─── Endpoint: crear oferta admin ──────────────────────────────
@router.post("/create-admin", status_code=201, dependencies=[Depends(oauth2)])
async def create_admin_job(
    request: Request,
    admin_sub: str = Depends(get_current_admin_sub)
):
    data         = await request.json()
    title        = data.get("title", "").strip()
    description  = data.get("description", "").strip()
    requirements = data.get("requirements", "").strip()
    expiration   = data.get("expirationDate")
    raw_user_id  = data.get("userId")
    label        = data.get("label", "manual")
    source       = data.get("source", "admin")
    is_paid      = bool(data.get("isPaid", False))
    c_email      = data.get("contactEmail")
    c_phone      = data.get("contactPhone")

    if not title or not description:
        raise HTTPException(status_code=400, detail="title y description son obligatorios")

    # Parse expirationDate
    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="expirationDate inválida (ISO-8601)")

    # Determinar userId: si viene numérico, úsalo; si no, busca el id del admin por email
    try:
        user_id = int(raw_user_id)
    except Exception:
        user_id = get_admin_id_by_email(admin_sub)
        if not user_id:
            raise HTTPException(status_code=400, detail="No se encontró admin en la tabla User")

    # Generar embedding (opcional)
    embedding = generate_embedding(f"{title}\n{description}\n{requirements}")

    # Insert en la BD
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO "Job"
              (title, description, requirements, "expirationDate",
               "userId", embedding, label, source,
               is_paid, contact_email, contact_phone)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
        """, (
            title, description, requirements, exp_date,
            user_id, embedding, label, source,
            is_paid, c_email, c_phone
        ))
        job_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return {"message": "Oferta creada", "jobId": job_id}

    except psycopg2.errors.UndefinedColumn:
        conn.rollback()
        raise HTTPException(
            status_code=500,
            detail="Columnas is_paid / contact_email / contact_phone faltan en la tabla Job"
        )
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error interno al crear la oferta")
