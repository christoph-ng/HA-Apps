"""End-to-end Route-Tests für den Migrations-Assistenten (app/main.py
entity_migrate_page/entity_migrate_preview/entity_migrate_execute) — die
eigentliche Migrations-/Overwrite-Logik ist bereits ausführlich in
test_entity_migration.py abgedeckt (isoliert, per tmp_path); hier geht es nur
um die Verdrahtung: Query-Param-Vorbelegung, Validierung von
overlap_resolution und dass die Ausführung über die echte Route denselben
Effekt hat wie ein direkter execute_migration()-Aufruf.

client ist session-scoped (siehe conftest.py) mit EINEM gemeinsamen
DATA_DIR/index für die ganze Testsitzung — Entity-IDs hier deshalb mit einem
eigenen "migrate_route_"-Präfix, um keine Entitäten anderer Testdateien zu
berühren."""

from __future__ import annotations

import pyarrow as pa
import pyarrow.parquet as pq


def _write_archive_month(entity_id: str, label: str, rows: list[tuple[float, float]]) -> None:
    from app.main import DATA_DIR

    archive_dir = DATA_DIR / "archive" / entity_id
    archive_dir.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.table({"ts": [r[0] for r in rows], "value": [r[1] for r in rows]}),
        archive_dir / f"{label}.parquet",
    )


def _make_entity(entity_id: str, domain: str, state_class: str, unit: str, rows: list[tuple[float, float]]) -> None:
    from datetime import datetime

    from app.main import TZ, index

    index.get_or_create_entity(entity_id, domain, state_class, unit)
    by_month: dict[str, list[tuple[float, float]]] = {}
    for ts, value in rows:
        by_month.setdefault(datetime.fromtimestamp(ts, TZ).strftime("%Y-%m"), []).append((ts, value))
    for label, month_rows in by_month.items():
        _write_archive_month(entity_id, label, month_rows)
    for ts, value in sorted(rows):
        index.record_write(entity_id, ts, value)


def _ts(y: int, m: int, d: int) -> float:
    from datetime import datetime

    from app.main import TZ

    return datetime(y, m, d, 12, tzinfo=TZ).timestamp()


