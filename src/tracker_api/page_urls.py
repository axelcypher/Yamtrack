from django.urls import path

from tracker_api import pages

urlpatterns = [
    path("statistics", pages.usage_statistics, name="usage_statistics"),
]
