from django.apps import AppConfig

class QSubConfig(AppConfig):
    name = 'qems2.qsub'
    verbose_name = 'QEMS2'

    def ready(self):
        # Register comment/question email notification handlers
        import qems2.qsub.signals  # noqa: F401
