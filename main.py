import json
from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from sqlalchemy import create_engine, Column, String, Text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# Inicialização do FastAPI
app = FastAPI(title="ORA CRM API", version="3.0.0")

# Configuração de CORS para permitir que o arquivo HTML acesse a API localmente
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Permite qualquer origem para desenvolvimento local
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Banco de Dados SQLite com SQLAlchemy
DATABASE_URL = "sqlite:///./ora_crm_database.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Modelos do Banco de Dados (Estratégia Híbrida NoSQL/SQL para flexibilidade máxima de campos)
class UserModel(Base):
    __tablename__ = "users"
    id = Column(String, primary_key=True, index=True)
    data = Column(Text)

class ProposalModel(Base):
    __tablename__ = "proposals"
    id = Column(String, primary_key=True, index=True)
    data = Column(Text)

class TemplateModel(Base):
    __tablename__ = "templates"
    id = Column(String, primary_key=True, index=True)
    data = Column(Text)

class SettingModel(Base):
    __tablename__ = "settings"
    id = Column(String, primary_key=True, index=True)
    data = Column(Text)

class AuditModel(Base):
    __tablename__ = "audit"
    id = Column(String, primary_key=True, index=True)
    data = Column(Text)

# Criar as tabelas se não existirem
Base.metadata.create_all(bind=engine)

# Dependency para obter a sessão do banco
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Esquemas de validação Pydantic Generic
class GenericData(BaseModel):
    id: str
    data: Dict[str, Any]

class SettingsData(BaseModel):
    data: Dict[str, Any]

# --- ENDPOINTS DA API ---

@app.get("/api/init")
def get_init_data(db: Session = Depends(get_db)):
    """Carrega todo o estado inicial do sistema consolidado"""
    users = [json.loads(u.data) for u in db.query(UserModel).all()]
    proposals = [json.loads(p.data) for p in db.query(ProposalModel).all()]
    templates = [json.loads(t.data) for t in db.query(TemplateModel).all()]
    
    settings_db = db.query(SettingModel).first()
    settings = json.loads(settings_db.data) if settings_db else {}
    
    audit_logs = [json.loads(a.data) for a in db.query(AuditModel).all()]
    
    return {
        "users": users,
        "proposals": proposals,
        "templates": templates,
        "settings": settings,
        "audit": audit_logs
    }

# --- USUÁRIOS ---
@app.post("/api/users")
def save_user(payload: Dict[str, Any], db: Session = Depends(get_db)):
    user_id = payload.get("id")
    if not user_id:
        raise HTTPException(status_code=400, detail="ID do usuário é obrigatório")
    
    db_user = db.query(UserModel).filter(UserModel.id == user_id).first()
    if db_user:
        db_user.data = json.dumps(payload)
    else:
        db_user = UserModel(id=user_id, data=json.dumps(payload))
        db.add(db_user)
    db.commit()
    return {"status": "success", "user": payload}

# --- PROPOSTAS ---
@app.post("/api/proposals")
def save_proposal(payload: Dict[str, Any], db: Session = Depends(get_db)):
    prop_id = payload.get("id")
    if not prop_id:
        raise HTTPException(status_code=400, detail="ID da proposta é obrigatório")
    
    db_prop = db.query(ProposalModel).filter(ProposalModel.id == prop_id).first()
    if db_prop:
        db_prop.data = json.dumps(payload)
    else:
        db_prop = ProposalModel(id=prop_id, data=json.dumps(payload))
        db.add(db_prop)
    db.commit()
    return {"status": "success", "proposal": payload}

# --- MODELOS / TEMPLATES ---
@app.post("/api/templates")
def save_template(payload: Dict[str, Any], db: Session = Depends(get_db)):
    tpl_id = payload.get("id")
    if not tpl_id:
        raise HTTPException(status_code=400, detail="ID do modelo é obrigatório")
    
    db_tpl = db.query(TemplateModel).filter(TemplateModel.id == tpl_id).first()
    if db_tpl:
        db_tpl.data = json.dumps(payload)
    else:
        db_tpl = TemplateModel(id=tpl_id, data=json.dumps(payload))
        db.add(db_tpl)
    db.commit()
    return {"status": "success", "template": payload}

@app.delete("/api/templates/{tpl_id}")
def delete_template(tpl_id: str, db: Session = Depends(get_db)):
    db_tpl = db.query(TemplateModel).filter(TemplateModel.id == tpl_id).first()
    if not db_tpl:
        raise HTTPException(status_code=404, detail="Modelo não encontrado")
    db.delete(db_tpl)
    db.commit()
    return {"status": "success", "message": "Modelo excluído"}

# --- CONFIGURAÇÕES ---
@app.post("/api/settings")
def save_settings(payload: Dict[str, Any], db: Session = Depends(get_db)):
    db_setting = db.query(SettingModel).filter(SettingModel.id == "global").first()
    if db_setting:
        db_setting.data = json.dumps(payload)
    else:
        db_setting = SettingModel(id="global", data=json.dumps(payload))
        db.add(db_setting)
    db.commit()
    return {"status": "success", "settings": payload}

# --- AUDITORIA ---
@app.post("/api/audit")
def save_audit(payload: Dict[str, Any], db: Session = Depends(get_db)):
    audit_id = payload.get("id")
    if not audit_id:
        raise HTTPException(status_code=400, detail="ID do log de auditoria é obrigatório")
    
    db_audit = AuditModel(id=audit_id, data=json.dumps(payload))
    db.add(db_audit)
    db.commit()
    return {"status": "success"}

if __name__ == "__main__":
    import uvicorn
    print("Iniciando servidor backend ORA CRM no endereço http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
