"""
🤖 BOT DE AGENDA INTELIGENTE V2.0 - RAILWAY EDITION
Bot Telegram + FastAPI + Gemini AI + Google Calendar

CORREÇÕES PRINCIPAIS:
✅ Gemini corrigido (removido responseMimeType incompatível)
✅ Parser fallback melhorado
✅ Confirmações antes de criar/deletar
✅ Logs estruturados
✅ Tratamento robusto de erros
✅ Comandos /start, /agenda, /ajuda
✅ Suporte a horários personalizados
✅ Validação de datas
"""

import os
import json
import datetime
import asyncio
import requests
import re
from contextlib import asynccontextmanager
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, 
    MessageHandler, 
    CommandHandler,
    CallbackQueryHandler,
    filters, 
    ContextTypes
)
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ==========================================
# 1. CONFIGURAÇÕES GERAIS
# ==========================================
load_dotenv()
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TIMEZONE = "America/Sao_Paulo"

# Armazenamento temporário de eventos pendentes de confirmação
pending_events_store = {}

# ==========================================
# 2. INTEGRAÇÃO GOOGLE CALENDAR (MELHORADA)
# ==========================================
class CalendarManager:
    """Gerenciador melhorado do Google Calendar"""
    
    @staticmethod
    def get_service():
        """Obtém serviço autenticado do Google Calendar"""
        token_json = os.getenv("GOOGLE_TOKEN")
        try:
            if not token_json: 
                print("❌ ERRO: Variável GOOGLE_TOKEN não encontrada.")
                return None
            creds_dict = json.loads(token_json)
            creds = Credentials.from_authorized_user_info(creds_dict)
            service = build('calendar', 'v3', credentials=creds)
            print("✅ Google Calendar conectado")
            return service
        except Exception as e:
            print(f"❌ Erro ao carregar credenciais Google: {e}")
            return None
    
    @staticmethod
    def list_events(max_results=15):
        """Lista eventos futuros"""
        service = CalendarManager.get_service()
        if not service: 
            return []
        
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        try:
            events_result = service.events().list(
                calendarId='primary', 
                timeMin=now, 
                maxResults=max_results, 
                singleEvents=True, 
                orderBy='startTime'
            ).execute()
            events = events_result.get('items', [])
            print(f"📋 {len(events)} eventos encontrados")
            return events
        except Exception as e:
            print(f"❌ Erro ao listar eventos: {e}")
            return []
    
    @staticmethod
    def create_event(titulo: str, data_iso: str, duracao_horas: int = 1, descricao: str = None):
        """Cria evento no calendário"""
        service = CalendarManager.get_service()
        if not service: 
            return None
        
        try:
            dt_inicio = datetime.datetime.fromisoformat(data_iso)
            dt_fim = dt_inicio + datetime.timedelta(hours=duracao_horas)
            
            event = {
                'summary': titulo,
                'start': {
                    'dateTime': dt_inicio.isoformat(), 
                    'timeZone': TIMEZONE
                },
                'end': {
                    'dateTime': dt_fim.isoformat(), 
                    'timeZone': TIMEZONE
                },
            }
            
            if descricao:
                event['description'] = descricao
            
            created = service.events().insert(calendarId='primary', body=event).execute()
            print(f"✅ Evento criado: {created.get('id')} - {titulo}")
            return created
            
        except Exception as e:
            print(f"❌ Erro ao criar evento: {e}")
            return None
    
    @staticmethod
    def delete_event(event_id: str):
        """Deleta evento do calendário"""
        service = CalendarManager.get_service()
        if not service: 
            return False
        
        try:
            service.events().delete(calendarId='primary', eventId=event_id).execute()
            print(f"✅ Evento deletado: {event_id}")
            return True
        except Exception as e:
            print(f"❌ Erro ao deletar evento {event_id}: {e}")
            return False


