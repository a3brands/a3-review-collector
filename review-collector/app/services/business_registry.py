"""Every dealership the collector actually works on.

`Settings.businesses()` is NOT this list. It returns only the two dealerships
with PLACE_IDs in .env (bmw_fwb, mb_fwb), which is correct for what it is used
for -- seeding and env-fallback -- and wrong for anything that means "all our
clients". Collection itself has enumerated the Business table for a long time
(collection_service.py), so the two have been diverging ever since the third
dealership was added at /admin.

The cost of that is a script which reports success over a third of the estate
and looks like it passed: scripts/diagnose_maps.py checked 2 of 6 dealerships
and said nothing about the other 4. Anything answering a question about "the
clients" should call this instead.
"""
from __future__ import annotations

from typing import List

from app.config import BusinessConfig
from app.database.database import session_scope
from app.database.models import Business


def active_business_configs() -> List[BusinessConfig]:
    """Every active dealership in the database, as BusinessConfig objects.

    Ordered by key so output is stable between runs and two reports can be
    diffed against each other.
    """
    with session_scope() as session:
        rows = (
            session.query(Business)
            .filter_by(active=True)
            .order_by(Business.key)
            .all()
        )
        return [
            BusinessConfig(
                key=row.key,
                name=row.name,
                google_url=row.google_url,
                place_id=row.place_id,
                gbp_location_name=row.gbp_location_name,
            )
            for row in rows
        ]
