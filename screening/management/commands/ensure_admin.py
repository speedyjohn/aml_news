from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "При первом запуске просит создать логин и пароль для входа в панель"

    def handle(self, *args, **opts):
        if get_user_model().objects.filter(is_superuser=True).exists():
            return
        self.stdout.write("\nПервый запуск: придумайте логин и пароль для входа в панель.\n"
                          "(email можно оставить пустым — просто нажмите Enter)\n")
        call_command("createsuperuser", interactive=True)
