from django.urls import path

from tracker_api import views

urlpatterns = [
    path("me", views.me, name="tracker_api_me"),
    path("search", views.search, name="tracker_api_search"),
    path("resolve", views.resolve, name="tracker_api_resolve"),
    path("progress", views.progress, name="tracker_api_progress"),
    path("scrobble", views.scrobble, name="tracker_api_scrobble"),
    path("undo", views.undo, name="tracker_api_undo"),
]
