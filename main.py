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
from collections import deque

# ==========================================
# 1. CONFIGURAÇÕES E MEMÓRIA DA LUMI
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# Memória de Curto Prazo (Últimas 8 interações)
historico_conversa = deque(maxlen=8)
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

def _sync_list_events(max_results=10):
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        res = service.events().list(calendarId='primary', timeMin=now, maxResults=max_results, singleEvents=True, orderBy='startTime').execute()
        return res.get('items', [])
    except: return []

# ==========================================
# 3. MOTOR DE VOZ (GROQ WHISPER)
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
# 4. INTELIGÊNCIA DA LUMI (IA + MEMÓRIA)
# ==========================================
async def process_with_lumi_brain(user_input, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    agenda_contexto = [{"titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]
    contexto_conversa = "\n".join(list(historico_conversa))

    prompt_sistema = f"""
    Seu nome é Lumi. Você é a assistente pessoal do Kauan em Jaicós, PI.
    Hoje é {agora.strftime("%A, %d de %B de %Y")}. Hora atual: {agora.strftime("%H:%M")}.
    
    PERSONALIDADE: Feminina, expressiva, usa muitos emojis, carinhosa e atenciosa.
    
    HISTÓRICO RECENTE:
    {contexto_conversa}

    AGENDA ATUAL:
    {json.dumps(agenda_contexto, ensure_ascii=False)}

    REGRAS:
    1. Datas Relativas: Hoje é dia {agora.day}. Se ele disser 'amanhã', é dia { (agora + datetime.timedelta(days=1)).day }.
    2. Conflitos: Se o horário bater com a agenda acima, avise com carinho! 🌸
    3. Categorias: {json.dumps(MINHAS_CATEGORIAS, ensure_ascii=False)}

    RETORNE APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat"|"config",
        "resposta_amigavel": "...",
        "parametros": {{"titulo": "...", "data_inicio": "ISO FORMAT", "color_id": "ID"}}
    }}
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instant", # Versão estável para evitar erros de cota
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15)
        return json.loads(res.json()['choices'][0]['message']['content'])
    except:
        return {"acao": "chat", "resposta_amigavel": "Kauan, tive um probleminha aqui... Pode falar de novo? 🥺"}

# ==========================================
# 5. TAREFAS PROATIVAS (LUMI TE CHAMA)
# ==========================================
async def send_daily_briefing():
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    eventos = _sync_list_events(max_results=15)
    if not eventos:
        msg = "Bom dia, Kauan! ✨ Sua agenda está livre hoje. Vamos planejar algo incrível? 🥰"
    else:
        lista = "\n".join([f"• *{e.get('summary')}*" for e in eventos])
        msg = f"Bom dia, Kauan! ☀️\n\nAqui está seu dia em Jaicós:\n\n{lista}\n\nEstou aqui se precisar! 🌸"
    await bot_app.bot.send_message(chat_id=MEU_ID_TELEGRAM, text=msg, parse_mode='Markdown')

# ==========================================
# 6. HANDLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_msg = update.message.text
    if update.message.voice:
        audio_file = await context.bot.get_file(update.message.voice.file_id)
        path = f"v_{update.message.voice.file_id}.ogg"
        await audio_file.download_to_drive(path)
        user_msg = await transcribe_audio(path)
        os.remove(path)
        if not user_msg: return await update.message.reply_text("Não consegui ouvir bem... 🎙️")

    eventos = await asyncio.to_thread(_sync_list_events)
    decisao = await process_with_lumi_brain(user_msg, eventos)
    
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "Certinho!")
    params = decisao.get("parametros", {})

    if acao == "create":
        ev = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
        if ev: msg_lumi += f"\n\n✨ [Ver na Agenda]({ev.get('htmlLink')})"

    # Atualiza memória
    historico_conversa.append(f"Kauan: {user_msg}")
    historico_conversa.append(f"Lumi: {msg_lumi}")

    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 7. SERVIDOR E SCHEDULER
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_update))
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    scheduler.add_job(send_daily_briefing, 'cron', hour=7, minute=0, timezone='America/Sao_Paulo')
    scheduler.start()
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"status": "ok"}