def test_migrate_page_lists_candidates_with_stats(client) -> None:
    _make_entity("sensor.migrate_route_a", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_b", "sensor", "measurement", "°C", [(_ts(2021, 1, 2), 2.0)])
    resp = client.get("/entities/sensor.migrate_route_a/migrate")
    assert resp.status_code == 200
    assert "sensor.migrate_route_b" in resp.text
    assert '"type_label"' in resp.text
    assert "const migrateTargetOptions" in resp.text


def test_migrate_page_filters_candidates_to_the_same_aggregation_type(client) -> None:
    """Eine Migration zwischen unterschiedlichen Zähltypen unterstützt
    entity_migration.py ohnehin nicht (IncompatibleTypesError) — die
    Zielauswahl darf einen inkompatiblen Typ deshalb erst gar nicht anbieten,
    statt es erst in der Vorschau als Fehler zu melden."""
    _make_entity("sensor.migrate_route_type_a", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_type_b", "sensor", "total_increasing", "kWh", [(_ts(2021, 1, 2), 2.0)])
    resp = client.get("/entities/sensor.migrate_route_type_a/migrate")
    assert resp.status_code == 200
    assert "sensor.migrate_route_type_b" not in resp.text


def test_migrate_page_prefills_target_from_query_param(client) -> None:
    _make_entity("sensor.migrate_route_c", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_d", "sensor", "measurement", "°C", [(_ts(2021, 1, 2), 2.0)])
    resp = client.get("/entities/sensor.migrate_route_c/migrate?target=sensor.migrate_route_d")
    assert resp.status_code == 200
    # entityPicker() bekommt die vorbelegte Ziel-ID als zweites Argument — und
    # das umschließende x-data MUSS einfach angeführt sein: tojson liefert
    # dafür echte Anführungszeichen (gültiges JSON), die ein doppelt
    # angeführtes x-data="..." mitten im Attributwert vorzeitig beendet
    # hätten (siehe entity_field() in _energiedashboard_setup.html, das aus
    # genau diesem Grund x-data='...' verwendet).
    assert "x-data='{...entityPicker(migrateTargetOptions, \"sensor.migrate_route_d\")" in resp.text


def test_migrate_page_ignores_unknown_target_query_param(client) -> None:
    _make_entity("sensor.migrate_route_e", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    resp = client.get("/entities/sensor.migrate_route_e/migrate?target=sensor.does_not_exist")
    assert resp.status_code == 200
    assert "entityPicker(migrateTargetOptions, \"\")" in resp.text


def test_migrate_preview_rejects_invalid_overlap_resolution(client) -> None:
    _make_entity("sensor.migrate_route_f", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_g", "sensor", "measurement", "°C", [(_ts(2021, 1, 2), 2.0)])
    resp = client.post(
        "/entities/sensor.migrate_route_f/migrate/preview",
        json={"target_entity_id": "sensor.migrate_route_g", "overlap_resolution": "bogus"},
    )
    assert resp.status_code == 400


def test_migrate_preview_prefills_the_suggested_factor_on_unit_mismatch(client) -> None:
    _make_entity("sensor.migrate_route_factor_a", "sensor", "measurement", "kWh", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_factor_b", "sensor", "measurement", "Wh", [(_ts(2021, 1, 2), 2.0)])
    resp = client.post(
        "/entities/sensor.migrate_route_factor_a/migrate/preview",
        json={"target_entity_id": "sensor.migrate_route_factor_b"},
    )
    assert resp.status_code == 200
    assert 'id="migrate-factor" value="1.000"' in resp.text
    assert "automatisch vorgeschlagen" in resp.text


def test_migrate_preview_does_not_override_an_already_recalculated_factor(client) -> None:
    """Ein bereits per "Neu berechnen" gesetzter eigener Faktor (factor !=
    1.0 im Request) darf nicht durch den Vorschlag überschrieben werden."""
    _make_entity("sensor.migrate_route_factor_c", "sensor", "measurement", "kWh", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_factor_d", "sensor", "measurement", "Wh", [(_ts(2021, 1, 2), 2.0)])
    resp = client.post(
        "/entities/sensor.migrate_route_factor_c/migrate/preview",
        json={"target_entity_id": "sensor.migrate_route_factor_d", "factor": 5.0},
    )
    assert resp.status_code == 200
    assert 'id="migrate-factor" value="5"' in resp.text
    assert "automatisch vorgeschlagen" not in resp.text


def test_migrate_preview_shows_overlap_radio_checked_for_source(client) -> None:
    overlap_ts = _ts(2021, 2, 10)
    _make_entity("sensor.migrate_route_h", "sensor", "measurement", "°C", [(overlap_ts, 5.0)])
    _make_entity("sensor.migrate_route_i", "sensor", "measurement", "°C", [(overlap_ts, 9.0)])
    resp = client.post(
        "/entities/sensor.migrate_route_h/migrate/preview",
        json={"target_entity_id": "sensor.migrate_route_i", "overlap_resolution": "source"},
    )
    assert resp.status_code == 200
    assert 'value="source" checked' in resp.text
    assert "Quell-Werte übernehmen" in resp.text


def test_migrate_execute_rejects_invalid_overlap_resolution(client) -> None:
    _make_entity("sensor.migrate_route_j", "sensor", "measurement", "°C", [(_ts(2021, 1, 1), 1.0)])
    _make_entity("sensor.migrate_route_k", "sensor", "measurement", "°C", [(_ts(2021, 1, 2), 2.0)])
    resp = client.post(
        "/entities/sensor.migrate_route_j/migrate",
        json={"target_entity_id": "sensor.migrate_route_k", "post_action": "keep", "overlap_resolution": "bogus"},
    )
    assert resp.status_code == 400


def test_migrate_execute_with_overlap_resolution_source_overwrites_via_the_real_route(client) -> None:
    from app.main import DATA_DIR

    overlap_ts = _ts(2021, 3, 10)
    _make_entity("sensor.migrate_route_l", "sensor", "measurement", "°C", [(overlap_ts, 999.0)])
    _make_entity("sensor.migrate_route_m", "sensor", "measurement", "°C", [(overlap_ts, 1.0)])

    resp = client.post(
        "/entities/sensor.migrate_route_l/migrate",
        json={
            "target_entity_id": "sensor.migrate_route_m",
            "post_action": "keep",
            "overlap_resolution": "source",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overlap_resolution"] == "source"
    assert body["overwritten_rows"] == 1
    assert body["duplicate_rows"] == 1

    table = pq.read_table(DATA_DIR / "archive" / "sensor.migrate_route_m" / "2021-03.parquet")
    values_by_ts = dict(zip(table.column("ts").to_pylist(), table.column("value").to_pylist()))
    assert values_by_ts[overlap_ts] == 999.0