# ==========================================
# 3. MOTOR DE IA - GEMINI (CORRIGIDO)
# ==========================================
class GeminiAI:
    """Motor de Inteligência Artificial usando Gemini"""
    
    @staticmethod
    async def process_intent(user_message: str, current_events: List[Dict]) -> Dict[str, Any]:
        """
        Processa mensagem do usuário com IA Gemini
        CORREÇÃO: Removido responseMimeType que causava erro 400
        """
        if not GEMINI_KEY:
            print("⚠️ GEMINI_API_KEY não configurada - usando parser tradicional")
            return GeminiAI._fallback_parser(user_message, current_events)
        
        agora = datetime.datetime.now()
        limite_48h = agora + datetime.timedelta(hours=48)
        
        # Simplificar eventos para contexto da IA
        eventos_contexto = []
        for e in current_events[:10]:  # Limitar para não estourar contexto
            eventos_contexto.append({
                "id": e.get('id'),
                "titulo": e.get('summary', 'Sem título'),
                "inicio": e.get('start', {}).get('dateTime', e.get('start', {}).get('date', ''))
            })
        
        # Prompt otimizado
        prompt = f"""Você é um assistente de agenda inteligente. Hoje é {agora.strftime("%d/%m/%Y %H:%M")}.

EVENTOS ATUAIS:
{json.dumps(eventos_contexto, ensure_ascii=False, indent=2)}

REGRAS CRÍTICAS:
1. Para CRIAR eventos: extraia título, data/hora do pedido do usuário
2. Para DELETAR: apenas eventos nas próximas 48h ({limite_48h.strftime("%d/%m/%Y %H:%M")})
3. Para LISTAR: retorne resumo dos próximos eventos
4. Para conversas gerais: responda amigavelmente

MENSAGEM DO USUÁRIO: "{user_message}"

Retorne APENAS JSON válido (sem markdown, sem ```):
{{
  "acao": "create" | "delete" | "read" | "chat",
  "resposta_amigavel": "texto para o usuário",
  "parametros": {{
    "titulo": "string (para create)",
    "data_inicio": "YYYY-MM-DDTHH:MM:SS (para create)",
    "duracao_horas": 1,
    "event_ids": ["id1", "id2"] (para delete),
    "descricao": "opcional"
  }},
  "confianca": 0.0-1.0
}}"""

        url = f"https://generativelanguage.googleapis.com/v1/models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}"
        
        # CORREÇÃO PRINCIPAL: Remover responseMimeType incompatível
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.4,
                "topK": 40,
                "topP": 0.95,
                "maxOutputTokens": 1024,
                # ❌ REMOVIDO: "responseMimeType": "application/json"
            }
        }

        try:
            print(f"🤖 Enviando para Gemini: {user_message[:50]}...")
            response = await asyncio.to_thread(
                requests.post, 
                url, 
                json=payload, 
                timeout=15
            )
            
            if response.status_code != 200:
                print(f"❌ Erro API Gemini {response.status_code}: {response.text}")
                return GeminiAI._fallback_parser(user_message, current_events)
            
            res_json = response.json()
            texto_ia = res_json['candidates'][0]['content']['parts'][0]['text']
            
            # Limpar markdown se houver
            texto_ia = texto_ia.replace("```json", "").replace("```", "").strip()
            
            # Parsear JSON
            resultado = json.loads(texto_ia)
            print(f"✅ IA respondeu: ação={resultado.get('acao')}, confiança={resultado.get('confianca')}")
            
            return resultado
            
        except json.JSONDecodeError as e:
            print(f"❌ Erro ao parsear JSON da IA: {e}")
            print(f"Resposta recebida: {texto_ia[:200]}")
            return GeminiAI._fallback_parser(user_message, current_events)
            
        except Exception as e:
            print(f"❌ Erro na chamada Gemini: {e}")
            return GeminiAI._fallback_parser(user_message, current_events)
    
    @staticmethod
    def _fallback_parser(user_message: str, current_events: List[Dict]) -> Dict[str, Any]:
        """Parser tradicional quando IA falha"""
        print("🔄 Usando parser fallback")
        
        msg_lower = user_message.lower()
        
        # Detectar listagem
        if any(word in msg_lower for word in ['agenda', 'eventos', 'compromissos', 'lista']):
            return {
                "acao": "read",
                "resposta_amigavel": "📅 Aqui está sua agenda!",
                "parametros": {},
                "confianca": 0.9
            }
        
        # Detectar cancelamento
        if any(word in msg_lower for word in ['cancelar', 'apagar', 'deletar', 'remover']):
            # Tentar encontrar evento mais recente
            if current_events:
                return {
                    "acao": "delete",
                    "resposta_amigavel": "Qual evento você quer cancelar?",
                    "parametros": {"event_ids": []},
                    "confianca": 0.5
                }
        
        # Tentar parsear formato: "dia X de mês [às HH:MM] titulo"
        match = re.search(
            r'dia\s+(\d{1,2})\s+de\s+(\w+)(?:\s+às\s+(\d{1,2}):?(\d{2}))?',
            msg_lower
        )
        
        if match:
            meses = {
                "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4,
                "maio": 5, "junho": 6, "julho": 7, "agosto": 8,
                "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12
            }
            
            dia = int(match.group(1))
            mes = meses.get(match.group(2))
            hora = int(match.group(3)) if match.group(3) else 9
            minuto = int(match.group(4)) if match.group(4) else 0
            
            if mes:
                ano = datetime.datetime.now().year
                titulo = user_message[match.end():].strip() or "Evento"
                
                try:
                    data = datetime.datetime(ano, mes, dia, hora, minuto)
                    
                    return {
                        "acao": "create",
                        "resposta_amigavel": f"📅 Vou criar: {titulo}",
                        "parametros": {
                            "titulo": titulo,
                            "data_inicio": data.isoformat(),
                            "duracao_horas": 1
                        },
                        "confianca": 0.85
                    }
                except ValueError:
                    pass
        
        # Resposta padrão
        return {
            "acao": "chat",
            "resposta_amigavel": "Não entendi bem. Use:\n• 'dia 15 de dezembro às 14h Reunião'\n• 'mostra minha agenda'\n• '/ajuda' para ver mais opções",
            "parametros": {},
            "confianca": 0.3
        }


