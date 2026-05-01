import re
import datetime
import os

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ===== GOOGLE CALENDAR =====
def get_service():
    creds = Credentials.from_authorized_user_file('token.json')
    return build('calendar', 'v3', credentials=creds)

service = get_service()

# ===== PARSER =====
def parse_message(text):
    match = re.search(r'dia (\d{1,2}) de (\w+)', text.lower())

    meses = {
        "janeiro":1,"fevereiro":2,"março":3,"abril":4,
        "maio":5,"junho":6,"julho":7,"agosto":8,
        "setembro":9,"outubro":10,"novembro":11,"dezembro":12
    }

    if not match:
        return None

    dia = int(match.group(1))
    mes = meses.get(match.group(2))

    titulo = text.split(match.group(0))[-1].strip()

    if not titulo:
        titulo = "Evento"

    ano = datetime.datetime.now().year
    data = datetime.datetime(ano, mes, dia, 9, 0)

    return titulo, data

# ===== CRIAR EVENTO =====
def create_event(titulo, data):
    event = {
        'summary': titulo,
        'start': {
            'dateTime': data.isoformat(),
            'timeZone': 'America/Sao_Paulo'
        },
        'end': {
            'dateTime': (data + datetime.timedelta(hours=1)).isoformat(),
            'timeZone': 'America/Sao_Paulo'
        },
    }

    service.events().insert(calendarId='primary', body=event).execute()

# ===== TELEGRAM =====
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    parsed = parse_message(text)

    if not parsed:
        await update.message.reply_text("Use: Dia X de mês ...")
        return

    titulo, data = parsed

    create_event(titulo, data)

    await update.message.reply_text(
        f"Evento criado: {titulo} em {data.strftime('%d/%m %H:%M')}"
    )

# ===== INICIAR BOT =====
TOKEN = os.getenv("TELEGRAM_TOKEN")

if not TOKEN:
    raise ValueError("Token do Telegram não encontrado. Configure a variável TELEGRAM_TOKEN.")

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(MessageHandler(filters.TEXT, handle_message))

print("Bot rodando...")

app.run_polling()