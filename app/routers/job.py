# app/routers/job.py

"""
Ofertas de empleo
────────────────────────────────────────────────────────────
• GET    /api/job/                      – listar ofertas vigentes (con candidatesCount)
• GET    /api/job/list                  – alias legacy
• GET    /api/job/my-applications       – postulaciones del usuario (con objeto job detallado)
• GET    /api/job/apply/{token}         – confirma enlace y crea postulación
• POST   /api/job/apply                 – postularse a una oferta directamente
• DELETE /api/job/cancel-application    – cancelar postulación
• GET    /api/job/{job_id}              – detalles de una oferta (con createdAt y expirationDate)
• POST   /api/job/create                – alta de oferta por EMPLEADOR
• POST   /api/job/create-admin          – alta de oferta por ADMIN
"""

from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime
from types import SimpleNamespace
from typing import List, Optional, Tuple, Dict, Any
from pgvector.psycopg2 import register_vector


import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, Path, status
from fastapi.security import OAuth2PasswordBearer, HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt

from app.database import get_db_connection
from app.routers.match import run_matching_for_job
from app.routers.proposal import deliver

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
oauth2_user = HTTPBearer()

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(oauth2_user)):
    token = credentials.credentials
    sub = _decode(token).get("sub")
    if not sub:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token usuario inválido")
    return SimpleNamespace(id=int(sub))

router = APIRouter(prefix="/api/job", tags=["job"])


# ─────────────────── Auth helpers ────────────────────
def _decode(token: str) -> Dict[str, Any]:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")


def get_current_admin_sub(tok: str = Depends(oauth2_admin)) -> str:
    return _decode(tok).get("sub", "")


# ─────────────────── DB helpers ──────────────────────
def get_admin_id_by_email(mail: str) -> Optional[int]:
    conn = get_db_connection()
    register_vector(conn)
    cur  = conn.cursor()
    try:
        cur.execute('SELECT id FROM "User" WHERE email=%s LIMIT 1;', (mail,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()


def job_has_column(cur, col: str) -> bool:
    cur.execute(
        """
        SELECT 1 FROM information_schema.columns
         WHERE table_schema='public'
           AND table_name='Job'
           AND column_name=%s
         LIMIT 1
        """,
        (col,),
    )
    return bool(cur.fetchone())


# ─────────────────── Embeddings ───────────────────────
def generate_embedding(txt: str) -> Optional[List[float]]:
    try:
        r = requests.post(
            "https://api.openai.com/v1/embeddings",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY','')}",
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
    payload: Dict[str, Any],
    owner_id: int,
    source: str,
    label_default: str = "manual",
) -> Tuple[int, str]:
    title = (payload.get("title") or "").strip()
    desc  = (payload.get("description") or "").strip()
    reqs  = (payload.get("requirements") or "").strip()

    if not title or not desc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "title y description son obligatorios")

    expiration = payload.get("expirationDate")
    try:
        exp_dt = datetime.fromisoformat(expiration.replace("Z", "+00:00")) if expiration else None
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expirationDate inválida")

    label         = payload.get("label", label_default)
    is_paid       = bool(payload.get("isPaid", False))
    contact_email = payload.get("contactEmail") or payload.get("contact_email")
    contact_phone = payload.get("contactPhone") or payload.get("contact_phone")

    if not contact_email or not contact_phone:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('SELECT email, phone FROM "User" WHERE id=%s', (owner_id,))
        mail_fb, phone_fb = cur.fetchone() or ("", "")
        cur.close(); conn.close()
        contact_email = contact_email or mail_fb
        contact_phone = contact_phone or phone_fb

    embedding = generate_embedding(f"{title}\n{desc}\n{reqs}")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

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
            '"userId"', "embedding", "label", "source"
        ]
        values = [title, desc, reqs, exp_dt, owner_id, embedding, label, source]

        if has_is_paid:
            fields.append("is_paid");        values.append(is_paid)
        if email_col:
            fields.extend([email_col, phone_col]); values.extend([contact_email, contact_phone])

        ph = ", ".join(["%s"] * len(fields))
        cur.execute(
            f'INSERT INTO "Job" ({", ".join(fields)}) VALUES ({ph}) RETURNING id;',
            tuple(values),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Error al crear oferta")
    finally:
        if cur: cur.close()
        if conn: conn.close()

    # lanzar matching en background
    threading.Thread(target=run_matching_for_job, args=(job_id,), daemon=True).start()
    return job_id, contact_email or ""


# ═════════════ RUTAS FIJAS ─═══════════════

@router.get("/my-applications", summary="Postulaciones del usuario")
async def my_applications(current_user=Depends(get_current_user)):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT
              p.id,
              j.id               AS job_id,
              j.title            AS job_title,
              j."createdAt"      AS job_created_at,
              j."expirationDate" AS job_expiration_at,
              COUNT(p2.*) FILTER (WHERE p2.status NOT IN ('cancelled','rejected')) AS job_candidates_count,
              p.label,
              p.status,
              p.created_at       AS applied_at
            FROM proposals p
            JOIN "Job" j           ON j.id = p.job_id
            LEFT JOIN proposals p2 ON p2.job_id = j.id
            WHERE p.applicant_id = %s
              AND p.status NOT IN ('cancelled','rejected')
            GROUP BY p.id, j.id, j."createdAt", j."expirationDate"
            ORDER BY p.created_at DESC
            """,
            (current_user.id,),
        )

        applications = []
        for (
            pid,
            jid,
            jtitle,
            jcreated,
            jexp,
            jcount,
            plabel,
            pstatus,
            papplied
        ) in cur.fetchall():
            job_obj = {
                "id": jid,
                "title": jtitle,
                "createdAt": jcreated.isoformat() if jcreated else None,
                "expirationDate": jexp.isoformat() if jexp else None,
                "candidatesCount": jcount
            }
            applications.append({
                "id": pid,
                "label": plabel,
                "status": pstatus,
                "createdAt": papplied.isoformat() if papplied else None,
                "job": job_obj
            })

        return {"applications": applications}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/list", include_in_schema=False, summary="Alias legacy de list()")
@router.get("/", summary="Listar ofertas activas con conteo de postulaciones")
async def list_jobs(userId: Optional[int] = None):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT
              j.id,
              j.title,
              j.description,
              j.requirements,
              j."createdAt",
              j."expirationDate",
              j."userId",
              COALESCE(j.source, '') AS source,
              COALESCE(j.label,  '') AS label,
              COUNT(p.*) FILTER (WHERE p.status NOT IN ('cancelled','rejected')) AS "candidatesCount"
            FROM "Job" j
            LEFT JOIN proposals p ON p.job_id = j.id
            WHERE j."expirationDate" IS NULL OR j."expirationDate" > NOW()
            """ + (' AND j."userId"=%s' if userId else '') + """
            GROUP BY j.id, j."createdAt", j."expirationDate"
            ORDER BY j.id DESC
            """,
            (userId,) if userId else (),
        )

        cols   = [d[0] for d in cur.description]
        offers = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            if d.get("createdAt"):
                d["createdAt"] = d["createdAt"].isoformat()
            if d.get("expirationDate"):
                d["expirationDate"] = d["expirationDate"].isoformat()
            offers.append(d)

        return {"offers": offers}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/apply/{token}", summary="Confirma y crea postulación vía enlace")
