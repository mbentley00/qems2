# Django settings for QuEST project.
import os

DEBUG = os.environ.get('DEBUG', 'True') == 'True'
TEMPLATE_DEBUG = DEBUG

ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')

# Origins trusted for unsafe (POST) requests over HTTPS. Required by Django
# when the site is served from a real domain behind a TLS-terminating proxy
# (e.g. Azure App Service). Comma-separated, each must include the scheme:
# CSRF_TRUSTED_ORIGINS="https://qems2.example.com,https://qems2.azurewebsites.net"
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get('CSRF_TRUSTED_ORIGINS', '').split(',') if o]

# App Service (and most PaaS proxies) terminate TLS and forward the original
# scheme in this header, so Django can tell the request was really HTTPS.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

# Production hardening: on by default whenever DEBUG is off, individually
# overridable. Safe locally because DEBUG defaults True (so all default off)
# and runserver never sends the forwarded-proto header.
def _env_bool(name, default):
    return os.environ.get(name, str(default)).lower() in ('1', 'true', 'yes', 'on')

SECURE_SSL_REDIRECT = _env_bool('SECURE_SSL_REDIRECT', not DEBUG)
SESSION_COOKIE_SECURE = _env_bool('SESSION_COOKIE_SECURE', not DEBUG)
CSRF_COOKIE_SECURE = _env_bool('CSRF_COOKIE_SECURE', not DEBUG)

ADMINS = (
    # ('Your Name', 'your_email@example.com'),
)

MANAGERS = ADMINS

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'TIMEOUT': 60 * 10,
    }
}

PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."),
)

if os.environ.get('DB_HOST'):
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.environ['DB_NAME'],
            'USER': os.environ['DB_USER'],
            'PASSWORD': os.environ['DB_PASSWORD'],
            'HOST': os.environ['DB_HOST'],
            'PORT': os.environ.get('DB_PORT', '5432'),
            # Azure Database for PostgreSQL requires SSL; 'require' is safe for
            # any managed Postgres. Override with DB_SSLMODE if needed.
            'OPTIONS': {'sslmode': os.environ.get('DB_SSLMODE', 'require')},
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.path.join(PROJECT_ROOT, 'db.sqlite3'),
        }
    }

# Local time zone for this installation. Choices can be found here:
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# although not all choices may be available on all operating systems.
# On Unix systems, a value of None will cause Django to use the same
# timezone as the operating system.
# If running in a Windows environment this must be set to the same as your
# system time zone.
TIME_ZONE = 'America/Chicago'

# Language code for this installation. All choices can be found here:
# http://www.i18nguy.com/unicode/language-identifiers.html
LANGUAGE_CODE = 'en-us'

SITE_ID = 1

# If you set this to False, Django will make some optimizations so as not
# to load the internationalization machinery.
USE_I18N = False

# If you set this to False, Django will not format dates, numbers and
# calendars according to the current locale.
USE_L10N = True

# If you set this to False, Django will not use timezone-aware datetimes.
USE_TZ = True

# Absolute filesystem path to the directory that will hold user-uploaded files.
# Example: "/home/media/media.lawrence.com/media/"
MEDIA_ROOT = ''

# URL that handles the media served from MEDIA_ROOT. Make sure to use a
# trailing slash.
# Examples: "http://media.lawrence.com/media/", "http://example.com/media/"
MEDIA_URL = ''

# Absolute path to the directory static files should be collected to.
# Don't put anything in this directory yourself; store your static files
# in apps' "static/" subdirectories and in STATICFILES_DIRS.
# Example: "/home/media/media.lawrence.com/static/"
STATIC_ROOT = os.path.join(PROJECT_ROOT, 'static')

# URL prefix for static files.
# Example: "http://media.lawrence.com/static/"
STATIC_URL = '/static/'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'

# Additional locations of static files
STATICFILES_DIRS = (
    # Put strings here, like "/home/html/static" or "C:/www/django/static".
    # Always use forward slashes, even on Windows.
    # Don't forget to use absolute paths, not relative paths.
    os.path.join(PROJECT_ROOT, 'qems2', 'qsub', 'static'),
)

# List of finder classes that know how to find static files in
# various locations.
STATICFILES_FINDERS = (
    'django.contrib.staticfiles.finders.FileSystemFinder',
    'django.contrib.staticfiles.finders.AppDirectoriesFinder',
    'djangobower.finders.BowerFinder'
#    'django.contrib.staticfiles.finders.DefaultStorageFinder',
)

BOWER_COMPONENTS_ROOT = os.path.join(PROJECT_ROOT, 'components')
# Only used by the (Django-6-incompatible) bower management command; static
# collection reads BOWER_COMPONENTS_ROOT directly. Overridable so the default
# isn't a Windows-only path in a Linux container.
BOWER_PATH = os.environ.get('BOWER_PATH', os.path.normpath(r'C:\Program Files (x86)\Nodist\bin\bower.cmd'))

