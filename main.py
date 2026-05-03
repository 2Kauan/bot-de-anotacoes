import os, json, arrow, requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# --- CONFIGURAÇÕES ---
API_KEY = os.getenv("GROQ_API_KEY") 
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))
TZ = "America/Sao_Paulo"

MEMORY = {"last_event": None}

# ---------------- CALENDAR ENGINE ----------------
def get_calendar():
    try:
        token_data = os.getenv("GOOGLE_TOKEN")
        if not token_data: return None
        creds = Credentials.from_authorized_user_info(json.loads(token_data))
        return build('calendar', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ ERRO AUTENTICAÇÃO: {e}")
        return None

def create_ev(titulo, data_iso):
    service = get_calendar()
    if not service: return None
    try:
        # AJUSTE DE FUSO HORÁRIO:
        # Interpretamos a data da IA como 'Local' e garantimos o ano de 2026
        # O replace(tzinfo=TZ) evita que o sistema subtraia 3 horas indevidamente
        start = arrow.get(data_iso).replace(year=2026, tzinfo=TZ)
        
        event = {
            'summary': titulo,
            'start': {'dateTime': start.isoformat(), 'timeZone': TZ},
            'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': TZ},
        }
        res = service.events().insert(calendarId='primary', body=event).execute()
        MEMORY["last_event"] = res
        return res
    except Exception as e:
        print(f"❌ ERRO CREATE: {e}")
        return None

def update_ev(eid, nova_data):
    service = get_calendar()
    if not service or not eid: return None
    try:
        event = service.events().get(calendarId='primary', eventId=eid).execute()
        # Aplica a mesma lógica de fuso horário no update
        start = arrow.get(nova_data).replace(year=2026, tzinfo=TZ)
        
        event['start'] = {'dateTime': start.isoformat(), 'timeZone': TZ}
        event['end'] = {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': TZ}
        res = service.events().update(calendarId='primary', eventId=eid, body=event).execute()
        MEMORY["last_event"] = res
        return res
    except Exception as e:
        print(f"❌ ERRO UPDATE: {e}")
        return None

def delete_ev(eid):
    service = get_calendar()
    if not service or not eid: return False
    try:
        service.events().delete(calendarId='primary', eventId=eid).execute()
        return True
    except: return False

def list_evs():
    service = get_calendar()
    if not service: return []
    try:
        now = arrow.now(TZ)
        return service.events().list(
            calendarId='primary',
            timeMin=now.floor('day').isoformat(),
            timeMax=now.shift(days=7).ceil('day').isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute().get('items', [])
    except: return []

# ---------------- AI ENGINE ----------------
async def ask_lumi(user_input, context_list):
    agora = arrow.now(TZ)
    ctx = "\n".join([f"{i} - {arrow.get(e['start'].get('dateTime')).to(TZ).format('DD/MM HH:mm')} - {e.get('summary')}" for i, e in enumerate(context_list)])

    system = f"""
    Nome: Lumi. Assistente do Kauan.
    Data de Hoje: {agora.format('DD/MM/YYYY')} ({agora.format('dddd')}). ANO ATUAL: 2026.
    Agenda Atual:
    {ctx}
    REGRAS: Retorne JSON. Sempre use o ano de 2026.
    JSON: {{"acao":"create|update|delete|read|chat", "titulo":"...", "data":"ISO", "index":0, "msg":"..."}}
    """
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_input}],
                "response_format": {"type": "json_object"}
            }, timeout=12)
        return res.json()['choices'][0]['message']['content']
    except:
        return json.dumps({"acao": "chat", "msg": "Erro de conexão com a IA."})

# ---------------- TELEGRAM ----------------
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    eventos = list_evs()

    raw = await ask_lumi(update.message.text, eventos)
    try:
        data = json.loads(raw)
        acao = data.get("acao")
        msg = data.get("msg", "")

        if acao == "create":
            ev = create_ev(data["titulo"], data["data"])
            msg = f"✅ *{ev['summary']}* criado!\n🔗 [Ver no Google]({ev['htmlLink']})" if ev else "Erro ao criar no Google."

        elif acao == "update":
            idx = data.get("index")
            eid = eventos[idx]["id"] if (idx is not None and idx < len(eventos)) else (MEMORY["last_event"]["id"] if MEMORY["last_event"] else None)
            res = update_ev(eid, data["data"])
            msg = f"🕒 Horário atualizado!\n🔗 [Conferir]({res['htmlLink']})" if res else "Não achei o evento."

        elif acao == "delete":
            idx = data.get("index")
            eid = eventos[idx]["id"] if (idx is not None and idx < len(eventos)) else (MEMORY["last_event"]["id"] if MEMORY["last_event"] else None)
            if eid and delete_ev(eid):
                msg = "🗑️ Evento removido com sucesso!"
                MEMORY["last_event"] = None
            else: msg = "Não consegui apagar o evento."

        elif acao == "read" or not msg:
            if not eventos: msg = "Sua agenda está vazia!"
            else:
                linhas = [f"📅 {arrow.get(e['start'].get('dateTime')).to(TZ).format('DD/MM HH:mm')} - {e.get('summary')}" for e in eventos]
                msg = "📋 *Sua Agenda:*\n\n" + "\n".join(linhas)

        await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=False)
    except Exception as e:
        await update.message.reply_text(f"Erro: {str(e)}")

# ---------------- INFRA ----------------
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize(); await bot_app.start(); yield; await bot_app.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root(): return {"status": "Lumi Online"}

@app.post("/webhook{full_path:path}")
async def webhook(request: Request, full_path: str = ""):
    try:
        data = await request.json()
        await bot_app.process_update(Update.de_json(data, bot_app.bot))
        return {"ok": True}
    except: return {"ok": False}