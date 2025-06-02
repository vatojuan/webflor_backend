"""
Matchings (coincidencias Job ↔ User)

• GET  /api/match/job/{job_id}/match     – calcular y devolver scores   (ya existía)
• GET  /api/match/user/{user_id}/match   – idem desde el lado usuario  (ya existía)

• GET  /api/match/admin                  – **NUEVO**  listado de matchings para el panel admin
• POST /api/match/resend/{mid}           – **NUEVO**  re-enviar e-mail / WhatsApp de un matching
"""

from __future__ import annotations

import os, logging, traceback
from datetime import datetime
from typing import List, Dict, Any, Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError

from app.database   import get_db_connection
from app.routers.proposal import send_mail, send_whatsapp           # re-usamos helpers

# ─────────────────── Config / auth ───────────────────
SECRET_KEY = os.getenv("SECRET_KEY", "")
ALGORITHM  = os.getenv("ALGORITHM", "HS256")

oauth2_admin = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_admin)) -> str:
    if not token:
        raise HTTPException(401, "Token requerido")
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
    except JWTError:
        raise HTTPException(401, "Token inválido")

# ─────────────────── Router ───────────────────────────
router = APIRouter(prefix="/api/match", tags=["matchings"])

logger = logging.getLogger(__name__)

# ───────────── Helper SQL → dict ─────────────
def qdict(cur) -> List[Dict[str,Any]]:
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]

# ═════════════════════════════════════════════
# 🆕  PANELES DE ADMINISTRACIÓN
# ═════════════════════════════════════════════
@router.get("/admin", summary="Listado de matchings", dependencies=[Depends(get_current_admin)])
def list_matchings():
    """
    Devuelve todos los registros de la tabla **matches** con los datos que
    el front espera (job.title, user.email, score, etc.).
    """
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()
        cur.execute("""
            SELECT
              m.id,
              m.score,
              m.sent_at,
              m.status,
              json_build_object('id',j.id,'title',j.title)  AS job,
              json_build_object('id',u.id,'email',u.email) AS user
            FROM matches m
            JOIN "Job"  j ON j.id = m.job_id
            JOIN "User" u ON u.id = m.user_id
            ORDER BY m.sent_at DESC NULLS FIRST, m.id DESC
        """)
        return qdict(cur)
    finally:
        if cur: cur.close()
        if conn: conn.close()

# ═════════════════════════════════════════════
# 🆕  REENVIAR MATCHING
# ═════════════════════════════════════════════
@router.post("/resend/{mid}", summary="Re-enviar e-mail/WhatsApp de un matching",
             dependencies=[Depends(get_current_admin)])
def resend_matching(mid: int):
    conn = cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor()

        # Datos principales
        cur.execute("""
            SELECT m.score, j.title, j.contact_email, j.contactEmail,
                   j.contact_phone, j.contactPhone,
                   u.name, u.email, u."cvUrl"
              FROM matches m
              JOIN "Job"  j ON j.id = m.job_id
              JOIN "User" u ON u.id = m.user_id
             WHERE m.id = %s
        """, (mid,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Matching no encontrado")

        score, title,\
        c_email1, c_email2, c_phone1, c_phone2,\
        cand_name, cand_mail, cv_url = row

        dest_email  = c_email1 or c_email2
        dest_phone  = c_phone1 or c_phone2
        subject     = f"🔄 Reenvío – Matching {cand_name} ↔ «{title}»"
        body        = (f"El candidato {cand_name} ({cand_mail}) coincide con «{title}» "
                       f"con un score de {round(score*100,1)} %.")

        if not dest_email:
            raise HTTPException(400, "Oferta sin e-mail de contacto")

        send_mail(dest_email, subject, body, cv_url)
        send_whatsapp(dest_phone, body)

        cur.execute("UPDATE matches SET sent_at = NOW(), status='resent' WHERE id = %s", (mid,))
        conn.commit()
        logger.info("Matching %d reenviado", mid)
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
