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
# Certifique-se de que a variável na Render agora é a da Groq
API_KEY = os.getenv("GROQ_API_KEY") 
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))
TZ = "America/Sao_Paulo"

MEMORY = {"last_event": None}

# ---------------- CALENDAR ENGINE ----------------
def get_calendar():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def create_ev(titulo, data_iso):
    service = get_calendar()
    if not service: return None
    try:
        start = arrow.get(data_iso).to(TZ)
        event = {
            'summary': titulo,
            'start': {'dateTime': start.isoformat(), 'timeZone': TZ},
            'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': TZ},
        }
        res = service.events().insert(calendarId='primary', body=event).execute()
        MEMORY["last_event"] = res
        return res
    except: return None

def update_ev(eid, nova_data):
    service = get_calendar()
    if not service or not eid: return None
    try:
        event = service.events().get(calendarId='primary', eventId=eid).execute()
        start = arrow.get(nova_data).to(TZ)
        event['start'] = {'dateTime': start.isoformat(), 'timeZone': TZ}
        event['end'] = {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': TZ}
        res = service.events().update(calendarId='primary', eventId=eid, body=event).execute()
        MEMORY["last_event"] = res
        return res
    except: return None

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

# ---------------- INTENT ----------------
def classify_intent(text):
    t = text.lower()
    if any(p in t for p in ["tem", "existe", "tenho", "lista", "agenda", "mostrar"]): return "read"
    if any(p in t for p in ["apaga", "remove", "deleta", "excluir"]): return "delete"
    if any(p in t for p in ["muda", "altera", "coloca", "remarca", "ajusta"]): return "update"
    if any(p in t for p in ["cria", "lembrete", "marcar", "agendar", "novo"]): return "create"
    return "unknown"

# ---------------- AI ENGINE (GROQ) ----------------
async def ask_lumi(user_input, context_list):
    agora = arrow.now(TZ)
    ctx = "\n".join([f"{i} - {arrow.get(e['start'].get('dateTime')).format('DD/MM HH:mm')} - {e.get('summary')}" for i, e in enumerate(context_list)])

    system = f"""
    Nome: Lumi. Papel: Assistente de Agenda.
    Agora: {agora.format('DD/MM HH:mm')} ({agora.format('dddd')}).
    Agenda: {ctx}
    REGRAS: Retorne APENAS JSON.
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
        
        # Validação da resposta para evitar o erro 'choices'
        json_res = res.json()
        if 'choices' in json_res:
            return json_res['choices'][0]['message']['content']
        else:
            print(f"RESPOSTA ESTRANHA DA API: {json_res}")
            return json.dumps({"acao": "chat", "msg": "A API respondeu de um jeito inesperado."})
            
    except Exception as e:
        print(f"ERRO API: {e}")
        return json.dumps({"acao": "chat", "msg": "Erro de conexão com a API."})

# ---------------- TELEGRAM ----------------
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    intent = classify_intent(update.message.text)
    eventos = list_evs()

    if intent == "read":
        if not eventos:
            await update.message.reply_text("Sua agenda está vazia!")
            return
        linhas = [f"📅 {arrow.get(e['start'].get('dateTime')).to(TZ).format('DD/MM HH:mm')} - {e.get('summary')}" for e in eventos]
        await update.message.reply_text("📋 *Sua Agenda:*\n\n" + "\n".join(linhas), parse_mode='Markdown')
        return

    raw = await ask_lumi(update.message.text, eventos)
    try:
        data = json.loads(raw)
        acao = data.get("acao")
        msg = data.get("msg", "Como posso ajudar?")

        if acao == "create":
            ev = create_ev(data["titulo"], data["data"])
            msg = f"✅ *{ev['summary']}* criado!\n🔗 [Ver no Google]({ev['htmlLink']})" if ev else "Erro ao criar ❌"

        elif acao == "update":
            idx = data.get("index")
            # Se a IA não der o index, tenta usar a memória do último evento criado
            eid = eventos[idx]["id"] if (idx is not None and idx < len(eventos)) else (MEMORY["last_event"]["id"] if MEMORY["last_event"] else None)
            res = update_ev(eid, data["data"])
            msg = f"🕒 Horário atualizado!\n🔗 [Conferir]({res['htmlLink']})" if res else "Não achei o evento 🤔"

        elif acao == "delete":
            idx = data.get("index")
            if idx is not None and idx < len(eventos):
                msg = "🗑️ Removido!\n🔗 [Abrir Agenda](https://calendar.google.com/)" if delete_ev(eventos[idx]["id"]) else "Erro ❌"
            else: msg = "Qual evento devo apagar? 🤔"

        await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=False)
    except Exception as e:
        await update.message.reply_text(f"Erro no processamento: {str(e)}")

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