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
# 1. CONFIGURAÇÕES E MEMÓRIA
# ==========================================
load_dotenv()
GROQ_KEY = os.getenv("GROQ_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
MEU_ID_TELEGRAM = int(os.getenv("MEU_ID_TELEGRAM", 0))

# Mapeamento amigável de cores para o Google Calendar
CORES_GOOGLE = {
    "lavanda": "1", "verde": "10", "azul": "7", "amarelo": "5", 
    "laranja": "6", "vermelho": "11", "rosa": "4", "roxo": "3", "cinza": "8"
}

# Categorias em memória (Serão atualizadas via chat)
MINHAS_CATEGORIAS = {
    "Estudo": "1", "Academia": "10", "Trabalho": "7"
}

# ==========================================
# 2. INTEGRAÇÃO CALENDAR
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
    dt = datetime.datetime.fromisoformat(data_iso)
    event = {
        'summary': titulo,
        'colorId': color_id,
        'start': {'dateTime': dt.isoformat(), 'timeZone': 'America/Sao_Paulo'},
        'end': {'dateTime': (dt + datetime.timedelta(hours=1)).isoformat(), 'timeZone': 'America/Sao_Paulo'},
    }
    return service.events().insert(calendarId='primary', body=event).execute()

# ==========================================
# 3. MOTOR DE IA (GESTÃO NATURAL)
# ==========================================
async def process_intent_with_ai(prompt_text):
    fuso = datetime.timezone(datetime.timedelta(hours=-3))
    agora = datetime.datetime.now(fuso)
    
    prompt_sistema = f"""
    Hoje: {agora.strftime("%Y-%m-%d %H:%M:%S")}. Local: Jaicós, PI.
    Categorias Atuais: {json.dumps(MINHAS_CATEGORIAS, ensure_ascii=False)}
    Cores Disponíveis: {list(CORES_GOOGLE.keys())}

    Responda APENAS JSON:
    {{
        "acao": "create"|"read"|"delete"|"config"|"chat", 
        "resposta_amigavel": "...", 
        "parametros": {{
            "titulo": "...", "data_inicio": "ISO", "color_id": "ID",
            "config_tipo": "add"|"del"|"list",
            "cat_nome": "...", "cat_cor_nome": "..."
        }}
    }}
    Se o usuário quiser criar/mudar categoria, use acao='config'.
    """

    url = "https://api.groq.com/openai/v1/chat/completions"
    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "system", "content": prompt_sistema}, {"role": "user", "content": prompt_text}],
        "response_format": {"type": "json_object"}
    }

    try:
        response = await asyncio.to_thread(requests.post, url, json=payload, headers={"Authorization": f"Bearer {GROQ_KEY}"}, timeout=10)
        return response.json()['choices'][0]['message']['content']
    except: return "{}"

# ==========================================
# 4. HANDLER PRINCIPAL
# ==========================================
async def handle_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    if update.effective_user.id != MEU_ID_TELEGRAM: return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action='typing')
    
    try:
        res_raw = await process_intent_with_ai(update.message.text)
        decisao = json.loads(res_raw)
        
        acao = decisao.get("acao")
        params = decisao.get("parametros", {})
        msg = decisao.get("resposta_amigavel", "Processado!")

        # --- LÓGICA DE CONFIGURAÇÃO NATURAL ---
        if acao == "config":
            tipo = params.get("config_tipo")
            nome = params.get("cat_nome")
            cor_nome = params.get("cat_cor_nome", "").lower()

            if tipo == "add":
                id_cor = CORES_GOOGLE.get(cor_nome, "1")
                MINHAS_CATEGORIAS[nome] = id_cor
                msg = f"✅ Categoria *{nome}* criada com a cor *{cor_nome}*!"
            elif tipo == "del":
                MINHAS_CATEGORIAS.pop(nome, None)
                msg = f"🗑️ Categoria *{nome}* removida."
            elif tipo == "list":
                lista = "\n".join([f"• {k}" for k in MINHAS_CATEGORIAS.keys()])
                msg = f"🎨 *Suas Categorias:*\n{lista}"

        # --- LÓGICA DE CRIAÇÃO ---
        elif acao == "create":
            # Se a IA não mandou color_id mas o título tem a categoria, nós forçamos
            color_id = params.get("color_id")
            evento = await asyncio.to_thread(_sync_create_event, params.get("titulo"), params.get("data_inicio"), color_id)
            link = evento.get('htmlLink')
            msg += f"\n\n🔗 [Ver no Google]({link})"

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        print(f"Erro: {e}")
        await update.message.reply_text("❌ Tive um problema ao processar.")

# ==========================================
# 5. SETUP FASTAPI
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_update))

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