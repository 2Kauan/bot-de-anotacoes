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

# --- CALENDAR ENGINE ---
def get_calendar():
    try:
        creds = Credentials.from_authorized_user_info(json.loads(os.getenv("GOOGLE_TOKEN")))
        return build('calendar', 'v3', credentials=creds)
    except: return None

def create_ev(titulo, data_iso):
    service = get_calendar()
    if not service: return None
    try:
        start = arrow.get(data_iso).replace(tzinfo='America/Sao_Paulo')
        event = {
            'summary': titulo,
            'start': {'dateTime': start.isoformat(), 'timeZone': 'America/Sao_Paulo'},
            'end': {'dateTime': start.shift(hours=1).isoformat(), 'timeZone': 'America/Sao_Paulo'},
        }
        return service.events().insert(calendarId='primary', body=event).execute()
    except: return None

def delete_ev(eid):
    service = get_calendar()
    if not service: return False
    try:
        service.events().delete(calendarId='primary', eventId=eid).execute()
        return True
    except: return False

def list_evs(t_min, t_max):
    service = get_calendar()
    if not service: return []
    try:
        return service.events().list(calendarId='primary', timeMin=t_min, timeMax=t_max, singleEvents=True, orderBy='startTime').execute().get('items', [])
    except: return []

# --- AI ENGINE ---
async def ask_lumi(user_input, context_str):
    agora = arrow.now('America/Sao_Paulo')
    system = f"""
    Nome: Lumi. Papel: Assistente de Agenda.
    Agora: {agora.format('YYYY-MM-DD HH:mm')} (Jaicós/PI).
    
    CONTEXTO (Eventos próximos):
    {context_str}

    REGRAS TÉCNICAS:
    - Retorne APENAS JSON.
    - Criar: {{"acao": "create", "titulo": "...", "data": "ISO"}}
    - Apagar: {{"acao": "delete", "id": "ID_DO_CONTEXTO"}}
    - Listar: {{"acao": "read"}}
    - Conversar: {{"acao": "chat", "msg": "..."}}
    """
    try:
        res = requests.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user_input}],
                "response_format": {"type": "json_object"}
            }, timeout=10)
        return res.json()['choices'][0]['message']['content']
    except: return json.dumps({"acao": "chat", "msg": "Tive um erro, Kauan. 😕"})

# --- TELEGRAM HANDLER ---
async def handle_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or update.effective_user.id != MEU_ID_TELEGRAM: return
    
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    # Busca contexto ampliado (7 dias antes e depois) para a IA não se perder em datas
    agora = arrow.now('America/Sao_Paulo')
    eventos = list_evs(agora.shift(days=-7).floor('day').isoformat(), agora.shift(days=7).ceil('day').isoformat())
    ctx_str = "\n".join([f"ID: {e['id']} | Data: {arrow.get(e['start'].get('dateTime')).format('DD/MM HH:mm')} | {e.get('summary')}" for e in eventos])

    raw = await ask_lumi(update.message.text or "oi", ctx_str)
    try:
        data = json.loads(raw)
        acao = data.get("acao")
        
        if acao == "create":
            res = create_ev(data.get("titulo"), data.get("data"))
            msg = f"✅ Criado: *{data.get('titulo')}* para às {arrow.get(data.get('data')).format('HH:mm')}! ✨" if res else "Erro ao criar. ❌"
        
        elif acao == "delete":
            msg = "🗑️ Evento removido com sucesso! ✨" if delete_ev(data.get("id")) else "Não achei esse evento para apagar. 🧐"
            
        elif acao == "read":
            # Listagem bonita direto via código
            if not eventos: msg = "Sua agenda está vazia nos próximos dias! 🌸"
            else:
                hoje_str = agora.format('DD/MM')
                lista = []
                for e in eventos:
                    dt = arrow.get(e['start'].get('dateTime')).format('DD/MM')
                    hr = arrow.get(e['start'].get('dateTime')).format('HH:mm')
                    prefixo = "📍 Hoje" if dt == hoje_str else f"📅 {dt}"
                    lista.append(f"{prefixo} - *{hr}*: {e.get('summary')}")
                msg = "📋 *Sua Agenda:*\n\n" + "\n".join(lista)
        
        else: msg = data.get("msg", "Oi Kauan! Como posso ajudar na sua agenda hoje? 🌸")

        await update.message.reply_text(msg, parse_mode='Markdown')
    except:
        await update.message.reply_text("Me enrolei aqui. Pode repetir? 😕")

# --- INFRA ---
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_msg))

@asynccontextmanager
async def lifespan(app: FastAPI):
    await bot_app.initialize(); await bot_app.start(); yield; await bot_app.stop()

app = FastAPI(lifespan=lifespan)
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    await bot_app.process_update(Update.de_json(data, bot_app.bot))
    return {"ok": True}