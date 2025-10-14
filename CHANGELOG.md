# Changelog

Todas as mudanças notáveis neste projeto serão documentadas neste arquivo.

## [1.0.0] - 2025-10-13

### ✨ Funcionalidades Iniciais

- **Agente IA conversacional** usando Claude 3.5 Sonnet
- **Integração WhatsApp** via Evolution API
- **Agendamento automático** de consultas
- **Integração Google Calendar** para sincronização
- **Cancelamento e remarcação** de consultas com validação de identidade
- **Regras de agendamento** configuráveis:
  - Mínimo de dias de antecedência
  - Bloqueio de domingos, sábados tarde e madrugadas
  - Intervalo entre consultas
  - Validação de horário de funcionamento
- **Escalação inteligente** para atendimento humano
- **Banco de dados SQLite** para armazenar pacientes e consultas
- **Sistema de estados** para gerenciar fluxo conversacional
- **Detecção de frustração** e linguagem inadequada
- **Configuração editável** via JSON
- **Deploy fácil** no Railway

### 📝 Documentação

- README.md completo
- SETUP_GUIDE.md com passo a passo
- Scripts de teste incluídos
- Exemplos de uso e fluxos conversacionais

### 🛠️ Infraestrutura

- FastAPI com webhooks
- SQLAlchemy ORM
- Logging completo
- Health checks
- Reload de configuração sem restart

### 🔒 Segurança

- Validação de identidade para modificações
- Proteção contra orientação médica
- Logs de auditoria
- Dados mínimos (LGPD)

---

## Próximas Features Planejadas

### 🔜 v1.1.0
- [ ] Painel web administrativo
- [ ] Estatísticas e métricas
- [ ] Envio de lembretes de consulta
- [ ] Suporte a múltiplas clínicas
- [ ] Rate limiting

### 🔜 v1.2.0
- [ ] Pagamento online
- [ ] Notificações push
- [ ] Fila de espera
- [ ] Histórico de consultas
- [ ] Feedback pós-consulta

### 🔜 v2.0.0
- [ ] Multi-idioma
- [ ] Integração com prontuário eletrônico
- [ ] IA melhorada com histórico médico
- [ ] App mobile para médicos

