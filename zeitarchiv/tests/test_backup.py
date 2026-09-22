"""Tests für app/storage/backup.py — welche Dateien ins Backup-ZIP
gehören (und welche bewusst nicht), Fortschritts-Callback, atomares Schreiben."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from datetime import datetime
from pathlib import Path


from app.storage import backup
from app.storage.coordinator import StorageCoordinator
from app.storage.index import Index


def _write(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_create_backup_includes_archive_rollup_hot_index_and_names() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "parquet-a")
        _write(tmp / "rollup" / "sensor.a" / "monat.parquet", "rollup-a")
        _write(tmp / "hot" / "sensor.a-2026-08.csv", "1,2\n")
        _write(tmp / "reports" / "import" / "2026" / "report.json", "{}")
        _write(tmp / "symcon_names.json", '{"1": {"name": "x", "parent": null}}')

        dest = tmp / "out" / "backup.zip"
        backup.create_backup(tmp, dest)

        assert dest.exists()
        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
        assert names == {
            "index.sqlite",
            "archive/sensor.a/2024-01.parquet",
            "rollup/sensor.a/monat.parquet",
            "hot/sensor.a-2026-08.csv",
            "reports/import/2026/report.json",
            "symcon_names.json",
            "zeitarchiv-manifest.json",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_backup_excludes_import_staging_and_server_log() -> None:
    """symcon_import/ und csv_import/ sind temporäre, potenziell riesige
    Einlese-Ordner (kein Teil des Archivs, jederzeit neu hochladbar) —
    server.log ist reine Diagnose. Keins von beiden gehört ins Backup."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        _write(tmp / "symcon_import" / "db" / "2024" / "01" / "123.csv", "1700000000,1\n")
        _write(tmp / "csv_import" / "upload.csv", "1700000000,1\n")
        _write(tmp / "server.log", "some log line\n")

        dest = tmp / "backup.zip"
        backup.create_backup(tmp, dest)

        with zipfile.ZipFile(dest) as zf:
            names = set(zf.namelist())
        assert names == {"index.sqlite", "zeitarchiv-manifest.json"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_backup_handles_missing_entries_gracefully() -> None:
    """Nicht jede Installation hat z. B. schon rollup/ oder symcon_names.json
    — fehlende Einträge werden einfach übersprungen, kein Fehler."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        dest = tmp / "backup.zip"
        backup.create_backup(tmp, dest)
        with zipfile.ZipFile(dest) as zf:
            assert zf.namelist() == ["index.sqlite", "zeitarchiv-manifest.json"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_backup_reports_progress_for_every_file() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "a")
        _write(tmp / "archive" / "sensor.a" / "2024-02.parquet", "b")
        _write(tmp / "archive" / "sensor.b" / "2024-01.parquet", "c")

        calls: list[tuple[int, int]] = []
        backup.create_backup(tmp, tmp / "backup.zip", on_progress=lambda done, total: calls.append((done, total)))

        assert calls == [(1, 3), (2, 3), (3, 3)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_backup_writes_atomically_via_part_file() -> None:
    """Schreibt zuerst in eine .part-Datei und benennt erst bei Erfolg um —
    ein Blick mitten im Schreiben darf nie ein halbfertiges ZIP zeigen, das
    wie ein vollständiges Backup aussieht."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        dest = tmp / "backup.zip"

        seen_part_exists = []

        def on_progress(done, total):
            seen_part_exists.append(dest.with_suffix(dest.suffix + ".part").exists())
            seen_part_exists.append(dest.exists())

        backup.create_backup(tmp, dest, on_progress=on_progress)

        # Während des Schreibens existierte nur die .part-Datei, nicht das Ziel.
        assert seen_part_exists == [True, False]
        assert dest.exists()
        assert not dest.with_suffix(dest.suffix + ".part").exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_estimate_file_count_matches_actual_backup_file_count() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        _write(tmp / "index.sqlite", "x")
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "x")
        _write(tmp / "archive" / "sensor.a" / "2024-02.parquet", "x")
        assert backup.estimate_file_count(tmp) == 3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_consistent_sqlite_uses_sqlite_backup_api() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('consistent')")
        conn.commit()
        conn.close()

        dest = tmp / "backup.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)
        extracted = tmp / "restored.sqlite"
        with zipfile.ZipFile(dest) as zf:
            extracted.write_bytes(zf.read("index.sqlite"))
        restored = sqlite3.connect(extracted)
        try:
            assert restored.execute("SELECT value FROM sample").fetchone()[0] == "consistent"
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()
        assert not list(tmp.glob("*.sqlite-snapshot"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_snapshot_contains_stable_backup_entries() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-snapshot-test-"))
    index = Index(tmp / "index.sqlite")
    try:
        index.get_or_create_entity("sensor.a", "sensor", "measurement", "°C")
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "archive")
        _write(tmp / "rollup" / "sensor.a" / "monat.parquet", "rollup")
        _write(tmp / "hot" / "sensor.a-2026-08.csv", "1,2\n")
        _write(tmp / "reports" / "import" / "2026" / "report.json", "{}")
        _write(tmp / "symcon_names.json", "{}")

        snapshot = tmp / "snapshot"
        backup.create_source_snapshot(
            tmp,
            snapshot,
            ["sensor.a"],
            StorageCoordinator(),
        )

        assert (snapshot / "archive" / "sensor.a" / "2024-01.parquet").read_text() == "archive"
        assert (snapshot / "rollup" / "sensor.a" / "monat.parquet").read_text() == "rollup"
        assert (snapshot / "hot" / "sensor.a-2026-08.csv").read_text() == "1,2\n"
        assert (snapshot / "reports" / "import" / "2026" / "report.json").is_file()
        restored = sqlite3.connect(snapshot / "index.sqlite")
        try:
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            restored.close()
    finally:
        index.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_snapshot_reports_progress_per_entity() -> None:
    """on_entity_done ist das Lebenszeichen für _last_backup_worker_tick in
    main.py (siehe Roadmap "Neu seit 0.76.1", Punkt 1) — muss nach jeder
    Entität aufgerufen werden, nicht erst am Ende."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-snapshot-test-"))
    index = Index(tmp / "index.sqlite")
    try:
        index.get_or_create_entity("sensor.a", "sensor", "measurement", "°C")
        index.get_or_create_entity("sensor.b", "sensor", "measurement", "°C")
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "a")
        _write(tmp / "archive" / "sensor.b" / "2024-01.parquet", "b")

        calls: list[tuple[int, int]] = []
        backup.create_source_snapshot(
            tmp,
            tmp / "snapshot",
            ["sensor.a", "sensor.b"],
            StorageCoordinator(),
            on_entity_done=lambda done, total: calls.append((done, total)),
        )

        assert calls == [(1, 2), (2, 2)]
    finally:
        index.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_cleanup_stale_source_snapshots_only_removes_internal_directories() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-cleanup-test-"))
    try:
        stale = tmp / ".backup-source-42-a1b2c3d4e5f6"
        stale.mkdir()
        _write(stale / "partial", "discard")
        unrelated = tmp / ".backup-source-not-ours"
        unrelated.mkdir()
        _write(unrelated / "keep", "keep")

        assert backup.cleanup_stale_source_snapshots(tmp) == 1
        assert not stale.exists()
        assert (unrelated / "keep").read_text() == "keep"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_source_snapshot_does_not_block_other_entities_during_large_copy() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-lock-test-"))
    index = Index(tmp / "index.sqlite")
    coordinator = StorageCoordinator()
    copy_started = threading.Event()
    release_copy = threading.Event()
    other_entity_acquired = threading.Event()
    original_copy2 = backup.shutil.copy2

    def blocking_copy(source, destination, *args, **kwargs):
        # copytree reicht für ``source`` ein DirEntry ohne Elternpfad durch;
        # der Zielpfad enthält die Entitäts-ID zuverlässig.
        if "sensor.a" in str(destination):
            copy_started.set()
            if not release_copy.wait(timeout=5):
                raise TimeoutError("Testkopie wurde nicht freigegeben")
        return original_copy2(source, destination, *args, **kwargs)

    try:
        index.get_or_create_entity("sensor.a", "sensor", "measurement", None)
        index.get_or_create_entity("sensor.b", "sensor", "measurement", None)
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "large-copy")
        backup.shutil.copy2 = blocking_copy

        snapshot_thread = threading.Thread(
            target=backup.create_source_snapshot,
            args=(tmp, tmp / "snapshot", ["sensor.a", "sensor.b"], coordinator),
        )
        snapshot_thread.start()
        assert copy_started.wait(timeout=5)

        def use_other_entity() -> None:
            with coordinator.entity("sensor.b"):
                other_entity_acquired.set()

        other_thread = threading.Thread(target=use_other_entity)
        other_thread.start()
        assert other_entity_acquired.wait(timeout=1), "Backup blockiert eine andere Entität global"

        release_copy.set()
        snapshot_thread.join(timeout=5)
        other_thread.join(timeout=5)
        assert not snapshot_thread.is_alive()
        assert not other_thread.is_alive()
    finally:
        release_copy.set()
        backup.shutil.copy2 = original_copy2
        index.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_delete_backup_removes_only_valid_backup_filename() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        valid = tmp / "zeitarchiv-backup-2026-08-24-143000.zip"
        valid.write_bytes(b"zip")
        unrelated = tmp / "important.zip"
        unrelated.write_bytes(b"keep")

        assert backup.delete_backup(tmp, valid.name)
        assert not valid.exists()
        assert not backup.delete_backup(tmp, unrelated.name)
        assert not backup.delete_backup(tmp, "../important.zip")
        assert unrelated.read_bytes() == b"keep"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_create_backup_hashes_each_file_exactly_once() -> None:
    """create_backup() hasht jede Datei beim Schreiben für das Manifest —
    die interne validate_backup()-Prüfung direkt danach darf dieselben Bytes
    nicht ein zweites Mal hashen (siehe PERFORMANCE.md, ZP-009). zf.testzip()
    prüft die ZIP-eigene (billigere) CRC32-Summe weiterhin, das zählt hier
    nicht mit."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-hash-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        _write(tmp / "archive" / "sensor.a" / "2024-01.parquet", "parquet-a")
        _write(tmp / "rollup" / "sensor.a" / "monat.parquet", "rollup-a")

        import hashlib as hashlib_mod

        calls = []
        original_sha256 = hashlib_mod.sha256

        def counting_sha256(*args, **kwargs):
            calls.append(1)
            return original_sha256(*args, **kwargs)

        backup.hashlib.sha256 = counting_sha256
        try:
            dest = tmp / "out" / "backup.zip"
            backup.create_backup(tmp, dest)
        finally:
            backup.hashlib.sha256 = original_sha256

        assert len(calls) == backup.estimate_file_count(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validate_backup_without_checksums_still_catches_size_mismatch() -> None:
    """verify_checksums=False überspringt nur das SHA256-Nachrechnen, nicht
    den (praktisch kostenlosen) Abgleich der im ZIP hinterlegten Dateigröße
    gegen das Manifest."""
    import json as json_mod

    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-hash-test-"))
    try:
        _write(tmp / "index.sqlite", "sqlite-bytes")
        dest = tmp / "backup.zip"
        backup.create_backup(tmp, dest)

        with zipfile.ZipFile(dest) as zf:
            manifest = json_mod.loads(zf.read("zeitarchiv-manifest.json"))
            other_members = {
                name: zf.read(name) for name in zf.namelist() if name != "zeitarchiv-manifest.json"
            }
        for entry in manifest["files"]:
            if entry["path"] == "index.sqlite":
                entry["size_bytes"] += 1
        tampered = tmp / "tampered.zip"
        with zipfile.ZipFile(tampered, "w") as zf:
            for name, data in other_members.items():
                zf.writestr(name, data)
            zf.writestr("zeitarchiv-manifest.json", json_mod.dumps(manifest))

        try:
            backup.validate_backup(tampered, check_sqlite=False, verify_checksums=False)
        except ValueError:
            pass
        else:
            raise AssertionError("Falsche Dateigröße wurde trotz verify_checksums=False akzeptiert")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validate_backup_rejects_corruption() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-test-"))
    try:
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.commit()
        conn.close()
        dest = tmp / "backup.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)
        backup.validate_backup(dest)
        dest.write_bytes(dest.read_bytes()[:20])
        try:
            backup.validate_backup(dest)
        except ValueError:
            pass
        else:
            raise AssertionError("Beschädigtes Backup wurde akzeptiert")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prepared_restore_keeps_previous_state_as_rollback() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-restore-test-"))
    try:
        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('backup')")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)

        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("UPDATE sample SET value = 'current'")
        conn.commit()
        conn.close()
        backup.prepare_restore(tmp, backups_dir, dest.name)
        result = backup.apply_pending_restore(tmp, backups_dir)

        assert result and result["success"] is True
        restored = sqlite3.connect(tmp / "index.sqlite")
        assert restored.execute("SELECT value FROM sample").fetchone()[0] == "backup"
        restored.close()
        rollback = sqlite3.connect(tmp / result["rollback"] / "index.sqlite")
        assert rollback.execute("SELECT value FROM sample").fetchone()[0] == "current"
        rollback.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_restore_moves_away_stale_wal_sidecars() -> None:
    """Phase 1 von ROADMAP.md 1.14: Index läuft jetzt mit journal_mode=WAL,
    das legt index.sqlite-wal/-shm neben index.sqlite an. Blieben die beim
    Restore stehen, würde SQLite sie beim nächsten Öffnen der frisch
    eingespielten index.sqlite fälschlich hineinspielen wollen — sie müssen
    also genauso wie index.sqlite selbst ins Rollback-Verzeichnis wandern,
    nicht nur gelöscht werden (dieselbe "nie destruktiv"-Linie wie der Rest
    von apply_pending_restore())."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-restore-wal-test-"))
    try:
        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('backup')")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)

        # Stale WAL-Sidecars simulieren, wie sie ein echter Index._conn im
        # laufenden Betrieb neben index.sqlite anlegt.
        (tmp / "index.sqlite-wal").write_bytes(b"stale-wal")
        (tmp / "index.sqlite-shm").write_bytes(b"stale-shm")

        backup.prepare_restore(tmp, backups_dir, dest.name)
        result = backup.apply_pending_restore(tmp, backups_dir)

        assert result and result["success"] is True
        assert not (tmp / "index.sqlite-wal").exists()
        assert not (tmp / "index.sqlite-shm").exists()
        rollback_dir = tmp / result["rollback"]
        assert (rollback_dir / "index.sqlite-wal").read_bytes() == b"stale-wal"
        assert (rollback_dir / "index.sqlite-shm").read_bytes() == b"stale-shm"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_pending_restore_writes_restore_info_into_the_rollback() -> None:
    """Grundlage für list_restore_events() (Ausführungsverlauf auf der
    Backup-Seite, Konzept "Restore-Rollbacks", 22.09.2026): jedes neu
    angelegte Rollback-Verzeichnis trägt seine eigene Herkunft."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-restore-info-test-"))
    try:
        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)

        backup.prepare_restore(tmp, backups_dir, dest.name)
        result = backup.apply_pending_restore(tmp, backups_dir)

        assert result and result["success"] is True
        assert result["source"] == dest.name
        info_path = tmp / result["rollback"] / backup.RESTORE_INFO_NAME
        info = json.loads(info_path.read_text(encoding="utf-8"))
        assert info["source"] == dest.name
        assert info["installed_size_bytes"] > 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_rollback_can_itself_be_restored() -> None:
    """Bislang ließ sich ein Rollback nur löschen, nie tatsächlich
    einspielen. prepare_restore_from_rollback() + apply_pending_restore()
    holen den Stand zurück UND legen dabei selbst einen frischen Rollback
    des Zwischenstands an, bevor der alte Rollback (RESTORE_ROLLBACK_KEEP_
    COUNT == 1) automatisch wegfällt — dessen Inhalt ist zu dem Zeitpunkt
    aber bereits live, geht also nicht verloren."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-restore-test-"))
    try:
        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.execute("INSERT INTO sample VALUES ('original')")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)

        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("UPDATE sample SET value = 'first-restore'")
        conn.commit()
        conn.close()
        backup.prepare_restore(tmp, backups_dir, dest.name)
        first = backup.apply_pending_restore(tmp, backups_dir)
        assert first and first["success"] is True
        first_rollback_name = first["rollback"]

        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("UPDATE sample SET value = 'second-change'")
        conn.commit()
        conn.close()

        backup.prepare_restore_from_rollback(tmp, first_rollback_name)
        second = backup.apply_pending_restore(tmp, backups_dir)

        assert second and second["success"] is True
        assert second["rollback"] != first_rollback_name
        assert f"Rollback {first_rollback_name}" in second["source"]
        restored = sqlite3.connect(tmp / "index.sqlite")
        assert restored.execute("SELECT value FROM sample").fetchone()[0] == "first-restore"
        restored.close()
        newest_rollback = sqlite3.connect(tmp / second["rollback"] / "index.sqlite")
        assert newest_rollback.execute("SELECT value FROM sample").fetchone()[0] == "second-change"
        newest_rollback.close()
        # Der ursprüngliche Rollback ist weg (RESTORE_ROLLBACK_KEEP_COUNT == 1)
        # — kein Datenverlust, sein Inhalt ('first-restore') ist gerade erst
        # live gegangen, steht also ohnehin schon in der aktuellen Datenbank.
        assert not (tmp / first_rollback_name).is_dir()
        assert backup.list_restore_rollbacks(tmp) == [second["rollback"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prepare_restore_from_rollback_rejects_bad_names() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-guard-test-"))
    try:
        for bad in ("../etc", "not-a-rollback", ".zeitarchiv-restore-rollback-20260101T000000Z/../x"):
            try:
                backup.prepare_restore_from_rollback(tmp, bad)
                raise AssertionError(f"Ungültiger Name wurde akzeptiert: {bad}")
            except ValueError:
                pass
        # Formal gültiger Name, aber es gibt das Verzeichnis nicht.
        try:
            backup.prepare_restore_from_rollback(tmp, ".zeitarchiv-restore-rollback-20260101T000000Z")
            raise AssertionError("Nicht vorhandenes Rollback wurde akzeptiert")
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_list_restore_events_tolerates_a_rollback_without_restore_info() -> None:
    """Rollbacks, die vor restore-info.json entstanden sind, sollen trotzdem
    als Zeile im Ausführungsverlauf auftauchen — nur ohne bekannte Quelle,
    statt sie wegzulassen. Beide Rollbacks hier von Hand angelegt (statt über
    apply_pending_restore()), damit das automatische Pruning
    (RESTORE_ROLLBACK_KEEP_COUNT == 1) den älteren nicht gleich wieder
    entfernt — geprüft wird hier ausschließlich list_restore_events()."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-restore-events-test-"))
    try:
        old_style = tmp / ".zeitarchiv-restore-rollback-20260101T000000Z"
        old_style.mkdir(parents=True)

        new_style = tmp / ".zeitarchiv-restore-rollback-20260201T000000Z"
        new_style.mkdir(parents=True)
        (new_style / "index.sqlite").write_bytes(b"x" * 42)
        (new_style / backup.RESTORE_INFO_NAME).write_text(
            json.dumps({
                "source": "zeitarchiv-backup-2026-01-31-030000.zip",
                "installed_size_bytes": 42,
            }),
            encoding="utf-8",
        )

        events = backup.list_restore_events(tmp)
        assert len(events) == 2
        by_rollback = {e["rollback"]: e for e in events}
        assert by_rollback[old_style.name]["source"] is None
        assert by_rollback[new_style.name]["source"] == "zeitarchiv-backup-2026-01-31-030000.zip"
        assert by_rollback[new_style.name]["size_bytes"] == 42
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_restore_rollbacks_keeps_only_the_newest_one_by_default() -> None:
    """Jedes Rollback ist eine volle Kopie des Datenbestands — ein Rollback
    macht nur den LETZTEN Restore rückgängig, nicht mehr (Konzept
    "Restore-Rollbacks", 22.09.2026: alles Ältere holt man sich über ein
    echtes Backup zurück, nicht über eine Rollback-Kette), deshalb
    RESTORE_ROLLBACK_KEEP_COUNT == 1 statt konfigurierbar."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-prune-test-"))
    try:
        names = [
            f".zeitarchiv-restore-rollback-2026010{n}T000000Z" for n in range(1, 4)
        ]
        for name in names:
            (tmp / name).mkdir()

        removed = backup.prune_restore_rollbacks(tmp)

        assert removed == 2
        assert backup.list_restore_rollbacks(tmp) == [names[-1]]  # nur der jüngste
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_prune_restore_rollbacks_respects_an_explicit_keep_count() -> None:
    """keep_count bleibt als Parameter allgemein nutzbar, auch wenn
    apply_pending_restore() ihn immer mit dem Standardwert 1 aufruft."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-prune-explicit-test-"))
    try:
        names = [
            f".zeitarchiv-restore-rollback-2026010{n}T000000Z" for n in range(1, 6)
        ]
        for name in names:
            (tmp / name).mkdir()

        removed = backup.prune_restore_rollbacks(tmp, keep_count=3)

        assert removed == 2
        assert set(backup.list_restore_rollbacks(tmp)) == set(names[-3:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_apply_pending_restore_prunes_automatically() -> None:
    """Das Limit greift nicht nur isoliert (siehe oben), sondern auch am
    tatsächlichen Auslöser: jeder neu angelegte Rollback räumt selbst auf."""
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-autoprune-test-"))
    try:
        for n in range(1, 3):
            (tmp / f".zeitarchiv-restore-rollback-2026010{n}T000000Z").mkdir()
        assert len(backup.list_restore_rollbacks(tmp)) == 2

        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)
        backup.prepare_restore(tmp, backups_dir, dest.name)
        result = backup.apply_pending_restore(tmp, backups_dir)

        assert result and result["success"] is True
        assert backup.list_restore_rollbacks(tmp) == [result["rollback"]]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_list_restore_rollback_details_reports_size_and_timestamp() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-rollback-details-test-"))
    try:
        backups_dir = tmp / "backups"
        conn = sqlite3.connect(tmp / "index.sqlite")
        conn.execute("CREATE TABLE sample (value TEXT)")
        conn.commit()
        conn.close()
        dest = backups_dir / "zeitarchiv-backup-2026-08-24-143000.zip"
        backup.create_backup(tmp, dest, consistent_sqlite=True)
        backup.prepare_restore(tmp, backups_dir, dest.name)
        result = backup.apply_pending_restore(tmp, backups_dir)
        assert result and result["success"] is True

        details = backup.list_restore_rollback_details(tmp)
        assert len(details) == 1
        row = details[0]
        assert row["name"] == result["rollback"]
        assert row["size_bytes"] == (tmp / result["rollback"] / "index.sqlite").stat().st_size
        assert row["created_at"] is not None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_install_validated_backup_never_overwrites_existing_file() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-import-test-"))
    try:
        backups_dir = tmp / "backups"
        backups_dir.mkdir()
        existing = backups_dir / "zeitarchiv-backup-2026-08-24-120000.zip"
        existing.write_bytes(b"existing")
        upload = tmp / "validated-upload.zip"
        upload.write_bytes(b"uploaded")

        installed = backup.install_validated_backup(
            upload, backups_dir, datetime.fromisoformat("2026-08-24T12:00:00+02:00")
        )

        assert installed.name == "zeitarchiv-backup-2026-08-24-120001.zip"
        assert installed.read_bytes() == b"uploaded"
        assert existing.read_bytes() == b"existing"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_validate_backup_rejects_zip_bomb_ratio() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="zeitarchiv-backup-ratio-test-"))
    try:
        path = tmp / "compressed.zip"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("index.sqlite", b"0" * (1024 * 1024))
        try:
            backup.validate_backup(path)
        except ValueError as exc:
            assert "Kompressionsverhältnis" in str(exc)
        else:
            raise AssertionError("Extremes Kompressionsverhältnis wurde akzeptiert")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_all() -> None:
    tests = [obj for name, obj in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"\n{len(tests)} Tests bestanden.")


if __name__ == "__main__":
    _run_all()
