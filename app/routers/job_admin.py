# app/routers/job_admin.py

"""
Administración de Ofertas de Empleo
Prefijo: /api/admin/job
• GET    /api/admin/job/offers       – listar todas las ofertas (filtrado de expiradas según configuración)
• PUT    /api/admin/job/update       – actualizar una oferta
• DELETE /api/admin/job/delete       – borrar oferta y sus propuestas
"""

from __future__ import annotations
import os, traceback
from datetime import datetime, timezone
from typing import Dict, Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY    = os.getenv("SECRET_KEY", "")
ALGORITHM     = os.getenv("ALGORITHM", "HS256")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(sub_token: str = Depends(oauth2_scheme)) -> str:
    if not sub_token:
        raise HTTPException(401, "Token no proporcionado")
    try:
        return jwt.decode(sub_token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido o expirado")

def get_db_connection():
    try:
        return psycopg2.connect(
            dbname   = os.getenv("DBNAME"),
            user     = os.getenv("USER"),
            password = os.getenv("PASSWORD"),
            host     = os.getenv("HOST"),
            port     = int(os.getenv("DB_PORT", 5432)),
            sslmode  = "require",
        )
    except Exception as e:
        raise HTTPException(500, f"Error conexión BD: {e}")

def get_admin_config() -> Dict[str, bool]:
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("SELECT key, value FROM admin_config;")
        return {k: v.lower() == "true" for k, v in cur.fetchall()}
    except Exception:
        traceback.print_exc()
        return {}
    finally:
        if cur: cur.close()
        if conn: conn.close()

def get_admin_id(email: str) -> Optional[int]:
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (email,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        if cur: cur.close()
        if conn: conn.close()

router = APIRouter(
    prefix="/api/admin/job",
    tags=["job_admin"],
    dependencies=[Depends(get_current_admin)],
)

@router.get("/offers")
def list_admin_offers(admin_sub: str = Depends(get_current_admin)):
    cfg      = get_admin_config()
    show_adm = cfg.get("show_expired_admin_offers", False)
    show_emp = cfg.get("show_expired_employer_offers", False)
    adm_id   = get_admin_id(admin_sub)

    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT id,title,description,requirements,
                   "expirationDate","userId",source,label,
                   contact_email   AS "contactEmail",
                   contact_phone   AS "contactPhone"
              FROM public."Job"
             ORDER BY id DESC;
            """
        )
        cols = [d[0] for d in cur.description]
        now  = datetime.now(timezone.utc)
        out  = []
        for row in cur.fetchall():
            o = dict(zip(cols, row))
            exp = o["expirationDate"]
            expired = False
            if exp:
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                expired = exp < now
                o["expirationDate"] = exp.isoformat()
            if expired:
                is_own = adm_id and o["userId"] == adm_id
                if (is_own and not show_adm) or (not is_own and not show_emp):
                    continue
            out.append(o)
        return {"offers": out}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Error al obtener ofertas: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.put("/update")
async def update_admin_offer(request: Request):
    body = await request.json()
    job_id      = int(body.get("id") or 0)
    title       = body.get("title")
    description = body.get("description")
    requirements= body.get("requirements", "")
    exp_str     = body.get("expirationDate")
    user_id     = int(body.get("userId") or 0)
    contact_e   = body.get("contactEmail") or body.get("contact_email")
    contact_p   = body.get("contactPhone") or body.get("contact_phone")
    source      = body.get("source", "admin")
    label       = body.get("label",  "automatic")

    if not (job_id and title and description and user_id):
        raise HTTPException(400, "Faltan campos obligatorios")
    if source == "admin" and not contact_e:
        raise HTTPException(400, "contactEmail requerido para admin")

    exp_date = None
    if exp_str:
        try:
            exp_date = datetime.fromisoformat(exp_str)
        except ValueError:
            raise HTTPException(400, "expirationDate inválida")

    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            UPDATE public."Job"
               SET title=%s, description=%s, requirements=%s,
                   "expirationDate"=%s, "userId"=%s,
                   source=%s, label=%s,
                   contact_email=%s, contact_phone=%s
             WHERE id=%s
         RETURNING id,title,description,requirements,
                   "expirationDate","userId",source,label,
                   contact_email AS "contactEmail",
                   contact_phone AS "contactPhone";
            """,
            (title,description,requirements,exp_date,user_id,
             source,label,contact_e,contact_p,job_id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Oferta no encontrada")
        conn.commit()
        keys = [
            "id","title","description","requirements",
            "expirationDate","userId","source","label",
            "contactEmail","contactPhone"
        ]
        out = dict(zip(keys, row))
        if out["expirationDate"]:
            out["expirationDate"] = out["expirationDate"].isoformat()
        return out
    except HTTPException:
        raise
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(500, f"Error al actualizar: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.delete("/delete")
async def delete_admin_offer(request: Request):
    body   = await request.json()
    job_id = int(body.get("jobId") or 0)
    if not job_id:
        raise HTTPException(400, "jobId es requerido")

    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        # cancelar propuestas
        cur.execute(
            "UPDATE proposals SET status='cancelled', cancelled_at=NOW() "
            "WHERE job_id=%s AND status IN ('waiting','pending');",
            (job_id,),
        )
        # borrar propuestas
        cur.execute("DELETE FROM proposals WHERE job_id=%s;", (job_id,))
        # borrar oferta
        cur.execute('DELETE FROM public."Job" WHERE id=%s RETURNING id;', (job_id,))
        if not cur.fetchone():
            raise HTTPException(404, "Oferta no encontrada")
        conn.commit()
        return {"message": "Oferta y propuestas eliminadas", "jobId": job_id}
    except HTTPException:
        raise
    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, f"Error al eliminar oferta: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()
