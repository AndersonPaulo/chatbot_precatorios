import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from twilio.rest import Client
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# ===============================
# 🔹 Banco de dados
# ===============================
DB_PATH = "clientes.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS clientes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero TEXT UNIQUE,
            nome TEXT,
            status TEXT,
            termo_enviado_em TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===============================
# 🔹 Twilio Config
# ===============================
account_sid = os.getenv("TWILIO_ACCOUNT_SID")
auth_token = os.getenv("TWILIO_AUTH_TOKEN")
from_number = os.getenv("TWILIO_WHATSAPP_NUMBER")
template_sid = os.getenv("TWILIO_TEMPLATE_SID")
client = Client(account_sid, auth_token)

# Intervalo do .env
REMINDER_INTERVAL = int(os.getenv("REMINDER_INTERVAL", "64800"))

# ===============================
# 🔹 Utilitário para enviar mensagens
# ===============================
def enviar_whatsapp(to, body):
    msg = client.messages.create(
        from_=from_number,
        to=to,
        body=body
    )
    print(f"✅ Twilio OK: {msg.sid}")
    return msg.sid

# ===============================
# 🔹 Rota para servir o termo
# ===============================
@app.route("/static/termo")
def termo():
    # Corrigido para o nome certo do arquivo
    return send_file("Termo_cedente.docx", as_attachment=True)

# ===============================
# 🔹 Endpoint para disparar mensagem inicial (via template Twilio)
# ===============================
@app.route("/api/disparar_template", methods=["POST"])
def disparar_template():
    data = request.json
    to_number = data.get("numero")
    nome = data.get("nome", "Cliente")

    if not to_number:
        return jsonify({"status": "erro", "mensagem": "Número de telefone ausente."}), 400

    # 🛠️ Correção: Garante que o número seja salvo com o prefixo
    if not to_number.startswith("whatsapp:"):
        to_number_db = "whatsapp:" + to_number
    else:
        to_number_db = to_number

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO clientes (numero, nome, status) VALUES (?, ?, ?)",
              (to_number_db, nome, "inicial"))
    conn.commit()
    conn.close()

    # Dispara TEMPLATE cadastrado no Twilio
    try:
        msg = client.messages.create(
            from_=from_number,
            to=to_number_db, # 🛠️ Correção: Usa o número já formatado para o envio
            content_sid=template_sid,
            content_variables=f'{{"1":"{nome}"}}'
        )
        print(f"✅ Template disparado: {msg.sid}")
        return jsonify({"status": "sucesso", "sid": msg.sid})
    except Exception as e:
        print(f"❌ Erro ao enviar template: {e}")
        return jsonify({"status": "erro", "mensagem": str(e)}), 500

# ===============================
# 🔹 Webhook (respostas do cliente)
# ===============================
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.form.to_dict()
    print(f"📩 Webhook recebido: {data}")

    # 🛠️ Correção: Agora o número do webhook já está no formato correto para a busca
    from_number_user = data.get("From")
    profile_name = data.get("ProfileName", "Cliente")
    body = data.get("Body", "").strip().lower()

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    # 🛠️ Correção: A query agora busca o número com o prefixo
    c.execute("SELECT status FROM clientes WHERE numero = ?", (from_number_user,))
    row = c.fetchone()
    
    if not row:
        print(f"⚠️ Aviso: Cliente com número {from_number_user} não encontrado no banco de dados.")
        return "OK", 200

    status = row[0]

    # ... O restante do seu fluxo de código (que já estava correto) ...
    if status == "inicial":
        if any(x in body for x in ["sim", "s", "yes"]):
            enviar_whatsapp(
                from_number_user,
                f"Excelente, {profile_name}! 🙌\n\n"
                "Segue em anexo o *Termo de Representação*.\n\n"
                f"📄 Acesse aqui: {os.getenv('TERMO_URL')}\n\n"
                "👉 Após preencher e assinar, envie a mensagem: *termo enviado*"
            )
            c.execute("UPDATE clientes SET status = ? WHERE numero = ?", ("aguardando_termo", from_number_user))

        elif any(x in body for x in ["não", "nao", "n", "no"]):
            enviar_whatsapp(
                from_number_user,
                f"Entendido, {profile_name}! 👌\n\n"
                "👉 Aceita receber propostas futuras? (SIM/NÃO)"
            )
            c.execute("UPDATE clientes SET status = ? WHERE numero = ?", ("oferta_futura", from_number_user))
            
    # ... os outros fluxos permanecem iguais ...
    
    conn.commit()
    conn.close()
    return "OK", 200

# ===============================
# 🔹 Tarefa agendada: Follow-up
# ===============================
def follow_up_job():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT numero, nome, termo_enviado_em FROM clientes WHERE status = 'pos_termo' AND termo_enviado_em IS NOT NULL")
    clientes = c.fetchall()

    for numero, nome, termo_enviado_em in clientes:
        dt_envio = datetime.strptime(termo_enviado_em, "%Y-%m-%d %H:%M:%S")
        if datetime.now() >= dt_envio + timedelta(minutes=REMINDER_INTERVAL):
            # 🛠️ Correção: O número do banco já tem o prefixo
            enviar_whatsapp(
                numero,
                f"Olá {nome}, 👋\n\n"
                "Estamos acompanhando sua oportunidade e queremos reforçar que seguimos em busca "
                "da melhor proposta para seu precatório.\n\n"
                "👉 Deseja continuar recebendo atualizações? (SIM/NÃO)"
            )
            c.execute("UPDATE clientes SET status = ? WHERE numero = ?", ("followup_enviado", numero))

    conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(follow_up_job, "interval", minutes=1)
scheduler.start()

# ===============================
# 🔹 Iniciar servidor Flask
# ===============================
if __name__ == "__main__":
    app.run(port=5000, debug=True)