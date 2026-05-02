import os
import json
import arrow
import requests
from typing import Optional, List, Literal
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field, ValidationError
from loguru import logger
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# ==========================================
# 1. MODELAGEM DE DADOS (PYDANTIC)
# ==========================================
class LumiAction(BaseModel):
    """Esquema rigoroso para a tomada de decisão da IA"""
    acao: Literal["create", "read", "delete", "chat"]
    resposta_amigavel: str = Field(..., description="Fala da Lumi com emojis e calor humano")
    titulo: Optional[str] = None
    data_inicio: Optional[str] = None
    event_id: Optional[str] = None
    color_id: Optional[str] = "1"

# ==========================================
# 2. CONFIGURAÇÕES & ESTADO
# ==========================================
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))
CATEGORIAS = {"Estudo": "1", "Academia": "10", "Trabalho": "7", "Lazer": "5"}

# ==========================================
# 3. CORE DE CALENDÁRIO (ARROW + GOOGLE)
# ==========================================
def get_calendar():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        logger.error(f"Falha na conexão Google: {e}")
        return None

def manage_event(action: LumiAction):
    service = get_calendar()
    if not service: return False

    try:
        if action.acao == "create":
            # Arrow garante precisão cirúrgica no fuso de Jaicós
            start = arrow.get(action.data_inicio).replace(tzinfo='America/Sao_Paulo')
            event = {
                'summary': action.titulo,
                'colorId': action.color_id if action.color_id in [str(i) for i in range(1,12)] else "1",
                'start': {'dateTime': start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
                'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': 'America/Sao_Paulo'},
            }
            return service.events().insert(calendarId='primary', body=event).execute()

        if action.acao == "delete" and action.event_id:
            service.events().delete(calendarId='primary', eventId=action.event_id).execute()
            return True
            
        return None
    except Exception as e:
        logger.error(f"Erro na execução da agenda: {e}")
        return None

# ==========================================
# 4. INTELIGÊNCIA ARTIFICIAL (GROQ)
# ==========================================
async def ask_lumi(prompt_user: str, context_events: list) -> LumiAction:
    agora = arrow.now('America/Sao_Paulo')
    
    # Contexto rico para evitar que ela "burreie"
    agenda_view = [{"id": e['id'], "summary": e.get('summary'), "start": e['start'].get('dateTime')} for e in context_events]

    system_prompt = f"""
    Seu nome é Lumi, assistente elite do Kauan em Jaicós, PI.
    Hoje: {agora.format('dddd, DD/MM/YYYY HH:mm')}.

    DIRETRIZES:
    1. Se ele pedir para apagar/remover, identifique o ID na lista abaixo.
    2. Se pedir para agendar, use ISO8601 com fuso -03:00.
    3. Seja feminina, expressiva e use emojis.
    
    CATEGORIAS: {json.dumps(CATEGORIAS)}
    AGENDA RECENTE: {json.dumps(agenda_view)}

    Responda EXCLUSIVAMENTE em JSON seguindo o esquema Pydantic.
    """

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt_user}],
                "response_format": {"type": "json_object"}
            },
            timeout=15
        )
        # Validação via Pydantic
        return LumiAction.model_validate_json(res.json()['choices'][0]['message']['content'])
    except Exception as e:
        logger.error(f"Erro Groq/Pydantic: {e}")
        return LumiAction(acao="chat", resposta_amigavel="Kauan, tive um pequeno soluço digital... pode repetir? 🌸")

# ==========================================
# 5. HANDLER TELEGRAM (FLUXO PRINCIPAL)
# ==========================================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return

    logger.info(f"Mensagem recebida: {update.message.text}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    # Busca contexto preventivo
    service = get_calendar()
    hoje = arrow.now('America/Sao_Paulo').floor('day')
    eventos_hoje = service.events().list(calendarId='primary', timeMin=hoje.isoformat()).execute().get('items', []) if service else []

    # Processa IA
    decisao = await ask_lumi(update.message.text, eventos_hoje)
    
    # Execução
    resultado = manage_event(decisao)
    
    final_msg = decisao.resposta_amigavel
    if decisao.acao == "create" and resultado:
        final_msg += f"\n\n✨ [Evento fixado na agenda!]({resultado.get('htmlLink')})"
    elif decisao.acao == "read":
        if not eventos_hoje:
            final_msg += "\n\nNada agendado por enquanto! 🌸"
        else:
            lista = "\n".join([f"• *{arrow.get(e['start'].get('dateTime')).format('HH:mm')}* - {e.get('summary')}" for e in eventos_hoje])
            final_msg += f"\n\n{lista}"

    await update.message.reply_text(final_msg, parse_mode='Markdown', disable_web_page_preview=True)

# ==========================================
# 6. INFRAESTRUTURA (LIFESPAN & WEBHOOK)
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    logger.info("Lumi Inicializada com sucesso!")
    yield
    await bot_app.stop()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"ok": True}