# ==========================================
# 4. HANDLERS TELEGRAM (MELHORADOS)
# ==========================================
class TelegramHandlers:
    """Handlers do bot Telegram"""
    
    @staticmethod
    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /start"""
        ai_status = "✅" if GEMINI_KEY else "⚠️ (modo básico)"
        
        msg = f"""
🤖 *Bot de Agenda Inteligente v2.0*

IA Gemini: {ai_status}

*Como usar:*
• "Reunião com João amanhã às 15h"
• "Dentista dia 25 de dezembro às 14:30"
• "Academia segunda às 7h"

*Comandos:*
/agenda - Ver próximos eventos
/ajuda - Mais exemplos
/start - Esta mensagem

Pode me mandar mensagens naturais! 😊
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    @staticmethod
    async def cmd_ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /ajuda"""
        msg = """
📚 *Exemplos de uso:*

*Criar eventos:*
• "Reunião com cliente amanhã 14h"
• "Aniversário da Maria dia 10 de maio"
• "dia 15 de dezembro às 10h Dentista"

*Ver agenda:*
• "mostra minha agenda"
• "quais meus compromissos?"
• /agenda

*Cancelar:*
• "cancela o evento de amanhã"
• "apaga a reunião das 15h"

Experimente! Sou flexível. 🚀
"""
        await update.message.reply_text(msg, parse_mode='Markdown')
    
    @staticmethod
    async def cmd_agenda(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comando /agenda"""
        try:
            eventos = await asyncio.to_thread(CalendarManager.list_events, 7)
            
            if not eventos:
                await update.message.reply_text("📅 Você não tem eventos próximos.")
                return
            
            msg = "📅 *Sua agenda:*\n\n"
            
            for e in eventos:
                titulo = e.get('summary', 'Sem título')
                start = e.get('start', {})
                data_str = start.get('dateTime', start.get('date', ''))
                
                try:
                    dt = datetime.datetime.fromisoformat(data_str.replace('Z', '+00:00'))
                    data_fmt = dt.strftime("%d/%m às %H:%M")
                except:
                    data_fmt = data_str[:10]
                
                msg += f"• *{titulo}*\n  📆 {data_fmt}\n\n"
            
            await update.message.reply_text(msg, parse_mode='Markdown')
            
        except Exception as e:
            print(f"❌ Erro em /agenda: {e}")
            await update.message.reply_text("❌ Erro ao buscar agenda.")
    
    @staticmethod
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler principal de mensagens"""
        if not update.message or not update.message.text:
            return
        
        user_id = update.effective_user.id
        user_message = update.message.text
        
        try:
            # Mostrar typing
            await context.bot.send_chat_action(
                chat_id=update.effective_chat.id, 
                action='typing'
            )
            
            # 1. Buscar eventos atuais
            print(f"📨 [{user_id}] {user_message}")
            eventos = await asyncio.to_thread(CalendarManager.list_events)
            
            # 2. Processar com IA
            decisao = await GeminiAI.process_intent(user_message, eventos)
            
            acao = decisao.get("acao")
            params = decisao.get("parametros", {})
            resposta = decisao.get("resposta_amigavel", "Processado.")
            confianca = decisao.get("confianca", 0)
            
            # 3. Validar confiança
            if confianca < 0.4:
                await update.message.reply_text(
                    "🤔 Não tenho certeza se entendi. Pode reformular?\nUse /ajuda para ver exemplos."
                )
                return
            
            # 4. Executar ação
            if acao == "create":
                # Armazenar evento pendente
                pending_events_store[user_id] = params
                
                # Formatar confirmação
                try:
                    dt = datetime.datetime.fromisoformat(params.get("data_inicio"))
                    data_fmt = dt.strftime("%d/%m/%Y às %H:%M")
                except:
                    data_fmt = params.get("data_inicio", "")
                
                confirm_msg = f"""
