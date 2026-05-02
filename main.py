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
from collections import deque

# ==========================================
# 1. CONFIGURAÇÕES E MEMÓRIA
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

historico_conversa = deque(maxlen=8)
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}
ID_PARA_NOME = {v: k for k, v in MINHAS_CATEGORIAS.items()}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR (CRUD COMPLETO)
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

def _sync_create_event(titulo, data_iso, color_id=None):
    service = get_calendar_service()
    if not service: return None
    dt = datetime.datetime.fromisoformat(data_iso)
    # Garante que o evento dure 1 hora por padrão
    event = {
        'summary': titulo,
        'colorId': color_id,
        'start': {'dateTime': dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (dt + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

def _sync_delete_event(event_id):
    service = get_calendar_service()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except: return False

# ==========================================
# 3. MOTOR LUMI (CÉREBRO EXECUTOR)
# ==========================================
async def process_with_lumi_brain(user_input, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    eventos_simplificados = [{"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]

    prompt_sistema = f"""
    Nome: Lumi. Papel: Assistente pessoal do Kauan em Jaicós, PI.
    Hoje: {agora.strftime("%d/%m/%Y %H:%M")}.
    Agenda Atual: {json.dumps(eventos_simplificados, ensure_ascii=False)}

    Instruções:
    - Para AGENDAR: use acao='create'. Identifique a categoria correta: {json.dumps(MINHAS_CATEGORIAS)}.
    - Para APAGAR: use acao='delete' e forneça o 'event_id' exato da agenda acima.
    - Para LISTAR: use acao='read' com time_min e time_max.

    Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat",
        "resposta_amigavel": "...",
        "parametros": {{"titulo": "...", "data_inicio": "ISO", "event_id": "...", "time_min": "...", "time_max": "...", "color_id": "..."}}
    }}
    """
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
        "response_format": {"type": "json_object"}
    }
    res = requests.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15)
    return json.loads(res.json()['choices'][0]['message']['content'])

# ==========================================
# 4. HANDLER DE AÇÃO (LÓGICA FINAL)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # Busca contexto para a Lumi saber o que pode apagar ou onde tem conflito
    eventos_atuais = await asyncio.to_thread(_sync_list_events)
    decisao = await process_with_lumi_brain(update.message.text or "", eventos_atuais)
    
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "")
    params = decisao.get("parametros", {})

    try:
        if acao == "create":
            ev = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
            if ev: msg_lumi += f"\n\n✨ [Evento criado! Clique para ver]({ev.get('htmlLink')})"
        
        elif acao == "delete":
            sucesso = await asyncio.to_thread(_sync_delete_event, params.get("event_id"))
            if sucesso: msg_lumi = "🗑️ Prontinho, Kauan! Já removi esse compromisso da sua agenda. ✨"
            else: msg_lumi = "Vish, não consegui apagar esse... Tem certeza que ele ainda existe? 🧐"

        elif acao == "read":
            fuso = datetime.timezone(datetime.timedelta(hours=-3))
            t_min = params.get("time_min") or datetime.datetime.now(fuso).replace(hour=0,minute=0,second=0).isoformat() + 'Z'
            t_max = params.get("time_max") or datetime.datetime.now(fuso).replace(hour=23,minute=59,second=59).isoformat() + 'Z'
            eventos = await asyncio.to_thread(_sync_list_events, t_min, t_max)
            
            if not eventos: msg_lumi += "\n\nAgenda limpinha por aqui! 🌸"
            else:
                msg_lumi += "\n\n"
                for e in eventos:
                    inicio = e['start'].get('dateTime', e['start'].get('date'))
                    dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00')).astimezone(fuso)
                    msg_lumi += f"⏰ *{dt.strftime('%H:%M')}* - {e.get('summary')}\n"

        await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)
    except Exception as e:
        print(f"Erro: {e}")
        await update.message.reply_text("Tive um probleminha técnico aqui, Kauan... 🥺")

# ==========================================
# 5. SERVER
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