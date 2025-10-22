#!/usr/bin/env python3
"""
Teste da função _format_appointment_date
"""

def _format_appointment_date(date_value):
    """Converte qualquer formato de data para DD/MM/YYYY"""
    if isinstance(date_value, str):
        # Se for string YYYYMMDD (ex: "20251022")
        if len(date_value) == 8 and date_value.isdigit():
            return f"{date_value[6:8]}/{date_value[4:6]}/{date_value[0:4]}"
        # Se for string DD-MM-YYYY (ex: "22-10-2025")
        elif '-' in date_value:
            return date_value.replace('-', '/')
        # Se for string DD/MM/YYYY (ex: "22/10/2025")
        elif '/' in date_value:
            return date_value
    elif hasattr(date_value, 'strftime'):
        # Se for datetime.date ou datetime.datetime
        return date_value.strftime('%d/%m/%Y')
    
    return str(date_value)

# Testar
test_cases = [
    "20251027",      # YYYYMMDD
    "27/10/2025",    # DD/MM/YYYY
    "27-10-2025",    # DD-MM-YYYY
]

print("=== TESTE DA FUNÇÃO _format_appointment_date ===\n")
for test in test_cases:
    result = _format_appointment_date(test)
    print(f"Input:  '{test}'")
    print(f"Output: '{result}'")
    print(f"Type:   {type(result)}")
    print()

