from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importar routers públicos (clientes)
from app.routers import (
    auth as public_auth,  # Si existe autenticación para clientes
    cv_confirm, cv_upload, cv_processing, files, 
    file_processing, integration, token_utils, users, webhooks
)

# Importar router administrativo desde backend
from backend.auth import router as admin_router

app = FastAPI()

# Configurar CORS para la parte pública
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

# Registrar rutas administrativas con prefijo "/auth"
# Esto hará que el endpoint admin-login quede en /auth/admin-login
app.include_router(admin_router, prefix="/auth", tags=["admin"])

@app.get("/")
def home():
    return {"ok": True, "message": "Hello from FastAPI"}

# (Opcional) Imprimir rutas cargadas para depuración
@app.on_event("startup")
def list_routes():
    for route in app.routes:
        print(f"✅ Ruta cargada: {route.path}")
