from __future__ import annotations

import os
import secrets
import logging
import traceback
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database import get_db_connection
from app.routers.proposal import send_mail, send_whatsapp

# ─────────────────── Configuración & Autenticación ───────────────────
SECRET_KEY   = os.getenv("SECRET_KEY", "")
ALGORITHM    = os.getenv("ALGORITHM", "HS256")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://fapmendoza.online")
# Forzar dominio .online si quedó .com
if FRONTEND_URL.endswith(".com"):
    FRONTEND_URL = "https://fapmendoza.online"

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/match", tags=["matchings"])


def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token admin inválido o requerido")


# ─────────────────── Helpers ───────────────────

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
        # 1) Embedding de la oferta
        cur.execute('SELECT embedding FROM "Job" WHERE id=%s AND embedding IS NOT NULL', (job_id,))
        row = cur.fetchone()
        if not row:
            logger.info("run_matching_for_job: oferta %d sin embedding", job_id)
            return
        emb = row[0]
        # 2) Borrar previos
        cur.execute("DELETE FROM matches WHERE job_id=%s", (job_id,))
        conn.commit()
        # 3) Insertar pendientes
        cur.execute(
            """
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT %s, u.id,
                   (1.0 - (u.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "User" u
             WHERE u.embedding IS NOT NULL
            """,
            (job_id, emb),
        )
        conn.commit()
        # 4) Enviar a top scorers
        cur.execute(
            """
            SELECT m.id, m.score,
                   u.name, u.email, u."cvUrl",
                   j.title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job" j  ON j.id = m.job_id
             WHERE m.job_id=%s AND m.score >= %s
            """,
            (job_id, 0.80),
        )
        tpl = _get_default_tpl(cur, "empleado")
        for mid, score, name, email, cv, title in cur.fetchall():
            if not email:
                continue
            token = secrets.token_urlsafe(32)
            link  = f"{FRONTEND_URL}/apply/{token}"
            # Guardar token
            cur.execute("UPDATE matches SET apply_token=%s WHERE id=%s", (token, mid))
            # Preparar email
            ctx = {
                "applicant_name": name,
                "job_title":      title,
                "cv_url":         cv or "",
                "score":          f"{round(score*100,1)}%",
                "apply_link":     link,
                "created_at":     "",
            }
            msg = _apply_tpl(tpl, ctx)
            try:
                send_mail(email, msg["subject"], msg["body"], cv)
                cur.execute("UPDATE matches SET status='sent', sent_at=NOW() WHERE id=%s", (mid,))
            except Exception:
                logger.exception("Error enviando match %d", mid)
        conn.commit()
    except Exception:
        if conn: conn.rollback()
        logger.exception("run_matching_for_job error")
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ═══════════ Matching batch (usuario nuevo) ═══════════

def run_matching_for_user(user_id: int) -> None:
    conn = cur = None
    try:
        conn = get_db_connection()
        cur  = conn.cursor()
        cur.execute("DELETE FROM matches WHERE user_id=%s", (user_id,))
        conn.commit()
        cur.execute('SELECT embedding FROM "User" WHERE id=%s AND embedding IS NOT NULL', (user_id,))
        row = cur.fetchone()
        if not row:
            logger.info("run_matching_for_user: usuario %d sin embedding", user_id)
            return
        emb = row[0]
        cur.execute(
            """
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT j.id, %s,
                   (1.0 - (j.embedding::vector <=> %s::vector)),
                   'pending'
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
            """,
            (user_id, emb),
        )
        conn.commit()
    except Exception:
        if conn: conn.rollback()
        logger.exception("run_matching_for_user error")
    finally:
        if cur: cur.close()
        if conn: conn.close()


# ═══════════ Panel admin + reenviar + previews ═══════════
@router.get(
    "/admin",
    dependencies=[Depends(get_current_admin)],
    summary="Listado de matchings (score ≥ 0.80)",
)
def list_matchings():
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.score, m.sent_at, m.status,
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
        if cur: cur.close()
        if conn: conn.close()


@router.post(
    "/resend/{mid}",
    dependencies=[Depends(get_current_admin)],
    summary="Reenviar mail de matching",
)
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute(
            """
            SELECT m.score, m.apply_token,
                   u.name AS cand_name, u.email AS cand_email, u."cvUrl" AS cand_cv,
                   j.title AS job_title
              FROM matches m
              JOIN "User" u ON u.id = m.user_id
              JOIN "Job"  j ON j.id = m.job_id
             WHERE m.id = %s
            """,
            (mid,),
        )
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")
        score, token, name, email, cv, job_title = row
        if not email:
            raise HTTPException(400, "Candidato sin email")
        tpl = _get_default_tpl(cur, "empleado")
        link = f"{FRONTEND_URL}/apply/{token or ''}"
        ctx = {
            "applicant_name": name,
            "job_title":      job_title,
            "cv_url":         cv or "",
            "score":          f"{round(score*100,1)}%",
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
        if conn: conn.rollback()
        logger.exception("Error reenviando matching %d", mid)
        raise HTTPException(500, "Error interno")
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/job/{job_id}/match", summary="Preview usuarios para un Job")
def match_for_job(job_id: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT embedding FROM "Job" WHERE id=%s AND embedding IS NOT NULL', (job_id,))
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
        return {"matches": [{"userId":r[0],"email":r[1],"score":float(r[2])} for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()


@router.get("/user/{user_id}/match", summary="Preview ofertas para un Usuario")
def match_for_user(user_id: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT embedding FROM "User" WHERE id=%s AND embedding IS NOT NULL', (user_id,))
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
        return {"matches": [{"jobId":r[0],"title":r[1],"score":float(r[2])} for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()
