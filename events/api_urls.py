from django.urls import path

from . import api

app_name = "events_api"

urlpatterns = [
    path("events/", api.receive_event, name="receive_event"),
    path("health/", api.health, name="health"),
]
