############################################################
# app/routers/proposal.py
# ----------------------------------------------------------
# Gestión de propuestas (postulaciones) y su ciclo de vida.
# Versión refactorizada y corregida - 27-jul-2025
############################################################

from __future__ import annotations

import os
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.database import engine

# Importaciones centralizadas para la comunicación
from app.email_utils import (
    send_proposal_to_employer,
    send_cancellation_warning,
    send_admin_alert,
)

load_dotenv()

# ───────────────────── Configuración Global ──────────────────────
SECRET_KEY: str = os.getenv("SECRET_KEY", "")
ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
AUTO_DELAY: int = int(os.getenv("AUTO_PROPOSAL_DELAY", "300"))  # segundos

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/proposals", tags=["proposals"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

# ───────────────────────────  Auth y DB  ─────────────────────────
def get_current_admin(token: str = Depends(oauth2_scheme)) -> str:
    try:
        sub = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub") or ""
        if not sub:
            raise ValueError("Token inválido")
        return sub
    except (JWTError, ValueError):
        raise HTTPException(status_code=401, detail="Token inválido o requerido")

def db() -> psycopg2.extensions.connection:
    conn = engine.raw_connection()
    conn.autocommit = False
    return conn

# ─────────────────── Lógica Principal de Envío (Deliver) ───────────────────
def deliver(proposal_id: int, sleep_first: bool) -> None:
    # ... (La lógica de esta función ya estaba correcta en la versión anterior)
    pass

# ───────────────────────── Endpoints de la API ─────────────────────────────

@router.get("/", dependencies=[Depends(get_current_admin)], summary="Listar todas las propuestas")
def list_proposals():
    conn = cur = None
    try:
        conn = db()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        
        # --- CORRECCIÓN CLAVE ---
        # Se cambió j."contactEmail" por j.contact_email para coincidir con el esquema de la BD.
        cur.execute("""
            SELECT
              p.id, p.label, p.status, p.notes,
              p.created_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS created_at,
              p.sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS sent_at,
              p.cancelled_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Argentina/Buenos_Aires' AS cancelled_at,
              j.id AS job_id,
              j.title AS job_title,
              u.id AS applicant_id,
              u.name AS applicant_name,
              u.email AS applicant_email,
              COALESCE(j.contact_email, emp.email) AS job_contact_email
            FROM proposals p
            JOIN "Job" j ON p.job_id = j.id
            JOIN "User" u ON p.applicant_id = u.id
            LEFT JOIN "User" emp ON j."userId" = emp.id
            ORDER BY p.created_at DESC
        """)
        proposals = cur.fetchall()
        return {"proposals": proposals}
    except Exception as e:
        logger.exception("Error al listar las propuestas.")
        raise HTTPException(status_code=500, detail=f"Error interno del servidor: {e}")
    finally:
        if cur: cur.close()
        if conn: conn.close()

# (El resto de los endpoints como /create, /cancel, etc. se mantienen igual)
@router.post("/create")
def create(data: dict, bg: BackgroundTasks):
    # ... (lógica de creación)
    pass

@router.patch("/{proposal_id}/send", dependencies=[Depends(get_current_admin)])
def send_manual(proposal_id: int, bg: BackgroundTasks):
    # ... (lógica de envío manual)
    pass

@router.post("/cancel")
def cancel(data: dict):
    # ... (lógica de cancelación)
    pass

@router.delete("/{pid}", dependencies=[Depends(get_current_admin)])
def delete_cancelled(pid: int):
    # ... (lógica de eliminación)
    pass
