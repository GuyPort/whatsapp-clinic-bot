# 🎛️ Dashboard de Consultas - Guia Completo

## 📋 Visão Geral

O Dashboard é uma interface web simples e elegante para visualizar todas as consultas agendadas pelo bot do WhatsApp. É atualizado manualmente com um clique no botão "Atualizar Consultas".

## 🚀 Como Acessar

### **URL do Dashboard**
```
http://localhost:8000/dashboard
```

### **Acesso via Página Principal**
1. Acesse `http://localhost:8000`
2. Clique no botão **"Abrir Dashboard"**

## 📊 Funcionalidades

### **Estatísticas em Tempo Real**
- ✅ **Consultas Agendadas**: Total de consultas com status "scheduled"
- ✅ **Total de Pacientes**: Número total de pacientes cadastrados
- ✅ **Consultas Hoje**: Consultas agendadas para hoje
- ✅ **Esta Semana**: Consultas agendadas para esta semana

### **Lista de Consultas**
- ✅ **Ordenação**: Por data e horário (mais próximas primeiro)
- ✅ **Informações Completas**: Nome, telefone, data de nascimento
- ✅ **Status Visual**: Badges coloridos para cada status
- ✅ **Data de Agendamento**: Quando a consulta foi criada

### **Atualização Manual**
- ✅ **Botão Atualizar**: Clique para buscar dados mais recentes
- ✅ **Timestamp**: Mostra quando foi a última atualização
- ✅ **Loading**: Indicador visual durante carregamento

## 🎨 Interface

### **Design Responsivo**
- ✅ **Bootstrap 5**: Framework CSS moderno
- ✅ **Mobile-Friendly**: Funciona em celulares e tablets
- ✅ **Gradiente Azul**: Visual profissional e moderno
- ✅ **Font Awesome**: Ícones elegantes

### **Cards de Consulta**
```
┌─────────────────────────────────────────────────────────┐
│ 14:30                    │ João Silva                  │
│ ter, 25/10/2025         │ 📞 (11) 99999-9999          │
│                         │ 🎂 15/03/1990               │
│                         │                             │
│                         │ [Agendada]                  │
│                         │ Agendado em: 25/10/2025...  │
└─────────────────────────────────────────────────────────┘
```

## 🔧 Como Funciona

### **Fluxo de Dados**
```
1. Paciente agenda via WhatsApp
2. Bot salva no SQLite
3. Dashboard busca dados via API
4. Interface atualiza automaticamente
```

### **API Endpoint**
```http
GET /api/appointments/scheduled
```

**Resposta:**
```json
{
  "stats": {
    "scheduled": 15,
    "total_patients": 127,
    "today": 3,
    "this_week": 12
  },
  "appointments": [
    {
      "id": 1,
      "patient_name": "João Silva",
      "patient_phone": "5511999999999",
      "patient_birth_date": "15/03/1990",
      "appointment_date": "2025-01-25",
      "appointment_time": "14:30:00",
      "status": "scheduled",
      "notes": null,
      "created_at": "2025-01-15T10:30:00"
    }
  ]
}
```

## 📱 Status das Consultas

### **Badges Coloridos**
- 🟢 **Agendada** (`scheduled`): Verde
- 🔵 **Realizada** (`completed`): Azul
- 🔴 **Cancelada** (`cancelled`): Vermelho
- 🟡 **Não Compareceu** (`no_show`): Amarelo

## 🛠️ Recursos Técnicos

### **Tecnologias Utilizadas**
- ✅ **FastAPI**: Backend Python
- ✅ **Bootstrap 5**: CSS Framework
- ✅ **Font Awesome**: Ícones
- ✅ **JavaScript Vanilla**: Sem dependências externas
- ✅ **SQLite**: Banco de dados

### **Performance**
- ✅ **Carregamento Rápido**: HTML inline (sem arquivos externos)
- ✅ **Cache do Navegador**: CSS/JS via CDN
- ✅ **Queries Otimizadas**: Busca apenas consultas agendadas
- ✅ **Responsivo**: Carrega rápido em mobile

## 📋 Casos de Uso

### **Para a Clínica**
1. **Ver consultas do dia**: Quantas consultas tem hoje?
2. **Planejamento**: Quantas consultas esta semana?
3. **Contato com pacientes**: Telefone e dados completos
4. **Histórico**: Quando cada consulta foi agendada

### **Para o Desenvolvedor**
1. **Monitoramento**: Ver se o bot está funcionando
2. **Debugging**: Verificar dados salvos no banco
3. **Estatísticas**: Quantos pacientes foram cadastrados
4. **Backup visual**: Interface para verificar dados

## 🔒 Segurança

### **Acesso Público**
- ⚠️ **Sem Login**: Dashboard é público (pode ser protegido)
- ✅ **Apenas Leitura**: Não permite editar dados
- ✅ **Dados Sensíveis**: Mostra apenas informações necessárias

### **Para Produção (Recomendado)**
```python
# Adicionar autenticação básica
@app.get("/dashboard")
async def dashboard(request: Request):
    # Verificar token ou sessão
    if not verify_auth(request):
        raise HTTPException(401, "Acesso negado")
    # ... resto do código
```

## 🚀 Deploy

### **Local**
```bash
# Rodar servidor
python run.py

# Acessar dashboard
http://localhost:8000/dashboard
```

### **Railway/Produção**
```bash
# Deploy automático
# Dashboard disponível em:
https://seu-app.up.railway.app/dashboard
```

## 🐛 Troubleshooting

### **Dashboard não carrega**
1. Verificar se servidor está rodando
2. Verificar URL: `/dashboard` (não `/admin/dashboard`)
3. Verificar logs do servidor

### **Dados não aparecem**
1. Clicar em "Atualizar Consultas"
2. Verificar se há consultas no banco
3. Verificar console do navegador (F12)

### **Erro de API**
1. Verificar endpoint: `/api/appointments/scheduled`
2. Verificar logs do servidor
3. Testar endpoint diretamente no navegador

## 📈 Melhorias Futuras

### **Funcionalidades Adicionais**
- 🔄 **Auto-refresh**: Atualização automática a cada 30s
- 🔍 **Busca**: Filtrar por nome, data, telefone
- 📅 **Calendário**: Visualização em formato calendário
- 📊 **Gráficos**: Estatísticas visuais
- ✏️ **Edição**: Permitir editar consultas
- 📤 **Exportação**: Exportar para Excel/PDF

### **Segurança**
- 🔐 **Login**: Autenticação com usuário/senha
- 🔑 **API Keys**: Proteção da API
- 📝 **Logs**: Auditoria de acessos

## 💡 Dicas de Uso

### **Para Uso Diário**
1. **Abrir dashboard** pela manhã
2. **Atualizar** para ver consultas do dia
3. **Verificar** dados dos pacientes
4. **Fechar** quando não precisar

### **Para Monitoramento**
1. **Verificar** se bot está funcionando
2. **Contar** quantas consultas foram agendadas
3. **Identificar** problemas rapidamente
4. **Fazer backup** visual dos dados

---

## 🎯 Resumo

O Dashboard é uma ferramenta **simples, eficiente e elegante** para visualizar consultas agendadas. Perfeito para clínicas que querem um controle visual rápido e fácil de usar.

**Acesso:** `http://localhost:8000/dashboard`  
**Atualização:** Manual (botão "Atualizar Consultas")  
**Dados:** Tempo real do SQLite  
**Design:** Responsivo e profissional  

**Ideal para uso diário na clínica!** 🏥✨
