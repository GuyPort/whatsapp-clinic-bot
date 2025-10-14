"""
Serviço de integração com Evolution API para WhatsApp.
"""
import httpx
from typing import Optional, Dict, Any
import logging

from app.config import settings

logger = logging.getLogger(__name__)


class WhatsAppService:
    """Cliente para Evolution API"""
    
    def __init__(self):
        self.base_url = settings.evolution_api_url.rstrip('/')
        self.api_key = settings.evolution_api_key
        self.instance_name = settings.evolution_instance_name
        self.headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json"
        }
    
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
            url = f"{self.base_url}/message/sendText/{self.instance_name}"
            
            # Garantir que o número está no formato correto
            if not phone.endswith('@s.whatsapp.net'):
                phone = f"{phone}@s.whatsapp.net"
            
            payload = {
                "number": phone,
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
            url = f"{self.base_url}/message/sendButtons/{self.instance_name}"
            
            if not phone.endswith('@s.whatsapp.net'):
                phone = f"{phone}@s.whatsapp.net"
            
            payload = {
                "number": phone,
                "title": message,
                "description": "",
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
            url = f"{self.base_url}/instance/connectionState/{self.instance_name}"
            
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
        
        Args:
            phone: Número do telefone
            message_id: ID da mensagem
            
        Returns:
            True se marcado com sucesso
        """
        try:
            url = f"{self.base_url}/chat/markMessageAsRead/{self.instance_name}"
            
            if not phone.endswith('@s.whatsapp.net'):
                phone = f"{phone}@s.whatsapp.net"
            
            payload = {
                "read_messages": [{
                    "id": message_id,
                    "fromMe": False,
                    "remoteJid": phone
                }]
            }
            
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(url, json=payload, headers=self.headers)
                return response.status_code in [200, 201]
                
        except Exception as e:
            logger.error(f"Erro ao marcar como lida: {str(e)}")
            return False


# Instância global do serviço
whatsapp_service = WhatsAppService()

