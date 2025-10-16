"""
Serviço de integração com Evolution API para WhatsApp.
"""
import httpx
from typing import Optional, Dict, Any
import logging

from app.simple_config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Cliente para Evolution API"""
    
    def __init__(self):
        self.base_url = settings.evolution_api_url.rstrip('/')
        self.api_key = settings.evolution_api_key
        self.instance_name = settings.evolution_instance_name
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        
        # Debug: Log das configurações
        logger.info(f"WhatsAppService - base_url: {self.base_url}")
        logger.info(f"WhatsAppService - instance_name: {self.instance_name}")
        logger.info(f"WhatsAppService - api_key: {self.api_key[:10] if self.api_key else 'None'}...")
    
    async def send_message(self, phone: str, message: str) -> bool:
        """
        Envia uma mensagem de texto para um número de WhatsApp.
        
        Args:
            phone: Número do telefone (formato: 5511999999999)
            message: Texto da mensagem
            
        Returns:
            True se enviado com sucesso, False caso contrário
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
                
                if response.status_code == 200 or response.status_code == 201:
                    logger.info(f"Mensagem enviada com sucesso para {phone}")
                    return True
                else:
                    logger.error(f"Erro ao enviar mensagem: {response.status_code} - {response.text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Exceção ao enviar mensagem: {str(e)}")
            return False
    
    async def send_message_with_buttons(self, phone: str, message: str, buttons: list) -> bool:
        """
        Envia uma mensagem com botões.
        
        Args:
            phone: Número do telefone
            message: Texto da mensagem
            buttons: Lista de botões [{"displayText": "Texto", "id": "id"}]
            
        Returns:
            True se enviado com sucesso
        """
        try:
            url = f"{self.base_url}/api/send-message"
            
            if not phone.endswith('@s.whatsapp.net'):
                phone = f"{phone}@s.whatsapp.net"
            
            payload = {
                "to": phone,
                "text": message,
                "buttons": buttons
            }
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                
                if response.status_code in [200, 201]:
                    logger.info(f"Mensagem com botões enviada para {phone}")
                    return True
                else:
                    logger.error(f"Erro ao enviar botões: {response.status_code}")
                    # Fallback: enviar como mensagem de texto
                    return await self.send_message(phone, message)
                    
        except Exception as e:
            logger.error(f"Exceção ao enviar botões: {str(e)}")
            # Fallback: enviar como mensagem de texto
            return await self.send_message(phone, message)
    
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


# Instância global do serviço
whatsapp_service = WhatsAppService()

