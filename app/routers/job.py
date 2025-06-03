# app/routers/job.py
"""
Ofertas de empleo
────────────────────────────────────────────────────────────
• GET  /api/job/                – ofertas vigentes
• GET  /api/job/list            – alias legacy
• GET  /api/job/my-applications – postulaciones del usuario
• POST /api/job/create          – alta de oferta por EMPLEADOR
• POST /api/job/create-admin    – alta de oferta por ADMIN
"""

from __future__ import annotations

import os, threading, traceback
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional, Tuple, Dict, Any

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from psycopg2.extensions import connection  # tipado

from app.database      import get_db_connection
from app.routers.match import run_matching_for_job   # ⚠️ debe existir

# ───────────────────────────────────────────────────────
load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
oauth2_user  = OAuth2PasswordBearer(tokenUrl="/auth/login")

router = APIRouter(prefix="/api/job", tags=["job"])


# ─────────────────── Auth helpers ────────────────────
def _decode(token: str) -> Dict[str,Any]:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])

def get_current_admin_sub(tok: str = Depends(oauth2_admin)) -> str:
    try:
        return _decode(tok)["sub"]
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token admin inválido")

def get_current_user(tok: str = Depends(oauth2_user)):
    try:
        uid = _decode(tok).get("sub")
        return SimpleNamespace(id=int(uid))
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token usuario inválido")


# ─────────────────── DB helpers ──────────────────────
def get_admin_id_by_email(mail: str) -> Optional[int]:
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (mail,))
        row = cur.fetchone(); return row[0] if row else None
    finally:
        cur.close(); conn.close()

