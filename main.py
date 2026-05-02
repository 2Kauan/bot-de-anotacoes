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
# 1. CONFIGURAÇÕES E MEMÓRIA DE SESSÃO
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# Memória persistente da conversa (Últimas 8 mensagens)
historico_conversa = deque(maxlen=8)
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
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
        res = service.events().list(calendarId='primary', timeMin=now, maxResults=15, singleEvents=True, orderBy='startTime').execute()
        return res.get('items', [])
    except: return []

# ==========================================
# 3. MOTOR LUMI 2.0 (COM MEMÓRIA E FOCO TEMPORAL)
# ==========================================
async def process_with_lumi_brain(user_input, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    # Contexto de Agenda e Histórico
    agenda_contexto = [{"titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} for e in current_events]
    contexto_conversa = "\n".join(list(historico_conversa))

    prompt_sistema = f"""
    Seu nome é Lumi, assistente inteligente do Kauan em Jaicós, PI.
    Hoje é {agora.strftime("%A, %d de %B de %Y")}. Hora exata: {agora.strftime("%H:%M")}.
    
    PERSONALIDADE: Feminina, expressiva, usa emojis, atenciosa e muito organizada.
    
    CONTEXTO DA CONVERSA RECENTE:
    {contexto_conversa}

    AGENDA ATUAL:
    {json.dumps(agenda_contexto, ensure_ascii=False)}

    REGRAS DE OURO:
    1. Datas Relativas: Se o Kauan disser "amanhã", considere { (agora + datetime.timedelta(days=1)).strftime("%Y-%m-%d") }.
    2. Continuidade: Recorde o que foi dito no histórico acima para entender pedidos incompletos.
    3. Conflitos: Avise se o horário escolhido bater com algo na agenda.
    
    RETORNE APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat"|"config",
        "resposta_amigavel": "...",
        "parametros": {{"titulo": "...", "data_inicio": "ISO FORMAT", "color_id": "ID"}}
    }}
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.1-70b-versatile", # Upgrade para modelo mais potente (70B)
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
        "response_format": {"type": "json_object"}
    }
    
    try:
        res = requests.post(url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=20)
        return json.loads(res.json()['choices'][0]['message']['content'])
    except Exception as e:
        print(f"Erro no cérebro da Lumi: {e}")
        return {"acao": "chat", "resposta_amigavel": "Kauan, tive um pequeno lapso de memória agora... pode repetir com mais detalhes? 🥺"}

# ==========================================
# 4. HANDLER PRINCIPAL (TRACKING DE HISTÓRICO)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    user_msg = update.message.text or "[Áudio/Arquivo]"
    
    # 1. Busca eventos e processa com IA
    eventos = await asyncio.to_thread(_sync_list_events)
    decisao = await process_with_lumi_brain(user_msg, eventos)
    
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "Certinho!")
    params = decisao.get("parametros", {})

    # 2. Execução (Calendar)
    if acao == "create":
        evento = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
        if evento: msg_lumi += f"\n\n✨ [Link da Agenda]({evento.get('htmlLink')})"

    # 3. Atualiza Memória da Lumi (Histórico)
    historico_conversa.append(f"Kauan: {user_msg}")
    historico_conversa.append(f"Lumi: {msg_lumi}")

    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 5. SERVER SETUP
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