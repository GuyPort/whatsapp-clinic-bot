# ✅ Checklist para a Clínica

## 📋 Antes de Começar

### Você vai precisar de:

- [ ] Número de WhatsApp da clínica (pode ser novo ou existente)
- [ ] Computador com Windows ou Mac
- [ ] ~30 minutos do seu tempo
- [ ] Cartão de crédito para APIs (custo ~R$50-100/mês)

---

## 🔑 Passo 1: Criar Contas Necessárias

### 1.1 Claude (IA)
- [x] Acessar https://console.anthropic.com/
- [x] Criar conta
- [x] Adicionar cartão de crédito
- [x] Criar API Key
- [x] Copiar e guardar a chave (começa com `sk-ant-`)

**Custo**: ~$5-10/mês

---

### 1.2 Evolution API (WhatsApp)

**Opção A: Contratar serviço (RECOMENDADO)**
- [x] Pesquisar "Evolution API hosting Brasil"
- [x] Contratar plano (R$30-50/mês)
- [x] Anotar URL da API
- [x] Anotar API Key
- [x] Criar instância "clinica-bot"
- [x] Escanear QR Code com WhatsApp da clínica

**Opção B: Self-hosted (técnico)**
- [ ] Contratar VPS
- [ ] Instalar Docker
- [ ] Configurar Evolution API
- [ ] Escanear QR Code

**Custo**: R$30-50/mês (opção A)

---

### 1.3 Google Calendar (Opcional mas Recomendado)

- [x] Acessar https://console.cloud.google.com/
- [x] Criar projeto "Clinic Bot"
- [x] Habilitar Google Calendar API
- [x] Criar Service Account
- [x] Baixar arquivo JSON de credenciais
- [x] Criar calendário no Google Calendar
- [x] Compartilhar calendário com email da Service Account
- [x] Copiar Calendar ID

**Custo**: GRÁTIS

---

### 1.4 Railway (Hospedagem)

- [ ] Acessar https://railway.app/
- [ ] Criar conta (pode usar GitHub)
- [ ] Tem $5 de crédito grátis todo mês

**Custo**: Geralmente grátis com os créditos

---

## 💻 Passo 2: Configurar o Bot

### 2.1 Baixar o Código

- [ ] Abrir PowerShell ou Terminal
- [ ] Digitar: `git clone <link-do-repositorio>`
- [ ] Entrar na pasta: `cd whatsapp-clinic-bot`

---

### 2.2 Configurar Arquivo .env

- [x] Copiar `env.example` para `.env`
- [x] Abrir `.env` com Bloco de Notas
- [x] Preencher com suas credenciais:

```env
ANTHROPIC_API_KEY=sk-ant-COLE-SUA-CHAVE-AQUI
EVOLUTION_API_URL=https://wasenderapi.com/api
EVOLUTION_API_KEY=df9030...628b
EVOLUTION_INSTANCE_NAME=Clinic-bot
GOOGLE_CALENDAR_ID=seu-id@group.calendar.google.com
```

- [x] Salvar arquivo

---

### 2.3 Adicionar Credenciais do Google

- [ ] Renomear o arquivo JSON baixado para `google-credentials.json`
- [ ] Colocar na pasta raiz do projeto
- [ ] ⚠️ NÃO compartilhar este arquivo!

---

### 2.4 Editar Informações da Clínica

- [ ] Abrir `data/clinic_info.json`
- [ ] Preencher com dados reais:
  - Nome da clínica
  - Endereço
  - Telefone de contato
  - Horários de funcionamento
  - Valores das consultas
  - Tipos de consulta

- [ ] Salvar arquivo

---

## 🚀 Passo 3: Fazer Deploy

### Opção A: Railway (Mais Fácil)

- [ ] Fazer upload do código para GitHub (ou usar GitHub Desktop)
- [ ] Acessar https://railway.app/
- [ ] Clicar em "New Project"
- [ ] Escolher "Deploy from GitHub repo"
- [ ] Selecionar seu repositório
- [ ] Ir em "Variables"
- [ ] Adicionar TODAS as variáveis do .env
- [ ] Esperar deploy finalizar
- [ ] Copiar URL do app (ex: `https://seu-app.up.railway.app`)

---

### Opção B: Computador da Clínica (Sempre Ligado)

- [ ] Instalar Python 3.11
- [ ] Abrir pasta do projeto
- [ ] Duplo clique em `quickstart.bat`
- [ ] Esperar instalar tudo
- [ ] Bot estará rodando!

