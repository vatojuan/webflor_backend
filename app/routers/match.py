# app/routers/match.py

"""
Matchings (Job ↔ User)

• GET  /api/match/job/{job_id}/match      → preview (no escribe BD)
• GET  /api/match/user/{user_id}/match    → preview inverso
• GET  /api/match/admin                   → panel admin (solo score ≥ 0.80)
• POST /api/match/resend/{mid}            → reenviar mail/WhatsApp

La rutina `run_matching_for_job(job_id)` se invoca al crear o actualizar una oferta.
Calcula los scores (pgvector) y guarda en `matches`. Después, envía automáticamente
e-mails a los candidatos cuyo score ≥ 0.80 usando la plantilla de tipo "empleado".
También genera un `apply_token` único para cada uno y lo guarda en la tabla, de modo
que el candidato se postule con un clic sin login. Los que queden con score < 0.80
permanecen en status='pending' sin envío ni token.

La nueva función `run_matching_for_user(user_id)` elimina todos los matchings actuales
para ese usuario, calcula su score frente a todas las ofertas con embedding y guarda
esos resultados en `matches` con status='pending'. 
"""
from __future__ import annotations

import os
import secrets
import logging
import traceback
from typing import Any, Dict, List, Tuple

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import get_db_connection
from app.routers.proposal import send_mail, send_whatsapp

# ─────────────────── Config & auth ───────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://fapmendoza.online")

# Forzamos .online si accidentalmente quedó configurado .com
if FRONTEND_URL.endswith(".com"):
    FRONTEND_URL = "https://fapmendoza.online"

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token admin inválido o requerido")

router = APIRouter(prefix="/api/match", tags=["matchings"])
logger = logging.getLogger(__name__)


# ───────────── helpers util ─────────────
def _cur_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def _get_default_tpl(cur, tpl_type: str) -> Dict[str, str]:
    cur.execute(
        """
        SELECT subject, body
          FROM proposal_templates
         WHERE type = %s AND is_default = TRUE
         LIMIT 1
        """,
        (tpl_type,),
    )
    row = cur.fetchone()
    return {"subject": row[0], "body": row[1]} if row else {}

def _apply_tpl(tpl: Dict[str, str], ctx: Dict[str, str]) -> Dict[str, str]:
    subj, body = tpl.get("subject", ""), tpl.get("body", "")
    for k, v in ctx.items():
        subj = subj.replace(f"{{{{{k}}}}}", v)
        body = body.replace(f"{{{{{k}}}}}", v)
    return {"subject": subj or "(sin asunto)", "body": body}

def _job_contact_columns(cur) -> Tuple[str, str]:
    cur.execute("""
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'Job';
    """)
    cols = {r[0] for r in cur.fetchall()}
    email_c = "contact_email" if "contact_email" in cols else \
              "contactEmail" if "contactEmail" in cols else None
    phone_c = "contact_phone" if "contact_phone" in cols else \
              "contactPhone" if "contactPhone" in cols else None
    return email_c, phone_c


