"""
Auto-match Tigris product-docs → Product rows with tiered confidence scoring.

Inventories the Tigris bucket, extracts model/SKU/brand tokens from key
+ optional first-page PDF text, scores each key against every Product,
writes HIGH-confidence rows to ProductDocument (is_verified=False, audit
fields filled) and emits a CSV review queue for the rest.

Idempotent: re-running does not duplicate rows (unique on r2_key); existing
rows keep their verification state unless --force is given.
Do NOT auto-verify — every created row is is_verified=False and must be
human-approved elsewhere.

Usage:
    python manage.py auto_match_docs                            # real run (writes DB + review.csv)
    python manage.py auto_match_docs --dry-run                  # preview only
    python manage.py auto_match_docs --review-csv /tmp/queue.csv
    python manage.py auto_match_docs --limit 200 --bucket product-docs
    python manage.py auto_match_docs --no-pdf-text              # score on key tokens only (fast / offline)
    python manage.py auto_match_docs --force                    # re-score even already-matched r2_keys
"""

from __future__ import annotations

import csv
import logging
import os
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from core.csv_safety import csv_safe

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = os.environ.get("PRODUCT_DOCS_BUCKET") or os.environ.get("BUCKET_NAME") or "product-docs"
DEFAULT_TIGRIS_ENDPOINT = os.environ.get("AWS_ENDPOINT_URL_S3") or "https://fly.storage.tigris.dev"


def _r2_url_for_key(key: str) -> str:
    """Best-effort public URL for an r2_key."""
    base = os.environ.get("R2_PUBLIC_BASE") or os.environ.get("R2_BASE") or ""
    if not base:
        return key
    base = base.rstrip("/")
    return f"{base}/{key.lstrip('/')}"


def _build_tigris_client():
    """Build a boto3 S3 client pointed at Tigris.

    Reads the same env vars as apps/worker-fly/src/storage.ts:
      AWS_ENDPOINT_URL_S3, AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    Returns None when creds are absent or boto3 is not installed.
    """
    try:
        import boto3  # type: ignore
    except ImportError:
        return None

    endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or DEFAULT_TIGRIS_ENDPOINT
    region = os.environ.get("AWS_REGION") or "auto"
    access = os.environ.get("AWS_ACCESS_KEY_ID") or ""
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or ""
    if not access or not secret:
        return None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
    )


def _load_index():
    """Load products + variants from the DB into matcher IndexEntry dicts."""
    from products.models import Product, ProductVariant  # noqa: PLC0415

    # One query for products + brand, one for variants, then assemble in python.
    products = list(
        Product.objects.select_related("brand").all()
    )
    # Map product_id -> {skus, mpns}
    variant_rows = list(
        ProductVariant.objects.values("product_id", "sku", "mpn")
    )
    by_product: dict[object, dict[str, list[str]]] = {}
    for r in variant_rows:
        pid = r["product_id"]
        slot = by_product.setdefault(pid, {"skus": [], "mpns": []})
        if r["sku"]:
            slot["skus"].append(r["sku"])
        if r["mpn"]:
            slot["mpns"].append(r["mpn"])

    entries: list[dict] = []
    for p in products:
        slot = by_product.get(p.id, {"skus": [], "mpns": []})
        brand_title = p.brand.title if p.brand else ""
        brand_slug = p.brand.slug if p.brand else ""
        entries.append({
            "product_id": p.id,
            "product_title": p.title or "",
            "product_slug": p.slug or "",
            "brand_title": brand_title,
            "brand_slug": brand_slug,
            "part_number": p.part_number or "",
            "product_mpn": p.mpn or "",
            "variant_skus": slot["skus"],
            "variant_mpns": slot["mpns"],
        })
    return entries


