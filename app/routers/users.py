import logging
from fastapi import APIRouter, Depends, HTTPException, Request, BackgroundTasks
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.user import User
from pydantic import BaseModel
import requests  # Para conectar con Supabase
from app.routers.match import run_matching_for_user  # <-- Importamos la función

router = APIRouter(
    prefix="/users",
    tags=["users"],
)

# Supabase Config
SUPABASE_URL = "https://apnfioxjddccokgkljvd.supabase.co"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFwbmZpb3hqZGRjY29rZ2tsanZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDA1MzYxMTMsImV4cCI6MjA1NjExMjExM30.dXasbL1EJi_yefvOlEA7UA6MYMjXw7jFYKjWTMjBNHI"

# Pydantic models
class UserUpdate(BaseModel):
    description: str

class UserOut(BaseModel):
    id: int
    email: str
    full_name: str = None
    description: str = None
    is_active: bool

    class Config:
        orm_mode = True

# DB Dependency
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Dependencia ficticia que obtiene al usuario logueado
def get_current_user():
    """
    Aquí debes usar tu propia lógica para obtener el usuario autenticado,
    p. ej. leyendo el token JWT y cargando el User correspondiente.
    """
    raise NotImplementedError("Implementar autenticación")

@router.put("/me", response_model=UserOut)
async def update_my_profile(
    request: Request,
    background_tasks: BackgroundTasks,
    user_update: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Actualiza la descripción del usuario autenticado y luego dispara
    en segundo plano el recalculo de sus matchings.
    """
    user_id = current_user.id
    new_description = user_update.description.strip()

    if not new_description:
        raise HTTPException(400, "La descripción no puede estar vacía")

    try:
        # 1) Actualizar la descripción en la base de datos
        db_user = db.query(User).filter(User.id == user_id).first()
        if not db_user:
            raise HTTPException(404, "Usuario no encontrado")

        db_user.description = new_description
        db.commit()

        # 2) Disparar en segundo plano el recálculo de matchings
        background_tasks.add_task(run_matching_for_user, user_id)

        return db_user

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logging.exception("Error actualizando perfil usuario %d: %s", user_id, e)
        raise HTTPException(500, f"Error interno actualizando perfil: {e}")
