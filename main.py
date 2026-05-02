import os
import json
import arrow
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from loguru import logger
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ==========================================
# 1. CONFIGURAÇÕES BÁSICAS
# ==========================================
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# ==========================================
# 2. FERRAMENTAS DE AGENDA (CRUD SIMPLIFICADO)
# ==========================================
def get_calendar():
    try:
        token_data = os.getenv("GOOGLE_TOKEN")
        creds = Credentials.from_authorized_user_info(json.loads(token_data))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def create_event(titulo, data_iso):
    service = get_calendar()
    if not service: return None
    try:
        # Forçamos o fuso de Jaicós e duração de 1h
        start = arrow.get(data_iso).replace(tzinfo='America/Sao_Paulo')
        event = {
            'summary': titulo,
            'start': {'dateTime': start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except: return None

def list_events(time_min, time_max):
    service = get_calendar()
    if not service: return []
    try:
        res = service.events().list(
            calendarId='primary', timeMin=time_min, timeMax=time_max,
            singleEvents=True, orderBy='startTime'
        ).execute()
        return res.get('items', [])
    except: return []

def delete_event(event_id):
    service = get_calendar()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except: return False

# ==========================================
# 3. ÁUDIO (WHISPER)
# ==========================================
async def transcribe_audio(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": ("audio.ogg", f), "model": (None, "whisper-large-v3")}
            response = requests.post(url, headers=headers, files=files, timeout=20)
        return response.json().get("text", "")
    except: return ""

# ==========================================
# 4. INTELIGÊNCIA DA LUMI (PROMPT ENXUTO)
# ==========================================
async def ask_lumi(user_input, agenda_context="Nenhum evento"):
    agora = arrow.now('America/Sao_Paulo')
    
    # Prompt simplificado para o modelo 8B não se perder
    system_prompt = f"""
    Nome: Lumi. Papel: Assistente do Kauan em Jaicós, PI.
    Hoje: {agora.format('DD/MM/YYYY HH:mm')}.
    
    AGENDA ATUAL: {agenda_context}

    REGRAS:
    - Para AGENDAR: use acao="create", forneça titulo e data_inicio (ISO).
    - Para LISTAR: use acao="read".
    - Para APAGAR: use acao="delete" e forneça o event_id.
    - Seja carinhosa, use emojis e responda em JSON plano.
    """

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
                "response_format": {"type": "json_object"}
            }, timeout=15
        )
        return res.json()['choices'][0]['message']['content']
    except:
        return json.dumps({"acao": "chat", "resposta_amigavel": "Desculpa Kauan, me perdi aqui... pode falar de novo? 🥺"})

# ==========================================
# 5. HANDLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # Lida com Voz ou Texto
    user_text = update.message.text
    if update.message.voice:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        path = f"v_{update.message.voice.file_id}.ogg"
        await voice_file.download_to_drive(path)
        user_text = await transcribe_audio(path)
        os.remove(path)
        if not user_text: return await update.message.reply_text("Não consegui te ouvir... 🎙️")

    # Busca eventos do dia para contexto
    hoje = arrow.now('America/Sao_Paulo')
    eventos = list_events(hoje.floor('day').isoformat(), hoje.ceil('day').isoformat())
    contexto = "\n".join([f"ID: {e['id']} | {e.get('summary')}" for e in eventos])

    # IA Decide
    raw_res = await ask_lumi(user_text, contexto)
    try:
        data = json.loads(raw_res)
        acao = data.get("acao")
        resposta = data.get("resposta_amigavel", "Certinho!")
        
        if acao == "create":
            ev = create_event(data.get("titulo"), data.get("data_inicio"))
            if ev: resposta += f"\n\n✨ [Evento Criado!]({ev.get('htmlLink')})"
        
        elif acao == "delete":
            if delete_event(data.get("event_id")):
                resposta = "🗑️ Pronto! Compromisso removido. ✨"
        
        elif acao == "read":
            if not eventos:
                resposta += "\n\nSua agenda está livre hoje! 🌸"
            else:
                lista = "\n".join([f"⏰ *{arrow.get(e['start'].get('dateTime')).format('HH:mm')}* - {e.get('summary')}" for e in eventos])
                resposta += f"\n\n{lista}"

        await update.message.reply_text(resposta, parse_mode='Markdown', disable_web_page_preview=True)
    except:
        await update.message.reply_text("Tive um erro de processamento, Kauan. 😕")

# ==========================================
# 6. SERVIDOR
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
async def webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"ok": True}