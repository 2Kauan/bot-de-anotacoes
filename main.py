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
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY") # Mude para a sua chave da Groq
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    try:
        if not token_json: return None
        creds_dict = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_dict)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Erro Calendar: {e}")
        return None

def _sync_list_events():
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=15, 
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        return []

def _sync_create_event(titulo, data_iso):
    service = get_calendar_service()
    if not service: return None
    dt = datetime.datetime.fromisoformat(data_iso)
    event = {
        'summary': titulo,
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
    except:
        return False

async def calendar_action(action, **kwargs):
    if action == "list": return await asyncio.to_thread(_sync_list_events)
    elif action == "create": return await asyncio.to_thread(_sync_create_event, kwargs.get("titulo"), kwargs.get("data"))
    elif action == "delete": return await asyncio.to_thread(_sync_delete_event, kwargs.get("id"))

# ==========================================
# 3. MOTOR DE IA (GROQ - MAIS RÁPIDO E ESTÁVEL)
# ==========================================
async def process_intent_with_ai(prompt_text, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    limite_48h = agora + datetime.timedelta(hours=48)
    
    eventos_simplificados = [
        {"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} 
        for e in current_events
    ]
    
    prompt_sistema = f"""
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")}. Local: Jaicós, PI.
    Agenda Atual: {json.dumps(eventos_simplificados, ensure_ascii=False)}
    Retorne APENAS JSON puro: 
    {{
        "acao": "create"|"read"|"delete"|"chat", 
        "resposta_amigavel": "...", 
        "parametros": {{"titulo": "...", "data_inicio": "ISO", "event_ids": []}}
    }}
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "llama-3.3-70b-specdec", # Modelo de altíssima performance
        "messages": [
            {"role": "system", "content": prompt_sistema},
            {"role": "user", "content": prompt_text}
        ],
        "response_format": {"type": "json_object"}
    }

    try:
        response = await asyncio.to_thread(requests.post, url, json=payload, headers=headers, timeout=10)
        res_json = response.json()
        texto_ia = res_json['choices'][0]['message']['content']
        return json.loads(texto_ia)
    except Exception as e:
        print(f"Erro Groq: {e}")
        return {"acao": "chat", "resposta_amigavel": "❌ Erro na Groq.", "parametros": {}}

# ==========================================
# 4. CONTROLLER & FASTAPI (IGUAL ANTERIOR)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    eventos = await calendar_action("list")
    decisao = await process_intent_with_ai(update.message.text, eventos)
    
    if decisao.get("acao") == "create":
        await calendar_action("create", titulo=decisao['parametros'].get("titulo"), data=decisao['parametros'].get("data_inicio"))
    elif decisao.get("acao") == "delete":
        for eid in decisao['parametros'].get("event_ids", []):
            await calendar_action("delete", id=eid)

    await update.message.reply_text(decisao.get("resposta_amigavel"), parse_mode='Markdown')

bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    yield
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    asyncio.create_task(bot_app.process_update(update))
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)