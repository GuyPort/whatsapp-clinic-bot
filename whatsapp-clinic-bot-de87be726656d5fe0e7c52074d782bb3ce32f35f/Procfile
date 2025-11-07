web: python run.py
worker: celery -A app.celery_app worker --loglevel=info --concurrency=10 -Q celery,send_queue