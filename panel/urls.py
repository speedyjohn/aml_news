from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from screening.views import dump_json_view

urlpatterns = [
    path("admin/dump.json", admin.site.admin_view(dump_json_view), name="dump_json"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
