import os
from flask import Flask, request, jsonify
from supabase import create_client, Client as SupabaseClient
from twilio.rest import Client as TwilioClient
from dotenv import load_dotenv
from datetime import datetime
from flask_cors import CORS 



# Carrega as variáveis de ambiente do arquivo .env
load_dotenv()

app = Flask(__name__)
CORS(app)

# ===============================
# 🔹 Configurações dos Clientes
# ===============================

# --- Twilio Config ---
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
template_sid = os.getenv("TWILIO_TEMPLATE_SID") # Essencial para o disparo inicial

if not all([account_sid, auth_token, from_number, template_sid]):
    print("❌ ERRO: Verifique as variáveis TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER e TWILIO_TEMPLATE_SID no .env.")
    exit()
twilio_client = TwilioClient(account_sid, auth_token)
print("✅ Cliente Twilio inicializado.")

# --- Supabase Config ---
supabase_url: str = os.getenv("SUPABASE_URL")
supabase_key: str = os.getenv("SUPABASE_KEY")

if not all([supabase_url, supabase_key]):
    print("❌ ERRO: Verifique as variáveis SUPABASE_URL e SUPABASE_KEY no .env.")
    exit()
supabase: SupabaseClient = create_client(supabase_url, supabase_key)
print("✅ Cliente Supabase inicializado.")


# ===============================
# 🔹 Funções Utilitárias
# ===============================

def enviar_whatsapp(to, body):
    """Envia uma mensagem de texto simples via WhatsApp."""
    try:
        msg = twilio_client.messages.create(from_=from_number, to=to, body=body)
        print(f"✅ Mensagem de texto enviada para {to}: {msg.sid}")
        return msg.sid
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp para {to}: {e}")
        return None

def disparar_e_registrar_contato_inicial(numero, nome):
    """Dispara o template inicial da Twilio e cria/atualiza o contato no Supabase."""
    try:
        # 1. Cria ou atualiza o contato no Supabase
        contact_data = {
            'phone': numero,
            'name': nome,
            'status': 'inicial',
            'automationStatus': 'Ativa'
        }
        # A função upsert irá inserir ou atualizar se o telefone já existir
        response = supabase.table('WhatsAppContacts').upsert(contact_data, on_conflict='phone').execute()
        
        if not response.data:
            raise Exception(f"Falha ao salvar contato no Supabase: {response.error}")
        
        # 2. Dispara o template da Twilio
        msg = twilio_client.messages.create(
            from_=from_number,
            to=numero,
            content_sid=template_sid,
            content_variables=f'{{"1":"{nome}"}}'
        )
        print(f"✅ Template inicial disparado para {numero}: {msg.sid}")
        return {"status": "sucesso", "sid": msg.sid}

    except Exception as e:
        print(f"❌ Erro no processo de disparo inicial para {numero}: {e}")
        return {"status": "erro", "mensagem": str(e)}

# ===============================
# 🔹 Endpoint para Disparo Inicial
# ===============================

# app.py

# Adicione esta nova rota ao seu arquivo, pode ser depois do webhook.

# ===============================
# 🔹 Endpoint para Disparo em Lote
# ===============================
@app.route("/api/disparar_lote", methods=["POST"])
def api_disparar_lote():
    data = request.json
    contatos = data.get("contatos") # Espera uma lista de objetos: [{"numero": "...", "nome": "..."}, ...]

    if not contatos or not isinstance(contatos, list):
        return jsonify({"status": "erro", "mensagem": "A lista de 'contatos' é inválida ou ausente."}), 400

    resultados = {
        "sucessos": [],
        "falhas": []
    }
    
    # ATENÇÃO: Boa prática para listas grandes
    # Para listas com centenas/milhares de contatos, este loop pode causar um timeout na requisição.
    # A solução ideal seria usar uma fila de tarefas em background (como Celery ou RQ).
    # Para começar, esta abordagem sequencial é suficiente.
    
    for contato in contatos:
        numero = contato.get("numero")
        nome = contato.get("nome", "Cliente")

        if not numero:
            resultados["falhas"].append({"contato": contato, "motivo": "Número ausente"})
            continue
        
        # Garante o formato correto do número
        if not numero.startswith("whatsapp:"):
            numero = "whatsapp:" + numero

        resultado_individual = disparar_e_registrar_contato_inicial(numero, nome)
        
        if resultado_individual["status"] == "sucesso":
            resultados["sucessos"].append({"contato": contato, "sid": resultado_individual["sid"]})
        else:
            resultados["falhas"].append({"contato": contato, "motivo": resultado_individual["mensagem"]})

    return jsonify(resultados)