# ═══════════ Matching batch (oferta nueva) ═══════════
def run_matching_for_job(job_id: int) -> None:
    """
    Recalcula todos los matchings Job→User:
      1) Borra previos de esta oferta.
      2) Inserta nuevos con status='pending' y SIN token.
      3) Para cada matching score ≥ 0.80:
         • genera apply_token,
         • envía mail con enlace FRONTEND_URL/apply/{token},
         • marca status='sent', sent_at.
      4) El resto queda 'pending'.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        # 1) Obtener embedding de la oferta
        cur.execute(
            'SELECT embedding FROM "Job" WHERE id = %s AND embedding IS NOT NULL',
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.info("Oferta %d sin embedding", job_id)
            return
        job_emb = row[0]

        # 2) Borrar previos
        cur.execute("DELETE FROM matches WHERE job_id = %s", (job_id,))
        conn.commit()

        # 3) Insertar nuevos pendientes
        cur.execute(
            """
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT
              %s,
              u.id,
              (1.0 - (u.embedding::vector <=> %s::vector)),
              'pending'
            FROM "User" u
            WHERE u.embedding IS NOT NULL
            """,
            (job_id, job_emb),
        )
        conn.commit()

        # 4) Envío a los top ≥ 0.80
        cur.execute(
            """
            SELECT m.id, m.score,
                   u.id, u.name, u.email, u."cvUrl",
                   j.title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.job_id = %s AND m.score >= %s
            """,
            (job_id, 0.80),
        )
        high = cur.fetchall()
        tpl_emp = _get_default_tpl(cur, "empleado")

        for m_id, score, u_id, name, email, cv, title in high:
            if not email:
                continue
            token      = secrets.token_urlsafe(32)
            apply_link = f"{FRONTEND_URL}/apply/{token}"

            # Guardar token en matches
            cur.execute(
                "UPDATE matches SET apply_token = %s WHERE id = %s",
                (token, m_id),
            )

            # Preparar y enviar mail
            ctx = {
                "applicant_name": name,
                "job_title":      title,
                "cv_url":         cv or "",
                "score":          f"{round(score*100,1)}%",
                "apply_link":     apply_link,
                "created_at":     "",
            }
            msg = _apply_tpl(tpl_emp, ctx)
            try:
                send_mail(email, msg["subject"], msg["body"], cv)
                cur.execute(
                    "UPDATE matches SET status='sent', sent_at=NOW() WHERE id = %s",
                    (m_id,),
                )
            except Exception:
                logger.exception("Error enviando match %d", m_id)

        conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("run_matching_for_job error")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═══════════ Matching batch (usuario nuevo o CV actualizado) ═══════════
def run_matching_for_user(user_id: int) -> None:
    """
    Recalcula User→Job:
      1) Borra previos.
      2) Inserta nuevos pendientes.
    No envía mails aquí.
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM matches WHERE user_id = %s", (user_id,))

        cur.execute(
            'SELECT embedding FROM "User" WHERE id = %s AND embedding IS NOT NULL',
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.info("Usuario %d sin embedding", user_id)
            return
        user_emb = row[0]

        cur.execute(
            """
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT j.id, %s,
                   (1.0 - (j.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
            """,
            (user_id, user_emb),
        )
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("run_matching_for_user error")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


# ═══════════ Panel admin + resend + previews ═══════════
@router.get(
    "/admin",
    dependencies=[Depends(get_current_admin)],
    summary="Listado de matchings guardados (solo score ≥ 0.80)",
)
def list_matchings():
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT m.id,
                   m.score,
                   m.sent_at,
                   m.status,
                   json_build_object('id', j.id, 'title', j.title) AS job,
                   json_build_object('id', u.id, 'email', u.email)   AS user
              FROM matches m
              JOIN "Job" j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE m.score >= %s
             ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
            """,
            (0.80,),
        )
        return _cur_to_dicts(cur)
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.post(
    "/resend/{mid}",
    dependencies=[Depends(get_current_admin)],
    summary="Reenviar mail/WhatsApp de un matching",
)
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        cur.execute(
            """
            SELECT m.score,
                   m.apply_token,
                   u.name AS cand_name,
                   u.email AS cand_email,
                   u."cvUrl"   AS cand_cv,
                   j.title     AS job_title
              FROM matches m
              JOIN "Job" j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE m.id = %s
            """,
            (mid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")

        score, token, name, email, cv, job_title = row
        if not email:
            raise HTTPException(400, "El candidato no tiene email")

        tpl = _get_default_tpl(cur, "empleado")
        apply_link = f"{FRONTEND_URL}/apply/{token or ''}"
        ctx = {
            "applicant_name": name,
            "job_title":      job_title,
            "cv_url":         cv or "",
            "score":          f"{round(score*100,1)}%",
            "apply_link":     apply_link,
            "created_at":     "",
        }
        msg = _apply_tpl(tpl, ctx)

        send_mail(email, msg["subject"], msg["body"], cv)
        cur.execute(
            "UPDATE matches SET sent_at=NOW(), status='resent' WHERE id=%s",
            (mid,),
        )
        conn.commit()
        return {"message": "reenviado"}
    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Error reenviando matching %d", mid)
        raise HTTPException(500, "Error interno")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.get("/job/{job_id}/match", summary="Preview usuarios para un Job")
def match_for_job(job_id: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            'SELECT embedding FROM "Job" WHERE id=%s AND embedding IS NOT NULL',
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Oferta sin embedding")
        emb = row[0]

        cur.execute(
            """
            SELECT u.id, u.email,
                   (1.0 - (u.embedding::vector <=> %s::vector)) AS score
              FROM "User" u
             WHERE u.embedding IS NOT NULL
             ORDER BY score DESC
             LIMIT 100
            """,
            (emb,),
        )
        return {"matches": [{"userId": r[0], "email": r[1], "score": float(r[2])} for r in cur.fetchall()]}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


@router.get("/user/{user_id}/match", summary="Preview ofertas para un Usuario")
def match_for_user(user_id: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute(
            'SELECT embedding FROM "User" WHERE id=%s AND embedding IS NOT NULL',
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Usuario sin embedding")
        emb = row[0]

        cur.execute(
            """
            SELECT j.id, j.title,
                   (1.0 - (j.embedding::vector <=> %s::vector)) AS score
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
             ORDER BY score DESC
             LIMIT 100
            """,
            (emb,),
        )
        return {"matches": [{"jobId": r[0], "title": r[1], "score": float(r[2])} for r in cur.fetchall()]}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
