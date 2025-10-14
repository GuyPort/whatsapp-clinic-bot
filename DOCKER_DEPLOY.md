# 🐳 Deploy com Docker

Este guia mostra como fazer deploy do bot usando Docker e Docker Compose.

## Por que usar Docker?

✅ **Isolamento**: O bot roda em um container isolado  
✅ **Portabilidade**: Funciona igual em qualquer servidor  
✅ **Fácil deploy**: Um comando para subir tudo  
✅ **Fácil atualizar**: Rebuild e restart rápidos  

---

## 📋 Pré-requisitos

- Docker instalado (https://docs.docker.com/get-docker/)
- Docker Compose instalado (geralmente vem com Docker Desktop)
- Arquivo `.env` configurado
- Arquivo `google-credentials.json` (se usar Google Calendar)

---

## 🚀 Deploy Local com Docker

### 1. Preparar Ambiente

```bash
# Clonar repositório
git clone <seu-repo>
cd whatsapp-clinic-bot

# Copiar e configurar .env
cp env.example .env
# Edite o .env com suas credenciais

# Adicionar google-credentials.json
# (baixe do Google Cloud e coloque na raiz)
```

### 2. Build e Run

```bash
# Build da imagem
docker-compose build

# Rodar container
docker-compose up -d

# Ver logs
docker-compose logs -f
```

### 3. Verificar

```bash
# Verificar se está rodando
docker-compose ps

# Testar health check
curl http://localhost:8000/health
```

**Resultado esperado:**
```json
{"status":"healthy","service":"whatsapp-clinic-bot","version":"1.0.0"}
```

---

## 🌐 Deploy em Servidor VPS

### Opção 1: Docker Simples

```bash
# Conectar ao servidor via SSH
ssh usuario@seu-servidor.com

# Instalar Docker (Ubuntu/Debian)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# Instalar Docker Compose
sudo apt-get install docker-compose-plugin

# Clonar repositório
git clone <seu-repo>
cd whatsapp-clinic-bot

# Configurar .env e google-credentials.json

# Rodar
docker-compose up -d

# Verificar
docker-compose logs -f
```

### Opção 2: Docker com Nginx (Recomendado)

#### 2.1 Criar docker-compose-prod.yml

```yaml
version: '3.8'

services:
  clinic-bot:
    build: .
    container_name: whatsapp-clinic-bot
    restart: unless-stopped
    expose:
      - "8000"
    volumes:
      - ./data:/app/data
      - ./google-credentials.json:/app/google-credentials.json:ro
    env_file:
      - .env
    networks:
      - bot-network

  nginx:
    image: nginx:alpine
    container_name: nginx-proxy
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./ssl:/etc/nginx/ssl:ro
    depends_on:
      - clinic-bot
    networks:
      - bot-network

networks:
  bot-network:
    driver: bridge
```

#### 2.2 Criar nginx.conf

```nginx
events {
    worker_connections 1024;
}

http {
    upstream clinic-bot {
        server clinic-bot:8000;
    }

    server {
        listen 80;
        server_name seu-dominio.com;

        location / {
            proxy_pass http://clinic-bot;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

#### 2.3 Deploy

```bash
docker-compose -f docker-compose-prod.yml up -d
```

---

## 🔧 Comandos Úteis

### Gerenciamento de Containers

```bash
# Parar containers
docker-compose stop

# Iniciar containers
docker-compose start

# Reiniciar containers
docker-compose restart

# Parar e remover containers
docker-compose down

# Ver logs
docker-compose logs -f

# Ver logs de serviço específico
docker-compose logs -f clinic-bot

# Executar comando dentro do container
docker-compose exec clinic-bot bash

# Ver status
docker-compose ps
```

### Atualização

```bash
# Parar containers
docker-compose down

# Atualizar código
git pull

# Rebuild imagem
docker-compose build --no-cache

# Subir novamente
docker-compose up -d

# Ver se atualizou
docker-compose logs -f
```

### Backup

```bash
# Backup do banco de dados
docker-compose exec clinic-bot cp /app/data/appointments.db /app/data/appointments.db.backup

# Copiar backup para host
docker cp whatsapp-clinic-bot:/app/data/appointments.db.backup ./backup/
```

### Monitoramento

```bash
# Ver uso de recursos
docker stats whatsapp-clinic-bot

# Ver logs em tempo real
docker-compose logs -f --tail=100

# Health check manual
docker-compose exec clinic-bot curl http://localhost:8000/health
```

---

## 🐛 Troubleshooting

### Container não inicia

```bash
# Ver logs detalhados
docker-compose logs clinic-bot

# Ver eventos do Docker
docker events

# Verificar se porta está em uso
sudo netstat -tulpn | grep 8000
```

### Erro de permissão no banco de dados

```bash
# Dar permissão na pasta data
sudo chown -R 1000:1000 data/

# Ou
sudo chmod 777 data/
```

### Container reiniciando constantemente

```bash
# Ver logs
docker-compose logs clinic-bot

# Verificar configurações
docker-compose exec clinic-bot env

# Verificar se .env está correto
cat .env
```

### Não consegue acessar via browser

```bash
# Verificar se porta está mapeada corretamente
docker-compose ps

# Verificar firewall
sudo ufw status
sudo ufw allow 8000
```

---

## 📊 Monitoramento com Docker Stats

```bash
# Ver uso em tempo real
docker stats whatsapp-clinic-bot

# Ou todos os containers
docker stats
```

**Saída:**
```
CONTAINER ID   NAME                    CPU %     MEM USAGE / LIMIT     MEM %
abc123def456   whatsapp-clinic-bot     0.50%     120MiB / 2GiB         6.00%
```

---

## 🔄 Auto-restart

O bot está configurado com `restart: unless-stopped`, ou seja:

✅ Reinicia automaticamente se crashar  
✅ Reinicia após reboot do servidor  
❌ Só não reinicia se você parar manualmente  

---

## 🌍 Expor para Internet (Ngrok - Desenvolvimento)

Para testes rápidos, use Ngrok:

```bash
# Instalar ngrok
# https://ngrok.com/download

# Expor porta 8000
ngrok http 8000

# Copie a URL https fornecida
# Configure no Evolution API webhook
```

⚠️ **Atenção**: Ngrok é para testes! Use um servidor real em produção.

---

## 🔐 SSL/HTTPS (Produção)

### Opção 1: Certbot (Let's Encrypt)

```bash
# Instalar Certbot
sudo apt-get install certbot python3-certbot-nginx

# Gerar certificado
sudo certbot --nginx -d seu-dominio.com

# Renovação automática já está configurada
sudo certbot renew --dry-run
```

### Opção 2: Cloudflare

1. Adicione seu domínio no Cloudflare
2. Configure DNS para apontar para seu servidor
3. Ative SSL/TLS no Cloudflare (modo Full)
4. Pronto! Cloudflare gerencia SSL automaticamente

---

## 📈 Escala (Múltiplas Instâncias)

Se precisar de mais performance:

```yaml
services:
  clinic-bot:
    build: .
    deploy:
      replicas: 3  # 3 instâncias
    # ... resto da config
```

---

## 💡 Dicas

1. **Use volumes** para persistir dados importantes
2. **Faça backups regulares** do banco de dados
3. **Monitore logs** regularmente
4. **Configure alertas** (Uptime Robot, etc)
5. **Use reverse proxy** (Nginx) em produção
6. **Configure SSL** sempre em produção
7. **Limite recursos** se necessário:

```yaml
services:
  clinic-bot:
    # ...
    deploy:
      resources:
        limits:
          cpus: '0.5'
          memory: 512M
```

---

## 📞 Suporte

Se tiver problemas com Docker:

1. Verifique logs: `docker-compose logs`
2. Verifique documentação oficial do Docker
3. Entre em contato com suporte

---

**Docker simplifica muito o deploy! 🚀**

