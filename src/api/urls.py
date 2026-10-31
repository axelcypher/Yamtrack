from django.urls import path

from api import views

urlpatterns = [
    path("me", views.me, name="api_me"),
    path("search", views.search, name="api_search"),
    path("resolve", views.resolve, name="api_resolve"),
    path("progress", views.progress, name="api_progress"),
    path("scrobble", views.scrobble, name="api_scrobble"),
    path("undo", views.undo, name="api_undo"),
]
