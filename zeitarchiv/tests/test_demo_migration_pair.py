"""Migrationstest-Paare im Demo-Daten-Generator (app/demo_generation.py) —
je eine "alte" Entität, die vor MIGRATION_OLD_STOP_DAYS_BEFORE_NOW Tagen
aufgehört hat zu senden, und eine "neue" mit identischem Typ, die
MIGRATION_NEW_START_DAYS_BEFORE_NOW Tage vor jetzt begonnen hat — bewusst mit
Überlappungsfenster, als Testdaten für app/storage/entity_migration.py. Das
erste Paar hat identische Einheiten (kWh/kWh, testet den additiven Merge und
"Quellwerte übernehmen"), das zweite bewusst unterschiedliche (kWh/Wh, testet
den Umrechnungsfaktor im Migrations-Assistenten)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.demo_generation import (
    APPEND_ANCHOR_ENTITY_ID,
    DEMO_ENTITIES,
    MIGRATION_BACKFILL_DAYS,
    MIGRATION_NEW_START_DAYS_BEFORE_NOW,
    MIGRATION_OLD_STOP_DAYS_BEFORE_NOW,
    MIGRATION_TEST_ENTITY_IDS,
    gen_migration2_new_counter,
    gen_migration2_old_counter,
    gen_migration_new_counter,
    gen_migration_old_counter,
    run_generation,
)
from app.storage.index import Index

TZ = ZoneInfo("Europe/Berlin")


def test_migration_pair_is_registered_with_matching_type_and_unit() -> None:
    by_id = {e.entity_id: e for e in DEMO_ENTITIES}
    old = by_id["sensor.demo_migration_alt_stromverbrauch"]
    new = by_id["sensor.demo_migration_stromverbrauch"]
    assert old.domain == new.domain == "sensor"
    assert old.state_class == new.state_class == "total_increasing"
    assert old.unit == new.unit == "kWh"


def test_second_migration_pair_matches_in_type_but_differs_in_unit() -> None:
    """Das zweite Paar testet den Umrechnungsfaktor — braucht denselben
    aggregation_type (sonst würde main.py es gar nicht als Ziel-Kandidat
    anbieten, siehe candidates-Filter in entity_migrate_page()), aber bewusst
    unterschiedliche Einheiten (sonst zeigte die Vorschau nie das
    migrate-factor-input aus _entity_migrate_preview.html)."""
    by_id = {e.entity_id: e for e in DEMO_ENTITIES}
    old = by_id["sensor.demo_migration2_alt_stromverbrauch"]
    new = by_id["sensor.demo_migration2_stromverbrauch"]
    assert old.domain == new.domain == "sensor"
    assert old.state_class == new.state_class == "total_increasing"
    assert old.unit == "kWh"
    assert new.unit == "Wh"
    assert old.unit != new.unit


def test_migration_test_entity_ids_lists_exactly_both_pairs() -> None:
    """MIGRATION_TEST_ENTITY_IDS steuert, welche Entitäten run_generation()
    bei --append zurücksetzt (siehe dort) — ein vergessenes/falsches Element
    hier würde eine dieser vier Entitäten entweder gar nicht zurücksetzen
    oder eine fremde Entität versehentlich mit-zurücksetzen."""
    assert set(MIGRATION_TEST_ENTITY_IDS) == {
        "sensor.demo_migration_alt_stromverbrauch",
        "sensor.demo_migration_stromverbrauch",
        "sensor.demo_migration2_alt_stromverbrauch",
        "sensor.demo_migration2_stromverbrauch",
    }


def test_old_counter_stops_and_new_counter_overlaps_then_continues_to_now() -> None:
    now = datetime(2026, 9, 19, 12, tzinfo=TZ)
    start = now - timedelta(days=90)
    rng = random.Random(42)

    old_rows = gen_migration_old_counter(start, now, rng)
    new_rows = gen_migration_new_counter(start, now, rng)

    old_stop = now - timedelta(days=MIGRATION_OLD_STOP_DAYS_BEFORE_NOW)
    new_start = now - timedelta(days=MIGRATION_NEW_START_DAYS_BEFORE_NOW)

    assert old_rows
    assert max(ts for ts, _ in old_rows) <= old_stop.timestamp()
    assert new_rows
    assert min(ts for ts, _ in new_rows) < old_stop.timestamp()  # Überlappungsfenster
    assert new_start.timestamp() <= min(ts for ts, _ in new_rows)
    assert max(ts for ts, _ in new_rows) <= now.timestamp()

    old_values = [v for _, v in old_rows]
    new_values = [v for _, v in new_rows]
    assert old_values == sorted(old_values)  # monoton steigender Zähler
    assert new_values == sorted(new_values)


def test_old_counter_yields_nothing_once_start_is_already_past_its_stop_date() -> None:
    """Genau der Fall bei einem --append-Lauf: start liegt dann selbst schon
    nahe "jetzt", also weit nach dem Stichtag der Alt-Entität — sie darf
    keine neuen Zeilen mehr bekommen."""
    now = datetime(2026, 9, 19, 12, tzinfo=TZ)
    start = now - timedelta(hours=1)
    assert gen_migration_old_counter(start, now, random.Random(1)) == []


def test_second_pair_new_counter_values_are_roughly_a_thousand_times_the_old_pairs() -> None:
    """Die Wh-Entität soll wie ein echter Wh-Zähler aussehen (vierstellige
    Werte), nicht nur ein anderes Einheiten-Label an denselben kleinen kWh-
    Zahlen tragen — sonst gäbe es beim Testen des Umrechnungsfaktors nichts
    Plausibles zu vergleichen."""
    now = datetime(2026, 9, 19, 12, tzinfo=TZ)
    start = now - timedelta(days=90)

    old_rows = gen_migration2_old_counter(start, now, random.Random(42))
    new_rows = gen_migration2_new_counter(start, now, random.Random(42))

    old_stop = now - timedelta(days=MIGRATION_OLD_STOP_DAYS_BEFORE_NOW)
    new_start = now - timedelta(days=MIGRATION_NEW_START_DAYS_BEFORE_NOW)

    assert old_rows
    assert max(ts for ts, _ in old_rows) <= old_stop.timestamp()
    assert new_rows
    assert min(ts for ts, _ in new_rows) < old_stop.timestamp()  # Überlappungsfenster
    assert new_start.timestamp() <= min(ts for ts, _ in new_rows)

    old_final = old_rows[-1][1]
    new_final = new_rows[-1][1]
    # Beide laufen ungefähr gleich lange (siehe Zeitfenster oben), die Wh-
    # Reihe wächst aber mit ~1000x größerer Schrittweite — grobe Prüfung auf
    # dieselbe Größenordnung statt eines exakten Faktors (Zufallsstreuung).
    assert new_final > old_final * 500


def test_run_generation_writes_a_type_and_unit_compatible_pair_with_overlap(tmp_path: Path) -> None:
    index = Index(tmp_path / "index.sqlite")
    try:
        run_generation(tmp_path, index, TZ, random.Random(7), months=3)

        old = index.get_entity("sensor.demo_migration_alt_stromverbrauch")
        new = index.get_entity("sensor.demo_migration_stromverbrauch")
        assert old is not None and new is not None
        assert old["aggregation_type"] == new["aggregation_type"]
        assert old["unit"] == new["unit"] == "kWh"

        now = datetime.now(TZ)
        old_stop = now - timedelta(days=MIGRATION_OLD_STOP_DAYS_BEFORE_NOW)
        assert abs(old["last_ts"] - old_stop.timestamp()) < 3600
        assert new["first_ts"] < old["last_ts"]  # Überlappungsfenster tatsächlich geschrieben
        assert now.timestamp() - new["last_ts"] < 3600
    finally:
        index.close()


def _seed_anchor_minutes_ago(index: Index, tz: ZoneInfo, minutes: int) -> None:
    """Setzt nur den Append-Anker, mit einem last_ts, das schon `minutes`
    zurückliegt — für einen --append-Aufruf direkt danach, ohne dass er
    wegen desselben CONTINUOUS_STEP_MINUTES-Fensters als "bereits aktuell"
    übersprungen wird (siehe test_append_backfills_the_migration_pair_on_an_
    environment_that_predates_it weiter unten für dieselbe Notwendigkeit)."""
    now = datetime.now(tz)
    index.get_or_create_entity(APPEND_ANCHOR_ENTITY_ID, "sensor", "measurement", "W")
    index.record_write(APPEND_ANCHOR_ENTITY_ID, (now - timedelta(minutes=minutes)).timestamp(), 100.0)


def test_run_generation_append_always_resets_both_migration_pairs(tmp_path: Path) -> None:
    """Anders als der Rest der Demo-Daten (die --append nur ergänzt) sollen
    die Migrationstest-Paare bei JEDEM --append-Lauf komplett neu aufgesetzt
    werden (siehe MIGRATION_TEST_ENTITY_IDS-Kommentar in demo_generation.py)
    — ein Vollaufbau mit knappem --months gibt der Alt-Entität zunächst nur
    wenige Tage Vorgeschichte; ein anschließender --append-Lauf muss sie
    trotzdem auf die volle MIGRATION_BACKFILL_DAYS-Spanne zurücksetzen,
    nicht bei der schmalen Vorgeschichte des ersten Laufs belassen."""
    index = Index(tmp_path / "index.sqlite")
    try:
        # months=3 (~90 Tage) statt des Standards (36): muss trotzdem über
        # MIGRATION_OLD_STOP_DAYS_BEFORE_NOW (45 Tage) hinausreichen, sonst
        # bekäme die Alt-Entität in DIESEM ersten Lauf schon gar keine Zeile
        # (gen_migration_old_counter() liefert dann eine leere Liste) — die
        # Vorgeschichte bleibt mit ~90 statt der späteren ~1095 Tage trotzdem
        # deutlich "knapp" genug für den folgenden Vergleich.
        run_generation(tmp_path, index, TZ, random.Random(7), months=3)
        old_after_full = index.get_entity("sensor.demo_migration_alt_stromverbrauch")
        old2_after_full = index.get_entity("sensor.demo_migration2_alt_stromverbrauch")

        _seed_anchor_minutes_ago(index, TZ, 10)
        run_generation(tmp_path, index, TZ, random.Random(9), append=True)
        old_after_append = index.get_entity("sensor.demo_migration_alt_stromverbrauch")
        old2_after_append = index.get_entity("sensor.demo_migration2_alt_stromverbrauch")

        backfill_horizon = (datetime.now(TZ) - timedelta(days=MIGRATION_BACKFILL_DAYS - 5)).timestamp()
        for before, after in ((old_after_full, old_after_append), (old2_after_full, old2_after_append)):
            assert after["first_ts"] < backfill_horizon
            # Echter Reset, kein bloßes Anhängen: die neue first_ts liegt
            # deutlich vor der des ersten (knappen) Laufs.
            assert after["first_ts"] < before["first_ts"]
    finally:
        index.close()


def test_run_generation_append_resets_the_migration_pairs_identically_on_repeat(tmp_path: Path) -> None:
    """Zweimal --append hintereinander soll dasselbe (zurückgesetzte) Bild
    liefern, nicht ein mit jedem Lauf wachsendes Überlappungsfenster voller
    leicht verschobener Zeitstempel — genau das macht die Instanz für
    wiederholtes Testen der Migrations-Funktion brauchbar."""
    index = Index(tmp_path / "index.sqlite")
    try:
        _seed_anchor_minutes_ago(index, TZ, 10)
        run_generation(tmp_path, index, TZ, random.Random(1), append=True)
        first_row_count = index.get_entity("sensor.demo_migration_alt_stromverbrauch")["row_count"]

        _seed_anchor_minutes_ago(index, TZ, 10)
        run_generation(tmp_path, index, TZ, random.Random(2), append=True)
        second_row_count = index.get_entity("sensor.demo_migration_alt_stromverbrauch")["row_count"]

        # Gleiche Größenordnung (derselbe Zeitraum, dieselbe Schrittweite) —
        # ein ungewollt additives Verhalten würde hier stattdessen etwa
        # doppelt so viele Zeilen zeigen.
        assert second_row_count < first_row_count * 1.1
    finally:
        index.close()


def test_append_backfills_the_migration_pair_on_an_environment_that_predates_it(tmp_path: Path) -> None:
    """Simuliert eine bereits laufende Demo-Instanz, die vor Einführung des
    Migrationstest-Paars gebaut wurde: nur der Anker (mit einem jüngeren
    last_ts, wie bei einer laufenden Instanz üblich) existiert schon, das
    Migrationstest-Paar dagegen noch gar nicht. Ein einfacher --append-Lauf
    muss das Paar dann trotzdem mit seiner vollen beabsichtigten Vorgeschichte
    nachziehen, nicht nur mit dem schmalen Ergänzungszeitraum seit dem Anker
    (der läge weit NACH dem Stichtag der Alt-Entität und NACH dem Startdatum
    der Neu-Entität). Setzt den Anker direkt statt über einen echten Vollauf
    — zwei run_generation()-Aufrufe kurz hintereinander lägen sonst beide
    innerhalb desselben CONTINUOUS_STEP_MINUTES-Fensters, der zweite würde
    also als "bereits aktuell" komplett übersprungen."""
    index = Index(tmp_path / "index.sqlite")
    try:
        now = datetime.now(TZ)
        index.get_or_create_entity(APPEND_ANCHOR_ENTITY_ID, "sensor", "measurement", "W")
        index.record_write(APPEND_ANCHOR_ENTITY_ID, (now - timedelta(minutes=10)).timestamp(), 100.0)
        assert index.get_entity("sensor.demo_migration_alt_stromverbrauch") is None

        run_generation(tmp_path, index, TZ, random.Random(9), append=True)

        old = index.get_entity("sensor.demo_migration_alt_stromverbrauch")
        new = index.get_entity("sensor.demo_migration_stromverbrauch")
        assert old is not None and new is not None

        old_stop = now - timedelta(days=MIGRATION_OLD_STOP_DAYS_BEFORE_NOW)
        new_start = now - timedelta(days=MIGRATION_NEW_START_DAYS_BEFORE_NOW)
        # Nicht nur der schmale Ergänzungszeitraum seit dem Anker: die
        # Alt-Entität beginnt deutlich vor ihrem eigenen Backfill-Horizont.
        assert old["first_ts"] < (now - timedelta(days=MIGRATION_BACKFILL_DAYS - 5)).timestamp()
        assert abs(old["last_ts"] - old_stop.timestamp()) < 3600
        assert abs(new["first_ts"] - new_start.timestamp()) < 3600
        assert now.timestamp() - new["last_ts"] < 3600
    finally:
        index.close()


def test_append_backfills_even_when_the_pair_was_already_registered_empty(tmp_path: Path) -> None:
    """Regressionsschutz: eine Instanz, deren letzter --append-Lauf vor
    Einführung des Backfills stattfand, hat die Alt-Entität längst per
    get_or_create_entity() REGISTRIERT (write_entity() tut das für jede
    Demo-Entität, auch mit leeren rows) — nur eben ohne je einen Wert
    bekommen zu haben (first_ts NULL), weil der damalige schmale
    Ergänzungszeitraum für sie leer ausging. "ist schon registriert" wäre
    für so eine Instanz für immer wahr; der Trigger muss stattdessen auf
    "hat schon einen Wert" (first_ts) prüfen, sonst backfillt ein weiterer
    --append-Lauf nie."""
    index = Index(tmp_path / "index.sqlite")
    try:
        now = datetime.now(TZ)
        index.get_or_create_entity(APPEND_ANCHOR_ENTITY_ID, "sensor", "measurement", "W")
        index.record_write(APPEND_ANCHOR_ENTITY_ID, (now - timedelta(minutes=10)).timestamp(), 100.0)
        # Registriert, aber nie ein Wert geschrieben — genau der Zustand nach
        # einem alten --append-Lauf, dessen schmales Fenster für die
        # Alt-Entität leer war.
        index.get_or_create_entity("sensor.demo_migration_alt_stromverbrauch", "sensor", "total_increasing", "kWh")
        index.get_or_create_entity("sensor.demo_migration_stromverbrauch", "sensor", "total_increasing", "kWh")
        assert index.get_entity("sensor.demo_migration_alt_stromverbrauch")["first_ts"] is None

        run_generation(tmp_path, index, TZ, random.Random(9), append=True)

        old = index.get_entity("sensor.demo_migration_alt_stromverbrauch")
        assert old["first_ts"] is not None
        assert old["first_ts"] < (now - timedelta(days=MIGRATION_BACKFILL_DAYS - 5)).timestamp()
    finally:
        index.close()
