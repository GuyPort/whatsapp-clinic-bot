"""
Serviço de integração com Google Calendar.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
import logging
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import pytz

from app.simple_config import settings
from app.utils import get_brazil_timezone

logger = logging.getLogger(__name__)

# Escopos necessários para o Google Calendar
SCOPES = ['https://www.googleapis.com/auth/calendar']


class GoogleCalendarService:
    """Cliente para Google Calendar API"""
    
    def __init__(self):
        self.calendar_id = settings.google_calendar_id
        self.service = None
        self.timezone = get_brazil_timezone()
        self._authenticate()
    
    def _authenticate(self):
        """Autentica com Google Calendar usando Service Account"""
        try:
            credentials_file = settings.google_service_account_file
            
            if not os.path.exists(credentials_file):
                logger.warning(f"Arquivo de credenciais não encontrado: {credentials_file}")
                logger.warning("Google Calendar não estará disponível até que as credenciais sejam configuradas.")
                return
            
            credentials = service_account.Credentials.from_service_account_file(
                credentials_file,
                scopes=SCOPES
            )
            
            self.service = build('calendar', 'v3', credentials=credentials)
            logger.info("✅ Autenticado com sucesso no Google Calendar")
            
        except Exception as e:
            logger.error(f"Erro ao autenticar com Google Calendar: {str(e)}")
            self.service = None
    
    def is_available(self) -> bool:
        """Verifica se o serviço está disponível"""
        return self.service is not None
    
    def get_events(self, start_date: datetime, end_date: datetime) -> List[Dict[str, Any]]:
        """
        Busca eventos no calendário em um intervalo de datas.
        
        Args:
            start_date: Data/hora inicial
            end_date: Data/hora final
            
        Returns:
            Lista de eventos
        """
        if not self.is_available():
            logger.warning("Google Calendar não disponível")
            return []
        
        try:
            # Converter para ISO format com timezone
            time_min = start_date.astimezone(self.timezone).isoformat()
            time_max = end_date.astimezone(self.timezone).isoformat()
            
            events_result = self.service.events().list(
                calendarId=self.calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy='startTime'
            ).execute()
            
            events = events_result.get('items', [])
            logger.info(f"Encontrados {len(events)} eventos entre {start_date} e {end_date}")
            
            return events
            
        except HttpError as e:
            logger.error(f"Erro HTTP ao buscar eventos: {str(e)}")
            return []
        except Exception as e:
            logger.error(f"Erro ao buscar eventos: {str(e)}")
            return []
    
    def create_event(
        self,
        title: str,
        start_datetime: datetime,
        duration_minutes: int,
        description: str = "",
        attendee_email: Optional[str] = None
    ) -> Optional[str]:
        """
        Cria um novo evento no calendário.
        
        Args:
            title: Título do evento
            start_datetime: Data/hora de início
            duration_minutes: Duração em minutos
            description: Descrição do evento
            attendee_email: Email do participante (opcional)
            
        Returns:
            ID do evento criado ou None se falhar
        """
        if not self.is_available():
            logger.warning("Google Calendar não disponível")
            return None
        
        try:
            end_datetime = start_datetime + timedelta(minutes=duration_minutes)
            
            # Garantir que estão no timezone correto
            start_datetime = start_datetime.astimezone(self.timezone)
            end_datetime = end_datetime.astimezone(self.timezone)
            
            event = {
                'summary': title,
                'description': description,
                'start': {
                    'dateTime': start_datetime.isoformat(),
                    'timeZone': str(self.timezone),
                },
                'end': {
                    'dateTime': end_datetime.isoformat(),
                    'timeZone': str(self.timezone),
                },
                'reminders': {
                    'useDefault': False,
                    'overrides': [
                        {'method': 'popup', 'minutes': 24 * 60},  # 1 dia antes
                        {'method': 'popup', 'minutes': 60},        # 1 hora antes
                    ],
                },
            }
            
            # Adicionar participante se fornecido
            if attendee_email:
                event['attendees'] = [{'email': attendee_email}]
            
            created_event = self.service.events().insert(
                calendarId=self.calendar_id,
                body=event,
                sendUpdates='none'  # Não enviar emails automáticos
            ).execute()
            
            event_id = created_event.get('id')
            logger.info(f"✅ Evento criado com sucesso: {event_id}")
            
            return event_id
            
        except HttpError as e:
            logger.error(f"Erro HTTP ao criar evento: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Erro ao criar evento: {str(e)}")
            return None
    
    def delete_event(self, event_id: str) -> bool:
        """
        Deleta um evento do calendário.
        
        Args:
            event_id: ID do evento
            
        Returns:
            True se deletado com sucesso
        """
        if not self.is_available():
            logger.warning("Google Calendar não disponível")
            return False
        
        try:
            self.service.events().delete(
                calendarId=self.calendar_id,
                eventId=event_id,
                sendUpdates='none'
            ).execute()
            
            logger.info(f"✅ Evento deletado: {event_id}")
            return True
            
        except HttpError as e:
            logger.error(f"Erro HTTP ao deletar evento: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Erro ao deletar evento: {str(e)}")
            return False
    
    def update_event(
        self,
        event_id: str,
        new_start_datetime: datetime,
        duration_minutes: int,
        title: Optional[str] = None,
        description: Optional[str] = None
    ) -> bool:
        """
        Atualiza um evento existente.
        
        Args:
            event_id: ID do evento
            new_start_datetime: Nova data/hora de início
            duration_minutes: Duração em minutos
            title: Novo título (opcional)
            description: Nova descrição (opcional)
            
        Returns:
            True se atualizado com sucesso
        """
        if not self.is_available():
            logger.warning("Google Calendar não disponível")
            return False
        
        try:
            # Buscar evento atual
            event = self.service.events().get(
                calendarId=self.calendar_id,
                eventId=event_id
            ).execute()
            
            # Atualizar campos
            new_end_datetime = new_start_datetime + timedelta(minutes=duration_minutes)
            new_start_datetime = new_start_datetime.astimezone(self.timezone)
            new_end_datetime = new_end_datetime.astimezone(self.timezone)
            
            event['start'] = {
                'dateTime': new_start_datetime.isoformat(),
                'timeZone': str(self.timezone),
            }
            event['end'] = {
                'dateTime': new_end_datetime.isoformat(),
                'timeZone': str(self.timezone),
            }
            
            if title:
                event['summary'] = title
            if description:
                event['description'] = description
            
            # Atualizar evento
            self.service.events().update(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=event,
                sendUpdates='none'
            ).execute()
            
            logger.info(f"✅ Evento atualizado: {event_id}")
            return True
            
        except HttpError as e:
            logger.error(f"Erro HTTP ao atualizar evento: {str(e)}")
            return False
        except Exception as e:
            logger.error(f"Erro ao atualizar evento: {str(e)}")
            return False


# Instância global do serviço
calendar_service = GoogleCalendarService()

