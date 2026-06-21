import json
import os
import hashlib
import secrets
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
# Por padrão, mantém o arquivo na pasta do projeto.
# Em produção, configure ORA_DB_PATH (ex.: /var/data/ora_crm_database.db em um disco persistente)
# para evitar perda de dados em redeploys.
def resolve_database_url() -> str:
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        return explicit_url

    db_path = os.getenv("ORA_DB_PATH") or os.getenv("SQLITE_DB_PATH")
    if not db_path:
        data_dir = os.getenv("RENDER_DISK_PATH") or os.getenv("DATA_DIR")
        db_path = str(Path(data_dir) / "ora_crm_database.db") if data_dir else "./ora_crm_database.db"

    db_file = Path(db_path)
    if db_file.parent and str(db_file.parent) not in ("", "."):
        db_file.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_file}"

DATABASE_URL = resolve_database_url()
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
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

class DeletedProposalModel(Base):
    __tablename__ = "deleted_proposals"
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

# Admin opcional por variável de ambiente para evitar travar o acesso online.
# No Render, configure ORA_ADMIN_EMAIL e ORA_ADMIN_PASSWORD para criar ou redefinir
# a senha do administrador sem apagar usuários, propostas ou configurações.
def _hash_password_for_frontend(password: str, salt: str) -> str:
    # O index.html usa SHA-256 com o formato "salt|senha".
    return hashlib.sha256(f"{salt}|{password}".encode("utf-8")).hexdigest()

def bootstrap_admin_from_env():
    admin_email = (os.getenv("ORA_ADMIN_EMAIL") or "").strip().lower()
    admin_password = os.getenv("ORA_ADMIN_PASSWORD") or ""
    admin_name = (os.getenv("ORA_ADMIN_NAME") or "Administrador ORA").strip()

    if not admin_email or not admin_password:
        return

    db = SessionLocal()
    try:
        target_row = None
        target_data = None

        for row in db.query(UserModel).all():
            try:
                data = json.loads(row.data)
            except Exception:
                continue
            if str(data.get("email", "")).strip().lower() == admin_email:
                target_row = row
                target_data = data
                break

        now = datetime.now(timezone.utc).isoformat()
        salt = secrets.token_hex(12)
        user_data = dict(target_data or {})
        user_data.update({
            "id": user_data.get("id") or f"user_admin_{hashlib.sha1(admin_email.encode('utf-8')).hexdigest()[:12]}",
            "name": user_data.get("name") or admin_name,
            "email": admin_email,
            "role": "admin",
            "active": True,
            "seller": True,
            "salt": salt,
            "passHash": _hash_password_for_frontend(admin_password, salt),
            "updatedAt": now,
        })
        user_data.setdefault("createdAt", now)

        if target_row:
            target_row.data = json.dumps(user_data, ensure_ascii=False)
        else:
            db.add(UserModel(id=user_data["id"], data=json.dumps(user_data, ensure_ascii=False)))

        db.commit()
        print(f"Administrador sincronizado pelo ambiente: {admin_email}")
    finally:
        db.close()

bootstrap_admin_from_env()

# Dependency para obter a sessão do banco
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def proposal_is_deleted(data: Dict[str, Any]) -> bool:
    return bool(
        data.get("_deleted") is True
        or data.get("deleted") is True
        or data.get("deletedAt")
        or data.get("status") == "Excluída"
        or data.get("etapa") == "Excluída"
    )

def deleted_proposal_ids(db: Session) -> set:
    ids = {row.id for row in db.query(DeletedProposalModel).all()}
    for row in db.query(ProposalModel).all():
        try:
            data = json.loads(row.data)
        except Exception:
            continue
        if proposal_is_deleted(data):
            ids.add(row.id)
    return ids

