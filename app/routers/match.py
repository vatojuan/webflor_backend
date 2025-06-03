# app/routers/match.py
"""
Matchings (Job ↔ User)

• GET  /api/match/job/{job_id}/match      → preview (no escribe BD)
• GET  /api/match/user/{user_id}/match    → preview inverso
• GET  /api/match/admin                   → panel admin
• POST /api/match/resend/{mid}            → reenviar mail/WhatsApp

La rutina `run_matching_for_job(job_id)` se invoca al crear una oferta
(job o job-admin). Calcula los scores (pgvector) y guarda en `matches`
con status = pending.  El envío real se delega al módulo *proposal*
cuando el candidato hace click en “Postularme”.
"""
from __future__ import annotations

import os, logging, traceback
from typing import Any, Dict, List

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database         import get_db_connection
from app.routers.proposal import send_mail, send_whatsapp

# ─────────────────── Config & auth ───────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token admin inválido o requerido")

router = APIRouter(prefix="/api/match", tags=["matchings"])
logger = logging.getLogger(__name__)

# ───────────── helpers util ─────────────
def _cur_to_dicts(cur) -> List[Dict[str,Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

def _get_default_tpl(cur, tpl_type: str) -> Dict[str,str]:
    cur.execute("""
        SELECT subject, body
          FROM proposal_templates
         WHERE type = %s AND is_default = TRUE
         LIMIT 1
    """, (tpl_type,))
    row = cur.fetchone()
    return {"subject": row[0], "body": row[1]} if row else {}

def _apply_tpl(tpl: Dict[str,str], ctx: Dict[str,str]) -> Dict[str,str]:
    subj, body = tpl.get("subject",""), tpl.get("body","")
    for k,v in ctx.items():
        subj = subj.replace(f"{{{{{k}}}}}", v)
        body = body.replace(f"{{{{{k}}}}}", v)
    return {"subject": subj or "(sin asunto)", "body": body}

# ═══════════ Matching batch (oferta nueva) ═══════════
def run_matching_for_job(job_id: int) -> None:
    """
    Recalcula todos los matchings Job→User y los deja en `matches`
    con status='pending'.  El envío al candidato sucede sólo cuando
    éste acepta / se postula.
    """
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()

        # embedding de la oferta
        cur.execute('SELECT embedding FROM "Job" WHERE id=%s AND embedding IS NOT NULL',
                    (job_id,))
        row = cur.fetchone()
        if not row:
            logger.info("run_matching_for_job: oferta %d sin embedding, omito.", job_id)
            return
        job_emb = row[0]

        # borrar matchings viejos
        cur.execute("DELETE FROM matches WHERE job_id = %s", (job_id,))

        # insertar nuevos (cast explícito a vector)
        cur.execute("""
            INSERT INTO matches (job_id, user_id, score, status)
            SELECT
              %s,
              u.id,
              (1 - (u.embedding::vector <=> %s::vector)) AS score,
              'pending'
            FROM "User" u
            WHERE u.embedding IS NOT NULL
        """, (job_id, job_emb))
        conn.commit()
        logger.info("run_matching_for_job: %d filas insertadas para job %d",
                    cur.rowcount, job_id)

    except Exception:
        if conn: conn.rollback()
        logger.exception("run_matching_for_job error job_id=%d", job_id)
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ═══════════ Listado admin ═══════════
@router.get("/admin", dependencies=[Depends(get_current_admin)],
            summary="Listado de matchings guardados")
def list_matchings():
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT m.id, m.score, m.sent_at, m.status,
                   json_build_object('id',j.id,'title',j.title) AS job,
                   json_build_object('id',u.id,'email',u.email) AS user
              FROM matches m
              JOIN "Job"  j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
        """)
        return _cur_to_dicts(cur)
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ═══════════ Reenviar a empleador ═══════════
@router.post("/resend/{mid}", dependencies=[Depends(get_current_admin)],
             summary="Reenviar mail/WhatsApp de un matching")
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()

        cur.execute("""
            SELECT m.score,
                   j.title, COALESCE(j.contact_email,j."contactEmail"),
                   COALESCE(j.contact_phone,j."contactPhone"),
                   u.name, u.email, u."cvUrl"
              FROM matches m
              JOIN "Job"  j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE m.id = %s
        """, (mid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")

        score, title, dest_email, dest_phone, cand_name, cand_email, cv_url = row
        if not dest_email:
            raise HTTPException(400, "Oferta sin email de contacto")

        tpl = _get_default_tpl(cur, "automatic") or {}
        ctx = dict(applicant_name=cand_name,
                   job_title=title,
                   score=f"{round(score*100,1)} %",
                   cv_url=cv_url or "",
                   created_at="")
        msg = _apply_tpl(tpl, ctx)

        send_mail(dest_email, msg["subject"], msg["body"], cv_url)
        send_whatsapp(dest_phone, msg["body"])

        cur.execute("UPDATE matches SET sent_at=NOW(), status='resent' WHERE id=%s",
                    (mid,))
        conn.commit()
        return {"message": "reenviado"}
    except HTTPException: raise
    except Exception:
        if conn: conn.rollback()
        logger.exception("Error reenviando matching %d", mid)
        raise HTTPException(500, "Error interno")
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ═══════════ Previews (sin persistir) ═══════════
@router.get("/job/{job_id}/match", summary="Preview usuarios para un Job")
def match_for_job(job_id: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT embedding FROM "Job" WHERE id=%s AND embedding IS NOT NULL',
                    (job_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404,"Oferta sin embedding")
        job_emb = row[0]

        cur.execute("""
            SELECT u.id, u.email,
                   (1 - (u.embedding::vector <=> %s::vector)) AS score
              FROM "User" u
             WHERE u.embedding IS NOT NULL
             ORDER BY score DESC
             LIMIT 100
        """, (job_emb,))
        return {"matches":[{"userId":r[0],"email":r[1],"score":float(r[2])}
                           for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.get("/user/{user_id}/match", summary="Preview ofertas para un User")
def match_for_user(user_id: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute('SELECT embedding FROM "User" WHERE id=%s AND embedding IS NOT NULL',
                    (user_id,))
        row = cur.fetchone()
        if not row: raise HTTPException(404,"Usuario sin embedding")
        user_emb = row[0]

        cur.execute("""
            SELECT j.id, j.title,
                   (1 - (j.embedding::vector <=> %s::vector)) AS score
              FROM "Job" j
             WHERE j.embedding IS NOT NULL
             ORDER BY score DESC
             LIMIT 100
        """, (user_emb,))
        return {"matches":[{"jobId":r[0],"title":r[1],"score":float(r[2])}
                           for r in cur.fetchall()]}
    finally:
        if cur: cur.close()
        if conn: conn.close()
