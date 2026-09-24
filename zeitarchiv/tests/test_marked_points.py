"""Housekeeping → Speicherplatz → "Markierte Datensätze anzeigen": der
zweistufige Dialog (erst betroffene Entitäten, dann deren einzelne
Markierungen samt Wert). Die reine Datenschicht (Index.get_deleted_points_by_
entity/list_deleted_points_for_entity, cleanup.read_values_for_timestamps)
deckt test_index.py/test_cleanup.py bereits ab — hier geht es nur um die
beiden Routen, die daraus die HTML-Fragmente für den Dialog bauen."""

from __future__ import annotations

import time


def test_the_first_level_lists_entities_not_individual_markers(client) -> None:
    from app.main import index

    index.get_or_create_entity("sensor.pytest_marked_a", "sensor", "measurement", "°C", friendly_name="Marked A")
    index.get_or_create_entity("sensor.pytest_marked_b", "sensor", "measurement", "W")
    index.mark_deleted("sensor.pytest_marked_a", [1.0, 2.0, 3.0])
    index.mark_deleted("sensor.pytest_marked_b", [1.0])

    html = client.get("/settings/purge/marked").text
    tabelle = html[html.index("<tbody>"):html.index("</tbody>")]
    assert "Marked A" in tabelle
    assert "sensor.pytest_marked_b" in tabelle
    # Keine einzelnen Zeitstempel auf dieser Ebene — nur die Zusammenfassung.
    assert 'hx-get="settings/purge/marked/sensor.pytest_marked_a"' in tabelle
    # Die ganze Zeile ist klickbar, kein Button um den Entitätsnamen.
    assert 'class="job-row-nav"' in tabelle
    assert "<button" not in tabelle


def test_the_search_filters_the_entity_list(client) -> None:
    from app.main import index

    index.get_or_create_entity("sensor.pytest_marked_search", "sensor", "measurement", "°C", friendly_name="Suchbar")
    index.mark_deleted("sensor.pytest_marked_search", [1.0])

    treffer = client.get("/settings/purge/marked?search=suchbar").text
    tabelle = treffer[treffer.index("<tbody>"):treffer.index("</tbody>")]
    assert "sensor.pytest_marked_search" in tabelle

    kein_treffer = client.get("/settings/purge/marked?search=xyz-nichts-passt").text
    assert "Keine passenden markierten Entitäten gefunden." in kein_treffer


def test_the_second_level_shows_the_value_of_each_marker(client) -> None:
    from app.main import DATA_DIR, TZ, index
    from app.storage import hotbuffer

    entity_id = "sensor.pytest_marked_value"
    index.get_or_create_entity(entity_id, "sensor", "measurement", "°C", friendly_name="Mit Wert")
    now = time.time()
    hotbuffer.append(DATA_DIR, entity_id, now, 21.437, TZ)
    index.mark_deleted(entity_id, [now])
    missing_ts = now - 10  # markiert, aber nie als Rohwert geschrieben
    index.mark_deleted(entity_id, [missing_ts])

    html = client.get(f"/settings/purge/marked/{entity_id}").text
    assert "Mit Wert" in html
    assert "data-sortable" in html  # Spalten klickbar sortierbar
    # BEWUSST kein data-paginate: die Liste ist schon serverseitig paginiert
    # (eigener Pager unten). sortable-table.js legt beim Sortieren sonst einen
    # zweiten, ungefragten Pager an (siehe dessen updatePager()-Aufruf im
    # Sortier-Klick-Handler) — ohne dieses Attribut bleibt es beim einen.
    assert "data-paginate" not in html
    tabelle = html[html.index("<tbody>"):html.index("</tbody>")]
    assert "21,437" in tabelle
    assert "°C" in tabelle
    assert "—" in tabelle  # der Zeitstempel ohne Rohdaten-Treffer
    assert f'data-sort="{now}"' in tabelle
    # "Zurück" führt wieder auf die erste Ebene.
    assert 'hx-get="settings/purge/marked"' in html


def test_the_second_level_for_an_unknown_entity_is_a_404(client) -> None:
    response = client.get("/settings/purge/marked/sensor.does_not_exist_at_all")
    assert response.status_code == 404


def test_the_first_level_paginates_at_20_per_page_by_default(client) -> None:
    from app.main import index

    for i in range(25):
        entity_id = f"sensor.pytest_marked_page_{i:02d}"
        index.get_or_create_entity(entity_id, "sensor", "measurement", "°C")
        index.mark_deleted(entity_id, [1.0])

    # Suche auf das eigene Präfix eingeschränkt — der client-Fixture teilt
    # den Index mit den anderen Tests dieser Datei, ohne Filter wären deren
    # Entitäten mit in der Zählung/Sortierung.
    erste_seite = client.get("/settings/purge/marked?search=pytest_marked_page_").text
    tabelle = erste_seite[erste_seite.index("<tbody>"):erste_seite.index("</tbody>")]
    # Sortiert nach Markierungsanzahl (gleich hier) dann entity_id ASC — die
    # ersten 20 von 25 landen alphabetisch auf Seite 1.
    assert "sensor.pytest_marked_page_00" in tabelle
    assert "sensor.pytest_marked_page_19" in tabelle
    assert "sensor.pytest_marked_page_20" not in tabelle
    assert "1&ndash;20 von 25" in erste_seite
    assert 'value="20"' in erste_seite  # page_size-Auswahl steht auf 20

    zweite_seite = client.get("/settings/purge/marked?search=pytest_marked_page_&page=2").text
    tabelle_2 = zweite_seite[zweite_seite.index("<tbody>"):zweite_seite.index("</tbody>")]
    assert "sensor.pytest_marked_page_20" in tabelle_2
    assert "sensor.pytest_marked_page_24" in tabelle_2
    assert "21&ndash;25 von 25" in zweite_seite