def delete_proposal_by_id(prop_id: str, db: Session, proposal_data_override: Optional[Dict[str, Any]] = None, deleted_by: Optional[str] = None) -> Dict[str, Any]:
    if not prop_id:
        raise HTTPException(status_code=400, detail="ID da proposta é obrigatório")

    db_prop = db.query(ProposalModel).filter(ProposalModel.id == prop_id).first()
    proposal_data = proposal_data_override or {}
    if db_prop:
        try:
            proposal_data = proposal_data or json.loads(db_prop.data)
        except Exception:
            proposal_data = proposal_data or {"id": prop_id}
        db.delete(db_prop)

    now = datetime.now(timezone.utc).isoformat()
    tombstone = {
        "id": prop_id,
        "proposal": proposal_data,
        "deletedAt": now,
        "deletedBy": deleted_by or proposal_data.get("deletedBy") or "",
    }
    db_deleted = db.query(DeletedProposalModel).filter(DeletedProposalModel.id == prop_id).first()
    if db_deleted:
        db_deleted.data = json.dumps(tombstone, ensure_ascii=False)
    else:
        db.add(DeletedProposalModel(id=prop_id, data=json.dumps(tombstone, ensure_ascii=False)))

    db.commit()
    return {"status": "success", "message": "Proposta excluída", "id": prop_id}


def _env_first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
    return default


def _truthy_value(value: Optional[str], default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "sim", "on")


def _proposal_display_value(proposal_data: Dict[str, Any]) -> str:
    value = (
        proposal_data.get("valueTotal")
        or proposal_data.get("total")
        or proposal_data.get("amount")
        or proposal_data.get("valor")
        or proposal_data.get("investment")
        or ""
    )
    if value in (None, ""):
        return "Não informado"
    try:
        if isinstance(value, str):
            normalized = value.strip().replace("R$", "").replace(".", "").replace(",", ".")
            numeric = float(normalized)
        else:
            numeric = float(value)
        return f"R$ {numeric:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except Exception:
        return str(value)


