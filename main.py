import os
import json
import arrow
import requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from loguru import logger
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes, CommandHandler
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
# 2. FERRAMENTAS DE AGENDA (CRUD)
# ==========================================
def get_calendar():
    try:
        token_data = os.getenv("GOOGLE_TOKEN")
        creds = Credentials.from_authorized_user_info(json.loads(token_data))
        return build('calendar', 'v3', credentials=creds)
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

def create_event(titulo, data_iso):
    service = get_calendar()
    if not service: return None
    try:
        start = arrow.get(data_iso).replace(tzinfo='America/Sao_Paulo')
        event = {
            'summary': titulo,
            'start': {'dateTime': start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except: return None

def delete_event(event_id):
    service = get_calendar()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except: return False

# ==========================================
# 3. COMANDOS DIRETOS (SEM IA - 100% SEGURO)
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🌸 *Lumi Online!* Como posso ajudar, Kauan?\n\n"
        "🚀 *Comandos Rápidos:*\n"
        "/hoje - Ver sua agenda de hoje\n"
        "/amanha - Ver sua agenda de amanhã\n"
        "/limpar - Apagar TUDO de hoje\n\n"
        "Ou pode me mandar áudio ou texto que eu uso minha inteligência! ✨"
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def hoje_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoje = arrow.now('America/Sao_Paulo')
    eventos = list_events(hoje.floor('day').isoformat(), hoje.ceil('day').isoformat())
    if not eventos:
        return await update.message.reply_text("Agenda limpinha para hoje! 🌸")
    lista = "\n".join([f"⏰ *{arrow.get(e['start'].get('dateTime')).format('HH:mm')}* - {e.get('summary')}" for e in eventos])
    await update.message.reply_text(f"📅 *Hoje em Jaicós:*\n\n{lista}", parse_mode='Markdown')

async def amanha_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amanha = arrow.now('America/Sao_Paulo').shift(days=1)
    eventos = list_events(amanha.floor('day').isoformat(), amanha.ceil('day').isoformat())
    if not eventos:
        return await update.message.reply_text("Nada agendado para amanhã ainda! ✨")
    lista = "\n".join([f"⏰ *{arrow.get(e['start'].get('dateTime')).format('HH:mm')}* - {e.get('summary')}" for e in eventos])
    await update.message.reply_text(f"📅 *Amanhã:*\n\n{lista}", parse_mode='Markdown')

async def limpar_hoje(update: Update, context: ContextTypes.DEFAULT_TYPE):
    hoje = arrow.now('America/Sao_Paulo')
    eventos = list_events(hoje.floor('day').isoformat(), hoje.ceil('day').isoformat())
    if not eventos:
        return await update.message.reply_text("Nada para limpar hoje! 😂")
    for e in eventos:
        delete_event(e['id'])
    await update.message.reply_text("🗑️ TUDO limpo! Sua agenda de hoje foi zerada. ✨")

# ==========================================
# 4. INTELIGÊNCIA ARTIFICIAL (VOZ E TEXTO)
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

async def ask_lumi(user_input, context_str):
    agora = arrow.now('America/Sao_Paulo')
    system_prompt = f"""
    Nome: Lumi. Assistente do Kauan em Jaicós, PI.
    Agora: {agora.format('DD/MM/YYYY HH:mm')}.
    AGENDA RECENTE (Ontem a Amanhã): {context_str}

    REGRAS:
    - Responder APENAS JSON plano.
    - Se for agendar: acao="create", titulo, data_inicio (ISO).
    - Se for apagar: acao="delete", event_id (pegue o ID correto no contexto).
    - Se for listar: acao="read".
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
    except: return json.dumps({"acao": "chat", "resposta_amigavel": "Me enrolei aqui... pode falar de novo? 🥺"})

# ==========================================
# 5. HANDLER PRINCIPAL (FLUXO IA)
# ==========================================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_text = update.message.text
    if update.message.voice:
        voice_file = await context.bot.get_file(update.message.voice.file_id)
        path = f"v_{update.message.voice.file_id}.ogg"
        await voice_file.download_to_drive(path)
        user_text = await transcribe_audio(path)
        os.remove(path)

    # Busca janela de 3 dias para contexto da IA
    hoje = arrow.now('America/Sao_Paulo')
    eventos_contexto = list_events(hoje.shift(days=-1).floor('day').isoformat(), hoje.shift(days=1).ceil('day').isoformat())
    contexto_str = "\n".join([f"ID: {e['id']} | Data: {arrow.get(e['start'].get('dateTime')).format('DD/MM')} | {e.get('summary')}" for e in eventos_contexto])

    raw_res = await ask_lumi(user_text, contexto_str)
    try:
        data = json.loads(raw_res)
        acao = data.get("acao")
        resposta = data.get("resposta_amigavel", "Certinho!")
        
        if acao == "create":
            ev = create_event(data.get("titulo"), data.get("data_inicio"))
            if ev: resposta += f"\n\n✨ [Evento Criado!]({ev.get('htmlLink')})"
        elif acao == "delete" and data.get("event_id"):
            if delete_event(data.get("event_id")):
                resposta = "🗑️ Prontinho! Já removi esse compromisso para você. ✨"
        elif acao == "read":
            lista = "\n".join([f"⏰ *{arrow.get(e['start'].get('dateTime')).format('HH:mm')}* - {e.get('summary')}" for e in eventos_contexto])
            resposta += f"\n\n{lista or 'Sua agenda está limpa! 🌸'}"

        await update.message.reply_text(resposta, parse_mode='Markdown', disable_web_page_preview=True)
    except:
        await update.message.reply_text("Ops, tive um erro de processamento. 😕")

# ==========================================
# 6. SERVER & INICIALIZAÇÃO
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Configuração de Handlers
bot_app.add_handler(CommandHandler("start", start_command))
bot_app.add_handler(CommandHandler("hoje", hoje_command))
bot_app.add_handler(CommandHandler("amanha", amanha_command))
bot_app.add_handler(CommandHandler("limpar", limpar_hoje))
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_message))

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