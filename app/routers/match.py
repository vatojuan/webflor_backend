from __future__ import annotations

import os
import secrets
import logging
import traceback
from datetime import datetime
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import get_db_connection
from app.routers.proposal import send_mail

# ─────────────────── Configuración & autenticación ───────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://fapmendoza.com").rstrip("/")
oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")
logger       = logging.getLogger(__name__)
router       = APIRouter(prefix="/api/match", tags=["matchings"])


def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token admin inválido o requerido")


# ─────────────────── Helpers internos ───────────────────

def _cur_to_dicts(cur) -> List[Dict[str, Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _get_default_tpl(cur, tpl_type: str) -> Dict[str, str]:
    cur.execute("""
        SELECT subject, body
          FROM proposal_templates
         WHERE type = %s AND is_default = TRUE
         LIMIT 1
    """, (tpl_type,))
    row = cur.fetchone()
    return {"subject": row[0], "body": row[1]} if row else {"subject": "", "body": ""}


def _apply_tpl(tpl: Dict[str, str], ctx: Dict[str, str]) -> Dict[str, str]:
    subj, body = tpl.get("subject", ""), tpl.get("body", "")
    for k, v in ctx.items():
        subj = subj.replace(f"{{{{{k}}}}}", v)
        body = body.replace(f"{{{{{k}}}}}", v)
    return {"subject": subj or "(sin asunto)", "body": body}


# ═══════════ Matching batch (oferta nueva) ═══════════

def run_matching_for_job(job_id: int) -> None:
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
            logger.info(f"run_matching_for_job: oferta {job_id} sin embedding, se omite.")
            return
        emb = row[0]

        # 2) Borrar previos
        logger.info(f"run_matching_for_job: borrando matchings previos para oferta {job_id}")
        cur.execute("DELETE FROM matches WHERE job_id = %s", (job_id,))
        conn.commit()

        # 3) Insertar nuevos pendientes
        logger.info(f"run_matching_for_job: insertando matchings pendientes para oferta {job_id}")
        cur.execute("""
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT %s, u.id,
                   (1.0 - (u.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "User" u
             WHERE u.embedding IS NOT NULL
        """, (job_id, emb))
        conn.commit()
        logger.info(f"run_matching_for_job: insertados {cur.rowcount} matchings pendientes")

        # 4) Consultar y enviar
        tpl = _get_default_tpl(cur, "empleado")
        cur.execute("""
            SELECT m.id, m.user_id, m.score,
                   u.name, u.email, u."cvUrl",
                   j.title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.job_id = %s
        """, (job_id,))
        for mid, u_id, score, name, email, cv, title in cur.fetchall():
            sc = float(score)
            if sc < 0.80 or not email:
                continue
            token      = secrets.token_urlsafe(32)
            apply_link = f"{FRONTEND_URL}/apply/{token}"

            # Guardar token en matches y centralizado
            cur.execute(
                "UPDATE matches SET apply_token=%s, status='sent', sent_at=NOW() WHERE id=%s",
                (token, mid)
            )
            cur.execute("""
                INSERT INTO apply_tokens (token, job_id, applicant_id, expires_at, used)
                VALUES (%s, %s, %s, NOW() + INTERVAL '30 days', FALSE)
                ON CONFLICT(token) DO NOTHING
            """, (token, job_id, u_id))
            conn.commit()

            ctx = {
                "applicant_name": name,
                "job_title":      title,
                "cv_url":         cv or "",
                "score":          f"{round(sc*100,1)}%",
                "apply_link":     apply_link,
                "created_at":     datetime.utcnow().isoformat(),
            }
            msg = _apply_tpl(tpl, ctx)
            try:
                send_mail(email, msg["subject"], msg["body"], cv)
                logger.info(f"✅ Email enviado exitosamente a {email}")
            except Exception as e:
                logger.exception(f"❌ Error enviando match {mid} a {email}")
                cur.execute(
                    "UPDATE matches SET status='error', error_msg=%s WHERE id=%s",
                    (str(e)[:250], mid),
                )
                conn.commit()

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("run_matching_for_job error inesperado")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ═══════════ Matching batch (usuario nuevo) ═══════════

def run_matching_for_user(user_id: int) -> None:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()

        logger.info(f"run_matching_for_user: borrando previos para usuario {user_id}")
        cur.execute("DELETE FROM matches WHERE user_id = %s", (user_id,))
        conn.commit()

        cur.execute(
            'SELECT embedding FROM "User" WHERE id = %s AND embedding IS NOT NULL',
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            logger.info(f"run_matching_for_user: usuario {user_id} sin embedding, se omite.")
            return
        emb = row[0]

        logger.info(f"run_matching_for_user: insertando matchings pendientes para usuario {user_id}")
        cur.execute("""
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT j.id, %s,
                   (1.0 - (j.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
        """, (user_id, emb))
        conn.commit()
        logger.info(f"run_matching_for_user: insertados {cur.rowcount} matchings pendientes")

    except Exception:
        if conn:
            conn.rollback()
        logger.exception("run_matching_for_user error inesperado")
    finally:
        if cur:  cur.close()
        if conn: conn.close()


# ═══════════ Panel admin & reenvío ═══════════

@router.get(
    "/admin",
    dependencies=[Depends(get_current_admin)],
    summary="Listado de matchings (score ≥ 0.80)",
)
def list_matchings():
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT m.id, m.score, m.sent_at, m.status,
                   json_build_object('id', j.id, 'title', j.title) AS job,
                   json_build_object('id', u.id, 'email', u.email)   AS user
              FROM matches m
              JOIN "Job"  j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE (m.score)::float >= %s
             ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
        """, (0.80,))
        return _cur_to_dicts(cur)
    finally:
        if cur:  cur.close()
        if conn: conn.close()


@router.post(
    "/resend/{mid}",
    dependencies=[Depends(get_current_admin)],
    summary="Reenviar email de matching",
)
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("""
            SELECT m.score, m.apply_token,
                   u.name AS cand_name, u.email AS cand_email, u."cvUrl" AS cand_cv,
                   j.title     AS job_title, m.user_id
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.id = %s
        """, (mid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")

        score, token, name, email, cv, job_title, u_id = row
        if not email:
            raise HTTPException(400, "Candidato sin email")

        # Asegurar token en apply_tokens
        cur.execute("""
            INSERT INTO apply_tokens (token, job_id, applicant_id, expires_at, used)
            SELECT %s, job_id, user_id, NOW() + INTERVAL '30 days', FALSE
              FROM matches WHERE id = %s
            ON CONFLICT(token) DO NOTHING
        """, (token, mid))
        conn.commit()

        tpl  = _get_default_tpl(cur, "empleado")
        link = f"{FRONTEND_URL}/apply/{token}"
        ctx  = {
            "applicant_name": name,
            "job_title":      job_title,
            "cv_url":         cv or "",
            "score":          f"{round(float(score)*100,1)}%",
            "apply_link":     link,
            "created_at":     "",
        }
        msg = _apply_tpl(tpl, ctx)

        send_mail(email, msg["subject"], msg["body"], cv)
        cur.execute("UPDATE matches SET sent_at=NOW(), status='resent' WHERE id=%s", (mid,))
        conn.commit()
        return {"message": "reenviado"}

    except HTTPException:
        raise
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("resend_matching error inesperado")
        raise HTTPException(500, "Error interno")
    finally:
        if cur:  cur.close()
        if conn: conn.close()
