
from django.conf import settings
from django.conf.urls.static import static
from django.urls import path

from . import views


urlpatterns = [
    # Authentification
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),

    # Berth Plan - Tableau de bord
    path("", views.home, name="home"),

    # Escales
    path("escales/", views.calls_list, name="calls_list"),
    path("escales/nouvelle/", views.call_create, name="call_create"),
    path("escales/<int:pk>/modifier/", views.call_edit, name="call_edit"),
    path("escales/<int:pk>/annuler/", views.call_cancel, name="call_cancel"),
    path("escales/<int:pk>/accoster/", views.call_berth, name="call_berth"),
    path("escales/<int:pk>/terminer/", views.call_complete, name="call_complete"),

    # Paramétrage
    path("parametrage/", views.quay_settings, name="quay_settings"),

    # Export PDF
    path("plan/pdf/", views.plan_pdf, name="plan_pdf"),

    # API
    path("api/plan/", views.api_plan, name="api_plan"),
]

if settings.DEBUG:
    urlpatterns += static(
        settings.MEDIA_URL,
        document_root=settings.MEDIA_ROOT
    )
