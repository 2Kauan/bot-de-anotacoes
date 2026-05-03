import os, json, arrow, requests
import google.generativeai as genai
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

# Configurações
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))
TZ = "America/Sao_Paulo"

MEMORY = {"last_event": None}
genai.configure(api_key=GEMINI_KEY)

# ---------------- CALENDAR ----------------
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

# ---------------- AI (GEMINI FLASH) ----------------
async def ask_lumi(user_input, context_list):
    agora = arrow.now(TZ)
    ctx = "\n".join([f"{i} - {arrow.get(e['start'].get('dateTime')).format('DD/MM HH:mm')} - {e.get('summary')}" for i, e in enumerate(context_list)])

    prompt = f"""
    Seu nome é Lumi, assistente do Kauan.
    Agora: {agora.format('DD/MM HH:mm')} ({agora.format('dddd')})
    
    Agenda Atual:
    {ctx}

    REGRAS:
    - Retorne APENAS JSON.
    - Se for conversa normal, use acao "chat".
    - JSON: {{"acao":"create|update|delete|read|chat", "titulo":"...", "data":"ISO", "index":0, "msg":"..."}}
    """
    try:
        # ALTERADO: Usando o identificador estável 'gemini-1.5-flash'
        model = genai.GenerativeModel('gemini-1.5-flash')
        
        # Configuração de resposta JSON movida para a geração para evitar erros de versão
        response = model.generate_content(
            [prompt, user_input],
            generation_config=genai.GenerationConfig(response_mime_type="application/json")
        )
        return response.text
    except Exception as e:
        print(f"ERRO TÉCNICO NO GEMINI: {str(e)}")
        return json.dumps({"acao": "chat", "msg": "Tive um problema na conexão. Pode repetir?"})

# ---------------- TELEGRAM ----------------
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    
    text = update.message.text
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    intent = classify_intent(text)
    eventos = list_evs()

    if intent == "read":
        if not eventos:
            await update.message.reply_text("Sua agenda está vazia!")
            return
        linhas = [f"📅 {arrow.get(e['start'].get('dateTime')).to(TZ).format('DD/MM HH:mm')} - {e.get('summary')}" for e in eventos]
        await update.message.reply_text("📋 *Sua Agenda:*\n\n" + "\n".join(linhas), parse_mode='Markdown')
        return

    raw = await ask_lumi(text, eventos)
    try:
        data = json.loads(raw)
        acao = data.get("acao")
        msg = data.get("msg", "Como posso ajudar?")

        if acao == "create":
            ev = create_ev(data["titulo"], data["data"])
            msg = f"✅ *{ev['summary']}* criado!\n🔗 [Ver no Google]({ev['htmlLink']})" if ev else "Erro ao criar ❌"

        elif acao == "update":
            idx = data.get("index")
            eid = eventos[idx]["id"] if (idx is not None and idx < len(eventos)) else (MEMORY["last_event"]["id"] if MEMORY["last_event"] else None)
            res = update_ev(eid, data["data"])
            msg = f"🕒 Horário atualizado!\n🔗 [Conferir]({res['htmlLink']})" if res else "Não achei o evento. 🤔"

        elif acao == "delete":
            idx = data.get("index")
            if idx is not None and idx < len(eventos):
                msg = "🗑️ Removido!\n🔗 [Ver Agenda](https://calendar.google.com/)" if delete_ev(eventos[idx]["id"]) else "Erro ❌"
            else: msg = "Qual evento devo apagar? 🤔"

        await update.message.reply_text(msg, parse_mode='Markdown', disable_web_page_preview=False)
    except Exception as e:
        await update.message.reply_text(f"Me confundi um pouco: {str(e)}")

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