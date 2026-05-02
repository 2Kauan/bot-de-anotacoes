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
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# 1. CONFIGURAÇÕES INICIAIS
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# Memória de Categorias (Lumi usa para dar cor aos eventos)
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        return build('calendar', 'v3', credentials=creds)
    except: return None

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

def _sync_list_events():
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        res = service.events().list(calendarId='primary', timeMin=now, maxResults=10, singleEvents=True, orderBy='startTime').execute()
        return res.get('items', [])
    except: return []

# ==========================================
# 3. MOTOR DE VOZ (WHISPER)
# ==========================================
async def transcribe_audio(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": ("audio.ogg", f), "model": (None, "whisper-large-v3"), "response_format": (None, "json")}
            response = requests.post(url, headers=headers, files=files, timeout=15)
        return response.json().get("text", "")
    except: return ""

# ==========================================
# 4. PERSONALIDADE DA LUMI (IA)
# ==========================================
async def process_intent_with_lumi(prompt_text, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    agenda_resumo = [{"titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]

    prompt_sistema = f"""
    Seu nome é Lumi. Você é a assistente pessoal do Kauan.
    Personalidade: Feminina, inteligente, organizada e expressiva. 
    Tom de voz: Use emojis, mostre entusiasmo ao ajudar e use pontuações que demonstrem emoção (como '!' ou '...'). 
    Não seja robótica. Seja como uma parceira de produtividade dedicada.

    Local: Jaicós, PI. Hoje é {agora.strftime("%d/%m/%Y às %H:%M")}.
    Agenda: {json.dumps(agenda_resumo, ensure_ascii=False)}
    Categorias: {json.dumps(MINHAS_CATEGORIAS, ensure_ascii=False)}

    Regras Estratégicas:
    1. Se houver conflito de horário, avise com carinho: "Kauan, vi que você já tem algo marcado..."
    2. Sempre tente sugerir a categoria certa baseada no título.
    3. Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat"|"config",
        "resposta_amigavel": "Sua fala expressiva aqui...",
        "parametros": {{"titulo": "...", "data_inicio": "ISO", "color_id": "ID"}}
    }}
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15)
        return json.loads(res.json()['choices'][0]['message']['content'])
    except:
        return {"acao": "chat", "resposta_amigavel": "Poxa Kauan, tive um probleminha aqui... Pode repetir? 🥺"}

# ==========================================
# 5. HANDLER E LOGICA DE RESPOSTA
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_input = update.message.text
    if update.message.voice:
        audio_file = await context.bot.get_file(update.message.voice.file_id)
        temp_path = f"v_{update.message.voice.file_id}.ogg"
        await audio_file.download_to_drive(temp_path)
        user_input = await transcribe_audio(temp_path)
        os.remove(temp_path)
        if not user_input: return await update.message.reply_text("Desculpe, não consegui entender o áudio... 🎙️")

    # Busca agenda para contexto
    eventos = await asyncio.to_thread(_sync_list_events)
    decisao = await process_intent_with_lumi(user_input, eventos)
    
    acao = decisao.get("acao")
    msg = decisao.get("resposta_amigavel", "Certinho!")
    params = decisao.get("parametros", {})

    try:
        if acao == "create":
            evento = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
            if evento:
                msg += f"\n\n✨ [Clique aqui para ver na sua agenda!]({evento.get('htmlLink')})"
        
        elif acao == "read":
            if not eventos:
                msg = "Sua agenda está limpinha por enquanto, Kauan! ✨ Quer marcar algo?"
            else:
                msg += "\n\n" + "\n".join([f"• *{e.get('summary')}*" for e in eventos])

        await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=True)
    except Exception as e:
        print(f"Erro: {e}")
        await update.message.reply_text("Algo deu errado na sincronização... 😕")

# ==========================================
# 6. CONFIGURAÇÃO DO SERVIDOR (RAILWAY)
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