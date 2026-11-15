import os
import secrets
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY')
if not SECRET_KEY:
    secret_file = BASE_DIR / '.django-secret'
    if not secret_file.exists():
        try:
            with secret_file.open('x', encoding='utf-8') as stream:
                stream.write(secrets.token_urlsafe(48))
        except FileExistsError:
            pass
    SECRET_KEY = secret_file.read_text(encoding='utf-8').strip()
DEBUG = False
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]']
INSTALLED_APPS = ['django.contrib.auth', 'django.contrib.contenttypes',
                  'django.contrib.sessions', 'django.contrib.messages',
                  'django.contrib.staticfiles', 'painel']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware',
              'django.contrib.sessions.middleware.SessionMiddleware',
              'django.middleware.common.CommonMiddleware',
              'django.middleware.csrf.CsrfViewMiddleware',
              'django.contrib.auth.middleware.AuthenticationMiddleware',
              'django.contrib.messages.middleware.MessageMiddleware',
              'django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF = 'config.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [], 'APP_DIRS': True, 'OPTIONS': {'context_processors': [
                  'django.template.context_processors.request',
                  'django.contrib.messages.context_processors.messages']}}]
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3',
                         'NAME': os.environ.get('PAINEL_DB', str(BASE_DIR / 'empresas.db')),
                         'OPTIONS': {'timeout': 30, 'transaction_mode': 'IMMEDIATE'},
                         'TEST': {'NAME': ':memory:'}}}
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
LANGUAGE_CODE = 'pt-br'
TIME_ZONE = 'America/Sao_Paulo'
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Strict'
CSRF_COOKIE_SAMESITE = 'Strict'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
