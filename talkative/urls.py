from django.urls import path

from . import views

urlpatterns = [
    path("", views.mailbox, name="mailbox"),
    path("agents/<str:nickname>/remove", views.remove_agent, name="remove_agent"),
]
