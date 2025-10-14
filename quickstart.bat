@echo off
echo ========================================
echo WhatsApp Clinic Bot - Quick Start
echo ========================================
echo.

REM Verificar se Python está instalado
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python nao encontrado!
    echo Por favor, instale Python 3.11 ou superior.
    pause
    exit /b 1
)

echo [OK] Python encontrado!
echo.

REM Verificar se venv existe
if not exist "venv\" (
    echo Criando ambiente virtual...
    python -m venv venv
    echo [OK] Ambiente virtual criado!
    echo.
)

REM Ativar venv
echo Ativando ambiente virtual...
call venv\Scripts\activate.bat

REM Instalar/atualizar dependências
echo.
echo Instalando dependencias...
pip install -r requirements.txt
echo.

REM Verificar se .env existe
if not exist ".env" (
    echo.
    echo [AVISO] Arquivo .env nao encontrado!
    echo Por favor, copie env.example para .env e configure.
    echo.
    pause
    exit /b 1
)

REM Rodar servidor
echo.
echo ========================================
echo Iniciando servidor...
echo ========================================
echo.
echo Acesse: http://localhost:8000
echo Pressione Ctrl+C para parar
echo.

python run.py

pause

