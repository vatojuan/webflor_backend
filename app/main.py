from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt

# Importar routers públicos (clientes)
from app.routers import (
    auth as public_auth,
    cv_confirm, cv_upload, cv_processing, files, 
    file_processing, integration, token_utils, users, webhooks
)

# Importar router administrativo (login admin) desde backend
from backend.auth import router as admin_router

# Importar el nuevo router para carga masiva de CVs (administración)
from app.routers import cv_admin_upload

# Configuración de JWT (debe coincidir con la usada en backend/auth.py)
SECRET_KEY = "A5DD9F4F87075741044F604C552C31ED32E5BD246066A765A4D18DE8D8D83F12"
ALGORITHM = "HS256"

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://fapmendoza.online",
        "https://webfloradmin-vatojuans-projects.vercel.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar routers públicos
app.include_router(public_auth.router)
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(cv_processing.router)
app.include_router(files.router)
app.include_router(file_processing.router)
app.include_router(integration.router)
# app.include_router(token_utils.router)  # Descomenta si es necesario
app.include_router(users.router)
app.include_router(webhooks.router)

# Registrar el router administrativo con prefijo "/auth" (para el login admin)
app.include_router(admin_router, prefix="/auth", tags=["admin"])

# Configurar el esquema de OAuth2 para la protección de endpoints de administrador
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/admin-login")

def get_current_admin(token: str = Depends(oauth2_scheme)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username != "support@fapmendoza.com":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o sesión expirada"
            )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o sesión expirada"
        )
    return username

# Agregar el endpoint protegido de administración (ejemplo)
@app.get("/admin/protected", tags=["admin"])
def admin_protected(current_admin: str = Depends(get_current_admin)):
    return {"message": f"Ruta protegida para administradores, bienvenido {current_admin}"}

# Registrar el nuevo router para carga masiva de CVs
# Aquí se protege el endpoint añadiendo la dependencia get_current_admin,
# de modo que solo administradores autenticados puedan acceder.
app.include_router(
    cv_admin_upload.router,
    prefix="/cv", 
    tags=["cv_admin"],
    dependencies=[Depends(get_current_admin)]
)

@app.get("/")
def home():
    return {"ok": True, "message": "Hello from FastAPI"}

@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
