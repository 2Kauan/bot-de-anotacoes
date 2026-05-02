import os, json, arrow, requests
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

TZ = "America/Sao_Paulo"

# Memória simples (último evento usado)
MEMORY = {"last_event_id": None}

# --- CALENDAR ---
def get_calendar():
    try:
        # Carrega o token das variáveis de ambiente da Render
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except:
        return None

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
        MEMORY["last_event_id"] = res["id"]
        return res
    except: return None

def update_ev(eid, nova_data):
    service = get_calendar()
    if not service or not eid: return False
    try:
        event = service.events().get(calendarId='primary', eventId=eid).execute()
        start = arrow.get(nova_data).to(TZ)
        event['start'] = {'dateTime': start.isoformat(), 'timeZone': TZ}
        event['end'] = {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': TZ}
        service.events().update(calendarId='primary', eventId=eid, body=event).execute()
        return True
    except: return False

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
        events = service.events().list(
            calendarId='primary',
            timeMin=now.floor('day').isoformat(),
            timeMax=now.shift(days=7).ceil('day').isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute().get('items', [])
        return events
    except: return []

# --- IA ---
async def ask_lumi(user_input, context_list):
    agora = arrow.now(TZ)
    ctx_text = "\n".join([
        f"{i} - {arrow.get(e['start'].get('dateTime')).format('DD/MM HH:mm')} - {e.get('summary')}"
        for i, e in enumerate(context_list)
    ])

    system = f"""
Você é a Lumi, assistente de agenda do Kauan.
Agora: {agora.format('DD/MM HH:mm')} ({agora.format('dddd')})

Agenda Atual:
{ctx_text}

REGRAS:
- Retorne APENAS JSON.
- Para update/delete, use o "index" da lista acima.
- Se o usuário quiser alterar algo que acabou de ser citado ou criado, e você não tiver index, envie "index": null.
"""

    try:
        res = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_input}
                ],
                "response_format": {"type": "json_object"}
            },
            timeout=15
        )
        return res.json()['choices'][0]['message']['content']
    except:
        return json.dumps({"acao": "chat", "msg": "Erro na IA 😕"})

# --- TELEGRAM ---
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')

    eventos = list_evs()
    raw = await ask_lumi(update.message.text, eventos)

    try:
        data = json.loads(raw)
        acao = data.get("acao")

        if acao == "create":
            ev = create_ev(data["titulo"], data["data"])
            msg = f"✅ *{data['titulo']}* criado! ✨" if ev else "Erro ao criar no Google ❌"

        elif acao == "update":
            idx = data.get("index")
            # Lógica de memória: se index for nulo ou inválido, usa o último ID criado
            if idx is None or not (0 <= idx < len(eventos)):
                eid = MEMORY["last_event_id"]
            else:
                eid = eventos[idx]["id"]
            
            if eid:
                ok = update_ev(eid, data["data"])
                msg = f"🕒 Horário atualizado para {arrow.get(data['data']).format('HH:mm')}! ✨" if ok else "Erro ao atualizar no Google ❌"
            else:
                msg = "Não encontrei qual evento você quer alterar. 🧐"

        elif acao == "delete":
            idx = data.get("index")
            if idx is not None and (0 <= idx < len(eventos)):
                eid = eventos[idx]["id"]
                ok = delete_ev(eid)
                msg = "🗑️ Evento removido com sucesso! ✨" if ok else "Erro ao deletar ❌"
            else:
                msg = "Não achei esse evento na lista para apagar. 🧐"

        elif acao == "read":
            if not eventos:
                msg = "Sua agenda está vazia nos próximos dias! 🌸"
            else:
                linhas = ["📋 *Sua Agenda:*"]
                for e in eventos:
                    dt = arrow.get(e['start'].get('dateTime')).to(TZ).format('DD/MM HH:mm')
                    linhas.append(f"📅 {dt} - {e.get('summary')}")
                msg = "\n".join(linhas)

        else:
            msg = data.get("msg", "Como posso ajudar? 🌸")

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        await update.message.reply_text(f"Ocorreu um erro: {str(e)}")

# --- INFRA ---
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize()
    await bot_app.start()
    yield
    await bot_app.stop()

app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"status": "Lumi online e operante! 🌸"}

# Rota de segurança para capturar Webhooks com caracteres extras
@app.post("/webhook{full_path:path}")
async def webhook(request: Request, full_path: str = ""):
    try:
        data = await request.json()
        await bot_app.process_update(Update.de_json(data, bot_app.bot))
        return {"ok": True}
    except:
        return {"ok": False}