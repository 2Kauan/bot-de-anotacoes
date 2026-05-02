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
# Inverso para exibição:
ID_PARA_NOME = {v: k for k, v in MINHAS_CATEGORIAS.items()}

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
            calendarId='primary', 
            timeMin=time_min, 
            timeMax=time_max,
            singleEvents=True, 
            orderBy='startTime'
        ).execute()
        return res.get('items', [])
    except: return []

def _sync_create_event(titulo, data_iso, color_id=None):
    service = get_calendar_service()
    if not service: return None
    dt = datetime.datetime.fromisoformat(data_iso)
    event = {
        'summary': titulo,
        'colorId': color_id,
        'start': {'dateTime': dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (dt + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

# ==========================================
# 3. MOTOR LUMI 2.0
# ==========================================
async def process_with_lumi_brain(user_input, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    prompt_sistema = f"""
    Você é a Lumi, assistente do Kauan em Jaicós, PI.
    Hoje é {agora.strftime("%A, %d/%m/%Y")}. Hora: {agora.strftime("%H:%M")}.
    
    Personalidade: Feminina, expressiva, organizada e gentil.
    Histórico: {" | ".join(list(historico_conversa))}

    Se o usuário perguntar o que tem amanhã ou hoje, use acao='read'.
    Sempre identifique o intervalo de tempo correto (ISO FORMAT) nos parametros.

    JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat",
        "resposta_amigavel": "Sua introdução carinhosa...",
        "parametros": {{"titulo": "...", "time_min": "ISO", "time_max": "ISO", "color_id": "ID"}}
    }}
    """

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions", 
                            json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15)
        return json.loads(res.json()['choices'][0]['message']['content'])
    except: return {"acao": "chat", "resposta_amigavel": "Poxa, me perdi aqui... 🥺"}

# ==========================================
# 4. HANDLER (LÓGICA DE LISTAGEM DETALHADA)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_msg = update.message.text or "[Mídia]"
    decisao = await process_with_lumi_brain(user_msg, [])
    
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "")
    params = decisao.get("parametros", {})

    # Lógica de LEITURA detalhada
    if acao == "read":
        t_min = params.get("time_min") or datetime.datetime.utcnow().isoformat() + 'Z'
        t_max = params.get("time_max")
        
        eventos = await asyncio.to_thread(_sync_list_events, t_min, t_max)
        
        if not eventos:
            msg_lumi += "\n\n✨ Sua agenda está livre para esse período! Quer aproveitar para descansar?"
        else:
            msg_lumi += "\n\n"
            for e in eventos:
                inicio = e['start'].get('dateTime', e['start'].get('date'))
                dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00')).astimezone(datetime.timezone(datetime.timedelta(hours=-3)))
                
                cat = ID_PARA_NOME.get(e.get('colorId'), "Geral")
                emoji = "🏷️" if cat == "Geral" else "📌"
                
                msg_lumi += f"⏰ *{dt.strftime('%H:%M')}* - {e.get('summary')}\n"
                msg_lumi += f"{emoji} Categoria: _{cat}_\n\n"

    elif acao == "create":
        ev = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
        if ev: msg_lumi += f"\n\n✨ [Link da Agenda]({ev.get('htmlLink')})"

    # Salva histórico e responde
    historico_conversa.append(f"Kauan: {user_msg} | Lumi: {msg_lumi}")
    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 5. SERVER
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_update))

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