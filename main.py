import os
import json
import datetime
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
import google.generativeai as genai  # Alterado para o SDK estável
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
load_dotenv()
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()

# Configuração do SDK Estável
genai.configure(api_key=GEMINI_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

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
        print(f"Erro no Token Google: {e}")
        return None

def _sync_list_events():
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    return service.events().list(calendarId='primary', timeMin=now, maxResults=15, singleEvents=True, orderBy='startTime').execute().get('items', [])

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
    service.events().delete(calendarId='primary', eventId=event_id).execute()
    return True

async def calendar_action(action, **kwargs):
    if action == "list": return await asyncio.to_thread(_sync_list_events)
    elif action == "create": return await asyncio.to_thread(_sync_create_event, kwargs.get("titulo"), kwargs.get("data"))
    elif action == "delete": return await asyncio.to_thread(_sync_delete_event, kwargs.get("id"))

# ==========================================
# 3. MOTOR DE INTELIGÊNCIA
# ==========================================
async def process_intent_with_ai(prompt_text, current_events):
    agora = datetime.datetime.now()
    limite_48h = agora + datetime.timedelta(hours=48)
    
    eventos_simplificados = [{"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]
    
    prompt = f"""
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")}.
    Agenda: {json.dumps(eventos_simplificados, ensure_ascii=False)}
    
    REGRA: IDs de eventos após {limite_48h.strftime("%Y-%m-%d %H:%M:%S")} NÃO podem ser deletados.

    Mensagem: "{prompt_text}"
    
    Retorne JSON: {{"acao": "create"|"read"|"update"|"delete"|"chat", "resposta_amigavel": "...", "parametros": {{"titulo": "...", "data_inicio": "ISO", "event_ids": []}}}}
    """
    
    # Chamada usando o SDK clássico e estável
    response = await asyncio.to_thread(
        model.generate_content,
        prompt,
        generation_config={"response_mime_type": "application/json"}
    )
    
    return json.loads(response.text)

# ==========================================
# 4. CONTROLLER TELEGRAM
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        eventos = await calendar_action("list")
        decisao = await process_intent_with_ai(update.message.text, eventos)
        
        acao = decisao.get("acao")
        params = decisao.get("parametros", {})

        if acao == "create":
            await calendar_action("create", titulo=params.get("titulo"), data=params.get("data_inicio"))
        elif acao == "delete":
            for eid in params.get("event_ids", []):
                await calendar_action("delete", id=eid)

        await update.message.reply_text(decisao.get("resposta_amigavel"), parse_mode='Markdown')
    except Exception as e:
        print(f"Erro no handle: {e}")
        await update.message.reply_text("❌ Erro ao processar. Tente novamente.")

# ==========================================
# 5. FASTAPI & WEBHOOK
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    print("🚀 API Online na Railway (SDK Estável).")
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
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)