SECRET_KEY = os.environ.get('SECRET_KEY')
if not SECRET_KEY:
    with open(os.path.join(PROJECT_ROOT, 'secret'), 'r') as f:
        SECRET_KEY = f.read().strip()

#SECRET_KEY = '%&amp;5&amp;wmrx-g8zpk8=m*kttzkxfy^38ziedy$1kf-4uwme8bksba'

# TEMPLATE_LOADERS removed (handled by TEMPLATES setting below)

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'qems2.qsub.context_processors.debug_flag',
            ],
        },
    },
]

AUTHENTICATION_BACKENDS = (
    # Needed to login by username in Django admin, regardless of `allauth`
    'django.contrib.auth.backends.ModelBackend',

    # `allauth` specific authentication methods, such as login by e-mail
    'allauth.account.auth_backends.AuthenticationBackend'
)

MIDDLEWARE = [
    'django.middleware.common.CommonMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'qems2.urls'

# AUTH_PROFILE_MODULE removed (deprecated in modern Django)

INTERNAL_IPS = ('127.0.0.1')

# Python dotted path to the WSGI application used by Django's runserver.
WSGI_APPLICATION = 'qems2.wsgi.application'

# TEMPLATE_DIRS removed (handled by TEMPLATES setting)

INSTALLED_APPS = (
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.sites',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # Uncomment the next line to enable the admin:
    'haystack',
    'django.contrib.admin',
    'django_comments',
    'djangobower',
    'unicodecsv',
#    'debug_toolbar',
    # Uncomment the next line to enable admin documentation:
    # 'django.contrib.admindocs',
    'qems2.qsub',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
)

# Account and registration settings
ACCOUNT_EMAIL_VERIFICATION='none'
ACCOUNT_SIGNUP_FIELDS = ['email*', 'username*', 'password1*', 'password2*']
ACCOUNT_SIGNUP_FORM_CLASS = 'qems2.qsub.forms.RegistrationFormWithName'

LOGIN_REDIRECT_URL = "/"

# Email: defaults to the console backend (local dev prints mail to stdout), so
# nothing is actually sent until the SMTP backend + credentials are configured
# via environment variables. The app sends through a generic SMTP relay, which
# every transactional provider offers, so switching providers is config-only.
#
# Recommended: a transactional email provider (good deliverability, free tier).
#   SendGrid:  EMAIL_HOST=smtp.sendgrid.net  EMAIL_HOST_USER=apikey
#              EMAIL_HOST_PASSWORD=<api-key>
#   Resend:    EMAIL_HOST=smtp.resend.com    EMAIL_HOST_USER=resend
#              EMAIL_HOST_PASSWORD=<api-key>
# In all cases also set, in the deployment environment:
#   EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
#   EMAIL_PORT=587  EMAIL_USE_TLS=True
#   DEFAULT_FROM_EMAIL=<a sender address verified with the provider>
#
# NOTE: the old smtp-mail.outlook.com default was dropped — Microsoft has
# disabled Basic Auth (SMTP AUTH) for Outlook.com/most M365 tenants, so
# username+password SMTP no longer works there.
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_USE_TLS = _env_bool('EMAIL_USE_TLS', True)
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'qems2@example.com')
SERVER_EMAIL = os.environ.get('SERVER_EMAIL', DEFAULT_FROM_EMAIL)
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')

# A sample logging configuration. The only tangible logging
# performed by this configuration is to send an email to
# the site admins on every HTTP 500 error when DEBUG=False.
# See http://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse'
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'DEBUG',
        },
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler'
        }
    },
    'loggers': {

        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': True,
        },
    }
}

BOWER_INSTALLED_APPS = (
    'jquery',
    'jquery-ui',
    'underscore',
    'backbone',
    'fontawesome',
    'tablesorter',
    'foundation',
    'sprintf',
    'expanding-textareas',
)

HAYSTACK_CONNECTIONS = {
    'default': {
        'ENGINE': 'haystack.backends.whoosh_backend.WhooshEngine',
        # Override to a persistent-volume path in production (the Whoosh index
        # is on-disk and single-writer; see hosting notes).
        'PATH': os.environ.get('WHOOSH_INDEX_PATH', os.path.join(os.path.dirname(__file__), 'whoosh_index')),
    },
}

# Search now uses Postgres full-text search (see views.fulltext_filter), so the
# Whoosh index is no longer written or read. The no-op base processor keeps
# Haystack inert (no per-save index writes, which were the Azure slowdown).
HAYSTACK_SIGNAL_PROCESSOR = 'haystack.signals.BaseSignalProcessor'

TEST_RUNNER = 'django.test.runner.DiscoverRunner'

DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
#COMMENTS_APP = "django_comments_xtd"
#COMMENTS_XTD_MAX_THREAD_LEVEL = 1
