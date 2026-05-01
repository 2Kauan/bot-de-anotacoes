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
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR
# ==========================================
def get_calendar_service():
    token_json = os.getenv("GOOGLE_TOKEN")
    try:
        if not token_json: 
            print("ERRO: Variável GOOGLE_TOKEN não encontrada.")
            return None
        creds_dict = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(creds_dict)
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"Erro ao carregar credenciais Google: {e}")
        return None

def _sync_list_events():
    service = get_calendar_service()
    if not service: return []
    now = datetime.datetime.utcnow().isoformat() + 'Z'
    try:
        events_result = service.events().list(
            calendarId='primary', timeMin=now, maxResults=15, 
            singleEvents=True, orderBy='startTime'
        ).execute()
        return events_result.get('items', [])
    except Exception as e:
        print(f"Erro ao listar eventos: {e}")
        return []

def _sync_create_event(titulo, data_iso):
    service = get_calendar_service()
    if not service: return None
    dt = datetime.datetime.fromisoformat(data_iso)
    event = {
        'summary': titulo,
        'start': {'dateTime': dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (dt + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

def _sync_delete_event(event_id):
    service = get_calendar_service()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        return True
    except Exception as e:
        print(f"Erro ao deletar evento {event_id}: {e}")
        return False

async def calendar_action(action, **kwargs):
    if action == "list": return await asyncio.to_thread(_sync_list_events)
    elif action == "create": return await asyncio.to_thread(_sync_create_event, kwargs.get("titulo"), kwargs.get("data"))
    elif action == "delete": return await asyncio.to_thread(_sync_delete_event, kwargs.get("id"))

# ==========================================
# 3. MOTOR DE INTELIGÊNCIA (CHAMADA REST DIRETA)
# ==========================================
async def process_intent_with_ai(prompt_text, current_events):
    # Fuso horário de Jaicós (Piauí) - UTC-3
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    limite_48h = agora + datetime.timedelta(hours=48)
    
    eventos_simplificados = [
        {"id": e['id'], "titulo": e.get('summary'), "inicio": e['start'].get('dateTime')} 
        for e in current_events
    ]
    
    prompt_completo = f"""
    Hoje é {agora.strftime("%Y-%m-%d %H:%M:%S")}.
    Agenda Atual: {json.dumps(eventos_simplificados, ensure_ascii=False)}
    
    REGRA CRÍTICA: Se o usuário pedir para apagar/cancelar, ignore qualquer ID de evento que comece DEPOIS DE {limite_48h.strftime("%Y-%m-%d %H:%M:%S")}.

    Mensagem do usuário: "{prompt_text}"
    
    Retorne OBRIGATORIAMENTE apenas um JSON puro, sem blocos de código markdown (sem ```json): 
    {{
        "acao": "create"|"read"|"delete"|"chat", 
        "resposta_amigavel": "...", 
        "parametros": {{"titulo": "...", "data_inicio": "ISO", "event_ids": []}}
    }}
    """

    url = f"[https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key=](https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key=){GEMINI_KEY}"
    
    # Payload simplificado para evitar erro 400 (Invalid JSON payload)
    payload = {
        "contents": [{"parts": [{"text": prompt_completo}]}]
    }

    try:
        response = await asyncio.to_thread(requests.post, url, json=payload, timeout=12)
        res_json = response.json()
        
        if response.status_code != 200:
            print(f"Erro API Google: {res_json}")
            raise Exception(f"Status {response.status_code}")

        texto_ia = res_json['candidates'][0]['content']['parts'][0]['text']
        
        # Limpeza robusta para garantir que pegamos apenas o JSON
        json_limpo = texto_ia.replace("```json", "").replace("
```", "").strip()
        
        return json.loads(json_limpo)
    except Exception as e:
        print(f"Erro na chamada Gemini: {e}")
        return {
            "acao": "chat", 
            "resposta_amigavel": "❌ Tive uma instabilidade técnica na minha lógica. Pode repetir o comando?", 
            "parametros": {}
        }

# ==========================================
# 4. CONTROLLER TELEGRAM
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
        
        # 1. Busca eventos atuais para dar contexto à IA
        eventos = await calendar_action("list")
        
        # 2. Processa intenção
        decisao = await process_intent_with_ai(update.message.text, eventos)
        
        acao = decisao.get("acao")
        params = decisao.get("parametros", {})
        msg_resposta = decisao.get("resposta_amigavel", "Processado com sucesso.")

        # 3. Executa ações baseadas na decisão
        if acao == "create":
            await calendar_action("create", titulo=params.get("titulo"), data=params.get("data_inicio"))
        elif acao == "delete":
            ids = params.get("event_ids", [])
            for eid in ids:
                await calendar_action("delete", id=eid)

        await update.message.reply_text(msg_resposta, parse_mode='Markdown')
        
    except Exception as e:
        print(f"Erro no fluxo principal: {e}")
        await update.message.reply_text("❌ Tive um problema interno ao processar sua mensagem.")

# ==========================================
# 5. FASTAPI & LIFESPAN
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT, handle_update))

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Inicialização do Bot ao subir o container
    await bot_app.initialize()
    await bot_app.start()
    print("🚀 Bot Online na Railway. Rota /webhook ativa.")
    yield
    # Shutdown limpo do Bot ao parar o container
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        # Processamento assíncrono do update
        asyncio.create_task(bot_app.process_update(update))
        return {"status": "ok"}
    except Exception as e:
        print(f"Erro no recebimento do Webhook: {e}")
        return {"status": "error"}

@app.get("/")
async def health_check():
    return {"status": "online", "message": "Bot de Anotações Inteligente operando."}

if __name__ == "__main__":
    import uvicorn
    # Porta dinâmica para Railway
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)