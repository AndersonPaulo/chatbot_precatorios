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
template_sid = os.getenv("TWILIO_TEMPLATE_SID")

if not all([account_sid, auth_token, from_number, template_sid]):
    print("❌ ERRO: Verifique as variáveis de ambiente da Twilio.")
    exit()
twilio_client = TwilioClient(account_sid, auth_token)
print("✅ Cliente Twilio inicializado.")

# --- Supabase Config ---
supabase_url: str = os.getenv("SUPABASE_URL")
supabase_key: str = os.getenv("SUPABASE_KEY")

if not all([supabase_url, supabase_key]):
    print("❌ ERRO: Verifique as variáveis de ambiente do Supabase.")
    exit()
supabase: SupabaseClient = create_client(supabase_url, supabase_key)
print("✅ Cliente Supabase inicializado.")

# ===============================
# 🔹 Funções Utilitárias
# ===============================

def enviar_whatsapp(to, body):
    try:
        msg = twilio_client.messages.create(from_=from_number, to=to, body=body)
        print(f"✅ Mensagem de texto enviada para {to}: {msg.sid}")
        return msg.sid
    except Exception as e:
        print(f"❌ Erro ao enviar WhatsApp para {to}: {e}")
        return None

def disparar_e_registrar_contato_inicial(numero, nome):
    try:
        contact_data = {'phone': numero, 'name': nome, 'status': 'inicial', 'automationStatus': 'Ativa'}
        response = supabase.table('WhatsAppContacts').upsert(contact_data, on_conflict='phone').execute()
        
        if not response.data:
            raise Exception(f"Falha ao salvar contato no Supabase: {response.error}")
        
        msg = twilio_client.messages.create(
            from_=from_number, to=numero, content_sid=template_sid, content_variables=f'{{"1":"{nome}"}}'
        )
        print(f"✅ Template inicial disparado para {numero}: {msg.sid}")
        return {"status": "sucesso", "sid": msg.sid}
    except Exception as e:
        print(f"❌ Erro no processo de disparo inicial para {numero}: {e}")
        return {"status": "erro", "mensagem": str(e)}

# ===============================
# 🔹 Endpoints da API
# ===============================

@app.route("/api/disparar_template", methods=["POST"])
def api_disparar_template():
    data = request.json
    numero = data.get("numero")
    nome = data.get("nome")
    if not numero or not nome:
        return jsonify({"status": "erro", "mensagem": "Número e nome são obrigatórios."}), 400
    if not numero.startswith("whatsapp:"):
        numero = "whatsapp:" + numero
    resultado = disparar_e_registrar_contato_inicial(numero, nome)
    if resultado["status"] == "sucesso":
        return jsonify(resultado), 200
    else:
        return jsonify(resultado), 500

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.form.to_dict()
    print(f"📩 Webhook recebido: {data}")

    from_number_user = data.get("From")
    profile_name = data.get("ProfileName", "Cliente")
    body = data.get("Body", "").strip().lower()
    original_body = data.get("Body", "").strip()
    message_sid = data.get("MessageSid")

    if not from_number_user:
        return "OK", 200

    try:
        response = supabase.table('WhatsAppContacts').select('id, status, name').eq('phone', from_number_user).limit(1).execute()
        contact = response.data[0] if response.data else None
        if not contact: return "OK", 200

        contact_id = contact['id']
        current_status = contact['status']
        contact_name = contact.get('name') or profile_name
        
        message_data = {"contactId": contact_id, "sender": "user", "text": original_body, "timestamp": datetime.now().isoformat(), "messageIdFromApi": message_sid}
        supabase.table('WhatsAppMessages').insert(message_data).execute()
        supabase.table('WhatsAppContacts').update({'lastMessage': original_body, 'lastTimestamp': datetime.now().isoformat(), 'unread': True}).eq('id', contact_id).execute()

        if "atendente" in body:
            enviar_whatsapp(from_number_user, "Certo! Um de nossos especialistas entrará em contato em breve. Obrigado!")
            supabase.table('WhatsAppContacts').update({"status": "Aguardando Vendedor", "automationStatus": "Pausada"}).eq('id', contact_id).execute()
            return "OK", 200
        
        # --- FLUXO DE CONVERSA PRINCIPAL --- (Seu fluxo continua aqui)
        # ...

    except Exception as e:
        print(f"🔥 Erro crítico ao processar o webhook para {from_number_user}: {e}")

    return "OK", 200


@app.route("/api/disparar_lote", methods=["POST"])
def api_disparar_lote():
    data = request.json
    contatos = data.get("contatos")
    if not contatos or not isinstance(contatos, list):
        return jsonify({"status": "erro", "mensagem": "A lista de 'contatos' é inválida ou ausente."}), 400
    resultados = {"sucessos": [], "falhas": []}
    for contato in contatos:
        numero = contato.get("numero")
        nome = contato.get("nome", "Cliente")
        if not numero:
            resultados["falhas"].append({"contato": contato, "motivo": "Número ausente"})
            continue
        if not numero.startswith("whatsapp:"):
            numero = "whatsapp:" + numero
        resultado_individual = disparar_e_registrar_contato_inicial(numero, nome)
        if resultado_individual["status"] == "sucesso":
            resultados["sucessos"].append({"contato": contato, "sid": resultado_individual["sid"]})
        else:
            resultados["falhas"].append({"contato": contato, "motivo": resultado_individual["mensagem"]})
    return jsonify(resultados)

# ==================================
# 🔹 NOVO Endpoint para Envio Manual
# ==================================
@app.route("/api/enviar_mensagem_manual", methods=["POST"])
def api_enviar_mensagem_manual():
    data = request.json
    contact_id = data.get("contactId")
    text = data.get("text")

    if not contact_id or not text:
        return jsonify({"status": "erro", "mensagem": "contactId e text são obrigatórios."}), 400

    try:
        contact_response = supabase.table('WhatsAppContacts').select('phone').eq('id', contact_id).limit(1).execute()
        if not contact_response.data:
            return jsonify({"status": "erro", "mensagem": "Contato não encontrado."}), 404
        
        contact_phone = contact_response.data[0]['phone']
        message_sid = enviar_whatsapp(contact_phone, text)

        if not message_sid:
            raise Exception("Falha ao enviar mensagem pela Twilio.")

        message_data = {"contactId": contact_id, "sender": "me", "text": text, "timestamp": datetime.now().isoformat(), "messageIdFromApi": message_sid, "status": "sent"}
        message_response = supabase.table('WhatsAppMessages').insert(message_data).execute()
        
        if not message_response.data:
            print(f"⚠️ Aviso: Mensagem enviada (SID: {message_sid}), mas falhou ao salvar no DB.")

        last_message_text = f"Você: {text}"
        supabase.table('WhatsAppContacts').update({'lastMessage': last_message_text[:100], 'lastTimestamp': datetime.now().isoformat()}).eq('id', contact_id).execute()

        return jsonify({"status": "sucesso", "sentMessage": message_response.data[0] if message_response.data else {}}), 201

    except Exception as e:
        print(f"🔥 Erro no envio manual para contactId {contact_id}: {e}")
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

# ===============================
# 🔹 Iniciar servidor
# ===============================
if __name__ == "__main__":
    print("🚀 Servidor Flask (Bot WhatsApp) iniciando...")
    app.run(port=5000, debug=True, use_reloader=False)