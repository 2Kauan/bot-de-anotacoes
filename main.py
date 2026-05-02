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

# ==========================================
# 1. CONFIGURAÇÕES
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))
MINHAS_CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR (FUSO BLINDADO)
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
    
    # Validação de cor (evitar erro 400)
    valid_ids = [str(i) for i in range(1, 12)]
    final_color = color_id if str(color_id) in valid_ids else "1"

    try:
        # Forçamos a interpretação da string ISO garantindo o fuso de Jaicós (-03:00)
        dt_start = datetime.datetime.fromisoformat(data_iso.replace('Z', '-03:00'))
        dt_end = dt_start + datetime.timedelta(hours=1)

        event = {
            'summary': titulo,
            'colorId': final_color,
            'start': {'dateTime': dt_start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': dt_end.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except Exception as e:
        print(f"Erro ao processar data/hora: {e}")
        return None

# ==========================================
# 3. MOTOR LUMI (INTELIGÊNCIA TEMPORAL)
# ==========================================
async def process_with_lumi_brain(user_input):
    # Fuso exato de Jaicós, Piauí
    fuso_piaui = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso_piaui)
    
    prompt_sistema = f"""
    Nome: Lumi. Papel: Assistente pessoal do Kauan em Jaicós, PI.
    CONTEXTO TEMPORAL CRÍTICO:
    - Agora são precisamente: {agora.strftime("%H:%M")} do dia {agora.strftime("%d/%m/%Y")}.
    - O fuso horário local é UTC-03:00.
    
    INSTRUÇÃO DE DATA:
    - Se o Kauan pedir "às 10", gere: {agora.strftime("%Y-%m-%d")}T10:00:00-03:00.
    - Sempre use o formato ISO8601 com o sufixo -03:00.
    
    Categorias: {json.dumps(MINHAS_CATEGORIAS)}
    
    Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"chat",
        "resposta_amigavel": "...",
        "parametros": {{"titulo": "...", "data_inicio": "ISO_COM_FUSO", "color_id": "ID"}}
    }}
    """
    
    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": user_input}],
                "response_format": {"type": "json_object"}
            },
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            timeout=15
        )
        data = res.json()
        if 'choices' not in data:
            return {"acao": "chat", "resposta_amigavel": "Kauan, a Groq demorou a me responder... 🕒 Tenta de novo em 5 segundos? ✨"}
        
        return json.loads(data['choices'][0]['message']['content'])
    except:
        return {"acao": "chat", "resposta_amigavel": "Tive um tropeço técnico com o horário, Kauan. Pode repetir? 🌸"}

# ==========================================
# 4. HANDLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    decisao = await process_with_lumi_brain(update.message.text or "")
    acao = decisao.get("acao")
    msg_lumi = decisao.get("resposta_amigavel", "")
    params = decisao.get("parametros", {})

    if acao == "create":
        # Tentamos criar o evento com a data garantida
        ev = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), params.get("color_id"))
        if ev:
            # Pegamos o horário de início real do evento criado para confirmar
            inicio_confirmado = ev['start'].get('dateTime')
            dt_confirmada = datetime.datetime.fromisoformat(inicio_confirmado.replace('Z', '-03:00'))
            msg_lumi += f"\n\n✅ *Confirmado para às {dt_confirmada.strftime('%H:%M')}!*\n✨ [Ver na Agenda]({ev.get('htmlLink')})"
        else:
            msg_lumi = "Kauan, tive um problema ao sincronizar o horário com o Google. Pode conferir se a data está certinha? 😕"

    await update.message.reply_text(msg_lumi, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 5. SERVER
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