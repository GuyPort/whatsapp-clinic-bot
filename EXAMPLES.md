# 📚 Exemplos Práticos de Uso

Este documento contém exemplos práticos de como usar e interagir com o bot.

---

## 🤖 Exemplos de Conversas

### 1. Agendamento Completo - Fluxo Feliz

```
👤 Paciente: Olá

🤖 Bot: Olá! Bem-vindo à Clínica Exemplo. Como posso ajudar?
       1 - Marcar consulta
       2 - Remarcar/Cancelar consulta  
       3 - Dúvidas

👤 Paciente: 1

🤖 Bot: Perfeito! Vou te ajudar a marcar uma consulta.
        Primeiro, qual é seu nome completo?

👤 Paciente: Maria Silva Santos

🤖 Bot: Obrigado, Maria! Agora preciso da sua data de nascimento (formato DD/MM/AAAA).

👤 Paciente: 15/03/1990

🤖 Bot: Perfeito! Que tipo de consulta você deseja agendar?

👤 Paciente: Dermatológica

🤖 Bot: Ótimo! Consulta Dermatológica - R$ 350,00
        Que dia seria melhor para você?

👤 Paciente: Quinta-feira

🤖 Bot: Horários disponíveis:
        
        1. 09:00
        2. 10:30
        3. 14:00
        
        Por favor, escolha o número do horário desejado.

👤 Paciente: 2

🤖 Bot: Perfeito! Vou agendar sua Consulta Dermatológica para 24/10/2025 às 10:30.
        
        Confirma o agendamento? (Sim/Não)

👤 Paciente: Sim

🤖 Bot: ✅ Consulta agendada com sucesso!
        
        📅 Data: 24/10/2025 às 10:30
        ⏱️ Duração: 30 minutos
        💰 Valor: R$ 350,00
        📍 Endereço: Rua Exemplo, 123 - Centro
        
        Lembramos que cancelamentos devem ser feitos com 24h de antecedência.
        Até lá! 😊
```

---

### 2. Consulta de Informações

```
👤 Paciente: Qual o horário de funcionamento?

🤖 Bot: Nosso horário de atendimento é:
        Segunda a Sexta: 08:00 às 18:00
        Sábado: 08:00 às 12:00
        Domingo: Fechado

👤 Paciente: Onde fica a clínica?

🤖 Bot: Estamos localizados na Rua Exemplo, 123 - Centro - Cidade/UF - CEP 12345-678
        Temos estacionamento gratuito no local e a clínica é acessível para cadeirantes.

👤 Paciente: Quais formas de pagamento vocês aceitam?

🤖 Bot: Aceitamos:
        • Dinheiro
        • Cartão de crédito
        • Cartão de débito
        • Pix
```

---

### 3. Cancelamento de Consulta

```
👤 Paciente: Preciso cancelar minha consulta

🤖 Bot: Para isso, preciso confirmar sua identidade. Qual é seu nome completo?

👤 Paciente: Maria Silva Santos

🤖 Bot: E sua data de nascimento (DD/MM/AAAA)?

👤 Paciente: 15/03/1990

🤖 Bot: Suas consultas agendadas:
        
        1. Consulta Dermatológica - 24/10/2025 às 10:30
        
        Qual consulta você deseja cancelar? (responda com o número)

👤 Paciente: 1

🤖 Bot: ✅ Consulta de 24/10/2025 às 10:30 cancelada com sucesso.
```

---

### 4. Remarcação de Consulta

```
👤 Paciente: Quero remarcar minha consulta

🤖 Bot: Para isso, preciso confirmar sua identidade. Qual é seu nome completo?

👤 Paciente: João Pedro Costa

🤖 Bot: E sua data de nascimento (DD/MM/AAAA)?

👤 Paciente: 20/05/1985

🤖 Bot: Suas consultas agendadas:
        
        1. Consulta Dermatológica - 25/10/2025 às 14:00
        
        Qual consulta você deseja remarcar? (responda com o número)

👤 Paciente: 1

🤖 Bot: Para qual dia você gostaria de remarcar?

👤 Paciente: Sexta-feira

🤖 Bot: Horários disponíveis:
        
        1. 09:00
        2. 11:00
        3. 15:00
        
        Por favor, escolha o número do horário desejado.

👤 Paciente: 3

🤖 Bot: ✅ Consulta remarcada com sucesso para 26/10/2025 às 15:00!
```

