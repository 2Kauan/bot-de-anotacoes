import os
import json
import datetime
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
load_dotenv()
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not GEMINI_KEY or not TELEGRAM_TOKEN:
    raise ValueError("Chaves da IA ou do Telegram faltando no .env.")

ia_client = genai.Client(api_key=GEMINI_KEY)
lembretes_enviados = set()

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    creds_dict = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(creds_dict)
    return build('calendar', 'v3', credentials=creds)

def _sync_list_events(max_days_ahead=30):
    service = get_calendar_service()
    now = datetime.datetime.utcnow()
    time_max = (now + datetime.timedelta(days=max_days_ahead)).isoformat() + 'Z'
    now_iso = now.isoformat() + 'Z'
    
    events_result = service.events().list(
        calendarId='primary', timeMin=now_iso, timeMax=time_max, 
        maxResults=50, singleEvents=True, orderBy='startTime'
    ).execute()
    
    return events_result.get('items', [])

def _sync_create_event(titulo: str, data_inicio_iso: str):
    service = get_calendar_service()
    data_inicio = datetime.datetime.fromisoformat(data_inicio_iso)
    event = {
        'summary': titulo,
        'start': {'dateTime': data_inicio.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (data_inicio + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

def _sync_update_event(event_id: str, titulo: str, data_inicio_iso: str):
    service = get_calendar_service()
    data_inicio = datetime.datetime.fromisoformat(data_inicio_iso)
    event = service.events().get(calendarId='primary', eventId=event_id).execute()
    if titulo: event['summary'] = titulo
    if data_inicio_iso:
        event['start'] = {'dateTime': data_inicio.isoformat(), 'timeZone': 'America/Sao_Paulo'}
        event['end'] = {'dateTime': (data_inicio + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'}
    return service.events().update(calendarId='primary', eventId=event_id, body=event).execute()

def _sync_delete_event(event_id: str):
    service = get_calendar_service()
    service.events().delete(calendarId='primary', eventId=event_id).execute()
    return True

async def calendar_action(action: str, **kwargs):
    if action == "list": return await asyncio.to_thread(_sync_list_events)
    elif action == "create": return await asyncio.to_thread(_sync_create_event, kwargs.get("titulo"), kwargs.get("data"))
    elif action == "update": return await asyncio.to_thread(_sync_update_event, kwargs.get("id"), kwargs.get("titulo"), kwargs.get("data"))
    elif action == "delete": return await asyncio.to_thread(_sync_delete_event, kwargs.get("id"))

# ==========================================
# 3. MOTOR DE INTELIGÊNCIA (GEMINI 1.5 FLASH)
# ==========================================
async def process_intent_with_ai(prompt_text: str, current_events: list, audio_path: str = None):
    agora = datetime.datetime.now()
    eventos_simplificados = [{"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]
    
    prompt = f"""
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")} ({agora.strftime("%A")}).
    Contexto da agenda (30 dias): {json.dumps(eventos_simplificados, ensure_ascii=False)}
    
    {'O usuário enviou um ÁUDIO. Transcreva a intenção dele.' if audio_path else f'Mensagem: "{prompt_text}"'}
    
    Retorne UM JSON com: 
    "acao": (create, read, update, delete, chat), 
    "resposta_amigavel": (o texto para o usuário), 
    "parametros": {{
        "titulo": (string ou nulo), 
        "data_inicio": (string ISO 8601 ou nulo), 
        "event_ids": (lista de strings com os IDs que sofrerão alteração ou exclusão)
    }}
    """
    
    contents = []
    gemini_file = None
    
    if audio_path:
        gemini_file = await asyncio.to_thread(ia_client.files.upload, file=audio_path)
        contents.append(gemini_file)
    
    contents.append(prompt)
    
    # REMOVIDO SAFETY_SETTINGS: Para evitar erros de validação de Enums no SDK
    response = await asyncio.to_thread(
        ia_client.models.generate_content,
        model='gemini-1.5-flash', 
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json"
        )
    )
    
    if gemini_file:
        await asyncio.to_thread(ia_client.files.delete, name=gemini_file.name)
        
    return json.loads(response.text)

# ==========================================
# 4. ROTINA DE LEMBRETES (WORKER)
# ==========================================
async def verificar_lembretes():
    if not TELEGRAM_CHAT_ID: return
    try:
        eventos = await calendar_action("list")
        agora = datetime.datetime.now(datetime.timezone.utc)
        for evento in eventos:
            inicio_str = evento['start'].get('dateTime')
            if not inicio_str: continue
            inicio_dt = datetime.datetime.fromisoformat(inicio_str.replace('Z', '+00:00'))
            minutos_faltando = (inicio_dt - agora).total_seconds() / 60
            evento_id = evento['id']
            titulo = evento.get('summary', 'Evento')
            
            id_1dia = f"{evento_id}_1dia"
            if 23 * 60 <= minutos_faltando <= 24.5 * 60 and id_1dia not in lembretes_enviados:
                await bot_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"📅 Amanhã: **{titulo}**.", parse_mode='Markdown')
                lembretes_enviados.add(id_1dia)
            
            id_15min = f"{evento_id}_15min"
            if 10 <= minutos_faltando <= 15 and id_15min not in lembretes_enviados:
                await bot_app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=f"⏳ Em 15 min: **{titulo}**!", parse_mode='Markdown')
                lembretes_enviados.add(id_15min)
    except Exception as e:
        print(f"Erro worker: {e}")

# ==========================================
# 5. CONTROLLER TELEGRAM
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global TELEGRAM_CHAT_ID
    if update.effective_chat:
        TELEGRAM_CHAT_ID = str(update.effective_chat.id)

    message = update.message
    if not message: return
    
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    except:
        pass
    
    audio_path = None
    texto = message.text or ""
    
    try:
        if message.voice:
            new_file = await context.bot.get_file(message.voice.file_id)
            audio_path = f"temp_{message.voice.file_id}.ogg"
            await new_file.download_to_drive(audio_path)
        
        eventos_atuais = await calendar_action("list")
        decisao = await process_intent_with_ai(texto, eventos_atuais, audio_path)
        
        if audio_path and os.path.exists(audio_path):
            os.remove(audio_path)

        acao = decisao.get("acao")
        params = decisao.get("parametros", {})
        msg_resposta = decisao.get("resposta_amigavel", "Processado.")

        if acao == "create": 
            await calendar_action("create", titulo=params.get("titulo"), data=params.get("data_inicio"))
        elif acao == "update":
            ids = params.get("event_ids", [])
            for eid in ids:
                await calendar_action("update", id=eid, titulo=params.get("titulo"), data=params.get("data_inicio"))
        elif acao == "delete": 
            ids = params.get("event_ids", [])
            if not ids: raise ValueError("Nenhum evento encontrado.")
            for eid in ids:
                await calendar_action("delete", id=eid)

        await message.reply_text(msg_resposta, parse_mode='Markdown')

    except Exception as e:
        if audio_path and os.path.exists(audio_path): os.remove(audio_path)
        
        error_msg = str(e)
        if "429" in error_msg:
            await message.reply_text("⏳ *Muitas solicitações!* Aguarde 30 segundos.")
        else:
            print(f"Erro: {e}")
            await message.reply_text("❌ Tive um problema ao processar isso. Tente novamente.")

# ==========================================
# 6. FASTAPI & LIFESPAN
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_update))
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    scheduler.add_job(verificar_lembretes, 'interval', minutes=5)
    scheduler.start()
    print("🚀 API Online - Sistema de Agenda Simplificado Ativado.")
    yield
    scheduler.shutdown()
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    json_data = await request.json()
    update = Update.de_json(json_data, bot_app.bot)
    asyncio.create_task(bot_app.process_update(update))
    return {"status": "ok"}