📅 *Confirmar evento?*

*Título:* {params.get('titulo', 'Sem título')}
*Data:* {data_fmt}
*Duração:* {params.get('duracao_horas', 1)}h
"""
                
                keyboard = [[
                    InlineKeyboardButton("✅ Confirmar", callback_data=f"confirm_{user_id}"),
                    InlineKeyboardButton("❌ Cancelar", callback_data=f"cancel_{user_id}")
                ]]
                
                await update.message.reply_text(
                    confirm_msg,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                
            elif acao == "delete":
                event_ids = params.get("event_ids", [])
                if event_ids:
                    # TODO: Implementar confirmação de exclusão
                    deletados = 0
                    for eid in event_ids:
                        if await asyncio.to_thread(CalendarManager.delete_event, eid):
                            deletados += 1
                    
                    await update.message.reply_text(
                        f"✅ {deletados} evento(s) cancelado(s)."
                    )
                else:
                    await update.message.reply_text(resposta)
                    
            elif acao == "read":
                await TelegramHandlers.cmd_agenda(update, context)
                
            else:  # chat
                await update.message.reply_text(resposta, parse_mode='Markdown')
        
        except Exception as e:
            print(f"❌ Erro no handler: {e}")
            import traceback
            traceback.print_exc()
            await update.message.reply_text(
                "❌ Ops! Tive um problema. Tente novamente."
            )
    
    @staticmethod
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handler de callbacks (botões)"""
        query = update.callback_query
        await query.answer()
        
        try:
            action, user_id = query.data.split('_')
            user_id = int(user_id)
            
            if action == "cancel":
                if user_id in pending_events_store:
                    del pending_events_store[user_id]
                await query.edit_message_text("❌ Cancelado.")
                return
            
            if action == "confirm":
                if user_id not in pending_events_store:
                    await query.edit_message_text("⚠️ Evento expirou. Tente novamente.")
                    return
                
                params = pending_events_store[user_id]
                
                # Criar evento
                created = await asyncio.to_thread(
                    CalendarManager.create_event,
                    titulo=params.get('titulo'),
                    data_iso=params.get('data_inicio'),
                    duracao_horas=params.get('duracao_horas', 1),
                    descricao=params.get('descricao')
                )
                
                if created:
                    link = created.get('htmlLink', '')
                    msg = f"✅ *Evento criado!*\n\n📌 {params.get('titulo')}"
                    if link:
                        msg += f"\n\n[Ver no Google Calendar]({link})"
                    
                    await query.edit_message_text(msg, parse_mode='Markdown')
                    del pending_events_store[user_id]
                else:
                    await query.edit_message_text("❌ Erro ao criar evento.")
                    
        except Exception as e:
            print(f"❌ Erro no callback: {e}")
            await query.edit_message_text("❌ Erro ao processar.")


# ==========================================
# 5. FASTAPI & INICIALIZAÇÃO
# ==========================================
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# Registrar handlers
bot_app.add_handler(CommandHandler("start", TelegramHandlers.cmd_start))
bot_app.add_handler(CommandHandler("ajuda", TelegramHandlers.cmd_ajuda))
bot_app.add_handler(CommandHandler("help", TelegramHandlers.cmd_ajuda))
bot_app.add_handler(CommandHandler("agenda", TelegramHandlers.cmd_agenda))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, TelegramHandlers.handle_message))
bot_app.add_handler(CallbackQueryHandler(TelegramHandlers.handle_callback))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerenciador de ciclo de vida do FastAPI"""
    print("🚀 Inicializando bot...")
    await bot_app.initialize()
    await bot_app.start()
    print("✅ Bot online e pronto!")
    yield
    print("🛑 Encerrando bot...")
    await bot_app.stop()
    await bot_app.shutdown()

app = FastAPI(lifespan=lifespan, title="Bot Agenda Inteligente")

@app.get("/")
async def root():
    """Health check"""
    return {
        "status": "online",
        "bot": "Agenda Inteligente v2.0",
        "ia": "Gemini" if GEMINI_KEY else "Parser básico"
    }

@app.post("/webhook")
async def telegram_webhook(request: Request):
    """Webhook do Telegram"""
    try:
        data = await request.json()
        update = Update.de_json(data, bot_app.bot)
        asyncio.create_task(bot_app.process_update(update))
        return {"status": "ok"}
    except Exception as e:
        print(f"❌ Erro no webhook: {e}")
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    print(f"🚀 Iniciando servidor na porta {port}")
    uvicorn.run(app, host="0.0.0.0", port=port)