# app/routers/job.py
"""Endpoints públicos y de administración para ofertas (Job).

• GET /api/job/                 → listado de ofertas vigentes
• GET /api/job/list             → alias legado (frontend)
• GET /api/job/my-applications  → postulaciones del usuario
• POST /api/job/create-admin    → crear oferta (admin) + matching

No depende de módulos externos; define su propio get_current_user.
"""
from __future__ import annotations

import os, threading, traceback
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional

import psycopg2, requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

from app.routers.match import run_matching_for_job

load_dotenv()

router = APIRouter(prefix="/api/job", tags=["job"])

# ─────────────────── Configuración ───────────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

DB_PARAMS = {
    "dbname":   os.getenv("DBNAME"),
    "user":     os.getenv("USER"),
    "password": os.getenv("PASSWORD"),
    "host":     os.getenv("HOST"),
    "port":     int(os.getenv("DB_PORT", 5432)),
    "sslmode":  "require",
}

# OAuth2 – uno para admin, otro para usuarios
oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
oauth2_user  = OAuth2PasswordBearer(tokenUrl="/auth/login")

# ─────────────── Helpers de autenticación ───────────────
def get_current_admin_sub(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "Token inválido o expirado (admin)")

def get_current_user(token: str = Depends(oauth2_user)):
    """Devuelve un objeto con atributo .id basado en el JWT público."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = int(payload.get("sub", ""))
        return SimpleNamespace(id=uid)
    except (ValueError, JWTError):
        raise HTTPException(401, "Token de usuario inválido o expirado")

# ─────────────── Utilidades BD ───────────────
def get_db_connection():
    return psycopg2.connect(**DB_PARAMS)

def get_admin_id_by_email(email: str) -> Optional[int]:
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row[0] if row else None

# ─────────────── Embeddings (OpenAI) ───────────────
def generate_embedding(text: str) -> Optional[List[float]]:
    try:
        resp = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            },
            json={"model": "text-embedding-ada-002", "input": text},
            timeout=20,
        ).json()
        return resp["data"][0]["embedding"]
    except Exception:
        traceback.print_exc(); return None

# ─────────────── Columnas opcionales ───────────────
def job_has_column(cur, col: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'Job' AND column_name = %s
         LIMIT 1
    """, (col,))
    return bool(cur.fetchone())

# ══════════════════ Endpoints públicos ══════════════════
@router.get("/", summary="Listar ofertas activas")
async def list_jobs():
    """Devuelve todas las ofertas no expiradas, ordenadas por id DESC."""
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT id, title, description, requirements,
               "expirationDate", "userId", source, label
          FROM public."Job"
         WHERE "expirationDate" IS NULL OR "expirationDate" > NOW()
         ORDER BY id DESC
    """)
    cols   = [c[0] for c in cur.description]
    offers = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close(); conn.close()
    return {"offers": offers}

@router.get("/list", summary="Alias legado para listado")
async def list_jobs_alias():
    return await list_jobs()

@router.get("/my-applications", summary="Postulaciones del usuario")
async def my_applications(current_user = Depends(get_current_user)):
    conn = get_db_connection(); cur = conn.cursor()
    cur.execute("""
        SELECT id,
               job_id AS "jobId",
               status,
               created_at AS "createdAt"
          FROM proposals
         WHERE user_id = %s
         ORDER BY created_at DESC
    """, (current_user.id,))
    cols = [c[0] for c in cur.description]
    apps = [dict(zip(cols, row)) for row in cur.fetchall()]
    cur.close(); conn.close()
    return {"applications": apps}

# ══════════════════ Endpoint admin: crear oferta ══════════════════
@router.post("/create-admin",
             status_code=201,
             dependencies=[Depends(oauth2_admin)])
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
    is_paid       =  bool(data.get("isPaid", False))
    contact_email =  data.get("contactEmail") or data.get("contact_email")
    contact_phone =  data.get("contactPhone") or data.get("contact_phone")

    if not title or not description:
        raise HTTPException(400, "title y description son obligatorios")
    if source == "admin" and not contact_email:
        raise HTTPException(400, "Las ofertas del administrador requieren contactEmail")

    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "expirationDate inválida (ISO-8601)")

    try:
        user_id = int(raw_user_id)
    except (TypeError, ValueError):
        user_id = get_admin_id_by_email(admin_sub) or 0
    if not user_id:
        raise HTTPException(400, "No se encontró admin en User")

    embedding = generate_embedding(f"{title}\n{description}\n{requirements}")

    conn = get_db_connection(); cur = conn.cursor()
    try:
        has_is_paid       = job_has_column(cur, "is_paid")
        has_snake_contact = job_has_column(cur, "contact_email")
        has_camel_contact = job_has_column(cur, "contactEmail")

        email_col = phone_col = None
        if has_snake_contact:
            email_col, phone_col = "contact_email", "contact_phone"
        elif has_camel_contact:
            email_col, phone_col = "contactEmail", "contactPhone"

        fields = [
            "title", "description", "requirements", '"expirationDate"',
            '"userId"', "embedding", "label", "source",
        ]
        values = [
            title, description, requirements, exp_date,
            user_id, embedding, label, source,
        ]
        if has_is_paid:
            fields.append("is_paid"); values.append(is_paid)
        if email_col:
            fields.extend([email_col, phone_col])
            values.extend([contact_email, contact_phone])

        placeholders = ", ".join(["%s"] * len(fields))
        cur.execute(
            f'INSERT INTO "Job" ({", ".join(fields)}) VALUES ({placeholders}) RETURNING id;',
            tuple(values)
        )
        job_id = cur.fetchone()[0]
        conn.commit()
    except Exception as exc:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500, f"Error interno al crear oferta: {exc}")
    finally:
        cur.close(); conn.close()

    threading.Thread(target=run_matching_for_job,
                     args=(job_id,), daemon=True).start()

    return {
        "message": "Oferta creada",
        "jobId":        job_id,
        "label":        label,
        "isPaid":       is_paid,
        "contactEmail": contact_email,
        "contactPhone": contact_phone,
    }