---

### 5. Escalação para Humano

```
👤 Paciente: Quero falar com um atendente

🤖 Bot: Entendo! Vou transferir você para nossa equipe de atendimento.
        
        📞 Telefone: (11) 91234-5678
        
        Horário de atendimento:
        Seg-Sex: 08:00-18:00
        Sábado: 08:00-12:00
```

---

### 6. Solicitação Inadequada (Orientação Médica)

```
👤 Paciente: Estou com uma mancha na pele, o que pode ser?

🤖 Bot: Desculpe, mas não posso fornecer diagnósticos ou orientações médicas.
        Para uma avaliação adequada, é necessário agendar uma consulta com a
        dermatologista. Gostaria de agendar?

👤 Paciente: Que remédio posso tomar para acne?

🤖 Bot: Não posso recomendar medicamentos. Apenas um médico pode prescrever
        tratamentos adequados para seu caso. Posso ajudá-lo a agendar uma
        consulta dermatológica?
```

---

## 💻 Exemplos de API

### Health Check

```bash
curl https://seu-app.railway.app/health
```

**Resposta:**
```json
{
  "status": "healthy",
  "service": "whatsapp-clinic-bot",
  "version": "1.0.0"
}
```

---

### Status Detalhado

```bash
curl https://seu-app.railway.app/status
```

**Resposta:**
```json
{
  "status": "operational",
  "whatsapp": {
    "state": "open",
    "instance": "clinica-bot"
  },
  "google_calendar": "available",
  "database": "connected"
}
```

---

### Recarregar Configurações

```bash
curl -X POST https://seu-app.railway.app/admin/reload-config
```

**Resposta:**
```json
{
  "status": "success",
  "message": "Configurações recarregadas"
}
```

---

## 🐍 Exemplos de Scripts Python

### Consultar Pacientes no Banco

```python
import sys
sys.path.insert(0, '.')

from app.database import get_db
from app.models import Patient

with get_db() as db:
    patients = db.query(Patient).all()
    
    for patient in patients:
        print(f"{patient.name} - {patient.phone}")
        print(f"  Consultas: {len(patient.appointments)}")
```

---

### Buscar Consultas de Hoje

```python
import sys
from datetime import datetime, timedelta
sys.path.insert(0, '.')

from app.database import get_db
from app.models import Appointment, AppointmentStatus
from app.utils import now_brazil

with get_db() as db:
    today = now_brazil().date()
    tomorrow = today + timedelta(days=1)
    
    appointments = db.query(Appointment).filter(
        Appointment.appointment_date >= datetime.combine(today, datetime.min.time()),
        Appointment.appointment_date < datetime.combine(tomorrow, datetime.min.time()),
        Appointment.status == AppointmentStatus.SCHEDULED
    ).all()
    
    print(f"Consultas de hoje: {len(appointments)}")
    
    for apt in appointments:
        print(f"\n{apt.appointment_date.strftime('%H:%M')} - {apt.patient.name}")
        print(f"  Tipo: {apt.consultation_type}")
        print(f"  Telefone: {apt.patient.phone}")
```

---

### Criar Backup Programático

```python
import sys
import shutil
from datetime import datetime
sys.path.insert(0, '.')

# Criar backup
db_path = "data/appointments.db"
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
backup_path = f"data/backups/appointments_backup_{timestamp}.db"

shutil.copy2(db_path, backup_path)
print(f"✅ Backup criado: {backup_path}")
```

---

### Estatísticas Customizadas

