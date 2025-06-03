# app/routers/cleanup.py
from datetime import datetime
import logging

from app.database import get_db_connection

logger = logging.getLogger(__name__)

def cleanup_expired_and_orphan_matches() -> None:
    """
    1) Elimina de matches todo registro cuyo job ya expiró (expirationDate < now).
    2) Elimina todo match que no tenga job asociado (job borrado).
    """
    conn = cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1) Borrar matchings de ofertas expiradas
        cur.execute(
            """
            DELETE FROM matches
             USING "Job" j
            WHERE matches.job_id = j.id
              AND j."expirationDate" IS NOT NULL
              AND j."expirationDate" < NOW();
            """
        )
        deleted_expired = cur.rowcount

        # 2) Borrar matchings huérfanos (jobs que ya no existen)
        cur.execute(
            """
            DELETE FROM matches m
             WHERE NOT EXISTS (
               SELECT 1 FROM "Job" j WHERE j.id = m.job_id
             );
            """
        )
        deleted_orphans = cur.rowcount

        conn.commit()
        logger.info(
            "cleanup: eliminados %d matchings expirados y %d huérfanos",
            deleted_expired, deleted_orphans
        )
    except Exception:
        if conn:
            conn.rollback()
        logger.exception("Error en cleanup_expired_and_orphan_matches")
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()
