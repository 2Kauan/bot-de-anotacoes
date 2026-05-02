import os
import json
import datetime
import asyncio
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ==========================================
# 1. CONFIGURAÇÕES
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def _sync_list_events(time_min=None, time_max=None):
    service = get_calendar_service()
    if not service: return []
    try:
        res = service.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return res.get('items', [])
    except: return []

def _sync_delete_event(event_id):
    service = get_calendar_service()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except: return False

def _sync_create_event(titulo, data_iso, color_id=None):
    service = get_calendar_service()
    if not service: return None
    try:
        dt_start = datetime.datetime.fromisoformat(data_iso.replace('Z', '-03:00'))
        event = {
            'summary': titulo,
            'colorId': color_id if color_id else "1",
            'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': (dt_start + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except: return None

# ==========================================
# 3. MOTOR LUMI (CÉREBRO COM TRAVA DE INTENÇÃO)
# ==========================================
async def process_with_lumi_brain(user_input, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    # Passamos os eventos REAIS para ela saber o que apagar
    agenda_contexto = [{"id": e['id'], "summary": e.get('summary'), "time": e['start'].get('dateTime')} for e in current_events]

    prompt_sistema = f"""
    Seu nome é Lumi. Assistente pessoal de Jaicós, PI.
    Hoje é {agora.strftime("%d/%m/%Y %H:%M")}.

    REGRAS CRÍTICAS DE INTENÇÃO:
    1. Se o usuário usar verbos como "APAGAR", "REMOVER", "EXCLUIR" ou "CANCELAR", você deve obrigatoriamente usar acao='delete'.
    2. Para APAGAR, escolha o 'event_id' correto da lista abaixo. Jamais crie um evento com o título "Apagar".
    3. Para CRIAR, use acao='create'.
    
    AGENDA ATUAL DISPONÍVEL:
    {json.dumps(agenda_contexto, ensure_ascii=False)}

    Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat",
        "resposta_amigavel": "...",
        "parametros": {{"event_id": "ID_PARA_APAGAR", "titulo": "...", "data_inicio": "ISO", "color_id": "ID"}}
    }}
    """
    
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
                "response_format": {"type": "json_object"}
            },
            headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15
        )
        return json.loads(res.json()['choices'][0]['message']['content'])
    except:
        return {"acao": "chat", "resposta_amigavel": "Tive um probleminha aqui, Kauan... pode repetir? 🥺"}

# ==========================================
# 4. HANDLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # 1. Busca eventos do dia para dar contexto à Lumi
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    hoje_inicio = datetime.datetime.now(fuso).replace(hour=0, minute=0, second=0).isoformat() + 'Z'
    hoje_fim = datetime.datetime.now(fuso).replace(hour=23, minute=59, second=59).isoformat() + 'Z'
    eventos_dia = await asyncio.to_thread(_sync_list_events, hoje_inicio, hoje_fim)

    # 2. Processa com a IA
    decisao = await process_with_lumi_brain(update.message.text or "", eventos_dia)
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "")
    params = decisao.get("parametros", {})

    # 3. Execução
    if acao == "delete":
        eid = params.get("event_id")
        if eid:
            sucesso = await asyncio.to_thread(_sync_delete_event, eid)
            msg_lumi = "🗑️ Prontinho, Kauan! Já apaguei esse compromisso para você. ✨" if sucesso else "Poxa, não consegui apagar esse evento... 😕"
        else:
            msg_lumi = "Kauan, você quer apagar qual evento exatamente? Não consegui identificar qual deles... 🧐"

    elif acao == "create":
        ev = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
        if ev: msg_lumi += f"\n\n✨ [Evento criado!]({ev.get('htmlLink')})"

    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 5. SERVIDOR
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    yield
    await bot_app.stop()

app = FastAPI(lifespan=lifespan)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"status": "ok"}