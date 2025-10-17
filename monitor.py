"""
Script de monitoramento do bot.
Verifica status de todos os serviços e envia alertas se necessário.
"""
import asyncio
import sys
import os
from datetime import datetime, timedelta
import json

sys.path.insert(0, '.')

from app.simple_config import settings
from app.database import get_db
from app.whatsapp_service import whatsapp_service
from app.models import Appointment, AppointmentStatus
from app.utils import now_brazil, format_datetime_br


class ServiceMonitor:
    """Monitor de serviços"""
    
    def __init__(self):
        self.results = {}
    
    async def check_whatsapp(self) -> dict:
        """Verifica status do WhatsApp"""
        try:
            status = await whatsapp_service.get_instance_status()
            
            if isinstance(status, dict) and 'error' not in status:
                state = status.get('state', 'unknown')
                return {
                    "status": "ok" if state == "open" else "warning",
                    "message": f"WhatsApp: {state}",
                    "details": status
                }
            else:
                return {
                    "status": "error",
                    "message": "WhatsApp: Erro ao obter status",
                    "details": status
                }
        except Exception as e:
            return {
                "status": "error",
                "message": f"WhatsApp: Erro - {str(e)}",
                "details": None
            }
    
    def check_calendar(self) -> dict:
        """Google Calendar removido - sempre OK"""
        return {
            "status": "ok",
            "message": "Google Calendar: Removido (usando apenas banco de dados)",
            "details": None
        }
    
    def check_database(self) -> dict:
        """Verifica status do banco de dados"""
        try:
            with get_db() as db:
                # Tentar fazer uma query simples
                db.query(Appointment).count()
                
                return {
                    "status": "ok",
                    "message": "Database: Conectado",
                    "details": None
                }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Database: Erro - {str(e)}",
                "details": None
            }
    
    def check_upcoming_appointments(self) -> dict:
        """Verifica consultas próximas"""
        try:
            with get_db() as db:
                now = now_brazil()
                
                # Consultas nas próximas 24 horas
                next_24h = now + timedelta(hours=24)
                upcoming = db.query(Appointment).filter(
                    Appointment.appointment_date >= now,
                    Appointment.appointment_date <= next_24h,
                    Appointment.status == AppointmentStatus.AGENDADA
                ).order_by(Appointment.appointment_date).all()
                
                if not upcoming:
                    return {
                        "status": "info",
                        "message": "Nenhuma consulta nas próximas 24h",
                        "details": None
                    }
                
                details = []
                for apt in upcoming:
                    details.append({
                        "patient": apt.patient_name,
                        "datetime": format_datetime_br(apt.appointment_date),
                        "phone": apt.patient_phone
                    })
                
                return {
                    "status": "info",
                    "message": f"{len(upcoming)} consulta(s) nas próximas 24h",
                    "details": details
                }
        except Exception as e:
            return {
                "status": "error",
                "message": f"Erro ao verificar consultas: {str(e)}",
                "details": None
            }
    
    def check_config_files(self) -> dict:
        """Verifica arquivos de configuração"""
        errors = []
        warnings = []
        
        # Verificar .env
        if not os.path.exists('.env'):
            errors.append("Arquivo .env não encontrado")
        
        # Verificar clinic_info.json
        if not os.path.exists('data/clinic_info.json'):
            errors.append("Arquivo clinic_info.json não encontrado")
        
        # Google Calendar removido - não precisa verificar credentials
        
        if errors:
            return {
                "status": "error",
                "message": f"Arquivos: {len(errors)} erro(s)",
                "details": {"errors": errors, "warnings": warnings}
            }
        elif warnings:
            return {
                "status": "warning",
                "message": f"Arquivos: {len(warnings)} aviso(s)",
                "details": {"warnings": warnings}
            }
        else:
            return {
                "status": "ok",
                "message": "Arquivos: Todos presentes",
                "details": None
            }
    
    async def run_all_checks(self):
        """Executa todas as verificações"""
        print("\n🔍 Monitoramento do Bot da Clínica")
        print("=" * 60)
        print(f"Data/Hora: {now_brazil().strftime('%d/%m/%Y %H:%M:%S')}\n")
        
        # Config files
        print("📄 Arquivos de Configuração...")
        self.results['config'] = self.check_config_files()
        self._print_result('config')
        
        # Database
        print("\n🗄️  Banco de Dados...")
        self.results['database'] = self.check_database()
        self._print_result('database')
        
        # WhatsApp
        print("\n💬 WhatsApp (Evolution API)...")
        self.results['whatsapp'] = await self.check_whatsapp()
        self._print_result('whatsapp')
        
        # Google Calendar
        print("\n📅 Google Calendar...")
        self.results['calendar'] = self.check_calendar()
        self._print_result('calendar')
        
        # Upcoming appointments
        print("\n🔜 Consultas Próximas...")
        self.results['appointments'] = self.check_upcoming_appointments()
        self._print_result('appointments')
        
        # Summary
        print("\n" + "=" * 60)
        self._print_summary()
        
        return self.results
    
    def _print_result(self, service):
        """Imprime resultado de um serviço"""
        result = self.results[service]
        status = result['status']
        message = result['message']
        
        icon = {
            'ok': '✅',
            'warning': '⚠️',
            'error': '❌',
            'info': 'ℹ️'
        }.get(status, '❓')
        
        print(f"  {icon} {message}")
        
        if result['details'] and status in ['error', 'warning']:
            print(f"     Detalhes: {result['details']}")
    
    def _print_summary(self):
        """Imprime resumo geral"""
        ok_count = sum(1 for r in self.results.values() if r['status'] == 'ok')
        warning_count = sum(1 for r in self.results.values() if r['status'] == 'warning')
        error_count = sum(1 for r in self.results.values() if r['status'] == 'error')
        
        print("📊 RESUMO:")
        print(f"   ✅ OK: {ok_count}")
        print(f"   ⚠️  Avisos: {warning_count}")
        print(f"   ❌ Erros: {error_count}")
        
        if error_count > 0:
            print("\n⚠️  ATENÇÃO: Há erros que precisam ser corrigidos!")
            print("   O bot pode não funcionar corretamente.")
        elif warning_count > 0:
            print("\nℹ️  Há avisos. O bot funcionará, mas algumas features podem estar desabilitadas.")
        else:
            print("\n✅ Tudo OK! O bot está operacional.")


