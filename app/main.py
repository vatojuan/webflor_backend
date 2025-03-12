from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# Importar routers públicos (clientes)
from app.routers import (
    auth as public_auth,
    cv_confirm, cv_upload, cv_processing, files, 
    file_processing, integration, token_utils, users, webhooks
)

# Importar router administrativo desde backend
from backend.auth import router as admin_router

# (Opcional) Función para validar el token admin, si ya la tienes definida
def get_current_admin(token: str = Depends(...)):
    # Tu lógica de validación (por ejemplo, usando OAuth2PasswordBearer)
    pass

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://fapmendoza.online", "https://webfloradmin-vatojuans-projects.vercel.app"],
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
# app.include_router(token_utils.router)
app.include_router(users.router)
app.include_router(webhooks.router)

# Registrar el router administrativo (login, etc.) con prefijo "/auth"
app.include_router(admin_router, prefix="/auth", tags=["admin"])

# Agregar la ruta protegida de administración
@app.get("/admin/protected", tags=["admin"])
def admin_protected():
    # Aquí puedes incluir lógica para validar el token admin,
    # por ejemplo, usando Depends(get_current_admin)
    return {"message": "Ruta protegida para administradores"}
    
@app.get("/")
def home():
    return {"ok": True, "message": "Hello from FastAPI"}

@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
