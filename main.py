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
ID_PARA_NOME = {v: k for k, v in MINHAS_CATEGORIAS.items()}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR (PRECISÃO TOTAL)
# ==========================================
def get_calendar_service():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def _sync_list_events(time_min, time_max):
    """Busca eventos em um intervalo específico com precisão de fuso."""
    service = get_calendar_service()
    if not service: return []
    try:
        # A API espera o formato RFC3339 (Z para UTC)
        res = service.events().list(
            calendarId='primary', 
            timeMin=time_min, 
            timeMax=time_max,
            singleEvents=True, 
            orderBy='startTime'
        ).execute()
        return res.get('items', [])
    except Exception as e:
        print(f"Erro ao listar: {e}")
        return []

# ==========================================
# 3. MOTOR LUMI (CÉREBRO ATUALIZADO)
# ==========================================
async def process_with_lumi_brain(user_input):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    prompt_sistema = f"""
    Seu nome é Lumi. Você é a assistente do Kauan em Jaicós, PI.
    Hoje é {agora.strftime("%A, %d de %B de %Y")}. Hora atual: {agora.strftime("%H:%M")}.
    
    Se o usuário perguntar sobre a agenda (hoje, amanhã, semana), use acao='read'.
    IMPORTANTE: Calcule as datas ISO corretamente baseadas em hoje ({agora.date()}).
    
    Exemplo para AMANHÃ: 
    time_min: { (agora + datetime.timedelta(days=1)).replace(hour=0,minute=0,second=0).isoformat() }Z
    time_max: { (agora + datetime.timedelta(days=1)).replace(hour=23,minute=59,second=59).isoformat() }Z

    Retorne JSON:
    {{
        "acao": "create"|"read"|"chat",
        "resposta_amigavel": "Sua fala expressiva...",
        "parametros": {{"time_min": "ISO", "time_max": "ISO", "titulo": "...", "data_inicio": "ISO", "color_id": "ID"}}
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
    except: return {"acao": "chat", "resposta_amigavel": "Ops, tive um tropeço aqui... pode repetir? 🥺"}

# ==========================================
# 4. HANDLER DE MENSAGENS (AÇÃO DE BUSCA)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    decisao = await process_with_lumi_brain(update.message.text or "")
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "")
    params = decisao.get("parametros", {})

    if acao == "read":
        # Se a IA não mandou as datas, usamos 'hoje' como padrão
        fuso = datetime.timezone(datetime.timedelta(hours=-3))
        t_min = params.get("time_min") or datetime.datetime.now(fuso).replace(hour=0,minute=0,second=0).isoformat() + 'Z'
        t_max = params.get("time_max") or datetime.datetime.now(fuso).replace(hour=23,minute=59,second=59).isoformat() + 'Z'
        
        eventos = await asyncio.to_thread(_sync_list_events, t_min, t_max)
        
        if not eventos:
            msg_lumi += "\n\nOlhei aqui com cuidado e não encontrei nada para esse período! ✨ Quer marcar algo?"
        else:
            msg_lumi += "\n\n"
            for e in eventos:
                inicio = e['start'].get('dateTime', e['start'].get('date'))
                # Converte para o horário local de Jaicós para exibir certo
                dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00')).astimezone(fuso)
                cat = ID_PARA_NOME.get(e.get('colorId'), "Geral")
                
                msg_lumi += f"⏰ *{dt.strftime('%H:%M')}* - {e.get('summary')}\n"
                msg_lumi += f"📌 Categoria: _{cat}_\n\n"

    elif acao == "create":
        # Lógica de criação (omitida aqui por brevidade, mas mantida no seu sistema)
        pass

    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 5. SERVER (RAILWAY)
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT, handle_update))

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