async def continuous_monitor(interval_minutes: int = 5):
    """
    Monitora continuamente.
    
    Args:
        interval_minutes: Intervalo entre verificações em minutos
    """
    monitor = ServiceMonitor()
    
    print(f"🔄 Monitoramento contínuo iniciado (intervalo: {interval_minutes} min)")
    print("Pressione Ctrl+C para parar\n")
    
    try:
        while True:
            await monitor.run_all_checks()
            
            print(f"\n⏳ Próxima verificação em {interval_minutes} minutos...")
            await asyncio.sleep(interval_minutes * 60)
            print("\n" + "="*60 + "\n")
            
    except KeyboardInterrupt:
        print("\n\n👋 Monitoramento interrompido")


async def single_check():
    """Executa uma verificação única"""
    monitor = ServiceMonitor()
    results = await monitor.run_all_checks()
    
    # Salvar relatório
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = f"monitor_report_{timestamp}.json"
    
    with open(report_file, 'w', encoding='utf-8') as f:
        json.dump({
            "timestamp": timestamp,
            "results": results
        }, f, indent=2, ensure_ascii=False)
    
    print(f"\n📄 Relatório salvo: {report_file}")


def main():
    """Menu principal"""
    print("\n🔍 Monitor do Bot da Clínica")
    print("=" * 60)
    print("1. Verificação única")
    print("2. Monitoramento contínuo (5 min)")
    print("3. Monitoramento contínuo (15 min)")
    print("4. Sair")
    
    choice = input("\nEscolha uma opção: ").strip()
    
    if choice == "1":
        asyncio.run(single_check())
    elif choice == "2":
        asyncio.run(continuous_monitor(interval_minutes=5))
    elif choice == "3":
        asyncio.run(continuous_monitor(interval_minutes=15))
    else:
        print("👋 Até logo!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 Programa interrompido")
    except Exception as e:
        print(f"\n❌ Erro: {str(e)}")
        import traceback
        traceback.print_exc()

