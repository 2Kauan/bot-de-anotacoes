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
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()

# ITEM 4: Segurança (Coloque seu ID do Telegram na Railway)
# Dica: Se não souber seu ID, mande uma mensagem e veja o log "BLOQUEADO"
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    try:
        if not token_json: return None
        creds_dict = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_dict)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Erro Calendar: {e}")
        return None

def _sync_list_events():
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=10, 
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except:
        return []

def _sync_create_event(titulo, data_iso):
    service = get_calendar_service()
    if not service: return None
    dt = datetime.datetime.fromisoformat(data_iso)
    event = {
        'summary': titulo,
        'description': 'Criado via Assistente Inteligente',
        'start': {'dateTime': dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (dt + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    # Retorna o evento criado para pegarmos o link
    return service.events().insert(calendarId='primary', body=event).execute()

def _sync_delete_event(event_id):
    service = get_calendar_service()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except:
        return False

async def calendar_action(action, **kwargs):
    if action == "list": return await asyncio.to_thread(_sync_list_events)
    elif action == "create": return await asyncio.to_thread(_sync_create_event, kwargs.get("titulo"), kwargs.get("data"))
    elif action == "delete": return await asyncio.to_thread(_sync_delete_event, kwargs.get("id"))

# ==========================================
# 3. MOTOR DE IA (GROQ 3.1)
# ==========================================
async def process_intent_with_ai(prompt_text, current_events):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    eventos_simplificados = [
        {"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} 
        for e in current_events
    ]
    
    prompt_sistema = f"""
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")}. Local: Jaicós, PI.
    Agenda: {json.dumps(eventos_simplificados, ensure_ascii=False)}
    Retorne APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"chat", 
        "resposta_amigavel": "texto curto com emojis", 
        "parametros": {{"titulo": "...", "data_inicio": "ISO", "event_ids": []}}
    }}
    Use Markdown para a resposta_amigavel (ex: *texto*).
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }

    try:
        response = await asyncio.to_thread(requests.post, url, json=payload, headers=headers, timeout=10)
        res_json = response.json()
        texto_ia = res_json['choices'][0]['message']['content']
        return json.loads(texto_ia)
    except Exception as e:
        print(f"Erro IA: {e}")
        return {"acao": "chat", "resposta_amigavel": "❌ Erro ao processar intenção.", "parametros": {}}

# ==========================================
# 4. CONTROLLER (UX + SEGURANÇA)
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    
    # --- ITEM 4: WHITELIST DE SEGURANÇA ---
    user_id = update.effective_user.id
    if user_id != MEU_ID_TELEGRAM:
        print(f"⚠️ ACESSO BLOQUEADO: Usuário {user_id} tentou usar o bot.")
        # Se for a primeira vez, você pode remover o return para descobrir seu ID no log
        return 

    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        # 1. Contexto e IA
        eventos = await calendar_action("list")
        decisao = await process_intent_with_ai(update.message.text, eventos)
        
        acao = decisao.get("acao")
        params = decisao.get("parametros", {})
        msg = decisao.get("resposta_amigavel", "Ok!")

        # 2. Execução e Melhoria Visual (ITEM 1)
        if acao == "create":
            evento_criado = await calendar_action("create", titulo=params.get("titulo"), data=params.get("data_inicio"))
            if evento_criado:
                link = evento_criado.get('htmlLink')
                msg += f"\n\n🔗 [Ver no Google Calendar]({link})"
        
        elif acao == "delete":
            for eid in params.get("event_ids", []):
                await calendar_action("delete", id=eid)
            msg = f"🗑️ *Sucesso:* Compromisso removido da agenda."

        elif acao == "read":
            if not eventos:
                msg = "📅 Sua agenda está livre por enquanto!"
            else:
                msg = "📅 *Próximos Compromissos:*\n\n"
                for e in eventos:
                    inicio = e['start'].get('dateTime', e['start'].get('date'))
                    dt = datetime.datetime.fromisoformat(inicio.replace('Z', '+00:00'))
                    msg += f"• *{dt.strftime('%d/%m - %H:%M')}*: {e.get('summary')}\n"

        await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=False)
        
    except Exception as e:
        print(f"Erro Controller: {e}")
        await update.message.reply_text("❌ Tive um problema ao sincronizar.")

# ==========================================
# 5. FASTAPI & LIFESPAN
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    print(f"🚀 Bot Protegido Online. Whitelist: {MEU_ID_TELEGRAM}")
    yield
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    asyncio.create_task(bot_app.process_update(update))
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)