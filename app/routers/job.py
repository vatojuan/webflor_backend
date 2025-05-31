# app/routers/job.py
"""
Endpoints Job
• GET  /api/job/                – ofertas vigentes
• GET  /api/job/list            – alias legacy
• GET  /api/job/my-applications – postulaciones del usuario
• POST /api/job/create-admin    – alta admin + matching
"""

from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from psycopg2.extensions import connection  # solo tipado

from app.database import get_db_connection  # conexión única
from app.routers.match import run_matching_for_job

load_dotenv()

# ─────────────── JWT ───────────────
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
oauth2_user = OAuth2PasswordBearer(tokenUrl="/auth/login")

router = APIRouter(prefix="/api/job", tags=["job"])


# ─────────────── Auth helpers ───────────────
def get_current_admin_sub(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])["sub"]
    except (JWTError, KeyError):
        raise HTTPException(401, "Token inválido o expirado (admin)")


def get_current_user(token: str = Depends(oauth2_user)):
    try:
        decoded = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        uid = decoded.get("sub")
        if not uid:
            raise ValueError("Sin sub en el token")
        return SimpleNamespace(id=int(uid))
    except (JWTError, ValueError):
        raise HTTPException(401, "Token de usuario inválido o expirado")


# ─────────────── DB helpers ───────────────
def get_admin_id_by_email(email: str) -> Optional[int]:
    conn: connection = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()


def job_has_column(cur, col: str) -> bool:
    cur.execute(
        """
        SELECT 1
          FROM information_schema.columns
         WHERE table_name = 'Job'
           AND column_name = %s
         LIMIT 1
        """,
        (col,),
    )
    return bool(cur.fetchone())


# ─────────────── OpenAI embedding ───────────────
def generate_embedding(text: str) -> Optional[List[float]]:
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            },
            json={"model": "text-embedding-ada-002", "input": text},
            timeout=20,
        ).json()
        return r["data"][0]["embedding"]
    except Exception:
        traceback.print_exc()
        return None


# ══════════ Endpoints públicos ══════════

@router.get("/", summary="Listar ofertas activas")
@router.get("/list", include_in_schema=False)  # alias legacy
async def list_jobs(userId: Optional[int] = None):
    """
    Devuelve todas las ofertas no expiradas, ordenadas por id DESC.
    Si se pasa userId, filtra solo las ofertas de ese empleador.
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        if userId is not None:
            cur.execute(
                """
                SELECT
                  id,
                  title,
                  description,
                  requirements,
                  "expirationDate",
                  "userId",
                  source,
                  label
                FROM public."Job"
                WHERE ("expirationDate" IS NULL OR "expirationDate" > NOW())
                  AND "userId" = %s
                ORDER BY id DESC
                """,
                (userId,),
            )
        else:
            cur.execute(
                """
                SELECT
                  id,
                  title,
                  description,
                  requirements,
                  "expirationDate",
                  "userId",
                  source,
                  label
                FROM public."Job"
                WHERE "expirationDate" IS NULL OR "expirationDate" > NOW()
                ORDER BY id DESC
                """
            )
        cols = [d[0] for d in cur.description]
        offers = [dict(zip(cols, row)) for row in cur.fetchall()]
        return {"offers": offers}
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Error interno al obtener las ofertas")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.get("/my-applications", summary="Postulaciones del usuario")
async def my_applications(current_user=Depends(get_current_user)):
    """
    Devuelve las propuestas que el usuario autenticado ha realizado.
    Requiere:
      Authorization: Bearer <token de usuario>
    """
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
              id,
              job_id AS "jobId",
              status,
              created_at AS "createdAt"
            FROM proposals
            WHERE user_id = %s
            ORDER BY created_at DESC
            """,
            (current_user.id,),
        )
        cols = [d[0] for d in cur.description]
        apps = [dict(zip(cols, row)) for row in cur.fetchall()]
        return {"applications": apps}
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Error interno al obtener las postulaciones")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ══════════ Alta admin + matching ══════════
@router.post(
    "/create-admin",
    status_code=201,
    dependencies=[Depends(oauth2_admin)],
)
async def create_admin_job(
    request: Request, admin_sub: str = Depends(get_current_admin_sub)
):
    """
    Crea una oferta nueva, genera su embedding y dispara matching asíncrono.
    Sólo accesible para admins (usa OAuth2 admin-login).
    """
    data = await request.json()
    title = (data.get("title") or "").strip()
    desc = (data.get("description") or "").strip()
    if not title or not desc:
        raise HTTPException(400, "title y description son obligatorios")

    reqs = (data.get("requirements") or "").strip()
    expiration = data.get("expirationDate")
    label = data.get("label", "manual")
    source = data.get("source", "admin")
    is_paid = bool(data.get("isPaid", False))
    contact_email = data.get("contactEmail") or data.get("contact_email")
    contact_phone = data.get("contactPhone") or data.get("contact_phone")

    if source == "admin" and not contact_email:
        raise HTTPException(400, "Las ofertas del administrador requieren contactEmail")

    exp_date = None
    if expiration:
        try:
            exp_date = datetime.fromisoformat(expiration.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, "expirationDate inválida")

    raw_uid = data.get("userId")
    try:
        user_id = int(raw_uid)
    except Exception:
        user_id = get_admin_id_by_email(admin_sub)
        if not user_id:
            raise HTTPException(400, "No se encontró admin en User")

    embedding = generate_embedding(f"{title}\n{desc}\n{reqs}")

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        has_is_paid = job_has_column(cur, "is_paid")
        has_snake_contact = job_has_column(cur, "contact_email")
        has_camel_contact = job_has_column(cur, "contactEmail")
        email_col = phone_col = None
        if has_snake_contact:
            email_col, phone_col = "contact_email", "contact_phone"
        elif has_camel_contact:
            email_col, phone_col = "contactEmail", "contactPhone"

        fields = [
            "title",
            "description",
            "requirements",
            '"expirationDate"',
            '"userId"',
            "embedding",
            "label",
            "source",
        ]
        values = [title, desc, reqs, exp_date, user_id, embedding, label, source]
        if has_is_paid:
            fields.append("is_paid")
            values.append(is_paid)
        if email_col:
            fields.extend([email_col, phone_col])
            values.extend([contact_email, contact_phone])

        ph = ", ".join(["%s"] * len(fields))
        cur.execute(
            f'INSERT INTO "Job" ({", ".join(fields)}) VALUES ({ph}) RETURNING id;',
            tuple(values),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
    except Exception as exc:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, f"Error interno al crear oferta: {exc}")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

    # Disparar matching en segundo plano (no bloquea la respuesta)
    threading.Thread(
        target=run_matching_for_job, args=(job_id,), daemon=True
    ).start()

    return {"message": "Oferta creada", "jobId": job_id}
