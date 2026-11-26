from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from screening.neg_views import overview_view
from screening.views import dump_json_view, neg_dump_json_view

urlpatterns = [
    path("admin/dump.json", admin.site.admin_view(dump_json_view), name="dump_json"),
    path("admin/negative/", admin.site.admin_view(overview_view), name="neg_overview"),
    path("admin/negative/dump.json", admin.site.admin_view(neg_dump_json_view),
         name="neg_dump_json"),
    path("admin/", admin.site.urls),
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
]
