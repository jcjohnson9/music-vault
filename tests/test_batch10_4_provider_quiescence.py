from __future__ import annotations

import json
import shutil
import threading
import time
from pathlib import Path

from music_vault.app import MusicVaultWindow
from music_vault.core.runtime_policy import RuntimePolicy
from music_vault.metadata.artist_images import (
    ArtistIdentity,
    ArtistImageCache,
    ArtistImageResult,
    ArtistImageService,
    ArtistImageStatus,
    DisabledArtistImageProvider,
    SyntheticArtistImageProvider,
    create_artist_image_provider,
)


def _wait_for(qapp, condition, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.01)
    assert condition()


def test_artist_provider_factory_blocks_before_token_or_transport(
    monkeypatch,
) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("blocked startup constructed a transport or read a token")

    monkeypatch.setattr(
        "music_vault.metadata.artist_images.MusicBrainzWikimediaProvider",
        forbidden,
    )
    monkeypatch.setattr(
        "music_vault.metadata.intelligence_settings.DiscogsTokenStore.read",
        forbidden,
    )

    for policy in (
        RuntimePolicy(acceptance_no_secrets=True),
        RuntimePolicy(acceptance_no_network=True),
        RuntimePolicy(migration_performed=True),
    ):
        provider = create_artist_image_provider(runtime_policy=policy)
        assert isinstance(provider, DisabledArtistImageProvider)


def test_lazy_artist_service_uses_cache_while_blocked_without_factory(
    tmp_path,
    qapp,
) -> None:
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Synthetic Cached Identity")
    cached = cache.store(SyntheticArtistImageProvider().resolve(identity))
    before = cache.index_path.read_bytes()
    factory_calls = 0

    def forbidden_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("blocked cache lookup constructed a provider")

    service = ArtistImageService(None, cache, provider_factory=forbidden_factory)
    results = []
    assert service.request(identity, results.append, network_enabled=False)
    _wait_for(qapp, lambda: len(results) == 1)

    assert results[0].status is ArtistImageStatus.RESOLVED
    assert results[0].cache_file == cached.cache_file
    assert results[0].from_cache is True
    assert factory_calls == 0
    assert cache.index_path.read_bytes() == before
    service.shutdown()


def test_blocked_missing_portrait_creates_no_negative_cache_or_provider(
    tmp_path,
    qapp,
) -> None:
    cache = ArtistImageCache(tmp_path / "artist_images")
    factory_calls = 0

    def forbidden_factory():
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("blocked miss constructed a provider")

    service = ArtistImageService(None, cache, provider_factory=forbidden_factory)
    results = []
    assert service.request(
        "Synthetic Missing Identity",
        results.append,
        network_enabled=False,
    )
    _wait_for(qapp, lambda: len(results) == 1)

    assert results[0].status is ArtistImageStatus.DISABLED
    assert factory_calls == 0
    assert not cache.index_path.exists()
    assert not cache.root.exists()
    service.shutdown()