```python
import sys
from datetime import timedelta
sys.path.insert(0, '.')

from app.database import get_db
from app.models import Patient, Appointment, AppointmentStatus
from app.utils import now_brazil

with get_db() as db:
    # Pacientes cadastrados
    total_patients = db.query(Patient).count()
    
    # Consultas por status
    scheduled = db.query(Appointment).filter(
        Appointment.status == AppointmentStatus.SCHEDULED
    ).count()
    
    completed = db.query(Appointment).filter(
        Appointment.status == AppointmentStatus.COMPLETED
    ).count()
    
    cancelled = db.query(Appointment).filter(
        Appointment.status == AppointmentStatus.CANCELLED
    ).count()
    
    # Taxa de cancelamento
    total = scheduled + completed + cancelled
    cancel_rate = (cancelled / total * 100) if total > 0 else 0
    
    print(f"""
📊 ESTATÍSTICAS

👥 Pacientes: {total_patients}

📅 Consultas:
   Agendadas: {scheduled}
   Realizadas: {completed}
   Canceladas: {cancelled}
   Total: {total}

📈 Taxa de Cancelamento: {cancel_rate:.1f}%
    """)
```

---

## 🧪 Testes

### Testar Bot Localmente (Sem WhatsApp)

```bash
python test_bot.py
```

Selecione opção 1 para simular uma conversa completa.

---

### Testar Integração com Google Calendar

```python
import sys
from datetime import datetime, timedelta
sys.path.insert(0, '.')

from app.calendar_service import calendar_service
from app.utils import now_brazil

# Testar criação de evento
start = now_brazil() + timedelta(days=3, hours=2)

event_id = calendar_service.create_event(
    title="Teste - Consulta",
    start_datetime=start,
    duration_minutes=30,
    description="Evento de teste"
)

if event_id:
    print(f"✅ Evento criado: {event_id}")
    
    # Deletar após teste
    calendar_service.delete_event(event_id)
    print("✅ Evento deletado")
else:
    print("❌ Erro ao criar evento")
```

---

### Testar WhatsApp (Enviar Mensagem)

```python
import sys
import asyncio
sys.path.insert(0, '.')

from app.whatsapp_service import whatsapp_service

async def test():
    # Substitua pelo seu número de teste
    test_phone = "5511999999999"
    
    success = await whatsapp_service.send_message(
        test_phone,
        "🤖 Teste do bot! Esta é uma mensagem de teste."
    )
    
    if success:
        print("✅ Mensagem enviada!")
    else:
        print("❌ Erro ao enviar mensagem")

asyncio.run(test())
```

---

## 📝 Editar Configurações

### Adicionar Novo Tipo de Consulta

Edite `data/clinic_info.json`:

```json
{
  "tipos_consulta": [
    {
      "tipo": "Limpeza de Pele",
      "duracao_minutos": 60,
      "valor_particular": 200.00,
      "convenios_aceitos": ["particular"]
    }
  ]
}
```

Recarregue sem reiniciar:
```bash
curl -X POST http://localhost:8000/admin/reload-config
```

---

### Mudar Dias de Antecedência

```json
{
  "regras_agendamento": {
    "dias_minimos_antecedencia": 3
  }
}
```

---

## 🚨 Troubleshooting

### Ver Logs em Tempo Real (Railway)

```bash
# Instale Railway CLI
npm install -g @railway/cli

# Login
railway login

# Ver logs
railway logs
```

---

### Resetar Conversa de um Paciente

```python
import sys
sys.path.insert(0, '.')

from app.database import get_db
from app.models import ConversationContext, ConversationState

phone = "5511999999999"  # Telefone do paciente

with get_db() as db:
    context = db.query(ConversationContext).filter(
        ConversationContext.phone == phone
    ).first()
    
    if context:
        context.state = ConversationState.IDLE
        context.context_data = "{}"
        db.commit()
        print("✅ Conversa resetada")
    else:
        print("❌ Conversa não encontrada")
```

---

Esses exemplos cobrem os casos de uso mais comuns. Para mais informações, veja a documentação completa no README.md.

