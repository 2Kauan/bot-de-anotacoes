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
# 1. CONFIGURAÇÕES E PERSONALIDADE LUMI
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# Memória de Categorias (Evolução conversacional)
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7"}

# ==========================================
# 2. MOTOR DE VOZ (GROQ WHISPER)
# ==========================================
async def transcribe_audio(file_path):
    url = "https://api.groq.com/openai/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}"}
    try:
        with open(file_path, "rb") as f:
            files = {
                "file": ("audio.ogg", f),
                "model": (None, "whisper-large-v3"),
                "response_format": (None, "json")
            }
            response = requests.post(url, headers=headers, files=files, timeout=15)
        return response.json().get("text", "")
    except Exception as e:
        print(f"Erro Whisper: {e}")
        return ""

# ==========================================
# 3. MOTOR DE IA (LUMI - PERSONALIDADE E LÓGICA)
# ==========================================
async def process_intent_with_lumi(prompt_text, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    # Contexto para análise de conflitos e buracos na agenda
    agenda_str = json.dumps([
        {"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} 
        for e in current_events
    ], ensure_ascii=False)

    prompt_sistema = f"""
    Você é a Lumi, uma assistente pessoal inteligente, eficiente e feminina. 
    Seu tom é profissional, gentil e direto, sem ser extravagante. 
    Você vive e trabalha considerando o contexto de Jaicós, PI.
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")}.

    Agenda Atual: {agenda_str}
    Categorias: {json.dumps(MINHAS_CATEGORIAS, ensure_ascii=False)}

    Sua missão:
    1. Se houver conflito de horário (mesmo horário de outro evento), avise gentilmente.
    2. Se o usuário perguntar por horários livres, analise os buracos na agenda.
    3. Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"config"|"chat", 
        "resposta_amigavel": "Sua resposta como Lumi aqui...", 
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
        res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=12)
        return json.loads(res.json()['choices'][0]['message']['content'])
    except:
        return {"acao": "chat", "resposta_amigavel": "Desculpe, tive um tropeço técnico. Pode repetir?"}

# ==========================================
# 4. TAREFAS AGENDADAS (NOTIFICAÇÕES)
# ==========================================
async def send_daily_briefing():
    # Lógica simplificada: Busca eventos de hoje e envia para o Telegram
    print("Executando Resumo Matinal da Lumi...")
    # Aqui entraria a chamada ao Calendar e bot.send_message

# ==========================================
# 5. CONTROLLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_input = update.message.text
    
    # Suporte a Áudio (Whisper)
    if update.message.voice:
        audio_file = await context.bot.get_file(update.message.voice.file_id)
        temp_path = f"audio_{update.message.voice.file_id}.ogg"
        await audio_file.download_to_drive(temp_path)
        user_input = await transcribe_audio(temp_path)
        os.remove(temp_path)
        if not user_input:
            return await update.message.reply_text("Não consegui ouvir bem o áudio, poderia repetir?")

    # Busca eventos para contexto
    # (Função get_calendar_service e calendar_action devem estar presentes conforme versões anteriores)
    eventos = [] # Aqui chamaria calendar_action("list")
    
    decisao = await process_intent_with_lumi(user_input, eventos)
    
    # Lógica de Execução (Create/Delete/Config) idêntica à versão anterior
    # Lumi agora responde com sua personalidade configurada no Prompt.
    await update.message.reply_text(decisao['resposta_amigavel'], parse_mode='Markdown')

# ==========================================
# 6. LIFESPAN COM SCHEDULER
# ==========================================
scheduler = AsyncIOScheduler()
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT | filters.VOICE, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    
    # Agenda o Resumo Matinal para as 07:00 de Jaicós
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