@app.route("/api/disparar_template", methods=["POST"])
def api_disparar_template():
    data = request.json
    to_number = data.get("numero")
    nome = data.get("nome", "Cliente")

    if not to_number:
        return jsonify({"status": "erro", "mensagem": "Número de telefone ausente."}), 400

    # Garante que o número está no formato correto para a Twilio
    if not to_number.startswith("whatsapp:"):
        to_number = "whatsapp:" + to_number
    
    resultado = disparar_e_registrar_contato_inicial(to_number, nome)
    
    if resultado["status"] == "sucesso":
        return jsonify(resultado)
    else:
        return jsonify(resultado), 500

# ===============================
# 🔹 Webhook (respostas do cliente)
# ===============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.form.to_dict()
    print(f"📩 Webhook recebido: {data}")

    from_number_user = data.get("From")
    profile_name = data.get("ProfileName", "Cliente")
    body = data.get("Body", "").strip().lower()
    original_body = data.get("Body", "").strip()
    message_sid = data.get("MessageSid") # Captura o ID da mensagem da Twilio

    if not from_number_user:
        return "OK", 200

    try:
        response = supabase.table('WhatsAppContacts').select('id, status, name').eq('phone', from_number_user).limit(1).execute()
        contact = response.data[0] if response.data else None

        if not contact:
            return "OK", 200 # Ignora mensagens de números não iniciados pelo sistema

        contact_id = contact['id']
        current_status = contact['status']
        contact_name = contact.get('name') or profile_name
        
        print(f"ℹ️ Status do cliente {from_number_user} (ID: {contact_id}): {current_status}")

        # Salva a mensagem recebida no histórico, agora incluindo o messageIdFromApi
        message_data = {
            "contactId": contact_id,
            "sender": "user",
            "text": original_body,
            "timestamp": datetime.now().isoformat(),
            "messageIdFromApi": message_sid # Adiciona o SID da mensagem aqui
        }
        supabase.table('WhatsAppMessages').insert(message_data).execute()

        supabase.table('WhatsAppContacts').update({'lastMessage': original_body, 'lastTimestamp': datetime.now().isoformat(), 'unread': True}).eq('id', contact_id).execute()

        # Handler universal para "atendente"
        if "atendente" in body:
            enviar_whatsapp(from_number_user, "Certo! Um de nossos especialistas entrará em contato em breve. Obrigado!")
            supabase.table('WhatsAppContacts').update({"status": "Aguardando Vendedor", "automationStatus": "Pausada"}).eq('id', contact_id).execute()
            return "OK", 200

        # --- FLUXO DE CONVERSA PRINCIPAL ---
        if current_status == "inicial":
            if any(x in body for x in ["sim", "s", "yes", "quero"]):
                termo_url = os.getenv('TERMO_URL', 'http://seusite.com/termo.docx')
                mensagem_com_termo = (
                    f"Excelente, {contact_name}! 🙌\n\n"
                    "Para darmos andamento de forma organizada e transparente, estou enviando em anexo o Termo de Representação.\n\n"
                    "Esse termo autoriza a Winston Serviços Corporativos a representar o(a) Sr.(a) na busca de propostas para o seu precatório.\n\n"
                    "📌 *Importante:*\n"
                    "• O documento não obriga a venda imediata;\n"
                    "• Garante apenas que a Winston poderá negociar em seu nome, protegendo sua oportunidade;\n"
                    "• Prevê uma remuneração de 6% para a Winston, paga somente em caso de efetiva venda.\n\n"
                    f"📄 *Acesse o termo aqui:* {termo_url}\n\n"
                    "Após o preenchimento, envie a palavra *preenchido* para prosseguirmos."
                )
                enviar_whatsapp(from_number_user, mensagem_com_termo)
                supabase.table('WhatsAppContacts').update({"status": "aguardando_termo"}).eq('id', contact_id).execute()

            elif any(x in body for x in ["não", "nao", "n", "no"]):
                mensagem_oferta_futura = (
                    f"Entendido, {contact_name}!\n\n"
                    "Mesmo assim, gostaríamos de manter você informado(a) sobre oportunidades futuras que podem oferecer condições ainda melhores.\n\n"
                    "Aceita que a Winston envie propostas futuras, caso surjam oportunidades vantajosas?\n\n"
                    "Responda com *SIM* ou *NÃO*."
                )
                enviar_whatsapp(from_number_user, mensagem_oferta_futura)
                supabase.table('WhatsAppContacts').update({"status": "oferta_futura"}).eq('id', contact_id).execute()
            else:
                 enviar_whatsapp(from_number_user, f"Olá {contact_name}, não entendi sua resposta. Por favor, responda com 'SIM' para prosseguir ou 'NÃO' para encerrar.")

        elif current_status == "oferta_futura":
            if any(x in body for x in ["sim", "s", "yes"]):
                enviar_whatsapp(from_number_user, f"Confirmado, {contact_name}! Manteremos seu contato para futuras oportunidades.")
                supabase.table('WhatsAppContacts').update({"status": "aguardando_oferta_futura", "automationStatus": "Pausada"}).eq('id', contact_id).execute()
            
            elif any(x in body for x in ["não", "nao", "n", "no"]):
                mensagem_final = (
                    f"Entendido, {contact_name}.\n\n"
                    "Respeitamos sua decisão. Caso mude de ideia, estaremos à disposição.\n\n"
                    "Obrigado pela atenção!\n\n"
                    "Winston Serviços Corporativos"
                )
                enviar_whatsapp(from_number_user, mensagem_final)
                supabase.table('WhatsAppContacts').update({"status": "recusou_contato_futuro", "automationStatus": "Concluída"}).eq('id', contact_id).execute()

        elif current_status == "aguardando_termo":
            if "preenchido" in body:
                mensagem_confirmacao = (
                    f"Agradecemos a confiança, {contact_name}! 🙏\n\n"
                    "A partir de agora, sua oportunidade será apresentada a bancos, fundos e investidores da nossa base qualificada.\n\n"
                    "Assim que tivermos uma boa proposta concreta, entraremos em contato imediatamente para compartilhar os detalhes.\n\n"
                    "Seguiremos juntos para viabilizar a melhor negociação possível.\n\n"
                    "Atenciosamente, Winston Serviços Corporativos."
                )
                enviar_whatsapp(from_number_user, mensagem_confirmacao)
                supabase.table('WhatsAppContacts').update({"status": "pos_termo", "termo_enviado_em": datetime.now().isoformat()}).eq('id', contact_id).execute()
            else:
                enviar_whatsapp(from_number_user, "Ainda estamos aguardando o preenchimento do termo. Assim que o fizer, por favor, envie a palavra *preenchido*.")

    except Exception as e:
        print(f"🔥 Erro crítico ao processar o webhook para {from_number_user}: {e}")

    return "OK", 200


