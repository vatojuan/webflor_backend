# app/routers/proposal_templates.py

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
import psycopg2, os, traceback
from dotenv import load_dotenv

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
oauth2 = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DBNAME"),
        user=os.getenv("USER"),
        password=os.getenv("PASSWORD"),
        host=os.getenv("HOST"),
        port=int(os.getenv("DB_PORT", 5432)),
        sslmode="require"
    )

router = APIRouter(prefix="/api/admin/templates", tags=["proposal_templates"])

@router.post("/{template_id}/set-default", dependencies=[Depends(oauth2)])
def set_default_template(template_id: int):
    """
    Marca la plantilla indicada como default para su tipo,
    desmarcando cualquier otra del mismo tipo.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        # 1) Averiguar el tipo de la plantilla a marcar
        cur.execute("SELECT type FROM proposal_templates WHERE id = %s;", (template_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "Plantilla no encontrada")
        tpl_type = row[0]

        # 2) En una única transacción, desmarcar todas y luego marcar la seleccionada
        cur.execute("UPDATE proposal_templates SET is_default = FALSE WHERE type = %s;", (tpl_type,))
        cur.execute("UPDATE proposal_templates SET is_default = TRUE WHERE id = %s;", (template_id,))

        conn.commit()
        return {"message": f"Plantilla {template_id} establecida como default para '{tpl_type}'"}
    except HTTPException:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(500, "Error interno al establecer default")
    finally:
        cur.close()
        conn.close()
