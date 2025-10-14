# ❓ FAQ - Perguntas Frequentes

## Configuração e Setup

### Como obter uma API Key do Claude (Anthropic)?

1. Acesse https://console.anthropic.com/
2. Crie uma conta ou faça login
3. Vá em **API Keys**
4. Clique em **Create Key**
5. Copie a chave (começa com `sk-ant-`)
6. Cole no arquivo `.env` na variável `ANTHROPIC_API_KEY`

**Custo**: ~$3 por milhão de tokens de entrada. Para uma clínica pequena, deve custar menos de $10/mês.

### Como configurar a Evolution API?

**Opção 1: Serviço Hospedado (Recomendado)**
- Existem vários provedores que hospedam Evolution API
- Busque por "Evolution API hosting" ou "WhatsApp API Brasil"
- Custo: geralmente R$20-50/mês

**Opção 2: Self-hosted**
- Precisa de um servidor VPS
- Repositório: https://github.com/EvolutionAPI/evolution-api
- Requer conhecimento técnico

### O bot vai funcionar sem Google Calendar?

Sim! O bot vai funcionar normalmente, mas:
- ✅ Vai salvar consultas no banco de dados local
- ❌ Não vai sincronizar com Google Calendar
- ⚠️ Médica terá que conferir consultas no banco ou criar painel admin

### Quanto custa rodar o bot?

**Custos mensais estimados (clínica pequena):**
- Claude API: ~$5-10
- Evolution API: ~R$30-50
- Railway hosting: Grátis até $5/mês de uso
- **Total**: ~R$50-100/mês

---

## Uso e Operação

### Como o bot sabe as informações da clínica?

Todas as informações estão no arquivo `data/clinic_info.json`. Para atualizar:

1. Edite o arquivo
2. Se o bot estiver rodando, recarregue: `POST /admin/reload-config`
3. Pronto! Mudanças aplicadas sem reiniciar

### O bot responde em horário comercial apenas?

Não! O bot responde **24/7**. Mas ele informa os horários de atendimento quando necessário.

Se quiser que só responda em horário comercial, precisa adicionar essa lógica no código.

### Como o bot valida se é realmente o paciente tentando cancelar?

O bot pede:
- Nome completo
- Data de nascimento

Se os dados coincidirem com o agendamento, permite cancelar/remarcar.

### O bot pode dar diagnósticos ou orientações médicas?

**NÃO!** O bot está programado para recusar qualquer solicitação de orientação médica. Ele sempre dirá que não pode dar orientações e que a pessoa deve consultar um médico.

---

## Problemas Técnicos

### Bot não está respondendo no WhatsApp

**Checklist:**

1. ✅ **Instância Evolution conectada?**
   - Entre no painel da Evolution API
   - Verifique se status está "connected"
   - Se não, escaneie QR code novamente

2. ✅ **Webhook configurado?**
   ```bash
   # Teste se o bot está online
   curl https://seu-app.railway.app/health
   ```
   - Deve retornar `{"status":"healthy"}`
   - Se não, o bot está offline

3. ✅ **Webhook URL correta?**
   - No painel Evolution: `https://seu-app.railway.app/webhook/whatsapp`
   - Events marcados: `messages.upsert`

4. ✅ **Logs do Railway**
   - Acesse Railway → seu projeto → View Logs
   - Procure por erros

### Erro: "ANTHROPIC_API_KEY not found"

**Solução:**
1. Verifique se a variável está no Railway (ou .env local)
2. Certifique-se que não tem espaços extras
3. Reinicie o deploy no Railway

### Erro: "Google Calendar permission denied"

**Soluções:**

1. **Calendário não compartilhado**
   - Google Calendar → Settings
   - Compartilhe com o email da Service Account
   - Permissão: "Make changes to events"

2. **Calendar ID errado**
   - Google Calendar → Settings → calendar específico
   - Copie o Calendar ID (formato: `xxx@group.calendar.google.com`)
   - Cole no `.env`

