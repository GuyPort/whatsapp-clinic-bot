# Dockerfile para WhatsApp Clinic Bot
FROM python:3.11-slim

# Definir diretório de trabalho
WORKDIR /app

# Instalar dependências do sistema
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código da aplicação
COPY app/ ./app/
COPY data/ ./data/
COPY run.py .

# Criar diretório para banco de dados
RUN mkdir -p /app/data

# Expor porta
EXPOSE 8000

# Variável de ambiente
ENV PYTHONUNBUFFERED=1

# Comando para rodar
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

