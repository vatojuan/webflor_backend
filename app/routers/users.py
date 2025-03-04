import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models.user import User
from pydantic import BaseModel
import requests  # Para conectar con Supabase

router = APIRouter(
    prefix="/users",
    tags=["users"],
)

# Supabase Config
SUPABASE_URL = "https://apnfioxjddccokgkljvd.supabase.co"
SUPABASE_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImFwbmZpb3hqZGRjY29rZ2tsanZkIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NDA1MzYxMTMsImV4cCI6MjA1NjExMjExM30.dXasbL1EJi_yefvOlEA7UA6MYMjXw7jFYKjWTMjBNHI"

# Pydantic models
class UserCreate(BaseModel):
    email: str
    full_name: str = None
    password: str

class UserOut(BaseModel):
    id: int
    email: str
    full_name: str = None
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

@router.post("/", response_model=UserOut)
def create_user(user: UserCreate, db: Session = Depends(get_db)):
    logging.info(f"Intentando crear usuario: {user.email}")

    # Verificar si el usuario ya existe en Supabase
    headers = {
        "apikey": SUPABASE_API_KEY,
        "Authorization": f"Bearer {SUPABASE_API_KEY}",
        "Content-Type": "application/json"
    }
    
    data = {
        "email": user.email,
        "full_name": user.full_name,
        "password": user.password  # En producción, debe estar hasheado
    }

    # Intentar insertar el usuario en Supabase
    response = requests.post(f"{SUPABASE_URL}/rest/v1/User", json=data, headers=headers)

    if response.status_code != 201:
        logging.error(f"Error en Supabase: {response.status_code} - {response.text}")
        raise HTTPException(status_code=400, detail="Error al crear usuario en Supabase")

    return {"message": "Usuario creado con éxito"}