3. **API não habilitada**
   - Google Cloud Console → APIs & Services
   - Busque "Google Calendar API"
   - Clique "Enable"

### Bot está respondendo devagar

**Possíveis causas:**

1. **Claude API lenta** (raro)
   - Verifique status: https://status.anthropic.com/

2. **Railway cold start**
   - Railway pode "dormir" após inatividade
   - Primeira mensagem pode demorar ~5-10s
   - Upgrade para plan pago evita isso

3. **Evolution API lenta**
   - Verifique com seu provedor

### Como ver as consultas agendadas?

**Opção 1: Script de gerenciamento**
```bash
python manage_db.py
# Escolha opção 2: Listar próximas consultas
```

**Opção 2: Diretamente no banco**
```bash
# Baixe o arquivo data/appointments.db
# Abra com DB Browser for SQLite
```

**Opção 3: Google Calendar** (se configurado)
- As consultas aparecem automaticamente no calendário

---

## Customização

### Como adicionar mais tipos de consulta?

Edite `data/clinic_info.json`:

```json
{
  "tipos_consulta": [
    {
      "tipo": "Consulta Nova",
      "duracao_minutos": 45,
      "valor_particular": 400.00,
      "convenios_aceitos": ["particular", "unimed"]
    }
  ]
}
```

Depois recarregue: `POST /admin/reload-config`

### Como mudar o horário de atendimento?

Edite `data/clinic_info.json`:

```json
{
  "horario_funcionamento": {
    "segunda": "09:00-17:00",
    "terca": "09:00-17:00",
    ...
  }
}
```

### Como mudar a antecedência mínima para agendar?

Edite `data/clinic_info.json`:

```json
{
  "regras_agendamento": {
    "dias_minimos_antecedencia": 3  // Mude para o número desejado
  }
}
```

### Como mudar o tom do bot?

Edite o system prompt em `app/ai_agent.py`, linha ~45:

```python
def _build_system_prompt(self) -> str:
    # Modifique as instruções aqui
```

---

## Segurança e Privacidade

### Onde os dados são armazenados?

- **Banco local**: `data/appointments.db` (SQLite)
- **Google Calendar**: Se configurado
- **Logs**: No Railway ou local

### Os dados são seguros?

- ✅ Conexões HTTPS
- ✅ Dados mínimos coletados (LGPD)
- ✅ Sem compartilhamento com terceiros
- ⚠️ Faça backups regulares do banco

### Como fazer backup?

```bash
python manage_db.py
# Escolha opção 3: Fazer backup
```

Ou manualmente:
```bash
cp data/appointments.db backups/backup_$(date +%Y%m%d).db
```

### Posso ver o histórico de mensagens?

Não. O bot armazena apenas:
- Contexto da conversa atual (resetado após 1h de inatividade)
- Dados de agendamentos

Mensagens completas estão nos logs (Railway ou local).

---

## Deploy e Manutenção

### Preciso reiniciar o bot quando atualizar o código?

**Railway**: Faz deploy automático ao dar push no GitHub

**Local**: Sim, pare (Ctrl+C) e rode `python run.py` novamente

### Como atualizar o bot para nova versão?

```bash
git pull origin main
pip install -r requirements.txt --upgrade
# Reinicie o servidor
```

### O que fazer se o banco corromper?

1. Restaure do backup mais recente:
   ```bash
   python manage_db.py
   # Opção 4: Restaurar backup
   ```

2. Se não tiver backup, o bot cria um novo banco vazio automaticamente

---

## Contato e Suporte

### Onde reportar bugs?

Abra uma issue no GitHub ou entre em contato com o desenvolvedor.

### Posso modificar o código?

Sim! O projeto é seu. Customize como quiser.

### Precisa de ajuda profissional?

Entre em contato com **Daniel Nobrega Medeiros** - Nobrega Medtech

