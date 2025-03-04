from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Importar routers
from app.routers import auth, cv_confirm, cv_upload, cv_processing, files, file_processing, integration, token_utils, users, webhooks

app = FastAPI()

# Habilitar CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Registrar routers
app.include_router(auth.router)
app.include_router(cv_confirm.router)
app.include_router(cv_upload.router)
app.include_router(cv_processing.router)
app.include_router(files.router)
app.include_router(file_processing.router)
app.include_router(integration.router)
#app.include_router(token_utils.router)
app.include_router(users.router)
app.include_router(webhooks.router)

@app.get("/")
def home():
    return {"ok": True, "message": "Hello from FastAPI"}