class Command(BaseCommand):
    help = "Inventory Tigris product-docs and auto-match each key to a product with confidence scoring."

    def add_arguments(self, parser):
        parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="Tigris bucket (default: %(default)s)")
        parser.add_argument("--prefix", default="", help="Only keys under this prefix")
        parser.add_argument("--limit", type=int, default=None, help="Cap the number of keys processed (for canary runs)")
        parser.add_argument("--review-csv", default="review_queue.csv", help="Path for the CSV review queue (default: %(default)s)")
        parser.add_argument("--dry-run", action="store_true", help="Preview only; do not write to the DB or emit the review CSV")
        parser.add_argument("--no-pdf-text", action="store_true", help="Skip PDF first-page text extract; score on key tokens only")
        parser.add_argument("--force", action="store_true", help="Re-score keys that already have a ProductDocument row")
        parser.add_argument("--min-bytes", type=int, default=1, help="Skip objects smaller than this (default: %(default)s)")
        parser.add_argument(
            "--high-threshold",
            type=float,
            default=None,
            help="Override HIGH confidence cut (default from matcher.HIGH_CONFIDENCE_MIN)",
        )

    def handle(self, *args, **options):
        bucket: str = options["bucket"]
        prefix: str = options["prefix"] or ""
        limit: int | None = options["limit"]
        review_csv: str = options["review_csv"]
        dry_run: bool = options["dry_run"]
        no_pdf: bool = options["no_pdf_text"]
        force: bool = options["force"]
        min_bytes: int = options["min_bytes"]

        from documents.matcher import (  # noqa: PLC0415
            HIGH_CONFIDENCE_MIN,
            REVIEW_CONFIDENCE_MIN,
            best_match,
            build_index,
            infer_doc_type,
        )
        from documents.models import ProductDocument  # noqa: PLC0415
        from documents.text_extract import extract_first_page_text  # noqa: PLC0415
        from documents.tigris_inventory import (  # noqa: PLC0415
            TigrisObject,
            fetch_first_bytes,
            list_bucket_keys,
        )

        high_min = options["high_threshold"] if options["high_threshold"] is not None else HIGH_CONFIDENCE_MIN

        self.stdout.write(f"auto_match_docs — bucket={bucket!r} prefix={prefix!r} limit={limit} dry_run={dry_run} no_pdf_text={no_pdf}")

        # ---- build matcher index from DB ----
        raw_entries = _load_index()
        self.stdout.write(f"  products in index: {len(raw_entries)}")
        index = build_index(raw_entries)
        self.stdout.write(f"  index entries built: {len(index)}")

        # ---- inventory ----
        client = _build_tigris_client()
        if client is None:
            self.stderr.write(self.style.WARNING(
                "No Tigris credentials (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY) or boto3 not installed.\n"
                "  Falling back to the existing documents_productdocument rows as the 'inventory'.\n"
                "  To inventory the real bucket, set the Fly Tigris secrets and install boto3."
            ))
            # Fallback: treat existing rows as inventory so dry-runs/tests stay useful.
            # Fallback objects carry no size, so --min-bytes cannot filter here.
            objs_by_key: dict[str, TigrisObject] = {
                row: TigrisObject(key=row)
                for row in ProductDocument.objects.values_list("r2_key", flat=True)
                if row
            }
            self.stdout.write(f"  fallback inventory: {len(objs_by_key)} existing r2_keys")
            if prefix:
                objs_by_key = {k: v for k, v in objs_by_key.items() if k.startswith(prefix)}
                self.stdout.write(f"  after prefix filter: {len(objs_by_key)}")
            if limit is not None:
                keys = sorted(objs_by_key.keys())[:limit]
                objs_by_key = {k: objs_by_key[k] for k in keys}
            tigris_objs = [objs_by_key[k] for k in sorted(objs_by_key.keys())]
        else:
            self.stdout.write(f"  listing s3://{bucket}/{prefix or ''} ...")
            tigris_objs = list_bucket_keys(client, bucket, prefix=prefix, max_keys=limit)
            self.stdout.write(f"  listed {len(tigris_objs)} objects")
            if min_bytes > 1:
                before = len(tigris_objs)
                tigris_objs = [o for o in tigris_objs if (o.size or 0) >= min_bytes]
                self.stdout.write(f"  after --min-bytes {min_bytes}: {len(tigris_objs)} (dropped {before - len(tigris_objs)})")
            if limit is not None and len(tigris_objs) > limit:
                tigris_objs = tigris_objs[:limit]

        if not tigris_objs:
            self.stdout.write(self.style.WARNING("No objects to score — nothing to do."))
            if not dry_run:
                self._write_empty_review(review_csv, REVIEW_CONFIDENCE_MIN)
            return

        # ---- skip already-matched keys (unless --force) ----
        existing_keys: set[str] = set()
        if not force:
            existing_keys = set(
                ProductDocument.objects.filter(r2_key__in=[o.key for o in tigris_objs]).values_list("r2_key", flat=True)
            )
            if existing_keys:
                self.stdout.write(f"  already matched (skipped, use --force to re-score): {len(existing_keys)}")

        # ---- score each key ----
        rows_high: list[dict] = []     # will become ProductDocument rows
        rows_review: list[dict] = []   # review CSV rows
        rows_unassigned: list[str] = []  # no candidate at all

        for obj in tigris_objs:
            key: str = obj.key or ""
            if not key:
                continue
            if not force and key in existing_keys:
                continue

            pdf_text = ""
            if not no_pdf and client is not None:
                pdf_bytes = fetch_first_bytes(client, bucket, key)
                if pdf_bytes:
                    pdf_text = extract_first_page_text(pdf_bytes)

            hit = best_match(key, pdf_text, index)
            if hit is None:
                rows_unassigned.append(key)
                rows_review.append({
                    "r2_key": key,
                    "file_size": getattr(obj, "size", ""),
                    "product_id": "",
                    "product_title": "",
                    "brand": "",
                    "confidence": "0.00",
                    "match_type": "no_match",
                    "matched_token": "",
                    "reason": "no candidate",
                    "suggested_action": "manual_review",
                })
                continue

            if hit.confidence >= high_min:
                rows_high.append({
                    "r2_key": key,
                    "file_size": getattr(obj, "size", None),
                    "product_id": hit.product_id,
                    "product_title": hit.product_title,
                    "brand": hit.brand_title,
                    "confidence": f"{hit.confidence:.2f}",
                    "match_type": hit.match_type,
                    "matched_token": hit.matched_token,
                    "matched_sources": ",".join(hit.matched_sources),
                    "reason": hit.reason,
                    "hit": hit,
                })
            elif hit.confidence >= REVIEW_CONFIDENCE_MIN:
                rows_review.append({
                    "r2_key": key,
                    "file_size": getattr(obj, "size", ""),
                    "product_id": str(hit.product_id),
                    "product_title": hit.product_title,
                    "brand": hit.brand_title,
                    "confidence": f"{hit.confidence:.2f}",
                    "match_type": hit.match_type,
                    "matched_token": hit.matched_token,
                    "matched_sources": ",".join(hit.matched_sources),
                    "reason": hit.reason,
                    "suggested_action": "review",
                })
            else:
                # Below review threshold — still queue, but flagged low.
                rows_review.append({
                    "r2_key": key,
                    "file_size": getattr(obj, "size", ""),
                    "product_id": str(hit.product_id),
                    "product_title": hit.product_title,
                    "brand": hit.brand_title,
                    "confidence": f"{hit.confidence:.2f}",
                    "match_type": hit.match_type,
                    "matched_token": hit.matched_token,
                    "matched_sources": ",".join(hit.matched_sources),
                    "reason": hit.reason,
                    "suggested_action": "review_low_confidence",
                })

        # ---- summary ----
        self.stdout.write(self.style.SUCCESS(
            f"  scored {len(tigris_objs)} keys: "
            f"HIGH={len(rows_high)} review={len(rows_review)} "
            f"unassigned_no_match={len([r for r in rows_review if r.get('match_type') == 'no_match'])}"
        ))

        if dry_run:
            shown = 0
            for r in rows_high[:20]:
                self.stdout.write(f"    HIGH  {r['confidence']} {r['match_type']:12} {r['r2_key']!r} -> {r['product_title']!r} ({r['matched_token']})")
                shown += 1
            for r in rows_review[:20]:
                self.stdout.write(f"    REVIEW {r['confidence']} {r['match_type']:12} {r['r2_key']!r} -> {r['product_title'] or '(none)'} ({r['matched_token'] or r['reason']})")
                shown += 1
            if shown == 0 and rows_unassigned:
                for k in rows_unassigned[:20]:
                    self.stdout.write(f"    NO_MATCH {k!r}")
            self.stdout.write(self.style.WARNING("Dry run — no DB writes, no CSV emitted."))
            return

        # ---- write HIGH-confidence rows (idempotent) ----
        created = 0
        updated = 0  # --force re-scored rows that already existed
        skipped = 0
        errors: list[str] = []
        if rows_high:
            for r in rows_high:
                hit = r["hit"]
                doc_type = infer_doc_type(r["r2_key"])
                title = r["r2_key"].split("/")[-1] or r["r2_key"]
                # Idempotent: key already checked via existing_keys unless --force.
                # With --force we upsert; without, get_or_create.
                defaults = {
                    "product_id": hit.product_id,
                    "document_type": doc_type,
                    "title": title[:255],
                    "r2_url": _r2_url_for_key(r["r2_key"]),
                    "file_size": r["file_size"],
                    "match_confidence": Decimal(r["confidence"]),
                    "match_method": ProductDocument.MatchMethod.AUTO_MATCH,
                    "matched_token": hit.matched_token[:200],
                    "match_details": hit.detail_dict(),
                    # Never auto-verify.
                    "is_verified": False,
                }
                try:
                    with transaction.atomic():
                        if force:
                            # Preserve verification state on existing rows: never un-verify
                            # a row a human approved.
                            prev_verified = False
                            existing = (
                                ProductDocument.objects.filter(r2_key=r["r2_key"])
                                .values_list("is_verified", flat=True)
                                .first()
                            )
                            if existing is not None:
                                prev_verified = bool(existing)
                            obj_, created_ = ProductDocument.objects.update_or_create(
                                r2_key=r["r2_key"],
                                defaults=defaults,
                            )
                            if not created_ and prev_verified:
                                ProductDocument.objects.filter(pk=obj_.pk).update(
                                    is_verified=True
                                )
                            if created_:
                                created += 1
                            else:
                                updated += 1
                        else:
                            _, was_created = ProductDocument.objects.get_or_create(
                                r2_key=r["r2_key"],
                                defaults=defaults,
                            )
                            if was_created:
                                created += 1
                            else:
                                skipped += 1
                except Exception as exc:
                    errors.append(f"{r['r2_key']}: {exc}")

            self.stdout.write(self.style.SUCCESS(f"  ProductDocument: created={created} updated={updated} skipped_existing={skipped} errors={len(errors)}"))
            for e in errors[:20]:
                self.stderr.write(f"    error: {e}")
        else:
            self.stdout.write("  no HIGH-confidence matches — no ProductDocument rows created")

        # ---- emit CSV review queue (REVIEW + unassigned only; HIGH rows already in DB) ----
        self._write_review_csv(review_csv, rows_review)
        self.stdout.write(self.style.SUCCESS(f"  review queue: {len(rows_review)} rows -> {review_csv}"))

        # ---- signal already handled by per-row saves (post_save refresh); for
        # fallback paths or if signals were missed, the 5-min cron is the
        # backstop, so no manual queue_refresh_* is needed here.
        # ----

    def _write_empty_review(self, path: str, _min: float):
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["r2_key", "file_size", "product_id", "product_title", "brand", "confidence", "match_type", "matched_token", "matched_sources", "reason", "suggested_action"])
        except Exception:
            logger.exception("Failed to write empty review CSV to %r", path)

    def _write_review_csv(self, path: str, rows: list[dict]):
        fieldnames = ["r2_key", "file_size", "product_id", "product_title", "brand", "confidence", "match_type", "matched_token", "matched_sources", "reason", "suggested_action"]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                # Ensure file_size is string/number; Decimal rows may have hit obj.
                if "hit" in r:
                    r = {k: v for k, v in r.items() if k != "hit"}
                w.writerow({k: csv_safe(v) for k, v in r.items()})
