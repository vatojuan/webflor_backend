# app/routers/job.py
import os
import traceback
import requests
import psycopg2
from datetime import datetime
from typing import Optional, List

from fastapi          import APIRouter, HTTPException, Request, Depends
from fastapi.security import OAuth2PasswordBearer
from jose             import jwt, JWTError
from dotenv           import load_dotenv

load_dotenv()
router = APIRouter(tags=["job"])

# ────────────────────────── Auth ──────────────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")
oauth2     = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin_sub(token: str = Depends(oauth2)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "Token inválido o expirado")


# ────────────────────────── DB ────────────────────────────
def get_db_connection():
    return psycopg2.connect(
        dbname   = os.getenv("DBNAME"),
        user     = os.getenv("USER"),
        password = os.getenv("PASSWORD"),
        host     = os.getenv("HOST"),
        port     = int(os.getenv("DB_PORT", 5432)),
        sslmode  = "require"
    )

def get_admin_id_by_email(email: str) -> Optional[int]:
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None


# ────────────────────── OpenAI Embedding ──────────────────
def generate_embedding(text: str) -> Optional[List[float]]:
    try:
        resp = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}"
            },
            json={"model": "text-embedding-ada-002", "input": text},
            timeout=20
        ).json()
        return resp["data"][0]["embedding"]
    except Exception:
        traceback.print_exc()
        return None


# ──────────────────── helpers columna ────────────────────
def job_has_column(cur, col: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'Job' AND column_name = %s
         LIMIT 1
        """, (col,)
    )
    return bool(cur.fetchone())


# ────────────────────  POST /create-admin  ───────────────
@router.post("/create-admin", status_code=201, dependencies=[Depends(oauth2)])
async def create_admin_job(
    request: Request,
    admin_sub: str = Depends(get_current_admin_sub)
):
    data          = await request.json()
    title         = (data.get("title") or "").strip()
    description   = (data.get("description") or "").strip()
    requirements  = (data.get("requirements") or "").strip()
    expiration    =  data.get("expirationDate")
    raw_user_id   =  data.get("userId")
    label         =  data.get("label",  "manual")
    source        =  data.get("source", "admin")

    # opcionales
    is_paid       =  bool(data.get("isPaid", False))
    contact_email =  data.get("contactEmail") or data.get("contact_email")
    contact_phone =  data.get("contactPhone") or data.get("contact_phone")

    # validaciones mínimas
    if not title or not description:
        raise HTTPException(400, "title y description son obligatorios")
    if source == "admin" and not contact_email:
        raise HTTPException(400, "Las ofertas del administrador requieren contactEmail")

    # expirationDate → datetime (o None)
    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "expirationDate inválida (ISO-8601)")

    # userId
    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        user_id = get_admin_id_by_email(admin_sub)
        if not user_id:
            raise HTTPException(400, "No se encontró admin en User")

    embedding = generate_embedding(f"{title}\n{description}\n{requirements}")

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # ── inspeccionamos columnas opcionales ──
        has_is_paid       = job_has_column(cur, "is_paid")
        has_snake_contact = job_has_column(cur, "contact_email")
        has_camel_contact = job_has_column(cur, "contactEmail")

        email_col = None
        phone_col = None
        if has_snake_contact:
            email_col, phone_col = "contact_email", "contact_phone"
        elif has_camel_contact:
            email_col, phone_col = "contactEmail", "contactPhone"

        # ── construimos query dinámica ──
        fields = [
            "title", "description", "requirements", '"expirationDate"',
            '"userId"', "embedding", "label", "source"
        ]
        values = [
            title, description, requirements, exp_date,
            user_id, embedding, label, source
        ]

        if has_is_paid:
            fields.append("is_paid")
            values.append(is_paid)
        if email_col:
            fields.extend([email_col, phone_col])
            values.extend([contact_email, contact_phone])

        placeholders = ", ".join(["%s"] * len(fields))
        sql = f'INSERT INTO "Job" ({", ".join(fields)}) VALUES ({placeholders}) RETURNING id;'
        cur.execute(sql, tuple(values))
        job_id = cur.fetchone()[0]
        conn.commit()

    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, f"Error interno al crear oferta: {e}")
    finally:
        cur.close()
        conn.close()

    # devolvemos lo grabado para que el front lo use al instante
    return {
        "message": "Oferta creada",
        "jobId":        job_id,
        "label":        label,
        "isPaid":       is_paid,
        "contactEmail": contact_email,
        "contactPhone": contact_phone
    }