async def confirm_apply(token: str = Path(..., description="Token enviado por email")):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()

        # Buscar el token en la tabla matches
        cur.execute("""
            SELECT id, job_id, applicant_id, apply_token_used
              FROM matches
             WHERE apply_token = %s
             LIMIT 1
        """, (token,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Token inválido o expirado")

        match_id, job_id, applicant_id, used = row

        if used:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Este enlace ya fue utilizado")

        cur.execute("""
            INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
            VALUES (%s, %s, 'automatic', 'waiting', NOW())
            RETURNING id
        """, (job_id, applicant_id))
        pid = cur.fetchone()[0]

        cur.execute("""
            UPDATE matches
               SET apply_token_used = TRUE
             WHERE id = %s
        """, (match_id,))

        conn.commit()

        threading.Thread(target=deliver, args=(pid, True), daemon=True).start()

        jwt_user = jwt.encode({"sub": str(applicant_id), "role": "empleado"}, SECRET_KEY, algorithm=ALGORITHM)
        return {"success": True, "token": jwt_user, "jobId": job_id}
    except HTTPException:
        raise
    except Exception as e:
        if conn: conn.rollback()
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.delete("/cancel-application", summary="Cancelar la postulación del usuario")
async def cancel_application(
    payload: Dict[str, Any],
    current_user=Depends(get_current_user),
):
    job_id = payload.get("jobId")
    if not job_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Falta jobId")
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT id
              FROM proposals
             WHERE job_id=%s AND applicant_id=%s AND status NOT IN ('cancelled','rejected')
             LIMIT 1
            """,
            (job_id, current_user.id),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="No existe postulación activa")
        cur.execute("UPDATE proposals SET status='cancelled', cancelled_at=NOW() WHERE id=%s", (row[0],))
        conn.commit()
        return {"message": "Postulación cancelada"}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/{job_id}", summary="Obtener detalles de una oferta")
async def get_job(job_id: int = Path(..., description="ID de la oferta")):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT
              j.id,
              j.title,
              j.description,
              j.requirements,
              j."createdAt",
              j."expirationDate",
              j."userId",
              COUNT(p.*) FILTER (WHERE p.status NOT IN ('cancelled','rejected')) AS "candidatesCount"
            FROM "Job" j
            LEFT JOIN proposals p ON p.job_id = j.id
            WHERE j.id = %s
            GROUP BY j.id, j."createdAt", j."expirationDate"
            """,
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Oferta no encontrada")
        cols = [d[0] for d in cur.description]
        job = dict(zip(cols, row))
        if job.get("createdAt"):
            job["createdAt"] = job["createdAt"].isoformat()
        if job.get("expirationDate"):
            job["expirationDate"] = job["expirationDate"].isoformat()
        return {"job": job}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.post("/create", status_code=status.HTTP_201_CREATED, summary="Crear oferta (empleador)")
async def create_job(data: Dict[str, Any], current_user=Depends(get_current_user)):
    job_id, _ = _insert_job(data, owner_id=current_user.id, source="employer")
    return {"message": "Oferta creada", "jobId": job_id}

@router.delete("/delete/{job_id}", summary="Eliminar una oferta (empleador)")
async def delete_job(
    job_id: int = Path(..., description="ID de la oferta"),
    current_user=Depends(get_current_user)
):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Verificar que la oferta le pertenece al usuario
        cur.execute('SELECT id FROM "Job" WHERE id=%s AND "userId"=%s', (job_id, current_user.id))
        if not cur.fetchone():
            raise HTTPException(status_code=403, detail="No tienes permiso para eliminar esta oferta")

        # Eliminar la oferta
        cur.execute('DELETE FROM "Job" WHERE id = %s', (job_id,))
        conn.commit()
        return {"message": "Oferta eliminada correctamente"}
    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.rollback()
        raise HTTPException(status_code=500, detail="Error al eliminar oferta")
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.post(
    "/create-admin",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(oauth2_admin)],
    summary="Crear oferta (admin)",
)
async def create_admin_job(request: Request, admin_sub: str = Depends(get_current_admin_sub)):
    data    = await request.json()
    raw_uid = data.get("userId")
    try:
        owner_id = int(raw_uid) if raw_uid else None
    except:
        owner_id = None
    if not owner_id:
        owner_id = get_admin_id_by_email(admin_sub)
        if not owner_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Admin sin usuario asociado")
    job_id, _ = _insert_job(
        data,
        owner_id=owner_id,
        source=data.get("source", "admin"),
        label_default=data.get("label", "manual"),
    )
    return {"message": "Oferta creada", "jobId": job_id}

@router.post("/apply", status_code=status.HTTP_201_CREATED, summary="Postularse a una oferta")
async def apply_to_job(request: Request, current_user=Depends(get_current_user)):
    data = await request.json()
    job_id = data.get("jobId")
    if not job_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Falta jobId")

    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # Verificar si ya existe una propuesta para este job y usuario
        cur.execute("""
            SELECT id, label FROM proposals
             WHERE job_id = %s AND applicant_id = %s
             LIMIT 1
        """, (job_id, current_user.id))
        row = cur.fetchone()

        if row:
            pid, label = row
            if label == "automatic":
                cur.execute("""
                    UPDATE proposals
                       SET status='waiting', cancelled_at=NULL, created_at=NOW()
                     WHERE id = %s
                     RETURNING id
                """, (pid,))
                conn.commit()

                # Relanzar entrega automática
                threading.Thread(target=deliver, args=(pid, True), daemon=True).start()
                return {"message": "Reactivada como automática", "proposalId": pid}
            else:
                cur.execute("""
                    UPDATE proposals
                       SET status='pending', cancelled_at=NULL, created_at=NOW()
                     WHERE id = %s
                     RETURNING id
                """, (pid,))
                conn.commit()
                return {"message": "Reactivada como manual", "proposalId": pid}

        # No existía propuesta previa, determinar tipo de oferta
        cur.execute("""
            SELECT label FROM "Job" WHERE id = %s
        """, (job_id,))
        job_row = cur.fetchone()
        if not job_row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Oferta no encontrada")
        
        job_label = job_row[0] or "manual"

        if job_label == "automatic":
            cur.execute("""
                INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
                VALUES (%s, %s, 'automatic', 'waiting', NOW())
                RETURNING id
            """, (job_id, current_user.id))
            pid = cur.fetchone()[0]
            conn.commit()

            threading.Thread(target=deliver, args=(pid, True), daemon=True).start()
            return {"message": "Postulación automática registrada", "proposalId": pid}
        else:
            cur.execute("""
                INSERT INTO proposals (job_id, applicant_id, label, status, created_at)
                VALUES (%s, %s, 'manual', 'pending', NOW())
                RETURNING id
            """, (job_id, current_user.id))
            pid = cur.fetchone()[0]
            conn.commit()
            return {"message": "Postulación manual registrada", "proposalId": pid}

    except Exception as e:
        if conn:
            conn.rollback()
        traceback.print_exc()
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error al postularse")
    finally:
        if cur: cur.close()
        if conn: conn.close()