def test_blocked_invalid_cache_entry_is_not_repaired_or_rewritten(
    tmp_path,
    qapp,
) -> None:
    cache = ArtistImageCache(tmp_path / "artist_images")
    identity = ArtistIdentity.from_display_name("Synthetic Invalid Identity")
    cache.root.mkdir(parents=True)
    cache.index_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "entries": {
                    cache._entry_key(identity): {
                        "status": "unexpected",
                        "normalized_key": identity.normalized_key,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    before = cache.index_path.read_bytes()
    service = ArtistImageService(
        None,
        cache,
        provider_factory=lambda: (_ for _ in ()).throw(
            AssertionError("blocked invalid cache lookup constructed a provider")
        ),
    )
    results = []
    assert service.request(identity, results.append, network_enabled=False)
    _wait_for(qapp, lambda: len(results) == 1)

    assert results[0].status is ArtistImageStatus.DISABLED
    assert cache.index_path.read_bytes() == before
    service.shutdown()


def test_ordinary_artist_service_constructs_provider_lazily_and_deduplicates(
    tmp_path,
    qapp,
) -> None:
    class Provider:
        def __init__(self) -> None:
            self.calls = 0
            self.release = threading.Event()

        def resolve(self, identity, cancel_event=None):
            del cancel_event
            self.calls += 1
            assert self.release.wait(2)
            return ArtistImageResult(ArtistImageStatus.NO_MATCH, identity)

    provider = Provider()
    factory_calls = 0

    def factory():
        nonlocal factory_calls
        factory_calls += 1
        return provider

    service = ArtistImageService(
        None,
        ArtistImageCache(tmp_path / "artist_images"),
        provider_factory=factory,
    )
    assert factory_calls == 0
    results = []
    assert service.request("Synthetic Ordinary Identity", results.append)
    assert not service.request(" synthetic ordinary identity ", results.append)
    provider.release.set()
    _wait_for(qapp, lambda: len(results) == 2)

    assert factory_calls == 1
    assert provider.calls == 1
    service.shutdown()


def test_migration_and_acceptance_startup_do_not_wake_metadata_jobs() -> None:
    class Deferred:
        def __init__(self, policy: RuntimePolicy) -> None:
            self.runtime_policy = policy

        @property
        def config(self):
            raise AssertionError("deferred wake inspected provider configuration")

    for policy in (
        RuntimePolicy(migration_performed=True),
        RuntimePolicy(acceptance_no_network=True),
        RuntimePolicy(acceptance_no_secrets=True),
    ):
        assert MusicVaultWindow.wake_metadata_intelligence(Deferred(policy)) is None


def test_next_ordinary_launch_resumes_metadata_wake() -> None:
    submissions = []

    class Tasks:
        pending_count = 0

        def submit(self, kind, callback):
            submissions.append((kind, callback))

    class Service:
        def process_automatic_queue(self, *, cancel_event):
            return cancel_event

    class Allowed:
        runtime_policy = RuntimePolicy()
        config = {
            "metadata_intelligence_enabled": True,
            "metadata_intelligence_consent_version": 1,
        }
        metadata_intelligence_tasks = Tasks()
        metadata_intelligence_service = Service()

    MusicVaultWindow.wake_metadata_intelligence(Allowed())
    assert [kind for kind, _callback in submissions] == ["metadata_automatic_imports"]


def test_manual_portrait_and_sync_dispatch_stop_at_policy_boundary() -> None:
    deferred_messages = []

    class Deferred:
        runtime_policy = RuntimePolicy(migration_performed=True)

        _provider_work_allowed = MusicVaultWindow._provider_work_allowed

        def _show_provider_deferred(self):
            deferred_messages.append(True)

        @property
        def config(self):
            raise AssertionError("blocked action inspected or changed configuration")

    harness = Deferred()
    MusicVaultWindow.refresh_artist_photo(harness, "synthetic-key")
    MusicVaultWindow.refresh_missing_artist_photos(harness)
    MusicVaultWindow.sync_youtube_playlist(harness)

    assert deferred_messages == [True, True, True]


def test_runtime_lyrics_settings_are_process_local() -> None:
    configured = {
        "party_mode_lyrics_enabled": True,
        "lyrics_online_lookup_enabled": True,
    }

    class Harness:
        def __init__(self, policy: RuntimePolicy) -> None:
            self.runtime_policy = policy

        _provider_work_allowed = MusicVaultWindow._provider_work_allowed

    blocked = MusicVaultWindow._runtime_lyrics_settings(
        Harness(RuntimePolicy(migration_performed=True)),
        configured,
    )
    allowed = MusicVaultWindow._runtime_lyrics_settings(
        Harness(RuntimePolicy()),
        configured,
    )

    assert blocked["lyrics_online_lookup_enabled"] is False
    assert allowed["lyrics_online_lookup_enabled"] is True
    assert configured["lyrics_online_lookup_enabled"] is True


def test_window_startup_in_acceptance_mode_reads_no_secrets_or_providers(
    tmp_path,
    monkeypatch,
    qapp,
) -> None:
    from music_vault import app as app_module
    from music_vault.core import paths
    from music_vault.metadata.intelligence_settings import DiscogsTokenStore
    from music_vault.ui.icons import clear_icon_cache

    root = tmp_path / "synthetic runtime"
    data = root / "data"
    downloads = data / "youtube_downloads"
    downloads.mkdir(parents=True)
    (root / "music_vault").mkdir()
    (root / "run.py").write_text("# synthetic project marker\n", encoding="utf-8")
    source_icons = Path(__file__).resolve().parents[1] / "assets" / "icons"
    shutil.copytree(source_icons, root / "assets" / "icons")
    (data / "music_vault_config.json").write_text(
        json.dumps(
            {
                "download_folder": str(downloads),
                "onboarding_completed": True,
                "artist_image_fetch_enabled": True,
                "metadata_intelligence_enabled": True,
                "metadata_intelligence_consent_version": 1,
                "metadata_discogs_enabled": True,
                "metadata_discogs_consent_version": 1,
            }
        ),
        encoding="utf-8",
    )
    api_key = data / "youtube_api_key.txt"
    discogs_token = data / "discogs_token.txt"
    api_key.write_text("synthetic-secret-marker", encoding="utf-8")
    discogs_token.write_text("synthetic-secret-marker", encoding="utf-8")

    monkeypatch.setenv("MUSIC_VAULT_PROJECT_ROOT", str(root))
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_SECRETS", "1")
    monkeypatch.setenv("MUSIC_VAULT_ACCEPTANCE_NO_NETWORK", "1")
    monkeypatch.setattr(paths, "_configured_data_directory", None)
    paths._resolved_project_root.cache_clear()
    clear_icon_cache()

    original_read_text = Path.read_text

    def guarded_read_text(path, *args, **kwargs):
        if Path(path) in {api_key, discogs_token}:
            raise AssertionError("acceptance startup read secret file content")
        return original_read_text(path, *args, **kwargs)

    def forbidden_token_read(*_args, **_kwargs):
        raise AssertionError("acceptance startup read the Discogs token")

    def forbidden_provider(*_args, **_kwargs):
        raise AssertionError("acceptance startup constructed an artist provider")

    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(DiscogsTokenStore, "read", forbidden_token_read)
    monkeypatch.setattr(
        app_module,
        "create_artist_image_provider",
        forbidden_provider,
    )
    monkeypatch.setattr(
        app_module.MusicVaultWindow,
        "use_system_default_audio_output",
        lambda self: None,
    )
    monkeypatch.setattr(app_module, "apply_dark_title_bar", lambda _window: False)

    window = app_module.MusicVaultWindow()
    try:
        qapp.processEvents()
        assert window.runtime_policy.startup_provider_work_deferred is True
        assert window.artist_image_service.provider is None
        payload = json.loads((data / "music_vault_status.json").read_text(encoding="utf-8"))
        assert payload["health"]["api_ready"] is False
        assert payload["discogs_ready"] is False
        assert payload["provider_work_deferred"] is True
        assert payload["provider_work_defer_reason"] == "acceptance_no_network"
    finally:
        window.audio_device_timer.stop()
        window.volume_save_timer.stop()
        window.close()
        window.db.close()
        window.deleteLater()
        qapp.processEvents()
        paths._resolved_project_root.cache_clear()
        clear_icon_cache()
