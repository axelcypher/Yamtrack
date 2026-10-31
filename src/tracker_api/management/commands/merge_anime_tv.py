from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from integrations.webhooks import anime_mappings
from tracker_api import merge


class Command(BaseCommand):
    """Merge anime tracked as TMDB TV shows into anime entries."""

    help = (
        "Merge anime that are tracked as TV shows into the user's anime entries, "
        "using the AniBridge mapping. Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        """Register the command options."""
        parser.add_argument("--user", help="Only this username (default: all users)")
        parser.add_argument(
            "--apply", action="store_true", help="Write the changes (default: dry run)"
        )
        parser.add_argument(
            "--include-tv-only",
            action="store_true",
            help="Also convert anime that are tracked only as TV into anime entries",
        )

    def handle(self, *_args, **options):
        """Plan, print and optionally apply the merge per user."""
        users = get_user_model().objects.order_by("username")
        if options["user"]:
            users = users.filter(username=options["user"])
            if not users.exists():
                msg = f"No user named {options['user']}"
                raise CommandError(msg)

        mapping = anime_mappings.fetch_mapping_data()
        for user in users:
            plan = merge.build_plan(
                user, mapping, include_tv_only=options["include_tv_only"]
            )
            if not plan.merges and not plan.skipped:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"{user.username}:"))
            for line in merge.describe_plan(plan):
                self.stdout.write(line)
            if options["apply"] and plan.merges:
                merge.apply_plan(plan)
                self.stdout.write(self.style.SUCCESS("  merged"))

        if not options["apply"]:
            self.stdout.write(
                self.style.WARNING("Dry run, nothing changed. Use --apply.")
            )
