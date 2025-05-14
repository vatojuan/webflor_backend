# app/routers/proposal_templates.py
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import psycopg2, os, traceback
from dotenv import load_dotenv

load_dotenv()

oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_db():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME"),
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        sslmode="require",
    )

router = APIRouter(prefix="/api/admin/templates", tags=["proposal_templates"])

# ────────────────────────────────────────────────────────────────
# 1) LISTAR (lo que necesita el front)
# ────────────────────────────────────────────────────────────────
@router.get("/", dependencies=[Depends(oauth2)])
def list_templates():
    """
    Devuelve todas las plantillas ordenadas primero por tipo,
    luego poniendo la que es default al principio.
    """
    conn = get_db(); cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT id, name, type, subject, body, is_default, created_at, updated_at
            FROM   proposal_templates
            ORDER  BY type,
                      is_default DESC,   -- la default primero
                      id
            """
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return {"templates": rows}
    except Exception:
        traceback.print_exc()
        raise HTTPException(500, "Error al listar plantillas")
    finally:
        cur.close(); conn.close()

# ────────────────────────────────────────────────────────────────
# 2) SET-DEFAULT (ya existía, lo dejo igual)
# ────────────────────────────────────────────────────────────────
@router.post("/{template_id}/set-default", dependencies=[Depends(oauth2)])
def set_default_template(template_id: int):
    conn = get_db(); cur = conn.cursor()
    try:
        # averiguar tipo
        cur.execute("SELECT type FROM proposal_templates WHERE id = %s", (template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Plantilla no encontrada")
        tpl_type = row[0]

        # desmarco todas las de ese tipo y marco la elegida
        cur.execute("UPDATE proposal_templates SET is_default = FALSE WHERE type = %s", (tpl_type,))
        cur.execute("UPDATE proposal_templates SET is_default = TRUE  WHERE id   = %s", (template_id,))
        conn.commit()
        return {"message": f"Plantilla {template_id} ahora es default para '{tpl_type}'"}
    except HTTPException:
        conn.rollback(); raise
    except Exception:
        conn.rollback(); traceback.print_exc()
        raise HTTPException(500, "Error interno")
    finally:
        cur.close(); conn.close()