def _display_datetime(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "Não informado"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.strftime("%d/%m/%Y %H:%M")
    except Exception:
        return text


def send_acceptance_notification(proposal_data: Dict[str, Any], acceptance_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Envia aviso de aceite por e-mail, sem bloquear o aceite se o SMTP falhar.

    Configure no Render, em Environment:
    SMTP_HOST, SMTP_PORT, SMTP_USERNAME ou SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
    Opcional: ACCEPTANCE_NOTIFY_TO, SMTP_USE_TLS, SMTP_USE_SSL.

    Também aceita nomes com prefixo ORA_, por exemplo ORA_SMTP_HOST.
    """
    notify_to = _env_first("ORA_ACCEPTANCE_NOTIFY_TO", "ACCEPTANCE_NOTIFY_TO", default="comercial@grupoora.com.br")
    smtp_host = _env_first("ORA_SMTP_HOST", "SMTP_HOST", "MAIL_HOST")
    try:
        smtp_port = int(_env_first("ORA_SMTP_PORT", "SMTP_PORT", "MAIL_PORT", default="587"))
    except ValueError:
        smtp_port = 587
    smtp_user = _env_first("ORA_SMTP_USERNAME", "ORA_SMTP_USER", "SMTP_USERNAME", "SMTP_USER", "MAIL_USERNAME", "MAIL_USER")
    smtp_password = _env_first("ORA_SMTP_PASSWORD", "ORA_SMTP_PASS", "SMTP_PASSWORD", "SMTP_PASS", "MAIL_PASSWORD", "MAIL_PASS")
    smtp_from = _env_first("ORA_SMTP_FROM", "SMTP_FROM", "MAIL_FROM", default=smtp_user or "comercial@grupoora.com.br")
    smtp_from_name = _env_first("ORA_SMTP_FROM_NAME", "SMTP_FROM_NAME", "MAIL_FROM_NAME", default="Sistema ORA")

    if not smtp_host:
        return {"sent": False, "to": notify_to, "reason": "SMTP não configurado no Render"}

    proposal_number = proposal_data.get("proposalNumber") or acceptance_payload.get("proposalNumber") or proposal_data.get("id") or "Proposta"
    client_name = proposal_data.get("clientName") or acceptance_payload.get("clientName") or "Cliente não informado"
    accepted_by = acceptance_payload.get("acceptedBy") or proposal_data.get("acceptedBy") or "Não informado"
    accepted_document = acceptance_payload.get("document") or proposal_data.get("acceptanceDocument") or "Não informado"
    accepted_at = acceptance_payload.get("acceptedAt") or proposal_data.get("acceptedAt") or datetime.now(timezone.utc).isoformat()
    protocol = acceptance_payload.get("protocol") or proposal_data.get("acceptanceProtocol") or "Não informado"
    public_site_url = _env_first("ORA_PUBLIC_SITE_URL", "PUBLIC_SITE_URL", default="https://comercial.oragestao.com.br").rstrip("/")
    proposal_link = f"{public_site_url}?publicProposal={proposal_data.get('id') or acceptance_payload.get('proposalId')}"

    subject = f"Proposta aceita | {proposal_number} | {client_name}"
    body = f"""Uma proposta foi aceita no sistema ORA.

Proposta: {proposal_number}
Cliente: {client_name}
CNPJ/CPF: {proposal_data.get('cnpj') or 'Não informado'}
E-mail do cliente: {proposal_data.get('contactEmail') or 'Não informado'}
Telefone do cliente: {proposal_data.get('contactPhone') or 'Não informado'}
Modelo: {proposal_data.get('type') or proposal_data.get('templateName') or proposal_data.get('modelName') or 'Não informado'}
Vendedor: {proposal_data.get('sellerName') or 'Não informado'}
Indicação: {proposal_data.get('indicatedBy') or 'Não informado'}
Valor: {_proposal_display_value(proposal_data)}

Dados do aceite:
Aceito por: {accepted_by}
Documento informado no aceite: {accepted_document}
Data/hora do aceite: {_display_datetime(accepted_at)}
Protocolo do aceite: {protocol}

ID interno da proposta: {proposal_data.get('id') or acceptance_payload.get('proposalId') or 'Não informado'}
Link da proposta: {proposal_link}

Este aviso foi enviado automaticamente pelo ORA CRM.
"""

    message = EmailMessage()
    message["From"] = f"{smtp_from_name} <{smtp_from}>"
    message["To"] = notify_to
    message["Subject"] = subject
    message.set_content(body)

    use_ssl = _truthy_value(_env_first("ORA_SMTP_SSL", "SMTP_USE_SSL", "SMTP_SSL", "MAIL_SSL"), default=(smtp_port == 465))
    use_tls = _truthy_value(_env_first("ORA_SMTP_TLS", "SMTP_USE_TLS", "SMTP_TLS", "MAIL_TLS"), default=not use_ssl)

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15, context=ssl.create_default_context()) as server:
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                if smtp_user and smtp_password:
                    server.login(smtp_user, smtp_password)
                server.send_message(message)
        return {"sent": True, "to": notify_to}
    except Exception as exc:
        print(f"Falha ao enviar e-mail de aceite para {notify_to}: {exc}")
        return {"sent": False, "to": notify_to, "reason": str(exc)}


def save_email_audit(db: Session, proposal_id: str, notification_result: Dict[str, Any], proposal_data: Dict[str, Any]):
    try:
        audit_data = {
            "id": f"audit_accept_email_{proposal_id}_{secrets.token_hex(8)}",
            "type": "email_aceite",
            "proposalId": proposal_id,
            "proposalNumber": proposal_data.get("proposalNumber"),
            "clientName": proposal_data.get("clientName"),
            "notification": notification_result,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        db.add(AuditModel(id=audit_data["id"], data=json.dumps(audit_data, ensure_ascii=False)))
    except Exception as exc:
        print(f"Falha ao registrar auditoria de e-mail de aceite: {exc}")

# Esquemas de validação Pydantic Generic
class GenericData(BaseModel):
    id: str
    data: Dict[str, Any]

class SettingsData(BaseModel):
    data: Dict[str, Any]


# --- FRONTEND ONLINE ---
@app.get("/")
def serve_frontend():
    """Mostra o sistema ao abrir a URL principal no navegador."""
    index_path = Path(__file__).with_name("index.html")
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="index.html não encontrado")
    return FileResponse(index_path)

@app.get("/index.html")
def serve_frontend_index():
    """Permite abrir também /index.html no Render."""
    return serve_frontend()

# --- ENDPOINTS DA API ---

@app.get("/api/init")
def get_init_data(response: Response, db: Session = Depends(get_db)):
    """Carrega todo o estado inicial do sistema consolidado"""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    users = [json.loads(u.data) for u in db.query(UserModel).all()]
    deleted_ids = deleted_proposal_ids(db)
    proposals = []
    for p in db.query(ProposalModel).all():
        if p.id in deleted_ids:
            continue
        data = json.loads(p.data)
        if proposal_is_deleted(data):
            deleted_ids.add(p.id)
            continue
        proposals.append(data)
    templates = [json.loads(t.data) for t in db.query(TemplateModel).all()]
    
    settings_db = db.query(SettingModel).first()
    settings = json.loads(settings_db.data) if settings_db else {}
    
    audit_logs = [json.loads(a.data) for a in db.query(AuditModel).all()]
    
    return {
        "users": users,
        "proposals": proposals,
        "deletedProposals": list(deleted_ids),
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

    if proposal_is_deleted(payload):
        return delete_proposal_by_id(prop_id, db, payload, payload.get("deletedBy"))

    # Evita que uma proposta excluída volte para o banco por cache/localStorage antigo.
    db_deleted = db.query(DeletedProposalModel).filter(DeletedProposalModel.id == prop_id).first()
    if db_deleted:
        db_prop = db.query(ProposalModel).filter(ProposalModel.id == prop_id).first()
        if db_prop:
            db.delete(db_prop)
            db.commit()
        return {"status": "ignored_deleted", "proposal": None}
    
    db_prop = db.query(ProposalModel).filter(ProposalModel.id == prop_id).first()
    if db_prop:
        db_prop.data = json.dumps(payload)
    else:
        db_prop = ProposalModel(id=prop_id, data=json.dumps(payload))
        db.add(db_prop)
    db.commit()
    return {"status": "success", "proposal": payload}

@app.delete("/api/proposals/{prop_id}")
def delete_proposal(prop_id: str, db: Session = Depends(get_db)):
    return delete_proposal_by_id(prop_id, db)

@app.post("/api/proposals/excluir")
def delete_proposal_post(payload: Dict[str, Any], db: Session = Depends(get_db)):
    # Endpoint alternativo para garantir a exclusão quando algum navegador/proxy
    # não executar o método DELETE corretamente.
    prop_id = payload.get("id") or payload.get("proposalId")
    return delete_proposal_by_id(prop_id, db)

@app.post("/api/proposals/delete")
def delete_proposal_post_alias(payload: Dict[str, Any], db: Session = Depends(get_db)):
    # Alias em inglês usado pelo front-end como fallback.
    prop_id = payload.get("id") or payload.get("proposalId")
    return delete_proposal_by_id(prop_id, db)

# --- PROPOSTAS ---
@app.post("/api/proposals/aceitar")
def accept_proposal(payload: Dict[str, Any], db: Session = Depends(get_db)):
    proposal_id = payload.get("proposalId")
    if not proposal_id:
        raise HTTPException(status_code=400, detail="ID da proposta é obrigatório")
    
    db_prop = db.query(ProposalModel).filter(ProposalModel.id == proposal_id).first()
    if not db_prop:
        raise HTTPException(status_code=404, detail="Proposta não encontrada")
    
    proposal_data = json.loads(db_prop.data)
    accepted_at = payload.get("acceptedAt") or datetime.now(timezone.utc).isoformat()
    protocol = payload.get("protocol")

    # O front-end usa o campo `stage` para mover o card no kanban e dashboards.
    # Mantemos `etapa` e `status` por compatibilidade com integrações já existentes.
    proposal_data["stage"] = "aprovada"
    proposal_data["etapa"] = "Aprovada"
    proposal_data["status"] = "Aprovada"
    proposal_data["probability"] = 100
    proposal_data["acceptedAt"] = accepted_at
    proposal_data["acceptedBy"] = payload.get("acceptedBy")
    proposal_data["acceptanceProtocol"] = protocol
    proposal_data["acceptanceDocument"] = payload.get("document")
    proposal_data["acceptanceUserAgent"] = payload.get("userAgent")
    proposal_data["dados_aceite"] = payload
    proposal_data["updatedAt"] = datetime.now(timezone.utc).isoformat()

    previous_email_notification = proposal_data.get("acceptanceEmailNotification")
    if isinstance(previous_email_notification, dict) and previous_email_notification.get("sent") is True:
        email_notification = dict(previous_email_notification)
        email_notification["skipped"] = True
        email_notification["reason"] = "Aviso de aceite já enviado anteriormente"
        stored_email_notification = previous_email_notification
    else:
        email_notification = send_acceptance_notification(proposal_data, payload)
        stored_email_notification = email_notification
        save_email_audit(db, proposal_id, email_notification, proposal_data)

    proposal_data["acceptanceEmailNotification"] = stored_email_notification

    db_prop.data = json.dumps(proposal_data, ensure_ascii=False)
    
    db.commit()
    return {"status": "success", "proposal": proposal_data, "emailNotification": email_notification}

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