def job_has_column(cur, col: str) -> bool:
    cur.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name='Job' AND column_name=%s LIMIT 1
    """, (col,))
    return bool(cur.fetchone())


# ─────────────────── Embeddings ───────────────────────
def generate_embedding(txt: str) -> Optional[List[float]]:
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            },
            json={"model": "text-embedding-ada-002", "input": txt},
            timeout=20,
        ).json()
        return r["data"][0]["embedding"]
    except Exception:
        traceback.print_exc()
        return None


# ═══════════ Helpers comunes (inserción + matching) ═══════════
def _insert_job(
    payload: Dict[str,Any],
    owner_id: int,
    source: str,
    label_default: str = "manual"
) -> Tuple[int,str]:
    """
    Inserta una oferta y dispara matching.
    Devuelve (job_id, contact_email) sólo para logging/uso futuro.
    """
    title  = (payload.get("title")       or "").strip()
    desc   = (payload.get("description") or "").strip()
    reqs   = (payload.get("requirements")or "").strip()

    if not title or not desc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "title y description son obligatorios"
        )

    expiration = payload.get("expirationDate")
    try:
        exp_dt = datetime.fromisoformat(
            expiration.replace("Z","+00:00")
        ) if expiration else None
    except ValueError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "expirationDate inválida"
        )

    label         = payload.get("label", label_default)
    is_paid       = bool(payload.get("isPaid", False))
    contact_email = payload.get("contactEmail") or payload.get("contact_email")
    contact_phone = payload.get("contactPhone") or payload.get("contact_phone")

    # Si el empleador no envía contacto, usamos su propio mail/phone
    if not contact_email or not contact_phone:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner_id,))
        mail_fallback, phone_fallback = cur.fetchone() or ("","")
        cur.close(); conn.close()
        contact_email = contact_email or mail_fallback
        contact_phone = contact_phone or phone_fallback

    embedding = generate_embedding(f"{title}\n{desc}\n{reqs}")

    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()

        has_is_paid       = job_has_column(cur, "is_paid")
        has_snake_contact = job_has_column(cur, "contact_email")
        has_camel_contact = job_has_column(cur, "contactEmail")

        email_col = phone_col = None
        if   has_snake_contact: email_col, phone_col = "contact_email", "contact_phone"
        elif has_camel_contact: email_col, phone_col = "contactEmail", "contactPhone"

        fields = [
            "title","description","requirements",
            '"expirationDate"','"userId"',"embedding",
            "label","source"
        ]
        values = [title, desc, reqs, exp_dt, owner_id, embedding, label, source]

        if has_is_paid:
            fields.append("is_paid"); values.append(is_paid)
        if email_col:
            fields.extend([email_col, phone_col])
            values.extend([contact_email, contact_phone])

        ph = ", ".join(["%s"] * len(fields))
        cur.execute(
            f'INSERT INTO "Job" ({", ".join(fields)}) VALUES ({ph}) RETURNING id;',
            tuple(values)
        )
        job_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "Error al crear oferta"
        )
    finally:
        if cur: cur.close()
        if conn: conn.close()

    # matching en background
    threading.Thread(
        target=run_matching_for_job,
        args=(job_id,),
        daemon=True
    ).start()

    return job_id, contact_email or ""


# ═════════════ LISTADO / APLICACIONES ═════════════
@router.get("/", summary="Listar ofertas activas")
@router.get("/list", include_in_schema=False)
async def list_jobs(userId: Optional[int] = None):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        has_source = job_has_column(cur, "source")
        has_label  = job_has_column(cur, "label")

        base = ['id','title','description','requirements',
                '"expirationDate"','"userId"']
        sel  = base + [
            ("source" if has_source else "NULL AS source"),
            ("label" if has_label else "NULL AS label")
        ]

        sql    = f'SELECT {",".join(sel)} FROM public."Job" ' \
                 'WHERE "expirationDate" IS NULL OR "expirationDate" > NOW()'
        params = ()
        if userId is not None:
            sql    += ' AND "userId"=%s'
            params  = (userId,)

        sql += ' ORDER BY id DESC'
        cur.execute(sql, params)

        cols = [d[0] for d in cur.description]
        return {"offers": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/my-applications", summary="Postulaciones del usuario")
async def my_applications(current_user=Depends(get_current_user)):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT
              id,
              job_id          AS "jobId",
              status,
              created_at      AS "createdAt"
            FROM proposals
            WHERE applicant_id = %s                     -- CORRECCIÓN: usar applicant_id
              AND status NOT IN ('cancelled','rejected')
            ORDER BY created_at DESC
        """, (current_user.id,))
        cols = [d[0] for d in cur.description]
        return {"applications": [dict(zip(cols, r)) for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ═════════════ CREACIÓN POR EMPLEADOR ═════════════
@router.post(
    "/create",
    status_code=status.HTTP_201_CREATED,
    summary="Crear oferta (empleador)"
)
async def create_job(
    data: Dict[str,Any],
    current_user = Depends(get_current_user)
):
    """
    Crea una oferta en nombre del empleador autenticado (`role` empleado/empleador).
    • source='employer'
    • label  por defecto 'manual'
    Se genera embedding y se dispara matching automático.
    """
    job_id, _ = _insert_job(
        data,
        owner_id=current_user.id,
        source="employer"
    )
    return {"message": "Oferta creada", "jobId": job_id}


# ═════════════ CREACIÓN POR ADMIN ═════════════
@router.post(
    "/create-admin",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(oauth2_admin)],
    summary="Crear oferta (admin)"
)
async def create_admin_job(
    request: Request,
    admin_sub: str = Depends(get_current_admin_sub)
):
    data = await request.json()

    # Determinar owner (puede venir userId o lo buscamos por mail admin)
    raw_uid = data.get("userId")
    try:
        owner_id = int(raw_uid) if raw_uid else None
    except:
        owner_id = None

    if not owner_id:
        owner_id = get_admin_id_by_email(admin_sub)
        if not owner_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Admin sin usuario asociado"
            )

    # Admin puede indicar libremente source/label/isPaid/contact*
    job_id, _ = _insert_job(
        data,
        owner_id=owner_id,
        source=data.get("source","admin"),
        label_default=data.get("label","manual")
    )
    return {"message": "Oferta creada", "jobId": job_id}
