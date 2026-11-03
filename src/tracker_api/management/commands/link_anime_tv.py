from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from integrations.webhooks import anime_mappings
from tracker_api import links, sync


class Command(BaseCommand):
    """Link TV shows that are also tracked as anime and sync their progress."""

    help = (
        "Link TMDB TV shows to the user's anime entries via AniBridge (the TV "
        "show is then listed as anime) and bring both to the same progress. "
        "Nothing is deleted. Dry run unless --apply is given."
    )

    def add_arguments(self, parser):
        """Register the command options."""
        parser.add_argument("--user", help="Only this username (default: all users)")
        parser.add_argument(
            "--apply", action="store_true", help="Write the changes (default: dry run)"
        )

    def handle(self, *_args, **options):
        """Plan, print and optionally apply links and progress sync per user."""
        users = get_user_model().objects.order_by("username")
        if options["user"]:
            users = users.filter(username=options["user"])
            if not users.exists():
                msg = f"No user named {options['user']}"
                raise CommandError(msg)

        dry_run = not options["apply"]
        mapping = anime_mappings.fetch_mapping_data()
        for user in users:
            plan = links.plan_links(user, mapping)
            results = sync.reconcile_user(user, mapping, dry_run=dry_run)
            if not (plan.changed or results):
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"{user.username}:"))
            for tv, item, seasons in plan.create:
                numbers = ", ".join(f"S{number}" for number in seasons)
                self.stdout.write(f"  link {tv.item.title} ({numbers}) -> {item.title}")
            for link in plan.remove:
                self.stdout.write(
                    f"  unlink {link.tv.item.title} -> {link.anime_item.title}"
                )
            for result in results:
                anime = (
                    f"anime {result.anime_before} -> {result.anime_after}"
                    if result.anime_after != result.anime_before
                    else "anime unchanged"
                )
                self.stdout.write(
                    f"  sync {result.title}: {anime}, "
                    f"{result.tv_episodes} TV episodes to record"
                )

        if dry_run:
            self.stdout.write(
                self.style.WARNING("Dry run, nothing changed. Use --apply.")
            )
