from django.db import migrations

TASK_NAME = "Refresh item facts"


def create_task(apps, _schema_editor):
    """Fill missing genres/runtimes once a day (django-celery-beat)."""
    interval_schedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    periodic_task = apps.get_model("django_celery_beat", "PeriodicTask")
    schedule, _ = interval_schedule.objects.get_or_create(every=1, period="days")
    periodic_task.objects.get_or_create(
        name=TASK_NAME,
        defaults={"task": TASK_NAME, "interval": schedule, "enabled": True},
    )


def remove_task(apps, _schema_editor):
    """Remove the periodic task again."""
    apps.get_model("django_celery_beat", "PeriodicTask").objects.filter(
        name=TASK_NAME
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("tracker_api", "0004_itemfacts"),
        ("django_celery_beat", "0019_alter_periodictasks_options"),
    ]

    operations = [migrations.RunPython(create_task, remove_task)]
