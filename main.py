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
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}

# ==========================================
# 2. FUNÇÕES DE SUPORTE (CALENDAR & VOZ)
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    try:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def _sync_list_events(max_results=10, time_min=None):
    service = get_calendar_service()
    if not service: return []
    if not time_min:
        time_min = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        res = service.events().list(calendarId='primary', timeMin=time_min, maxResults=max_results, singleEvents=True, orderBy='startTime').execute()
        return res.get('items', [])
    except: return []

# ==========================================
# 3. TAREFAS PROATIVAS (A ALMA DA LUMI)
# ==========================================
async def send_daily_briefing():
    """Lumi envia o resumo matinal às 07:00"""
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    hoje = datetime.datetime.now(fuso).replace(hour=0, minute=0, second=0).isoformat() + 'Z'
    eventos = _sync_list_events(max_results=20, time_min=hoje)
    
    if not eventos:
        texto = "Bom dia, Kauan! ✨ Sua agenda está livre para hoje. Que tal aproveitar para descansar ou planejar algo novo? 🥰"
    else:
        lista = ""
        for e in eventos:
            inicio = e['start'].get('dateTime', e['start'].get('date'))
            dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00')).astimezone(fuso)
            if dt.date() == datetime.datetime.now(fuso).date():
                lista += f"• *{dt.strftime('%H:%M')}*: {e.get('summary')}\n"
        
        texto = f"Bom dia, Kauan! ☀️\n\nAqui está sua agenda de hoje em Jaicós:\n\n{lista}\nEstou aqui se precisar de algo! 🌸"

    await bot_app.bot.send_message(chat_id=MEU_ID_TELEGRAM, text=texto, parse_mode='Markdown')

async def check_upcoming_reminders():
    """Lumi avisa 15 minutos antes de um compromisso"""
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    eventos = _sync_list_events(max_results=5)

    for e in eventos:
        inicio_str = e['start'].get('dateTime')
        if not inicio_str: continue
        
        inicio_dt = datetime.datetime.fromisoformat(inicio_str.replace('Z', '+00:00')).astimezone(fuso)
        diferenca = (inicio_dt - agora).total_seconds() / 60

        # Se faltar entre 10 e 15 minutos, ela avisa
        if 10 <= diferenca <= 15:
            aviso = f"Kauan, passando para lembrar que seu compromisso *'{e.get('summary')}'* começa em breve (às {inicio_dt.strftime('%H:%M')})! ✨ Não se atrase, hein? ☺️"
            # Evitar avisos duplicados (simplificado: você pode melhorar usando um cache/ID)
            await bot_app.bot.send_message(chat_id=MEU_ID_TELEGRAM, text=aviso, parse_mode='Markdown')

# ==========================================
# 4. MOTOR LUMI (IA EXPRESSIVA)
# ==========================================
async def process_intent_with_lumi(prompt_text, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    agenda_resumo = [{"titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]

    prompt_sistema = f"""
    Nome: Lumi. Papel: Assistente dedicada do Kauan.
    Tom: Expressivo, emojis, carinhoso e inteligente. 
    Contexto: Jaicós, PI. Hoje é {agora.strftime('%d/%m/%Y às %H:%M')}.
    Agenda: {json.dumps(agenda_resumo, ensure_ascii=False)}
    Retorne APENAS JSON com acao, resposta_amigavel e parametros.
    """
    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }
    res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=15)
    return json.loads(res.json()['choices'][0]['message']['content'])

# ==========================================
# 5. HANDLER TELEGRAM
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    eventos = await asyncio.to_thread(_sync_list_events)
    decisao = await process_intent_with_lumi(update.message.text or "Áudio recebido", eventos)
    
    # ... (Lógica de criar/deletar/responder idêntica à anterior)
    await update.message.reply_text(decisao['resposta_amigavel'], parse_mode='Markdown')

# ==========================================
# 6. LIFESPAN & SCHEDULER (FASTAPI)
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_update))
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicia o Bot
    await bot_app.initialize()
    await bot_app.start()
    
    # Agenda as Tarefas
    scheduler.add_job(send_daily_briefing, 'cron', hour=7, minute=0, timezone='America/Sao_Paulo')
    scheduler.add_job(check_upcoming_reminders, 'interval', minutes=10)
    scheduler.start()
    
    print("🚀 Lumi está acordada e monitorando sua agenda!")
    yield
    scheduler.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"status": "ok"}