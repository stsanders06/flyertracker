---
name: feedback-tjunction-fix
description: How street segmentation works and the bug that was fixed — junction detection used int vs str comparison that was always True
metadata:
  type: feedback
---

The `split_ways_to_segments` function in `flyertracker/app/app.py` had a critical type mismatch bug:
`all_coords` stored `(position_type, way_idx)` where `way_idx` was an **integer** (from `enumerate`), but the junction check compared it against `way['id']` which is an **OSM string ID**. In Python 3 `int != str` is always `True`, so every intermediate node was flagged as a junction, creating thousands of spurious 2-point segments.

**Fix applied (v2.0.4):** Replaced `all_coords` with `coord_to_ways: dict[tuple, set[int]]` mapping each coordinate to the set of integer way indices that use it. A node is a junction if `len(coord_to_ways[coord]) > 1` (shared across multiple ways). Sharp bends still split at < 100°.

**Cache versioning added:** `CACHE_VERSION = "2"` constant in `app.py`. `get_streets_geojson()` reads the `cache_version` field from the cached JSON and rebuilds if it doesn't match. Bump `CACHE_VERSION` whenever segmentation logic changes.

**Why:** The stale cache (`/data/streets_cache.json`) was built before segmentation code existed and was never invalidated (only bbox changes triggered cache busting). The fix ensures future logic changes automatically bust the cache.

**How to apply:** If segmentation behavior changes again, increment `CACHE_VERSION` — the next app startup will fetch fresh OSM data and re-segment.