⚠️ **Atenção**: Computador deve ficar ligado 24/7

---

## 🔗 Passo 4: Conectar WhatsApp

- [ ] Acessar painel da Evolution API
- [ ] Ir em "Webhooks"
- [ ] Configurar webhook:
  - **URL**: `https://seu-app.railway.app/webhook/whatsapp`
  - **Events**: Marcar `messages.upsert`
- [ ] Salvar

- [ ] Verificar se instância está "connected" (verde)
- [ ] Se não, escanear QR code novamente

---

## ✅ Passo 5: Testar!

### Teste 1: Health Check
- [ ] Abrir navegador
- [ ] Acessar: `https://seu-app.railway.app/health`
- [ ] Deve aparecer: `{"status":"healthy"}`

### Teste 2: WhatsApp
- [ ] Pegar outro celular
- [ ] Enviar mensagem para o WhatsApp da clínica: "Olá"
- [ ] Bot deve responder!

### Teste 3: Agendamento Completo
- [ ] Perguntar: "Quero agendar uma consulta"
- [ ] Seguir o fluxo completo
- [ ] Verificar se apareceu no Google Calendar

---

## 📊 Passo 6: Monitoramento

### Diariamente:
- [ ] Ver consultas no Google Calendar
- [ ] Responder pacientes escalados para humano

### Semanalmente:
- [ ] Verificar logs no Railway
- [ ] Rodar `python monitor.py` para ver status

### Mensalmente:
- [ ] Fazer backup: `python manage_db.py` → opção 3
- [ ] Ver estatísticas: `python manage_db.py` → opção 1
- [ ] Verificar custos das APIs

---

## 🔧 Manutenção

### Atualizar Informações da Clínica

1. [ ] Editar `data/clinic_info.json`
2. [ ] Se no Railway: fazer commit e push (deploy automático)
3. [ ] Ou chamar: `POST /admin/reload-config`

### Mudar Valores de Consulta

1. [ ] Abrir `data/clinic_info.json`
2. [ ] Encontrar `tipos_consulta`
3. [ ] Mudar `valor_particular`
4. [ ] Salvar
5. [ ] Recarregar config

### Mudar Horários

1. [ ] Abrir `data/clinic_info.json`
2. [ ] Encontrar `horario_funcionamento`
3. [ ] Editar horários
4. [ ] Salvar
5. [ ] Recarregar config

---

## ❓ Problemas Comuns

### Bot não responde
1. [ ] Verificar se Railway está online
2. [ ] Verificar logs no Railway
3. [ ] Verificar se Evolution API está conectada
4. [ ] Verificar webhook configurado corretamente

### WhatsApp desconectou
1. [ ] Entrar no painel Evolution API
2. [ ] Escanear QR code novamente
3. [ ] Pronto!

### Erro no Google Calendar
1. [ ] Verificar se compartilhou calendário
2. [ ] Verificar Calendar ID no .env
3. [ ] Verificar se API está habilitada

---

## 📞 Contatos Importantes

### Suporte Técnico
- Email: (seu email de suporte)
- Telefone: (seu telefone)

### Documentação
- README.md - Documentação geral
- SETUP_GUIDE.md - Setup detalhado
- FAQ.md - Perguntas frequentes
- EXAMPLES.md - Exemplos de uso

---

## 💡 Dicas

✅ **Faça backup** do banco semanalmente  
✅ **Monitore os logs** regularmente  
✅ **Teste mudanças** antes de aplicar  
✅ **Mantenha .env seguro** (não compartilhe)  
✅ **Use número dedicado** para o bot  
✅ **Responda escalações** rapidamente  

---

## 🎉 Pronto!

Se você chegou até aqui e tudo funciona:

✅ Bot está respondendo no WhatsApp  
✅ Consegue agendar consultas  
✅ Sincroniza com Google Calendar  
✅ Consegue cancelar/remarcar  

**Parabéns! Seu bot está no ar!** 🚀

---

## 📈 Próximos Passos (Opcional)

- [ ] Divulgar número do WhatsApp para pacientes
- [ ] Criar mensagens automáticas de boas-vindas
- [ ] Adicionar mais tipos de consulta
- [ ] Configurar lembretes de consulta
- [ ] Implementar painel administrativo

---

**Dúvidas? Consulte FAQ.md ou entre em contato com suporte!**

