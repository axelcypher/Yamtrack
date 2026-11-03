from django.shortcuts import render
from django.views.decorators.http import require_GET

from app import statistics as app_statistics
from users.models import DateFormatChoices

from . import facts, usage_stats


@require_GET
def usage_statistics(request):
    """Render activity per week, watch/play time, platforms and genres."""
    start, end = app_statistics.parse_activity_date_range(request)
    context = usage_stats.build(request.user, start, end)
    if context["facts_known"] < context["facts_total"]:
        # Fill missing genres/runtimes in the background for the next visit.
        facts.refresh_item_facts.delay(request.user.pk)
    return render(
        request,
        "tracker_api/usage_statistics.html",
        {
            **context,
            "start_date": start,
            "end_date": end,
            "date_format_values": DateFormatChoices.values,
        },
    )
