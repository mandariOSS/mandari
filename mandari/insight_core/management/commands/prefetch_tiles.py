"""
Management Command: Tiles für Kommunen prefetchen.

Lädt alle Map-Tiles für die Bounding Boxes der Kommunen
und speichert sie im lokalen Cache für maximale Performance.

Usage:
    python manage.py prefetch_tiles                     # Alle Kommunen
    python manage.py prefetch_tiles --body-id <UUID>    # Einzelne Kommune
    python manage.py prefetch_tiles --zoom 10,16        # Nur bestimmte Zoom-Levels
    python manage.py prefetch_tiles --clear             # Cache leeren vor Prefetch
"""

import time
import httpx
from django.core.management.base import BaseCommand, CommandError

from insight_core.models import OParlBody, TileCache


class Command(BaseCommand):
    help = "Prefetch map tiles for municipalities and store in local cache"

    def add_arguments(self, parser):
        parser.add_argument(
            "--body-id",
            type=str,
            help="UUID of a specific body to prefetch tiles for",
        )
        parser.add_argument(
            "--zoom",
            type=str,
            default="10,16",
            help="Zoom level range, e.g., '10,16' for levels 10-16 (default: 10,16)",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Clear existing cache before prefetching",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be fetched without actually fetching",
        )

    def handle(self, *args, **options):
        # Parse zoom levels
        zoom_range = options["zoom"].split(",")
        if len(zoom_range) != 2:
            raise CommandError("Zoom must be in format 'min,max', e.g., '10,16'")

        try:
            zoom_min, zoom_max = int(zoom_range[0]), int(zoom_range[1])
            zoom_levels = range(zoom_min, zoom_max + 1)
        except ValueError:
            raise CommandError("Invalid zoom level values")

        # Clear cache if requested
        if options["clear"]:
            count = TileCache.objects.count()
            TileCache.objects.all().delete()
            self.stdout.write(self.style.WARNING(f"Cleared {count} tiles from cache"))

        # Get bodies to process
        if options["body_id"]:
            bodies = OParlBody.objects.filter(id=options["body_id"])
            if not bodies.exists():
                raise CommandError(f"Body with ID {options['body_id']} not found")
        else:
            bodies = OParlBody.objects.exclude(
                bbox_north__isnull=True
            ).exclude(
                bbox_south__isnull=True
            ).exclude(
                bbox_east__isnull=True
            ).exclude(
                bbox_west__isnull=True
            )

        if not bodies.exists():
            self.stdout.write(self.style.WARNING(
                "No bodies with bounding box data found. "
                "Please add geographic data (bbox_north, bbox_south, bbox_east, bbox_west) to bodies first."
            ))
            return

        self.stdout.write(self.style.SUCCESS(
            f"Processing {bodies.count()} bodies with zoom levels {zoom_min}-{zoom_max}"
        ))

        total_tiles = 0
        total_fetched = 0
        total_cached = 0
        total_errors = 0

        for body in bodies:
            self.stdout.write(f"\n{body.get_display_name()}:")

            # Calculate tiles for bounding box
            tiles = TileCache.tiles_for_bbox(
                float(body.bbox_north),
                float(body.bbox_south),
                float(body.bbox_east),
                float(body.bbox_west),
                zoom_levels=zoom_levels
            )

            total_tiles += len(tiles)
            self.stdout.write(f"  {len(tiles)} tiles to process")

            if options["dry_run"]:
                for z in zoom_levels:
                    z_tiles = [t for t in tiles if t[0] == z]
                    self.stdout.write(f"    Zoom {z}: {len(z_tiles)} tiles")
                continue

            # Fetch tiles
            fetched = 0
            cached = 0
            errors = 0

            with httpx.Client(
                timeout=10.0,
                headers={"User-Agent": "Mandari/1.0 (https://mandari.dev; contact@mandari.dev)"}
            ) as client:
                for i, (z, x, y) in enumerate(tiles):
                    # Check if already cached
                    existing, _ = TileCache.get_tile(z, x, y)
                    if existing:
                        cached += 1
                        continue

                    # Fetch from OSM
                    subdomain = ['a', 'b', 'c'][x % 3]
                    tile_url = f"https://{subdomain}.tile.openstreetmap.org/{z}/{x}/{y}.png"

                    try:
                        response = client.get(tile_url)

                        if response.status_code == 200:
                            TileCache.store_tile(z, x, y, response.content, "image/png", "openstreetmap")
                            fetched += 1
                        else:
                            errors += 1
                            self.stdout.write(
                                self.style.WARNING(f"    HTTP {response.status_code} for {z}/{x}/{y}")
                            )

                    except Exception as e:
                        errors += 1
                        self.stdout.write(self.style.ERROR(f"    Error fetching {z}/{x}/{y}: {e}"))

                    # Progress indicator
                    if (i + 1) % 100 == 0:
                        self.stdout.write(f"    Progress: {i + 1}/{len(tiles)} tiles...")

                    # Rate limiting - OSM policy requires max 2 requests per second
                    time.sleep(0.5)

            total_fetched += fetched
            total_cached += cached
            total_errors += errors

            self.stdout.write(
                f"  Fetched: {fetched}, Already cached: {cached}, Errors: {errors}"
            )

        # Summary
        self.stdout.write("\n" + "=" * 50)
        self.stdout.write(self.style.SUCCESS(f"Summary:"))
        self.stdout.write(f"  Total tiles: {total_tiles}")
        self.stdout.write(f"  Newly fetched: {total_fetched}")
        self.stdout.write(f"  Already cached: {total_cached}")
        self.stdout.write(f"  Errors: {total_errors}")
        self.stdout.write(f"  Cache size: {TileCache.objects.count()} tiles")