# ===============================
# 🔹 Endpoint para Disparo em Lote
# ===============================
@app.route("/api/disparar_lote", methods=["POST"])
def api_disparar_lote():
    data = request.json
    contatos = data.get("contatos") # Espera uma lista de objetos: [{"numero": "...", "nome": "..."}, ...]

    if not contatos or not isinstance(contatos, list):
        return jsonify({"status": "erro", "mensagem": "A lista de 'contatos' é inválida ou ausente."}), 400

    resultados = {
        "sucessos": [],
        "falhas": []
    }
    
    # ATENÇÃO: Boa prática para listas grandes
    # Para listas com centenas/milhares de contatos, este loop pode causar um timeout na requisição.
    # A solução ideal seria usar uma fila de tarefas em background (como Celery ou RQ).
    # Para dezenas de contatos por vez, esta abordagem sequencial é suficiente para começar.
    
    for contato in contatos:
        numero = contato.get("numero")
        nome = contato.get("nome", "Cliente")

        if not numero:
            resultados["falhas"].append({"contato": contato, "motivo": "Número ausente"})
            continue
        
        # Garante o formato correto do número
        if not numero.startswith("whatsapp:"):
            numero = "whatsapp:" + numero

        resultado_individual = disparar_e_registrar_contato_inicial(numero, nome)
        
        if resultado_individual["status"] == "sucesso":
            resultados["sucessos"].append({"contato": contato, "sid": resultado_individual["sid"]})
        else:
            resultados["falhas"].append({"contato": contato, "motivo": resultado_individual["mensagem"]})

    return jsonify(resultados)



# 🔹 Iniciar servidor
# ===============================
if __name__ == "__main__":
    print("🚀 Servidor Flask (Bot WhatsApp) iniciando...")
    app.run(port=5000, debug=True, use_reloader=False)


# curl -X POST -H "Content-Type: application/json" \
#  -d '{"numero": "+5521969927793", "nome": "Cliente Teste"}' \
#  https://chatbot-python-webhook.onrender.com/api/disparar_template

