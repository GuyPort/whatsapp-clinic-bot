"""
Serviço de integração com Evolution API para WhatsApp.
"""
import httpx
from typing import Optional, Dict, Any
import logging
import asyncio
import redis
from redis.lock import Lock

from app.simple_config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Cliente para Evolution API"""
    
    def __init__(self):
        self.base_url = settings.evolution_api_url.rstrip('/')
        self.api_key = settings.evolution_api_key.strip() if settings.evolution_api_key else None
        self.instance_name = settings.evolution_instance_name
        self.headers = {
            "Authorization": f"Bearer {self.api_key.strip() if self.api_key else ''}",
            "Content-Type": "application/json"
        }
        
        # Cliente Redis para locks distribuídos
        try:
            self.redis_client = redis.from_url(settings.redis_url, decode_responses=False)
            logger.info(f"✅ Cliente Redis conectado para rate limiting")
        except Exception as e:
            logger.error(f"❌ Erro ao conectar Redis: {str(e)}")
            self.redis_client = None
        
        # Debug: Log das configurações
        logger.info(f"WhatsAppService - base_url: {self.base_url}")
        logger.info(f"WhatsAppService - instance_name: {self.instance_name}")
        logger.info(f"WhatsAppService - api_key: {self.api_key[:10] if self.api_key else 'None'}...")
    
    async def send_message(self, phone: str, message: str) -> bool:
        """
        Envia uma mensagem de texto para um número de WhatsApp.
        Usa Redis Lock para garantir rate limiting de 1 mensagem a cada 5 segundos.
        
        Args:
            phone: Número do telefone (formato: 5511999999999)
            message: Texto da mensagem
            
        Returns:
            True se enviado com sucesso, False caso contrário
        """
        # Se Redis não estiver disponível, tenta enviar sem lock (fallback)
        if not self.redis_client:
            logger.warning("⚠️ Redis não disponível, enviando sem rate limiting")
            return await self._send_message_internal(phone, message)
        
        lock_key = "whatsapp:send_message:lock"
        lock = Lock(
            self.redis_client,
            lock_key,
            timeout=5,  # Lock expira após 5 segundos (garante intervalo mínimo)
            blocking_timeout=30  # Aguarda até 30 segundos para adquirir lock
        )
        
        try:
            # Adquirir lock antes de enviar (aguarda até 30s)
            logger.debug(f"🔒 Tentando adquirir lock para enviar mensagem para {phone}")
            acquired = lock.acquire(blocking=True)
            
            if not acquired:
                logger.error(f"❌ Timeout ao aguardar lock para enviar mensagem para {phone}")
                return False
            
            logger.debug(f"✅ Lock adquirido, enviando mensagem para {phone}")
            
            # Tentar enviar mensagem (com retry automático para 429)
            max_retries = 3
            for attempt in range(max_retries):
                success = await self._send_message_internal(phone, message)
                
                if success:
                    # Manter lock por 5 segundos para garantir intervalo mínimo
                    # Isso previne que outro worker envie imediatamente após
                    logger.debug(f"✅ Mensagem enviada com sucesso, mantendo lock por 5s para rate limiting")
                    await asyncio.sleep(5)
                    # Lock será liberado no finally
                    return True
                
                # Se erro 429, aguardar e tentar novamente
                if attempt < max_retries - 1:
                    wait_time = 5  # Aguardar 5 segundos antes de retry
                    logger.warning(f"⚠️ Erro ao enviar mensagem (tentativa {attempt + 1}/{max_retries}), aguardando {wait_time}s antes de retry")
                    await asyncio.sleep(wait_time)
            
            logger.error(f"❌ Falha ao enviar mensagem após {max_retries} tentativas")
            return False
            
        except Exception as e:
            logger.error(f"❌ Exceção ao enviar mensagem: {str(e)}")
            return False
        finally:
            # Sempre liberar lock se adquirido
            try:
                if lock.owned():
                    lock.release()
                    logger.debug(f"🔓 Lock liberado")
            except Exception as e:
                logger.warning(f"⚠️ Erro ao liberar lock: {str(e)}")
    
    async def _send_message_internal(self, phone: str, message: str) -> bool:
        """
        Método interno para enviar mensagem sem lock.
        Trata erros 429 automaticamente.
        """
        try:
            url = f"{self.base_url}/api/send-message"
            
            # Garantir que o número está no formato correto
            if not phone.endswith('@s.whatsapp.net'):
                phone = f"{phone}@s.whatsapp.net"
            
            payload = {
                "to": phone,
                "text": message,
                "delay": 1200  # Delay de 1.2s para parecer mais humano
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                
                # Tratar erro 429 (rate limit)
                if response.status_code == 429:
                    try:
                        error_data = response.json()
                        retry_after = error_data.get('retry_after', 5)
                        logger.warning(f"⚠️ Rate limit atingido (429), retry_after: {retry_after}s")
                        await asyncio.sleep(retry_after)
                        return False  # Retorna False para trigger retry no método principal
                    except Exception:
                        logger.warning(f"⚠️ Rate limit atingido (429), aguardando 5s")
                        await asyncio.sleep(5)
                        return False
                
                if response.status_code == 200 or response.status_code == 201:
                    logger.info(f"Mensagem enviada com sucesso para {phone}")
                    return True
                else:
                    logger.error(f"Erro ao enviar mensagem: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Exceção ao enviar mensagem: {str(e)}")
            return False
    
# send_message_with_buttons removido - não utilizado
    
    async def get_instance_status(self) -> Dict[str, Any]:
        """
        Verifica o status da instância Evolution API.
        
        Returns:
            Dicionário com status da instância
        """
        try:
            url = f"{self.base_url}/api/status"
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=self.headers)
                
                if response.status_code == 200:
                    return response.json()
                else:
                    return {"error": f"Status {response.status_code}"}
                    
        except Exception as e:
            logger.error(f"Erro ao verificar status: {str(e)}")
            return {"error": str(e)}
    
    def acquire_chat_lock(self, phone: str, timeout: int = 60, blocking_timeout: int = 10) -> Optional[Lock]:
        """
        Retorna um lock Redis específico por contato.
        Quando Redis não estiver disponível, retorna None (seguimos sem lock).
        """
        if not self.redis_client:
            logger.warning("⚠️ Redis indisponível - processamento seguirá sem lock por contato")
            return None
        
        lock_key = f"whatsapp:chat-lock:{phone}"
        return Lock(
            self.redis_client,
            lock_key,
            timeout=timeout,
            blocking_timeout=blocking_timeout
        )
    
    async def mark_message_as_read(self, phone: str, message_id: str) -> bool:
        """
        Marca uma mensagem como lida.
        Nota: Wasender pode não suportar esta funcionalidade.

        Args:
            phone: Número do telefone
            message_id: ID da mensagem

        Returns:
            True se marcado com sucesso
        """
        # Por enquanto, sempre retorna True pois o Wasender pode não suportar
        # marcar mensagens como lidas
        logger.info(f"Marcando mensagem {message_id} como lida para {phone}")
        return True

    # =========================================================================
    # MESSAGE BUFFER - Sistema de debounce para agrupar mensagens
    # =========================================================================

    MESSAGE_BUFFER_KEY = "whatsapp:message_buffer:{phone}"
    LAST_MESSAGE_TIME_KEY = "whatsapp:last_message_time:{phone}"
    MESSAGE_DEBOUNCE_SECONDS = 7  # Tempo de espera para agrupar mensagens

    def add_message_to_buffer(self, phone: str, message: str, message_id: str = None) -> bool:
        """
        Adiciona uma mensagem ao buffer temporário.
        Atualiza o timestamp da última mensagem recebida.

        Args:
            phone: Número do telefone
            message: Texto da mensagem
            message_id: ID da mensagem (opcional)

        Returns:
            True se adicionado com sucesso
        """
        if not self.redis_client:
            logger.warning("Redis indisponivel - buffer de mensagens desativado")
            return False

        try:
            import time
            import json

            buffer_key = self.MESSAGE_BUFFER_KEY.format(phone=phone)
            time_key = self.LAST_MESSAGE_TIME_KEY.format(phone=phone)

            # Criar objeto da mensagem com timestamp
            msg_data = json.dumps({
                "text": message,
                "message_id": message_id,
                "timestamp": time.time()
            })

            # Adicionar mensagem ao buffer (lista Redis)
            self.redis_client.rpush(buffer_key, msg_data)

            # Definir TTL no buffer (expira após 5 minutos se não processado)
            self.redis_client.expire(buffer_key, 300)

            # Atualizar timestamp da última mensagem
            self.redis_client.set(time_key, str(time.time()), ex=300)

            # Contar mensagens no buffer
            count = self.redis_client.llen(buffer_key)
            logger.info(f"[BUFFER] Mensagem adicionada para {phone} (total: {count})")

            return True

        except Exception as e:
            logger.error(f"[BUFFER] Erro ao adicionar mensagem: {str(e)}")
            return False

    def get_buffered_messages(self, phone: str) -> list:
        """
        Retorna todas as mensagens do buffer e limpa o buffer.

        Args:
            phone: Número do telefone

        Returns:
            Lista de mensagens concatenadas ou lista vazia
        """
        if not self.redis_client:
            return []

        try:
            import json

            buffer_key = self.MESSAGE_BUFFER_KEY.format(phone=phone)

            # Obter todas as mensagens
            raw_messages = self.redis_client.lrange(buffer_key, 0, -1)

            if not raw_messages:
                return []

            # Limpar o buffer
            self.redis_client.delete(buffer_key)

            # Decodificar mensagens
            messages = []
            for raw in raw_messages:
                try:
                    data = json.loads(raw.decode('utf-8') if isinstance(raw, bytes) else raw)
                    messages.append(data)
                except Exception:
                    # Se falhar ao decodificar, usar como string
                    messages.append({"text": raw.decode('utf-8') if isinstance(raw, bytes) else str(raw)})

            logger.info(f"[BUFFER] {len(messages)} mensagens recuperadas para {phone}")
            return messages

        except Exception as e:
            logger.error(f"[BUFFER] Erro ao obter mensagens: {str(e)}")
            return []

    def get_concatenated_message(self, phone: str) -> str:
        """
        Retorna todas as mensagens do buffer concatenadas em uma única string.

        Args:
            phone: Número do telefone

        Returns:
            Mensagens concatenadas ou string vazia
        """
        messages = self.get_buffered_messages(phone)
        if not messages:
            return ""

        # Extrair apenas o texto de cada mensagem
        texts = [msg.get("text", "") for msg in messages if msg.get("text")]

        # Concatenar com quebra de linha
        concatenated = "\n".join(texts)

        logger.info(f"[BUFFER] Mensagens concatenadas para {phone}: {concatenated[:100]}...")
        return concatenated

    def should_process_now(self, phone: str) -> bool:
        """
        Verifica se já passou tempo suficiente desde a última mensagem
        para processar o buffer.

        Args:
            phone: Número do telefone

        Returns:
            True se deve processar agora, False se deve esperar
        """
        if not self.redis_client:
            return True  # Sem Redis, processa imediatamente

        try:
            import time

            time_key = self.LAST_MESSAGE_TIME_KEY.format(phone=phone)
            last_time_str = self.redis_client.get(time_key)

            if not last_time_str:
                return True  # Sem registro, pode processar

            last_time = float(last_time_str.decode('utf-8') if isinstance(last_time_str, bytes) else last_time_str)
            elapsed = time.time() - last_time

            should_process = elapsed >= self.MESSAGE_DEBOUNCE_SECONDS

            logger.info(f"[BUFFER] {phone} - elapsed: {elapsed:.1f}s, debounce: {self.MESSAGE_DEBOUNCE_SECONDS}s, should_process: {should_process}")

            return should_process

        except Exception as e:
            logger.error(f"[BUFFER] Erro ao verificar tempo: {str(e)}")
            return True  # Em caso de erro, processa

    def has_pending_messages(self, phone: str) -> bool:
        """
        Verifica se há mensagens pendentes no buffer.

        Args:
            phone: Número do telefone

        Returns:
            True se há mensagens pendentes
        """
        if not self.redis_client:
            return False

        try:
            buffer_key = self.MESSAGE_BUFFER_KEY.format(phone=phone)
            count = self.redis_client.llen(buffer_key)
            return count > 0
        except Exception:
            return False


# Instância global do serviço
whatsapp_service = WhatsAppService()